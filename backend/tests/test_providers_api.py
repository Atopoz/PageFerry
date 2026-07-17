"""验证 provider 配置 API 集成行为."""

import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock

import httpx2 as httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.settings import Settings
from main import create_app
from modules.model_catalog.provider_config import (
    ProviderConfigError,
    ProviderConfigRepositoryError,
    ProviderPublicErrorCode,
)
from modules.model_catalog.provider_repository import ProviderInventoryItem
from modules.model_catalog.providers import DeepSeekProvider, OpenAICompatibleProvider
from modules.model_catalog.secrets import SecretStoreOperationError

TEST_API_KEY = "provider-test-secret"


def _runtime_fields(
    *,
    reasoning_policy: str | None = None,
    supported_reasoning_policies: list[str] | None = None,
    probe_status: str = "not_tested",
    probe_error_code: str | None = None,
    latency_ms: int | None = None,
    last_probed_at: str | None = None,
) -> dict[str, object]:
    """返回 model response 共用的默认 runtime settings 字段。"""

    return {
        "available": True,
        "probe_status": probe_status,
        "probe_error_code": probe_error_code,
        "latency_ms": latency_ms,
        "last_probed_at": last_probed_at,
        "reasoning_policy": reasoning_policy,
        "reasoning_policy_override": None,
        "supported_reasoning_policies": supported_reasoning_policies or [],
        "per_job_concurrency": 6,
        "per_job_concurrency_override": None,
        "global_concurrency": 15,
        "global_concurrency_override": None,
    }


class MemorySecretStore:
    """记录凭据变更的内存 SecretStore fake."""

    def __init__(self) -> None:
        """创建空的 fake secret store."""

        self.values: dict[str, str] = {}
        self.set_calls = 0
        self.delete_calls = 0

    def set_secret(self, reference: str, secret: str) -> None:
        """保存 secret 并记录写操作."""

        self.values[reference] = secret
        self.set_calls += 1

    def get_secret(self, reference: str) -> str | None:
        """返回 reference 对应的已存 secret."""

        return self.values.get(reference)

    def delete_secret(self, reference: str) -> bool:
        """删除 secret 并记录变更尝试."""

        self.delete_calls += 1
        return self.values.pop(reference, None) is not None


class FailingReadSecretStore(MemorySecretStore):
    """允许测试在配置成功后模拟 Keychain 读取故障。"""

    def __init__(self) -> None:
        """创建默认可读、可按需切换失败的 secret store。"""

        super().__init__()
        self.fail_reads = False

    def get_secret(self, reference: str) -> str | None:
        """读取开关开启时抛出不应暴露给 API 的内部错误。"""

        if self.fail_reads:
            raise SecretStoreOperationError("synthetic private keychain failure")
        return super().get_secret(reference)


def _successful_handler(
    requests: list[tuple[str, dict[str, object] | None]],
    secret_store: MemorySecretStore | None = None,
) -> httpx.MockTransport:
    """构造暴露 model 且接受最小 probe 的 transport."""

    def handler(request: httpx.Request) -> httpx.Response:
        """记录一个 DeepSeek 请求并返回确定性 response."""

        if secret_store is not None:
            assert secret_store.values == {}
        if request.method == "GET":
            requests.append((request.method, None))
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [{"id": "deepseek-v4-flash", "owned_by": "deepseek"}],
                },
            )
        payload = json.loads(request.content)
        requests.append((request.method, payload))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "OK"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    return httpx.MockTransport(handler)


def _create_test_app(
    data_dir: Path,
    secret_store: MemorySecretStore,
    transport: httpx.MockTransport,
    *,
    boot_token: str | None = None,
) -> FastAPI:
    """创建 provider HTTP 与 secret 边界完全本地化的应用."""

    def client_factory() -> httpx.AsyncClient:
        """返回新 client, 因为 service 负责关闭每个 client."""

        return httpx.AsyncClient(transport=transport)

    return create_app(
        Settings(data_dir=data_dir, boot_token=boot_token),
        secret_store=secret_store,
        http_client_factory=client_factory,
    )


def test_provider_configuration_probes_before_persisting_and_survives_restart(tmp_path) -> None:
    """成功的两阶段 probe 只持久化 metadata, reference 与 latency."""

    secret_store = MemorySecretStore()
    requests: list[tuple[str, dict[str, object] | None]] = []
    app = _create_test_app(
        tmp_path,
        secret_store,
        _successful_handler(requests, secret_store),
    )

    with TestClient(app) as client:
        initial = client.get("/api/v1/providers")
        configured = client.put(
            "/api/v1/providers/deepseek",
            json={"api_key": TEST_API_KEY, "model_id": "deepseek-v4-flash"},
        )
        listed = client.get("/api/v1/providers")

    assert initial.status_code == 200
    initial_by_id = {item["provider_id"]: item for item in initial.json()}
    assert list(initial_by_id) == ["deepseek", "kimi", "glm", "minimax", "mimo"]
    assert initial_by_id["deepseek"] == {
        "provider_id": "deepseek",
        "display_name": "DeepSeek",
        "protocol": "openai",
        "is_custom": False,
        "base_url": "https://api.deepseek.com",
        "base_url_overridden": False,
        "base_url_editable": True,
        "deletable": False,
        "available": True,
        "configured": False,
        "active": False,
        "probe_status": "not_configured",
        "probe_error_code": None,
        "latency_ms": None,
        "model_id": None,
        "default_model_id": None,
        "enabled_model_ids": [],
        "models": [
            {
                "id": "deepseek-v4-flash",
                "display_name": "DeepSeek V4 Flash",
                "source": "catalog",
                "enabled": False,
                **_runtime_fields(
                    reasoning_policy="off",
                    supported_reasoning_policies=[
                        "provider_default",
                        "off",
                        "high",
                        "max",
                    ],
                ),
            },
            {
                "id": "deepseek-v4-pro",
                "display_name": "DeepSeek V4 Pro",
                "source": "catalog",
                "enabled": False,
                **_runtime_fields(
                    reasoning_policy="off",
                    supported_reasoning_policies=[
                        "provider_default",
                        "off",
                        "high",
                        "max",
                    ],
                ),
            },
        ],
        "supports_model_sync": True,
        "last_probed_at": None,
        "last_synced_at": None,
    }
    assert configured.status_code == 200
    configured_body = configured.json()
    assert configured_body["configured"] is True
    assert configured_body["active"] is True
    assert configured_body["probe_status"] == "succeeded"
    assert configured_body["model_id"] == "deepseek-v4-flash"
    assert isinstance(configured_body["latency_ms"], int)
    assert configured_body["latency_ms"] >= 0
    listed_by_id = {item["provider_id"]: item for item in listed.json()}
    assert listed_by_id["deepseek"] == configured_body
    assert set(configured_body) == {
        "provider_id",
        "display_name",
        "protocol",
        "is_custom",
        "base_url",
        "base_url_overridden",
        "base_url_editable",
        "deletable",
        "available",
        "configured",
        "active",
        "probe_status",
        "probe_error_code",
        "latency_ms",
        "model_id",
        "default_model_id",
        "enabled_model_ids",
        "models",
        "supports_model_sync",
        "last_probed_at",
        "last_synced_at",
    }
    assert TEST_API_KEY not in json.dumps(configured_body)
    assert "secret_ref" not in configured_body

    assert [method for method, _ in requests] == ["GET", "POST"]
    probe_payload = requests[1][1]
    assert probe_payload is not None
    assert probe_payload["model"] == "deepseek-v4-flash"
    assert probe_payload["thinking"] == {"type": "disabled"}
    assert secret_store.values == {"keychain:provider/deepseek": TEST_API_KEY}

    with sqlite3.connect(tmp_path / "pageferry.sqlite3") as connection:
        row = connection.execute(
            """
            SELECT provider_id, model_id, secret_ref, probe_status, latency_ms
            FROM provider_configs
            """
        ).fetchone()
    assert row == (
        "deepseek",
        "deepseek-v4-flash",
        "keychain:provider/deepseek",
        "succeeded",
        configured_body["latency_ms"],
    )

    restarted_app = _create_test_app(
        tmp_path,
        secret_store,
        httpx.MockTransport(lambda _: pytest.fail("GET provider status must not call DeepSeek")),
    )
    with TestClient(restarted_app) as restarted_client:
        after_restart = restarted_client.get("/api/v1/providers")
    assert after_restart.status_code == 200
    restarted_by_id = {item["provider_id"]: item for item in after_restart.json()}
    assert restarted_by_id["deepseek"] == configured_body


def test_provider_api_key_endpoint_returns_one_secret_and_disables_caching(tmp_path) -> None:
    """专用 endpoint 经 boot token 鉴权后返回完整 Key, 列表仍不得暴露。"""

    secret_store = MemorySecretStore()
    requests: list[tuple[str, dict[str, object] | None]] = []
    app = _create_test_app(
        tmp_path,
        secret_store,
        _successful_handler(requests),
        boot_token="provider-boot-token",
    )
    auth_headers = {"X-PageFerry-Boot-Token": "provider-boot-token"}

    with TestClient(app) as client:
        configured = client.put(
            "/api/v1/providers/deepseek",
            headers=auth_headers,
            json={"api_key": TEST_API_KEY, "model_id": "deepseek-v4-flash"},
        )
        unauthorized = client.get("/api/v1/providers/deepseek/api-key")
        revealed = client.get(
            "/api/v1/providers/deepseek/api-key",
            headers=auth_headers,
        )
        listed = client.get("/api/v1/providers")

    assert configured.status_code == 200
    assert unauthorized.status_code == 401
    assert unauthorized.json() == {
        "detail": {"code": "unauthorized", "message": "Invalid boot token."}
    }
    assert unauthorized.headers["cache-control"] == "no-store, private"
    assert unauthorized.headers["pragma"] == "no-cache"
    assert unauthorized.headers["expires"] == "0"
    assert revealed.status_code == 200
    assert revealed.json() == {"api_key": TEST_API_KEY}
    assert revealed.headers["cache-control"] == "no-store, private"
    assert revealed.headers["pragma"] == "no-cache"
    assert revealed.headers["expires"] == "0"
    assert TEST_API_KEY not in json.dumps(listed.json())


def test_provider_api_key_endpoint_reports_missing_provider_and_credential(tmp_path) -> None:
    """不存在、未配置和 Keychain 条目缺失必须返回稳定的脱敏错误。"""

    secret_store = MemorySecretStore()
    requests: list[tuple[str, dict[str, object] | None]] = []
    app = _create_test_app(
        tmp_path,
        secret_store,
        _successful_handler(requests),
    )

    with TestClient(app) as client:
        unknown = client.get("/api/v1/providers/not-a-provider/api-key")
        unconfigured = client.get("/api/v1/providers/deepseek/api-key")
        configured = client.put(
            "/api/v1/providers/deepseek",
            json={"api_key": TEST_API_KEY, "model_id": "deepseek-v4-flash"},
        )
        secret_store.values.clear()
        missing = client.get("/api/v1/providers/deepseek/api-key")

    assert unknown.status_code == 404
    assert unknown.json() == {
        "code": "endpoint",
        "message": "The requested provider is not available.",
    }
    assert unconfigured.status_code == 409
    assert unconfigured.json() == {
        "code": "key",
        "message": "The provider has not been configured.",
    }
    assert configured.status_code == 200
    assert missing.status_code == 409
    assert missing.json() == {
        "code": "key",
        "message": "The provider credential is missing from system storage.",
    }
    assert missing.headers["cache-control"] == "no-store, private"


def test_provider_api_key_endpoint_redacts_keychain_failures(tmp_path) -> None:
    """Keychain 异常只返回公开错误, 不把内部 exception 文本传给 frontend。"""

    secret_store = FailingReadSecretStore()
    requests: list[tuple[str, dict[str, object] | None]] = []
    app = _create_test_app(
        tmp_path,
        secret_store,
        _successful_handler(requests),
    )

    with TestClient(app) as client:
        configured = client.put(
            "/api/v1/providers/deepseek",
            json={"api_key": TEST_API_KEY, "model_id": "deepseek-v4-flash"},
        )
        secret_store.fail_reads = True
        failed = client.get("/api/v1/providers/deepseek/api-key")

    assert configured.status_code == 200
    assert failed.status_code == 503
    assert failed.json() == {
        "code": "key",
        "message": "System credential storage is unavailable.",
    }
    assert "synthetic" not in failed.text
    assert failed.headers["cache-control"] == "no-store, private"


def test_first_configuration_can_probe_and_enable_the_full_inventory(tmp_path) -> None:
    """显式 enable-all 应 probe 完整 inventory, 并采用请求指定的 default。"""

    requested_models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """返回一个账号额外 model, 并记录所有 inference probe。"""

        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "account-extra"}]})
        payload = json.loads(request.content)
        requested_models.append(payload["model"])
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "OK"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    app = _create_test_app(
        tmp_path,
        MemorySecretStore(),
        httpx.MockTransport(handler),
    )
    with TestClient(app) as client:
        configured = client.put(
            "/api/v1/providers/deepseek",
            json={
                "api_key": TEST_API_KEY,
                "enable_all_models": True,
                "default_model_id": "deepseek-v4-pro",
            },
        )
        calls_before_conflict = list(requested_models)
        conflicting = client.put(
            "/api/v1/providers/deepseek",
            json={
                "enable_all_models": True,
                "enabled_model_ids": ["deepseek-v4-flash"],
                "default_model_id": "deepseek-v4-flash",
            },
        )

    assert configured.status_code == 200
    assert configured.json()["active"] is True
    assert configured.json()["default_model_id"] == "deepseek-v4-pro"
    assert set(configured.json()["enabled_model_ids"]) == {
        "account-extra",
        "deepseek-v4-flash",
        "deepseek-v4-pro",
    }
    assert {model["id"] for model in configured.json()["models"] if model["enabled"]} == {
        "account-extra",
        "deepseek-v4-flash",
        "deepseek-v4-pro",
    }
    assert requested_models == [
        "deepseek-v4-flash",
        "deepseek-v4-pro",
        "account-extra",
    ]
    assert conflicting.status_code == 400
    assert conflicting.json()["code"] == "model"
    assert requested_models == calls_before_conflict


