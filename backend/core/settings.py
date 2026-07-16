from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
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
    allowed_origins: tuple[str, ...] = (
        "http://127.0.0.1:1420",
        "http://localhost:1420",
        "tauri://localhost",
        "http://tauri.localhost",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
