"""验证 FastAPI sidecar 的启动布局与基础只读 API。"""

import shutil
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main
from core.settings import Settings
from db import migrations as migration_module
from db.jobs import JobRepository
from db.sqlite import initialize_database
from main import create_app
from modules.model_catalog.secrets import SecretStoreOperationError

MIGRATIONS_DIR = Path(__file__).parents[1] / "db" / "migrations"


class RecordingSecretStore:
    """记录 create_app 传入 Keyring adapter 的 service namespace。"""

    def __init__(self, service_name: str) -> None:
        """保存被注入的 service namespace。"""

        self.service_name = service_name

    def set_secret(self, reference: str, secret: str) -> None:
        """测试不执行写入, 仅满足 SecretStore contract。"""

    def get_secret(self, reference: str) -> str | None:
        """测试没有预置 secret。"""

        return None

    def delete_secret(self, reference: str) -> bool:
        """测试没有可删除的 secret。"""

        return False


class ReconciliationSecretStore:
    """记录 startup reconciliation 读取与删除行为的内存 Keychain fake。"""

    def __init__(self, values: dict[str, str]) -> None:
        """复制初始 secrets, 并创建可控的一次性删除故障集合。"""

        self.values = dict(values)
        self.get_calls: list[str] = []
        self.delete_calls: list[str] = []
        self.fail_delete_once: set[str] = set()

    def set_secret(self, reference: str, secret: str) -> None:
        """保存 reconciliation 之外可能发生的 secret 写入。"""

        self.values[reference] = secret

    def get_secret(self, reference: str) -> str | None:
        """记录并返回一个 opaque reference 的 secret。"""

        self.get_calls.append(reference)
        return self.values.get(reference)

    def delete_secret(self, reference: str) -> bool:
        """幂等删除 secret, 或按测试设置模拟一次失败。"""

        self.delete_calls.append(reference)
        if reference in self.fail_delete_once:
            self.fail_delete_once.remove(reference)
            raise SecretStoreOperationError("synthetic keychain delete failure")
        return self.values.pop(reference, None) is not None


class NoAccessSecretStore:
    """在无 reconciliation 候选时拒绝任何意外 Keychain 操作。"""

    def set_secret(self, reference: str, secret: str) -> None:
        """拒绝意外写入。"""

        raise AssertionError((reference, secret))

    def get_secret(self, reference: str) -> str | None:
        """拒绝意外读取。"""

        raise AssertionError(reference)

    def delete_secret(self, reference: str) -> bool:
        """拒绝意外删除。"""

        raise AssertionError(reference)


def _prepare_database_before_provider_dedup(
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    rows: list[tuple[str, str, str, str, str]],
) -> Path:
    """创建停在 0011 的数据库, 插入旧 schema 允许的重复 provider rows。"""

    database = data_dir / "pageferry.sqlite3"
    staged_migrations = data_dir / "staged-migrations"
    staged_migrations.mkdir(parents=True)
    for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if migration.name >= "0012_unique_provider_configs.sql":
            continue
        shutil.copy2(migration, staged_migrations / migration.name)
    monkeypatch.setattr(migration_module, "MIGRATIONS_DIR", staged_migrations)
    initialize_database(database)

    with sqlite3.connect(database) as connection:
        for row_id, provider_id, secret_ref, created_at, updated_at in rows:
            connection.execute(
                """
                INSERT INTO provider_configs (
                    id, provider_id, model_id, default_model_id, base_url,
                    catalog_version, secret_ref, probe_status, created_at, updated_at
                )
                VALUES (
                    ?, ?, 'legacy-model', 'legacy-model', 'https://provider.example/v1',
                    'legacy', ?, 'succeeded', ?, ?
                )
                """,
                (row_id, provider_id, secret_ref, created_at, updated_at),
            )

    for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if migration.name >= "0012_unique_provider_configs.sql":
            shutil.copy2(migration, staged_migrations / migration.name)
    return database


def test_startup_creates_local_data_layout_and_database(tmp_path) -> None:
    """首次启动应创建 app 目录、数据库与可用健康检查。"""

    app = create_app(Settings(data_dir=tmp_path))

    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {
        "code": "success",
        "data": {"service": "pageferry-api", "version": "0.1.0"},
    }
    for directory in ("workspace", "outputs", "models", "cache", "logs"):
        assert (tmp_path / directory).is_dir()

    with sqlite3.connect(tmp_path / "pageferry.sqlite3") as connection:
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'translation_jobs'"
        ).fetchone()
    assert table == ("translation_jobs",)


