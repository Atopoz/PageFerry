"""验证 DeepSeek adapter 请求, response 与脱敏行为."""

import asyncio
import json

import httpx2 as httpx
import pytest

from modules.model_catalog.providers import (
    DeepSeekProvider,
    OpenAICompatibleProvider,
    ProviderErrorCode,
    ProviderRequestError,
    ProviderTranslationRequest,
)
from modules.model_catalog.providers import openai_compatible as openai_compatible_module
from modules.translation.model_runtime import ModelConcurrencyRegistry

TEST_API_KEY = "sensitive-test-key"


def test_direct_client_can_ignore_proxy_environment_for_loopback_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """trust_env=false 必须传入默认 client, 防止 loopback Key 被 HTTP_PROXY 截获。"""

    constructor_options: dict[str, object] = {}
    real_response = httpx.Response

    class RecordingAsyncClient:
        """记录 adapter 传给默认 httpx client 的安全选项。"""

        def __init__(self, **kwargs: object) -> None:
            """保存构造参数供断言。"""

            constructor_options.update(kwargs)

        async def __aenter__(self) -> "RecordingAsyncClient":
            """模拟 async context manager 进入。"""

            return self

        async def __aexit__(self, *args: object) -> None:
            """模拟 async context manager 退出。"""

            del args

        async def request(self, *args: object, **kwargs: object) -> httpx.Response:
            """返回确定性 model discovery 响应。"""

            del args, kwargs
            return real_response(200, json={"data": [{"id": "local-model"}]})

    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:8080")
    monkeypatch.setattr(
        openai_compatible_module.httpx,
        "AsyncClient",
        RecordingAsyncClient,
    )
    provider = OpenAICompatibleProvider(
        TEST_API_KEY,
        base_url="http://127.0.0.1:11434/v1",
        model_id="local-model",
        trust_env=False,
    )

    result = asyncio.run(provider.discover_models())

    assert [model.model_id for model in result.models] == ["local-model"]
    assert constructor_options["trust_env"] is False


def test_generic_openai_adapter_omits_provider_specific_request_fields() -> None:
    """通用 adapter 不得默认发送 thinking 或未确认的 JSON mode 字段."""

    captured_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        """记录 batch request 并返回 prompt contract 要求的 JSON 文本."""

        payload = json.loads(request.content)
        captured_payload.update(payload)
        segments = json.loads(payload["messages"][2]["content"])["segments"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "segments": [
                                        {"index": item["index"], "text": "translated"}
                                        for item in segments
                                    ]
                                }
                            ),
                        },
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(
        TEST_API_KEY,
        base_url="https://provider.example/v1",
        model_id="provider-model",
        client=client,
    )

    result = provider.translate_batch(
        texts=["source"],
        source_language="en",
        target_language="zh-CN",
        format_hint="txt",
    )
    asyncio.run(client.aclose())

    assert result.items[0].text == "translated"
    assert "thinking" not in captured_payload
    assert "response_format" not in captured_payload


def test_discover_models_uses_dedicated_models_endpoint() -> None:
    """Discovery 使用 bearer authentication 调用显式 model-list path."""

    def handler(request: httpx.Request) -> httpx.Response:
        """返回确定性的 DeepSeek model 列表."""

        assert request.method == "GET"
        assert str(request.url) == "https://api.deepseek.com/models"
        assert request.headers["Authorization"] == f"Bearer {TEST_API_KEY}"
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": "deepseek-v4-flash", "object": "model", "owned_by": "deepseek"},
                    {"id": "deepseek-v4", "object": "model", "owned_by": "deepseek"},
                ],
            },
        )

    async def exercise():
        """使用注入的 async client 执行 discovery."""

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await DeepSeekProvider(TEST_API_KEY, client=client).discover_models()

    result = asyncio.run(exercise())

    assert [model.model_id for model in result.models] == [
        "deepseek-v4-flash",
        "deepseek-v4",
    ]
    assert result.models[0].owned_by == "deepseek"
    assert result.latency_ms >= 0


