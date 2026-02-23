"""Audit trail routes — GET /audit/{subject_id}/history, GET /audit/recent."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.audit.audit_log import get_subject_history
from app.db.models import AuditEvent

router = APIRouter(prefix="/audit", tags=["audit"])


def _serialize_event(ev: AuditEvent) -> dict:
    return {
        "event_type": ev.event_type,
        "actor": ev.actor,
        "decision": ev.decision,
        "timestamp": ev.timestamp.isoformat() if ev.timestamp else None,
        "regulatory_basis": ev.regulatory_basis,
    }


@router.get("/recent", summary="Get most recent audit events")
def get_recent(limit: int = 10, db: Session = Depends(get_db)):
    stmt = (
        select(AuditEvent)
        .order_by(AuditEvent.timestamp.desc())
        .limit(limit)
    )
    events = list(db.execute(stmt).scalars().all())
    return [_serialize_event(ev) for ev in events]


@router.get("/{subject_id}/history", summary="Get audit history for a subject")
def get_history(subject_id: str, db: Session = Depends(get_db)):
    events = get_subject_history(db, subject_id)
    if not events:
        raise HTTPException(status_code=404, detail=f"No audit history for subject {subject_id}")

    return [_serialize_event(ev) for ev in events]