def test_provider_key_failures_use_the_top_level_error_envelope(tmp_path) -> None:
    """Configure、probe、discover 与 sync 缺 Key 时应返回顶层 provider error。"""

    requests: list[tuple[str, dict[str, object] | None]] = []
    secret_store = MemorySecretStore()
    app = _create_test_app(
        tmp_path,
        secret_store,
        _successful_handler(requests),
    )
    with TestClient(app) as client:
        configure_without_key = client.put("/api/v1/providers/deepseek", json={})
        probe_without_key = client.post("/api/v1/providers/deepseek/probe", json={})
        discover_without_key = client.post(
            "/api/v1/providers/deepseek/models/discover",
            json={},
        )
        configured = client.put(
            "/api/v1/providers/deepseek",
            json={"api_key": TEST_API_KEY},
        )
        calls_after_configure = list(requests)
        secret_store.values.clear()
        sync_without_key = client.post("/api/v1/providers/deepseek/models/sync")

    assert configured.status_code == 200
    for response in (
        configure_without_key,
        probe_without_key,
        discover_without_key,
        sync_without_key,
    ):
        assert response.status_code == 401
        assert response.json()["code"] == "key"
        assert "message" in response.json()
        assert "detail" not in response.json()
    assert requests == calls_after_configure


def test_provider_probe_uses_temporary_settings_without_persisting(tmp_path) -> None:
    """纯检测使用临时 Key、URL 与显式 model, 成功后本地状态仍完全不变。"""

    temporary_key = "temporary-probe-secret"
    temporary_url = "https://temporary-gateway.example/v1"
    requests: list[tuple[str, str, str, dict[str, object] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """校验临时配置只进入本次 discovery 与 inference。"""

        payload = json.loads(request.content) if request.content else None
        requests.append(
            (
                request.method,
                str(request.url),
                request.headers["Authorization"],
                payload,
            )
        )
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "deepseek-v4-flash"},
                        {"id": "deepseek-v4-pro"},
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "OK"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    secret_store = MemorySecretStore()
    app = _create_test_app(
        tmp_path,
        secret_store,
        httpx.MockTransport(handler),
    )
    with TestClient(app) as client:
        before = client.get("/api/v1/providers").json()
        probed = client.post(
            "/api/v1/providers/deepseek/probe",
            json={
                "api_key": temporary_key,
                "base_url": f"{temporary_url}/",
                "model_id": "deepseek-v4-pro",
            },
        )
        after = client.get("/api/v1/providers").json()

    assert probed.status_code == 200
    assert probed.json()["provider_id"] == "deepseek"
    assert probed.json()["model_id"] == "deepseek-v4-pro"
    assert probed.json()["display_name"] == "DeepSeek V4 Pro"
    assert isinstance(probed.json()["latency_ms"], int)
    assert probed.json()["latency_ms"] >= 0
    assert requests[0][:3] == (
        "GET",
        f"{temporary_url}/models",
        f"Bearer {temporary_key}",
    )
    assert requests[1][:3] == (
        "POST",
        f"{temporary_url}/chat/completions",
        f"Bearer {temporary_key}",
    )
    assert requests[1][3] is not None
    assert requests[1][3]["model"] == "deepseek-v4-pro"
    assert requests[1][3]["thinking"] == {"type": "disabled"}
    assert before == after
    assert secret_store.values == {}
    assert secret_store.set_calls == 0
    with sqlite3.connect(tmp_path / "pageferry.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM provider_configs").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM provider_model_configs").fetchone() == (0,)


def test_provider_probe_failure_preserves_existing_runtime_snapshot(tmp_path) -> None:
    """临时检测失败不能覆盖现有 Key、URL、active 或 model probe 状态。"""

    temporary_key = "failing-temporary-secret"
    temporary_url = "https://failing-probe.example/v1"
    fail_probe = False

    def handler(request: httpx.Request) -> httpx.Response:
        """先完成正式配置, 再让临时 endpoint 的 inference 失败。"""

        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "deepseek-v4-flash"}]})
        if fail_probe:
            assert str(request.url) == f"{temporary_url}/chat/completions"
            assert request.headers["Authorization"] == f"Bearer {temporary_key}"
            assert json.loads(request.content)["model"] == "deepseek-v4-flash"
            return httpx.Response(503, json={"error": {"message": "private outage"}})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "OK"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    secret_store = MemorySecretStore()
    app = _create_test_app(tmp_path, secret_store, httpx.MockTransport(handler))
    with TestClient(app) as client:
        configured = client.put(
            "/api/v1/providers/deepseek",
            json={"api_key": TEST_API_KEY, "model_id": "deepseek-v4-flash"},
        )
        before = client.get("/api/v1/providers").json()
        saved_secret = dict(secret_store.values)
        saved_set_calls = secret_store.set_calls
        fail_probe = True
        failed = client.post(
            "/api/v1/providers/deepseek/probe",
            json={
                "api_key": temporary_key,
                "base_url": temporary_url,
            },
        )
        after = client.get("/api/v1/providers").json()

    assert configured.status_code == 200
    assert failed.status_code == 503
    assert failed.json()["code"] == "network"
    assert temporary_key not in failed.text
    assert "private outage" not in failed.text
    assert after == before
    assert secret_store.values == saved_secret
    assert secret_store.set_calls == saved_set_calls


def test_provider_probe_service_falls_back_to_manual_model_without_writes(tmp_path) -> None:
    """`/models` 故障时 service 仍可检测 manual model, 且不改变其本地状态。"""

    requests: list[tuple[str, str, dict[str, object] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """让 discovery 失败, 只接受 manual model 的最小 inference。"""

        payload = json.loads(request.content) if request.content else None
        requests.append((request.method, str(request.url), payload))
        if request.method == "GET":
            return httpx.Response(503, json={"error": {"message": "private outage"}})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "OK"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    secret_store = MemorySecretStore()
    app = _create_test_app(tmp_path, secret_store, httpx.MockTransport(handler))
    service = app.state.provider_config_service
    with TestClient(app):
        custom = service.create_custom_provider(
            display_name="Manual Probe",
            base_url="https://manual-probe.example/v1",
        )
        service.add_manual_model(
            custom.provider_id,
            model_id="manual-translation",
            display_name="Manual Translation",
        )
        before = service.list_statuses()

        result = asyncio.run(
            service.probe(
                custom.provider_id,
                api_key=TEST_API_KEY,
            )
        )
        after = service.list_statuses()

    assert result.provider_id == custom.provider_id
    assert result.model_id == "manual-translation"
    assert result.display_name == "Manual Translation"
    assert result.latency_ms >= 0
    assert requests[0][:2] == (
        "GET",
        "https://manual-probe.example/v1/models",
    )
    assert requests[1][:2] == (
        "POST",
        "https://manual-probe.example/v1/chat/completions",
    )
    assert requests[1][2] is not None
    assert requests[1][2]["model"] == "manual-translation"
    assert before == after
    assert secret_store.values == {}
    with sqlite3.connect(tmp_path / "pageferry.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM provider_configs").fetchone() == (0,)
        assert connection.execute(
            """
            SELECT source, enabled, probe_status
            FROM provider_model_configs
            WHERE provider_id = ? AND model_id = 'manual-translation'
            """,
            (custom.provider_id,),
        ).fetchone() == ("manual", 0, "not_tested")


def test_provider_active_toggle_is_non_destructive_and_reconfigure_preserves_inactive(
    tmp_path,
) -> None:
    """停用应保留 Key 与 inventory, 再保存也不能偷偷恢复 active。"""

    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """接受配置 probe, active 开关本身不得调用上游。"""

        requests.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "deepseek-v4-flash"}]})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "OK"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    secret_store = MemorySecretStore()
    app = _create_test_app(tmp_path, secret_store, httpx.MockTransport(handler))
    service = app.state.provider_config_service
    with TestClient(app) as client:
        configured = client.put(
            "/api/v1/providers/deepseek",
            json={"api_key": TEST_API_KEY, "model_id": "deepseek-v4-flash"},
        )
        calls_after_configure = list(requests)
        deactivated = client.put(
            "/api/v1/providers/deepseek/active",
            json={"active": False},
        )
        with pytest.raises(ProviderConfigError) as inactive:
            service.build_translator("deepseek", "deepseek-v4-flash")
        reconfigured = client.put(
            "/api/v1/providers/deepseek",
            json={
                "enabled_model_ids": ["deepseek-v4-flash"],
                "default_model_id": "deepseek-v4-flash",
            },
        )
        secret_store.values.clear()
        missing_secret = client.put(
            "/api/v1/providers/deepseek/active",
            json={"active": True},
        )
        secret_store.values["keychain:provider/deepseek"] = TEST_API_KEY
        activated = client.put(
            "/api/v1/providers/deepseek/active",
            json={"active": True},
        )
        translator = service.build_translator("deepseek", "deepseek-v4-flash")

    assert configured.status_code == 200
    assert deactivated.status_code == 200
    assert deactivated.json()["active"] is False
    assert deactivated.json()["configured"] is True
    assert deactivated.json()["enabled_model_ids"] == ["deepseek-v4-flash"]
    assert requests == [*calls_after_configure, "GET", "POST"]
    assert inactive.value.code is ProviderPublicErrorCode.CONFLICT
    assert reconfigured.status_code == 200
    assert reconfigured.json()["active"] is False
    assert missing_secret.status_code == 409
    assert missing_secret.json()["code"] == "key"
    assert activated.status_code == 200
    assert activated.json()["active"] is True
    assert isinstance(translator, DeepSeekProvider)


def test_provider_activation_and_translator_require_a_runnable_model(tmp_path) -> None:
    """active 与 translator 都要求 model 同时 enabled、available 且 probe 成功。"""

    app = _create_test_app(
        tmp_path,
        MemorySecretStore(),
        _successful_handler([]),
    )
    service = app.state.provider_config_service
    with TestClient(app) as client:
        configured = client.put(
            "/api/v1/providers/deepseek",
            json={"api_key": TEST_API_KEY, "model_id": "deepseek-v4-flash"},
        )
        with sqlite3.connect(tmp_path / "pageferry.sqlite3") as connection:
            connection.execute(
                "UPDATE provider_model_configs SET available = 0 WHERE provider_id = 'deepseek'"
            )
        with pytest.raises(ProviderConfigError) as unavailable_translator:
            service.build_translator("deepseek", "deepseek-v4-flash")
        deactivated = client.put(
            "/api/v1/providers/deepseek/active",
            json={"active": False},
        )
        rejected = client.put(
            "/api/v1/providers/deepseek/active",
            json={"active": True},
        )

    assert configured.status_code == 200
    assert unavailable_translator.value.code is ProviderPublicErrorCode.MODEL
    assert deactivated.status_code == 200
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "model_required"


def test_model_enabled_endpoint_probes_enable_and_atomically_replaces_default(tmp_path) -> None:
    """单 model 开关应在启用前 probe, 停用 default 时原子选择下一个。"""

    probed_models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """返回空 remote extras, 并记录 catalog/manual model probe。"""

        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        payload = json.loads(request.content)
        probed_models.append(payload["model"])
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "OK"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    app = _create_test_app(
        tmp_path,
        MemorySecretStore(),
        httpx.MockTransport(handler),
    )
    with TestClient(app) as client:
        configured = client.put(
            "/api/v1/providers/deepseek",
            json={
                "api_key": TEST_API_KEY,
                "enable_all_models": True,
                "default_model_id": "deepseek-v4-flash",
            },
        )
        probes_after_configure = list(probed_models)
        disabled_default = client.put(
            "/api/v1/providers/deepseek/models/deepseek-v4-flash/enabled",
            json={"enabled": False},
        )
        rejected_last = client.put(
            "/api/v1/providers/deepseek/models/deepseek-v4-pro/enabled",
            json={"enabled": False},
        )
        added = client.post(
            "/api/v1/providers/deepseek/models",
            json={"model_id": "manual-translation"},
        )
        enabled_manual = client.put(
            "/api/v1/providers/deepseek/models/manual-translation/enabled",
            json={"enabled": True},
        )
        probes_after_manual_enable = list(probed_models)
        repeated_enable = client.put(
            "/api/v1/providers/deepseek/models/manual-translation/enabled",
            json={"enabled": True},
        )

    assert configured.status_code == 200
    assert probes_after_configure == ["deepseek-v4-flash", "deepseek-v4-pro"]
    assert disabled_default.status_code == 200
    assert disabled_default.json()["default_model_id"] == "deepseek-v4-pro"
    assert disabled_default.json()["enabled_model_ids"] == ["deepseek-v4-pro"]
    assert probed_models == [*probes_after_configure, "manual-translation"]
    assert rejected_last.status_code == 409
    assert rejected_last.json()["code"] == "model_required"
    assert added.status_code == 201
    assert added.json()["enabled"] is False
    assert enabled_manual.status_code == 200
    assert set(enabled_manual.json()["enabled_model_ids"]) == {
        "deepseek-v4-pro",
        "manual-translation",
    }
    assert enabled_manual.json()["default_model_id"] == "deepseek-v4-pro"
    assert repeated_enable.status_code == 200
    assert repeated_enable.json() == enabled_manual.json()
    assert probed_models == probes_after_manual_enable
    with sqlite3.connect(tmp_path / "pageferry.sqlite3") as connection:
        provider_row = connection.execute(
            "SELECT model_id, default_model_id FROM provider_configs WHERE provider_id = 'deepseek'"
        ).fetchone()
        model_rows = dict(
            connection.execute(
                """
                SELECT model_id, enabled
                FROM provider_model_configs
                WHERE provider_id = 'deepseek'
                """
            ).fetchall()
        )
    assert provider_row == ("deepseek-v4-pro", "deepseek-v4-pro")
    assert model_rows == {
        "deepseek-v4-flash": 0,
        "deepseek-v4-pro": 1,
        "manual-translation": 1,
    }


