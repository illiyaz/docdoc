"""FastAPI application factory.

Assembles CORS, PII filter middleware, and all API routers.
This module is the authoritative app object — app/main.py re-exports it.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.middleware.pii_filter import PIIFilterMiddleware
from app.api.routes.audit import router as audit_router
from app.api.routes.health import router as health_router
from app.api.routes.jobs import router as jobs_router
from app.api.routes.review import router as review_router
from app.core.logging import setup_logging
from app.core.settings import get_settings


@asynccontextmanager
async def lifespan(_: FastAPI):
    setup_logging()
    yield


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
