"""End-to-end gate test for Phase 4 — HITL workflow + audit trail.

No mocks. All business logic runs real against SQLite in-memory.
"""
from __future__ import annotations

import random
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.audit.audit_log import get_subject_history
from app.audit.events import (
    EVENT_APPROVAL,
    EVENT_ESCALATION,
    EVENT_HUMAN_REVIEW,
    EVENT_NOTIFICATION_SENT,
)
from app.db.base import Base
from app.db.models import NotificationSubject
from app.review.queue_manager import QueueManager
from app.review.sampling import SamplingStrategy
from app.review.workflow import WorkflowEngine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with Session() as session:
        yield session


def _make_subject(db_session, status="AI_PENDING") -> NotificationSubject:
    ns = NotificationSubject(
        subject_id=uuid4(),
        canonical_name="Test Subject",
        pii_types_found=["US_SSN"],
        notification_required=True,
        review_status=status,
    )
    db_session.add(ns)
    db_session.flush()
    return ns


# ===========================================================================
# Happy path: QC sampling → review → approval → notification
# ===========================================================================

class TestHappyPath:
    def test_full_workflow(self, db_session):
        random.seed(42)
        subject = _make_subject(db_session)
        sid = str(subject.subject_id)

        qm = QueueManager(db_session)
        wf = WorkflowEngine(db_session)

        # -- Step 1: subject starts AI_PENDING --------------------------------
        assert subject.review_status == "AI_PENDING"

        # -- Step 2–3: SamplingStrategy selects it for QC ---------------------
        ss = SamplingStrategy(db_session, sample_rate=0.10, min_sample=1)
        tasks = ss.generate_qc_sample(qm)

        assert len(tasks) == 1
        qc_task = tasks[0]
        assert qc_task.queue_type == "qc_sampling"
        assert str(qc_task.subject_id) == sid

        # -- Step 4: assign to QC_SAMPLER ------------------------------------
        qm.assign_task(str(qc_task.review_task_id), "reviewer_1", "QC_SAMPLER")

        assert qc_task.status == "IN_PROGRESS"
        assert qc_task.assigned_to == "reviewer_1"

        # -- Step 5: complete QC task ----------------------------------------
        qm.complete_task(
            str(qc_task.review_task_id),
            "reviewer_1",
            "QC_SAMPLER",
            decision="approved",
            rationale="QC check passed",
            db_session_audit=db_session,
        )

        assert qc_task.status == "COMPLETED"
        assert qc_task.completed_at is not None

        # -- Step 6: transition AI_PENDING → HUMAN_REVIEW --------------------
        wf.transition(sid, "HUMAN_REVIEW", actor="system", rationale="Entering review queue")

        db_session.refresh(subject)
        assert subject.review_status == "HUMAN_REVIEW"

        # -- Step 7: transition HUMAN_REVIEW → APPROVED ----------------------
        wf.transition(sid, "APPROVED", actor="reviewer_1", rationale="All PII confirmed correct")

        db_session.refresh(subject)
        assert subject.review_status == "APPROVED"

        # -- Step 8: transition APPROVED → NOTIFIED --------------------------
        wf.transition(sid, "NOTIFIED", actor="system", rationale="Notification delivered")

        db_session.refresh(subject)
        assert subject.review_status == "NOTIFIED"

        # -- Final assertions -------------------------------------------------
        history = get_subject_history(db_session, sid)

        # QC complete_task logs EVENT_APPROVAL, then 3 workflow transitions
        assert len(history) >= 3

        # Every event is immutable and has a non-empty actor
        for ev in history:
            assert ev.immutable is True
            assert ev.actor and ev.actor.strip()

        # Events in order: approval (QC), human_review, approval, notification_sent
        types = [ev.event_type for ev in history]
        assert EVENT_APPROVAL in types
        assert EVENT_HUMAN_REVIEW in types
        assert EVENT_NOTIFICATION_SENT in types

        # Last event is notification_sent
        assert types[-1] == EVENT_NOTIFICATION_SENT


# ===========================================================================
# Escalation path: reviewer → legal review → approval
# ===========================================================================

class TestEscalationPath:
    def test_legal_review_with_regulatory_basis(self, db_session):
        subject_b = _make_subject(db_session)
        sid = str(subject_b.subject_id)
        wf = WorkflowEngine(db_session)

        # -- Step 1: AI_PENDING → HUMAN_REVIEW --------------------------------
        wf.transition(sid, "HUMAN_REVIEW", actor="system", rationale="Auto-triage")

        db_session.refresh(subject_b)
        assert subject_b.review_status == "HUMAN_REVIEW"

        # -- Step 2: HUMAN_REVIEW → LEGAL_REVIEW (escalation) ----------------
        wf.transition(
            sid, "LEGAL_REVIEW",
            actor="reviewer_2",
            rationale="Uncertain if GDPR applies — needs legal opinion",
        )

        db_session.refresh(subject_b)
        assert subject_b.review_status == "LEGAL_REVIEW"

        # -- Step 3: LEGAL_REVIEW → APPROVED with regulatory_basis -----------
        wf.transition(
            sid, "APPROVED",
            actor="legal_1",
            rationale="GDPR Article 33 applies",
            regulatory_basis="GDPR Art. 33 — personal data breach",
        )

        db_session.refresh(subject_b)
        assert subject_b.review_status == "APPROVED"

        # -- Assertions -------------------------------------------------------
        history = get_subject_history(db_session, sid)
        types = [ev.event_type for ev in history]

        assert EVENT_ESCALATION in types
        assert EVENT_APPROVAL in types

        # Find the approval event and check regulatory_basis
        approval_events = [ev for ev in history if ev.event_type == EVENT_APPROVAL]
        assert len(approval_events) == 1
        assert approval_events[0].regulatory_basis == "GDPR Art. 33 — personal data breach"
        assert approval_events[0].actor == "legal_1"

        # Escalation event logged by reviewer_2
        escalation_events = [ev for ev in history if ev.event_type == EVENT_ESCALATION]
        assert len(escalation_events) == 1
        assert escalation_events[0].actor == "reviewer_2"
