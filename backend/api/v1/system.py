"""提供 sidecar 健康检查与版本信息。"""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from core.version import __version__

router = APIRouter(tags=["system"])


class HealthData(BaseModel):
    """描述健康响应中的服务身份。"""

    service: str
    version: str


class HealthResponse(BaseModel):
    """定义稳定的成功响应 envelope。"""

    code: Literal["success"] = "success"
    data: HealthData


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """返回无需访问外部服务的进程健康状态。"""

    return HealthResponse(data=HealthData(service="pageferry-api", version=__version__))
