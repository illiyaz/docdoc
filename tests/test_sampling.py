"""Tests for app/review/sampling.py — Phase 4."""
from __future__ import annotations

import random
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import NotificationSubject
from app.review.queue_manager import QueueManager
from app.review.sampling import SamplingStrategy


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


def _make_subjects(db_session, n: int, status="AI_PENDING") -> list[NotificationSubject]:
    subjects = []
    for _ in range(n):
        ns = NotificationSubject(
            subject_id=uuid4(),
            pii_types_found=["US_SSN"],
            notification_required=True,
            review_status=status,
        )
        db_session.add(ns)
        subjects.append(ns)
    db_session.flush()
    return subjects


# ===========================================================================
# __init__ validation
# ===========================================================================

class TestInit:
    def test_rate_zero_raises(self, db_session):
        with pytest.raises(ValueError, match="sample_rate"):
            SamplingStrategy(db_session, sample_rate=0.0)

    def test_rate_negative_raises(self, db_session):
        with pytest.raises(ValueError, match="sample_rate"):
            SamplingStrategy(db_session, sample_rate=-0.1)

    def test_rate_above_one_raises(self, db_session):
        with pytest.raises(ValueError, match="sample_rate"):
            SamplingStrategy(db_session, sample_rate=1.5)

    def test_rate_one_valid(self, db_session):
        ss = SamplingStrategy(db_session, sample_rate=1.0)
        assert ss.sample_rate == 1.0

    def test_min_sample_zero_raises(self, db_session):
        with pytest.raises(ValueError, match="min_sample"):
            SamplingStrategy(db_session, min_sample=0)


# ===========================================================================
# calculate_sample_size
# ===========================================================================

class TestCalculateSampleSize:
    def test_100_at_5_percent(self, db_session):
        ss = SamplingStrategy(db_session, sample_rate=0.05)
        assert ss.calculate_sample_size(100) == 5

    def test_100_at_10_percent(self, db_session):
        ss = SamplingStrategy(db_session, sample_rate=0.10)
        assert ss.calculate_sample_size(100) == 10

    def test_3_at_5_percent_uses_min(self, db_session):
        ss = SamplingStrategy(db_session, sample_rate=0.05, min_sample=1)
        # ceil(3 * 0.05) = ceil(0.15) = 1, max(1,1) = 1
        assert ss.calculate_sample_size(3) == 1

    def test_zero_population(self, db_session):
        ss = SamplingStrategy(db_session, sample_rate=0.05)
        assert ss.calculate_sample_size(0) == 0

    def test_max_sample_caps(self, db_session):
        ss = SamplingStrategy(db_session, sample_rate=0.10, max_sample=3)
        # ceil(100 * 0.10) = 10, capped at 3
        assert ss.calculate_sample_size(100) == 3

    def test_never_exceeds_population(self, db_session):
        ss = SamplingStrategy(db_session, sample_rate=1.0, min_sample=50)
        # min_sample=50 but population=10 → capped at 10
        assert ss.calculate_sample_size(10) == 10


# ===========================================================================
# generate_qc_sample
# ===========================================================================

class TestGenerateQcSample:
    def test_20_subjects_at_5_percent(self, db_session):
        random.seed(42)
        _make_subjects(db_session, 20)
        ss = SamplingStrategy(db_session, sample_rate=0.05)
        qm = QueueManager(db_session)

        tasks = ss.generate_qc_sample(qm)

        # ceil(20 * 0.05) = 1, max(1, 1) = 1
        assert len(tasks) == 1

    def test_20_subjects_at_10_percent(self, db_session):
        random.seed(42)
        _make_subjects(db_session, 20)
        ss = SamplingStrategy(db_session, sample_rate=0.10)
        qm = QueueManager(db_session)

        tasks = ss.generate_qc_sample(qm)

        # ceil(20 * 0.10) = 2
        assert len(tasks) == 2

    def test_existing_qc_task_skipped(self, db_session):
        random.seed(42)
        subjects = _make_subjects(db_session, 5)
        qm = QueueManager(db_session)

        # Pre-create a qc_sampling task for every subject
        for s in subjects:
            qm.create_task("qc_sampling", str(s.subject_id))

        ss = SamplingStrategy(db_session, sample_rate=1.0)
        tasks = ss.generate_qc_sample(qm)

        # All skipped because they already have PENDING qc_sampling tasks
        assert tasks == []

    def test_no_ai_pending_subjects(self, db_session):
        _make_subjects(db_session, 5, status="APPROVED")
        ss = SamplingStrategy(db_session, sample_rate=0.10)
        qm = QueueManager(db_session)

        tasks = ss.generate_qc_sample(qm)
        assert tasks == []

    def test_all_tasks_are_qc_sampling(self, db_session):
        random.seed(42)
        _make_subjects(db_session, 10)
        ss = SamplingStrategy(db_session, sample_rate=0.50)
        qm = QueueManager(db_session)

        tasks = ss.generate_qc_sample(qm)
        assert all(t.queue_type == "qc_sampling" for t in tasks)

    def test_all_tasks_require_qc_sampler_role(self, db_session):
        random.seed(42)
        _make_subjects(db_session, 10)
        ss = SamplingStrategy(db_session, sample_rate=0.50)
        qm = QueueManager(db_session)

        tasks = ss.generate_qc_sample(qm)
        assert all(t.required_role == "QC_SAMPLER" for t in tasks)

    def test_distinct_subjects(self, db_session):
        random.seed(42)
        _make_subjects(db_session, 20)
        ss = SamplingStrategy(db_session, sample_rate=0.50)
        qm = QueueManager(db_session)

        tasks = ss.generate_qc_sample(qm)
        subject_ids = [str(t.subject_id) for t in tasks]
        assert len(subject_ids) == len(set(subject_ids))
