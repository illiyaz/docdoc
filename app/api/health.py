from fastapi import APIRouter

from app.core.settings import get_settings

router = APIRouter(prefix="/health", tags=["health"])


@router.get("", summary="Basic health check")
def health_check() -> dict[str, str]:
    settings = get_settings()
    return {
        "status": "ok",
        "service": settings.app_name,
        "version": settings.app_version,
        "environment": settings.app_env,
    }