def test_probe_runs_minimal_inference_with_thinking_disabled() -> None:
    """权威 probe 是一次关闭 thinking 的极小 generation."""

    captured_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        """捕获 probe payload, 并返回最小 completion."""

        assert request.method == "POST"
        assert str(request.url) == "https://api.deepseek.com/chat/completions"
        captured_payload.update(json.loads(request.content))
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

    async def exercise():
        """使用注入的 async client 执行最小 probe."""

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await DeepSeekProvider(TEST_API_KEY, client=client).probe_model(
                "deepseek-v4-flash"
            )

    result = asyncio.run(exercise())

    assert captured_payload == {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": "Reply with OK."}],
        "max_tokens": 8,
        "stream": False,
        "thinking": {"type": "disabled"},
    }
    assert result.model_id == "deepseek-v4-flash"
    assert result.latency_ms >= 0


def test_deepseek_reasoning_policy_enables_verified_effort() -> None:
    """模型设置选择 high 时应同时开启 thinking 并发送 effort。"""

    captured_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        """捕获 probe payload 并返回最小 completion。"""

        captured_payload.update(json.loads(request.content))
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

    async def exercise():
        """以 high reasoning policy 执行 probe。"""

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await DeepSeekProvider(
                TEST_API_KEY,
                client=client,
                reasoning_policy="high",
            ).probe_model("deepseek-v4-pro")

    asyncio.run(exercise())

    assert captured_payload["thinking"] == {"type": "enabled"}
    assert captured_payload["reasoning_effort"] == "high"


def test_translate_batch_retries_retryable_failure_outside_shared_slot() -> None:
    """429 可重试一次, 且最终可靠归还跨 job 共享的模型槽。"""

    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        """首个请求返回 429, 第二个请求返回合法 batch。"""

        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(429)
        payload = json.loads(request.content)
        segments = json.loads(payload["messages"][2]["content"])["segments"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "segments": [
                                        {"index": segment["index"], "text": "译文"}
                                        for segment in segments
                                    ]
                                }
                            ),
                        },
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    registry = ModelConcurrencyRegistry()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = DeepSeekProvider(
        TEST_API_KEY,
        client=client,
        concurrency_registry=registry,
        global_concurrency=2,
        batch_retry_delay_seconds=0,
    )

    result = provider.translate_batch(
        texts=["source"],
        source_language="en",
        target_language="zh-CN",
        format_hint="docx",
    )
    asyncio.run(client.aclose())

    assert result.items[0].text == "译文"
    assert requests == 2
    assert registry.snapshot(("deepseek", "deepseek-v4-flash")).active == 0  # type: ignore[union-attr]


def test_translate_keeps_system_and_source_messages_separate() -> None:
    """单文本翻译保持 system 与 user message 分离."""

    captured_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        """捕获翻译请求并返回 token usage."""

        captured_payload.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "Translated text"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 3,
                    "prompt_cache_hit_tokens": 7,
                },
            },
        )

    async def exercise():
        """执行一次底层翻译请求."""

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = DeepSeekProvider(TEST_API_KEY, client=client)
            return await provider.translate(
                ProviderTranslationRequest(
                    model_id="deepseek-v4-flash",
                    system_prompt="Translate faithfully.",
                    source_text="Source text",
                    max_output_tokens=256,
                )
            )

    result = asyncio.run(exercise())

    assert captured_payload == {
        "model": "deepseek-v4-flash",
        "messages": [
            {"role": "system", "content": "Translate faithfully."},
            {"role": "user", "content": "Source text"},
        ],
        "stream": False,
        "thinking": {"type": "disabled"},
        "max_tokens": 256,
    }
    assert result.text == "Translated text"
    assert result.finish_reason == "stop"
    assert result.usage.input_tokens == 12
    assert result.usage.output_tokens == 3
    assert result.usage.cache_read_tokens == 7


