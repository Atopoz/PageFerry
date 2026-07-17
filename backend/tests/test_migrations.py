"""验证 SQLite migration 不可变、可重复执行、兼容旧库且不保存 credential。"""

import hashlib
import shutil
import sqlite3
from pathlib import Path

import pytest

from db import migrations as migration_module
from db.sqlite import initialize_database

MIGRATIONS_DIR = Path(__file__).parents[1] / "db" / "migrations"
PUBLISHED_MIGRATION_CHECKSUMS = {
    "0001_initial.sql": "a0fd90cb32e6fc30ea0b862f85aba735a7ed07f73f7cfee2e0637b9a0af6ce4b",
    "0002_expand_document_types.sql": (
        "a3f4a6d1f7ae5db08ceac543f058f7140a795d9f1d0e0b36fd77026cb4b3c6e6"
    ),
    "0003_provider_configs.sql": (
        "723aa0dbd67415574813a055db5bfc799acabc21e7d11a4a3240136f7244fed0"
    ),
    "0004_job_results.sql": "1cdaee4faf39db5fb98bf7270877abc5c6d830b9a23c2284db03b93fc2e7b91a",
    "0005_provider_probe_latency.sql": (
        "7c1fd07b3a8cd9a9bef4a11768daa791fc818a4044ec808b4db14939144b0863"
    ),
    "0006_job_languages.sql": "2e964e04bd313a199d5e7286043e37f8340f77c9fc66c546cf9f4fcc4547d1e4",
    "0007_sanitize_job_source_names.sql": (
        "71c46a505a4c5e92007a7809188024613cb7dfae2040142e519a3026ea6b487f"
    ),
    "0008_job_options.sql": "b865fd6c1527cdacf8d686eaa7f4ed0b4a84f0921a6711679fe2fc26fd998a7f",
    "0009_provider_model_inventory.sql": (
        "f53a3fbf9175efc1b22582038283beeedf3845d56911712d1c7dd66abcc1ff0d"
    ),
    "0010_rename_discovered_model_source.sql": (
        "868c8b8c47664c0afe60345e31fe2f0fa2f171c2b394249577ee1b2d646c651a"
    ),
    "0011_custom_openai_providers.sql": (
        "4833fd7753e3b31ce0fc5232a3f6cd6221657d3e003f503239bb023396c66b45"
    ),
    "0012_unique_provider_configs.sql": (
        "e653b049c634d119287c39f45367bbba363b47f676f86953d071c7927b13698f"
    ),
    "0013_provider_secret_reconciliation.sql": (
        "4117b95b910cc538ec5904dffe47b953150f07e8105a63f9d24a807780bf70be"
    ),
    "0014_provider_base_url_overrides.sql": (
        "7bb8838b1bf7d3ab12bb79b040bf04a79ea33536efe5e14800ca87b4dfaddc3c"
    ),
    "0015_job_stage_progress.sql": (
        "e84f5076e8891c15dbe0d9e6ae21f47de4e33d76c857b81de77537dea2002866"
    ),
    "0016_rename_job_progress_stages.sql": (
        "1a950b321687f54a539db4fe376d1538262c2f72f7ecf3ee93c4bc2770672690"
    ),
    "0017_provider_model_runtime_settings.sql": (
        "2470bc61ec58c271d41ee834f057523bb3c1ac4f953376c40ec08799785b3180"
    ),
    "0018_manual_provider_models.sql": (
        "dfa508f5d235abf993316f4503a0ea94d43447d25ed9a44271aaad09be063ead"
    ),
    "0019_provider_active_state.sql": (
        "9a1a6a55812e63e1c0f32a09df3ee39e4f2c3648482671baaee035b1f328fa25"
    ),
}

LEGACY_SCHEMA = """
CREATE TABLE translation_jobs (
    id TEXT PRIMARY KEY,
    source_path TEXT NOT NULL,
    output_path TEXT,
    document_type TEXT NOT NULL CHECK (document_type IN ('docx', 'pptx', 'pdf')),
    status TEXT NOT NULL CHECK (
        status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')
    ),
    progress INTEGER NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
    provider_id TEXT,
    model_id TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_translation_jobs_created_at
ON translation_jobs(created_at DESC);
"""


