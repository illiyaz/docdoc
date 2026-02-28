from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = Field(default="Forentis AI", alias="APP_NAME")
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
    pii_masking_enabled: bool = Field(default=True, alias="PII_MASKING_ENABLED")
    upload_dir: str = Field(default="/tmp/docdoc_uploads", alias="UPLOAD_DIR")
    upload_max_file_size_mb: int = Field(default=100, alias="UPLOAD_MAX_FILE_SIZE_MB")
    upload_max_total_size_mb: int = Field(default=500, alias="UPLOAD_MAX_TOTAL_SIZE_MB")
    llm_assist_enabled: bool = Field(default=False, alias="LLM_ASSIST_ENABLED")
    ollama_url: str = Field(default="http://localhost:11434", alias="OLLAMA_URL")
    ollama_model: str = Field(default="qwen2.5:7b", alias="OLLAMA_MODEL")
    ollama_timeout_s: int = Field(default=60, alias="OLLAMA_TIMEOUT_S")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
