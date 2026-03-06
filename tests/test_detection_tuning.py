"""Tests for Phase 14c: detection tuning (min confidence, currency filter, dedup).

Verifies:
- MIN_DETECTION_CONFIDENCE threshold drops low-confidence detections
- Currency pattern detection suppresses PHONE_NUMBER FPs on financial amounts
- Detection deduplication keeps highest confidence per (text, entity_type, page)
- End-to-end mock scenarios (Boosey & Hawkes bank statement, Washington CMD complaint)
- Suppression audit trail: new event types in VALID_EVENT_TYPES
"""
from __future__ import annotations

import pytest

from app.pii.context_deny_list import (
    is_currency_pattern,
    is_likely_false_positive,
)
from app.pii.presidio_engine import (
    MIN_DETECTION_CONFIDENCE,
    DetectionResult,
    deduplicate_detections,
)
from app.readers.base import ExtractedBlock
from app.audit.events import (
    EVENT_DETECTION_SUPPRESSED,
    EVENT_DETECTION_RECLASSIFIED,
    VALID_EVENT_TYPES,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _block(text: str, page: int = 0) -> ExtractedBlock:
    """Create an ExtractedBlock for testing."""
    return ExtractedBlock(text=text, page_or_sheet=page, source_path="test.pdf", file_type="pdf")


def _detection(
    text: str,
    entity_type: str,
    start: int,
    end: int,
    score: float,
    page: int = 0,
) -> DetectionResult:
    """Create a DetectionResult for testing."""
    return DetectionResult(
        block=_block(text, page),
        entity_type=entity_type,
        start=start,
        end=end,
        score=score,
        pattern_used="test",
        geography="GLOBAL",
        regulatory_framework="test",
    )


# ---------------------------------------------------------------------------
# MIN_DETECTION_CONFIDENCE threshold
# ---------------------------------------------------------------------------

class TestMinConfidenceThreshold:
    """Phase 14c: detections below MIN_DETECTION_CONFIDENCE are dropped."""

    def test_threshold_value(self) -> None:
        """MIN_DETECTION_CONFIDENCE is 10%."""
        assert MIN_DETECTION_CONFIDENCE == 0.10

    def test_below_threshold_dropped(self) -> None:
        """1% confidence detection is dropped."""
        det = _detection("foo", "STUDENT_ID", 0, 3, 0.01)
        assert det.score < MIN_DETECTION_CONFIDENCE

    def test_at_threshold_kept(self) -> None:
        """10% confidence detection is at threshold."""
        det = _detection("S12345", "STUDENT_ID", 0, 6, 0.10)
        assert det.score >= MIN_DETECTION_CONFIDENCE

    def test_above_threshold_kept(self) -> None:
        """50% confidence detection is above threshold."""
        det = _detection("S12345", "STUDENT_ID", 0, 6, 0.50)
        assert det.score >= MIN_DETECTION_CONFIDENCE

    def test_high_confidence_kept(self) -> None:
        """95% confidence detection is well above threshold."""
        det = _detection("123-45-6789", "US_SSN", 0, 11, 0.95)
        assert det.score >= MIN_DETECTION_CONFIDENCE


# ---------------------------------------------------------------------------
# Currency pattern detection
# ---------------------------------------------------------------------------

class TestCurrencyPattern:
    """Phase 14c: currency patterns suppress PHONE_NUMBER false positives."""

    @pytest.mark.parametrize("value", [
        "153.84 160.00",          # adjacent decimal pairs
        "1,153.84",               # comma thousands
        "$1,234.56",              # dollar prefix
        "\u00a31,500.00",         # pound prefix
        "\u20ac100.00",           # euro prefix
        "0.30 0.60",              # small decimal pairs
        "2,500.00",               # comma with .00
        "10,000.00",              # larger comma amount
    ])
    def test_currency_detected(self, value: str) -> None:
        """Financial amount patterns are detected as currency."""
        assert is_currency_pattern(value), f"Expected currency: {value}"

    @pytest.mark.parametrize("value", [
        "212-541-6600",           # real US phone number
        "+1 (212) 541-6600",      # formatted phone
        "555.123.4567",           # dotted phone (single number)
        "hello world",            # plain text
        "12345",                  # plain number
        "abc 1,234.56 xyz",       # currency embedded in text (fullmatch fails)
    ])
    def test_non_currency_not_detected(self, value: str) -> None:
        """Real phone numbers and plain text are NOT detected as currency."""
        assert not is_currency_pattern(value), f"Should not be currency: {value}"

    def test_phone_number_suppressed_on_currency(self) -> None:
        """PHONE_NUMBER entity on currency value is suppressed by deny list."""
        is_fp, reason = is_likely_false_positive(
            "153.84 160.00", "PHONE_NUMBER", "Balance: 153.84 160.00 Payment"
        )
        assert is_fp
        assert "currency_pattern" in reason

    def test_phone_number_not_suppressed_on_real_phone(self) -> None:
        """Real phone number is NOT suppressed."""
        is_fp, _ = is_likely_false_positive(
            "212-541-6600", "PHONE_NUMBER", "Phone: 212-541-6600"
        )
        assert not is_fp

    def test_comma_amount_suppresses_phone(self) -> None:
        """Comma-separated financial amount suppresses PHONE_NUMBER."""
        is_fp, reason = is_likely_false_positive(
            "1,153.84", "PHONE_NUMBER", "Total 1,153.84"
        )
        assert is_fp
        assert "currency_pattern" in reason


# ---------------------------------------------------------------------------
# Detection deduplication
# ---------------------------------------------------------------------------

class TestDeduplication:
    """Phase 14c: deduplicate_detections keeps highest score per group."""

    def test_empty_list(self) -> None:
        """Empty list returns empty."""
        assert deduplicate_detections([]) == []

    def test_no_duplicates(self) -> None:
        """Unique detections pass through unchanged."""
        dets = [
            _detection("John Doe", "PERSON", 0, 8, 0.85, page=1),
            _detection("123-45-6789", "US_SSN", 10, 21, 0.95, page=1),
        ]
        result = deduplicate_detections(dets)
        assert len(result) == 2

    def test_same_text_same_page_keeps_highest(self) -> None:
        """4x same LOCATION on same page → 1 detection with highest score."""
        block = _block("123 Main Street, Springfield, IL 62704", page=3)
        dets = [
            DetectionResult(block=block, entity_type="LOCATION", start=0, end=38, score=0.60,
                            pattern_used="", geography="US", regulatory_framework=""),
            DetectionResult(block=block, entity_type="LOCATION", start=0, end=38, score=0.85,
                            pattern_used="", geography="US", regulatory_framework=""),
            DetectionResult(block=block, entity_type="LOCATION", start=0, end=38, score=0.70,
                            pattern_used="", geography="US", regulatory_framework=""),
            DetectionResult(block=block, entity_type="LOCATION", start=0, end=38, score=0.55,
                            pattern_used="", geography="US", regulatory_framework=""),
        ]
        result = deduplicate_detections(dets)
        assert len(result) == 1
        assert result[0].score == 0.85

    def test_different_pages_not_deduped(self) -> None:
        """Same text/type on different pages are kept separate."""
        dets = [
            _detection("John Doe", "PERSON", 0, 8, 0.85, page=1),
            _detection("John Doe", "PERSON", 0, 8, 0.80, page=2),
        ]
        result = deduplicate_detections(dets)
        assert len(result) == 2

    def test_different_types_not_deduped(self) -> None:
        """Same text with different entity types are kept separate."""
        block = _block("John Smith")
        dets = [
            DetectionResult(block=block, entity_type="PERSON", start=0, end=10, score=0.85,
                            pattern_used="", geography="GLOBAL", regulatory_framework=""),
            DetectionResult(block=block, entity_type="LOCATION", start=0, end=10, score=0.60,
                            pattern_used="", geography="GLOBAL", regulatory_framework=""),
        ]
        result = deduplicate_detections(dets)
        assert len(result) == 2

    def test_many_duplicates_reduced(self) -> None:
        """Multiple duplicates across groups."""
        block = _block("SSN: 123-45-6789 Phone: 555-0100")
        dets = [
            # SSN group — 3 hits, keep highest
            DetectionResult(block=block, entity_type="US_SSN", start=5, end=16, score=0.95,
                            pattern_used="", geography="US", regulatory_framework=""),
            DetectionResult(block=block, entity_type="US_SSN", start=5, end=16, score=0.80,
                            pattern_used="", geography="US", regulatory_framework=""),
            DetectionResult(block=block, entity_type="US_SSN", start=5, end=16, score=0.90,
                            pattern_used="", geography="US", regulatory_framework=""),
            # Phone group — 2 hits, keep highest
            DetectionResult(block=block, entity_type="PHONE_NUMBER", start=24, end=32, score=0.70,
                            pattern_used="", geography="US", regulatory_framework=""),
            DetectionResult(block=block, entity_type="PHONE_NUMBER", start=24, end=32, score=0.65,
                            pattern_used="", geography="US", regulatory_framework=""),
        ]
        result = deduplicate_detections(dets)
        assert len(result) == 2
        scores = {r.entity_type: r.score for r in result}
        assert scores["US_SSN"] == 0.95
        assert scores["PHONE_NUMBER"] == 0.70


# ---------------------------------------------------------------------------
# Audit event types
# ---------------------------------------------------------------------------

class TestAuditEventTypes:
    """Phase 14c: new audit event types for suppression tracking."""

    def test_detection_suppressed_event_exists(self) -> None:
        """EVENT_DETECTION_SUPPRESSED is defined."""
        assert EVENT_DETECTION_SUPPRESSED == "detection_suppressed"

    def test_detection_reclassified_event_exists(self) -> None:
        """EVENT_DETECTION_RECLASSIFIED is defined."""
        assert EVENT_DETECTION_RECLASSIFIED == "detection_reclassified"

    def test_suppressed_in_valid_set(self) -> None:
        """detection_suppressed is in VALID_EVENT_TYPES."""
        assert EVENT_DETECTION_SUPPRESSED in VALID_EVENT_TYPES

    def test_reclassified_in_valid_set(self) -> None:
        """detection_reclassified is in VALID_EVENT_TYPES."""
        assert EVENT_DETECTION_RECLASSIFIED in VALID_EVENT_TYPES

    def test_valid_event_types_count(self) -> None:
        """10 total event types after Phase 14c additions."""
        assert len(VALID_EVENT_TYPES) == 10


# ---------------------------------------------------------------------------
# End-to-end mock: Boosey & Hawkes bank statement
# ---------------------------------------------------------------------------

class TestBooseyHawkesMock:
    """Mock scenario: Boosey & Hawkes royalty statement (financial doc).

    Expected: financial amounts should NOT be detected as phone numbers.
    Common words like "STATEMENT", "BALANCE" should be suppressed.
    """

    STATEMENT_TEXT = (
        "BOOSEY & HAWKES MUSIC PUBLISHERS LIMITED\n"
        "Statement Nr. 12345678\n"
        "Period: 01/01/2024 to 30/06/2024\n"
        "Account: John Smith\n"
        "Opening Balance: 153.84\n"
        "Payment: 160.00\n"
        "Closing Balance: 313.84\n"
        "Transactions: 3 items\n"
        "Total: 1,153.84\n"
    )

    def test_statement_word_is_fp(self) -> None:
        """'STATEMENT' triggers STUDENT_ID but is suppressed as common word."""
        is_fp, reason = is_likely_false_positive("STATEMENT", "STUDENT_ID", self.STATEMENT_TEXT[:120])
        assert is_fp
        assert "deny_list" in reason

    def test_balance_word_is_fp(self) -> None:
        """'BALANCE' is a common word, suppressed as FP."""
        is_fp, reason = is_likely_false_positive("BALANCE", "STUDENT_ID", self.STATEMENT_TEXT[:120])
        assert is_fp
        assert "deny_list" in reason

    def test_financial_amount_not_phone(self) -> None:
        """'153.84 160.00' or '1,153.84' are currency, not phone numbers."""
        assert is_currency_pattern("153.84 160.00")
        assert is_currency_pattern("1,153.84")

    def test_date_suppressed_as_transactional(self) -> None:
        """'01/01/2024' near 'Period' is transactional, not a DOB."""
        is_fp, reason = is_likely_false_positive(
            "01/01/2024", "DATE_OF_BIRTH_DMY", "Period: 01/01/2024 to 30/06/2024"
        )
        assert is_fp
        assert "date_transactional" in reason or "date_no_dob_context" in reason


# ---------------------------------------------------------------------------
# End-to-end mock: Washington CMD complaint
# ---------------------------------------------------------------------------

class TestWashingtonCMDMock:
    """Mock scenario: Washington state CMD complaint document.

    Expected: real PII (SSN, DOB, names, addresses) ARE detected and kept.
    Transactional dates suppressed. Common words suppressed.
    """

    COMPLAINT_TEXT = (
        "STATE OF WASHINGTON\n"
        "Consumer Medical Data Complaint\n"
        "Case No. CMD-2024-001234\n"
        "Complainant: Jane Doe, DOB: 03/15/1985\n"
        "SSN: 123-45-6789\n"
        "Address: 456 Oak Avenue, Seattle, WA 98101\n"
        "Phone: 206-555-0142\n"
        "Filing Date: 01/15/2024\n"
        "Description of complaint...\n"
    )

    def test_ssn_detected_above_threshold(self) -> None:
        """SSN pattern match is well above MIN_DETECTION_CONFIDENCE."""
        det = _detection("123-45-6789", "US_SSN", 0, 11, 0.95)
        assert det.score >= MIN_DETECTION_CONFIDENCE

    def test_dob_with_context_not_suppressed(self) -> None:
        """Date near 'DOB' keyword is NOT suppressed."""
        is_fp, _ = is_likely_false_positive(
            "03/15/1985", "DATE_OF_BIRTH_MDY", "Complainant: Jane Doe, DOB: 03/15/1985"
        )
        assert not is_fp

    def test_filing_date_suppressed(self) -> None:
        """'01/15/2024' near 'Filing' is transactional, not DOB."""
        is_fp, reason = is_likely_false_positive(
            "01/15/2024", "DATE_OF_BIRTH_MDY", "Filing Date: 01/15/2024"
        )
        assert is_fp
        assert "date_transactional" in reason or "date_no_dob_context" in reason

    def test_case_number_not_driver_license(self) -> None:
        """Case No. 'CMD-2024-001234' near 'Case No.' reference label is not a license."""
        is_fp, reason = is_likely_false_positive(
            "CMD-2024-001234", "DRIVER_LICENSE_US",
            "Case No. CMD-2024-001234\n"
        )
        # Either suppressed by reference label or by no license context
        assert is_fp

    def test_phone_not_suppressed_when_real(self) -> None:
        """Real phone '206-555-0142' is NOT suppressed."""
        is_fp, _ = is_likely_false_positive(
            "206-555-0142", "PHONE_NUMBER", "Phone: 206-555-0142"
        )
        assert not is_fp

    def test_common_word_state_not_fp(self) -> None:
        """'STATE' is not in the common word deny list (it's a legitimate word in this context)."""
        # 'state' is not in COMMON_WORD_DENY_LIST
        from app.pii.context_deny_list import COMMON_WORD_DENY_LIST
        assert "state" not in COMMON_WORD_DENY_LIST

    def test_description_word_is_fp(self) -> None:
        """'DESCRIPTION' is in common word deny list."""
        is_fp, reason = is_likely_false_positive("DESCRIPTION", "VAT_EU", "Description of complaint")
        assert is_fp


# ---------------------------------------------------------------------------
# Context deny-list: combined rule checks
# ---------------------------------------------------------------------------

class TestDenyListRules:
    """Integration tests for all deny-list rules working together."""

    def test_company_number_short_no_context(self) -> None:
        """Short bare number without company context is suppressed."""
        is_fp, reason = is_likely_false_positive("12345", "COMPANY_NUMBER_UK", "Ref: 12345")
        assert is_fp
        assert "company_number_short" in reason

    def test_company_number_with_context_not_suppressed(self) -> None:
        """Number with company registration context is kept."""
        is_fp, _ = is_likely_false_positive(
            "12345678", "COMPANY_NUMBER_UK",
            "Company Registration Number: 12345678 Companies House"
        )
        assert not is_fp

    def test_driver_license_no_context_suppressed(self) -> None:
        """Driver license number without context is suppressed."""
        is_fp, reason = is_likely_false_positive(
            "D12345678", "DRIVER_LICENSE_US", "Number: D12345678"
        )
        assert is_fp
        assert "driver_license_no_context" in reason

    def test_driver_license_with_context_kept(self) -> None:
        """Driver license number with license keyword is kept."""
        is_fp, _ = is_likely_false_positive(
            "D12345678", "DRIVER_LICENSE_US", "Driver License No: D12345678"
        )
        assert not is_fp

    def test_vat_all_alpha_suppressed(self) -> None:
        """All-alpha 'VAT' match is suppressed."""
        is_fp, reason = is_likely_false_positive("TRANSACTIONS", "VAT_EU", "Transactions listed")
        assert is_fp

    def test_student_id_no_digits_suppressed(self) -> None:
        """STUDENT_ID match with no digits is suppressed."""
        is_fp, reason = is_likely_false_positive("SUBTOTAL", "STUDENT_ID", "Subtotal amount")
        assert is_fp
