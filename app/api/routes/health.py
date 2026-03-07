"""GET /health — liveness check.  GET /settings — read-only app config."""
from __future__ import annotations

from fastapi import APIRouter

from app.core.settings import get_settings

router = APIRouter(tags=["health"])


@router.get("/health", summary="Basic health check")
def health_check() -> dict[str, str]:
    settings = get_settings()
    return {
        "status": "ok",
        "service": settings.app_name,
        "version": settings.app_version,
        "environment": settings.app_env,
    }


@router.get("/settings", summary="Read-only application settings")
def get_app_settings() -> dict:
    settings = get_settings()
    return {
        "app_name": settings.app_name,
        "app_version": settings.app_version,
        "app_env": settings.app_env,
        "database_url_set": bool(settings.database_url),
        "llm_assist_enabled": settings.llm_assist_enabled,
        "ollama_url": settings.ollama_url,
        "ollama_model": settings.ollama_model,
        "pii_masking_enabled": settings.pii_masking_enabled,
    }
