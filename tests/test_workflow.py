"""Tests for app/review/workflow.py — Phase 4."""
from __future__ import annotations

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
        pii_types_found=["US_SSN"],
        notification_required=True,
        review_status=status,
    )
    db_session.add(ns)
    db_session.flush()
    return ns


# ===========================================================================
# can_transition
# ===========================================================================

class TestCanTransition:
    def setup_method(self):
        self.wf = WorkflowEngine.__new__(WorkflowEngine)

    # -- valid transitions --
    def test_ai_pending_to_human_review(self):
        assert self.wf.can_transition("AI_PENDING", "HUMAN_REVIEW") is True

    def test_human_review_to_legal_review(self):
        assert self.wf.can_transition("HUMAN_REVIEW", "LEGAL_REVIEW") is True

    def test_human_review_to_approved(self):
        assert self.wf.can_transition("HUMAN_REVIEW", "APPROVED") is True

    def test_human_review_to_rejected(self):
        assert self.wf.can_transition("HUMAN_REVIEW", "REJECTED") is True

    def test_legal_review_to_approved(self):
        assert self.wf.can_transition("LEGAL_REVIEW", "APPROVED") is True

    def test_legal_review_to_rejected(self):
        assert self.wf.can_transition("LEGAL_REVIEW", "REJECTED") is True

    def test_approved_to_notified(self):
        assert self.wf.can_transition("APPROVED", "NOTIFIED") is True

    # -- invalid transitions --
    def test_ai_pending_to_approved_skip(self):
        assert self.wf.can_transition("AI_PENDING", "APPROVED") is False

    def test_approved_to_human_review_backwards(self):
        assert self.wf.can_transition("APPROVED", "HUMAN_REVIEW") is False

    def test_notified_terminal(self):
        for target in ("AI_PENDING", "HUMAN_REVIEW", "LEGAL_REVIEW", "APPROVED", "REJECTED", "NOTIFIED"):
            assert self.wf.can_transition("NOTIFIED", target) is False

    def test_rejected_terminal(self):
        for target in ("AI_PENDING", "HUMAN_REVIEW", "APPROVED"):
            assert self.wf.can_transition("REJECTED", target) is False


# ===========================================================================
# transition
# ===========================================================================

