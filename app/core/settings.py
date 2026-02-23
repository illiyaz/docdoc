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
        default="postgresql+psycopg://notifai:notifai@localhost:3849/notifai",
        alias="DATABASE_URL",
    )
    tenant_salt: str = Field(default="change-me", alias="TENANT_SALT")
    fernet_key: str | None = Field(default=None, alias="FERNET_KEY")
    redis_url: str = Field(default="redis://localhost:3850/0", alias="REDIS_URL")
    minio_url: str = Field(default="http://localhost:3851", alias="MINIO_URL")
    smtp_host: str = Field(default="localhost", alias="SMTP_HOST")
    smtp_port: int = Field(default=3853, alias="SMTP_PORT")
    storage_mode: str = Field(default="strict", alias="STORAGE_MODE")
    secret_key: str = Field(default="change-me-in-production", alias="SECRET_KEY")
    vault_addr: str | None = Field(default=None, alias="VAULT_ADDR")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
