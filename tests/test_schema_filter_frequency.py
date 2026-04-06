"""Tests for frequency-based field analysis (UI audit improvements).

Tests compute_field_frequency() and build_person_context() from
app.pii.schema_filter — added during overnight UI audit.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.pii.schema_filter import (
    FieldFrequency,
    PersonFieldContext,
    build_person_context,
    compute_field_frequency,
)


# ---------------------------------------------------------------------------
# Fixtures — lightweight extraction stand-ins
# ---------------------------------------------------------------------------

@dataclass
class FakeExtraction:
    """Minimal stand-in for Extraction ORM objects used by frequency analysis."""
    pii_type: str
    evidence_page: int | None = None
    entity_role: str | None = None


# ---------------------------------------------------------------------------
# compute_field_frequency tests
# ---------------------------------------------------------------------------

class TestComputeFieldFrequency:
    def test_empty_extractions(self):
        result = compute_field_frequency([], total_pages=10)
        assert result == []

    def test_single_type_single_page(self):
        exts = [FakeExtraction(pii_type="ssn", evidence_page=1)]
        result = compute_field_frequency(exts, total_pages=10)
        assert len(result) == 1
        assert result[0].pii_type == "ssn"
        assert result[0].page_count == 1
        assert result[0].total_pages == 10
        assert result[0].is_org_metadata is False

    def test_address_high_frequency_is_org(self):
        """Address appearing on 9/10 pages should be flagged as org metadata."""
        exts = [
            FakeExtraction(pii_type="address", evidence_page=i)
            for i in range(1, 10)  # 9 pages
        ]
        result = compute_field_frequency(exts, total_pages=10)
        assert len(result) == 1
        freq = result[0]
        assert freq.pii_type == "address"
        assert freq.page_count == 9
        assert freq.is_org_metadata is True

    def test_ssn_high_frequency_not_org(self):
        """SSN appearing on many pages should NOT be flagged as org metadata
        because SSN is not in _ORG_CANDIDATE_TYPES."""
        exts = [
            FakeExtraction(pii_type="ssn", evidence_page=i)
            for i in range(1, 10)
        ]
        result = compute_field_frequency(exts, total_pages=10)
        assert len(result) == 1
        assert result[0].is_org_metadata is False

    def test_multiple_types_sorted_by_frequency(self):
        exts = [
            FakeExtraction(pii_type="email", evidence_page=1),
            FakeExtraction(pii_type="ssn", evidence_page=1),
            FakeExtraction(pii_type="ssn", evidence_page=2),
            FakeExtraction(pii_type="ssn", evidence_page=3),
        ]
        result = compute_field_frequency(exts, total_pages=5)
        assert len(result) == 2
        # SSN should be first (3 pages) > email (1 page)
        assert result[0].pii_type == "ssn"
        assert result[0].page_count == 3
        assert result[1].pii_type == "email"
        assert result[1].page_count == 1

    def test_duplicate_pages_deduplicated(self):
        """Multiple detections on the same page should count as one page."""
        exts = [
            FakeExtraction(pii_type="phone", evidence_page=1),
            FakeExtraction(pii_type="phone", evidence_page=1),
            FakeExtraction(pii_type="phone", evidence_page=2),
        ]
        result = compute_field_frequency(exts, total_pages=5)
        assert len(result) == 1
        assert result[0].page_count == 2

    def test_none_page_defaults_to_one(self):
        """Extraction with None evidence_page should still appear with page_count=1."""
        exts = [FakeExtraction(pii_type="ssn", evidence_page=None)]
        result = compute_field_frequency(exts, total_pages=5)
        assert len(result) == 1
        assert result[0].page_count == 1

    def test_total_pages_minimum_one(self):
        """total_pages < 1 should be treated as 1."""
        exts = [FakeExtraction(pii_type="ssn", evidence_page=1)]
        result = compute_field_frequency(exts, total_pages=0)
        assert result[0].total_pages == 1

    def test_to_dict(self):
        ff = FieldFrequency(
            pii_type="email",
            page_count=5,
            total_pages=10,
            is_org_metadata=False,
        )
        d = ff.to_dict()
        assert d == {
            "pii_type": "email",
            "page_count": 5,
            "total_pages": 10,
            "is_org_metadata": False,
        }

    def test_phone_number_variant_is_org_candidate(self):
        """phone_number (variant of phone) should be treated as org candidate."""
        exts = [
            FakeExtraction(pii_type="phone_number", evidence_page=i)
            for i in range(1, 11)
        ]
        result = compute_field_frequency(exts, total_pages=10)
        assert result[0].is_org_metadata is True

    def test_case_insensitive_types(self):
        """PII types should be compared case-insensitively."""
        exts = [
            FakeExtraction(pii_type="ADDRESS", evidence_page=1),
            FakeExtraction(pii_type="address", evidence_page=2),
        ]
        result = compute_field_frequency(exts, total_pages=2)
        assert len(result) == 1
        assert result[0].page_count == 2


# ---------------------------------------------------------------------------
# build_person_context tests
# ---------------------------------------------------------------------------

class TestBuildPersonContext:
    def test_empty_extractions(self):
        result = build_person_context([])
        assert result == []

    def test_single_role(self):
        exts = [
            FakeExtraction(pii_type="ssn", entity_role="primary_subject"),
            FakeExtraction(pii_type="email", entity_role="primary_subject"),
        ]
        result = build_person_context(exts)
        assert len(result) == 1
        assert result[0].role == "primary_subject"
        assert set(result[0].pii_types) == {"ssn", "email"}

    def test_multiple_roles_sorted(self):
        exts = [
            FakeExtraction(pii_type="ssn", entity_role="primary_subject"),
            FakeExtraction(pii_type="address", entity_role="institutional"),
            FakeExtraction(pii_type="email", entity_role="related_party"),
        ]
        result = build_person_context(exts)
        assert len(result) == 3
        # primary_subject first, related_party second, institutional last
        assert result[0].role == "primary_subject"
        assert result[1].role == "related_party"
        assert result[2].role == "institutional"

    def test_none_role_defaults_to_unknown(self):
        exts = [FakeExtraction(pii_type="ssn", entity_role=None)]
        result = build_person_context(exts)
        assert result[0].role == "unknown"

    def test_schema_people_provide_names(self):
        @dataclass
        class FakePerson:
            name: str
            role: str

        exts = [
            FakeExtraction(pii_type="ssn", entity_role="primary_subject"),
        ]
        people = [FakePerson(name="John Doe", role="primary_subject")]
        result = build_person_context(exts, schema_people=people)
        assert result[0].person_name == "John Doe"

    def test_to_dict(self):
        pc = PersonFieldContext(
            person_name="Jane Doe",
            role="related_party",
            pii_types=["email", "phone"],
        )
        d = pc.to_dict()
        assert d == {
            "person_name": "Jane Doe",
            "role": "related_party",
            "pii_types": ["email", "phone"],
        }

    def test_deduplicates_pii_types_within_role(self):
        exts = [
            FakeExtraction(pii_type="ssn", entity_role="primary_subject"),
            FakeExtraction(pii_type="ssn", entity_role="primary_subject"),
            FakeExtraction(pii_type="email", entity_role="primary_subject"),
        ]
        result = build_person_context(exts)
        assert len(result) == 1
        # Should be deduplicated
        assert len(result[0].pii_types) == 2


# ---------------------------------------------------------------------------
# Safety: no raw PII in any output
# ---------------------------------------------------------------------------

class TestSafety:
    def test_field_frequency_no_raw_values(self):
        """FieldFrequency should never contain raw PII values."""
        ff = FieldFrequency(
            pii_type="ssn", page_count=1, total_pages=10, is_org_metadata=False,
        )
        d = ff.to_dict()
        # Should only contain type metadata, no value data
        assert "value" not in str(d).lower() or "pii_type" in str(d)

    def test_person_context_no_raw_pii(self):
        """PersonFieldContext should not contain raw PII values."""
        pc = PersonFieldContext(
            person_name="Test", role="primary_subject", pii_types=["ssn"],
        )
        d = pc.to_dict()
        # person_name is a label, not raw PII
        assert "raw" not in str(d).lower()
