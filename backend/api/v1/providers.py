"""提供 provider 列表、model discovery、安全配置与删除 endpoint."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from api.security import SENSITIVE_RESPONSE_HEADERS, require_boot_token
from modules.model_catalog.provider_config import (
    ProviderConfigError,
    ProviderConfigService,
    ProviderPublicErrorCode,
)
from modules.translation.model_runtime import MAX_MODEL_CONCURRENCY

router = APIRouter(prefix="/providers", tags=["providers"])

_SECRET_RESPONSE_HEADERS = SENSITIVE_RESPONSE_HEADERS

ProbeStatus = Literal["not_configured", "not_tested", "succeeded", "failed"]
ModelProbeStatus = Literal["not_tested", "succeeded", "failed"]
ModelSource = Literal["remote", "catalog", "manual"]
ReasoningPolicy = Literal[
    "provider_default",
    "off",
    "on",
    "low",
    "medium",
    "high",
    "max",
]


class ProviderConfigureRequest(BaseModel):
    """接收新凭据、preset Base URL override 与用户启用的 model set。"""

    model_config = ConfigDict(extra="forbid")

    api_key: SecretStr | None = Field(default=None, repr=False)
    enabled_model_ids: list[str] | None = None
    default_model_id: str | None = None
    enable_all_models: bool = False
    base_url: str | None = Field(default=None, max_length=2048)
    # 保留 v0.1 DeepSeek 单 model request, 旧 renderer 可无缝迁移。
    model_id: str | None = Field(default=None, min_length=1)


class ProviderDiscoveryRequest(BaseModel):
    """Model discovery 接收临时凭据与 Base URL, 都不会被持久化。"""

    model_config = ConfigDict(extra="forbid")

    api_key: SecretStr | None = Field(default=None, repr=False)
    base_url: str | None = Field(default=None, max_length=2048)


class ProviderProbeRequest(BaseModel):
    """接收只供当次 inference probe 使用的临时配置。"""

    model_config = ConfigDict(extra="forbid")

    api_key: SecretStr | None = Field(default=None, repr=False)
    base_url: str | None = Field(default=None, max_length=2048)
    model_id: str | None = Field(default=None, min_length=1, max_length=256)


class CustomProviderCreateRequest(BaseModel):
    """接收一个由 PageFerry 生成稳定 id 的 OpenAI-compatible 定义。"""

    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=80)
    base_url: str = Field(min_length=1, max_length=2048)


class ProviderModelCreateRequest(BaseModel):
    """接收一个与上游请求值一致的手动 model id 和可选显示名。"""

    model_config = ConfigDict(extra="forbid")

    model_id: str = Field(min_length=1, max_length=256)
    display_name: str | None = Field(default=None, max_length=120)


class ProviderActiveRequest(BaseModel):
    """接收 provider 的非破坏 active 开关。"""

    model_config = ConfigDict(extra="forbid")

    active: bool


class ProviderModelEnabledRequest(BaseModel):
    """接收单个 model 的 enabled 目标状态。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool


class ProviderModelSettingsRequest(BaseModel):
    """按缺省保留、NULL 恢复的语义接收 model runtime override。"""

    model_config = ConfigDict(extra="forbid")

    reasoning_policy_override: ReasoningPolicy | None = None
    per_job_concurrency_override: int | None = Field(
        default=None,
        ge=1,
        le=MAX_MODEL_CONCURRENCY,
    )
    global_concurrency_override: int | None = Field(
        default=None,
        ge=1,
        le=MAX_MODEL_CONCURRENCY,
    )


