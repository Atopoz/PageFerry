"""定义由环境变量驱动的应用设置."""

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """从 PAGEFERRY_ 前缀环境变量加载的 runtime 设置."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="PAGEFERRY_",
        extra="ignore",
    )

    app_name: str = "PageFerry"
    version: str = "0.1.0"
    debug: bool = False
    host: str = "127.0.0.1"
    port: int = 8765
    data_dir: Path | None = None
    layout_model_path: Path | None = None
    layout_max_concurrency: int = 1
    layout_intra_op_threads: int | None = None
    boot_token: SecretStr | None = None
    secret_service_name: str = "com.pageferry.provider-secrets"
    allowed_origins: tuple[str, ...] = (
        "http://127.0.0.1:1420",
        "http://localhost:1420",
        "tauri://localhost",
        "http://tauri.localhost",
    )


@lru_cache
def get_settings() -> Settings:
    """返回进程级 settings 实例."""

    return Settings()