class TestTransition:
    def test_ai_pending_to_human_review(self, db_session):
        subj = _make_subject(db_session)
        wf = WorkflowEngine(db_session)

        result = wf.transition(
            str(subj.subject_id), "HUMAN_REVIEW",
            actor="system", rationale="Low confidence extraction",
        )

        assert result.review_status == "HUMAN_REVIEW"
        history = get_subject_history(db_session, str(subj.subject_id))
        assert len(history) == 1
        assert history[0].event_type == EVENT_HUMAN_REVIEW

    def test_human_review_to_legal_review(self, db_session):
        subj = _make_subject(db_session, status="HUMAN_REVIEW")
        wf = WorkflowEngine(db_session)

        result = wf.transition(
            str(subj.subject_id), "LEGAL_REVIEW",
            actor="reviewer-1", rationale="Needs legal opinion",
        )

        assert result.review_status == "LEGAL_REVIEW"
        history = get_subject_history(db_session, str(subj.subject_id))
        assert history[0].event_type == EVENT_ESCALATION

    def test_human_review_to_approved(self, db_session):
        subj = _make_subject(db_session, status="HUMAN_REVIEW")
        wf = WorkflowEngine(db_session)

        result = wf.transition(
            str(subj.subject_id), "APPROVED",
            actor="reviewer-1", rationale="Confirmed PII match",
        )

        assert result.review_status == "APPROVED"
        history = get_subject_history(db_session, str(subj.subject_id))
        assert history[0].event_type == EVENT_APPROVAL

    def test_legal_review_to_approved(self, db_session):
        subj = _make_subject(db_session, status="LEGAL_REVIEW")
        wf = WorkflowEngine(db_session)

        result = wf.transition(
            str(subj.subject_id), "APPROVED",
            actor="legal-1", rationale="Compliant with HIPAA",
            regulatory_basis="45 CFR §164.404",
        )

        assert result.review_status == "APPROVED"
        history = get_subject_history(db_session, str(subj.subject_id))
        assert history[0].event_type == EVENT_APPROVAL
        assert history[0].regulatory_basis == "45 CFR §164.404"

    def test_approved_to_notified(self, db_session):
        subj = _make_subject(db_session, status="APPROVED")
        wf = WorkflowEngine(db_session)

        result = wf.transition(
            str(subj.subject_id), "NOTIFIED",
            actor="system", rationale="Email delivered",
        )

        assert result.review_status == "NOTIFIED"
        history = get_subject_history(db_session, str(subj.subject_id))
        assert history[0].event_type == EVENT_NOTIFICATION_SENT

    def test_invalid_transition_raises_and_no_update(self, db_session):
        subj = _make_subject(db_session)
        wf = WorkflowEngine(db_session)

        with pytest.raises(ValueError, match="Invalid transition"):
            wf.transition(
                str(subj.subject_id), "APPROVED",
                actor="system", rationale="skip",
            )

        db_session.refresh(subj)
        assert subj.review_status == "AI_PENDING"

    def test_unknown_subject_raises_key(self, db_session):
        wf = WorkflowEngine(db_session)

        with pytest.raises(KeyError, match="not found"):
            wf.transition(
                str(uuid4()), "HUMAN_REVIEW",
                actor="system", rationale="test",
            )

    def test_event_actor_matches(self, db_session):
        subj = _make_subject(db_session)
        wf = WorkflowEngine(db_session)

        wf.transition(
            str(subj.subject_id), "HUMAN_REVIEW",
            actor="auto-triage", rationale="Below threshold",
        )

        history = get_subject_history(db_session, str(subj.subject_id))
        assert history[0].actor == "auto-triage"

    def test_correct_event_type_per_transition(self, db_session):
        subj = _make_subject(db_session)
        wf = WorkflowEngine(db_session)

        wf.transition(str(subj.subject_id), "HUMAN_REVIEW", actor="system", rationale="triage")
        wf.transition(str(subj.subject_id), "APPROVED", actor="rev-1", rationale="confirmed")
        wf.transition(str(subj.subject_id), "NOTIFIED", actor="system", rationale="sent")

        history = get_subject_history(db_session, str(subj.subject_id))
        types = [e.event_type for e in history]
        assert types == [EVENT_HUMAN_REVIEW, EVENT_APPROVAL, EVENT_NOTIFICATION_SENT]


# ===========================================================================
# get_subjects_by_status
# ===========================================================================

class TestGetSubjectsByStatus:
    def test_returns_matching_only(self, db_session):
        s1 = _make_subject(db_session, status="AI_PENDING")
        s2 = _make_subject(db_session, status="HUMAN_REVIEW")
        s3 = _make_subject(db_session, status="AI_PENDING")
        wf = WorkflowEngine(db_session)

        results = wf.get_subjects_by_status("AI_PENDING")
        result_ids = {str(r.subject_id) for r in results}

        assert len(results) == 2
        assert str(s1.subject_id) in result_ids
        assert str(s3.subject_id) in result_ids

    def test_ordered_by_created_at(self, db_session):
        s1 = _make_subject(db_session)
        s2 = _make_subject(db_session)
        s3 = _make_subject(db_session)
        wf = WorkflowEngine(db_session)

        results = wf.get_subjects_by_status("AI_PENDING")
        ids = [str(r.subject_id) for r in results]
        assert ids == [str(s1.subject_id), str(s2.subject_id), str(s3.subject_id)]

    def test_unknown_status_returns_empty(self, db_session):
        _make_subject(db_session)
        wf = WorkflowEngine(db_session)

        assert wf.get_subjects_by_status("NONEXISTENT") == []
