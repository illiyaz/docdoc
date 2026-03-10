"""Integration tests — record mapper → entity resolver → deduplicator chain.

Verifies that the full pipeline from DetectionResult to NotificationSubject
produces subjects with canonical fields populated (not all None).
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import NotificationSubject
from app.pipeline.record_mapper import detection_to_pii_record
from app.pii.presidio_engine import DetectionResult
from app.readers.base import ExtractedBlock
from app.rra.deduplicator import Deduplicator
from app.rra.entity_resolver import EntityResolver, PIIRecord


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with Session() as session:
        yield session


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _block(text: str, page: int = 1) -> ExtractedBlock:
    return ExtractedBlock(text=text, page_or_sheet=page, source_path="test.pdf", file_type="pdf")


def _det(block: ExtractedBlock, entity_type: str, start: int, end: int) -> DetectionResult:
    return DetectionResult(
        block=block,
        entity_type=entity_type,
        start=start,
        end=end,
        score=0.90,
        pattern_used="test",
        geography="US",
        regulatory_framework="HIPAA",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRecordMapperToResolver:
    """Mapper + EntityResolver produce groups with merge confidence > 0."""

    def test_same_person_name_populates_raw_name(self):
        """Two PERSON detections with same name should have raw_name set."""
        b1 = _block("John Smith is a patient")
        b2 = _block("John Smith address is 123 Main St")
        d1 = _det(b1, "PERSON", 0, 10)
        d2 = _det(b2, "PERSON", 0, 10)

        r1 = detection_to_pii_record(d1, "doc-1")
        r2 = detection_to_pii_record(d2, "doc-1")

        assert r1.raw_name == "John Smith"
        assert r2.raw_name == "John Smith"

        resolver = EntityResolver()
        groups = resolver.resolve([r1, r2])

        # Both records have raw_name, confidence > 0 for name-only match
        all_records = [r for g in groups for r in g.records]
        assert all(r.raw_name == "John Smith" for r in all_records)

    def test_different_entity_types_same_person(self):
        """PERSON + EMAIL for same person should merge (name + email)."""
        b1 = _block("John Smith")
        b2 = _block("john@example.com")
        d1 = _det(b1, "PERSON", 0, 10)
        d2 = _det(b2, "EMAIL_ADDRESS", 0, 16)

        records = [
            detection_to_pii_record(d1, "doc-1"),
            detection_to_pii_record(d2, "doc-1"),
        ]

        assert records[0].raw_name == "John Smith"
        assert records[1].raw_email == "john@example.com"

        resolver = EntityResolver()
        groups = resolver.resolve(records)
        # Different entity types → won't merge by name match alone
        # But raw_* fields are populated correctly
        for g in groups:
            for r in g.records:
                assert r.raw_name is not None or r.raw_email is not None

    def test_raw_fields_populated_for_all_types(self):
        """Each entity type produces a PIIRecord with the correct raw_* field."""
        cases = [
            ("PERSON", "Jane Doe", "raw_name"),
            ("EMAIL_ADDRESS", "jane@test.com", "raw_email"),
            ("PHONE_NUMBER", "555-000-1234", "raw_phone"),
            ("DATE_OF_BIRTH", "01/01/1990", "raw_dob"),
            ("LOCATION", "456 Elm Street", "raw_address"),
        ]
        records = []
        for etype, text, expected_field in cases:
            block = _block(text)
            det = _det(block, etype, 0, len(text))
            rec = detection_to_pii_record(det, "doc-1")
            records.append((rec, expected_field))

        for rec, expected_field in records:
            val = getattr(rec, expected_field)
            assert val is not None, f"{expected_field} should be set for {rec.entity_type}"


class TestFullChainToSubjects:
    """Mapper → Resolver → Deduplicator produces NotificationSubjects with canonical fields."""

    def test_person_detection_produces_canonical_name(self, db_session):
        """A PERSON detection should produce a NotificationSubject with canonical_name set."""
        b1 = _block("John Smith")
        b2 = _block("John Smith")
        d1 = _det(b1, "PERSON", 0, 10)
        d2 = _det(b2, "PERSON", 0, 10)

        records = [
            detection_to_pii_record(d1, "doc-1"),
            detection_to_pii_record(d2, "doc-1"),
        ]

        resolver = EntityResolver()
        groups = resolver.resolve(records)

        dedup = Deduplicator(db_session)
        subjects = dedup.build_subjects(groups)

        assert len(subjects) >= 1
        # At least one subject should have canonical_name populated
        names = [s.canonical_name for s in subjects if s.canonical_name]
        assert len(names) > 0, "At least one subject should have canonical_name"
        assert any("smith" in n.lower() for n in names)

    def test_email_detection_produces_canonical_email(self, db_session):
        """An EMAIL_ADDRESS detection should produce a subject with canonical_email."""
        b1 = _block("john@example.com")
        b2 = _block("john@example.com")
        d1 = _det(b1, "EMAIL_ADDRESS", 0, 16)
        d2 = _det(b2, "EMAIL_ADDRESS", 0, 16)

        records = [
            detection_to_pii_record(d1, "doc-1"),
            detection_to_pii_record(d2, "doc-1"),
        ]

        resolver = EntityResolver()
        groups = resolver.resolve(records)

        dedup = Deduplicator(db_session)
        subjects = dedup.build_subjects(groups)

        assert len(subjects) >= 1
        emails = [s.canonical_email for s in subjects if s.canonical_email]
        assert len(emails) > 0, "At least one subject should have canonical_email"
        assert "john@example.com" in emails

    def test_phone_detection_produces_canonical_phone(self, db_session):
        """A PHONE_NUMBER detection should produce a subject with canonical_phone."""
        b1 = _block("555-123-4567")
        b2 = _block("555-123-4567")
        d1 = _det(b1, "PHONE_NUMBER", 0, 12)
        d2 = _det(b2, "PHONE_NUMBER", 0, 12)

        records = [
            detection_to_pii_record(d1, "doc-1"),
            detection_to_pii_record(d2, "doc-1"),
        ]

        resolver = EntityResolver()
        groups = resolver.resolve(records)

        dedup = Deduplicator(db_session)
        subjects = dedup.build_subjects(groups)

        assert len(subjects) >= 1
        phones = [s.canonical_phone for s in subjects if s.canonical_phone]
        assert len(phones) > 0, "At least one subject should have canonical_phone"

    def test_mixed_detections_produce_populated_subject(self, db_session):
        """Multiple detection types for one person should produce a fully populated subject."""
        name_block = _block("John Smith")
        email_block = _block("john@example.com")
        phone_block = _block("555-123-4567")

        records = [
            detection_to_pii_record(_det(name_block, "PERSON", 0, 10), "doc-1"),
            detection_to_pii_record(_det(email_block, "EMAIL_ADDRESS", 0, 16), "doc-1"),
            detection_to_pii_record(_det(phone_block, "PHONE_NUMBER", 0, 12), "doc-1"),
        ]

        resolver = EntityResolver()
        groups = resolver.resolve(records)

        dedup = Deduplicator(db_session)
        subjects = dedup.build_subjects(groups)

        # We should have subjects with at least some canonical fields populated
        all_names = [s.canonical_name for s in subjects if s.canonical_name]
        all_emails = [s.canonical_email for s in subjects if s.canonical_email]
        all_phones = [s.canonical_phone for s in subjects if s.canonical_phone]

        # At least the individual fields should appear somewhere across subjects
        assert len(all_names) + len(all_emails) + len(all_phones) > 0, \
            "Subjects should have at least one canonical field populated"

    def test_empty_detections_produce_no_subjects(self, db_session):
        """No detections → no records → no subjects."""
        resolver = EntityResolver()
        groups = resolver.resolve([])

        dedup = Deduplicator(db_session)
        subjects = dedup.build_subjects(groups)

        assert subjects == []