def test_provider_configure_openapi_declares_conflict_and_storage_errors(tmp_path) -> None:
    """Provider OpenAPI 必须声明各 read/write 路径可返回的结构化错误。"""

    app = create_app(
        Settings(data_dir=tmp_path),
        secret_store=NoAccessSecretStore(),
    )

    paths = app.openapi()["paths"]
    responses = paths["/api/v1/providers/{provider_id}"]["put"]["responses"]

    assert "409" in responses
    assert "500" in responses
    assert "500" in paths["/api/v1/providers"]["get"]["responses"]
    discovery_responses = paths["/api/v1/providers/{provider_id}/models/discover"]["post"][
        "responses"
    ]
    assert "404" in discovery_responses
    assert "500" in discovery_responses
    probe_responses = paths["/api/v1/providers/{provider_id}/probe"]["post"]["responses"]
    assert probe_responses["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ProviderProbeResponse"
    }
    assert {"400", "401", "404", "429", "500", "502", "503"}.issubset(probe_responses)


def test_provider_protected_openapi_declares_actual_unauthorized_envelopes(tmp_path) -> None:
    """401 schema 应区分纯 boot-token 路径与也会返回 service key 错误的路径。"""

    app = create_app(
        Settings(data_dir=tmp_path),
        secret_store=NoAccessSecretStore(),
    )

    paths = app.openapi()["paths"]
    schemas = app.openapi()["components"]["schemas"]
    boot_token_schema = {"$ref": "#/components/schemas/BootTokenErrorResponse"}
    unauthorized_schema = {"$ref": "#/components/schemas/ProviderUnauthorizedResponse"}
    assert schemas["ProviderUnauthorizedResponse"] == {
        "anyOf": [
            boot_token_schema,
            {"$ref": "#/components/schemas/ProviderErrorResponse"},
        ]
    }

    boot_token_only_operations = (
        paths["/api/v1/providers/custom"]["post"],
        paths["/api/v1/providers/{provider_id}/api-key"]["get"],
        paths["/api/v1/providers/{provider_id}/models"]["post"],
        paths["/api/v1/providers/{provider_id}/active"]["put"],
        paths["/api/v1/providers/{provider_id}/models/{model_id}/settings"]["put"],
        paths["/api/v1/providers/{provider_id}"]["delete"],
    )
    dual_envelope_operations = (
        paths["/api/v1/providers/{provider_id}"]["put"],
        paths["/api/v1/providers/{provider_id}/probe"]["post"],
        paths["/api/v1/providers/{provider_id}/models/discover"]["post"],
        paths["/api/v1/providers/{provider_id}/models/sync"]["post"],
        paths["/api/v1/providers/{provider_id}/models/{model_id}/enabled"]["put"],
    )

    for operation in boot_token_only_operations:
        assert operation["responses"]["401"]["content"]["application/json"]["schema"] == (
            boot_token_schema
        )
    for operation in dual_envelope_operations:
        assert operation["responses"]["401"]["content"]["application/json"]["schema"] == (
            unauthorized_schema
        )

    api_key_success = paths["/api/v1/providers/{provider_id}/api-key"]["get"]["responses"]["200"]
    assert set(api_key_success["headers"]) == {"Cache-Control", "Pragma", "Expires"}


def test_startup_without_reconciliation_candidates_never_reads_keychain(tmp_path) -> None:
    """全新或已完成 reconciliation 的数据库启动时不得触发 Keychain。"""

    app = create_app(
        Settings(data_dir=tmp_path),
        secret_store=NoAccessSecretStore(),
    )

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200


