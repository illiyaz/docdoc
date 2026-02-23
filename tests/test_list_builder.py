"""Tests for app/notification/list_builder.py — Phase 3."""
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import NotificationList, NotificationSubject
from app.notification.list_builder import build_notification_list, get_notification_subjects
from app.protocols.protocol import Protocol


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with Session() as session:
        yield session


def _hipaa() -> Protocol:
    return Protocol(
        protocol_id="hipaa_breach_rule",
        name="HIPAA",
        jurisdiction="US-FEDERAL",
        triggering_entity_types=["US_SSN", "PHI_MRN"],
        notification_threshold=1,
        notification_deadline_days=60,
        required_notification_content=["desc"],
        regulatory_framework="45 CFR §164.400-414",
    )


def _subject(db_session, pii_types: list[str]) -> NotificationSubject:
    ns = NotificationSubject(
        subject_id=uuid4(),
        pii_types_found=pii_types,
        notification_required=False,
        review_status="AI_PENDING",
    )
    db_session.add(ns)
    db_session.flush()
    return ns


# ===========================================================================
# build_notification_list
# ===========================================================================

class TestBuildNotificationList:
    def test_two_of_three_triggered(self, db_session):
        s1 = _subject(db_session, ["US_SSN", "EMAIL"])
        s2 = _subject(db_session, ["PHI_MRN"])
        s3 = _subject(db_session, ["EMAIL", "PHONE"])

        nl = build_notification_list("job-1", _hipaa(), [s1, s2, s3], db_session)
        db_session.commit()

        assert len(nl.subject_ids) == 2
        triggered = set(nl.subject_ids)
        assert str(s1.subject_id) in triggered
        assert str(s2.subject_id) in triggered
        assert str(s3.subject_id) not in triggered

    def test_persisted_in_db(self, db_session):
        s = _subject(db_session, ["US_SSN"])
        nl = build_notification_list("job-2", _hipaa(), [s], db_session)
        db_session.commit()

        persisted = db_session.get(NotificationList, nl.notification_list_id)
        assert persisted is not None
        assert persisted.job_id == "job-2"

    def test_protocol_id_matches(self, db_session):
        s = _subject(db_session, ["US_SSN"])
        nl = build_notification_list("job-3", _hipaa(), [s], db_session)
        db_session.commit()

        assert nl.protocol_id == "hipaa_breach_rule"

    def test_job_id_matches(self, db_session):
        s = _subject(db_session, ["US_SSN"])
        nl = build_notification_list("my-job-id", _hipaa(), [s], db_session)
        db_session.commit()

        assert nl.job_id == "my-job-id"

    def test_status_pending_when_triggered(self, db_session):
        s = _subject(db_session, ["US_SSN"])
        nl = build_notification_list("job-4", _hipaa(), [s], db_session)
        db_session.commit()

        assert nl.status == "PENDING"

    def test_status_empty_when_none_triggered(self, db_session):
        s = _subject(db_session, ["EMAIL", "PHONE"])
        nl = build_notification_list("job-5", _hipaa(), [s], db_session)
        db_session.commit()

        assert nl.status == "EMPTY"
        assert nl.subject_ids == []

    def test_all_triggered(self, db_session):
        s1 = _subject(db_session, ["US_SSN"])
        s2 = _subject(db_session, ["PHI_MRN"])
        nl = build_notification_list("job-6", _hipaa(), [s1, s2], db_session)
        db_session.commit()

        assert len(nl.subject_ids) == 2


# ===========================================================================
# get_notification_subjects
# ===========================================================================

class TestGetNotificationSubjects:
    def test_returns_correct_subjects_in_order(self, db_session):
        s1 = _subject(db_session, ["US_SSN"])
        s2 = _subject(db_session, ["PHI_MRN"])
        nl = build_notification_list("job-7", _hipaa(), [s1, s2], db_session)
        db_session.commit()

        results = get_notification_subjects(nl, db_session)

        assert len(results) == 2
        result_ids = [str(r.subject_id) for r in results]
        assert result_ids == nl.subject_ids

    def test_missing_subject_skipped_with_warning(self, db_session, caplog):
        s = _subject(db_session, ["US_SSN"])
        nl = build_notification_list("job-8", _hipaa(), [s], db_session)
        db_session.commit()

        # Inject a fake UUID that doesn't exist
        fake_id = str(uuid4())
        nl.subject_ids = [fake_id, str(s.subject_id)]
        db_session.flush()
        db_session.commit()

        with caplog.at_level("WARNING"):
            results = get_notification_subjects(nl, db_session)

        assert len(results) == 1
        assert str(results[0].subject_id) == str(s.subject_id)
        assert fake_id in caplog.text

    def test_empty_subject_ids(self, db_session):
        s = _subject(db_session, ["EMAIL"])
        nl = build_notification_list("job-9", _hipaa(), [s], db_session)
        db_session.commit()

        # status is EMPTY, subject_ids is []
        results = get_notification_subjects(nl, db_session)
        assert results == []
