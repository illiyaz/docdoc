"""End-to-end smoke tests for the full RRA pipeline.

PIIRecords → EntityResolver → Deduplicator → NotificationSubjects.

No mocks — real implementations of all components, SQLite in-memory DB.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import NotificationSubject
from app.rra.entity_resolver import EntityResolver, PIIRecord
from app.rra.deduplicator import Deduplicator


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


def _run_pipeline(
    records: list[PIIRecord],
    db_session,
) -> list[NotificationSubject]:
    """Run full RRA pipeline: resolve → deduplicate → persist."""
    resolver = EntityResolver()
    groups = resolver.resolve(records)
    dedup = Deduplicator(db_session)
    subjects = dedup.build_subjects(groups)
    db_session.commit()
    return subjects


# ===========================================================================
# Scenario 1 — Same person across three documents, different formats
# ===========================================================================

def test_same_person_three_documents(db_session):
    """Three records for 'John Smith' from different documents.

    A↔B linked via gmail dot-normalized email match
    (john.smith@gmail.com == johnsmith@gmail.com after normalization).
    A↔C linked via shared SSN + shared phone.
    B↔C linked transitively through A (union-find).
    Result: one NotificationSubject covering all three records.
    """
    a = PIIRecord(
        record_id="a",
        entity_type="US_SSN",
        normalized_value="123-45-6789",
        raw_name="John Smith",
        raw_email="john.smith@gmail.com",
        raw_phone="+12125551234",
        country="US",
        source_document_id="doc1",
        page_or_sheet=1,
    )
    b = PIIRecord(
        record_id="b",
        entity_type="EMAIL",
        normalized_value="johnsmith@gmail.com",
        raw_name="Smith, John",
        raw_email="johnsmith@gmail.com",
        raw_phone=None,
        country="US",
        source_document_id="doc2",
        page_or_sheet=1,
    )
    c = PIIRecord(
        record_id="c",
        entity_type="US_SSN",
        normalized_value="123-45-6789",
        raw_name="J. Smith",
        raw_email=None,
        raw_phone="+12125551234",
        country="US",
        source_document_id="doc3",
        page_or_sheet=1,
    )

    subjects = _run_pipeline([a, b, c], db_session)

    assert len(subjects) == 1
    ns = subjects[0]
    assert len(ns.source_records) == 3
    assert "US_SSN" in ns.pii_types_found
    assert "EMAIL" in ns.pii_types_found
    # Deduplicator stores the raw (lowercased) email; the dot-variant
    # is longer so it wins the _best_value tiebreak.
    assert ns.canonical_email == "john.smith@gmail.com"
    assert ns.canonical_phone == "+12125551234"
    assert ns.notification_required is False
    assert ns.review_status in ("AI_PENDING", "HUMAN_REVIEW")


# ===========================================================================
# Scenario 2 — Two distinct people, no overlap
# ===========================================================================

def test_two_distinct_people(db_session):
    """Two records with entirely different PII → two separate subjects."""
    d = PIIRecord(
        record_id="d",
        entity_type="US_SSN",
        normalized_value="111-11-1111",
        raw_name="Alice Brown",
        raw_email="alice@example.com",
        country="US",
        source_document_id="doc1",
        page_or_sheet=1,
    )
    e = PIIRecord(
        record_id="e",
        entity_type="US_SSN",
        normalized_value="222-22-2222",
        raw_name="Bob Jones",
        raw_email="bob@example.com",
        country="US",
        source_document_id="doc2",
        page_or_sheet=1,
    )

    subjects = _run_pipeline([d, e], db_session)

    assert len(subjects) == 2
    emails = {ns.canonical_email for ns in subjects}
    assert emails == {"alice@example.com", "bob@example.com"}


# ===========================================================================
# Scenario 3 — Multi-geography, Indian Aadhaar records
# ===========================================================================

def test_indian_aadhaar_records_merged(db_session):
    """Two IN_AADHAAR records for the same person.

    Signals: gov-ID match (+0.50) + phone match (+0.35) = 0.85.
    Honorific 'Smt.' is stripped by normalize_name in the deduplicator.
    """
    f = PIIRecord(
        record_id="f",
        entity_type="IN_AADHAAR",
        normalized_value="2345 6789 0123",
        raw_name="Priya Sharma",
        raw_phone="+919876543210",
        country="IN",
        source_document_id="doc4",
        page_or_sheet=1,
    )
    g = PIIRecord(
        record_id="g",
        entity_type="IN_AADHAAR",
        normalized_value="2345 6789 0123",
        raw_name="Smt. Priya Sharma",
        raw_phone="+919876543210",
        country="IN",
        source_document_id="doc5",
        page_or_sheet=1,
    )

    subjects = _run_pipeline([f, g], db_session)

    assert len(subjects) == 1
    ns = subjects[0]
    assert ns.canonical_name == "Priya Sharma"
    assert len(ns.source_records) == 2


# ===========================================================================
# Scenario 4 — Cross-country records merged via email
# ===========================================================================

def test_cross_country_email_match(db_session):
    """Same email across US and IN records → merged (email is country-agnostic).

    Addresses have different countries so address matching returns False,
    but email match (+0.40) is sufficient to link the records.
    """
    h = PIIRecord(
        record_id="h",
        entity_type="EMAIL",
        normalized_value="test@example.com",
        raw_name="Test User",
        raw_email="test@example.com",
        raw_address={
            "street": "123 main st",
            "city": "new york",
            "state": "NY",
            "zip": "10001",
            "country": "US",
        },
        country="US",
        source_document_id="doc6",
        page_or_sheet=1,
    )
    i = PIIRecord(
        record_id="i",
        entity_type="EMAIL",
        normalized_value="test@example.com",
        raw_name="Test User",
        raw_email="test@example.com",
        raw_address={
            "street": "456 mg road",
            "city": "mumbai",
            "state": "MH",
            "zip": "10001",
            "country": "IN",
        },
        country="IN",
        source_document_id="doc7",
        page_or_sheet=1,
    )

    subjects = _run_pipeline([h, i], db_session)

    # Email match overrides country difference
    assert len(subjects) == 1
    assert subjects[0].canonical_email == "test@example.com"
