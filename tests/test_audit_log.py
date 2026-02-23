"""Tests for app/audit/audit_log.py — Phase 4."""
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.audit.audit_log import get_events_by_type, get_subject_history, record_event
from app.audit.events import (
    EVENT_AI_EXTRACTION,
    EVENT_APPROVAL,
    EVENT_ESCALATION,
    EVENT_HUMAN_REVIEW,
    EVENT_LEGAL_REVIEW,
    EVENT_NOTIFICATION_SENT,
    EVENT_PROTOCOL_APPLIED,
    EVENT_RRA_MERGE,
    VALID_EVENT_TYPES,
)
from app.db.base import Base


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


# ===========================================================================
# record_event
# ===========================================================================

class TestRecordEvent:
    def test_valid_event_persisted_and_immutable(self, db_session):
        ev = record_event(
            db_session,
            event_type=EVENT_AI_EXTRACTION,
            actor="system",
            subject_id="subj-1",
        )
        db_session.commit()

        assert ev.audit_event_id is not None
        assert ev.event_type == EVENT_AI_EXTRACTION
        assert ev.actor == "system"
        assert ev.subject_id == "subj-1"
        assert ev.immutable is True

    def test_invalid_event_type_raises(self, db_session):
        with pytest.raises(ValueError, match="Invalid event_type"):
            record_event(db_session, event_type="bogus", actor="system")

    def test_empty_actor_raises(self, db_session):
        with pytest.raises(ValueError, match="actor must be a non-empty string"):
            record_event(db_session, event_type=EVENT_AI_EXTRACTION, actor="")

    def test_whitespace_actor_raises(self, db_session):
        with pytest.raises(ValueError, match="actor must be a non-empty string"):
            record_event(db_session, event_type=EVENT_AI_EXTRACTION, actor="   ")

    def test_legal_review_without_regulatory_basis_raises(self, db_session):
        with pytest.raises(ValueError, match="regulatory_basis is required"):
            record_event(
                db_session,
                event_type=EVENT_LEGAL_REVIEW,
                actor="legal-1",
                rationale="reviewed",
            )

    def test_legal_review_with_regulatory_basis_succeeds(self, db_session):
        ev = record_event(
            db_session,
            event_type=EVENT_LEGAL_REVIEW,
            actor="legal-1",
            rationale="reviewed",
            regulatory_basis="HIPAA §164.404",
        )
        db_session.commit()

        assert ev.event_type == EVENT_LEGAL_REVIEW
        assert ev.regulatory_basis == "HIPAA §164.404"

    def test_human_review_without_rationale_raises(self, db_session):
        with pytest.raises(ValueError, match="rationale is required"):
            record_event(
                db_session,
                event_type=EVENT_HUMAN_REVIEW,
                actor="reviewer-1",
            )

    def test_human_review_with_rationale_succeeds(self, db_session):
        ev = record_event(
            db_session,
            event_type=EVENT_HUMAN_REVIEW,
            actor="reviewer-1",
            rationale="confirmed SSN match",
        )
        db_session.commit()

        assert ev.rationale == "confirmed SSN match"

    def test_approval_without_rationale_raises(self, db_session):
        with pytest.raises(ValueError, match="rationale is required"):
            record_event(
                db_session,
                event_type=EVENT_APPROVAL,
                actor="approver-1",
            )

    def test_pii_fields_never_in_logs(self, db_session, caplog):
        sid = str(uuid4())
        with caplog.at_level("DEBUG"):
            record_event(
                db_session,
                event_type=EVENT_AI_EXTRACTION,
                actor="system",
                subject_id=sid,
                pii_record_id="pii-secret-123",
                rationale="sensitive rationale text",
                regulatory_basis="secret regulatory ref",
            )

        assert sid not in caplog.text
        assert "pii-secret-123" not in caplog.text
        assert "sensitive rationale text" not in caplog.text
        assert "secret regulatory ref" not in caplog.text
        # actor and event_type ARE logged
        assert "ai_extraction" in caplog.text
        assert "system" in caplog.text

    def test_two_events_same_subject_both_persisted(self, db_session):
        sid = "subj-dup"
        e1 = record_event(
            db_session,
            event_type=EVENT_AI_EXTRACTION,
            actor="system",
            subject_id=sid,
        )
        e2 = record_event(
            db_session,
            event_type=EVENT_NOTIFICATION_SENT,
            actor="system",
            subject_id=sid,
        )
        db_session.commit()

        assert e1.audit_event_id != e2.audit_event_id
        history = get_subject_history(db_session, sid)
        assert len(history) == 2


# ===========================================================================
# get_subject_history
# ===========================================================================

class TestGetSubjectHistory:
    def test_returns_events_in_timestamp_order(self, db_session):
        sid = "subj-order"
        record_event(db_session, event_type=EVENT_AI_EXTRACTION, actor="system", subject_id=sid)
        record_event(db_session, event_type=EVENT_PROTOCOL_APPLIED, actor="system", subject_id=sid)
        record_event(db_session, event_type=EVENT_NOTIFICATION_SENT, actor="system", subject_id=sid)
        db_session.commit()

        history = get_subject_history(db_session, sid)
        assert len(history) == 3
        types = [e.event_type for e in history]
        assert types == [EVENT_AI_EXTRACTION, EVENT_PROTOCOL_APPLIED, EVENT_NOTIFICATION_SENT]

    def test_unknown_subject_returns_empty(self, db_session):
        history = get_subject_history(db_session, "nonexistent")
        assert history == []


# ===========================================================================
# get_events_by_type
# ===========================================================================

class TestGetEventsByType:
    def test_returns_only_matching_type(self, db_session):
        record_event(db_session, event_type=EVENT_AI_EXTRACTION, actor="system")
        record_event(db_session, event_type=EVENT_RRA_MERGE, actor="system")
        record_event(db_session, event_type=EVENT_AI_EXTRACTION, actor="system")
        db_session.commit()

        results = get_events_by_type(db_session, EVENT_AI_EXTRACTION)
        assert len(results) == 2
        assert all(e.event_type == EVENT_AI_EXTRACTION for e in results)

    def test_invalid_type_raises(self, db_session):
        with pytest.raises(ValueError, match="Invalid event_type"):
            get_events_by_type(db_session, "not_real")


# ===========================================================================
# events.py constants
# ===========================================================================

class TestEventConstants:
    def test_all_eight_types_in_frozenset(self):
        assert len(VALID_EVENT_TYPES) == 8
        for t in [
            EVENT_AI_EXTRACTION, EVENT_HUMAN_REVIEW, EVENT_ESCALATION,
            EVENT_LEGAL_REVIEW, EVENT_APPROVAL, EVENT_NOTIFICATION_SENT,
            EVENT_PROTOCOL_APPLIED, EVENT_RRA_MERGE,
        ]:
            assert t in VALID_EVENT_TYPES
