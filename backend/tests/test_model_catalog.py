"""验证内置 model catalog."""

import pytest
from pydantic import ValidationError

from modules.model_catalog import ModelCatalog, load_bundled_catalog


def test_bundled_catalog_enables_initial_translation_providers() -> None:
    """首批已实现 provider 应带显式 endpoint 或 catalog fallback."""

    catalog = load_bundled_catalog()

    available_providers = [provider for provider in catalog.providers if provider.available]

    assert [provider.id for provider in available_providers] == [
        "deepseek",
        "kimi",
        "glm",
        "minimax",
        "mimo",
    ]
    by_id = {provider.id: provider for provider in available_providers}
    deepseek = by_id["deepseek"]
    assert deepseek.default_base_url == "https://api.deepseek.com"
    assert deepseek.chat_path == "/chat/completions"
    assert deepseek.models_path == "/models"
    assert deepseek.docs_url == "https://api-docs.deepseek.com/"
    assert deepseek.api_key_url == "https://platform.deepseek.com/api_keys"
    assert by_id["kimi"].default_base_url == "https://api.moonshot.cn/v1"
    assert by_id["kimi"].models_path == "/models"
    assert by_id["minimax"].default_base_url == "https://api.minimaxi.com/v1"
    assert by_id["minimax"].models_path == "/models"
    assert by_id["glm"].models_path is None
    assert by_id["mimo"].models_path is None


def test_bundled_catalog_disables_thinking_for_deepseek_v4_flash() -> None:
    """内置 DeepSeek model 默认使用关闭 thinking 的翻译请求."""

    catalog = load_bundled_catalog()

    model = next(model for model in catalog.models if model.id == "deepseek-v4-flash")
    provider_model = next(
        provider_model
        for provider_model in catalog.provider_models
        if provider_model.provider_id == "deepseek"
        and provider_model.model_id == "deepseek-v4-flash"
    )

    assert "translation" in model.capabilities
    assert provider_model.upstream_model_id == "deepseek-v4-flash"
    assert provider_model.enabled_by_default is True
    assert provider_model.default_request_options.thinking is not None
    assert provider_model.default_request_options.thinking.type == "disabled"


def test_catalog_rejects_duplicate_provider_ids() -> None:
    """重复 provider 身份无法通过 catalog 校验."""

    raw_catalog = load_bundled_catalog().model_dump(mode="json")
    raw_catalog["providers"].append(raw_catalog["providers"][0])

    with pytest.raises(ValidationError, match="duplicate provider ids"):
        ModelCatalog.model_validate(raw_catalog)


def test_catalog_rejects_broken_provider_model_reference() -> None:
    """Provider-model 映射不能引用不存在的 provider."""

    raw_catalog = load_bundled_catalog().model_dump(mode="json")
    raw_catalog["provider_models"][0]["provider_id"] = "missing-provider"

    with pytest.raises(ValidationError, match="references unknown provider"):
        ModelCatalog.model_validate(raw_catalog)


def test_catalog_rejects_endpoint_path_without_leading_slash() -> None:
    """Provider endpoint path 必须保持为显式相对路径."""

    raw_catalog = load_bundled_catalog().model_dump(mode="json")
    deepseek = next(
        provider for provider in raw_catalog["providers"] if provider["id"] == "deepseek"
    )
    deepseek["chat_path"] = "chat/completions"

    with pytest.raises(ValidationError, match="must start with"):
        ModelCatalog.model_validate(raw_catalog)
