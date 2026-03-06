"""Tests for Phase 14a: context deny-list and false positive reduction.

Covers:
- COMMON_WORD_DENY_LIST entries suppress STUDENT_ID/VAT_EU matches
- REFERENCE_LABELS suppress DRIVER_LICENSE/COMPANY_NUMBER matches
- Real PII (actual SSNs, real driver licenses) still passes through
- Tightened regex patterns in layer1_patterns.py
- Regression test: simulate Boosey & Hawkes detections, verify FP reduction
"""
from __future__ import annotations

import re

import pytest

from app.pii.context_deny_list import (
    COMMON_WORD_DENY_LIST,
    REFERENCE_LABELS,
    is_likely_false_positive,
)
from app.pii.layer1_patterns import CUSTOM_PATTERNS, PatternDefinition


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_pattern(entity_type: str) -> PatternDefinition:
    """Return the first PatternDefinition matching entity_type."""
    for p in CUSTOM_PATTERNS:
        if p.entity_type == entity_type:
            return p
    raise ValueError(f"No pattern for {entity_type}")


def _matches(pattern: PatternDefinition, text: str) -> list[str]:
    """Return all matches of pattern.regex in text."""
    return [m.group() for m in re.finditer(pattern.regex, text)]


# =========================================================================
# Test COMMON_WORD_DENY_LIST
# =========================================================================


class TestCommonWordDenyList:
    """COMMON_WORD_DENY_LIST should suppress common English words."""

    def test_deny_list_is_nonempty(self):
        assert len(COMMON_WORD_DENY_LIST) >= 20

    def test_statement_in_deny_list(self):
        assert "statement" in COMMON_WORD_DENY_LIST

    def test_description_in_deny_list(self):
        assert "description" in COMMON_WORD_DENY_LIST

    def test_transactions_in_deny_list(self):
        assert "transactions" in COMMON_WORD_DENY_LIST

    def test_street_in_deny_list(self):
        assert "street" in COMMON_WORD_DENY_LIST

    def test_summary_in_deny_list(self):
        assert "summary" in COMMON_WORD_DENY_LIST

    def test_real_student_id_not_in_deny_list(self):
        assert "stu12345" not in COMMON_WORD_DENY_LIST


# =========================================================================
# Test REFERENCE_LABELS
# =========================================================================


class TestReferenceLabels:
    """REFERENCE_LABELS should contain common reference field labels."""

    def test_ref_in_labels(self):
        assert "ref" in REFERENCE_LABELS

    def test_statement_nr_in_labels(self):
        assert "statement nr" in REFERENCE_LABELS

    def test_client_in_labels(self):
        assert "client" in REFERENCE_LABELS

    def test_account_no_in_labels(self):
        assert "account no" in REFERENCE_LABELS

    def test_invoice_no_in_labels(self):
        assert "invoice no" in REFERENCE_LABELS


# =========================================================================
# Test is_likely_false_positive — STUDENT_ID
# =========================================================================


class TestStudentIdFP:
    """STUDENT_ID false positive detection."""

    def test_statement_is_fp(self):
        is_fp, reason = is_likely_false_positive("Statement", "STUDENT_ID", "Account Statement for period")
        assert is_fp
        assert "deny_list" in reason or "no_digit" in reason

    def test_summary_is_fp(self):
        is_fp, reason = is_likely_false_positive("SUMMARY", "STUDENT_ID", "Account Summary")
        assert is_fp

    def test_street_is_fp(self):
        is_fp, reason = is_likely_false_positive("STREET", "STUDENT_ID", "123 Main STREET")
        assert is_fp

    def test_no_digits_is_fp(self):
        is_fp, reason = is_likely_false_positive("SABCDEFGH", "STUDENT_ID", "some text")
        assert is_fp
        assert "no_digit" in reason

    def test_real_student_id_passes(self):
        is_fp, _ = is_likely_false_positive("STU12345", "STUDENT_ID", "Student ID: STU12345")
        assert not is_fp

    def test_real_sid_passes(self):
        is_fp, _ = is_likely_false_positive("SID-0099", "STUDENT_ID", "Student SID-0099 enrolled")
        assert not is_fp


# =========================================================================
# Test is_likely_false_positive — VAT_EU
# =========================================================================