def test_translate_batch_is_sync_pipeline_bridge_with_index_and_usage() -> None:
    """同步 bridge 保留 index, 且只暴露有证据的 usage."""

    request_payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """确定性翻译带 index 的 JSON payload."""

        payload = json.loads(request.content)
        request_payloads.append(payload)
        segment_payload = json.loads(payload["messages"][2]["content"])
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "segments": [
                                        {
                                            "index": segment["index"],
                                            "text": f"translated:{segment['text']}",
                                        }
                                        for segment in segment_payload["segments"]
                                    ]
                                }
                            ),
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 4,
                    "completion_tokens": 2,
                    "prompt_cache_miss_tokens": 11,
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = DeepSeekProvider(TEST_API_KEY, client=client)

    results = provider.translate_batch(
        texts=["first", "  ", "second"],
        source_language=None,
        target_language="zh-CN",
        format_hint="docx",
    )
    asyncio.run(client.aclose())

    assert [(item.index, item.text) for item in results.items] == [
        (0, "translated:first"),
        (1, "  "),
        (2, "translated:second"),
    ]
    assert results.usage.input_tokens == 4
    assert results.usage.output_tokens == 2
    assert results.usage.cache_write_tokens is None
    assert len(request_payloads) == 1
    assert request_payloads[0]["model"] == "deepseek-v4-flash"
    assert request_payloads[0]["thinking"] == {"type": "disabled"}
    assert request_payloads[0]["response_format"] == {"type": "json_object"}
    assert request_payloads[0]["max_tokens"] == 8192
    assert request_payloads[0]["messages"][0]["role"] == "system"
    assert json.loads(request_payloads[0]["messages"][1]["content"])["format"] == "docx"
    assert json.loads(request_payloads[0]["messages"][2]["content"]) == {
        "segments": [
            {"index": 0, "text": "first"},
            {"index": 1, "text": "second"},
        ]
    }


def test_translate_batch_rejects_missing_or_duplicate_indexes() -> None:
    """异常 indexed translation JSON 会失败, 不会破坏 segment."""

    def handler(request: httpx.Request) -> httpx.Response:
        """返回重复 index 以验证 response 校验."""

        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "segments": [
                                        {"index": 0, "text": "first"},
                                        {"index": 0, "text": "duplicate"},
                                    ]
                                }
                            ),
                        },
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = DeepSeekProvider(TEST_API_KEY, client=client)

    with pytest.raises(ProviderRequestError) as error_info:
        provider.translate_batch(
            texts=["first", "second"],
            source_language="en",
            target_language="zh-CN",
            format_hint="docx",
        )
    asyncio.run(client.aclose())

    assert error_info.value.detail.code is ProviderErrorCode.INVALID_RESPONSE


def test_translate_batch_retries_only_unchanged_natural_language_outside_docx() -> None:
    """非 DOCX 格式只把漏译标题组成定向 retry batch, 并合并 usage。"""

    request_payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """首次保留原文, retry 时只翻译被判定为漏译的标题。"""

        payload = json.loads(request.content)
        request_payloads.append(payload)
        context = json.loads(payload["messages"][1]["content"])
        segment_payload = json.loads(payload["messages"][2]["content"])
        if context["format"].endswith("_quality_retry"):
            texts = ["# 收据事宜"]
        else:
            texts = [segment["text"] for segment in segment_payload["segments"]]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "segments": [
                                        {"index": index, "text": text}
                                        for index, text in enumerate(texts)
                                    ]
                                }
                            ),
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 4,
                    "completion_tokens": 2,
                    "prompt_cache_hit_tokens": 1,
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = DeepSeekProvider(TEST_API_KEY, client=client)
    heading = "# Receipt matters"
    mixed_brand = "卖方: Example Tech (SH) Co., Ltd."

    result = provider.translate_batch(
        texts=[heading, mixed_brand],
        source_language="en",
        target_language="zh-CN",
        format_hint="md",
    )
    asyncio.run(client.aclose())

    assert [item.text for item in result.items] == [
        "# 收据事宜",
        mixed_brand,
    ]
    assert len(request_payloads) == 2
    retry_context = json.loads(request_payloads[1]["messages"][1]["content"])
    retry_segments = json.loads(request_payloads[1]["messages"][2]["content"])
    assert retry_context["format"] == "md_quality_retry"
    assert "retry_reason" in retry_context
    assert retry_segments == {"segments": [{"index": 0, "text": heading}]}
    assert result.usage.input_tokens == 8
    assert result.usage.output_tokens == 4
    assert result.usage.cache_read_tokens == 2


