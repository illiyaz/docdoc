"""Tests for Step 17 — financial false positive suppression and cross-type suppression."""
from __future__ import annotations

import pytest
from dataclasses import dataclass

from app.pii.context_deny_list import (
    FINANCIAL_TERM_DENY_LIST,
    cross_type_suppression,
    is_likely_false_positive,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@dataclass
class _FakeBlock:
    text: str
    page_or_sheet: int = 0
    source_path: str = ""
    file_type: str = "pdf"


@dataclass
class _FakeDetection:
    entity_type: str
    start: int
    end: int
    score: float
    block: _FakeBlock


def _det(entity_type: str, text: str, start: int = 0, end: int | None = None, score: float = 0.85) -> _FakeDetection:
    if end is None:
        end = len(text)
    return _FakeDetection(
        entity_type=entity_type,
        start=start,
        end=end,
        score=score,
        block=_FakeBlock(text=text),
    )


# ---------------------------------------------------------------------------
# FINANCIAL_TERM_DENY_LIST
# ---------------------------------------------------------------------------

class TestFinancialTermDenyList:
    def test_deny_list_has_common_terms(self):
        assert "lump sum" in FINANCIAL_TERM_DENY_LIST
        assert "transfer value" in FINANCIAL_TERM_DENY_LIST
        assert "pension" in FINANCIAL_TERM_DENY_LIST
        assert "premium" in FINANCIAL_TERM_DENY_LIST
        assert "annuity" in FINANCIAL_TERM_DENY_LIST
        assert "remuneration" in FINANCIAL_TERM_DENY_LIST
        assert "protected rights" in FINANCIAL_TERM_DENY_LIST

    def test_lump_sum_suppressed_as_person(self):
        is_fp, reason = is_likely_false_positive("Lump Sum", "PERSON", "context text")
        assert is_fp is True
        assert "financial_term" in reason

    def test_transfer_value_suppressed_as_person(self):
        is_fp, reason = is_likely_false_positive("Transfer Value", "PERSON", "context")
        assert is_fp is True
        assert "financial_term" in reason

    def test_pension_suppressed_as_person(self):
        is_fp, reason = is_likely_false_positive("Pension", "PERSON", "context")
        assert is_fp is True

    def test_real_name_not_suppressed(self):
        is_fp, _ = is_likely_false_positive("K P Acheampong", "PERSON", "Member Name: K P Acheampong")
        assert is_fp is False

    def test_financial_term_case_insensitive(self):
        is_fp, _ = is_likely_false_positive("LUMP SUM", "PERSON", "context")
        assert is_fp is True

    def test_financial_term_not_suppressed_for_non_person(self):
        """Financial term deny-list only applies to PERSON entity type."""
        is_fp, _ = is_likely_false_positive("Lump Sum", "ORGANIZATION", "context")
        assert is_fp is False


# ---------------------------------------------------------------------------
# cross_type_suppression
# ---------------------------------------------------------------------------

class TestCrossTypeSuppression:
    def test_person_vs_location_keeps_location(self):
        """Same text detected as PERSON and LOCATION — keep LOCATION by default."""
        dets = [
            _det("PERSON", "Harrow Weald", start=0, end=12),
            _det("LOCATION", "Harrow Weald", start=0, end=12),
        ]
        result = cross_type_suppression(dets)
        types = [d.entity_type for d in result]
        assert "LOCATION" in types
        assert "PERSON" not in types

    def test_titled_name_keeps_person(self):
        """Name with title (Mr) should keep PERSON over LOCATION."""
        dets = [
            _det("PERSON", "Mr Smith", start=0, end=8),
            _det("LOCATION", "Mr Smith", start=0, end=8),
        ]
        result = cross_type_suppression(dets)
        types = [d.entity_type for d in result]
        assert "PERSON" in types
        # LOCATION should be suppressed when keeping PERSON
        assert "LOCATION" not in types

    def test_text_with_digits_suppresses_person(self):
        """Text with digits → suppress PERSON."""
        dets = [
            _det("PERSON", "NW2 3UD", start=0, end=7),
            _det("LOCATION", "NW2 3UD", start=0, end=7),
        ]
        result = cross_type_suppression(dets)
        types = [d.entity_type for d in result]
        assert "LOCATION" in types
        assert "PERSON" not in types

    def test_financial_term_suppresses_person(self):
        """Financial term → suppress PERSON."""
        dets = [
            _det("PERSON", "Pension", start=0, end=7),
            _det("ORGANIZATION", "Pension", start=0, end=7),
        ]
        result = cross_type_suppression(dets)
        types = [d.entity_type for d in result]
        assert "PERSON" not in types

    def test_no_conflict_keeps_all(self):
        """Non-conflicting detections at different positions — keep all."""
        dets = [
            _det("PERSON", "K P Acheampong", start=0, end=14),
            _det("LOCATION", "85 Waltings Gardens", start=20, end=39),
        ]
        result = cross_type_suppression(dets)
        assert len(result) == 2

    def test_single_detection_unchanged(self):
        """Single detection per span — keep it."""
        dets = [_det("PERSON", "John Smith", start=0, end=10)]
        result = cross_type_suppression(dets)
        assert len(result) == 1
        assert result[0].entity_type == "PERSON"

    def test_empty_input(self):
        assert cross_type_suppression([]) == []

    def test_person_vs_organization_keeps_org(self):
        """PERSON vs ORGANIZATION — keep ORGANIZATION by default."""
        dets = [
            _det("PERSON", "Buckman", start=0, end=7),
            _det("ORGANIZATION", "Buckman", start=0, end=7),
        ]
        result = cross_type_suppression(dets)
        types = [d.entity_type for d in result]
        assert "ORGANIZATION" in types
        assert "PERSON" not in types

    def test_mrs_title_keeps_person(self):
        """Mrs title → keep PERSON."""
        dets = [
            _det("PERSON", "Mrs Johnson", start=0, end=11),
            _det("LOCATION", "Mrs Johnson", start=0, end=11),
        ]
        result = cross_type_suppression(dets)
        types = [d.entity_type for d in result]
        assert "PERSON" in types

    def test_dr_title_keeps_person(self):
        """Dr title → keep PERSON."""
        dets = [
            _det("PERSON", "Dr Patel", start=0, end=8),
            _det("LOCATION", "Dr Patel", start=0, end=8),
        ]
        result = cross_type_suppression(dets)
        types = [d.entity_type for d in result]
        assert "PERSON" in types

    def test_multiple_non_conflicting_spans(self):
        """Multiple spans, no conflicts within any span."""
        dets = [
            _det("PERSON", "John Smith", start=0, end=10),
            _det("EMAIL_ADDRESS", "john@test.com", start=20, end=33),
            _det("PHONE_NUMBER", "020 7123 4567", start=40, end=53),
        ]
        result = cross_type_suppression(dets)
        assert len(result) == 3
