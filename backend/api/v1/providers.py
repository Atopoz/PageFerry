"""提供 provider 列表、model discovery、安全配置与删除 endpoint."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from api.security import require_boot_token
from modules.model_catalog.provider_config import (
    ProviderConfigError,
    ProviderConfigService,
    ProviderPublicErrorCode,
)

router = APIRouter(prefix="/providers", tags=["providers"])

ProbeStatus = Literal["not_configured", "not_tested", "succeeded", "failed"]
ModelSource = Literal["remote", "catalog"]


class ProviderConfigureRequest(BaseModel):
    """接收新凭据、preset Base URL override 与用户启用的 model set。"""

    model_config = ConfigDict(extra="forbid")

    api_key: SecretStr | None = Field(default=None, repr=False)
    enabled_model_ids: list[str] | None = None
    default_model_id: str | None = None
    base_url: str | None = Field(default=None, max_length=2048)
    # 保留 v0.1 DeepSeek 单 model request, 旧 renderer 可无缝迁移。
    model_id: str | None = Field(default=None, min_length=1)


class ProviderDiscoveryRequest(BaseModel):
    """Model discovery 接收临时凭据与 Base URL, 都不会被持久化。"""

    model_config = ConfigDict(extra="forbid")

    api_key: SecretStr | None = Field(default=None, repr=False)
    base_url: str | None = Field(default=None, max_length=2048)


class CustomProviderCreateRequest(BaseModel):
    """接收一个由 PageFerry 生成稳定 id 的 OpenAI-compatible 定义。"""

    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=80)
    base_url: str = Field(min_length=1, max_length=2048)


class ProviderModelResponse(BaseModel):
    """一个不含价格或上游 payload 的 model 摘要."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    display_name: str | None = None
    source: ModelSource
    enabled: bool


class ProviderDiscoveryResponse(BaseModel):
    """返回一次不写入本地状态的 model discovery 结果."""

    model_config = ConfigDict(from_attributes=True)

    provider_id: str
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


class ProviderErrorResponse(BaseModel):
    """Provider 检查使用的稳定公开错误结构."""

    code: ProviderPublicErrorCode
    message: str


def get_provider_config_service(request: Request) -> ProviderConfigService:
    """读取应用初始化时创建的 app-scoped provider service."""

    return request.app.state.provider_config_service


@router.get("", response_model=list[ProviderStatusResponse])
def list_providers(
    service: Annotated[ProviderConfigService, Depends(get_provider_config_service)],
) -> list[ProviderStatusResponse] | JSONResponse:
    """列出所有已实现 provider 状态, 不读取或暴露 Keychain 值."""

    try:
        statuses = service.list_statuses()
    except ProviderConfigError as error:
        return _error_response(error)
    return [ProviderStatusResponse.model_validate(provider) for provider in statuses]


@router.post(
    "/custom",
    response_model=ProviderStatusResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        400: {"model": ProviderErrorResponse},
        401: {"model": ProviderErrorResponse},
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
    "/{provider_id}/models/discover",
    response_model=ProviderDiscoveryResponse,
    responses={
        400: {"model": ProviderErrorResponse},
        401: {"model": ProviderErrorResponse},
        429: {"model": ProviderErrorResponse},
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


@router.put(
    "/{provider_id}",
    response_model=ProviderStatusResponse,
    responses={
        400: {"model": ProviderErrorResponse},
        401: {"model": ProviderErrorResponse},
        404: {"model": ProviderErrorResponse},
        429: {"model": ProviderErrorResponse},
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
        401: {"model": ProviderErrorResponse},
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


def _error_response(error: ProviderConfigError) -> JSONResponse:
    """只序列化已经脱敏的 service 错误字段."""

    payload = ProviderErrorResponse(code=error.code, message=error.message)
    return JSONResponse(status_code=error.status_code, content=payload.model_dump(mode="json"))
