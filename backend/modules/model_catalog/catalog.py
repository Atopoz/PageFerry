import json
from importlib.resources import files
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ProviderDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    display_name: str
    protocol: Literal["openai", "anthropic", "gemini", "custom"]
    credential_type: Literal["api_key"] = "api_key"
    default_base_url: str | None = None
    base_url_editable: bool = False


class ModelDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    display_name: str
    capabilities: list[str] = Field(default_factory=list)


class ProviderModelDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str
    model_id: str
    upstream_model_id: str
    enabled_by_default: bool = True


class ModelCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int
    catalog_version: str
    released_at: str | None
    providers: list[ProviderDefinition]
    models: list[ModelDefinition]
    provider_models: list[ProviderModelDefinition]


def load_bundled_catalog() -> ModelCatalog:
    catalog_file = files("resources").joinpath("model_catalog/catalog.json")
    with catalog_file.open(encoding="utf-8") as handle:
        return ModelCatalog.model_validate(json.load(handle))
