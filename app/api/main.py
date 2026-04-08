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
from app.api.routes.analysis_review import router as analysis_review_router
from app.api.routes.audit import router as audit_router
from app.api.routes.dashboard import router as dashboard_router
from app.api.routes.diagnostic import router as diagnostic_router
from app.api.routes.exports import router as exports_router
from app.api.routes.health import router as health_router
from app.api.routes.jobs import router as jobs_router
from app.api.routes.projects import router as projects_router
from app.api.routes.protocols import base_router as base_protocols_router
from app.api.routes.protocols import router as protocols_router
from app.api.routes.documents import router as documents_router
from app.api.routes.intelligence import router as intelligence_router
from app.api.routes.notifications import router as notifications_router
from app.api.routes.review import router as review_router
from app.core.logging import setup_logging
from app.core.settings import get_settings

logger = logging.getLogger(__name__)

UPLOAD_TTL_SECONDS = 60 * 60 * 24  # 24 hours (analysis + review + extraction can take hours)
UPLOAD_SWEEP_INTERVAL = 60 * 30  # 30 minutes


async def _sweep_expired_uploads() -> None:
    """Periodically delete upload directories older than TTL.

    Skips directories referenced by active (non-terminal) ingestion runs
    to prevent deleting files mid-pipeline.
    """
    settings = get_settings()
    upload_root = Path(settings.upload_dir)
    while True:
        await asyncio.sleep(UPLOAD_SWEEP_INTERVAL)
        if not upload_root.is_dir():
            continue

        # Build set of upload dirs used by active jobs
        active_upload_dirs: set[str] = set()
        try:
            from app.api.deps import _get_session_factory
            factory = _get_session_factory()
            db = factory()
            try:
                from app.db.models import IngestionRun
                active_runs = db.query(IngestionRun).filter(
                    IngestionRun.status.in_(["pending", "running", "analyzing", "analyzed", "extracting"])
                ).all()
                for run in active_runs:
                    if run.source_path and "uploads/" in run.source_path:
                        # Extract the upload UUID dir name
                        parts = run.source_path.split("uploads/")
                        if len(parts) > 1:
                            upload_id = parts[1].split("/")[0]
                            active_upload_dirs.add(upload_id)
            finally:
                db.close()
        except Exception:
            pass  # If DB unavailable, fall back to TTL-only

        now = time.time()
        for child in upload_root.iterdir():
            if not child.is_dir():
                continue
            if child.name in active_upload_dirs:
                continue  # Skip — active job is using this directory
            age = now - child.stat().st_mtime
            if age > UPLOAD_TTL_SECONDS:
                logger.info("Sweeping expired upload directory: %s", child.name)
                shutil.rmtree(child, ignore_errors=True)


@asynccontextmanager
async def lifespan(_: FastAPI):
    setup_logging()
    # Ensure upload directory exists (persistent, survives restarts)
    upload_root = Path(get_settings().upload_dir)
    upload_root.mkdir(parents=True, exist_ok=True)
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

app.include_router(health_router, prefix="/api")
app.include_router(projects_router, prefix="/api")
app.include_router(protocols_router, prefix="/api")
app.include_router(base_protocols_router, prefix="/api")
app.include_router(exports_router, prefix="/api")
app.include_router(analysis_review_router, prefix="/api")
app.include_router(jobs_router, prefix="/api")
app.include_router(review_router, prefix="/api")
app.include_router(documents_router, prefix="/api")
app.include_router(notifications_router, prefix="/api")
app.include_router(audit_router, prefix="/api")
app.include_router(dashboard_router, prefix="/api")
app.include_router(diagnostic_router, prefix="/api")
app.include_router(intelligence_router, prefix="/api")
