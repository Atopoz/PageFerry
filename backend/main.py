"""创建并暴露 PageFerry FastAPI sidecar 应用."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.registry import api_router
from api.v1.system import HealthResponse, health
from core.paths import resolve_app_paths
from core.settings import Settings, get_settings
from db.jobs import JobRepository
from db.sqlite import initialize_database
from modules.model_catalog.provider_config import (
    HttpClientFactory,
    ProviderConfigError,
    ProviderConfigService,
    SQLiteProviderConfigRepository,
)
from modules.model_catalog.secrets import KeyringSecretStore, SecretStore
from modules.pdf.assets import (
    find_pdf_asset,
    load_default_pdf_asset_manifest,
    pdf_asset_pack_path,
    pdf_asset_path,
)
from modules.pdf.lazy_layout import LazyLayoutDetector
from modules.pdf.resource_installer import PdfResourceInstaller
from modules.translation.jobs import TranslationJobService

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    secret_store: SecretStore | None = None,
    http_client_factory: HttpClientFactory | None = None,
    pdf_resource_installer: PdfResourceInstaller | None = None,
) -> FastAPI:
    """构造可注入 provider 与 PDF 资源基础设施的应用, 方便安全测试."""

    resolved_settings = settings or get_settings()
    paths = resolve_app_paths(resolved_settings.data_dir)
    provider_config_service = ProviderConfigService(
        SQLiteProviderConfigRepository(paths.database),
        (
            secret_store
            if secret_store is not None
            else KeyringSecretStore(service_name=resolved_settings.secret_service_name)
        ),
        http_client_factory=http_client_factory,
    )
    job_repository = JobRepository(paths.database)
    pdf_asset_manifest = load_default_pdf_asset_manifest()
    pdf_asset_pack = pdf_asset_pack_path(paths.root, pdf_asset_manifest)
    layout_asset = find_pdf_asset(pdf_asset_manifest, "pp-doclayout-v3-onnx")
    resolved_pdf_resource_installer = pdf_resource_installer or PdfResourceInstaller(
        pdf_asset_manifest,
        pdf_asset_pack,
    )
    configured_layout_model_path = resolved_settings.layout_model_path
    layout_detector = LazyLayoutDetector(
        configured_layout_model_path or pdf_asset_path(pdf_asset_pack, layout_asset),
        max_concurrency=resolved_settings.layout_max_concurrency,
        intra_op_threads=resolved_settings.layout_intra_op_threads,
        # canonical pack 由 installer 在首个 PDF 任务前统一校验模型和字体。
        # 显式 override 不属于 manifest pack, 仍交给 LayoutDetector 自行校验。
        resource_validator=(
            resolved_pdf_resource_installer.ensure_ready
            if configured_layout_model_path is None
            else None
        ),
        verify_model_checksum=configured_layout_model_path is not None,
    )
    translation_job_service = TranslationJobService(
        job_repository,
        provider_config_service,
        workspace_dir=paths.workspace,
        output_dir=paths.outputs,
        pdf_layout_detector=layout_detector,
        pdf_font_directory=pdf_asset_pack / "fonts",
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        """服务请求前创建本地路径并应用 SQLite migration."""

        paths.ensure()
        initialize_database(paths.database)
        # 启动前先尝试 reconciliation 0012 staging 的 Keychain refs。
        # 失败时保留 staging 并继续提供非敏感功能, 下次启动再安全重试。
        try:
            provider_config_service.reconcile_legacy_secret_references()
        except ProviderConfigError:
            # cleanup staging 会保留, 不能因临时 Keychain 故障制造 sidecar startup failure。
            # 固定 warning 不携带 exception, reference 或 Key; 下次启动会幂等重试。
            logger.warning("Provider credential reconciliation is pending; retrying next startup.")
        # BackgroundTasks 没有 durable worker; 重启后的 queued/running 都不可能自动继续。
        job_repository.mark_interrupted_jobs()
        resolved_pdf_resource_installer.initialize()
        try:
            yield
        finally:
            resolved_pdf_resource_installer.close()

    application = FastAPI(
        title=resolved_settings.app_name,
        version=resolved_settings.version,
        debug=resolved_settings.debug,
        lifespan=lifespan,
    )

    @application.exception_handler(RequestValidationError)
    async def handle_request_validation_error(
        _: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        """返回不含原始 input、ctx 或 URL 的全局 request validation 错误。"""

        # FastAPI 默认会把失败字段的 input 原样放进 422; provider API 中这可能是
        # API Key 或超长 endpoint, 因此这里只保留定位与稳定错误类型。
        details: list[dict[str, object]] = []
        for item in error.errors():
            raw_location = item.get("loc", ())
            scope = (
                raw_location[0]
                if raw_location and raw_location[0] in {"body", "query", "path", "header", "cookie"}
                else "request"
            )
            # 字段名也可能来自用户构造的 extra key, 因此 loc 只保留固定 request scope。
            details.append(
                {
                    "type": item.get("type", "value_error"),
                    "loc": [scope],
                    "msg": "Invalid request value.",
                }
            )
        return JSONResponse(status_code=422, content={"detail": details})

    application.state.settings = resolved_settings
    application.state.paths = paths
    application.state.provider_config_service = provider_config_service
    application.state.translation_job_service = translation_job_service
    application.state.layout_detector = layout_detector
    application.state.pdf_asset_manifest = pdf_asset_manifest
    application.state.pdf_asset_pack = pdf_asset_pack
    application.state.pdf_resource_installer = resolved_pdf_resource_installer
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(resolved_settings.allowed_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "X-PageFerry-Boot-Token"],
    )
    application.add_api_route("/healthz", health, response_model=HealthResponse, tags=["system"])
    application.include_router(api_router)
    return application
