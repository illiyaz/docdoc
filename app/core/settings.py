from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = Field(default="DocDoc API", alias="APP_NAME")
    app_env: str = Field(default="local", alias="APP_ENV")
    app_version: str = Field(default="0.1.0", alias="APP_VERSION")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    database_url: str = Field(
        default="postgresql+psycopg://docdoc:docdoc@localhost:5432/docdoc",
        alias="DATABASE_URL",
    )
    tenant_salt: str = Field(default="change-me", alias="TENANT_SALT")
    fernet_key: str | None = Field(default=None, alias="FERNET_KEY")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
