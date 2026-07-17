"""实现通用 OpenAI-compatible discovery, probe 与翻译 adapter."""

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext, suppress
from time import perf_counter, sleep
from typing import Any, Literal

import httpx2 as httpx

from modules.model_catalog.providers.contracts import (
    DiscoveredModel,
    InferenceProbeResult,
    ModelDiscoveryResult,
    ProviderErrorCode,
    ProviderErrorDetail,
    ProviderRequestError,
    ProviderTranslationRequest,
    ProviderTranslationResult,
    TokenUsage,
)
from modules.translation.contracts import (
    TranslationBatchItem,
    TranslationBatchResult,
    TranslationUsage,
)
from modules.translation.model_runtime import (
    DEFAULT_GLOBAL_CONCURRENCY,
    DEFAULT_PER_JOB_CONCURRENCY,
    MAX_MODEL_CONCURRENCY,
    ModelConcurrencyRegistry,
)
from modules.translation.prompt import TranslationMessages, build_translation_messages
from modules.translation.quality import should_retry_unchanged_translation

BATCH_MAX_TOKENS = 8192
DISABLED_THINKING = {"type": "disabled"}
ENABLED_THINKING = {"type": "enabled"}
_REASONING_POLICIES = {
    "provider_default",
    "off",
    "on",
    "low",
    "medium",
    "high",
    "max",
}

HttpClientFactory = Callable[[], httpx.AsyncClient]


