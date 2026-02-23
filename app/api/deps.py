"""FastAPI dependency injection — database sessions and service factories."""
from __future__ import annotations

from collections.abc import Generator

from fastapi import Depends
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.settings import get_settings
from app.protocols.registry import ProtocolRegistry
from app.review.queue_manager import QueueManager
from app.review.workflow import WorkflowEngine

_engine = None
_SessionLocal = None


def _get_session_factory() -> sessionmaker:
    global _engine, _SessionLocal
    if _SessionLocal is None:
        _engine = create_engine(get_settings().database_url)
        _SessionLocal = sessionmaker(bind=_engine, autocommit=False, autoflush=False)
    return _SessionLocal


def get_db() -> Generator[Session, None, None]:
    """Yield a SQLAlchemy session; commit on success, rollback on error."""
    factory = _get_session_factory()
    db = factory()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_protocol_registry() -> ProtocolRegistry:
    """Return the default protocol registry loaded from config/protocols/."""
    return ProtocolRegistry.default()


def get_queue_manager(db: Session = Depends(get_db)) -> QueueManager:
    """Return a QueueManager bound to the current DB session."""
    return QueueManager(db)


def get_workflow_engine(db: Session = Depends(get_db)) -> WorkflowEngine:
    """Return a WorkflowEngine bound to the current DB session."""
    return WorkflowEngine(db)