def _insert_job(
    connection: sqlite3.Connection,
    job_id: str,
    document_type: str,
    *,
    source_path: str | None = None,
) -> None:
    """向旧 schema 插入最小任务, 模拟升级前的真实数据。"""

    connection.execute(
        """
        INSERT INTO translation_jobs (
            id, source_path, document_type, status, created_at, updated_at
        )
        VALUES (?, ?, ?, 'queued', '2026-07-16T00:00:00Z', '2026-07-16T00:00:00Z')
        """,
        (job_id, source_path or f"/tmp/{job_id}.{document_type}", document_type),
    )


def test_published_migration_checksums_are_stable() -> None:
    """已经进入开发数据库的 migration 不得被原地改写。"""

    actual = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in MIGRATIONS_DIR.glob("*.sql")
        if path.name in PUBLISHED_MIGRATION_CHECKSUMS
    }

    assert actual == PUBLISHED_MIGRATION_CHECKSUMS


def test_migrations_upgrade_legacy_database_without_losing_jobs(tmp_path) -> None:
    """旧 DOCX 任务应保留, 新 schema 还应接受 TXT 与 Markdown。"""

    database = tmp_path / "pageferry.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(LEGACY_SCHEMA)
        _insert_job(connection, "legacy-job", "docx")
        _insert_job(
            connection,
            "windows-job",
            "pptx",
            source_path=r"C:\Users\alice\Private\slides.pptx",
        )

    initialize_database(database)
    initialize_database(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT id, document_type, source_name FROM translation_jobs WHERE id = 'legacy-job'"
        ).fetchone() == ("legacy-job", "docx", "legacy-job.docx")
        assert connection.execute(
            "SELECT source_name FROM translation_jobs WHERE id = 'windows-job'"
        ).fetchone() == ("slides.pptx",)

        _insert_job(connection, "text-job", "txt")
        _insert_job(connection, "markdown-job", "md")
        _insert_job(connection, "excel-job", "xlsx")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_job(connection, "unsupported-job", "csv")

        migrations = connection.execute(
            "SELECT name FROM schema_migrations ORDER BY name"
        ).fetchall()

    assert migrations == [
        ("0001_initial.sql",),
        ("0002_expand_document_types.sql",),
        ("0003_provider_configs.sql",),
        ("0004_job_results.sql",),
        ("0005_provider_probe_latency.sql",),
        ("0006_job_languages.sql",),
        ("0007_sanitize_job_source_names.sql",),
        ("0008_job_options.sql",),
        ("0009_provider_model_inventory.sql",),
        ("0010_rename_discovered_model_source.sql",),
        ("0011_custom_openai_providers.sql",),
        ("0012_unique_provider_configs.sql",),
        ("0013_provider_secret_reconciliation.sql",),
        ("0014_provider_base_url_overrides.sql",),
        ("0015_job_stage_progress.sql",),
        ("0016_rename_job_progress_stages.sql",),
        ("0017_provider_model_runtime_settings.sql",),
        ("0018_manual_provider_models.sql",),
        ("0019_provider_active_state.sql",),
        ("0020_job_artifacts_and_xlsx.sql",),
    ]


