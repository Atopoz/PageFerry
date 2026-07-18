"""汇总 sidecar 暴露的版本化 API router."""

from fastapi import APIRouter

from api.v1.jobs import router as jobs_router
from api.v1.model_catalog import router as model_catalog_router
from api.v1.pdf_resources import router as pdf_resources_router
from api.v1.providers import router as providers_router
from api.v1.system import router as system_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(system_router)
api_router.include_router(model_catalog_router)
api_router.include_router(pdf_resources_router)
api_router.include_router(providers_router)
api_router.include_router(jobs_router)