def test_catalog_only_model_materializes_only_after_a_successful_enable_probe(tmp_path) -> None:
    """旧库缺少新版 catalog row 时, 启用应先 probe 再原子落库。"""

    allow_catalog_probe = False
    probed_models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """先拒绝新增 catalog model, 再允许第二次真实 probe。"""

        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        model_id = json.loads(request.content)["model"]
        probed_models.append(model_id)
        if model_id == "deepseek-v4-pro" and not allow_catalog_probe:
            return httpx.Response(401, json={"error": {"message": "invalid key"}})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "OK"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    database = tmp_path / "pageferry.sqlite3"
    app = _create_test_app(
        tmp_path,
        MemorySecretStore(),
        httpx.MockTransport(handler),
    )
    with TestClient(app) as client:
        configured = client.put(
            "/api/v1/providers/deepseek",
            json={"api_key": TEST_API_KEY, "model_id": "deepseek-v4-flash"},
        )
        with sqlite3.connect(database) as connection:
            connection.execute(
                """
                DELETE FROM provider_model_configs
                WHERE provider_id = 'deepseek' AND model_id = 'deepseek-v4-pro'
                """
            )
        overlaid = client.get("/api/v1/providers")
        rejected = client.put(
            "/api/v1/providers/deepseek/models/deepseek-v4-pro/enabled",
            json={"enabled": True},
        )
        with sqlite3.connect(database) as connection:
            row_after_failure = connection.execute(
                """
                SELECT source, enabled
                FROM provider_model_configs
                WHERE provider_id = 'deepseek' AND model_id = 'deepseek-v4-pro'
                """
            ).fetchone()
        allow_catalog_probe = True
        enabled = client.put(
            "/api/v1/providers/deepseek/models/deepseek-v4-pro/enabled",
            json={"enabled": True},
        )

    assert configured.status_code == 200
    overlaid_deepseek = next(
        provider for provider in overlaid.json() if provider["provider_id"] == "deepseek"
    )
    overlaid_pro = next(
        model for model in overlaid_deepseek["models"] if model["id"] == "deepseek-v4-pro"
    )
    assert overlaid_pro["source"] == "catalog"
    assert overlaid_pro["enabled"] is False
    assert overlaid_pro["probe_status"] == "not_tested"
    assert overlaid_pro["latency_ms"] is None
    assert rejected.status_code == 401
    assert rejected.json()["code"] == "key"
    assert "detail" not in rejected.json()
    assert row_after_failure is None
    assert enabled.status_code == 200
    enabled_pro = next(
        model for model in enabled.json()["models"] if model["id"] == "deepseek-v4-pro"
    )
    assert enabled_pro["source"] == "catalog"
    assert enabled_pro["enabled"] is True
    assert enabled_pro["available"] is True
    assert enabled_pro["probe_status"] == "succeeded"
    assert enabled_pro["probe_error_code"] is None
    assert isinstance(enabled_pro["latency_ms"], int)
    assert enabled_pro["last_probed_at"] is not None
    assert enabled_pro["reasoning_policy"] == "off"
    assert enabled_pro["reasoning_policy_override"] is None
    assert enabled_pro["per_job_concurrency"] == 6
    assert enabled_pro["per_job_concurrency_override"] is None
    assert enabled_pro["global_concurrency"] == 15
    assert enabled_pro["global_concurrency_override"] is None
    with sqlite3.connect(database) as connection:
        materialized = connection.execute(
            """
            SELECT source, enabled, available, probe_status, probe_error_code,
                   latency_ms, last_seen_at, last_probed_at,
                   reasoning_policy_override,
                   per_job_concurrency_override,
                   global_concurrency_override
            FROM provider_model_configs
            WHERE provider_id = 'deepseek' AND model_id = 'deepseek-v4-pro'
            """
        ).fetchone()
    assert materialized == (
        "catalog",
        1,
        1,
        "succeeded",
        None,
        enabled_pro["latency_ms"],
        None,
        enabled_pro["last_probed_at"],
        None,
        None,
        None,
    )
    assert probed_models == [
        "deepseek-v4-flash",
        "deepseek-v4-pro",
        "deepseek-v4-pro",
    ]


def test_model_disable_preserves_active_runnable_and_default_invariants(tmp_path) -> None:
    """停用 model 不得破坏 active 可运行集合, default 替换也必须真实可用。"""

    probed_models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """只允许配置阶段 discovery 与 probe, model 停用不得访问上游。"""

        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        probed_models.append(json.loads(request.content)["model"])
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "OK"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    app = _create_test_app(
        tmp_path,
        MemorySecretStore(),
        httpx.MockTransport(handler),
    )
    database = tmp_path / "pageferry.sqlite3"
    with TestClient(app) as client:
        configured = client.put(
            "/api/v1/providers/deepseek",
            json={
                "api_key": TEST_API_KEY,
                "enable_all_models": True,
                "default_model_id": "deepseek-v4-flash",
            },
        )
        probes_after_configure = list(probed_models)
        deactivated = client.put(
            "/api/v1/providers/deepseek/active",
            json={"active": False},
        )
        with sqlite3.connect(database) as connection:
            connection.execute(
                """
                UPDATE provider_model_configs
                SET available = 0
                WHERE provider_id = 'deepseek' AND model_id = 'deepseek-v4-pro'
                """
            )
        unavailable_default_replacement = client.put(
            "/api/v1/providers/deepseek/models/deepseek-v4-flash/enabled",
            json={"enabled": False},
        )
        with sqlite3.connect(database) as connection:
            connection.execute(
                """
                UPDATE provider_model_configs
                SET available = CASE model_id
                    WHEN 'deepseek-v4-flash' THEN 0
                    ELSE 1
                END
                WHERE provider_id = 'deepseek'
                """
            )
        activated = client.put(
            "/api/v1/providers/deepseek/active",
            json={"active": True},
        )
        would_remove_last_runnable = client.put(
            "/api/v1/providers/deepseek/models/deepseek-v4-pro/enabled",
            json={"enabled": False},
        )
        client.put(
            "/api/v1/providers/deepseek/active",
            json={"active": False},
        )
        disabled_while_inactive = client.put(
            "/api/v1/providers/deepseek/models/deepseek-v4-pro/enabled",
            json={"enabled": False},
        )

    assert configured.status_code == 200
    assert deactivated.status_code == 200
    assert unavailable_default_replacement.status_code == 409
    assert unavailable_default_replacement.json()["code"] == "model_required"
    assert activated.status_code == 200
    assert would_remove_last_runnable.status_code == 409
    assert would_remove_last_runnable.json()["code"] == "model_required"
    assert disabled_while_inactive.status_code == 200
    assert disabled_while_inactive.json()["enabled_model_ids"] == ["deepseek-v4-flash"]
    assert probed_models == probes_after_configure


def test_model_enable_discards_probe_if_provider_state_changes(tmp_path) -> None:
    """model probe 期间 provider 状态变化时必须以 conflict 放弃迟到结果。"""

    enable_probe_started = Event()
    release_enable_probe = Event()

    def handler(request: httpx.Request) -> httpx.Response:
        """只阻塞待启用 model 的 inference probe。"""

        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        payload = json.loads(request.content)
        if payload["model"] == "deepseek-v4-pro":
            enable_probe_started.set()
            assert release_enable_probe.wait(timeout=2)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "OK"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    app = _create_test_app(
        tmp_path,
        MemorySecretStore(),
        httpx.MockTransport(handler),
    )
    service = app.state.provider_config_service
    with TestClient(app) as client:
        configured = client.put(
            "/api/v1/providers/deepseek",
            json={"api_key": TEST_API_KEY, "model_id": "deepseek-v4-flash"},
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                lambda: asyncio.run(
                    service.set_model_enabled(
                        "deepseek",
                        "deepseek-v4-pro",
                        enabled=True,
                    )
                )
            )
            assert enable_probe_started.wait(timeout=2)
            deactivated = service.set_active("deepseek", active=False)
            release_enable_probe.set()
            with pytest.raises(ProviderConfigError) as conflict:
                future.result(timeout=2)

    assert configured.status_code == 200
    assert deactivated.active is False
    assert conflict.value.code is ProviderPublicErrorCode.CONFLICT
    deepseek = next(
        provider for provider in service.list_statuses() if provider.provider_id == "deepseek"
    )
    pro = next(model for model in deepseek.models if model.id == "deepseek-v4-pro")
    assert deepseek.active is False
    assert pro.enabled is False


def test_preset_base_url_override_survives_restart_and_drives_all_runtime_paths(
    tmp_path,
) -> None:
    """preset override 应驱动 probe、discovery 与 translator, 省略字段时保持不变。"""

    override_url = "https://gateway.example/v1"
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """记录 endpoint, 并分别返回 discovery、probe 与翻译响应。"""

        requested_urls.append(str(request.url))
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "deepseek-v4-flash"}]})
        payload = json.loads(request.content)
        if payload["messages"] == [{"role": "user", "content": "Reply with OK."}]:
            content = "OK"
        else:
            segment_payload = json.loads(payload["messages"][2]["content"])
            content = json.dumps(
                {
                    "segments": [
                        {"index": segment["index"], "text": f"译文:{segment['text']}"}
                        for segment in segment_payload["segments"]
                    ]
                }
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    secret_store = MemorySecretStore()
    transport = httpx.MockTransport(handler)
    app = _create_test_app(tmp_path, secret_store, transport)
    with TestClient(app) as client:
        configured = client.put(
            "/api/v1/providers/deepseek",
            json={"api_key": TEST_API_KEY, "base_url": f"  {override_url}/  "},
        )
        preserved = client.put("/api/v1/providers/deepseek", json={})

    assert configured.status_code == 200
    assert configured.json()["base_url"] == override_url
    assert configured.json()["base_url_overridden"] is True
    assert configured.json()["base_url_editable"] is True
    assert preserved.status_code == 200
    assert preserved.json()["base_url"] == override_url
    with sqlite3.connect(tmp_path / "pageferry.sqlite3") as connection:
        assert connection.execute(
            """
            SELECT base_url, base_url_override
            FROM provider_configs
            WHERE provider_id = 'deepseek'
            """
        ).fetchone() == (override_url, override_url)

    restarted_app = _create_test_app(tmp_path, secret_store, transport)
    with TestClient(restarted_app) as restarted_client:
        after_restart = restarted_client.get("/api/v1/providers")
        discovered = restarted_client.post(
            "/api/v1/providers/deepseek/models/discover",
            json={},
        )
        previewed_default = restarted_client.post(
            "/api/v1/providers/deepseek/models/discover",
            json={"base_url": None},
        )
        after_preview = restarted_client.get("/api/v1/providers")
        translator = restarted_app.state.provider_config_service.build_translator(
            "deepseek", "deepseek-v4-flash"
        )
        translated = translator.translate_batch(
            texts=["hello"],
            source_language="en",
            target_language="zh-CN",
            format_hint="txt",
        )
        restored = restarted_client.put(
            "/api/v1/providers/deepseek",
            json={"base_url": None},
        )

    restarted_by_id = {item["provider_id"]: item for item in after_restart.json()}
    assert restarted_by_id["deepseek"]["base_url"] == override_url
    assert discovered.status_code == 200
    assert previewed_default.status_code == 200
    after_preview_by_id = {item["provider_id"]: item for item in after_preview.json()}
    assert after_preview_by_id["deepseek"]["base_url"] == override_url
    assert after_preview_by_id["deepseek"]["base_url_overridden"] is True
    assert translated.items[0].text == "译文:hello"
    assert restored.status_code == 200
    assert restored.json()["base_url"] == "https://api.deepseek.com"
    assert restored.json()["base_url_overridden"] is False
    assert requested_urls == [
        f"{override_url}/models",
        f"{override_url}/chat/completions",
        f"{override_url}/models",
        f"{override_url}/chat/completions",
        f"{override_url}/models",
        "https://api.deepseek.com/models",
        f"{override_url}/chat/completions",
        "https://api.deepseek.com/models",
        "https://api.deepseek.com/chat/completions",
    ]
    with sqlite3.connect(tmp_path / "pageferry.sqlite3") as connection:
        assert connection.execute(
            """
            SELECT base_url, base_url_override
            FROM provider_configs
            WHERE provider_id = 'deepseek'
            """
        ).fetchone() == ("https://api.deepseek.com", None)


def test_discovery_accepts_temporary_preset_base_url_without_persisting(tmp_path) -> None:
    """未保存的高级设置应可直接同步模型, 但不能写入 URL、Keychain 或 SQLite。"""

    temporary_url = "https://preview-gateway.example/v1"
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """验证临时 discovery 确实使用 draft Base URL。"""

        requested_urls.append(str(request.url))
        return httpx.Response(200, json={"data": [{"id": "deepseek-v4-flash"}]})

    secret_store = MemorySecretStore()
    app = _create_test_app(tmp_path, secret_store, httpx.MockTransport(handler))
    with TestClient(app) as client:
        discovered = client.post(
            "/api/v1/providers/deepseek/models/discover",
            json={"api_key": TEST_API_KEY, "base_url": f"{temporary_url}/"},
        )
        listed = client.get("/api/v1/providers")

    assert discovered.status_code == 200
    assert discovered.json()["models"][0]["id"] == "deepseek-v4-flash"
    assert requested_urls == [f"{temporary_url}/models"]
    listed_by_id = {item["provider_id"]: item for item in listed.json()}
    assert listed_by_id["deepseek"]["base_url"] == "https://api.deepseek.com"
    assert listed_by_id["deepseek"]["base_url_overridden"] is False
    assert secret_store.values == {}
    with sqlite3.connect(tmp_path / "pageferry.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM provider_configs").fetchone() == (0,)


def test_catalog_configuration_falls_back_to_inference_when_model_list_fails(
    tmp_path,
) -> None:
    """Catalog model 不应被 `/models` 故障卡住, 但 discover/sync 仍显式失败。"""

    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """让 model-list 始终失败, 只允许真实 inference probe 成功。"""

        methods.append(request.method)
        if request.method == "GET":
            return httpx.Response(503, json={"error": {"message": "private outage"}})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "OK"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    secret_store = MemorySecretStore()
    app = _create_test_app(tmp_path, secret_store, httpx.MockTransport(handler))

    with TestClient(app) as client:
        configured = client.put(
            "/api/v1/providers/deepseek",
            json={"api_key": TEST_API_KEY, "model_id": "deepseek-v4-flash"},
        )
        with sqlite3.connect(tmp_path / "pageferry.sqlite3") as connection:
            before_failed_sync = connection.execute(
                """
                SELECT last_synced_at, updated_at
                FROM provider_configs
                WHERE provider_id = 'deepseek'
                """
            ).fetchone()
        preview = client.post(
            "/api/v1/providers/deepseek/models/discover",
            json={},
        )
        synced = client.post("/api/v1/providers/deepseek/models/sync")
        with sqlite3.connect(tmp_path / "pageferry.sqlite3") as connection:
            after_failed_sync = connection.execute(
                """
                SELECT last_synced_at, updated_at
                FROM provider_configs
                WHERE provider_id = 'deepseek'
                """
            ).fetchone()

    assert configured.status_code == 200
    assert configured.json()["enabled_model_ids"] == ["deepseek-v4-flash"]
    assert preview.status_code == 503
    assert synced.status_code == 503
    assert before_failed_sync == after_failed_sync
    assert methods == ["GET", "POST", "GET", "GET"]
    assert secret_store.values == {"keychain:provider/deepseek": TEST_API_KEY}


def test_remote_only_configuration_does_not_fallback_when_model_list_fails(tmp_path) -> None:
    """包含 remote-only model 的配置仍必须完成 discovery, 不能拿 catalog 冒充。"""

    secret_store = MemorySecretStore()
    app = _create_test_app(
        tmp_path,
        secret_store,
        httpx.MockTransport(
            lambda _: httpx.Response(
                503,
                json={"error": {"message": "private outage"}},
            )
        ),
    )

    with TestClient(app) as client:
        configured = client.put(
            "/api/v1/providers/kimi",
            json={
                "api_key": TEST_API_KEY,
                "enabled_model_ids": ["remote-only-model"],
                "default_model_id": "remote-only-model",
            },
        )

    assert configured.status_code == 503
    assert secret_store.values == {}
    with sqlite3.connect(tmp_path / "pageferry.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM provider_configs").fetchone() == (0,)


def test_remote_deepseek_model_does_not_receive_unverified_reasoning_fields(
    tmp_path,
) -> None:
    """Catalog 外 DeepSeek model 必须保持 provider_default, 不注入 thinking。"""

    payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """返回 remote-only inventory 并记录最小 inference payload。"""

        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "deepseek-remote-only"}]})
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "OK"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    app = _create_test_app(
        tmp_path,
        MemorySecretStore(),
        httpx.MockTransport(handler),
    )
    with TestClient(app) as client:
        configured = client.put(
            "/api/v1/providers/deepseek",
            json={
                "api_key": TEST_API_KEY,
                "enabled_model_ids": ["deepseek-remote-only"],
                "default_model_id": "deepseek-remote-only",
            },
        )
        translator = app.state.provider_config_service.build_translator(
            "deepseek",
            "deepseek-remote-only",
        )

    assert configured.status_code == 200
    remote = next(
        model for model in configured.json()["models"] if model["id"] == "deepseek-remote-only"
    )
    assert remote["reasoning_policy"] is None
    assert remote["supported_reasoning_policies"] == []
    assert len(payloads) == 1
    assert "thinking" not in payloads[0]
    assert "reasoning_effort" not in payloads[0]
    assert translator._reasoning_policy == "provider_default"