def test_model_source_migration_preserves_inventory_and_renames_discovered(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0010 应保留 model inventory, 并把 discovered 原子改名为 remote。"""

    database = tmp_path / "pageferry.sqlite3"
    staged_migrations = tmp_path / "migrations"
    staged_migrations.mkdir()
    for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if migration.name >= "0010_rename_discovered_model_source.sql":
            continue
        shutil.copy2(migration, staged_migrations / migration.name)
    monkeypatch.setattr(migration_module, "MIGRATIONS_DIR", staged_migrations)

    initialize_database(database)

    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO provider_model_configs (
                provider_id,
                model_id,
                upstream_model_id,
                display_name,
                source,
                enabled,
                available,
                probe_status,
                created_at,
                updated_at
            )
            VALUES (
                'deepseek',
                'deepseek-v4-flash',
                'deepseek-v4-flash',
                'DeepSeek V4 Flash',
                'discovered',
                1,
                1,
                'succeeded',
                '2026-07-16T00:00:00Z',
                '2026-07-16T00:00:00Z'
            )
            """
        )

    shutil.copy2(
        MIGRATIONS_DIR / "0010_rename_discovered_model_source.sql",
        staged_migrations / "0010_rename_discovered_model_source.sql",
    )
    initialize_database(database)

    with sqlite3.connect(database) as connection:
        stored = connection.execute(
            """
            SELECT model_id, source, enabled, probe_status
            FROM provider_model_configs
            WHERE provider_id = 'deepseek'
            """
        ).fetchone()

        assert stored == ("deepseek-v4-flash", "remote", 1, "succeeded")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE provider_model_configs
                SET source = 'discovered'
                WHERE provider_id = 'deepseek'
                """
            )


def test_provider_configs_store_secret_references_without_key_material(tmp_path) -> None:
    """provider 表只能保存 Keychain reference, 且约束 probe status。"""

    database = tmp_path / "pageferry.sqlite3"
    initialize_database(database)

    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(provider_configs)")}
        assert "secret_ref" in columns
        assert "base_url_override" in columns
        assert "api_key" not in columns

        connection.execute(
            """
            INSERT INTO provider_configs (
                id,
                provider_id,
                model_id,
                catalog_version,
                secret_ref,
                probe_status,
                created_at,
                updated_at
            )
            VALUES (
                'deepseek-default',
                'deepseek',
                'deepseek-v4-flash',
                '0.1.0-dev',
                'keychain:provider/deepseek-default',
                'succeeded',
                '2026-07-16T00:00:00Z',
                '2026-07-16T00:00:00Z'
            )
            """
        )
        stored = connection.execute(
            """
            SELECT provider_id, model_id, secret_ref, probe_status
            FROM provider_configs
            WHERE id = 'deepseek-default'
            """
        ).fetchone()
        assert stored == (
            "deepseek",
            "deepseek-v4-flash",
            "keychain:provider/deepseek-default",
            "succeeded",
        )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO provider_configs (
                    id,
                    provider_id,
                    catalog_version,
                    secret_ref,
                    probe_status,
                    created_at,
                    updated_at
                )
                VALUES (
                    'invalid-probe',
                    'deepseek',
                    '0.1.0-dev',
                    'keychain:provider/invalid-probe',
                    'unknown',
                    '2026-07-16T00:00:00Z',
                    '2026-07-16T00:00:00Z'
                )
                """
            )


