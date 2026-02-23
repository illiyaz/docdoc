"""Tests for app/rra/deduplicator.py — Phase 2 deduplication + subject building."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import NotificationSubject
from app.rra.deduplicator import Deduplicator, _best_value, _best_address
from app.rra.entity_resolver import PIIRecord, ResolvedGroup


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


def _rec(
    *,
    record_id: str = "r1",
    entity_type: str = "PERSON",
    normalized_value: str = "",
    raw_name: str | None = None,
    raw_address: dict | None = None,
    raw_phone: str | None = None,
    raw_email: str | None = None,
    raw_dob: str | None = None,
    country: str = "US",
    source_document_id: str = "doc1",
    page_or_sheet: str | int = 0,
) -> PIIRecord:
    return PIIRecord(
        record_id=record_id,
        entity_type=entity_type,
        normalized_value=normalized_value,
        raw_name=raw_name,
        raw_address=raw_address,
        raw_phone=raw_phone,
        raw_email=raw_email,
        raw_dob=raw_dob,
        country=country,
        source_document_id=source_document_id,
        page_or_sheet=page_or_sheet,
    )


def _group(
    records: list[PIIRecord],
    merge_confidence: float = 1.0,
    needs_human_review: bool = False,
) -> ResolvedGroup:
    return ResolvedGroup(
        records=records,
        merge_confidence=merge_confidence,
        needs_human_review=needs_human_review,
    )


# ===========================================================================
# _best_value / canonical field selection
# ===========================================================================

class TestBestValue:
    def test_most_frequent_wins(self):
        assert _best_value(["alice", "bob", "alice"]) == "alice"

    def test_tie_on_count_longest_wins(self):
        assert _best_value(["Al", "Bob"]) == "Bob"

    def test_tie_on_count_and_length_alpha_first(self):
        assert _best_value(["Bob", "Ann"]) == "Ann"

    def test_all_none_returns_none(self):
        assert _best_value([None, None]) is None

    def test_empty_list(self):
        assert _best_value([]) is None

    def test_single_value(self):
        assert _best_value(["alice"]) == "alice"

    def test_none_values_ignored(self):
        assert _best_value([None, "alice", None]) == "alice"

    def test_empty_strings_ignored(self):
        assert _best_value(["", "alice", ""]) == "alice"


class TestBestAddress:
    def test_most_frequent_zip_wins(self):
        a1 = {"street": "123 main", "zip": "62701", "country": "US"}
        a2 = {"street": "456 oak", "zip": "90210", "country": "US"}
        a3 = {"street": "789 elm", "zip": "62701", "country": "US"}
        result = _best_address([a1, a2, a3])
        assert result["zip"] == "62701"

    def test_all_none_returns_none(self):
        assert _best_address([None, None]) is None

    def test_empty_list_returns_none(self):
        assert _best_address([]) is None

    def test_single_address(self):
        a = {"street": "1 st", "zip": "10001", "country": "US"}
        assert _best_address([a]) == a


# ===========================================================================
# Deduplicator.build_subjects
# ===========================================================================

class TestBuildSubjects:
    def test_single_record_group(self, db_session):
        r = _rec(
            raw_name="John Smith",
            raw_email="john@example.com",
            raw_phone="+15551234567",
            entity_type="US_SSN",
        )
        group = _group([r])
        dedup = Deduplicator(db_session)
        subjects = dedup.build_subjects([group])
        db_session.commit()

        assert len(subjects) == 1
        ns = subjects[0]
        assert ns.canonical_name == "John Smith"
        assert ns.canonical_email == "john@example.com"
        assert ns.canonical_phone == "+15551234567"
        assert ns.pii_types_found == ["US_SSN"]
        assert ns.source_records == ["r1"]
        assert ns.notification_required is False
        assert ns.review_status == "AI_PENDING"

    def test_three_record_group_pii_types_sorted_unique(self, db_session):
        r1 = _rec(record_id="r1", entity_type="EMAIL", raw_email="a@b.com")
        r2 = _rec(record_id="r2", entity_type="US_SSN", raw_email="a@b.com")
        r3 = _rec(record_id="r3", entity_type="EMAIL", raw_email="a@b.com")
        group = _group([r1, r2, r3])
        dedup = Deduplicator(db_session)
        subjects = dedup.build_subjects([group])
        db_session.commit()

        assert subjects[0].pii_types_found == ["EMAIL", "US_SSN"]

    def test_source_records_contains_all_ids(self, db_session):
        r1 = _rec(record_id="aaa", raw_email="x@y.com")
        r2 = _rec(record_id="bbb", raw_email="x@y.com")
        r3 = _rec(record_id="ccc", raw_email="x@y.com")
        group = _group([r1, r2, r3])
        dedup = Deduplicator(db_session)
        subjects = dedup.build_subjects([group])
        db_session.commit()

        assert set(subjects[0].source_records) == {"aaa", "bbb", "ccc"}

    def test_needs_human_review_true_sets_human_review(self, db_session):
        r = _rec(raw_email="a@b.com")
        group = _group([r], merge_confidence=0.65, needs_human_review=True)
        dedup = Deduplicator(db_session)
        subjects = dedup.build_subjects([group])
        db_session.commit()

        assert subjects[0].review_status == "HUMAN_REVIEW"

    def test_needs_human_review_false_sets_ai_pending(self, db_session):
        r = _rec(raw_email="a@b.com")
        group = _group([r], merge_confidence=0.90, needs_human_review=False)
        dedup = Deduplicator(db_session)
        subjects = dedup.build_subjects([group])
        db_session.commit()

        assert subjects[0].review_status == "AI_PENDING"

    def test_notification_required_always_false(self, db_session):
        r = _rec(raw_email="a@b.com")
        group = _group([r])
        dedup = Deduplicator(db_session)
        subjects = dedup.build_subjects([group])
        db_session.commit()

        assert subjects[0].notification_required is False

    def test_most_frequent_name_wins(self, db_session):
        r1 = _rec(record_id="r1", raw_name="John Smith", entity_type="A")
        r2 = _rec(record_id="r2", raw_name="John Smith", entity_type="B")
        r3 = _rec(record_id="r3", raw_name="Jonathan Smith", entity_type="C")
        group = _group([r1, r2, r3])
        dedup = Deduplicator(db_session)
        subjects = dedup.build_subjects([group])
        db_session.commit()

        assert subjects[0].canonical_name == "John Smith"

    def test_all_none_emails_gives_none(self, db_session):
        r1 = _rec(record_id="r1", raw_email=None, entity_type="X")
        r2 = _rec(record_id="r2", raw_email=None, entity_type="Y")
        group = _group([r1, r2])
        dedup = Deduplicator(db_session)
        subjects = dedup.build_subjects([group])
        db_session.commit()

        assert subjects[0].canonical_email is None

    def test_upsert_same_email_merges_into_one(self, db_session):
        """Two groups with the same canonical_email → one subject."""
        g1 = _group([
            _rec(record_id="r1", raw_email="shared@x.com", entity_type="EMAIL"),
        ], merge_confidence=0.90)
        g2 = _group([
            _rec(record_id="r2", raw_email="shared@x.com", entity_type="US_SSN"),
        ], merge_confidence=0.80)

        dedup = Deduplicator(db_session)
        subjects = dedup.build_subjects([g1, g2])
        db_session.commit()

        # Only one unique subject returned (the second merged into the first)
        persisted = db_session.query(NotificationSubject).all()
        assert len(persisted) == 1
        ns = persisted[0]
        assert set(ns.pii_types_found) == {"EMAIL", "US_SSN"}
        assert set(ns.source_records) == {"r1", "r2"}

    def test_upsert_keeps_lower_merge_confidence(self, db_session):
        g1 = _group([
            _rec(record_id="r1", raw_email="shared@x.com", entity_type="A"),
        ], merge_confidence=0.90)
        g2 = _group([
            _rec(record_id="r2", raw_email="shared@x.com", entity_type="B"),
        ], merge_confidence=0.70)

        dedup = Deduplicator(db_session)
        dedup.build_subjects([g1, g2])
        db_session.commit()

        ns = db_session.query(NotificationSubject).one()
        assert ns.merge_confidence == pytest.approx(0.70)

    def test_upsert_by_phone_when_no_email(self, db_session):
        g1 = _group([
            _rec(record_id="r1", raw_phone="+15551234567", entity_type="A"),
        ])
        g2 = _group([
            _rec(record_id="r2", raw_phone="+15551234567", entity_type="B"),
        ])

        dedup = Deduplicator(db_session)
        dedup.build_subjects([g1, g2])
        db_session.commit()

        persisted = db_session.query(NotificationSubject).all()
        assert len(persisted) == 1
        assert set(persisted[0].source_records) == {"r1", "r2"}

    def test_no_email_no_phone_creates_separate_subjects(self, db_session):
        g1 = _group([_rec(record_id="r1", entity_type="A")])
        g2 = _group([_rec(record_id="r2", entity_type="B")])

        dedup = Deduplicator(db_session)
        dedup.build_subjects([g1, g2])
        db_session.commit()

        persisted = db_session.query(NotificationSubject).all()
        assert len(persisted) == 2

    def test_persisted_to_db(self, db_session):
        r = _rec(raw_email="test@example.com")
        dedup = Deduplicator(db_session)
        dedup.build_subjects([_group([r])])
        db_session.commit()

        count = db_session.query(NotificationSubject).count()
        assert count == 1

    def test_merge_confidence_from_group(self, db_session):
        r = _rec(raw_email="a@b.com")
        group = _group([r], merge_confidence=0.73)
        dedup = Deduplicator(db_session)
        subjects = dedup.build_subjects([group])
        db_session.commit()

        assert subjects[0].merge_confidence == pytest.approx(0.73)
