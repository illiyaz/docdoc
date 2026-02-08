from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.core.logging import setup_logging
from app.core.settings import get_settings


@asynccontextmanager
async def lifespan(_: FastAPI):
    setup_logging()
    yield


settings = get_settings()
app = FastAPI(title=settings.app_name, version=settings.app_version, lifespan=lifespan)
app.include_router(health_router)