def test_docx_batch_does_not_run_generic_unchanged_quality_retry() -> None:
    """DOCX 保留首轮合法结构结果, 不把专名误判成漏译后重发整批。"""

    request_payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """返回结构合法但包含应保留专名的 DOCX 结果。"""

        payload = json.loads(request.content)
        request_payloads.append(payload)
        segment_payload = json.loads(payload["messages"][2]["content"])
        texts = [segment["text"] for segment in segment_payload["segments"]]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "segments": [
                                        {"index": index, "text": text}
                                        for index, text in enumerate(texts)
                                    ]
                                }
                            ),
                        },
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = DeepSeekProvider(TEST_API_KEY, client=client)
    source = "[PARA_103]<span>Example Tech (SH) Co., Ltd.</span>"

    result = provider.translate_batch(
        texts=[source],
        source_language="en",
        target_language="zh-CN",
        format_hint="docx",
    )
    asyncio.run(client.aclose())

    assert [item.text for item in result.items] == [source]
    assert len(request_payloads) == 1


def test_authentication_error_does_not_expose_key_or_upstream_body() -> None:
    """Authentication 错误丢弃 API key 与上游 body."""

    upstream_detail = f"rejected {TEST_API_KEY}"

    def handler(request: httpx.Request) -> httpx.Response:
        """返回包含敏感细节的上游 body."""

        return httpx.Response(401, json={"error": {"message": upstream_detail}})

    async def exercise() -> ProviderRequestError:
        """捕获 adapter 脱敏后的 authentication 错误."""

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ProviderRequestError) as error_info:
                await DeepSeekProvider(TEST_API_KEY, client=client).discover_models()
            return error_info.value

    error = asyncio.run(exercise())

    assert error.detail.code is ProviderErrorCode.AUTHENTICATION_FAILED
    assert error.detail.status_code == 401
    assert TEST_API_KEY not in str(error)
    assert upstream_detail not in str(error)


def test_network_error_does_not_expose_exception_detail() -> None:
    """Transport 异常文本不能越过 provider 边界."""

    network_detail = f"socket failed with {TEST_API_KEY}"

    def handler(request: httpx.Request) -> httpx.Response:
        """抛出 message 含敏感文本的 timeout."""

        raise httpx.ReadTimeout(network_detail, request=request)

    async def exercise() -> ProviderRequestError:
        """捕获 adapter 脱敏后的 timeout 错误."""

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ProviderRequestError) as error_info:
                await DeepSeekProvider(TEST_API_KEY, client=client).discover_models()
            return error_info.value

    error = asyncio.run(exercise())

    assert error.detail.code is ProviderErrorCode.TIMEOUT
    assert error.detail.retryable is True
    assert TEST_API_KEY not in str(error)
    assert network_detail not in str(error)


def test_invalid_completion_is_structured() -> None:
    """缺失 completion choice 时映射为结构化 protocol 错误."""

    def handler(request: httpx.Request) -> httpx.Response:
        """返回无效的空 choices 列表."""

        return httpx.Response(200, json={"choices": []})

    async def exercise() -> ProviderRequestError:
        """捕获无效 completion 的处理结果."""

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ProviderRequestError) as error_info:
                await DeepSeekProvider(TEST_API_KEY, client=client).probe_model()
            return error_info.value

    error = asyncio.run(exercise())

    assert error.detail.code is ProviderErrorCode.INVALID_RESPONSE


def test_missing_api_key_fails_before_network_request() -> None:
    """空凭据在本地失败, 不发送请求."""

    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        """记录任何非预期网络访问."""

        nonlocal called
        called = True
        return httpx.Response(200, json={"data": []})

    async def exercise() -> ProviderRequestError:
        """捕获本地 missing-credential 错误."""

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ProviderRequestError) as error_info:
                await DeepSeekProvider(" ", client=client).discover_models()
            return error_info.value

    error = asyncio.run(exercise())

    assert error.detail.code is ProviderErrorCode.MISSING_CREDENTIALS
    assert called is False
