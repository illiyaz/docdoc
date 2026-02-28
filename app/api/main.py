"""FastAPI application factory.

Assembles CORS, PII filter middleware, and all API routers.
This module is the authoritative app object — app/main.py re-exports it.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.middleware.pii_filter import PIIFilterMiddleware
from app.api.routes.audit import router as audit_router
from app.api.routes.diagnostic import router as diagnostic_router
from app.api.routes.health import router as health_router
from app.api.routes.jobs import router as jobs_router
from app.api.routes.review import router as review_router
from app.core.logging import setup_logging
from app.core.settings import get_settings

logger = logging.getLogger(__name__)

UPLOAD_TTL_SECONDS = 60 * 60  # 1 hour
UPLOAD_SWEEP_INTERVAL = 60 * 5  # 5 minutes


async def _sweep_expired_uploads() -> None:
    """Periodically delete upload directories older than TTL."""
    settings = get_settings()
    upload_root = Path(settings.upload_dir)
    while True:
        await asyncio.sleep(UPLOAD_SWEEP_INTERVAL)
        if not upload_root.is_dir():
            continue
        now = time.time()
        for child in upload_root.iterdir():
            if not child.is_dir():
                continue
            age = now - child.stat().st_mtime
            if age > UPLOAD_TTL_SECONDS:
                logger.info("Sweeping expired upload directory: %s", child.name)
                shutil.rmtree(child, ignore_errors=True)


@asynccontextmanager
async def lifespan(_: FastAPI):
    setup_logging()
    task = asyncio.create_task(_sweep_expired_uploads())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
)

# CORS — restrict origins in production via ALLOWED_ORIGINS env var
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# PII safety net — must be registered AFTER CORS so it runs on the inner response
app.add_middleware(PIIFilterMiddleware)

app.include_router(health_router)
app.include_router(jobs_router)
app.include_router(review_router)
app.include_router(audit_router)
app.include_router(diagnostic_router)