def test_startup_reconciles_duplicate_provider_secret_references(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """winner 缺 Key 时接管最高优先级 loser, 并删除未被引用的其余 secrets。"""

    deep_winner = "keychain:provider/deepseek-winner"
    deep_fallback = "keychain:provider/deepseek-fallback"
    deep_extra = "keychain:provider/deepseek-extra"
    kimi_winner = "keychain:provider/kimi-winner"
    kimi_loser = "keychain:provider/kimi-loser"
    database = _prepare_database_before_provider_dedup(
        tmp_path,
        monkeypatch,
        [
            (
                "deepseek-winner",
                "deepseek",
                deep_winner,
                "2026-01-01T00:00:00Z",
                "2026-01-03T00:00:00Z",
            ),
            (
                "deepseek-fallback",
                "deepseek",
                deep_fallback,
                "2026-01-01T00:00:00Z",
                "2026-01-02T00:00:00Z",
            ),
            (
                "deepseek-extra",
                "deepseek",
                deep_extra,
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
            ),
            (
                "kimi-winner",
                "kimi",
                kimi_winner,
                "2026-01-01T00:00:00Z",
                "2026-01-03T00:00:00Z",
            ),
            (
                "kimi-loser",
                "kimi",
                kimi_loser,
                "2026-01-01T00:00:00Z",
                "2026-01-02T00:00:00Z",
            ),
        ],
    )
    secret_store = ReconciliationSecretStore(
        {
            deep_fallback: "deep-fallback-key",
            deep_extra: "deep-extra-key",
            kimi_winner: "kimi-winner-key",
            kimi_loser: "kimi-loser-key",
        }
    )
    app = create_app(Settings(data_dir=tmp_path), secret_store=secret_store)

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200

    with sqlite3.connect(database) as connection:
        references = connection.execute(
            "SELECT provider_id, secret_ref FROM provider_configs ORDER BY provider_id"
        ).fetchall()
        staged_count = connection.execute(
            "SELECT COUNT(*) FROM provider_secret_reconciliation_candidates"
        ).fetchone()
    assert references == [
        ("deepseek", deep_fallback),
        ("kimi", kimi_winner),
    ]
    assert staged_count == (0,)
    assert secret_store.values == {
        deep_fallback: "deep-fallback-key",
        kimi_winner: "kimi-winner-key",
    }
    assert deep_winner in secret_store.delete_calls
    assert deep_extra in secret_store.delete_calls
    assert kimi_loser in secret_store.delete_calls


def test_failed_secret_cleanup_keeps_staging_for_next_startup(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Keychain cleanup 中断后保留 staging, 下一次启动应幂等完成。"""

    winner_ref = "keychain:provider/deepseek-winner"
    loser_ref = "keychain:provider/deepseek-loser"
    database = _prepare_database_before_provider_dedup(
        tmp_path,
        monkeypatch,
        [
            (
                "deepseek-winner",
                "deepseek",
                winner_ref,
                "2026-01-01T00:00:00Z",
                "2026-01-03T00:00:00Z",
            ),
            (
                "deepseek-loser",
                "deepseek",
                loser_ref,
                "2026-01-01T00:00:00Z",
                "2026-01-02T00:00:00Z",
            ),
        ],
    )
    secret_store = ReconciliationSecretStore({winner_ref: "winner-key", loser_ref: "loser-key"})
    secret_store.fail_delete_once.add(loser_ref)
    first_app = create_app(Settings(data_dir=tmp_path), secret_store=secret_store)

    with TestClient(first_app) as client:
        assert client.get("/healthz").status_code == 200

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM provider_secret_reconciliation_candidates"
        ).fetchone() == (2,)
    assert secret_store.values == {winner_ref: "winner-key", loser_ref: "loser-key"}
    assert [record.message for record in caplog.records if record.name == "main"] == [
        "Provider credential reconciliation is pending; retrying next startup."
    ]
    assert winner_ref not in caplog.text
    assert loser_ref not in caplog.text

    second_app = create_app(Settings(data_dir=tmp_path), secret_store=secret_store)
    with TestClient(second_app) as client:
        assert client.get("/healthz").status_code == 200

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM provider_secret_reconciliation_candidates"
        ).fetchone() == (0,)
    assert secret_store.values == {winner_ref: "winner-key"}


def test_startup_interrupts_queued_and_running_jobs_without_a_durable_worker(tmp_path) -> None:
    """startup 必须终止进程内执行器无法恢复的 queued 与 running 任务。"""

    database = tmp_path / "pageferry.sqlite3"
    initialize_database(database)
    repository = JobRepository(database)
    for job_id in ("queued-job", "running-job"):
        repository.create(
            job_id=job_id,
            source_path=tmp_path / f"{job_id}.txt",
            source_name=f"{job_id}.txt",
            document_type="txt",
            provider_id="deepseek",
            model_id="deepseek-v4-flash",
            source_language=None,
            target_language="zh-CN",
        )
    repository.mark_running("running-job")

    app = create_app(Settings(data_dir=tmp_path))
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200

    assert repository.get("queued-job").error_code == "process_interrupted"
    assert repository.get("running-job").error_code == "process_interrupted"


def test_model_catalog_is_versioned_and_contains_bootstrap_providers(tmp_path) -> None:
    """catalog 应带版本并列出首期调研过的 provider preset。"""

    app = create_app(Settings(data_dir=tmp_path))

    with TestClient(app) as client:
        response = client.get("/api/v1/model-catalog")

    assert response.status_code == 200
    catalog = response.json()
    assert catalog["schema_version"] == 1
    assert catalog["catalog_version"] == "0.2.0-dev"
    assert {provider["id"] for provider in catalog["providers"]} >= {
        "openai",
        "gemini",
        "custom_openai",
    }


def test_create_app_applies_configured_secret_service_namespace(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """create_app 应把 settings 中的 namespace 交给 Keyring adapter。"""

    created_stores: list[RecordingSecretStore] = []

    def create_secret_store(*, service_name: str) -> RecordingSecretStore:
        """构造并记录应用创建的 SecretStore。"""

        store = RecordingSecretStore(service_name)
        created_stores.append(store)
        return store

    monkeypatch.setattr(main, "KeyringSecretStore", create_secret_store)

    app = create_app(
        Settings(
            data_dir=tmp_path,
            secret_service_name="com.pageferry.provider-secrets.integration-test",
        )
    )

    assert app.state.settings.secret_service_name == (
        "com.pageferry.provider-secrets.integration-test"
    )
    assert [store.service_name for store in created_stores] == [
        "com.pageferry.provider-secrets.integration-test"
    ]
