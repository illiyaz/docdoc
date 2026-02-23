"""Append-only audit logger — Phase 4.

Provides ``record_event()`` to persist ``AuditEvent`` rows.
All writes are immutable — ``immutable=True`` always.

Safety: subject_id, pii_record_id, rationale, and regulatory_basis
are never logged — only event_type and actor.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.events import (
    EVENT_APPROVAL,
    EVENT_HUMAN_REVIEW,
    EVENT_LEGAL_REVIEW,
    VALID_EVENT_TYPES,
)
from app.db.models import AuditEvent

logger = logging.getLogger(__name__)


def record_event(
    db_session: Session,
    event_type: str,
    actor: str,
    subject_id: str | None = None,
    pii_record_id: str | None = None,
    decision: str | None = None,
    rationale: str | None = None,
    regulatory_basis: str | None = None,
) -> AuditEvent:
    """Create and persist an immutable ``AuditEvent``.

    Raises ``ValueError`` for invalid inputs.  Flushes but does **not**
    commit — the caller controls the transaction boundary.
    """
    if event_type not in VALID_EVENT_TYPES:
        raise ValueError(
            f"Invalid event_type {event_type!r}; "
            f"must be one of {sorted(VALID_EVENT_TYPES)}"
        )

    if not actor or not actor.strip():
        raise ValueError("actor must be a non-empty string")

    if event_type == EVENT_LEGAL_REVIEW and not regulatory_basis:
        raise ValueError("regulatory_basis is required for legal_review events")

    if event_type in {EVENT_HUMAN_REVIEW, EVENT_APPROVAL} and not rationale:
        raise ValueError(f"rationale is required for {event_type} events")

    event = AuditEvent(
        event_type=event_type,
        actor=actor,
        subject_id=subject_id,
        pii_record_id=pii_record_id,
        decision=decision,
        rationale=rationale,
        regulatory_basis=regulatory_basis,
        immutable=True,
    )
    db_session.add(event)
    db_session.flush()

    logger.info("Audit event recorded: type=%s actor=%s", event_type, actor)
    return event


def get_subject_history(
    db_session: Session,
    subject_id: str,
) -> list[AuditEvent]:
    """Return all ``AuditEvent`` rows for *subject_id*, ordered by timestamp."""
    stmt = (
        select(AuditEvent)
        .where(AuditEvent.subject_id == subject_id)
        .order_by(AuditEvent.timestamp.asc())
    )
    return list(db_session.execute(stmt).scalars().all())


def get_events_by_type(
    db_session: Session,
    event_type: str,
) -> list[AuditEvent]:
    """Return all ``AuditEvent`` rows of *event_type*, ordered by timestamp."""
    if event_type not in VALID_EVENT_TYPES:
        raise ValueError(
            f"Invalid event_type {event_type!r}; "
            f"must be one of {sorted(VALID_EVENT_TYPES)}"
        )
    stmt = (
        select(AuditEvent)
        .where(AuditEvent.event_type == event_type)
        .order_by(AuditEvent.timestamp.asc())
    )
    return list(db_session.execute(stmt).scalars().all())
