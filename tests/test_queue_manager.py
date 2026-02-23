"""Tests for app/review/roles.py and app/review/queue_manager.py — Phase 4."""
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import NotificationSubject, ReviewTask
from app.review.queue_manager import QueueManager
from app.review.roles import (
    can_action_queue,
    required_role_for_queue,
)


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


def _make_subject(db_session, pii_types=None) -> NotificationSubject:
    ns = NotificationSubject(
        subject_id=uuid4(),
        pii_types_found=pii_types or ["US_SSN"],
        notification_required=True,
        review_status="AI_PENDING",
    )
    db_session.add(ns)
    db_session.flush()
    return ns


# ===========================================================================
# roles.py
# ===========================================================================

class TestRequiredRoleForQueue:
    def test_low_confidence(self):
        assert required_role_for_queue("low_confidence") == "REVIEWER"

    def test_escalation(self):
        assert required_role_for_queue("escalation") == "LEGAL_REVIEWER"

    def test_qc_sampling(self):
        assert required_role_for_queue("qc_sampling") == "QC_SAMPLER"

    def test_rra_review(self):
        assert required_role_for_queue("rra_review") == "REVIEWER"

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown queue_type"):
            required_role_for_queue("unknown")


class TestCanActionQueue:
    def test_reviewer_low_confidence_true(self):
        assert can_action_queue("REVIEWER", "low_confidence") is True

    def test_reviewer_escalation_false(self):
        assert can_action_queue("REVIEWER", "escalation") is False

    def test_approver_escalation_override(self):
        assert can_action_queue("APPROVER", "escalation") is True

    def test_approver_any_queue(self):
        for qt in ("low_confidence", "escalation", "qc_sampling", "rra_review"):
            assert can_action_queue("APPROVER", qt) is True

    def test_qc_sampler_own_queue(self):
        assert can_action_queue("QC_SAMPLER", "qc_sampling") is True

    def test_qc_sampler_other_queue_false(self):
        assert can_action_queue("QC_SAMPLER", "low_confidence") is False

    def test_unknown_role_raises(self):
        with pytest.raises(ValueError, match="Unknown role"):
            can_action_queue("UNKNOWN_ROLE", "low_confidence")

    def test_unknown_queue_raises(self):
        with pytest.raises(ValueError, match="Unknown queue_type"):
            can_action_queue("REVIEWER", "unknown")


# ===========================================================================
# QueueManager.create_task
# ===========================================================================

class TestCreateTask:
    def test_valid_creates_pending_with_correct_role(self, db_session):
        subj = _make_subject(db_session)
        qm = QueueManager(db_session)

        task = qm.create_task("low_confidence", str(subj.subject_id))

        assert task.status == "PENDING"
        assert task.queue_type == "low_confidence"
        assert task.required_role == "REVIEWER"
        assert task.subject_id == subj.subject_id

    def test_duplicate_subject_queue_raises(self, db_session):
        subj = _make_subject(db_session)
        qm = QueueManager(db_session)

        qm.create_task("low_confidence", str(subj.subject_id))
        with pytest.raises(ValueError, match="already has a PENDING task"):
            qm.create_task("low_confidence", str(subj.subject_id))

    def test_same_subject_different_queue_ok(self, db_session):
        subj = _make_subject(db_session)
        qm = QueueManager(db_session)

        t1 = qm.create_task("low_confidence", str(subj.subject_id))
        t2 = qm.create_task("rra_review", str(subj.subject_id))

        assert t1.review_task_id != t2.review_task_id

    def test_invalid_queue_type_raises(self, db_session):
        subj = _make_subject(db_session)
        qm = QueueManager(db_session)

        with pytest.raises(ValueError, match="Unknown queue_type"):
            qm.create_task("bogus", str(subj.subject_id))


# ===========================================================================
# QueueManager.assign_task
# ===========================================================================

class TestAssignTask:
    def test_valid_role_sets_in_progress(self, db_session):
        subj = _make_subject(db_session)
        qm = QueueManager(db_session)
        task = qm.create_task("low_confidence", str(subj.subject_id))

        result = qm.assign_task(str(task.review_task_id), "rev-1", "REVIEWER")

        assert result.status == "IN_PROGRESS"
        assert result.assigned_to == "rev-1"

    def test_wrong_role_raises_permission(self, db_session):
        subj = _make_subject(db_session)
        qm = QueueManager(db_session)
        task = qm.create_task("escalation", str(subj.subject_id))

        with pytest.raises(PermissionError, match="cannot action queue"):
            qm.assign_task(str(task.review_task_id), "rev-1", "REVIEWER")

    def test_approver_can_assign_any(self, db_session):
        subj = _make_subject(db_session)
        qm = QueueManager(db_session)
        task = qm.create_task("escalation", str(subj.subject_id))

        result = qm.assign_task(str(task.review_task_id), "approver-1", "APPROVER")
        assert result.status == "IN_PROGRESS"

    def test_unknown_task_raises_key(self, db_session):
        qm = QueueManager(db_session)

        with pytest.raises(KeyError, match="not found"):
            qm.assign_task(str(uuid4()), "rev-1", "REVIEWER")

    def test_not_pending_raises_value(self, db_session):
        subj = _make_subject(db_session)
        qm = QueueManager(db_session)
        task = qm.create_task("low_confidence", str(subj.subject_id))
        qm.assign_task(str(task.review_task_id), "rev-1", "REVIEWER")

        with pytest.raises(ValueError, match="must be PENDING"):
            qm.assign_task(str(task.review_task_id), "rev-2", "REVIEWER")