def test_blank_preset_base_url_restores_catalog_default(tmp_path) -> None:
    """显式空白 Base URL 与 null 一样恢复 catalog 默认值。"""

    secret_store = MemorySecretStore()
    requests: list[tuple[str, dict[str, object] | None]] = []
    app = _create_test_app(tmp_path, secret_store, _successful_handler(requests))

    with TestClient(app) as client:
        configured = client.put(
            "/api/v1/providers/deepseek",
            json={"api_key": TEST_API_KEY, "base_url": "https://gateway.example/v1"},
        )
        restored = client.put(
            "/api/v1/providers/deepseek",
            json={"base_url": "   "},
        )

    assert configured.status_code == 200
    assert restored.status_code == 200
    assert restored.json()["base_url"] == "https://api.deepseek.com"
    with sqlite3.connect(tmp_path / "pageferry.sqlite3") as connection:
        assert connection.execute(
            "SELECT base_url_override FROM provider_configs WHERE provider_id = 'deepseek'"
        ).fetchone() == (None,)


def test_preset_override_is_committed_only_after_successful_probe(tmp_path) -> None:
    """新 endpoint probe 失败时应继续保留上一份可用 URL 配置。"""

    override_url = "https://failing-gateway.example/v1"

    def handler(request: httpx.Request) -> httpx.Response:
        """默认 endpoint 成功, override 的 inference 则返回脱敏上游错误。"""

        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "deepseek-v4-flash"}]})
        if str(request.url).startswith(override_url):
            return httpx.Response(500, json={"error": {"message": "private upstream detail"}})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "OK"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    secret_store = MemorySecretStore()
    app = _create_test_app(tmp_path, secret_store, httpx.MockTransport(handler))
    with TestClient(app) as client:
        first = client.put(
            "/api/v1/providers/deepseek",
            json={"api_key": TEST_API_KEY},
        )
        failed = client.put(
            "/api/v1/providers/deepseek",
            json={"base_url": override_url},
        )
        listed = client.get("/api/v1/providers")

    assert first.status_code == 200
    assert failed.status_code == 503
    assert override_url not in failed.text
    assert "private upstream detail" not in failed.text
    listed_by_id = {item["provider_id"]: item for item in listed.json()}
    assert listed_by_id["deepseek"]["base_url"] == "https://api.deepseek.com"
    with sqlite3.connect(tmp_path / "pageferry.sqlite3") as connection:
        assert connection.execute(
            """
            SELECT base_url, base_url_override
            FROM provider_configs
            WHERE provider_id = 'deepseek'
            """
        ).fetchone() == ("https://api.deepseek.com", None)


def test_existing_configuration_allows_blank_key_to_reuse_keychain(tmp_path) -> None:
    """更新时空 key 复用已有 secret, 不重复写 Keychain."""

    secret_store = MemorySecretStore()
    requests: list[tuple[str, dict[str, object] | None]] = []
    transport = _successful_handler(requests)
    app = _create_test_app(tmp_path, secret_store, transport)

    with TestClient(app) as client:
        first = client.put(
            "/api/v1/providers/deepseek",
            json={"api_key": TEST_API_KEY},
        )
        second = client.put(
            "/api/v1/providers/deepseek",
            json={"api_key": "", "model_id": "deepseek-v4-flash"},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert secret_store.set_calls == 1
    assert secret_store.values["keychain:provider/deepseek"] == TEST_API_KEY
    assert [method for method, _ in requests] == ["GET", "POST", "GET", "POST"]


def test_generic_provider_discovery_is_read_only_and_configuration_enables_models(
    tmp_path,
) -> None:
    """Kimi discovery 不落盘凭据, 配置成功后才保存 enabled model inventory."""

    payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """返回 Kimi model list 与最小 inference response."""

        assert request.headers["Authorization"] == f"Bearer {TEST_API_KEY}"
        if request.method == "GET":
            assert str(request.url) == "https://api.moonshot.cn/v1/models"
            return httpx.Response(
                200,
                json={"data": [{"id": "kimi-translation", "owned_by": "moonshot"}]},
            )
        assert str(request.url) == "https://api.moonshot.cn/v1/chat/completions"
        payload = json.loads(request.content)
        payloads.append(payload)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "OK"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    secret_store = MemorySecretStore()
    app = _create_test_app(
        tmp_path,
        secret_store,
        httpx.MockTransport(handler),
    )

    with TestClient(app) as client:
        discovered = client.post(
            "/api/v1/providers/kimi/models/discover",
            json={"api_key": TEST_API_KEY},
        )
        assert secret_store.values == {}
        configured = client.put(
            "/api/v1/providers/kimi",
            json={
                "api_key": TEST_API_KEY,
                "enabled_model_ids": ["kimi-translation"],
                "default_model_id": "kimi-translation",
            },
        )

    assert discovered.status_code == 200
    assert discovered.json() == {
        "provider_id": "kimi",
        "models": [
            {
                "id": "kimi-k2.6",
                "display_name": "Kimi K2.6",
                "source": "catalog",
                "enabled": False,
                **_runtime_fields(
                    reasoning_policy="off",
                    supported_reasoning_policies=["provider_default", "off", "on"],
                ),
            },
            {
                "id": "kimi-translation",
                "display_name": "kimi-translation",
                "source": "remote",
                "enabled": False,
                **_runtime_fields(),
            },
        ],
    }
    assert configured.status_code == 200
    body = configured.json()
    assert body["enabled_model_ids"] == ["kimi-translation"]
    assert body["default_model_id"] == "kimi-translation"
    assert body["models"][0]["enabled"] is True
    assert secret_store.values == {"keychain:provider/kimi": TEST_API_KEY}
    assert len(payloads) == 1
    assert "thinking" not in payloads[0]
    assert "response_format" not in payloads[0]


def test_catalog_model_probe_uses_its_effective_reasoning_default(tmp_path) -> None:
    """配置 Kimi catalog model 时应按翻译 runtime 默认值关闭 thinking。"""

    payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """返回 Kimi catalog model, 并记录真实 inference probe payload。"""

        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "kimi-k2.6"}]})
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "OK"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    app = _create_test_app(
        tmp_path,
        MemorySecretStore(),
        httpx.MockTransport(handler),
    )
    with TestClient(app) as client:
        configured = client.put(
            "/api/v1/providers/kimi",
            json={
                "api_key": TEST_API_KEY,
                "enabled_model_ids": ["kimi-k2.6"],
                "default_model_id": "kimi-k2.6",
            },
        )

    assert configured.status_code == 200
    assert len(payloads) == 1
    assert payloads[0]["model"] == "kimi-k2.6"
    assert payloads[0]["thinking"] == {"type": "disabled"}


def test_enabled_model_runtime_settings_support_partial_update_and_reset(tmp_path) -> None:
    """Model settings 应区分字段缺省与 NULL, 并立即更新 effective 值。"""

    secret_store = MemorySecretStore()
    requests: list[tuple[str, dict[str, object] | None]] = []
    app = _create_test_app(tmp_path, secret_store, _successful_handler(requests))
    service = app.state.provider_config_service

    with TestClient(app) as client:
        configured = client.put(
            "/api/v1/providers/deepseek",
            json={"api_key": TEST_API_KEY},
        )
        updated = client.put(
            "/api/v1/providers/deepseek/models/deepseek-v4-flash/settings",
            json={
                "reasoning_policy_override": "high",
                "per_job_concurrency_override": 8,
                "global_concurrency_override": 20,
            },
        )
        partial = client.put(
            "/api/v1/providers/deepseek/models/deepseek-v4-flash/settings",
            json={"global_concurrency_override": 18},
        )
        first_translator = service.build_translator("deepseek", "deepseek-v4-flash")
        second_translator = service.build_translator("deepseek", "deepseek-v4-flash")
        configured_limiter = service._concurrency_registry.snapshot(
            ("deepseek", "deepseek-v4-flash")
        )
        unsupported = client.put(
            "/api/v1/providers/deepseek/models/deepseek-v4-flash/settings",
            json={"reasoning_policy_override": "medium"},
        )
        invalid_limits = client.put(
            "/api/v1/providers/deepseek/models/deepseek-v4-flash/settings",
            json={"per_job_concurrency_override": 19},
        )
        disabled_model = client.put(
            "/api/v1/providers/deepseek/models/deepseek-v4-pro/settings",
            json={"global_concurrency_override": 20},
        )
        reset = client.put(
            "/api/v1/providers/deepseek/models/deepseek-v4-flash/settings",
            json={
                "reasoning_policy_override": None,
                "per_job_concurrency_override": None,
                "global_concurrency_override": None,
            },
        )
        reset_limiter = service._concurrency_registry.snapshot(("deepseek", "deepseek-v4-flash"))
        listed = client.get("/api/v1/providers")

    assert configured.status_code == 200
    configured_model = next(
        model for model in configured.json()["models"] if model["id"] == "deepseek-v4-flash"
    )
    assert updated.status_code == 200
    assert updated.json() == {
        "id": "deepseek-v4-flash",
        "display_name": "DeepSeek V4 Flash",
        "source": "catalog",
        "enabled": True,
        "available": True,
        "probe_status": "succeeded",
        "probe_error_code": None,
        "latency_ms": configured_model["latency_ms"],
        "last_probed_at": configured_model["last_probed_at"],
        "reasoning_policy": "high",
        "reasoning_policy_override": "high",
        "supported_reasoning_policies": ["provider_default", "off", "high", "max"],
        "per_job_concurrency": 8,
        "per_job_concurrency_override": 8,
        "global_concurrency": 20,
        "global_concurrency_override": 20,
    }
    assert partial.status_code == 200
    assert partial.json()["reasoning_policy_override"] == "high"
    assert partial.json()["per_job_concurrency_override"] == 8
    assert partial.json()["global_concurrency"] == 18
    assert first_translator.per_job_concurrency == 8
    assert first_translator.global_concurrency == 18
    assert first_translator._reasoning_policy == "high"
    assert first_translator._concurrency_registry is second_translator._concurrency_registry
    assert configured_limiter is not None
    assert configured_limiter.limit == 18
    assert unsupported.status_code == 400
    assert unsupported.json()["code"] == "model"
    assert invalid_limits.status_code == 400
    assert disabled_model.status_code == 400
    assert reset.status_code == 200
    assert reset.json()["reasoning_policy"] == "off"
    assert reset.json()["reasoning_policy_override"] is None
    assert reset.json()["per_job_concurrency"] == 6
    assert reset.json()["global_concurrency"] == 15
    assert reset_limiter is not None
    assert reset_limiter.limit == 15
    listed_model = next(
        model
        for provider in listed.json()
        if provider["provider_id"] == "deepseek"
        for model in provider["models"]
        if model["id"] == "deepseek-v4-flash"
    )
    assert listed_model == reset.json()
    with sqlite3.connect(tmp_path / "pageferry.sqlite3") as connection:
        assert connection.execute(
            """
            SELECT reasoning_policy_override,
                   per_job_concurrency_override,
                   global_concurrency_override
            FROM provider_model_configs
            WHERE provider_id = 'deepseek' AND model_id = 'deepseek-v4-flash'
            """
        ).fetchone() == (None, None, None)


