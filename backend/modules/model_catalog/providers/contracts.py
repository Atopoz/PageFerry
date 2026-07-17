"""定义 provider-neutral 结果与脱敏 error contract."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from modules.translation.contracts import BatchTranslator


class ProviderErrorCode(StrEnum):
    """用于安全映射的内部 provider 错误分类."""

    MISSING_CREDENTIALS = "missing_credentials"
    AUTHENTICATION_FAILED = "authentication_failed"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    INVALID_REQUEST = "invalid_request"
    MODEL_NOT_FOUND = "model_not_found"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    NETWORK_ERROR = "network_error"
    INVALID_RESPONSE = "invalid_response"
    EMPTY_RESPONSE = "empty_response"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    UPSTREAM_ERROR = "upstream_error"


@dataclass(frozen=True, slots=True)
class ProviderErrorDetail:
    """不含上游 payload 的安全 provider 错误详情."""

    code: ProviderErrorCode
    message: str
    retryable: bool = False
    status_code: int | None = None


class ProviderRequestError(RuntimeError):
    """公开信息可安全返回或记录的 provider 错误."""

    def __init__(self, detail: ProviderErrorDetail) -> None:
        """使用已脱敏的详情初始化异常."""

        super().__init__(detail.message)
        self.detail = detail


@dataclass(frozen=True, slots=True)
class DiscoveredModel:
    """Provider discovery 返回的一个 model 身份."""

    model_id: str
    owned_by: str | None = None


@dataclass(frozen=True, slots=True)
class ModelDiscoveryResult:
    """Model discovery 结果与实测请求 latency."""

    models: tuple[DiscoveredModel, ...]
    latency_ms: int


@dataclass(frozen=True, slots=True)
class InferenceProbeResult:
    """成功的最小 inference probe 结果."""

    model_id: str
    latency_ms: int


@dataclass(frozen=True, slots=True)
class ProviderTranslationRequest:
    """底层单文本翻译请求."""

    model_id: str
    system_prompt: str
    source_text: str
    max_output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """当前 DeepSeek 证据支持的 token 计数."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ProviderTranslationResult:
    """底层单文本翻译结果."""

    text: str
    finish_reason: str | None = None
    usage: TokenUsage = field(default_factory=TokenUsage)


class TranslationProvider(BatchTranslator, Protocol):
    """配置与翻译流程使用的完整 provider 行为."""

    async def discover_models(self) -> ModelDiscoveryResult:
        """列出当前凭据可见的 model."""

        ...

    async def probe_model(self, model_id: str) -> InferenceProbeResult:
        """对一个 model 执行最小 inference."""

        ...

    async def translate(self, request: ProviderTranslationRequest) -> ProviderTranslationResult:
        """翻译一个文本请求."""

        ...