# ===========================================================================
# QueueManager.complete_task
# ===========================================================================

class TestCompleteTask:
    def _setup_in_progress(self, db_session, queue_type="low_confidence"):
        subj = _make_subject(db_session)
        qm = QueueManager(db_session)
        task = qm.create_task(queue_type, str(subj.subject_id))
        qm.assign_task(str(task.review_task_id), "rev-1", "REVIEWER" if queue_type != "escalation" else "APPROVER")
        return qm, task

    def test_reviewer_approves(self, db_session):
        qm, task = self._setup_in_progress(db_session)

        result = qm.complete_task(
            str(task.review_task_id), "rev-1", "REVIEWER",
            decision="approved", rationale="Confirmed match",
            db_session_audit=db_session,
        )

        assert result.status == "COMPLETED"
        assert result.completed_at is not None

    def test_reviewer_rejects(self, db_session):
        qm, task = self._setup_in_progress(db_session)

        result = qm.complete_task(
            str(task.review_task_id), "rev-1", "REVIEWER",
            decision="rejected", rationale="False positive",
            db_session_audit=db_session,
        )

        assert result.status == "COMPLETED"

    def test_reviewer_escalates(self, db_session):
        qm, task = self._setup_in_progress(db_session)

        result = qm.complete_task(
            str(task.review_task_id), "rev-1", "REVIEWER",
            decision="escalated", rationale="Needs legal review",
            db_session_audit=db_session,
        )

        assert result.status == "COMPLETED"

    def test_legal_reviewer_without_regulatory_basis_raises(self, db_session):
        subj = _make_subject(db_session)
        qm = QueueManager(db_session)
        task = qm.create_task("escalation", str(subj.subject_id))
        qm.assign_task(str(task.review_task_id), "legal-1", "LEGAL_REVIEWER")

        with pytest.raises(ValueError, match="regulatory_basis is required"):
            qm.complete_task(
                str(task.review_task_id), "legal-1", "LEGAL_REVIEWER",
                decision="approved", rationale="Reviewed",
                db_session_audit=db_session,
            )

    def test_legal_reviewer_with_regulatory_basis_succeeds(self, db_session):
        subj = _make_subject(db_session)
        qm = QueueManager(db_session)
        task = qm.create_task("escalation", str(subj.subject_id))
        qm.assign_task(str(task.review_task_id), "legal-1", "LEGAL_REVIEWER")

        result = qm.complete_task(
            str(task.review_task_id), "legal-1", "LEGAL_REVIEWER",
            decision="approved", rationale="Compliant",
            db_session_audit=db_session,
            regulatory_basis="HIPAA §164.404",
        )

        assert result.status == "COMPLETED"

    def test_task_not_in_progress_raises(self, db_session):
        subj = _make_subject(db_session)
        qm = QueueManager(db_session)
        task = qm.create_task("low_confidence", str(subj.subject_id))

        with pytest.raises(ValueError, match="must be IN_PROGRESS"):
            qm.complete_task(
                str(task.review_task_id), "rev-1", "REVIEWER",
                decision="approved", rationale="ok",
                db_session_audit=db_session,
            )

    def test_invalid_decision_raises(self, db_session):
        qm, task = self._setup_in_progress(db_session)

        with pytest.raises(ValueError, match="Invalid decision"):
            qm.complete_task(
                str(task.review_task_id), "rev-1", "REVIEWER",
                decision="maybe", rationale="hmm",
                db_session_audit=db_session,
            )

    def test_empty_rationale_raises(self, db_session):
        qm, task = self._setup_in_progress(db_session)

        with pytest.raises(ValueError, match="rationale must be non-empty"):
            qm.complete_task(
                str(task.review_task_id), "rev-1", "REVIEWER",
                decision="approved", rationale="",
                db_session_audit=db_session,
            )


# ===========================================================================
# QueueManager.get_queue
# ===========================================================================

class TestGetQueue:
    def test_returns_tasks_in_created_order(self, db_session):
        s1 = _make_subject(db_session)
        s2 = _make_subject(db_session)
        s3 = _make_subject(db_session)
        qm = QueueManager(db_session)

        qm.create_task("low_confidence", str(s1.subject_id))
        qm.create_task("low_confidence", str(s2.subject_id))
        qm.create_task("low_confidence", str(s3.subject_id))

        tasks = qm.get_queue("low_confidence")
        assert len(tasks) == 3
        sids = [str(t.subject_id) for t in tasks]
        assert sids == [str(s1.subject_id), str(s2.subject_id), str(s3.subject_id)]

    def test_filters_by_status(self, db_session):
        s1 = _make_subject(db_session)
        s2 = _make_subject(db_session)
        qm = QueueManager(db_session)

        t1 = qm.create_task("low_confidence", str(s1.subject_id))
        qm.create_task("low_confidence", str(s2.subject_id))
        qm.assign_task(str(t1.review_task_id), "rev-1", "REVIEWER")

        pending = qm.get_queue("low_confidence", status="PENDING")
        in_progress = qm.get_queue("low_confidence", status="IN_PROGRESS")

        assert len(pending) == 1
        assert len(in_progress) == 1
        assert str(pending[0].subject_id) == str(s2.subject_id)
        assert str(in_progress[0].subject_id) == str(s1.subject_id)

    def test_empty_queue(self, db_session):
        qm = QueueManager(db_session)
        assert qm.get_queue("escalation") == []