def test_settings_update_cannot_be_overwritten_by_a_stale_translator_build(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """较早读取旧值的 build 不能在 settings 保存后把 limiter 放宽回去。"""

    secret_store = MemorySecretStore()
    requests: list[tuple[str, dict[str, object] | None]] = []
    app = _create_test_app(tmp_path, secret_store, _successful_handler(requests))
    service = app.state.provider_config_service
    with TestClient(app) as client:
        configured = client.put(
            "/api/v1/providers/deepseek",
            json={"api_key": TEST_API_KEY},
        )
    assert configured.status_code == 200

    repository = service._repository
    original_get_model = repository.get_model
    first_read = Event()
    release_first = Event()
    second_read = Event()
    call_lock = Lock()
    call_count = 0

    def blocking_get_model(provider_id: str, model_id: str):
        """让首次 build 持有旧 snapshot, 观察 settings 是否能越过 service lock。"""

        nonlocal call_count
        model = original_get_model(provider_id, model_id)
        with call_lock:
            call_count += 1
            current_call = call_count
        if current_call == 1:
            first_read.set()
            assert release_first.wait(timeout=2)
        else:
            second_read.set()
        return model

    monkeypatch.setattr(repository, "get_model", blocking_get_model)
    with ThreadPoolExecutor(max_workers=2) as executor:
        build = executor.submit(
            service.build_translator,
            "deepseek",
            "deepseek-v4-flash",
        )
        assert first_read.wait(timeout=2)
        update = executor.submit(
            service.update_model_settings,
            "deepseek",
            "deepseek-v4-flash",
            global_concurrency_override=10,
        )
        # update 必须等 build 完成同一个 settings snapshot. 否则它先配成 10,
        # 随后恢复的 build 会再用旧值 15 覆盖。
        assert not second_read.wait(timeout=0.1)
        release_first.set()
        build.result(timeout=2)
        updated = update.result(timeout=2)

    limiter = service._concurrency_registry.snapshot(("deepseek", "deepseek-v4-flash"))
    assert updated.global_concurrency == 10
    assert limiter is not None
    assert limiter.limit == 10


def test_reconfigure_commit_never_exposes_mixed_url_and_secret(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keychain 与 SQLite 提交之间 build 只能看到完整的旧或新 snapshot。"""

    secret_store = MemorySecretStore()
    requests: list[tuple[str, dict[str, object] | None]] = []
    app = _create_test_app(
        tmp_path,
        secret_store,
        _successful_handler(requests),
    )
    service = app.state.provider_config_service
    with TestClient(app) as client:
        configured = client.put(
            "/api/v1/providers/deepseek",
            json={"api_key": "sk-old-snapshot"},
        )
    assert configured.status_code == 200

    repository = service._repository
    original_save = repository.save_successful_probe
    commit_entered = Event()
    release_commit = Event()

    def blocking_save_successful_probe(**kwargs):
        """停在新 Key 已写入而 SQLite 尚未提交的临界区。"""

        commit_entered.set()
        assert release_commit.wait(timeout=2)
        return original_save(**kwargs)

    monkeypatch.setattr(
        repository,
        "save_successful_probe",
        blocking_save_successful_probe,
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        reconfigure = executor.submit(
            lambda: asyncio.run(
                service.configure(
                    provider_id="deepseek",
                    api_key="sk-new-snapshot",
                    enabled_model_ids=["deepseek-v4-flash"],
                    default_model_id="deepseek-v4-flash",
                    base_url="https://new-gateway.example/v1",
                    base_url_was_provided=True,
                )
            )
        )
        assert commit_entered.wait(timeout=2)

        original_get_model = repository.get_model
        build_read = Event()

        def observed_get_model(provider_id: str, model_id: str):
            """记录 build 是否越过尚未完成的 provider state commit。"""

            build_read.set()
            return original_get_model(provider_id, model_id)

        monkeypatch.setattr(repository, "get_model", observed_get_model)
        build = executor.submit(
            service.build_translator,
            "deepseek",
            "deepseek-v4-flash",
        )
        try:
            assert not build_read.wait(timeout=0.1)
        finally:
            release_commit.set()
        reconfigure.result(timeout=2)
        translator = build.result(timeout=2)

    assert build_read.is_set()
    assert translator._api_key == "sk-new-snapshot"
    assert translator._chat_url == "https://new-gateway.example/v1/chat/completions"


def test_stale_reconfigure_cannot_overwrite_newer_secret_or_metadata(tmp_path) -> None:
    """较早开始的慢 probe 必须在较新配置提交后以 conflict 结束。"""

    slow_probe_started = Event()
    release_slow_probe = Event()

    def handler(request: httpx.Request) -> httpx.Response:
        """只阻塞 slow gateway 的 inference, 让 fast configure 先提交。"""

        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "deepseek-v4-flash"}]})
        if str(request.url).startswith("https://slow-gateway.example/"):
            slow_probe_started.set()
            assert release_slow_probe.wait(timeout=2)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "OK"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    secret_store = MemorySecretStore()
    app = _create_test_app(
        tmp_path,
        secret_store,
        httpx.MockTransport(handler),
    )
    service = app.state.provider_config_service
    with TestClient(app) as client:
        configured = client.put(
            "/api/v1/providers/deepseek",
            json={"api_key": "sk-initial"},
        )
    assert configured.status_code == 200

    with ThreadPoolExecutor(max_workers=1) as executor:
        stale = executor.submit(
            lambda: asyncio.run(
                service.configure(
                    provider_id="deepseek",
                    api_key="sk-stale",
                    enabled_model_ids=["deepseek-v4-flash"],
                    default_model_id="deepseek-v4-flash",
                    base_url="https://slow-gateway.example/v1",
                    base_url_was_provided=True,
                )
            )
        )
        assert slow_probe_started.wait(timeout=2)
        newer = asyncio.run(
            service.configure(
                provider_id="deepseek",
                api_key="sk-newer",
                enabled_model_ids=["deepseek-v4-flash"],
                default_model_id="deepseek-v4-flash",
                base_url="https://fast-gateway.example/v1",
                base_url_was_provided=True,
            )
        )
        release_slow_probe.set()
        with pytest.raises(ProviderConfigError) as stale_error:
            stale.result(timeout=2)

    assert newer.base_url == "https://fast-gateway.example/v1"
    assert stale_error.value.code is ProviderPublicErrorCode.CONFLICT
    assert secret_store.values["keychain:provider/deepseek"] == "sk-newer"
    current = service.list_statuses()[0]
    assert current.base_url == "https://fast-gateway.example/v1"


def test_reconfigure_rejects_model_settings_changed_during_probe(tmp_path) -> None:
    """Configure 必须验证 probe 使用的 model runtime snapshot 仍然有效。"""

    probe_started = Event()
    release_probe = Event()
    block_probe = False

    def handler(request: httpx.Request) -> httpx.Response:
        """在第二次配置的 inference 阶段留出 settings update 窗口。"""

        nonlocal block_probe
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "deepseek-v4-flash"}]})
        if block_probe:
            probe_started.set()
            assert release_probe.wait(timeout=2)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "OK"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    app = _create_test_app(
        tmp_path,
        MemorySecretStore(),
        httpx.MockTransport(handler),
    )
    service = app.state.provider_config_service
    with TestClient(app) as client:
        configured = client.put(
            "/api/v1/providers/deepseek",
            json={"api_key": TEST_API_KEY},
        )
    assert configured.status_code == 200

    block_probe = True
    with ThreadPoolExecutor(max_workers=1) as executor:
        stale_configure = executor.submit(
            lambda: asyncio.run(
                service.configure(
                    provider_id="deepseek",
                    api_key=None,
                    enabled_model_ids=["deepseek-v4-flash"],
                    default_model_id="deepseek-v4-flash",
                )
            )
        )
        assert probe_started.wait(timeout=2)
        updated = service.update_model_settings(
            "deepseek",
            "deepseek-v4-flash",
            reasoning_policy_override="high",
        )
        release_probe.set()
        with pytest.raises(ProviderConfigError) as stale_error:
            stale_configure.result(timeout=2)

    assert updated.reasoning_policy == "high"
    assert stale_error.value.code is ProviderPublicErrorCode.CONFLICT
    translator = service.build_translator("deepseek", "deepseek-v4-flash")
    assert translator._reasoning_policy == "high"


def test_concurrent_partial_settings_updates_preserve_each_other(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两个 partial PUT 必须串行读取最新 snapshot, 不能互相清掉未提交字段。"""

    requests: list[tuple[str, dict[str, object] | None]] = []
    app = _create_test_app(
        tmp_path,
        MemorySecretStore(),
        _successful_handler(requests),
    )
    service = app.state.provider_config_service
    with TestClient(app) as client:
        configured = client.put(
            "/api/v1/providers/deepseek",
            json={"api_key": TEST_API_KEY},
        )
    assert configured.status_code == 200

    repository = service._repository
    original_get_model = repository.get_model
    first_read = Event()
    release_first = Event()
    second_read = Event()
    call_lock = Lock()
    call_count = 0

    def blocking_get_model(provider_id: str, model_id: str):
        """固定两个 update 的读取顺序, 验证第二个读取被 service lock 阻挡。"""

        nonlocal call_count
        model = original_get_model(provider_id, model_id)
        with call_lock:
            call_count += 1
            current_call = call_count
        if current_call == 1:
            first_read.set()
            assert release_first.wait(timeout=2)
        else:
            second_read.set()
        return model

    monkeypatch.setattr(repository, "get_model", blocking_get_model)
    with ThreadPoolExecutor(max_workers=2) as executor:
        reasoning_update = executor.submit(
            service.update_model_settings,
            "deepseek",
            "deepseek-v4-flash",
            reasoning_policy_override="high",
        )
        assert first_read.wait(timeout=2)
        concurrency_update = executor.submit(
            service.update_model_settings,
            "deepseek",
            "deepseek-v4-flash",
            global_concurrency_override=20,
        )
        assert not second_read.wait(timeout=0.1)
        release_first.set()
        reasoning_update.result(timeout=2)
        final = concurrency_update.result(timeout=2)

    assert final.reasoning_policy_override == "high"
    assert final.global_concurrency_override == 20
    persisted = original_get_model("deepseek", "deepseek-v4-flash")
    assert persisted is not None
    assert persisted.reasoning_policy_override == "high"
    assert persisted.global_concurrency_override == 20


def test_stale_sync_result_cannot_merge_after_provider_reconfigure(tmp_path) -> None:
    """旧 endpoint 的慢 sync 必须在新配置提交后因 CAS 冲突而放弃 merge。"""

    old_sync_started = Event()
    release_old_sync = Event()
    block_old_models = False

    def handler(request: httpx.Request) -> httpx.Response:
        """分别模拟旧 endpoint 的慢 inventory 与新 endpoint 的当前 inventory。"""

        nonlocal block_old_models
        url = str(request.url)
        if request.method == "GET":
            if url.startswith("https://api.deepseek.com/"):
                if block_old_models:
                    old_sync_started.set()
                    assert release_old_sync.wait(timeout=2)
                model_id = "old-endpoint-only"
            else:
                assert url.startswith("https://new-gateway.example/v1/")
                model_id = "new-endpoint-only"
            return httpx.Response(200, json={"data": [{"id": model_id}]})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "OK"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    secret_store = MemorySecretStore()
    app = _create_test_app(
        tmp_path,
        secret_store,
        httpx.MockTransport(handler),
    )
    service = app.state.provider_config_service
    with TestClient(app) as client:
        configured = client.put(
            "/api/v1/providers/deepseek",
            json={"api_key": "sk-old-endpoint"},
        )
    assert configured.status_code == 200

    block_old_models = True
    with ThreadPoolExecutor(max_workers=1) as executor:
        stale_sync = executor.submit(lambda: asyncio.run(service.sync_inventory("deepseek")))
        assert old_sync_started.wait(timeout=2)
        reconfigured = asyncio.run(
            service.configure(
                provider_id="deepseek",
                api_key="sk-new-endpoint",
                enabled_model_ids=["deepseek-v4-flash"],
                default_model_id="deepseek-v4-flash",
                base_url="https://new-gateway.example/v1",
                base_url_was_provided=True,
            )
        )
        release_old_sync.set()
        with pytest.raises(ProviderConfigError) as stale_error:
            stale_sync.result(timeout=2)

    assert reconfigured.base_url == "https://new-gateway.example/v1"
    assert stale_error.value.code is ProviderPublicErrorCode.CONFLICT
    assert stale_error.value.status_code == 409
    assert secret_store.values["keychain:provider/deepseek"] == "sk-new-endpoint"
    with sqlite3.connect(tmp_path / "pageferry.sqlite3") as connection:
        rows = dict(
            connection.execute(
                """
                SELECT model_id, available
                FROM provider_model_configs
                WHERE provider_id = 'deepseek'
                  AND model_id IN ('old-endpoint-only', 'new-endpoint-only')
                """
            ).fetchall()
        )
    assert rows == {"old-endpoint-only": 0, "new-endpoint-only": 1}


def test_inventory_sync_is_idempotent_and_preserves_model_state(tmp_path) -> None:
    """远端 inventory 变化只切 available, 不破坏选择、默认值或 runtime override。"""

    inventories = [
        ["kimi-remote"],
        ["kimi-remote", "remote-new"],
        ["kimi-remote", "remote-new"],
        ["remote-new"],
        ["kimi-remote", "remote-new"],
    ]
    discovery_index = 0

    def handler(request: httpx.Request) -> httpx.Response:
        """按调用顺序返回配置与四轮同步使用的远端 inventory。"""

        nonlocal discovery_index
        assert request.headers["Authorization"] == f"Bearer {TEST_API_KEY}"
        if request.method == "GET":
            current = inventories[discovery_index]
            discovery_index += 1
            return httpx.Response(200, json={"data": [{"id": item} for item in current]})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "OK"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    secret_store = MemorySecretStore()
    app = _create_test_app(tmp_path, secret_store, httpx.MockTransport(handler))

    with TestClient(app) as client:
        configured = client.put(
            "/api/v1/providers/kimi",
            json={
                "api_key": TEST_API_KEY,
                "enabled_model_ids": ["kimi-remote"],
                "default_model_id": "kimi-remote",
            },
        )
        settings = client.put(
            "/api/v1/providers/kimi/models/kimi-remote/settings",
            json={
                "per_job_concurrency_override": 8,
                "global_concurrency_override": 20,
            },
        )
        first = client.post("/api/v1/providers/kimi/models/sync")
        repeated = client.post("/api/v1/providers/kimi/models/sync")
        missing = client.post("/api/v1/providers/kimi/models/sync")
        with sqlite3.connect(tmp_path / "pageferry.sqlite3") as connection:
            missing_row = connection.execute(
                """
                SELECT enabled, available,
                       per_job_concurrency_override,
                       global_concurrency_override
                FROM provider_model_configs
                WHERE provider_id = 'kimi' AND model_id = 'kimi-remote'
                """
            ).fetchone()
            default_model = connection.execute(
                """
                SELECT default_model_id
                FROM provider_configs
                WHERE provider_id = 'kimi'
                """
            ).fetchone()
        restored = client.post("/api/v1/providers/kimi/models/sync")

    assert configured.status_code == 200
    assert settings.status_code == 200
    assert first.status_code == 200
    assert {
        key: first.json()[key] for key in ("added", "restored", "unavailable", "unchanged")
    } == {"added": 1, "restored": 0, "unavailable": 0, "unchanged": 2}
    assert repeated.status_code == 200
    assert {
        key: repeated.json()[key] for key in ("added", "restored", "unavailable", "unchanged")
    } == {"added": 0, "restored": 0, "unavailable": 0, "unchanged": 3}
    assert missing.status_code == 200
    assert missing.json()["unavailable"] == 1
    missing_model = next(
        model for model in missing.json()["models"] if model["id"] == "kimi-remote"
    )
    assert missing_model["enabled"] is True
    assert missing_model["available"] is False
    assert missing_model["per_job_concurrency"] == 8
    assert missing_model["global_concurrency"] == 20
    assert missing_row == (1, 0, 8, 20)
    assert default_model == ("kimi-remote",)
    assert restored.status_code == 200
    assert restored.json()["restored"] == 1
    restored_model = next(
        model for model in restored.json()["models"] if model["id"] == "kimi-remote"
    )
    assert restored_model["available"] is True
    assert restored_model["enabled"] is True
    assert restored_model["global_concurrency_override"] == 20
    assert discovery_index == len(inventories)
    assert secret_store.set_calls == 1


def test_inventory_sync_requires_saved_key_and_remote_models_endpoint(tmp_path) -> None:
    """Sync 不接受 draft Key, 且没有 models endpoint 的 provider 应明确拒绝。"""

    secret_store = MemorySecretStore()
    app = _create_test_app(
        tmp_path,
        secret_store,
        httpx.MockTransport(lambda _: pytest.fail("rejected sync must not use HTTP")),
    )

    with TestClient(app) as client:
        unconfigured = client.post("/api/v1/providers/deepseek/models/sync")
        unsupported = client.post("/api/v1/providers/glm/models/sync")

    assert unconfigured.status_code == 409
    assert unconfigured.json()["code"] == "key"
    assert unsupported.status_code == 400
    assert unsupported.json()["code"] == "model"


def test_manual_model_is_registered_then_probed_and_survives_empty_sync(tmp_path) -> None:
    """手动 model 应先保持关闭, 真实 probe 成功后启用, 且不会被空远端列表隐藏。"""

    requests: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """远端始终不列出手动 model, 但接受该 id 的 inference。"""

        assert request.headers["Authorization"] == f"Bearer {TEST_API_KEY}"
        if request.method == "GET":
            requests.append((request.method, None))
            return httpx.Response(200, json={"data": []})
        payload = json.loads(request.content)
        requests.append((request.method, payload["model"]))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "OK"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    secret_store = MemorySecretStore()
    app = _create_test_app(
        tmp_path,
        secret_store,
        httpx.MockTransport(handler),
    )

    with TestClient(app) as client:
        custom = client.post(
            "/api/v1/providers/custom",
            json={
                "display_name": "Manual Gateway",
                "base_url": "http://127.0.0.1:11434/v1",
            },
        )
        provider_id = custom.json()["provider_id"]
        added = client.post(
            f"/api/v1/providers/{provider_id}/models",
            json={
                "model_id": "  org/translate-v2  ",
                "display_name": "  Translate V2  ",
            },
        )
        before_probe = client.get("/api/v1/providers")
        configured = client.put(
            f"/api/v1/providers/{provider_id}",
            json={
                "api_key": TEST_API_KEY,
                "enabled_model_ids": ["org/translate-v2"],
                "default_model_id": "org/translate-v2",
            },
        )
        synced = client.post(f"/api/v1/providers/{provider_id}/models/sync")
        duplicate = client.post(
            f"/api/v1/providers/{provider_id}/models",
            json={"model_id": "org/translate-v2"},
        )

    assert custom.status_code == 201
    assert added.status_code == 201
    assert added.json() == {
        "id": "org/translate-v2",
        "display_name": "Translate V2",
        "source": "manual",
        "enabled": False,
        **_runtime_fields(),
    }
    before_by_id = {provider["provider_id"]: provider for provider in before_probe.json()}
    assert before_by_id[provider_id]["models"] == [added.json()]
    assert before_by_id[provider_id]["enabled_model_ids"] == []
    assert configured.status_code == 200
    configured_model = next(
        model for model in configured.json()["models"] if model["id"] == "org/translate-v2"
    )
    assert configured_model["source"] == "manual"
    assert configured_model["enabled"] is True
    assert synced.status_code == 200
    synced_model = next(
        model for model in synced.json()["models"] if model["id"] == "org/translate-v2"
    )
    assert synced.json()["unavailable"] == 0
    assert synced_model["source"] == "manual"
    assert synced_model["available"] is True
    assert synced_model["enabled"] is True
    assert duplicate.status_code == 409
    assert duplicate.json() == {
        "code": "model",
        "message": "The model already exists in this provider inventory.",
    }
    assert requests == [
        ("GET", None),
        ("POST", "org/translate-v2"),
        ("GET", None),
    ]
    assert secret_store.set_calls == 1


def test_manual_model_rejects_invalid_unknown_and_catalog_duplicates(tmp_path) -> None:
    """手动入口应拒绝空白 id、未知 provider 与已有 catalog model。"""

    app = _create_test_app(
        tmp_path,
        MemorySecretStore(),
        httpx.MockTransport(lambda _: pytest.fail("manual registration must not use HTTP")),
    )
    with TestClient(app) as client:
        invalid = client.post(
            "/api/v1/providers/deepseek/models",
            json={"model_id": "bad model"},
        )
        duplicate = client.post(
            "/api/v1/providers/deepseek/models",
            json={"model_id": "deepseek-v4-flash"},
        )
        unknown = client.post(
            "/api/v1/providers/missing/models",
            json={"model_id": "manual-v1"},
        )

    assert invalid.status_code == 400
    assert invalid.json()["code"] == "model"
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "model"
    assert unknown.status_code == 404
    assert unknown.json()["code"] == "endpoint"


def test_manual_model_promotes_existing_remote_identity_without_losing_state(tmp_path) -> None:
    """用户固定 remote model 时应恢复 availability, 并保留 probe 与 runtime 字段。"""

    app = _create_test_app(
        tmp_path,
        MemorySecretStore(),
        httpx.MockTransport(lambda _: pytest.fail("manual promotion must not use HTTP")),
    )
    with TestClient(app) as client:
        with sqlite3.connect(tmp_path / "pageferry.sqlite3") as connection:
            connection.execute(
                """
                INSERT INTO provider_model_configs (
                    provider_id, model_id, upstream_model_id, display_name,
                    source, enabled, available, probe_status, probe_error_code,
                    latency_ms, last_seen_at, last_probed_at,
                    reasoning_policy_override,
                    per_job_concurrency_override,
                    global_concurrency_override,
                    created_at, updated_at
                )
                VALUES (
                    'deepseek', 'remote-pinned', 'remote-pinned', 'remote-pinned',
                    'remote', 0, 0, 'failed', 'model', 91, NULL, NULL,
                    NULL, 4, 9,
                    '2026-07-17T01:00:00Z', '2026-07-17T01:00:00Z'
                )
                """
            )
        promoted = client.post(
            "/api/v1/providers/deepseek/models",
            json={"model_id": "remote-pinned", "display_name": "Pinned Remote"},
        )
        with sqlite3.connect(tmp_path / "pageferry.sqlite3") as connection:
            stored = connection.execute(
                """
                SELECT display_name, source, enabled, available,
                       probe_status, probe_error_code, latency_ms,
                       per_job_concurrency_override, global_concurrency_override
                FROM provider_model_configs
                WHERE provider_id = 'deepseek' AND model_id = 'remote-pinned'
                """
            ).fetchone()

    assert promoted.status_code == 201
    assert promoted.json()["source"] == "manual"
    assert promoted.json()["available"] is True
    assert promoted.json()["per_job_concurrency"] == 4
    assert promoted.json()["global_concurrency"] == 9
    assert stored == ("Pinned Remote", "manual", 0, 1, "failed", "model", 91, 4, 9)


def test_deleting_unconfigured_preset_clears_manual_models(tmp_path) -> None:
    """Preset 尚未保存 Key 时, DELETE 仍应清理它的手动 inventory。"""

    app = _create_test_app(
        tmp_path,
        MemorySecretStore(),
        httpx.MockTransport(lambda _: pytest.fail("inventory cleanup must not use HTTP")),
    )
    with TestClient(app) as client:
        added = client.post(
            "/api/v1/providers/deepseek/models",
            json={"model_id": "manual-before-config"},
        )
        deleted = client.delete("/api/v1/providers/deepseek")
        listed = client.get("/api/v1/providers")

    assert added.status_code == 201
    assert deleted.status_code == 204
    deepseek = next(provider for provider in listed.json() if provider["provider_id"] == "deepseek")
    assert all(model["source"] != "manual" for model in deepseek["models"])


def test_manual_model_added_during_probe_rejects_stale_inventory_commit(tmp_path) -> None:
    """Probe 期间新增的手动 model 不能被较早开始的完整 inventory 保存覆盖。"""

    probe_started = Event()
    release_probe = Event()

    def handler(request: httpx.Request) -> httpx.Response:
        """先返回空远端 inventory, 再阻塞手动 model 的 inference。"""

        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        probe_started.set()
        assert release_probe.wait(timeout=2)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "OK"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    secret_store = MemorySecretStore()
    app = _create_test_app(
        tmp_path,
        secret_store,
        httpx.MockTransport(handler),
    )
    service = app.state.provider_config_service
    with TestClient(app):
        service.add_manual_model("deepseek", model_id="manual-first")

        with ThreadPoolExecutor(max_workers=1) as executor:
            stale = executor.submit(
                lambda: asyncio.run(
                    service.configure(
                        provider_id="deepseek",
                        api_key=TEST_API_KEY,
                        enabled_model_ids=["manual-first"],
                        default_model_id="manual-first",
                    )
                )
            )
            assert probe_started.wait(timeout=2)
            service.add_manual_model("deepseek", model_id="manual-late")
            release_probe.set()
            with pytest.raises(ProviderConfigError) as stale_error:
                stale.result(timeout=2)

        assert stale_error.value.code is ProviderPublicErrorCode.CONFLICT
        deepseek = next(
            provider for provider in service.list_statuses() if provider.provider_id == "deepseek"
        )
        manual_models = {model.id for model in deepseek.models if model.source == "manual"}
        assert manual_models == {"manual-first", "manual-late"}
        assert deepseek.configured is False
        assert secret_store.values == {}


def test_manual_model_can_probe_when_remote_inventory_endpoint_fails(tmp_path) -> None:
    """手动 model 不应被不可用的 `/models` 卡住, 但仍必须通过真实 inference。"""

    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """模拟网关不实现 models endpoint, 但 chat inference 正常。"""

        requests.append(request.method)
        if request.method == "GET":
            return httpx.Response(404, json={"error": {"message": "not found"}})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "OK"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    app = _create_test_app(
        tmp_path,
        MemorySecretStore(),
        httpx.MockTransport(handler),
    )
    with TestClient(app) as client:
        custom = client.post(
            "/api/v1/providers/custom",
            json={
                "display_name": "Chat-only Gateway",
                "base_url": "http://127.0.0.1:11434/v1",
            },
        )
        provider_id = custom.json()["provider_id"]
        added = client.post(
            f"/api/v1/providers/{provider_id}/models",
            json={"model_id": "chat-only-v1"},
        )
        configured = client.put(
            f"/api/v1/providers/{provider_id}",
            json={
                "api_key": TEST_API_KEY,
                "enabled_model_ids": ["chat-only-v1"],
                "default_model_id": "chat-only-v1",
            },
        )

    assert added.status_code == 201
    assert configured.status_code == 200
    assert configured.json()["enabled_model_ids"] == ["chat-only-v1"]
    assert requests == ["GET", "POST"]


def test_provider_without_models_endpoint_uses_catalog_discovery(tmp_path) -> None:
    """GLM 未公开 models endpoint 时只返回 catalog fallback, 不猜测远端路径."""

    secret_store = MemorySecretStore()
    app = _create_test_app(
        tmp_path,
        secret_store,
        httpx.MockTransport(lambda _: pytest.fail("catalog discovery must not use HTTP")),
    )

    with TestClient(app) as client:
        discovered = client.post(
            "/api/v1/providers/glm/models/discover",
            json={},
        )

    assert discovered.status_code == 200
    assert discovered.json()["models"] == [
        {
            "id": "glm-5.2",
            "display_name": "GLM 5.2",
            "source": "catalog",
            "enabled": False,
            **_runtime_fields(
                reasoning_policy="provider_default",
                supported_reasoning_policies=[
                    "provider_default",
                    "off",
                    "high",
                    "max",
                ],
            ),
        }
    ]
    assert secret_store.values == {}


def test_provider_status_overlays_new_catalog_baseline_without_writing_database(
    tmp_path,
) -> None:
    """GET 应叠加新增 catalog model, 并把同 upstream 历史行投影成可用 baseline。"""

    def handler(request: httpx.Request) -> httpx.Response:
        """只接受 GLM 配置时的最小 inference probe。"""

        assert request.method == "POST"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "OK"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    secret_store = MemorySecretStore()
    app = _create_test_app(tmp_path, secret_store, httpx.MockTransport(handler))

    with TestClient(app) as client:
        configured = client.put(
            "/api/v1/providers/glm",
            json={
                "api_key": TEST_API_KEY,
                "enabled_model_ids": ["glm-5.2"],
                "default_model_id": "glm-5.2",
            },
        )
        with sqlite3.connect(tmp_path / "pageferry.sqlite3") as connection:
            connection.execute(
                """
                UPDATE provider_model_configs
                SET display_name = 'glm-5.2', source = 'remote', available = 0
                WHERE provider_id = 'glm' AND model_id = 'glm-5.2'
                """
            )
        overlaid = client.get("/api/v1/providers")
        with sqlite3.connect(tmp_path / "pageferry.sqlite3") as connection:
            unchanged_row = connection.execute(
                """
                SELECT display_name, source, enabled, available
                FROM provider_model_configs
                WHERE provider_id = 'glm' AND model_id = 'glm-5.2'
                """
            ).fetchone()
            connection.execute(
                """
                DELETE FROM provider_model_configs
                WHERE provider_id = 'glm' AND model_id = 'glm-5.2'
                """
            )
        missing_row = client.get("/api/v1/providers")

    assert configured.status_code == 200
    overlaid_glm = next(
        provider for provider in overlaid.json() if provider["provider_id"] == "glm"
    )
    assert overlaid_glm["models"][0]["display_name"] == "GLM 5.2"
    assert overlaid_glm["models"][0]["source"] == "catalog"
    assert overlaid_glm["models"][0]["available"] is True
    assert overlaid_glm["models"][0]["enabled"] is True
    assert unchanged_row == ("glm-5.2", "remote", 1, 0)
    missing_glm = next(
        provider for provider in missing_row.json() if provider["provider_id"] == "glm"
    )
    assert missing_glm["default_model_id"] == "glm-5.2"
    assert missing_glm["models"][0]["id"] == "glm-5.2"
    assert missing_glm["models"][0]["available"] is True
    assert missing_glm["models"][0]["enabled"] is False


def test_provider_status_exposes_per_model_probe_state_and_latency(tmp_path) -> None:
    """Model 的失败状态必须独立返回, 不能被 provider 级 succeeded 掩盖。"""

    app = _create_test_app(
        tmp_path,
        MemorySecretStore(),
        _successful_handler([]),
    )
    failed_at = "2026-07-17T12:34:56+00:00"
    with TestClient(app) as client:
        configured = client.put(
            "/api/v1/providers/deepseek",
            json={"api_key": TEST_API_KEY, "model_id": "deepseek-v4-flash"},
        )
        with sqlite3.connect(tmp_path / "pageferry.sqlite3") as connection:
            connection.execute(
                """
                UPDATE provider_model_configs
                SET probe_status = 'failed',
                    probe_error_code = 'model',
                    latency_ms = 91,
                    last_probed_at = ?
                WHERE provider_id = 'deepseek' AND model_id = 'deepseek-v4-flash'
                """,
                (failed_at,),
            )
        listed = client.get("/api/v1/providers")

    assert configured.status_code == 200
    deepseek = next(provider for provider in listed.json() if provider["provider_id"] == "deepseek")
    assert deepseek["probe_status"] == "succeeded"
    flash = next(model for model in deepseek["models"] if model["id"] == "deepseek-v4-flash")
    assert flash["enabled"] is True
    assert flash["available"] is True
    assert flash["probe_status"] == "failed"
    assert flash["probe_error_code"] == "model"
    assert flash["latency_ms"] == 91
    assert flash["last_probed_at"] == failed_at


def test_custom_openai_provider_uses_shared_discovery_probe_and_keychain(
    tmp_path,
) -> None:
    """自定义 provider 应跨重启保留定义, 并复用统一 discovery 与 probe。"""

    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """验证规范化 endpoint, 并返回 OpenAI-compatible 最小响应。"""

        requests.append((request.method, str(request.url)))
        assert request.headers["Authorization"] == f"Bearer {TEST_API_KEY}"
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "local-translate"}]})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "OK"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    secret_store = MemorySecretStore()
    transport = httpx.MockTransport(handler)
    app = _create_test_app(tmp_path, secret_store, transport)

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/providers/custom",
            json={
                "display_name": "  Local Gateway  ",
                "base_url": "http://127.0.0.1:11434/v1/",
            },
        )
        custom_id = created.json()["provider_id"]
        discovered = client.post(
            f"/api/v1/providers/{custom_id}/models/discover",
            json={"api_key": TEST_API_KEY},
        )
        configured = client.put(
            f"/api/v1/providers/{custom_id}",
            json={
                "api_key": TEST_API_KEY,
                "enabled_model_ids": ["local-translate"],
                "default_model_id": "local-translate",
            },
        )
        listed = client.get("/api/v1/providers")
        translator = app.state.provider_config_service.build_translator(
            custom_id, "local-translate"
        )

    assert created.status_code == 201
    assert custom_id.startswith("custom-")
    assert created.json() == {
        "provider_id": custom_id,
        "display_name": "Local Gateway",
        "protocol": "openai",
        "is_custom": True,
        "base_url": "http://127.0.0.1:11434/v1",
        "base_url_overridden": False,
        "base_url_editable": False,
        "deletable": True,
        "available": True,
        "configured": False,
        "active": False,
        "probe_status": "not_configured",
        "probe_error_code": None,
        "latency_ms": None,
        "model_id": None,
        "default_model_id": None,
        "enabled_model_ids": [],
        "models": [],
        "supports_model_sync": True,
        "last_probed_at": None,
        "last_synced_at": None,
    }
    assert discovered.status_code == 200
    assert discovered.json()["models"] == [
        {
            "id": "local-translate",
            "display_name": "local-translate",
            "source": "remote",
            "enabled": False,
            **_runtime_fields(),
        }
    ]
    assert configured.status_code == 200
    assert configured.json()["configured"] is True
    assert configured.json()["enabled_model_ids"] == ["local-translate"]
    assert isinstance(translator, OpenAICompatibleProvider)
    assert translator._trust_env is False
    assert requests == [
        ("GET", "http://127.0.0.1:11434/v1/models"),
        ("GET", "http://127.0.0.1:11434/v1/models"),
        ("POST", "http://127.0.0.1:11434/v1/chat/completions"),
    ]
    assert [provider["provider_id"] for provider in listed.json()][-1] == custom_id
    assert secret_store.values == {f"keychain:provider/{custom_id}": TEST_API_KEY}

    restarted_app = _create_test_app(
        tmp_path,
        secret_store,
        httpx.MockTransport(lambda _: pytest.fail("listing must not call the custom endpoint")),
    )
    with TestClient(restarted_app) as restarted_client:
        after_restart = restarted_client.get("/api/v1/providers")
        deleted = restarted_client.delete(f"/api/v1/providers/{custom_id}")
        after_delete = restarted_client.get("/api/v1/providers")

    restarted_by_id = {provider["provider_id"]: provider for provider in after_restart.json()}
    assert restarted_by_id[custom_id] == configured.json()
    assert deleted.status_code == 204
    assert custom_id not in {provider["provider_id"] for provider in after_delete.json()}
    assert secret_store.values == {}
    with sqlite3.connect(tmp_path / "pageferry.sqlite3") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM custom_provider_definitions"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM provider_configs WHERE provider_id = ?",
            (custom_id,),
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    ("base_url", "normalized_url"),
    [
        ("http://localhost:11434/v1/", "http://localhost:11434/v1"),
        ("http://worker.localhost:11434/v1/", "http://worker.localhost:11434/v1"),
        ("http://127.42.0.9:11434/v1/", "http://127.42.0.9:11434/v1"),
        ("http://[::1]:11434/v1/", "http://[::1]:11434/v1"),
        ("https://192.168.1.20/v1/", "https://192.168.1.20/v1"),
        ("https://provider.example/v1/", "https://provider.example/v1"),
    ],
)
def test_custom_provider_allows_https_and_explicit_loopback_http(
    tmp_path,
    base_url: str,
    normalized_url: str,
) -> None:
    """公网和 LAN 走 HTTPS 时可用, HTTP 则只放行显式 loopback。"""

    secret_store = MemorySecretStore()
    app = _create_test_app(
        tmp_path,
        secret_store,
        httpx.MockTransport(lambda _: pytest.fail("creation must not use HTTP")),
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/providers/custom",
            json={"display_name": "Safe", "base_url": base_url},
        )

    assert response.status_code == 201
    assert response.json()["base_url"] == normalized_url
    assert response.json()["base_url_editable"] is False
    assert secret_store.values == {}


