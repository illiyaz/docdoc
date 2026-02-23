"""Review queue manager — Phase 4.

Manages four queues:
- low_confidence: AI extractions with score < 0.75
- escalation: records flagged by Reviewer for regulatory judgment
- qc_sampling: 5-10% random sample of AI-approved records
- rra_review: entity merges with confidence < 0.80
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.audit_log import record_event
from app.audit.events import EVENT_APPROVAL, EVENT_ESCALATION, EVENT_HUMAN_REVIEW
from app.db.models import ReviewTask
from app.review.roles import (
    VALID_QUEUE_TYPES,
    can_action_queue,
    required_role_for_queue,
)

_VALID_DECISIONS = frozenset({"approved", "rejected", "escalated"})

_DECISION_EVENT_MAP: dict[str, str] = {
    "escalated": EVENT_ESCALATION,
    "approved": EVENT_APPROVAL,
    "rejected": EVENT_HUMAN_REVIEW,
}


class QueueManager:
    """Create, assign, and complete review tasks across queues."""

    def __init__(self, db_session: Session) -> None:
        self.db = db_session

    # -- create -------------------------------------------------------------

    def create_task(
        self,
        queue_type: str,
        subject_id: str,
    ) -> ReviewTask:
        """Create a ``ReviewTask`` for *subject_id* in *queue_type*."""
        if queue_type not in VALID_QUEUE_TYPES:
            raise ValueError(
                f"Unknown queue_type {queue_type!r}; "
                f"must be one of {sorted(VALID_QUEUE_TYPES)}"
            )

        # Duplicate check: no PENDING task for same subject + queue
        sid = UUID(subject_id) if isinstance(subject_id, str) else subject_id
        existing = self.db.execute(
            select(ReviewTask).where(
                ReviewTask.queue_type == queue_type,
                ReviewTask.subject_id == sid,
                ReviewTask.status == "PENDING",
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise ValueError(
                f"Subject {subject_id} already has a PENDING task "
                f"in queue {queue_type!r}"
            )

        task = ReviewTask(
            queue_type=queue_type,
            subject_id=sid,
            status="PENDING",
            required_role=required_role_for_queue(queue_type),
        )
        self.db.add(task)
        self.db.flush()
        return task

    # -- assign -------------------------------------------------------------

    def assign_task(
        self,
        task_id: str,
        reviewer_id: str,
        role: str,
    ) -> ReviewTask:
        """Assign *task_id* to *reviewer_id* with *role*."""
        tid = UUID(task_id) if isinstance(task_id, str) else task_id
        task = self.db.get(ReviewTask, tid)
        if task is None:
            raise KeyError(f"ReviewTask {task_id} not found")
        if task.status != "PENDING":
            raise ValueError(
                f"Cannot assign task in status {task.status!r}; must be PENDING"
            )
        if not can_action_queue(role, task.queue_type):
            raise PermissionError(
                f"Role {role!r} cannot action queue {task.queue_type!r}"
            )

        task.assigned_to = reviewer_id
        task.status = "IN_PROGRESS"
        self.db.flush()
        return task

    # -- complete -----------------------------------------------------------

    def complete_task(
        self,
        task_id: str,
        reviewer_id: str,
        role: str,
        decision: str,
        rationale: str,
        db_session_audit: Session,
        regulatory_basis: str | None = None,
    ) -> ReviewTask:
        """Mark *task_id* complete and write an audit event."""
        if decision not in _VALID_DECISIONS:
            raise ValueError(
                f"Invalid decision {decision!r}; "
                f"must be one of {sorted(_VALID_DECISIONS)}"
            )
        if not rationale or not rationale.strip():
            raise ValueError("rationale must be non-empty")

        if role == "LEGAL_REVIEWER" and not regulatory_basis:
            raise ValueError(
                "regulatory_basis is required for LEGAL_REVIEWER decisions"
            )

        tid = UUID(task_id) if isinstance(task_id, str) else task_id
        task = self.db.get(ReviewTask, tid)
        if task is None:
            raise KeyError(f"ReviewTask {task_id} not found")
        if task.status != "IN_PROGRESS":
            raise ValueError(
                f"Cannot complete task in status {task.status!r}; "
                f"must be IN_PROGRESS"
            )

        task.status = "COMPLETED"
        task.completed_at = datetime.now(timezone.utc)
        self.db.flush()

        event_type = _DECISION_EVENT_MAP[decision]
        record_event(
            db_session_audit,
            event_type=event_type,
            actor=reviewer_id,
            subject_id=str(task.subject_id) if task.subject_id else None,
            decision=decision,
            rationale=rationale,
            regulatory_basis=regulatory_basis,
        )

        return task

    # -- query --------------------------------------------------------------

    def get_queue(
        self,
        queue_type: str,
        status: str = "PENDING",
    ) -> list[ReviewTask]:
        """Return tasks for *queue_type* filtered by *status*, oldest first."""
        stmt = (
            select(ReviewTask)
            .where(
                ReviewTask.queue_type == queue_type,
                ReviewTask.status == status,
            )
            .order_by(ReviewTask.created_at.asc())
        )
        return list(self.db.execute(stmt).scalars().all())
