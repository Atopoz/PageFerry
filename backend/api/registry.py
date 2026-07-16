from fastapi import APIRouter

from api.v1.model_catalog import router as model_catalog_router
from api.v1.system import router as system_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(system_router)
api_router.include_router(model_catalog_router)