class OpenAICompatibleProvider:
    """通过可配置的 OpenAI-compatible endpoint 执行发现与翻译."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str,
        model_id: str,
        chat_path: str = "/chat/completions",
        models_path: str | None = "/models",
        client: httpx.AsyncClient | None = None,
        client_factory: HttpClientFactory | None = None,
        timeout_seconds: float = 30.0,
        trust_env: bool = True,
        thinking_disabled: bool = False,
        json_response_format: bool = False,
        max_tokens_field: Literal["max_tokens", "max_completion_tokens"] = "max_tokens",
        provider_id: str | None = None,
        reasoning_policy: str | None = None,
        per_job_concurrency: int = DEFAULT_PER_JOB_CONCURRENCY,
        global_concurrency: int = DEFAULT_GLOBAL_CONCURRENCY,
        concurrency_registry: ModelConcurrencyRegistry | None = None,
        batch_retry_count: int = 1,
        batch_retry_delay_seconds: float = 0.5,
    ) -> None:
        """配置 endpoint 与可选的可注入 HTTP 边界."""

        if client is not None and client_factory is not None:
            raise ValueError("client and client_factory are mutually exclusive")
        self._api_key = api_key
        self._chat_url = _join_endpoint(base_url, chat_path)
        self._models_url = (
            _join_endpoint(base_url, models_path) if models_path is not None else None
        )
        self._model_id = model_id
        self._client = client
        self._client_factory = client_factory
        self._timeout_seconds = timeout_seconds
        self._trust_env = trust_env
        self._thinking_disabled = thinking_disabled
        self._json_response_format = json_response_format
        self._max_tokens_field = max_tokens_field
        self._provider_id = provider_id
        self._reasoning_policy = _normalize_reasoning_policy(
            reasoning_policy,
            thinking_disabled=thinking_disabled,
        )
        self._per_job_concurrency = _validate_concurrency(per_job_concurrency)
        self._global_concurrency = _validate_concurrency(global_concurrency)
        self._concurrency_registry = concurrency_registry
        if concurrency_registry is not None and (provider_id is None or not provider_id.strip()):
            raise ValueError("provider id is required when concurrency registry is configured")
        if isinstance(batch_retry_count, bool) or not 0 <= batch_retry_count <= 3:
            raise ValueError("batch retry count must be between 0 and 3")
        if batch_retry_delay_seconds < 0:
            raise ValueError("batch retry delay must not be negative")
        self._batch_retry_count = batch_retry_count
        self._batch_retry_delay_seconds = batch_retry_delay_seconds

    @property
    def per_job_concurrency(self) -> int:
        """返回格式 pipeline 可用于 fan-out 的单任务并发上限。"""

        return self._per_job_concurrency

    @property
    def global_concurrency(self) -> int:
        """返回当前模型跨 job 共享的有效并发上限。"""

        return self._global_concurrency

    async def discover_models(self) -> ModelDiscoveryResult:
        """请求并校验 provider 的显式 model-list endpoint."""

        if self._models_url is None:
            raise _provider_error(
                ProviderErrorCode.INVALID_REQUEST,
                "This provider does not expose model discovery.",
            )
        payload, latency_ms = await self._request_json("GET", self._models_url)
        raw_models = payload.get("data")
        if not isinstance(raw_models, list):
            raise _provider_error(
                ProviderErrorCode.INVALID_RESPONSE,
                "Model discovery returned an invalid response.",
            )

        models: list[DiscoveredModel] = []
        seen_model_ids: set[str] = set()
        for raw_model in raw_models:
            if not isinstance(raw_model, Mapping):
                raise _provider_error(
                    ProviderErrorCode.INVALID_RESPONSE,
                    "Model discovery returned an invalid model entry.",
                )
            model_id = raw_model.get("id")
            if not isinstance(model_id, str) or not model_id.strip():
                raise _provider_error(
                    ProviderErrorCode.INVALID_RESPONSE,
                    "Model discovery returned an invalid model entry.",
                )
            if model_id in seen_model_ids:
                continue
            owned_by = raw_model.get("owned_by")
            models.append(
                DiscoveredModel(
                    model_id=model_id,
                    owned_by=owned_by if isinstance(owned_by, str) else None,
                )
            )
            seen_model_ids.add(model_id)

        return ModelDiscoveryResult(models=tuple(models), latency_ms=latency_ms)

    async def probe_model(self, model_id: str | None = None) -> InferenceProbeResult:
        """执行一次极小的非 streaming inference."""

        model_id = model_id or self._model_id
        _require_non_empty(model_id, "model id")
        payload: dict[str, Any] = {
            "model": model_id,
            "messages": [{"role": "user", "content": "Reply with OK."}],
            "stream": False,
        }
        payload[self._max_tokens_field] = 8
        self._apply_provider_options(payload)
        response, latency_ms = await self._request_json(
            "POST", self._chat_url, json_payload=payload
        )
        _parse_completion(response)
        return InferenceProbeResult(model_id=model_id, latency_ms=latency_ms)

    async def translate(self, request: ProviderTranslationRequest) -> ProviderTranslationResult:
        """通过底层 provider contract 翻译一个文本."""

        _require_non_empty(request.model_id, "model id")
        _require_non_empty(request.source_text, "source text")
        if request.max_output_tokens is not None and request.max_output_tokens < 1:
            raise _provider_error(
                ProviderErrorCode.INVALID_REQUEST,
                "Maximum output tokens must be greater than zero.",
            )

        payload: dict[str, Any] = {
            "model": request.model_id,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.source_text},
            ],
            "stream": False,
        }
        self._apply_provider_options(payload)
        if request.max_output_tokens is not None:
            payload[self._max_tokens_field] = request.max_output_tokens

        response, _ = await self._request_json("POST", self._chat_url, json_payload=payload)
        return _parse_completion(response)

    def translate_batch(
        self,
        *,
        texts: Sequence[str],
        source_language: str | None,
        target_language: str,
        format_hint: str,
        read_only_context: Sequence[str] = (),
        repair_candidates: Sequence[str] = (),
    ) -> TranslationBatchResult:
        """为 event loop 外运行的文档 pipeline 提供同步 bridge."""

        # 格式 pipeline 刻意保持同步, provider I/O 则是 async. 把 bridge 留在这里,
        # 避免 DOCX/PPTX 代码依赖 event loop 或 httpx.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("translate_batch cannot run inside an active event loop")

        for attempt in range(self._batch_retry_count + 1):
            try:
                slot = (
                    self._concurrency_registry.slot(
                        (self._provider_id or "", self._model_id),
                        default_limit=self._global_concurrency,
                    )
                    if self._concurrency_registry is not None
                    else nullcontext()
                )
                with slot:
                    return asyncio.run(
                        self.translate_batch_async(
                            texts=texts,
                            source_language=source_language,
                            target_language=target_language,
                            format_hint=format_hint,
                            read_only_context=read_only_context,
                            repair_candidates=repair_candidates,
                        )
                    )
            except ProviderRequestError as error:
                if not error.detail.retryable or attempt >= self._batch_retry_count:
                    raise
                # 等待发生在共享 slot 之外; 暂时故障不能占着容量阻塞其他 job。
                sleep(self._batch_retry_delay_seconds * (2**attempt))
        raise AssertionError("batch retry loop exited without a result")

    async def translate_batch_async(
        self,
        *,
        texts: Sequence[str],
        source_language: str | None,
        target_language: str,
        format_hint: str,
        read_only_context: Sequence[str] = (),
        repair_candidates: Sequence[str] = (),
    ) -> TranslationBatchResult:
        """使用共享 prompt 与 response contract 翻译带 index 的 batch."""

        _require_non_empty(self._model_id, "model id")
        _require_non_empty(target_language, "target language")
        _require_non_empty(format_hint, "format hint")
        source_texts = tuple(texts)
        candidate_values = tuple(repair_candidates)
        if candidate_values and len(candidate_values) != len(source_texts):
            raise ValueError("repair candidate count must match segment count")
        translatable_indices = [
            index for index, source_text in enumerate(source_texts) if source_text.strip()
        ]
        if not translatable_indices:
            return TranslationBatchResult(
                items=tuple(
                    TranslationBatchItem(index=index, text=source_text)
                    for index, source_text in enumerate(source_texts)
                )
            )

        source_batch = [source_texts[index] for index in translatable_indices]
        candidate_batch = (
            [candidate_values[index] for index in translatable_indices] if candidate_values else []
        )
        messages = build_translation_messages(
            texts=source_batch,
            source_language=source_language,
            target_language=target_language,
            format_hint=format_hint,
            read_only_context=read_only_context,
            repair_candidates=candidate_batch,
        )
        completion = await self._complete_translation_batch(messages)
        translated_texts = list(
            _parse_batch_content(
                completion.text,
                expected_count=len(translatable_indices),
            )
        )
        # DOCX 已有 marker/span 结构 repair, 不再叠加通用 unchanged retry。
        # 对 DOCX 再发一个只含“疑似未翻译文本”的 batch, 会把地址和
        # 公司名等本来就应保留的内容误判为漏译。该 retry 一旦响应损坏, 还会丢弃首轮
        # 已经有效的整批结果。因此只让无 DOCX 结构 contract 的格式使用这条 heuristic。
        retry_positions = (
            [
                index
                for index, (source_text, translated_text) in enumerate(
                    zip(source_batch, translated_texts, strict=True)
                )
                if should_retry_unchanged_translation(
                    source_text=source_text,
                    translated_text=translated_text,
                    source_language=source_language,
                    target_language=target_language,
                )
            ]
            if not format_hint.startswith("docx")
            else []
        )
        usage = completion.usage
        if retry_positions:
            retry_sources = [source_batch[index] for index in retry_positions]
            retry_candidates = (
                [candidate_batch[index] for index in retry_positions] if candidate_batch else []
            )
            retry_messages = build_translation_messages(
                texts=retry_sources,
                source_language=source_language,
                target_language=target_language,
                format_hint=f"{format_hint}_quality_retry",
                read_only_context=read_only_context,
                repair_candidates=retry_candidates,
            )
            retry_completion = await self._complete_translation_batch(retry_messages)
            retry_texts = _parse_batch_content(
                retry_completion.text,
                expected_count=len(retry_positions),
            )
            usage = _merge_usage(usage, retry_completion.usage)
            for position, retry_text in zip(retry_positions, retry_texts, strict=True):
                # 第二次仍保持不变时保留原候选。它可能是品牌名, 不能为了通过
                # heuristic 强行伪造译文。
                if not should_retry_unchanged_translation(
                    source_text=source_batch[position],
                    translated_text=retry_text,
                    source_language=source_language,
                    target_language=target_language,
                ):
                    translated_texts[position] = retry_text

        translated_by_index = {
            source_index: translated_texts[batch_index]
            for batch_index, source_index in enumerate(translatable_indices)
        }
        return TranslationBatchResult(
            items=tuple(
                TranslationBatchItem(
                    index=index,
                    text=translated_by_index.get(index, source_text),
                )
                for index, source_text in enumerate(source_texts)
            ),
            usage=_to_translation_usage(usage),
        )

    async def _complete_translation_batch(
        self,
        messages: TranslationMessages,
    ) -> ProviderTranslationResult:
        """发送一次 JSON-mode batch completion, 并校验 completion 外壳。"""

        payload: dict[str, Any] = {
            "model": self._model_id,
            "messages": [
                {"role": "system", "content": messages.system},
                {"role": "user", "content": messages.task_context},
                {"role": "user", "content": messages.segment_payload},
            ],
            "stream": False,
        }
        payload[self._max_tokens_field] = BATCH_MAX_TOKENS
        self._apply_provider_options(payload, batch_json=True)
        response, _ = await self._request_json(
            "POST",
            self._chat_url,
            json_payload=payload,
        )
        return _parse_completion(response)

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        json_payload: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], int]:
        """发送一次请求, 返回校验后的 JSON 与实测 latency."""

        headers = self._authorization_headers()
        started_at = perf_counter()
        try:
            if self._client is not None:
                response = await self._client.request(
                    method,
                    url,
                    headers=headers,
                    json=json_payload,
                    timeout=self._timeout_seconds,
                )
            elif self._client_factory is not None:
                client = self._client_factory()
                try:
                    response = await client.request(
                        method,
                        url,
                        headers=headers,
                        json=json_payload,
                        timeout=self._timeout_seconds,
                    )
                finally:
                    # 清理失败不能用可能包含请求细节的 transport 内部信息覆盖脱敏结果.
                    with suppress(Exception):
                        await client.aclose()
            else:
                async with httpx.AsyncClient(
                    timeout=self._timeout_seconds,
                    trust_env=self._trust_env,
                ) as client:
                    response = await client.request(
                        method,
                        url,
                        headers=headers,
                        json=json_payload,
                    )
        except httpx.TimeoutException:
            # 不转发异常文本, transport 信息可能包含 URL, header 或 payload.
            raise _provider_error(
                ProviderErrorCode.TIMEOUT,
                "The model provider request timed out.",
                retryable=True,
            ) from None
        except httpx.RequestError:
            raise _provider_error(
                ProviderErrorCode.NETWORK_ERROR,
                "The model provider could not be reached.",
                retryable=True,
            ) from None

        latency_ms = round((perf_counter() - started_at) * 1000)
        if not 200 <= response.status_code < 300:
            raise _http_status_error(response.status_code)

        try:
            payload = response.json()
        except (TypeError, ValueError):
            raise _provider_error(
                ProviderErrorCode.INVALID_RESPONSE,
                "The model provider returned invalid JSON.",
            ) from None
        if not isinstance(payload, dict):
            raise _provider_error(
                ProviderErrorCode.INVALID_RESPONSE,
                "The model provider returned an invalid response.",
            )
        return payload, latency_ms

    def _authorization_headers(self) -> dict[str, str]:
        """拒绝空凭据后构造 authorization header."""

        if not self._api_key or not self._api_key.strip():
            raise _provider_error(
                ProviderErrorCode.MISSING_CREDENTIALS,
                "An API key is required for this provider.",
            )
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }

    def _apply_provider_options(
        self,
        payload: dict[str, Any],
        *,
        batch_json: bool = False,
    ) -> None:
        """只为明确支持的 provider 添加非 OpenAI 通用请求字段."""

        payload.update(_reasoning_payload(self._provider_id, self._reasoning_policy))
        if self._json_response_format and batch_json:
            # 只有已用真实 endpoint 确认 JSON mode 的 preset 才发送该字段。
            payload["response_format"] = {"type": "json_object"}


def _normalize_reasoning_policy(value: str | None, *, thinking_disabled: bool) -> str:
    """校验模型设置解析出的统一 reasoning policy。"""

    normalized = value.strip().lower() if isinstance(value, str) else ""
    if not normalized:
        normalized = "off" if thinking_disabled else "provider_default"
    if normalized not in _REASONING_POLICIES:
        raise ValueError(f"unsupported reasoning policy: {normalized}")
    return normalized


def _reasoning_payload(provider_id: str | None, policy: str) -> dict[str, Any]:
    """把 catalog 校验后的统一策略映射为各 preset 的请求字段。"""

    if policy == "provider_default":
        return {}
    if provider_id in {"deepseek", "kimi", "glm", "mimo"}:
        if policy == "off":
            return {"thinking": DISABLED_THINKING}
        if policy == "on":
            return {"thinking": ENABLED_THINKING}
        if policy in {"low", "medium", "high", "max"}:
            return {
                "thinking": ENABLED_THINKING,
                "reasoning_effort": policy,
            }
    # custom 与未核验 provider 只能使用 provider_default; catalog/service 会拒绝
    # 其他组合。这里保持保守, 不向未知 endpoint 注入私有字段。
    return {}


def _validate_concurrency(value: int) -> int:
    """校验 provider adapter 接收的模型并发配置。"""

    if isinstance(value, bool) or not 1 <= value <= MAX_MODEL_CONCURRENCY:
        raise ValueError(f"model concurrency must be between 1 and {MAX_MODEL_CONCURRENCY}")
    return value


def _join_endpoint(base_url: str, path: str) -> str:
    """拼接显式 provider base URL 与 endpoint path."""

    if not base_url or not base_url.strip():
        raise ValueError("base URL must not be empty")
    if not path.startswith("/"):
        raise ValueError("endpoint path must start with '/'")
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _require_non_empty(value: str, field_name: str) -> None:
    """为空的本地输入抛出结构化 invalid-request 错误."""

    if not value or not value.strip():
        raise _provider_error(
            ProviderErrorCode.INVALID_REQUEST,
            f"Provider {field_name} must not be empty.",
        )


def _parse_completion(payload: Mapping[str, Any]) -> ProviderTranslationResult:
    """不信任可选字段, 校验一次 OpenAI-compatible completion."""

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise _provider_error(
            ProviderErrorCode.INVALID_RESPONSE,
            "The model provider returned no completion choice.",
        )
    choice = choices[0]
    if not isinstance(choice, Mapping):
        raise _provider_error(
            ProviderErrorCode.INVALID_RESPONSE,
            "The model provider returned an invalid completion choice.",
        )
    message = choice.get("message")
    if not isinstance(message, Mapping):
        raise _provider_error(
            ProviderErrorCode.INVALID_RESPONSE,
            "The model provider returned an invalid completion message.",
        )
    content = message.get("content")
    if not isinstance(content, str):
        raise _provider_error(
            ProviderErrorCode.INVALID_RESPONSE,
            "The model provider returned invalid completion text.",
        )
    if not content.strip():
        raise _provider_error(
            ProviderErrorCode.EMPTY_RESPONSE,
            "The model provider returned an empty completion.",
        )

    finish_reason = choice.get("finish_reason")
    usage = payload.get("usage")
    return ProviderTranslationResult(
        text=content,
        finish_reason=finish_reason if isinstance(finish_reason, str) else None,
        usage=_parse_usage(usage),
    )


def _parse_usage(raw_usage: Any) -> TokenUsage:
    """读取 OpenAI-compatible response 中的 token 计数."""

    if not isinstance(raw_usage, Mapping):
        return TokenUsage()
    return TokenUsage(
        input_tokens=_optional_token_count(raw_usage.get("prompt_tokens")),
        output_tokens=_optional_token_count(raw_usage.get("completion_tokens")),
        cache_read_tokens=_optional_token_count(raw_usage.get("prompt_cache_hit_tokens")),
    )


def _optional_token_count(value: Any) -> int | None:
    """返回非负整数 token 计数, 无效时返回空值."""

    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _parse_batch_content(content: str, *, expected_count: int) -> tuple[str, ...]:
    """校验 model 翻译 JSON 中 index 完整且唯一."""

    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        raise _provider_error(
            ProviderErrorCode.INVALID_RESPONSE,
            "The model provider returned invalid translation JSON.",
        ) from None
    if not isinstance(payload, Mapping) or not isinstance(payload.get("segments"), list):
        raise _provider_error(
            ProviderErrorCode.INVALID_RESPONSE,
            "The model provider returned an invalid translation batch.",
        )

    translations: dict[int, str] = {}
    for raw_segment in payload["segments"]:
        if not isinstance(raw_segment, Mapping):
            raise _invalid_translation_segment()
        index = raw_segment.get("index")
        text = raw_segment.get("text")
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or not 0 <= index < expected_count
            or index in translations
            or not isinstance(text, str)
            or not text.strip()
        ):
            raise _invalid_translation_segment()
        translations[index] = text

    if set(translations) != set(range(expected_count)):
        raise _provider_error(
            ProviderErrorCode.INVALID_RESPONSE,
            "The model provider returned an incomplete translation batch.",
        )
    return tuple(translations[index] for index in range(expected_count))


def _invalid_translation_segment() -> ProviderRequestError:
    """为异常翻译 segment 创建统一的脱敏错误."""

    return _provider_error(
        ProviderErrorCode.INVALID_RESPONSE,
        "The model provider returned an invalid translation segment.",
    )


def _to_translation_usage(usage: TokenUsage) -> TranslationUsage:
    """把有证据的 provider 计数映射到公开 TranslationUsage."""

    return TranslationUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        # OpenAI-compatible usage 没有统一的 cache write 字段, 因此不虚构该指标.
        cache_write_tokens=None,
    )


def _merge_usage(first: TokenUsage, second: TokenUsage) -> TokenUsage:
    """合并初次翻译与定向 quality retry 的 token 计数。"""

    return TokenUsage(
        input_tokens=_sum_optional_counts(first.input_tokens, second.input_tokens),
        output_tokens=_sum_optional_counts(first.output_tokens, second.output_tokens),
        cache_read_tokens=_sum_optional_counts(
            first.cache_read_tokens,
            second.cache_read_tokens,
        ),
    )


def _sum_optional_counts(first: int | None, second: int | None) -> int | None:
    """相加可用计数; 两端都缺失时继续返回 None。"""

    if first is None and second is None:
        return None
    return (first or 0) + (second or 0)


def _http_status_error(status_code: int) -> ProviderRequestError:
    """不读取 body, 把 HTTP status 映射为安全 provider 错误详情."""

    if status_code in {401, 403}:
        return _provider_error(
            ProviderErrorCode.AUTHENTICATION_FAILED,
            "The model provider rejected the API key.",
            status_code=status_code,
        )
    if status_code == 402:
        return _provider_error(
            ProviderErrorCode.INSUFFICIENT_BALANCE,
            "The model provider account has insufficient balance.",
            status_code=status_code,
        )
    if status_code == 404:
        return _provider_error(
            ProviderErrorCode.MODEL_NOT_FOUND,
            "The requested model or endpoint was not found.",
            status_code=status_code,
        )
    if status_code == 408:
        return _provider_error(
            ProviderErrorCode.TIMEOUT,
            "The model provider request timed out.",
            retryable=True,
            status_code=status_code,
        )
    if status_code == 429:
        return _provider_error(
            ProviderErrorCode.RATE_LIMITED,
            "The model provider rate limit was reached.",
            retryable=True,
            status_code=status_code,
        )
    if status_code in {400, 422}:
        return _provider_error(
            ProviderErrorCode.INVALID_REQUEST,
            "The model provider rejected the request.",
            status_code=status_code,
        )
    if status_code >= 500:
        return _provider_error(
            ProviderErrorCode.UPSTREAM_UNAVAILABLE,
            "The model provider is temporarily unavailable.",
            retryable=True,
            status_code=status_code,
        )
    return _provider_error(
        ProviderErrorCode.UPSTREAM_ERROR,
        "The model provider request failed.",
        status_code=status_code,
    )


def _provider_error(
    code: ProviderErrorCode,
    message: str,
    *,
    retryable: bool = False,
    status_code: int | None = None,
) -> ProviderRequestError:
    """只用明确安全的字段构造 provider 异常."""

    return ProviderRequestError(
        ProviderErrorDetail(
            code=code,
            message=message,
            retryable=retryable,
            status_code=status_code,
        )
    )