class TestVatEuFP:
    """VAT_EU false positive detection."""

    def test_description_is_fp(self):
        is_fp, reason = is_likely_false_positive("DESCRIPTION", "VAT_EU", "Column: DESCRIPTION")
        assert is_fp

    def test_transactions_is_fp(self):
        is_fp, reason = is_likely_false_positive("TRANSACTIONS", "VAT_EU", "Recent TRANSACTIONS")
        assert is_fp

    def test_all_alpha_is_fp(self):
        is_fp, reason = is_likely_false_positive("ABCDEFGHIJ", "VAT_EU", "text ABCDEFGHIJ more")
        assert is_fp
        assert "all_alpha" in reason

    def test_real_vat_passes(self):
        is_fp, _ = is_likely_false_positive("GB123456789", "VAT_EU", "VAT No: GB123456789")
        assert not is_fp

    def test_real_de_vat_passes(self):
        is_fp, _ = is_likely_false_positive("DE987654321", "VAT_EU", "VAT: DE987654321")
        assert not is_fp


# =========================================================================
# Test is_likely_false_positive — COMPANY_NUMBER_UK
# =========================================================================


class TestCompanyNumberFP:
    """COMPANY_NUMBER_UK false positive detection."""

    def test_short_number_near_reference_label_is_fp(self):
        is_fp, reason = is_likely_false_positive(
            "001968", "COMPANY_NUMBER_UK",
            "Client: 001968 some more text after",
        )
        assert is_fp

    def test_short_number_no_company_context_is_fp(self):
        is_fp, reason = is_likely_false_positive(
            "001968", "COMPANY_NUMBER_UK",
            "Statement for 001968 quarterly report",
        )
        assert is_fp
        assert "company_number_short" in reason

    def test_8_digit_number_passes(self):
        """8-digit bare numeric should pass (correct UK format)."""
        is_fp, _ = is_likely_false_positive(
            "12345678", "COMPANY_NUMBER_UK",
            "Registration 12345678 confirmed",
        )
        assert not is_fp

    def test_short_with_company_context_passes(self):
        is_fp, _ = is_likely_false_positive(
            "001968", "COMPANY_NUMBER_UK",
            "Company registration no. 001968 at Companies House",
        )
        assert not is_fp


# =========================================================================
# Test is_likely_false_positive — DRIVER_LICENSE_US
# =========================================================================


class TestDriverLicenseFP:
    """DRIVER_LICENSE_US false positive detection."""

    def test_number_without_license_context_is_fp(self):
        is_fp, reason = is_likely_false_positive(
            "1121799", "DRIVER_LICENSE_US",
            "Statement Nr.: 1121799 for account",
        )
        assert is_fp

    def test_near_reference_label_is_fp(self):
        is_fp, reason = is_likely_false_positive(
            "001967", "DRIVER_LICENSE_US",
            "Ref. 001967 Heirs Transfer",
        )
        assert is_fp

    def test_real_license_with_context_passes(self):
        is_fp, _ = is_likely_false_positive(
            "D1234567", "DRIVER_LICENSE_US",
            "Driver License No: D1234567 State: NY",
        )
        assert not is_fp

    def test_dl_abbreviation_context_passes(self):
        is_fp, _ = is_likely_false_positive(
            "AB123456", "DRIVER_LICENSE_US",
            "DL# AB123456 issued 2020",
        )
        assert not is_fp


# =========================================================================
# Test is_likely_false_positive — DATE_OF_BIRTH
# =========================================================================


class TestDateOfBirthFP:
    """DATE_OF_BIRTH false positive detection."""

    def test_transaction_date_is_fp(self):
        is_fp, reason = is_likely_false_positive(
            "30/06/2020", "DATE_OF_BIRTH_DMY",
            "Transaction date: 30/06/2020 Amount: 65.29",
        )
        assert is_fp
        assert "transactional" in reason

    def test_statement_period_date_is_fp(self):
        is_fp, reason = is_likely_false_positive(
            "01/01/2020", "DATE_OF_BIRTH_DMY",
            "Statement period from 01/01/2020 to 30/06/2020",
        )
        assert is_fp

    def test_date_without_any_context_is_fp(self):
        is_fp, reason = is_likely_false_positive(
            "15/03/1985", "DATE_OF_BIRTH_DMY",
            "some random text 15/03/1985 more text",
        )
        assert is_fp
        assert "no_dob_context" in reason

    def test_real_dob_with_context_passes(self):
        is_fp, _ = is_likely_false_positive(
            "15/03/1985", "DATE_OF_BIRTH_DMY",
            "Date of Birth: 15/03/1985 Gender: F",
        )
        assert not is_fp

    def test_dob_abbreviation_passes(self):
        is_fp, _ = is_likely_false_positive(
            "03/15/1985", "DATE_OF_BIRTH_MDY",
            "DOB: 03/15/1985 Patient Name:",
        )
        assert not is_fp

    def test_dob_with_both_contexts_passes(self):
        """If DOB context is present, even transaction keywords don't suppress."""
        is_fp, _ = is_likely_false_positive(
            "15/03/1985", "DATE_OF_BIRTH_DMY",
            "Date of Birth: 15/03/1985 from previous statement",
        )
        assert not is_fp


