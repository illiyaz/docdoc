"""Entry point for uvicorn: uvicorn app.main:app --reload

The authoritative app object lives in app.api.main; this module re-exports
it so uvicorn's conventional invocation continues to work.
"""
from app.api.main import app  # noqa: F401

__all__ = ["app"]
