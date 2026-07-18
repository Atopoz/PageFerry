"""提供 PDF required resource packs 的只读状态与显式安装控制 endpoint。"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict

from api.security import require_boot_token
from modules.pdf.resource_installer import (
    PdfResourceErrorCode,
    PdfResourceInstaller,
    PdfResourceState,
    PdfResourceStatus,
)

router = APIRouter(prefix="/pdf-resources", tags=["pdf-resources"])


class PdfResourcePackResponse(BaseModel):
    """返回一个 required pack 的体积、已完成字节与可用状态。"""

    model_config = ConfigDict(from_attributes=True)

    pack: str
    size_bytes: int
    completed_bytes: int
    ready: bool


class PdfResourceStatusData(BaseModel):
    """返回可直接轮询且不泄露本地路径或远端 URL 的安装状态。"""

    pack_revision: str
    state: PdfResourceState
    total_bytes: int
    completed_bytes: int
    current_asset_id: str | None
    error_code: PdfResourceErrorCode | None
    resources: list[PdfResourcePackResponse]


class PdfResourceStatusResponse(BaseModel):
    """使用统一 success envelope 包装 PDF 资源状态。"""

    code: Literal["success"] = "success"
    data: PdfResourceStatusData


def get_pdf_resource_installer(request: Request) -> PdfResourceInstaller:
    """读取应用 lifespan 管理的 app-scoped PDF 资源安装器。"""

    return request.app.state.pdf_resource_installer


@router.get("", response_model=PdfResourceStatusResponse)
def get_pdf_resource_status(
    installer: Annotated[PdfResourceInstaller, Depends(get_pdf_resource_installer)],
) -> PdfResourceStatusResponse:
    """返回 required packs 当前校验与安装进度。"""

    return _status_response(installer.status())


@router.post(
    "/install",
    response_model=PdfResourceStatusResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def install_pdf_resources(
    installer: Annotated[PdfResourceInstaller, Depends(get_pdf_resource_installer)],
    _: Annotated[None, Depends(require_boot_token)],
) -> PdfResourceStatusResponse:
    """显式启动单 worker 安装, 重复请求不会创建并发下载。"""

    return _status_response(installer.start_install())


@router.post("/cancel", response_model=PdfResourceStatusResponse)
def cancel_pdf_resource_install(
    installer: Annotated[PdfResourceInstaller, Depends(get_pdf_resource_installer)],
    _: Annotated[None, Depends(require_boot_token)],
) -> PdfResourceStatusResponse:
    """请求当前安装在下一个安全下载边界取消。"""

    return _status_response(installer.cancel())


def _status_response(resource_status: PdfResourceStatus) -> PdfResourceStatusResponse:
    """把内部 immutable status 显式投影为 API contract。"""

    return PdfResourceStatusResponse(
        data=PdfResourceStatusData(
            pack_revision=resource_status.pack_revision,
            state=resource_status.state,
            total_bytes=resource_status.total_bytes,
            completed_bytes=resource_status.completed_bytes,
            current_asset_id=resource_status.current_asset_id,
            error_code=resource_status.error_code,
            resources=[
                PdfResourcePackResponse.model_validate(resource)
                for resource in resource_status.resources
            ],
        )
    )
