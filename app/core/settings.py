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
        default="postgresql+psycopg://forentis:forentis@localhost:5499/forentis",
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
    upload_dir: str = Field(default="./uploads", alias="UPLOAD_DIR")
    upload_max_file_size_mb: int = Field(default=100, alias="UPLOAD_MAX_FILE_SIZE_MB")
    upload_max_total_size_mb: int = Field(default=500, alias="UPLOAD_MAX_TOTAL_SIZE_MB")
    llm_assist_enabled: bool = Field(default=False, alias="LLM_ASSIST_ENABLED")
    ollama_url: str = Field(default="http://localhost:11434", alias="OLLAMA_URL")
    ollama_model: str = Field(default="llama3.2-vision:latest", alias="OLLAMA_MODEL")
    ollama_timeout_s: int = Field(default=60, alias="OLLAMA_TIMEOUT_S")
    ollama_vision_model: str = Field(default="qwen2.5vl:32b", alias="OLLAMA_VISION_MODEL")
    ollama_vision_fallback_model: str = Field(default="llama3.2-vision:latest", alias="OLLAMA_VISION_FALLBACK_MODEL")
    use_vision_extraction: bool = Field(default=True, alias="USE_VISION_EXTRACTION")
    vision_page_dpi: int = Field(default=150, alias="VISION_PAGE_DPI")
    vision_routing_workers: int = Field(default=4, alias="VISION_ROUTING_WORKERS")
    ollama_understanding_model: str | None = Field(default=None, alias="OLLAMA_UNDERSTANDING_MODEL")
    use_extraction_selector: bool = Field(default=False, alias="USE_EXTRACTION_SELECTOR")
    use_text_llm_batch: bool = Field(default=True, alias="USE_TEXT_LLM_BATCH")
    doc_hard_timeout_s: int = Field(default=1800, alias="DOC_HARD_TIMEOUT_S")
    completeness_vision_step: int = Field(default=2, alias="COMPLETENESS_VISION_STEP")
    completeness_vision_window: int = Field(default=40, alias="COMPLETENESS_VISION_WINDOW")
    completeness_vision_max_pages: int = Field(default=15, alias="COMPLETENESS_VISION_MAX_PAGES")
    # Task #25 / #26 guardrails (2026-04-19): escape hatch + confidence gate
    # so operators can disable the trivial-doc fast-paths if they suspect
    # quality regressions.
    disable_trivial_skips: bool = Field(default=False, alias="DISABLE_TRIVIAL_SKIPS")
    trivial_skip_min_confidence: float = Field(
        default=0.9, alias="TRIVIAL_SKIP_MIN_CONFIDENCE"
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()