def test_provider_model_runtime_settings_migration_adds_nullable_checked_overrides(
    tmp_path,
) -> None:
    """0017 应为 model 增加可恢复默认的 override, 并拒绝越界持久化。"""

    database = tmp_path / "pageferry.sqlite3"
    initialize_database(database)

    with sqlite3.connect(database) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(provider_model_configs)")
        }
        assert {
            "reasoning_policy_override",
            "per_job_concurrency_override",
            "global_concurrency_override",
        } <= columns
        connection.execute(
            """
            INSERT INTO provider_model_configs (
                provider_id, model_id, upstream_model_id, display_name,
                source, enabled, available, probe_status,
                reasoning_policy_override,
                per_job_concurrency_override,
                global_concurrency_override,
                created_at, updated_at
            )
            VALUES (
                'deepseek', 'deepseek-v4-flash', 'deepseek-v4-flash',
                'DeepSeek V4 Flash', 'catalog', 1, 1, 'succeeded',
                'high', 8, 20,
                '2026-07-17T00:00:00Z', '2026-07-17T00:00:00Z'
            )
            """
        )
        stored = connection.execute(
            """
            SELECT reasoning_policy_override,
                   per_job_concurrency_override,
                   global_concurrency_override
            FROM provider_model_configs
            """
        ).fetchone()
        assert stored == ("high", 8, 20)

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE provider_model_configs
                SET reasoning_policy_override = 'ultra'
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE provider_model_configs
                SET global_concurrency_override = 33
                """
            )


def test_manual_model_source_migration_preserves_existing_runtime_settings(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0018 应新增 manual 来源, 并完整复制 0017 已有的 runtime override。"""

    database = tmp_path / "pageferry.sqlite3"
    staged_migrations = tmp_path / "migrations"
    staged_migrations.mkdir()
    for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if migration.name >= "0018_manual_provider_models.sql":
            continue
        shutil.copy2(migration, staged_migrations / migration.name)
    monkeypatch.setattr(migration_module, "MIGRATIONS_DIR", staged_migrations)
    initialize_database(database)

    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO provider_model_configs (
                provider_id, model_id, upstream_model_id, display_name,
                source, enabled, available, probe_status,
                reasoning_policy_override,
                per_job_concurrency_override,
                global_concurrency_override,
                created_at, updated_at
            )
            VALUES (
                'custom-a1', 'remote-v1', 'remote-v1', 'Remote V1',
                'remote', 1, 1, 'succeeded', 'medium', 8, 20,
                '2026-07-17T00:00:00Z', '2026-07-17T00:00:00Z'
            )
            """
        )

    monkeypatch.setattr(migration_module, "MIGRATIONS_DIR", MIGRATIONS_DIR)
    initialize_database(database)

    with sqlite3.connect(database) as connection:
        preserved = connection.execute(
            """
            SELECT source, enabled, available, probe_status,
                   reasoning_policy_override,
                   per_job_concurrency_override,
                   global_concurrency_override
            FROM provider_model_configs
            WHERE provider_id = 'custom-a1' AND model_id = 'remote-v1'
            """
        ).fetchone()
        assert preserved == ("remote", 1, 1, "succeeded", "medium", 8, 20)
        connection.execute(
            """
            INSERT INTO provider_model_configs (
                provider_id, model_id, upstream_model_id, display_name,
                source, created_at, updated_at
            )
            VALUES (
                'custom-a1', 'manual-v2', 'manual-v2', 'Manual V2',
                'manual', '2026-07-17T01:00:00Z', '2026-07-17T01:00:00Z'
            )
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO provider_model_configs (
                    provider_id, model_id, upstream_model_id, display_name,
                    source, created_at, updated_at
                )
                VALUES (
                    'custom-a1', 'invalid-v3', 'invalid-v3', 'Invalid V3',
                    'local', '2026-07-17T01:00:00Z', '2026-07-17T01:00:00Z'
                )
                """
            )


def test_provider_active_state_migration_keeps_existing_configs_active(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0019 应让旧配置默认保持 active, 并拒绝非法状态。"""

    database = tmp_path / "pageferry.sqlite3"
    staged_migrations = tmp_path / "migrations"
    staged_migrations.mkdir()
    for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if migration.name >= "0019_provider_active_state.sql":
            continue
        shutil.copy2(migration, staged_migrations / migration.name)
    monkeypatch.setattr(migration_module, "MIGRATIONS_DIR", staged_migrations)
    initialize_database(database)

    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO provider_configs (
                id, provider_id, catalog_version, secret_ref, probe_status,
                created_at, updated_at
            )
            VALUES (
                'deepseek', 'deepseek', 'legacy', 'keychain:provider/deepseek',
                'succeeded', '2026-07-17T00:00:00Z', '2026-07-17T00:00:00Z'
            )
            """
        )

    shutil.copy2(
        MIGRATIONS_DIR / "0019_provider_active_state.sql",
        staged_migrations / "0019_provider_active_state.sql",
    )
    initialize_database(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT active FROM provider_configs WHERE provider_id = 'deepseek'"
        ).fetchone() == (1,)
        connection.execute("UPDATE provider_configs SET active = 0 WHERE provider_id = 'deepseek'")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE provider_configs SET active = 2 WHERE provider_id = 'deepseek'"
            )


def test_base_url_override_migration_preserves_existing_provider_config(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0014 应保留旧 effective URL, 并让旧配置默认继续跟随 catalog。"""

    database = tmp_path / "pageferry.sqlite3"
    staged_migrations = tmp_path / "migrations"
    staged_migrations.mkdir()
    for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if migration.name >= "0014_provider_base_url_overrides.sql":
            continue
        shutil.copy2(migration, staged_migrations / migration.name)
    monkeypatch.setattr(migration_module, "MIGRATIONS_DIR", staged_migrations)
    initialize_database(database)

    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO provider_configs (
                id, provider_id, model_id, default_model_id, base_url,
                catalog_version, secret_ref, probe_status,
                created_at, updated_at
            )
            VALUES (
                'deepseek', 'deepseek', 'deepseek-v4-flash',
                'deepseek-v4-flash', 'https://api.deepseek.com',
                'legacy', 'keychain:provider/deepseek', 'succeeded',
                '2026-07-16T00:00:00Z', '2026-07-16T00:00:00Z'
            )
            """
        )

    shutil.copy2(
        MIGRATIONS_DIR / "0014_provider_base_url_overrides.sql",
        staged_migrations / "0014_provider_base_url_overrides.sql",
    )
    initialize_database(database)

    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(provider_configs)")}
        stored = connection.execute(
            """
            SELECT base_url, base_url_override
            FROM provider_configs
            WHERE provider_id = 'deepseek'
            """
        ).fetchone()

    assert "base_url_override" in columns
    assert stored == ("https://api.deepseek.com", None)


def test_custom_provider_migration_stores_definition_without_credentials(tmp_path) -> None:
    """0011 只保存公开 endpoint 定义, 不给 API Key 留下数据库列。"""

    database = tmp_path / "pageferry.sqlite3"
    initialize_database(database)

    with sqlite3.connect(database) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(custom_provider_definitions)")
        }
        assert columns == {
            "provider_id",
            "display_name",
            "base_url",
            "created_at",
            "updated_at",
        }
        connection.execute(
            """
            INSERT INTO custom_provider_definitions (
                provider_id, display_name, base_url, created_at, updated_at
            )
            VALUES (
                'custom-12345678',
                'Local Gateway',
                'http://127.0.0.1:11434/v1',
                '2026-07-16T00:00:00Z',
                '2026-07-16T00:00:00Z'
            )
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO custom_provider_definitions (
                    provider_id, display_name, base_url, created_at, updated_at
                )
                VALUES (
                    'deepseek',
                    'Not Custom',
                    'https://example.com/v1',
                    '2026-07-16T00:00:00Z',
                    '2026-07-16T00:00:00Z'
                )
                """
            )


def test_provider_unique_migration_keeps_latest_legacy_row(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0012 按 updated_at、created_at、rowid 去重后约束单 provider 单配置。"""

    database = tmp_path / "pageferry.sqlite3"
    staged_migrations = tmp_path / "migrations"
    staged_migrations.mkdir()
    for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if migration.name >= "0012_unique_provider_configs.sql":
            continue
        shutil.copy2(migration, staged_migrations / migration.name)
    monkeypatch.setattr(migration_module, "MIGRATIONS_DIR", staged_migrations)
    initialize_database(database)

    def insert_provider(
        connection: sqlite3.Connection,
        *,
        row_id: str,
        provider_id: str,
        created_at: str,
        updated_at: str,
    ) -> None:
        """插入旧 schema 允许的重复 provider 配置。"""

        connection.execute(
            """
            INSERT INTO provider_configs (
                id, provider_id, catalog_version, secret_ref, probe_status,
                created_at, updated_at
            )
            VALUES (?, ?, 'legacy', ?, 'succeeded', ?, ?)
            """,
            (row_id, provider_id, f"keychain:provider/{row_id}", created_at, updated_at),
        )

    with sqlite3.connect(database) as connection:
        insert_provider(
            connection,
            row_id="deepseek-newer-created",
            provider_id="deepseek",
            created_at="2026-01-05T00:00:00Z",
            updated_at="2026-01-02T00:00:00Z",
        )
        insert_provider(
            connection,
            row_id="deepseek-winner",
            provider_id="deepseek",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-03T00:00:00Z",
        )
        insert_provider(
            connection,
            row_id="kimi-older-created",
            provider_id="kimi",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-03T00:00:00Z",
        )
        insert_provider(
            connection,
            row_id="kimi-winner",
            provider_id="kimi",
            created_at="2026-01-02T00:00:00Z",
            updated_at="2026-01-03T00:00:00Z",
        )
        insert_provider(
            connection,
            row_id="glm-older-rowid",
            provider_id="glm",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-03T00:00:00Z",
        )
        insert_provider(
            connection,
            row_id="glm-winner",
            provider_id="glm",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-03T00:00:00Z",
        )

    shutil.copy2(
        MIGRATIONS_DIR / "0012_unique_provider_configs.sql",
        staged_migrations / "0012_unique_provider_configs.sql",
    )
    initialize_database(database)

    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT provider_id, id FROM provider_configs ORDER BY provider_id"
        ).fetchall()
        indexes = connection.execute("PRAGMA index_list(provider_configs)").fetchall()
        candidates = connection.execute(
            """
            SELECT provider_id, secret_ref, priority
            FROM provider_secret_reconciliation_candidates
            ORDER BY provider_id, priority
            """
        ).fetchall()
        provider_index = next(
            index for index in indexes if index[1] == "idx_provider_configs_provider_id"
        )
        assert rows == [
            ("deepseek", "deepseek-winner"),
            ("glm", "glm-winner"),
            ("kimi", "kimi-winner"),
        ]
        assert provider_index[2] == 1
        assert candidates == [
            ("deepseek", "keychain:provider/deepseek-winner", 1),
            ("deepseek", "keychain:provider/deepseek-newer-created", 2),
            ("glm", "keychain:provider/glm-winner", 1),
            ("glm", "keychain:provider/glm-older-rowid", 2),
            ("kimi", "keychain:provider/kimi-winner", 1),
            ("kimi", "keychain:provider/kimi-older-created", 2),
        ]
        with pytest.raises(sqlite3.IntegrityError):
            insert_provider(
                connection,
                row_id="deepseek-duplicate",
                provider_id="deepseek",
                created_at="2026-01-06T00:00:00Z",
                updated_at="2026-01-06T00:00:00Z",
            )


