"""验证 provider 配置 API 集成行为."""

import json
import sqlite3
from pathlib import Path

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

TEST_API_KEY = "provider-test-secret"


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
            }
        ],
        "supports_model_sync": True,
        "last_probed_at": None,
        "last_synced_at": None,
    }
    assert configured.status_code == 200
    configured_body = configured.json()
    assert configured_body["configured"] is True
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
                "id": "kimi-translation",
                "display_name": "kimi-translation",
                "source": "remote",
                "enabled": False,
            }
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
        }
    ]
    assert secret_store.values == {}


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
            return httpx.Response(200, json={"data": [{"id": "another-model"}]})
        if scenario == "protocol":
            return httpx.Response(200, json={"data": upstream_private_detail})
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "deepseek-v4-flash"}]})
        return httpx.Response(200, json={"choices": []})

    secret_store = MemorySecretStore()
    app = _create_test_app(tmp_path, secret_store, httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = client.put(
            "/api/v1/providers/deepseek",
            json={"api_key": secret, "model_id": "deepseek-v4-flash"},
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
        denied_put = client.put(
            "/api/v1/providers/deepseek",
            json={"api_key": TEST_API_KEY},
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
    assert denied_put.status_code == 401
    assert boot_token not in denied_put.text
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

    def fail_post_commit_read(provider_id: str):
        """模拟旧实现中 SQLite commit 后的 model inventory 读取故障。"""

        del provider_id
        raise ProviderConfigRepositoryError("synthetic post-commit read failure")

    monkeypatch.setattr(service._repository, "list_models", fail_post_commit_read)
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