def test_preset_and_https_custom_adapters_keep_proxy_environment(tmp_path) -> None:
    """只有 loopback HTTP 绕过 proxy, preset 与 HTTPS custom 仍使用系统代理。"""

    secret_store = MemorySecretStore()
    app = _create_test_app(
        tmp_path,
        secret_store,
        httpx.MockTransport(lambda _: pytest.fail("adapter construction must not use HTTP")),
    )
    service = app.state.provider_config_service

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/providers/custom",
            json={"display_name": "HTTPS Gateway", "base_url": "https://provider.example/v1"},
        )

    custom_id = created.json()["provider_id"]
    model = ProviderInventoryItem(
        model_id="model",
        upstream_model_id="model",
        display_name="Model",
        source="remote",
    )
    preset_adapter = service._adapter(service._provider("deepseek"), TEST_API_KEY, model)
    custom_adapter = service._adapter(service._provider(custom_id), TEST_API_KEY, model)

    assert preset_adapter._trust_env is True
    assert custom_adapter._trust_env is True


def test_loopback_http_preset_override_bypasses_proxy_environment(tmp_path) -> None:
    """preset override 使用 loopback HTTP 时也必须阻止 proxy 携带 Bearer Key。"""

    loopback_url = "http://127.0.0.1:11434/v1"
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """记录 loopback 请求并返回可通过的 discovery 与 probe。"""

        requested_urls.append(str(request.url))
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "deepseek-v4-flash"}]})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "OK"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    secret_store = MemorySecretStore()
    app = _create_test_app(tmp_path, secret_store, httpx.MockTransport(handler))
    with TestClient(app) as client:
        configured = client.put(
            "/api/v1/providers/deepseek",
            json={"api_key": TEST_API_KEY, "base_url": f"{loopback_url}/"},
        )
        translator = app.state.provider_config_service.build_translator(
            "deepseek", "deepseek-v4-flash"
        )

    assert configured.status_code == 200
    assert configured.json()["base_url"] == loopback_url
    assert translator._trust_env is False
    assert requested_urls == [
        f"{loopback_url}/models",
        f"{loopback_url}/chat/completions",
    ]


