"""加载内置 provider catalog, 并执行类型与引用完整性校验."""

import json
from importlib.resources import files
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

type ReasoningPolicy = Literal[
    "provider_default",
    "off",
    "on",
    "low",
    "medium",
    "high",
    "max",
]


class ThinkingOptions(BaseModel):
    """Provider 的 thinking mode 请求选项."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["enabled", "disabled"]


class ProviderModelRequestOptions(BaseModel):
    """一组 provider-model 映射使用的默认请求选项."""

    model_config = ConfigDict(extra="forbid")

    thinking: ThinkingOptions | None = None


class ProviderDefinition(BaseModel):
    """一个 provider 身份及其显式 HTTP endpoint."""

    model_config = ConfigDict(extra="forbid")

    id: str
    display_name: str
    protocol: Literal["openai", "anthropic", "gemini", "custom"]
    credential_type: Literal["api_key"] = "api_key"
    available: bool = False
    default_base_url: str | None = None
    base_url_editable: bool = False
    chat_path: str | None = None
    models_path: str | None = None
    docs_url: str | None = None
    api_key_url: str | None = None

    @field_validator("chat_path", "models_path")
    @classmethod
    def validate_endpoint_path(cls, value: str | None) -> str | None:
        """确保 endpoint path 始终相对于 provider base URL."""

        if value is not None and not value.startswith("/"):
            raise ValueError("provider endpoint paths must start with '/'")
        return value


class ModelDefinition(BaseModel):
    """一个产品级 model 身份及其声明的能力."""

    model_config = ConfigDict(extra="forbid")

    id: str
    display_name: str
    capabilities: list[str] = Field(default_factory=list)


class ProviderModelDefinition(BaseModel):
    """把产品 model 映射到 provider 上游 model id."""

    model_config = ConfigDict(extra="forbid")

    provider_id: str
    model_id: str
    upstream_model_id: str
    enabled_by_default: bool = True
    supported_reasoning_policies: list[ReasoningPolicy] = Field(default_factory=list)
    default_reasoning_policy: ReasoningPolicy | None = None
    default_request_options: ProviderModelRequestOptions = Field(
        default_factory=ProviderModelRequestOptions
    )

    @model_validator(mode="after")
    def validate_reasoning_contract(self) -> Self:
        """确保 model identity 与 reasoning contract 可被当前 SQLite schema 安全持久化。"""

        if self.model_id != self.upstream_model_id:
            # 当前 inventory 用 model_id 作为稳定主键. 如果应用升级时才为已同步的
            # upstream id 引入 alias, 必须先有显式 migration 改写旧行, 不能靠只读
            # catalog overlay 假装已经迁移。
            raise ValueError("provider model aliases require an explicit data migration")

        duplicate_policies = _duplicates(self.supported_reasoning_policies)
        if duplicate_policies:
            raise ValueError(
                f"duplicate supported reasoning policies: {', '.join(sorted(duplicate_policies))}"
            )
        if not self.supported_reasoning_policies:
            if self.default_reasoning_policy is not None:
                raise ValueError("reasoning default requires supported policies")
            return self
        if self.default_reasoning_policy is None:
            raise ValueError("supported reasoning policies require a default")
        if self.default_reasoning_policy not in self.supported_reasoning_policies:
            raise ValueError("reasoning default must be included in supported policies")
        return self


class ModelCatalog(BaseModel):
    """随 PageFerry 发版的版本化 provider 与 model catalog."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int
    catalog_version: str
    released_at: str | None
    providers: list[ProviderDefinition]
    models: list[ModelDefinition]
    provider_models: list[ProviderModelDefinition]

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        """拒绝重复身份与断裂的 provider-model 引用."""

        provider_ids = [provider.id for provider in self.providers]
        model_ids = [model.id for model in self.models]

        duplicate_provider_ids = _duplicates(provider_ids)
        if duplicate_provider_ids:
            raise ValueError(f"duplicate provider ids: {', '.join(sorted(duplicate_provider_ids))}")

        duplicate_model_ids = _duplicates(model_ids)
        if duplicate_model_ids:
            raise ValueError(f"duplicate model ids: {', '.join(sorted(duplicate_model_ids))}")

        provider_id_set = set(provider_ids)
        model_id_set = set(model_ids)
        provider_model_pairs = [
            (provider_model.provider_id, provider_model.model_id)
            for provider_model in self.provider_models
        ]
        duplicate_provider_models = _duplicates(provider_model_pairs)
        if duplicate_provider_models:
            formatted_pairs = ", ".join(
                f"{provider_id}/{model_id}"
                for provider_id, model_id in sorted(duplicate_provider_models)
            )
            raise ValueError(f"duplicate provider models: {formatted_pairs}")

        for provider_model in self.provider_models:
            if provider_model.provider_id not in provider_id_set:
                raise ValueError(
                    f"provider model references unknown provider: {provider_model.provider_id}"
                )
            if provider_model.model_id not in model_id_set:
                raise ValueError(
                    f"provider model references unknown model: {provider_model.model_id}"
                )

        return self


def _duplicates[T](values: list[T]) -> set[T]:
    """返回出现超过一次的值."""

    seen: set[T] = set()
    duplicates: set[T] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return duplicates


def load_bundled_catalog() -> ModelCatalog:
    """加载并校验 backend 内置的 catalog resource."""

    catalog_file = files("resources").joinpath("model_catalog/catalog.json")
    with catalog_file.open(encoding="utf-8") as handle:
        return ModelCatalog.model_validate(json.load(handle))