class ProviderModelResponse(BaseModel):
    """一个不含价格或上游 payload 的 model 摘要."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    display_name: str | None = None
    source: ModelSource
    enabled: bool
    available: bool
    probe_status: ModelProbeStatus
    probe_error_code: str | None
    latency_ms: int | None
    last_probed_at: str | None
    reasoning_policy: ReasoningPolicy | None
    reasoning_policy_override: ReasoningPolicy | None
    supported_reasoning_policies: list[ReasoningPolicy]
    per_job_concurrency: int
    per_job_concurrency_override: int | None
    global_concurrency: int
    global_concurrency_override: int | None


class ProviderDiscoveryResponse(BaseModel):
    """返回一次不写入本地状态的 model discovery 结果."""

    model_config = ConfigDict(from_attributes=True)

    provider_id: str
    models: list[ProviderModelResponse]


class ProviderProbeResponse(BaseModel):
    """返回一次纯检测选中的 model 与 inference latency。"""

    model_config = ConfigDict(from_attributes=True)

    provider_id: str
    model_id: str
    display_name: str
    latency_ms: int


class ProviderInventorySyncResponse(BaseModel):
    """返回 inventory merge 的增量统计与当前可见 models。"""

    model_config = ConfigDict(from_attributes=True)

    provider_id: str
    added: int
    restored: int
    unavailable: int
    unchanged: int
    last_synced_at: str
    models: list[ProviderModelResponse]


class ProviderStatusResponse(BaseModel):
    """返回 frontend 且不含 secret 的 provider 状态."""

    model_config = ConfigDict(from_attributes=True)

    provider_id: str
    display_name: str
    protocol: Literal["openai"]
    is_custom: bool
    base_url: str
    base_url_overridden: bool
    base_url_editable: bool
    deletable: bool
    available: bool
    configured: bool
    active: bool
    probe_status: ProbeStatus
    probe_error_code: str | None
    latency_ms: int | None
    model_id: str | None
    default_model_id: str | None
    enabled_model_ids: list[str]
    models: list[ProviderModelResponse]
    supports_model_sync: bool
    last_probed_at: str | None
    last_synced_at: str | None


class ProviderApiKeyResponse(BaseModel):
    """只在专用受保护 endpoint 返回一个 provider 的完整 API Key。"""

    api_key: str = Field(repr=False)


class ProviderErrorResponse(BaseModel):
    """Provider 检查使用的稳定公开错误结构."""

    code: ProviderPublicErrorCode
    message: str


class BootTokenErrorDetail(BaseModel):
    """FastAPI HTTPException 包装的本地 write 认证错误。"""

    code: Literal["unauthorized"]
    message: str


class BootTokenErrorResponse(BaseModel):
    """描述 require_boot_token 实际返回的 detail envelope。"""

    detail: BootTokenErrorDetail


type ProviderUnauthorizedResponse = BootTokenErrorResponse | ProviderErrorResponse


def get_provider_config_service(request: Request) -> ProviderConfigService:
    """读取应用初始化时创建的 app-scoped provider service."""

    return request.app.state.provider_config_service


@router.get(
    "",
    response_model=list[ProviderStatusResponse],
    responses={500: {"model": ProviderErrorResponse}},
)
def list_providers(
    service: Annotated[ProviderConfigService, Depends(get_provider_config_service)],
) -> list[ProviderStatusResponse] | JSONResponse:
    """列出所有已实现 provider 状态, 不读取或暴露 Keychain 值."""

    try:
        statuses = service.list_statuses()
    except ProviderConfigError as error:
        return _error_response(error)
    return [ProviderStatusResponse.model_validate(provider) for provider in statuses]


@router.get(
    "/{provider_id}/api-key",
    response_model=ProviderApiKeyResponse,
    responses={
        200: {
            "headers": {
                "Cache-Control": {
                    "description": "禁止浏览器与中间缓存保存 API Key response。",
                    "schema": {"type": "string"},
                },
                "Pragma": {
                    "description": "兼容旧缓存实现的 no-cache 指令。",
                    "schema": {"type": "string"},
                },
                "Expires": {
                    "description": "立即过期当前 API Key response。",
                    "schema": {"type": "string"},
                },
            }
        },
        401: {"model": BootTokenErrorResponse},
        404: {"model": ProviderErrorResponse},
        409: {"model": ProviderErrorResponse},
        500: {"model": ProviderErrorResponse},
        503: {"model": ProviderErrorResponse},
    },
)
def get_provider_api_key(
    provider_id: str,
    response: Response,
    service: Annotated[ProviderConfigService, Depends(get_provider_config_service)],
    _: Annotated[None, Depends(require_boot_token)],
) -> ProviderApiKeyResponse | JSONResponse:
    """读取当前 provider 的 Keychain 凭据, 且明确禁止缓存 response。"""

    response.headers.update(_SECRET_RESPONSE_HEADERS)
    try:
        api_key = service.get_api_key(provider_id)
    except ProviderConfigError as error:
        return _error_response(error, headers=_SECRET_RESPONSE_HEADERS)
    return ProviderApiKeyResponse(api_key=api_key)


@router.post(
    "/custom",
    response_model=ProviderStatusResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        400: {"model": ProviderErrorResponse},
        401: {"model": BootTokenErrorResponse},
        500: {"model": ProviderErrorResponse},
    },
)
def create_custom_provider(
    payload: CustomProviderCreateRequest,
    service: Annotated[ProviderConfigService, Depends(get_provider_config_service)],
    _: Annotated[None, Depends(require_boot_token)],
) -> ProviderStatusResponse | JSONResponse:
    """创建未配置 Key 的自定义 OpenAI-compatible provider。"""

    try:
        provider = service.create_custom_provider(
            display_name=payload.display_name,
            base_url=payload.base_url,
        )
    except ProviderConfigError as error:
        return _error_response(error)
    return ProviderStatusResponse.model_validate(provider)


@router.post(
    "/{provider_id}/models",
    response_model=ProviderModelResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        400: {"model": ProviderErrorResponse},
        401: {"model": BootTokenErrorResponse},
        404: {"model": ProviderErrorResponse},
        409: {"model": ProviderErrorResponse},
        500: {"model": ProviderErrorResponse},
    },
)
def create_provider_model(
    provider_id: str,
    payload: ProviderModelCreateRequest,
    service: Annotated[ProviderConfigService, Depends(get_provider_config_service)],
    _: Annotated[None, Depends(require_boot_token)],
) -> ProviderModelResponse | JSONResponse:
    """登记一个默认关闭的手动 model, 后续仍须随 provider 配置完成 probe。"""

    try:
        model = service.add_manual_model(
            provider_id,
            model_id=payload.model_id,
            display_name=payload.display_name,
        )
    except ProviderConfigError as error:
        return _error_response(error)
    return ProviderModelResponse.model_validate(model)


@router.post(
    "/{provider_id}/probe",
    response_model=ProviderProbeResponse,
    responses={
        400: {"model": ProviderErrorResponse},
        401: {"model": ProviderUnauthorizedResponse},
        404: {"model": ProviderErrorResponse},
        429: {"model": ProviderErrorResponse},
        500: {"model": ProviderErrorResponse},
        502: {"model": ProviderErrorResponse},
        503: {"model": ProviderErrorResponse},
    },
)
async def probe_provider(
    provider_id: str,
    payload: ProviderProbeRequest,
    service: Annotated[ProviderConfigService, Depends(get_provider_config_service)],
    _: Annotated[None, Depends(require_boot_token)],
) -> ProviderProbeResponse | JSONResponse:
    """执行一次真实 inference, 且不保存凭据、URL、model 或启用状态。"""

    api_key = payload.api_key.get_secret_value() if payload.api_key is not None else None
    try:
        result = await service.probe(
            provider_id,
            api_key=api_key,
            base_url=payload.base_url,
            base_url_was_provided="base_url" in payload.model_fields_set,
            model_id=payload.model_id,
        )
    except ProviderConfigError as error:
        return _error_response(error)
    return ProviderProbeResponse.model_validate(result)


@router.post(
    "/{provider_id}/models/discover",
    response_model=ProviderDiscoveryResponse,
    responses={
        400: {"model": ProviderErrorResponse},
        401: {"model": ProviderUnauthorizedResponse},
        404: {"model": ProviderErrorResponse},
        429: {"model": ProviderErrorResponse},
        500: {"model": ProviderErrorResponse},
        502: {"model": ProviderErrorResponse},
        503: {"model": ProviderErrorResponse},
    },
)
async def discover_provider_models(
    provider_id: str,
    payload: ProviderDiscoveryRequest,
    service: Annotated[ProviderConfigService, Depends(get_provider_config_service)],
    _: Annotated[None, Depends(require_boot_token)],
) -> ProviderDiscoveryResponse | JSONResponse:
    """使用当次凭据或已存 Keychain 凭据安全发现 model."""

    api_key = payload.api_key.get_secret_value() if payload.api_key is not None else None
    try:
        result = await service.discover(
            provider_id,
            api_key=api_key,
            base_url=payload.base_url,
            base_url_was_provided="base_url" in payload.model_fields_set,
        )
    except ProviderConfigError as error:
        return _error_response(error)
    return ProviderDiscoveryResponse.model_validate(result)


@router.post(
    "/{provider_id}/models/sync",
    response_model=ProviderInventorySyncResponse,
    responses={
        400: {"model": ProviderErrorResponse},
        401: {"model": ProviderUnauthorizedResponse},
        404: {"model": ProviderErrorResponse},
        409: {"model": ProviderErrorResponse},
        429: {"model": ProviderErrorResponse},
        500: {"model": ProviderErrorResponse},
        502: {"model": ProviderErrorResponse},
        503: {"model": ProviderErrorResponse},
    },
)
async def sync_provider_models(
    provider_id: str,
    service: Annotated[ProviderConfigService, Depends(get_provider_config_service)],
    _: Annotated[None, Depends(require_boot_token)],
) -> ProviderInventorySyncResponse | JSONResponse:
    """只用 Keychain 中已保存的 Key 幂等同步 model inventory。"""

    try:
        result = await service.sync_inventory(provider_id)
    except ProviderConfigError as error:
        return _error_response(error)
    return ProviderInventorySyncResponse.model_validate(result)


@router.put(
    "/{provider_id}/models/{model_id:path}/enabled",
    response_model=ProviderStatusResponse,
    responses={
        400: {"model": ProviderErrorResponse},
        401: {"model": ProviderUnauthorizedResponse},
        404: {"model": ProviderErrorResponse},
        409: {"model": ProviderErrorResponse},
        429: {"model": ProviderErrorResponse},
        500: {"model": ProviderErrorResponse},
        502: {"model": ProviderErrorResponse},
        503: {"model": ProviderErrorResponse},
    },
)
async def update_provider_model_enabled(
    provider_id: str,
    model_id: str,
    payload: ProviderModelEnabledRequest,
    service: Annotated[ProviderConfigService, Depends(get_provider_config_service)],
    _: Annotated[None, Depends(require_boot_token)],
) -> ProviderStatusResponse | JSONResponse:
    """启用时完成真实 probe, 停用时原子维护 enabled/default。"""

    try:
        provider = await service.set_model_enabled(
            provider_id,
            model_id,
            enabled=payload.enabled,
        )
    except ProviderConfigError as error:
        return _error_response(error)
    return ProviderStatusResponse.model_validate(provider)


@router.put(
    "/{provider_id}/models/{model_id:path}/settings",
    response_model=ProviderModelResponse,
    responses={
        400: {"model": ProviderErrorResponse},
        401: {"model": BootTokenErrorResponse},
        404: {"model": ProviderErrorResponse},
        500: {"model": ProviderErrorResponse},
    },
)
def update_provider_model_settings(
    provider_id: str,
    model_id: str,
    payload: ProviderModelSettingsRequest,
    service: Annotated[ProviderConfigService, Depends(get_provider_config_service)],
    _: Annotated[None, Depends(require_boot_token)],
) -> ProviderModelResponse | JSONResponse:
    """更新已启用 model 的 reasoning 与两层并发 override。"""

    updates = {field: getattr(payload, field) for field in payload.model_fields_set}
    try:
        model = service.update_model_settings(
            provider_id,
            model_id,
            **updates,
        )
    except ProviderConfigError as error:
        return _error_response(error)
    return ProviderModelResponse.model_validate(model)


@router.put(
    "/{provider_id}/active",
    response_model=ProviderStatusResponse,
    responses={
        401: {"model": BootTokenErrorResponse},
        404: {"model": ProviderErrorResponse},
        409: {"model": ProviderErrorResponse},
        500: {"model": ProviderErrorResponse},
        503: {"model": ProviderErrorResponse},
    },
)
def update_provider_active(
    provider_id: str,
    payload: ProviderActiveRequest,
    service: Annotated[ProviderConfigService, Depends(get_provider_config_service)],
    _: Annotated[None, Depends(require_boot_token)],
) -> ProviderStatusResponse | JSONResponse:
    """非破坏停用 provider, 重新启用前检查 Key 与可用 model。"""

    try:
        provider = service.set_active(provider_id, active=payload.active)
    except ProviderConfigError as error:
        return _error_response(error)
    return ProviderStatusResponse.model_validate(provider)


@router.put(
    "/{provider_id}",
    response_model=ProviderStatusResponse,
    responses={
        400: {"model": ProviderErrorResponse},
        401: {"model": ProviderUnauthorizedResponse},
        404: {"model": ProviderErrorResponse},
        409: {"model": ProviderErrorResponse},
        429: {"model": ProviderErrorResponse},
        500: {"model": ProviderErrorResponse},
        502: {"model": ProviderErrorResponse},
        503: {"model": ProviderErrorResponse},
    },
)
async def configure_provider(
    provider_id: str,
    payload: ProviderConfigureRequest,
    service: Annotated[ProviderConfigService, Depends(get_provider_config_service)],
    _: Annotated[None, Depends(require_boot_token)],
) -> ProviderStatusResponse | JSONResponse:
    """所有启用 model 完成最小 inference 后才保存 provider 配置。"""

    api_key = payload.api_key.get_secret_value() if payload.api_key is not None else None
    try:
        provider = await service.configure(
            provider_id=provider_id,
            api_key=api_key,
            enabled_model_ids=payload.enabled_model_ids,
            default_model_id=payload.default_model_id,
            model_id=payload.model_id,
            enable_all_models=payload.enable_all_models,
            base_url=payload.base_url,
            base_url_was_provided="base_url" in payload.model_fields_set,
        )
    except ProviderConfigError as error:
        return _error_response(error)
    return ProviderStatusResponse.model_validate(provider)


@router.delete(
    "/{provider_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    responses={
        401: {"model": BootTokenErrorResponse},
        404: {"model": ProviderErrorResponse},
        500: {"model": ProviderErrorResponse},
        503: {"model": ProviderErrorResponse},
    },
)
def delete_provider(
    provider_id: str,
    service: Annotated[ProviderConfigService, Depends(get_provider_config_service)],
    _: Annotated[None, Depends(require_boot_token)],
) -> Response | JSONResponse:
    """清空 preset 配置, 或删除 custom 定义及其 Keychain 凭据。"""

    try:
        service.delete(provider_id)
    except ProviderConfigError as error:
        return _error_response(error)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _error_response(
    error: ProviderConfigError,
    *,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """只序列化已经脱敏的 service 错误字段."""

    payload = ProviderErrorResponse(code=error.code, message=error.message)
    return JSONResponse(
        status_code=error.status_code,
        content=payload.model_dump(mode="json"),
        headers=headers,
    )