def test_preset_override_rejects_unsafe_url_without_echo_or_side_effect(tmp_path) -> None:
    """preset override 与 custom 共用 URL 边界, 失败时不回显 URL 或 Key。"""

    unsafe_url = "http://private-gateway.example/v1"
    secret = "sk-private-override"
    secret_store = MemorySecretStore()
    app = _create_test_app(
        tmp_path,
        secret_store,
        httpx.MockTransport(lambda _: pytest.fail("invalid override must not use HTTP")),
    )

    with TestClient(app) as client:
        response = client.put(
            "/api/v1/providers/deepseek",
            json={"api_key": secret, "base_url": unsafe_url},
        )

    assert response.status_code == 400
    assert response.json() == {
        "code": "endpoint",
        "message": "The provider base URL is invalid.",
    }
    assert unsafe_url not in response.text
    assert secret not in response.text
    assert secret_store.values == {}
    with sqlite3.connect(tmp_path / "pageferry.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM provider_configs").fetchone() == (0,)


def test_custom_provider_put_does_not_claim_base_url_is_editable(tmp_path) -> None:
    """custom 定义首版只能删除重建, PUT 携带 Base URL 必须诚实拒绝。"""

    attempted_url = "https://replacement.example/v1"
    secret = "sk-private-custom-edit"
    secret_store = MemorySecretStore()
    app = _create_test_app(
        tmp_path,
        secret_store,
        httpx.MockTransport(lambda _: pytest.fail("rejected edit must not use HTTP")),
    )

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/providers/custom",
            json={"display_name": "Gateway", "base_url": "https://original.example/v1"},
        )
        custom_id = created.json()["provider_id"]
        rejected = client.put(
            f"/api/v1/providers/{custom_id}",
            json={"api_key": secret, "base_url": attempted_url},
        )
        rejected_discovery = client.post(
            f"/api/v1/providers/{custom_id}/models/discover",
            json={"api_key": secret, "base_url": attempted_url},
        )

    assert created.status_code == 201
    assert created.json()["base_url_editable"] is False
    assert rejected.status_code == 400
    assert rejected.json() == {
        "code": "endpoint",
        "message": "The custom provider base URL cannot be changed.",
    }
    assert attempted_url not in rejected.text
    assert secret not in rejected.text
    assert rejected_discovery.status_code == 400
    assert rejected_discovery.json() == rejected.json()
    assert attempted_url not in rejected_discovery.text
    assert secret not in rejected_discovery.text
    assert secret_store.values == {}


@pytest.mark.parametrize(
    "base_url",
    [
        "ftp://example.com/v1",
        "http://provider.example/v1",
        "http://192.168.1.20/v1",
        "http://10.0.0.8/v1",
        "http://[2001:db8::1]/v1",
        "https://user:private@example.com/v1",
        "https://example.com/v1?token=private",
        "https://example.com/v1?",
        "https://example.com/v1#private",
        "https://example.com/v1#",
        "https://exa mple.com/v1",
        "https://example.com:invalid/v1",
        r"https://example.com\evil",
    ],
)
def test_custom_provider_rejects_unsafe_base_urls(tmp_path, base_url: str) -> None:
    """自定义 endpoint 不得携带凭据、歧义字符或非 HTTP scheme。"""

    secret_store = MemorySecretStore()
    app = _create_test_app(
        tmp_path,
        secret_store,
        httpx.MockTransport(lambda _: pytest.fail("invalid URL must not use HTTP")),
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/providers/custom",
            json={"display_name": "Unsafe", "base_url": base_url},
        )

    assert response.status_code == 400
    assert response.json() == {
        "code": "endpoint",
        "message": "The custom provider base URL is invalid.",
    }
    assert "private" not in response.text
    with sqlite3.connect(tmp_path / "pageferry.sqlite3") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM custom_provider_definitions"
        ).fetchone() == (0,)