# =========================================================================
# Test non-FP-prone entity types pass through
# =========================================================================


class TestPassthrough:
    """Entity types not in _FP_PRONE_ENTITY_TYPES should always pass."""

    def test_ssn_passes(self):
        is_fp, _ = is_likely_false_positive("123-45-6789", "SSN", "SSN: 123-45-6789")
        assert not is_fp

    def test_email_passes(self):
        is_fp, _ = is_likely_false_positive("test@example.com", "EMAIL", "email test@example.com")
        assert not is_fp

    def test_phone_passes(self):
        is_fp, _ = is_likely_false_positive("(555) 123-4567", "PHONE_US", "Phone: (555) 123-4567")
        assert not is_fp

    def test_iban_passes(self):
        is_fp, _ = is_likely_false_positive("GB82WEST12345698765432", "IBAN", "IBAN: GB82WEST12345698765432")
        assert not is_fp

    def test_person_passes(self):
        is_fp, _ = is_likely_false_positive("John Smith", "PERSON", "Name: John Smith")
        assert not is_fp


# =========================================================================
# Test tightened regex patterns
# =========================================================================


class TestTightenedPatterns:
    """Verify tightened regex patterns in layer1_patterns.py."""

    def test_student_id_rejects_statement(self):
        """'Statement' should NOT match tightened STUDENT_ID pattern."""
        pat = _get_pattern("STUDENT_ID")
        matches = _matches(pat, "Account Statement for client")
        assert "Statement" not in matches

    def test_student_id_rejects_summary(self):
        pat = _get_pattern("STUDENT_ID")
        matches = _matches(pat, "Account SUMMARY report")
        # SUMMARY starts with S but has no digits
        assert "SUMMARY" not in matches

    def test_student_id_accepts_real(self):
        pat = _get_pattern("STUDENT_ID")
        matches = _matches(pat, "Student STU12345 enrolled")
        assert "STU12345" in matches

    def test_student_id_accepts_sid_with_digits(self):
        pat = _get_pattern("STUDENT_ID")
        matches = _matches(pat, "Record SID-0099 found")
        assert "SID-0099" in matches

    def test_vat_eu_rejects_description(self):
        """All-alpha words should not match tightened VAT_EU pattern."""
        pat = _get_pattern("VAT_EU")
        matches = _matches(pat, "Column: DESCRIPTION of items")
        assert "DESCRIPTION" not in matches

    def test_vat_eu_rejects_transactions(self):
        pat = _get_pattern("VAT_EU")
        matches = _matches(pat, "Recent TRANSACTIONS logged")
        assert "TRANSACTIONS" not in matches

    def test_vat_eu_accepts_real(self):
        pat = _get_pattern("VAT_EU")
        matches = _matches(pat, "VAT: GB123456789")
        assert "GB123456789" in matches

    def test_company_number_uk_rejects_short(self):
        """Short bare numerics (< 8 digits) should not match."""
        pat = _get_pattern("COMPANY_NUMBER_UK")
        matches = _matches(pat, "Client: 001968 details")
        assert "001968" not in matches

    def test_company_number_uk_accepts_8_digits(self):
        pat = _get_pattern("COMPANY_NUMBER_UK")
        matches = _matches(pat, "Company 12345678 registered")
        assert "12345678" in matches

    def test_company_number_uk_accepts_prefixed(self):
        pat = _get_pattern("COMPANY_NUMBER_UK")
        matches = _matches(pat, "OC123456 is a valid company")
        assert "OC123456" in matches

    def test_driver_license_us_rejects_short(self):
        """6-digit bare numbers should not match tightened pattern."""
        pat = _get_pattern("DRIVER_LICENSE_US")
        matches = _matches(pat, "Ref: 001968 transfer")
        assert "001968" not in matches

    def test_driver_license_us_accepts_prefixed(self):
        pat = _get_pattern("DRIVER_LICENSE_US")
        matches = _matches(pat, "DL: D1234567 issued")
        assert "D1234567" in matches

    def test_driver_license_us_accepts_7_digits(self):
        pat = _get_pattern("DRIVER_LICENSE_US")
        matches = _matches(pat, "License 1234567 state")
        assert "1234567" in matches


