from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """re:action backend runtime settings.

    환경변수 키는 .env.example 참고.
    민감 값(LLM/DB/OAuth/암호화 키)은 후속 이슈에서 채운다.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: Literal["local", "dev", "staging", "prod"] = "local"
    app_name: str = "reaction-backend"
    app_version: str = "0.1.0"

    host: str = "0.0.0.0"
    port: int = 8000

    cors_allow_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"]
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
