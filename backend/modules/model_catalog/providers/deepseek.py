"""定义 DeepSeek 的 OpenAI-compatible provider preset."""

from typing import Any

from modules.model_catalog.providers.openai_compatible import OpenAICompatibleProvider

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_CHAT_PATH = "/chat/completions"
DEFAULT_MODELS_PATH = "/models"
DEFAULT_MODEL_ID = "deepseek-v4-flash"


class DeepSeekProvider(OpenAICompatibleProvider):
    """使用 DeepSeek 默认 endpoint 和请求选项的 provider adapter."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        chat_path: str = DEFAULT_CHAT_PATH,
        models_path: str | None = DEFAULT_MODELS_PATH,
        model_id: str = DEFAULT_MODEL_ID,
        **kwargs: Any,
    ) -> None:
        """保留 DeepSeek 默认值, 并显式关闭 thinking 与启用 JSON mode."""

        kwargs.setdefault("thinking_disabled", True)
        kwargs.setdefault("json_response_format", True)
        super().__init__(
            api_key,
            base_url=base_url,
            chat_path=chat_path,
            models_path=models_path,
            model_id=model_id,
            **kwargs,
        )