# =========================================================================
# Boosey & Hawkes regression test
# =========================================================================


class TestBooseyHawkesRegression:
    """Simulate the Boosey & Hawkes financial statement FP scenario.

    The original document produced 38 detections with ~85% FP rate.
    Phase 14a deny-lists + tightened patterns should suppress most.
    """

    # Simulated detections from the financial statement
    SIMULATED_DETECTIONS = [
        # Real PII — should NOT be suppressed
        ("285-07-5085", "SSN", "Tax No.: 285-07-5085 Client: Adeline"),
        ("ADELINE CHANDLER", "PERSON", "Name: ADELINE CHANDLER Address:"),

        # False positives — SHOULD be suppressed
        ("001968", "COMPANY_NUMBER_UK", "Client: 001968 quarterly statement"),
        ("1121799", "DRIVER_LICENSE_US", "Statement Nr.: 1121799 report"),
        ("001967", "DRIVER_LICENSE_US", "Ref. 001967 Heirs Transfer from"),
        ("Statement", "STUDENT_ID", "Account Statement for period"),
        ("Summary", "STUDENT_ID", "Royalty Summary for client"),
        ("STREET", "STUDENT_ID", "48TH STREET NEW YORK NY"),
        ("30/06/2020", "DATE_OF_BIRTH_DMY", "Transaction date 30/06/2020 Amount"),
        ("CLIFFORD BARNES", "ORGANIZATION", "Transfer from CLIFFORD BARNES 65.29"),
        # Additional common FPs from financial docs
        ("Description", "STUDENT_ID", "Column: Description Amount"),
    ]

    def test_real_ssn_not_suppressed(self):
        is_fp, _ = is_likely_false_positive(
            "285-07-5085", "SSN",
            "Tax No.: 285-07-5085 Client: Adeline",
        )
        assert not is_fp

    def test_real_person_not_suppressed(self):
        is_fp, _ = is_likely_false_positive(
            "ADELINE CHANDLER", "PERSON",
            "Name: ADELINE CHANDLER Address:",
        )
        assert not is_fp

    def test_client_number_suppressed(self):
        is_fp, _ = is_likely_false_positive(
            "001968", "COMPANY_NUMBER_UK",
            "Client: 001968 quarterly statement",
        )
        assert is_fp

    def test_statement_nr_suppressed(self):
        is_fp, _ = is_likely_false_positive(
            "1121799", "DRIVER_LICENSE_US",
            "Statement Nr.: 1121799 report for period",
        )
        assert is_fp

    def test_ref_number_suppressed(self):
        is_fp, _ = is_likely_false_positive(
            "001967", "DRIVER_LICENSE_US",
            "Ref. 001967 Heirs Transfer from CLIFFORD BARNES",
        )
        assert is_fp

    def test_statement_word_suppressed(self):
        is_fp, _ = is_likely_false_positive(
            "Statement", "STUDENT_ID",
            "Account Statement for period ending",
        )
        assert is_fp

    def test_summary_word_suppressed(self):
        is_fp, _ = is_likely_false_positive(
            "Summary", "STUDENT_ID",
            "Royalty Summary for client Adeline",
        )
        assert is_fp

    def test_street_word_suppressed(self):
        is_fp, _ = is_likely_false_positive(
            "STREET", "STUDENT_ID",
            "48TH STREET NEW YORK NY 10036",
        )
        assert is_fp

    def test_transaction_date_suppressed(self):
        is_fp, _ = is_likely_false_positive(
            "30/06/2020", "DATE_OF_BIRTH_DMY",
            "Transaction date 30/06/2020 Amount: 65.29 CR",
        )
        assert is_fp

    def test_fp_reduction_count(self):
        """Overall FP reduction: most false positives are caught."""
        suppressed = 0
        kept = 0
        for text, entity_type, context in self.SIMULATED_DETECTIONS:
            is_fp, _ = is_likely_false_positive(text, entity_type, context)
            if is_fp:
                suppressed += 1
            else:
                kept += 1

        # Real PII (SSN, PERSON, ORGANIZATION) should be kept
        assert kept >= 2, f"Too many real detections suppressed: kept={kept}"
        # Most false positives should be caught
        assert suppressed >= 7, f"Not enough FPs caught: suppressed={suppressed}"
        # NOTE: ORGANIZATION is NOT an FP-prone type so CLIFFORD BARNES passes
        # through. That's correct — reclassification (ORG → PERSON) is Phase 14b.