def test_provider_validation_errors_never_echo_secret_or_endpoint_input(tmp_path) -> None:
    """422 只返回字段位置与错误类型, 不回显 API Key、body 或 malformed URL。"""

    secret = "sk-private-validation-input"
    oversized_url = f"https://{'x' * 2100}.example/v1"
    secret_store = MemorySecretStore()
    app = _create_test_app(
        tmp_path,
        secret_store,
        httpx.MockTransport(lambda _: pytest.fail("invalid body must not use HTTP")),
    )

    with TestClient(app) as client:
        invalid_key = client.put(
            "/api/v1/providers/deepseek",
            json={"api_key": [secret], "model_id": "deepseek-v4-flash"},
        )
        invalid_url = client.post(
            "/api/v1/providers/custom",
            json={"display_name": "Oversized", "base_url": oversized_url},
        )
        invalid_preset_url = client.put(
            "/api/v1/providers/deepseek",
            json={"api_key": secret, "base_url": oversized_url},
        )
        invalid_discovery_url = client.post(
            "/api/v1/providers/deepseek/models/discover",
            json={"api_key": secret, "base_url": oversized_url},
        )
        forbidden_extra = client.post(
            "/api/v1/providers/custom",
            json={
                "display_name": "Extra",
                "base_url": "https://provider.example/v1",
                secret: "unexpected field value",
            },
        )

    for response in (
        invalid_key,
        invalid_url,
        invalid_preset_url,
        invalid_discovery_url,
        forbidden_extra,
    ):
        assert response.status_code == 422
        assert secret not in response.text
        assert oversized_url not in response.text
        for detail in response.json()["detail"]:
            assert set(detail) == {"type", "loc", "msg"}
            assert detail["loc"] == ["body"]
            assert detail["msg"] == "Invalid request value."
    assert secret_store.values == {}


@pytest.mark.parametrize(
    ("scenario", "expected_code", "expected_status"),
    [
        ("key", "key", 401),
        ("endpoint", "endpoint", 502),
        ("model", "model", 400),
        ("rate_limit", "rate_limit", 429),
        ("network", "network", 503),
        ("protocol", "protocol", 502),
        ("inference_protocol", "protocol", 502),
    ],
)
def test_provider_failures_are_redacted_and_never_persisted(
    tmp_path,
    scenario: str,
    expected_code: str,
    expected_status: int,
) -> None:
    """公开错误分类保持稳定, 且不改动两类 store."""

    secret = f"secret-{scenario}"
    upstream_private_detail = f"upstream rejected {secret}"

    def handler(request: httpx.Request) -> httpx.Response:
        """返回当前参数化场景指定的错误."""

        if scenario == "network":
            raise httpx.ConnectError(upstream_private_detail, request=request)
        if scenario == "key":
            return httpx.Response(401, json={"error": {"message": upstream_private_detail}})
        if scenario == "endpoint":
            return httpx.Response(404, json={"error": {"message": upstream_private_detail}})
        if scenario == "rate_limit":
            return httpx.Response(429, json={"error": {"message": upstream_private_detail}})
        if scenario == "model":
            if request.method == "GET":
                return httpx.Response(200, json={"data": [{"id": "another-model"}]})
            return httpx.Response(
                404,
                json={"error": {"message": upstream_private_detail}},
            )
        if scenario == "protocol":
            return httpx.Response(200, json={"data": upstream_private_detail})
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "deepseek-v4-flash"}]})
        return httpx.Response(200, json={"choices": []})

    secret_store = MemorySecretStore()
    app = _create_test_app(tmp_path, secret_store, httpx.MockTransport(handler))
    provider_id = "kimi" if scenario == "endpoint" else "deepseek"
    request_payload = (
        {
            "api_key": secret,
            "enabled_model_ids": ["remote-only-model"],
            "default_model_id": "remote-only-model",
        }
        if scenario == "endpoint"
        else {"api_key": secret, "model_id": "deepseek-v4-flash"}
    )
    with TestClient(app) as client:
        response = client.put(
            f"/api/v1/providers/{provider_id}",
            json=request_payload,
        )

    assert response.status_code == expected_status
    assert response.json()["code"] == expected_code
    assert secret not in response.text
    assert upstream_private_detail not in response.text
    assert secret_store.values == {}
    with sqlite3.connect(tmp_path / "pageferry.sqlite3") as connection:
        count = connection.execute("SELECT COUNT(*) FROM provider_configs").fetchone()[0]
    assert count == 0


def test_boot_token_protects_writes_and_delete_removes_both_stores(tmp_path) -> None:
    """可选 boot token 保护 POST、PUT 与 DELETE, GET 保持本地开放."""

    boot_token = "boot-test-secret"
    secret_store = MemorySecretStore()
    requests: list[tuple[str, dict[str, object] | None]] = []
    app = _create_test_app(
        tmp_path,
        secret_store,
        _successful_handler(requests),
        boot_token=boot_token,
    )

    with TestClient(app) as client:
        denied_create = client.post(
            "/api/v1/providers/custom",
            json={"display_name": "Denied", "base_url": "https://example.com/v1"},
        )
        denied_model = client.post(
            "/api/v1/providers/deepseek/models",
            json={"model_id": "denied-manual"},
        )
        denied_put = client.put(
            "/api/v1/providers/deepseek",
            json={"api_key": TEST_API_KEY},
        )
        denied_active = client.put(
            "/api/v1/providers/deepseek/active",
            json={"active": False},
        )
        denied_enabled = client.put(
            "/api/v1/providers/deepseek/models/deepseek-v4-flash/enabled",
            json={"enabled": True},
        )
        allowed_put = client.put(
            "/api/v1/providers/deepseek",
            headers={"X-PageFerry-Boot-Token": boot_token},
            json={"api_key": TEST_API_KEY},
        )
        open_get = client.get("/api/v1/providers")
        denied_delete = client.delete("/api/v1/providers/deepseek")
        allowed_delete = client.delete(
            "/api/v1/providers/deepseek",
            headers={"X-PageFerry-Boot-Token": boot_token},
        )
        after_delete = client.get("/api/v1/providers")

    assert denied_create.status_code == 401
    assert denied_model.status_code == 401
    assert denied_model.json() == {
        "detail": {"code": "unauthorized", "message": "Invalid boot token."}
    }
    assert denied_put.status_code == 401
    assert boot_token not in denied_put.text
    assert denied_active.status_code == 401
    assert denied_active.json() == denied_model.json()
    assert denied_enabled.status_code == 401
    assert denied_enabled.json() == denied_model.json()
    assert allowed_put.status_code == 200
    assert open_get.status_code == 200
    assert denied_delete.status_code == 401
    assert allowed_delete.status_code == 204
    assert allowed_delete.content == b""
    assert after_delete.json()[0]["configured"] is False
    assert secret_store.values == {}
    assert secret_store.delete_calls == 1
    with sqlite3.connect(tmp_path / "pageferry.sqlite3") as connection:
        count = connection.execute("SELECT COUNT(*) FROM provider_configs").fetchone()[0]
    assert count == 0


def test_translator_resolver_requires_verified_current_catalog_model(tmp_path) -> None:
    """Pipeline resolver 只返回已配置且具有有效 key 的 catalog model."""

    def handler(request: httpx.Request) -> httpx.Response:
        """响应 discovery, probe 与一次 indexed translation 请求."""

        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "deepseek-v4-flash"}]})
        payload = json.loads(request.content)
        if payload["messages"] == [{"role": "user", "content": "Reply with OK."}]:
            content = "OK"
        else:
            segment_payload = json.loads(payload["messages"][2]["content"])
            content = json.dumps(
                {
                    "segments": [
                        {"index": segment["index"], "text": f"translated:{segment['text']}"}
                        for segment in segment_payload["segments"]
                    ]
                }
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    secret_store = MemorySecretStore()
    app = _create_test_app(tmp_path, secret_store, httpx.MockTransport(handler))
    service = app.state.provider_config_service

    with TestClient(app) as client:
        with pytest.raises(ProviderConfigError) as unconfigured:
            service.build_translator("deepseek", "deepseek-v4-flash")
        configured = client.put(
            "/api/v1/providers/deepseek",
            json={"api_key": TEST_API_KEY},
        )
        translator = service.build_translator("deepseek", "deepseek-v4-flash")
        with pytest.raises(ProviderConfigError) as wrong_provider:
            service.build_translator("other", "deepseek-v4-flash")
        with pytest.raises(ProviderConfigError) as wrong_model:
            service.build_translator("deepseek", "unlisted-model")
        translated = translator.translate_batch(
            texts=["hello"],
            source_language="en",
            target_language="zh-CN",
            format_hint="txt",
        )

    assert configured.status_code == 200
    assert unconfigured.value.code is ProviderPublicErrorCode.KEY
    assert wrong_provider.value.code is ProviderPublicErrorCode.ENDPOINT
    assert wrong_model.value.code is ProviderPublicErrorCode.MODEL
    assert isinstance(translator, DeepSeekProvider)
    assert translated.items[0].text == "translated:hello"
    with sqlite3.connect(tmp_path / "pageferry.sqlite3") as connection:
        stored_values = connection.execute("SELECT * FROM provider_configs").fetchone()
    assert TEST_API_KEY not in repr(stored_values)

    secret_store.values.clear()
    with pytest.raises(ProviderConfigError) as missing_key:
        service.build_translator("deepseek", "deepseek-v4-flash")
    assert missing_key.value.code is ProviderPublicErrorCode.KEY


def test_metadata_write_failure_restores_previous_keychain_secret(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SQLite 更新失败时补偿已经发生的 Keychain 变更."""

    old_key = "old-provider-secret"
    new_key = "new-provider-secret"
    secret_store = MemorySecretStore()
    requests: list[tuple[str, dict[str, object] | None]] = []
    app = _create_test_app(
        tmp_path,
        secret_store,
        _successful_handler(requests),
    )
    service = app.state.provider_config_service

    def fail_save(**kwargs):
        """模拟 probe 成功后发生的脱敏 SQLite 错误."""

        del kwargs
        raise ProviderConfigRepositoryError("private sqlite detail")

    with TestClient(app) as client:
        first = client.put("/api/v1/providers/deepseek", json={"api_key": old_key})
        monkeypatch.setattr(service._repository, "save_successful_probe", fail_save)
        failed_update = client.put(
            "/api/v1/providers/deepseek",
            json={"api_key": new_key},
        )

    assert first.status_code == 200
    assert failed_update.status_code == 500
    assert failed_update.json()["code"] == "protocol"
    assert old_key not in failed_update.text
    assert new_key not in failed_update.text
    assert secret_store.values["keychain:provider/deepseek"] == old_key


def test_successful_probe_returns_models_without_post_commit_repository_read(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功 transaction 返回完整 snapshot, 不在提交后触发会误回滚 Keychain 的读取。"""

    secret_store = MemorySecretStore()
    requests: list[tuple[str, dict[str, object] | None]] = []
    app = _create_test_app(
        tmp_path,
        secret_store,
        _successful_handler(requests),
    )
    service = app.state.provider_config_service
    original_list_models = service._repository.list_models
    original_save = service._repository.save_successful_probe
    committed = False

    def fail_post_commit_read(provider_id: str):
        """模拟旧实现中 SQLite commit 后的 model inventory 读取故障。"""

        if committed:
            raise ProviderConfigRepositoryError("synthetic post-commit read failure")
        return original_list_models(provider_id)

    def save_and_mark_committed(**kwargs):
        """保留真实 transaction, 并从返回后开始禁止额外 inventory 读取。"""

        nonlocal committed
        result = original_save(**kwargs)
        committed = True
        return result

    monkeypatch.setattr(service._repository, "list_models", fail_post_commit_read)
    monkeypatch.setattr(service._repository, "save_successful_probe", save_and_mark_committed)
    with TestClient(app) as client:
        configured = client.put(
            "/api/v1/providers/deepseek",
            json={"api_key": TEST_API_KEY},
        )

    assert configured.status_code == 200
    assert configured.json()["enabled_model_ids"] == ["deepseek-v4-flash"]
    assert secret_store.values == {"keychain:provider/deepseek": TEST_API_KEY}
    with sqlite3.connect(tmp_path / "pageferry.sqlite3") as connection:
        assert connection.execute(
            "SELECT provider_id, probe_status FROM provider_configs"
        ).fetchone() == ("deepseek", "succeeded")


def test_provider_upsert_reuses_legacy_row_with_distinct_primary_id(tmp_path) -> None:
    """provider_id 唯一键应更新旧 id 行, 而不是插入第二份单 Key 配置。"""

    old_key = "legacy-provider-secret"
    new_key = "replacement-provider-secret"
    secret_ref = "keychain:provider/deepseek-default"
    secret_store = MemorySecretStore()
    requests: list[tuple[str, dict[str, object] | None]] = []
    app = _create_test_app(
        tmp_path,
        secret_store,
        _successful_handler(requests),
    )

    with TestClient(app) as client:
        with sqlite3.connect(tmp_path / "pageferry.sqlite3") as connection:
            connection.execute(
                """
                INSERT INTO provider_configs (
                    id, provider_id, model_id, default_model_id, base_url,
                    catalog_version, secret_ref, probe_status, created_at, updated_at
                )
                VALUES (
                    'deepseek-default', 'deepseek', 'legacy-model', 'legacy-model',
                    'https://api.deepseek.com', 'legacy', ?, 'succeeded',
                    '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
                )
                """,
                (secret_ref,),
            )
        secret_store.values[secret_ref] = old_key
        configured = client.put(
            "/api/v1/providers/deepseek",
            json={"api_key": new_key},
        )

    assert configured.status_code == 200
    assert secret_store.values == {secret_ref: new_key}
    with sqlite3.connect(tmp_path / "pageferry.sqlite3") as connection:
        rows = connection.execute(
            """
            SELECT id, provider_id, model_id, secret_ref
            FROM provider_configs
            WHERE provider_id = 'deepseek'
            """
        ).fetchall()
    assert rows == [("deepseek-default", "deepseek", "deepseek-v4-flash", secret_ref)]