def test_reconciliation_migration_is_compatible_with_already_applied_0012(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """历史上已执行 0012 且没有 staging 的数据库应由 0013 创建空表。"""

    database = tmp_path / "pageferry.sqlite3"
    staged_migrations = tmp_path / "migrations"
    staged_migrations.mkdir()
    for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if migration.name >= "0013_provider_secret_reconciliation.sql":
            continue
        shutil.copy2(migration, staged_migrations / migration.name)
    original_hook = migration_module._stage_provider_secret_candidates

    def skip_unavailable_historical_hook(connection: sqlite3.Connection) -> None:
        """模拟 0012 初次发布时尚不存在的 pre-migration hook。"""

        del connection

    monkeypatch.setattr(
        migration_module,
        "_stage_provider_secret_candidates",
        skip_unavailable_historical_hook,
    )
    monkeypatch.setattr(migration_module, "MIGRATIONS_DIR", staged_migrations)
    initialize_database(database)

    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                """
            SELECT name FROM sqlite_master
            WHERE type = 'table'
              AND name = 'provider_secret_reconciliation_candidates'
            """
            ).fetchone()
            is None
        )

    shutil.copy2(
        MIGRATIONS_DIR / "0013_provider_secret_reconciliation.sql",
        staged_migrations / "0013_provider_secret_reconciliation.sql",
    )
    monkeypatch.setattr(
        migration_module,
        "_stage_provider_secret_candidates",
        original_hook,
    )
    initialize_database(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM provider_secret_reconciliation_candidates"
        ).fetchone() == (0,)
