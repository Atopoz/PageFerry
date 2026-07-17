"""暴露随应用版本发布的只读 provider/model catalog。"""

from fastapi import APIRouter

from modules.model_catalog import ModelCatalog, load_bundled_catalog

router = APIRouter(prefix="/model-catalog", tags=["model-catalog"])


@router.get("", response_model=ModelCatalog)
def get_model_catalog() -> ModelCatalog:
    """返回已通过 schema 与引用校验的内置 catalog。"""

    return load_bundled_catalog()
