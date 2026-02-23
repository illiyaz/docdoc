"""HITL workflow state machine — Phase 4.

Manages per-subject ``review_status`` transitions:

    AI_PENDING → HUMAN_REVIEW → LEGAL_REVIEW (if escalated) → APPROVED → NOTIFIED
                             ↘ APPROVED
                             ↘ REJECTED
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.audit_log import record_event
from app.audit.events import (
    EVENT_AI_EXTRACTION,
    EVENT_APPROVAL,
    EVENT_ESCALATION,
    EVENT_HUMAN_REVIEW,
    EVENT_NOTIFICATION_SENT,
)
from app.db.models import NotificationSubject

# Allowed transitions: current_status → {valid target statuses}
_TRANSITIONS: dict[str, set[str]] = {
    "AI_PENDING": {"HUMAN_REVIEW"},
    "HUMAN_REVIEW": {"LEGAL_REVIEW", "APPROVED", "REJECTED"},
    "LEGAL_REVIEW": {"APPROVED", "REJECTED"},
    "APPROVED": {"NOTIFIED"},
}

# Map target status → audit event type
_STATUS_EVENT_MAP: dict[str, str] = {
    "AI_PENDING": EVENT_AI_EXTRACTION,
    "HUMAN_REVIEW": EVENT_HUMAN_REVIEW,
    "LEGAL_REVIEW": EVENT_ESCALATION,
    "APPROVED": EVENT_APPROVAL,
    "REJECTED": EVENT_HUMAN_REVIEW,
    "NOTIFIED": EVENT_NOTIFICATION_SENT,
}


class WorkflowEngine:
    """Transition subjects through review states with audit logging."""

    def __init__(self, db_session: Session) -> None:
        self.db = db_session

    def can_transition(self, current_status: str, to_status: str) -> bool:
        """Return whether *current_status* → *to_status* is allowed."""
        return to_status in _TRANSITIONS.get(current_status, set())

    def transition(
        self,
        subject_id: str,
        to_status: str,
        actor: str,
        rationale: str,
        regulatory_basis: str | None = None,
    ) -> NotificationSubject:
        """Move subject to *to_status* if transition is valid."""
        sid = UUID(subject_id) if isinstance(subject_id, str) else subject_id
        subject = self.db.get(NotificationSubject, sid)
        if subject is None:
            raise KeyError(f"NotificationSubject {subject_id} not found")

        current = subject.review_status
        if not self.can_transition(current, to_status):
            raise ValueError(
                f"Invalid transition {current!r} → {to_status!r}"
            )

        subject.review_status = to_status
        self.db.flush()

        event_type = _STATUS_EVENT_MAP[to_status]
        record_event(
            self.db,
            event_type=event_type,
            actor=actor,
            subject_id=str(sid),
            rationale=rationale,
            regulatory_basis=regulatory_basis,
        )

        return subject

    def get_subjects_by_status(
        self,
        status: str,
    ) -> list[NotificationSubject]:
        """Return subjects with *status*, ordered by created_at ascending."""
        stmt = (
            select(NotificationSubject)
            .where(NotificationSubject.review_status == status)
            .order_by(NotificationSubject.created_at.asc())
        )
        return list(self.db.execute(stmt).scalars().all())
