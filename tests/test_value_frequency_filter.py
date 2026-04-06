"""Tests for value-frequency filtering and label deny-list (Overnight Pipeline Phase 3).

Validates:
- ValueFrequencyFilter identifies high-frequency values as org metadata
- is_blank_or_placeholder catches blanks, underscores, dashes
- is_email_sender_context detects sender-role PII
- is_label_as_person catches label misclassification
- STRICT storage: no raw PII in any assertion message
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# ValueFrequencyFilter
# ---------------------------------------------------------------------------

class TestValueFrequencyFilter:
    """Test frequency-based suppression of organizational metadata."""

    def _make_extraction(self, pii_type, detected_text, page, hashed_value=None):
        ext = MagicMock()
        ext.pii_type = pii_type
        ext.detected_text = detected_text
        ext.evidence_page = page
        ext.hashed_value = hashed_value or detected_text.strip().lower()
        return ext

    def test_high_frequency_phone_flagged(self):
        """Phone appearing on >80% of pages is flagged as org metadata."""
        from app.pii.schema_filter import ValueFrequencyFilter

        # 10-page doc, phone on 9 pages
        extractions = [
            self._make_extraction("PHONE_NUMBER", "(555) 100-2000", page)
            for page in range(1, 10)
        ]
        # Add a different phone on 1 page (should NOT be flagged)
        extractions.append(self._make_extraction("PHONE_NUMBER", "(555) 999-8888", 5))

        vff = ValueFrequencyFilter.from_extractions(extractions, total_pages=10)

        is_org, reason = vff.is_org_metadata("(555) 100-2000", "PHONE_NUMBER")
        assert is_org, "High-frequency phone should be flagged"
        assert "high_frequency_value" in reason

        is_org2, _ = vff.is_org_metadata("(555) 999-8888", "PHONE_NUMBER")
        assert not is_org2, "Low-frequency phone should not be flagged"

    def test_high_frequency_email_flagged(self):
        """Email appearing on >80% of pages is flagged."""
        from app.pii.schema_filter import ValueFrequencyFilter

        extractions = [
            self._make_extraction("EMAIL_ADDRESS", "support@example.com", page)
            for page in range(1, 9)
        ]
        vff = ValueFrequencyFilter.from_extractions(extractions, total_pages=10)

        is_org, _ = vff.is_org_metadata("support@example.com", "EMAIL_ADDRESS")
        assert is_org

    def test_below_threshold_not_flagged(self):
        """Value on only 70% of pages should not be flagged."""
        from app.pii.schema_filter import ValueFrequencyFilter

        extractions = [
            self._make_extraction("PHONE_NUMBER", "(555) 100-2000", page)
            for page in range(1, 8)  # 7 out of 10 = 70%
        ]
        vff = ValueFrequencyFilter.from_extractions(extractions, total_pages=10)

        is_org, _ = vff.is_org_metadata("(555) 100-2000", "PHONE_NUMBER")
        assert not is_org, "70% frequency should not trigger suppression"

    def test_too_few_pages_skipped(self):
        """Documents with < 3 pages skip frequency analysis entirely."""
        from app.pii.schema_filter import ValueFrequencyFilter

        extractions = [
            self._make_extraction("PHONE_NUMBER", "(555) 100-2000", 1),
            self._make_extraction("PHONE_NUMBER", "(555) 100-2000", 2),
        ]
        vff = ValueFrequencyFilter.from_extractions(extractions, total_pages=2)

        is_org, _ = vff.is_org_metadata("(555) 100-2000", "PHONE_NUMBER")
        assert not is_org, "Should not flag on 2-page documents"

    def test_non_eligible_type_not_flagged(self):
        """US_SSN should never be flagged as org metadata regardless of frequency."""
        from app.pii.schema_filter import ValueFrequencyFilter

        extractions = [
            self._make_extraction("US_SSN", "123-45-6789", page)
            for page in range(1, 10)
        ]
        vff = ValueFrequencyFilter.from_extractions(extractions, total_pages=10)

        is_org, _ = vff.is_org_metadata("123-45-6789", "US_SSN")
        assert not is_org, "SSN should never be flagged as org metadata"

    def test_flagged_values_property(self):
        """flagged_values returns all identified high-frequency values."""
        from app.pii.schema_filter import ValueFrequencyFilter

        extractions = [
            self._make_extraction("PHONE_NUMBER", "(555) 100-2000", page)
            for page in range(1, 10)
        ]
        vff = ValueFrequencyFilter.from_extractions(extractions, total_pages=10)

        flagged = vff.flagged_values
        assert len(flagged) >= 1
        assert flagged[0].pii_type == "PHONE_NUMBER"
        assert flagged[0].frequency >= 0.80

    def test_high_frequency_location_flagged(self):
        """Address appearing on >80% of pages is flagged."""
        from app.pii.schema_filter import ValueFrequencyFilter

        extractions = [
            self._make_extraction("LOCATION", "123 Main St, Anytown, US 12345", page)
            for page in range(1, 10)
        ]
        vff = ValueFrequencyFilter.from_extractions(extractions, total_pages=10)

        is_org, _ = vff.is_org_metadata("123 Main St, Anytown, US 12345", "LOCATION")
        assert is_org


# ---------------------------------------------------------------------------
# Blank / placeholder detection
# ---------------------------------------------------------------------------

class TestBlankPlaceholder:
    """Test is_blank_or_placeholder function."""

    def test_underscores(self):
        from app.pii.schema_filter import is_blank_or_placeholder
        assert is_blank_or_placeholder("____")
        assert is_blank_or_placeholder("_ _ _ _")

    def test_empty(self):
        from app.pii.schema_filter import is_blank_or_placeholder
        assert is_blank_or_placeholder("")
        assert is_blank_or_placeholder("   ")

    def test_dashes(self):
        from app.pii.schema_filter import is_blank_or_placeholder
        assert is_blank_or_placeholder("----")
        assert is_blank_or_placeholder("- - - -")

    def test_dots(self):
        from app.pii.schema_filter import is_blank_or_placeholder
        assert is_blank_or_placeholder(".....")
        assert is_blank_or_placeholder("***")

    def test_real_name_not_blank(self):
        from app.pii.schema_filter import is_blank_or_placeholder
        assert not is_blank_or_placeholder("John Smith")
        assert not is_blank_or_placeholder("123 Main St")


# ---------------------------------------------------------------------------
# Email sender context detection
# ---------------------------------------------------------------------------

class TestEmailSenderContext:
    """Test is_email_sender_context function."""

    def test_from_label(self):
        from app.pii.context_deny_list import is_email_sender_context
        is_sender, reason = is_email_sender_context(
            "John Doe", "PERSON",
            "From: John Doe <john@example.com>"
        )
        assert is_sender
        assert "email_sender_context" in reason

    def test_regards_signature(self):
        from app.pii.context_deny_list import is_email_sender_context
        is_sender, _ = is_email_sender_context(
            "Jane Smith", "PERSON",
            "Best regards, Jane Smith\nVP Operations"
        )
        assert is_sender

    def test_non_sender_person(self):
        from app.pii.context_deny_list import is_email_sender_context
        is_sender, _ = is_email_sender_context(
            "Patient Zero", "PERSON",
            "The records for Patient Zero show elevated markers"
        )
        assert not is_sender

    def test_non_person_type_ignored(self):
        from app.pii.context_deny_list import is_email_sender_context
        is_sender, _ = is_email_sender_context(
            "123-45-6789", "US_SSN",
            "From: admin SSN 123-45-6789"
        )
        assert not is_sender, "SSN should not be affected by sender context"


# ---------------------------------------------------------------------------
# Label-as-PERSON detection
# ---------------------------------------------------------------------------

class TestLabelAsPerson:
    """Test is_label_as_person function."""

    def test_drug_test_rejected(self):
        from app.pii.context_deny_list import is_label_as_person
        is_label, reason = is_label_as_person("Drug Test")
        assert is_label
        assert "label_as_person" in reason

    def test_group_rochester_rejected(self):
        from app.pii.context_deny_list import is_label_as_person
        is_label, _ = is_label_as_person("Group Rochester")
        assert is_label

    def test_real_name_not_rejected(self):
        from app.pii.context_deny_list import is_label_as_person
        is_label, _ = is_label_as_person("Karen Craft")
        assert not is_label

    def test_case_insensitive(self):
        from app.pii.context_deny_list import is_label_as_person
        is_label, _ = is_label_as_person("DRUG TEST")
        assert is_label


# ---------------------------------------------------------------------------
# No raw PII in audit logs (safety test)
# ---------------------------------------------------------------------------

class TestSafety:
    """Ensure no raw PII leaks through filter mechanisms."""

    def test_high_frequency_value_masked_in_dict(self):
        """HighFrequencyValue.to_dict() masks the raw value."""
        from app.pii.schema_filter import HighFrequencyValue

        hfv = HighFrequencyValue(
            value="(555) 100-2000",
            pii_type="PHONE_NUMBER",
            page_count=9,
            total_pages=10,
            frequency=0.9,
        )
        d = hfv.to_dict()
        # Should be masked — not contain the full phone number
        assert d["value_masked"] != "(555) 100-2000"
        assert "***" in d["value_masked"]
