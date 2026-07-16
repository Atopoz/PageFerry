from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from core.version import __version__

router = APIRouter(tags=["system"])


class HealthData(BaseModel):
    service: str
    version: str


class HealthResponse(BaseModel):
    code: Literal["success"] = "success"
    data: HealthData


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(data=HealthData(service="pageferry-api", version=__version__))
