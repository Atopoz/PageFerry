from fastapi import APIRouter

from modules.model_catalog import ModelCatalog, load_bundled_catalog

router = APIRouter(prefix="/model-catalog", tags=["model-catalog"])


@router.get("", response_model=ModelCatalog)
def get_model_catalog() -> ModelCatalog:
    return load_bundled_catalog()
