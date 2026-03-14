"""Tests for Step 20 Part 2 — pattern validation + per-protocol model config."""
from __future__ import annotations

from uuid import uuid4

import pytest

from app.pii.pattern_validator import (
    DATE_PATTERNS,
    VALIDATION_PATTERNS,
    validate_extracted_records,
)
from app.rra.entity_resolver import PIIRecord


def _make_record(**kwargs) -> PIIRecord:
    """Helper to create a PIIRecord with defaults."""
    defaults = {
        "record_id": str(uuid4()),
        "entity_type": "PERSON",
        "normalized_value": kwargs.get("raw_name", "Test Person"),
        "raw_name": "Test Person",
        "source_document_id": "doc-1",
        "page_or_sheet": 0,
    }
    defaults.update(kwargs)
    return PIIRecord(**defaults)


# ---------------------------------------------------------------------------
# Government ID validation
# ---------------------------------------------------------------------------


class TestGovIdValidation:
    def test_valid_ni_number_no_flags(self):
        rec = _make_record(
            raw_government_id="NE724362D",
            entity_types_found=("NI_NUMBER", "PERSON"),
        )
        result = validate_extracted_records([rec])
        assert len(result) == 1
        assert "gov_id_format_mismatch:NI_NUMBER" not in result[0].validation_flags

    def test_invalid_ni_number_flagged(self):
        rec = _make_record(
            raw_government_id="INVALID",
            entity_types_found=("NI_NUMBER", "PERSON"),
        )
        result = validate_extracted_records([rec])
        assert len(result) == 1
        assert "gov_id_format_mismatch:NI_NUMBER" in result[0].validation_flags

    def test_valid_us_ssn_no_flags(self):
        rec = _make_record(
            raw_government_id="123-45-6789",
            entity_types_found=("US_SSN",),
        )
        result = validate_extracted_records([rec])
        assert len(result) == 1
        assert not any("gov_id" in f for f in result[0].validation_flags)

    def test_invalid_us_ssn_flagged(self):
        rec = _make_record(
            raw_government_id="12345",
            entity_types_found=("US_SSN",),
        )
        result = validate_extracted_records([rec])
        assert "gov_id_format_mismatch:US_SSN" in result[0].validation_flags

    def test_no_gov_id_type_no_flag(self):
        """Gov ID present but no matching type in entity_types_found → no flag."""
        rec = _make_record(
            raw_government_id="ABC123",
            entity_types_found=("PERSON",),
        )
        result = validate_extracted_records([rec])
        assert len(result) == 1
        assert not any("gov_id" in f for f in result[0].validation_flags)


# ---------------------------------------------------------------------------
# Date validation
# ---------------------------------------------------------------------------


class TestDateValidation:
    def test_valid_date_dmy_no_flags(self):
        rec = _make_record(raw_dob="10-Aug-1959")
        result = validate_extracted_records([rec])
        assert "dob_format_unrecognized" not in result[0].validation_flags

    def test_valid_date_slash_no_flags(self):
        rec = _make_record(raw_dob="10/08/1959")
        result = validate_extracted_records([rec])
        assert "dob_format_unrecognized" not in result[0].validation_flags

    def test_valid_date_iso_no_flags(self):
        rec = _make_record(raw_dob="1959-08-10")
        result = validate_extracted_records([rec])
        assert "dob_format_unrecognized" not in result[0].validation_flags

    def test_valid_date_long_no_flags(self):
        rec = _make_record(raw_dob="10 August 1959")
        result = validate_extracted_records([rec])
        assert "dob_format_unrecognized" not in result[0].validation_flags

    def test_invalid_date_flagged(self):
        rec = _make_record(raw_dob="not a date")
        result = validate_extracted_records([rec])
        assert "dob_format_unrecognized" in result[0].validation_flags


# ---------------------------------------------------------------------------
# Email validation
# ---------------------------------------------------------------------------


class TestEmailValidation:
    def test_valid_email_no_flags(self):
        rec = _make_record(raw_email="john@example.com")
        result = validate_extracted_records([rec])
        assert "email_format_invalid" not in result[0].validation_flags

    def test_invalid_email_flagged(self):
        rec = _make_record(raw_email="not-an-email")
        result = validate_extracted_records([rec])
        assert "email_format_invalid" in result[0].validation_flags


# ---------------------------------------------------------------------------
# Address validation
# ---------------------------------------------------------------------------


class TestAddressValidation:
    def test_full_address_no_flags(self):
        rec = _make_record(raw_address={"raw": "123 Main Street, London, EC1A 1BB"})
        result = validate_extracted_records([rec])
        assert "address_too_short" not in result[0].validation_flags

    def test_short_address_flagged(self):
        rec = _make_record(raw_address={"raw": "London"})
        result = validate_extracted_records([rec])
        assert "address_too_short" in result[0].validation_flags

    def test_short_address_not_suppressed(self):
        """Short address is flagged but record is kept."""
        rec = _make_record(raw_address={"raw": "London"})
        result = validate_extracted_records([rec])
        assert len(result) == 1  # record kept


# ---------------------------------------------------------------------------
# Name suppression
# ---------------------------------------------------------------------------


class TestNameSuppression:
    def test_financial_term_suppressed(self):
        rec = _make_record(raw_name="Lump Sum", normalized_value="Lump Sum")
        result = validate_extracted_records([rec])
        assert len(result) == 0  # suppressed

    def test_organization_name_suppressed(self):
        rec = _make_record(
            raw_name="William M Mercer Limited",
            normalized_value="William M Mercer Limited",
        )
        result = validate_extracted_records([rec])
        assert len(result) == 0

    def test_normal_name_kept(self):
        rec = _make_record(raw_name="Mr K P Acheampong")
        result = validate_extracted_records([rec])
        assert len(result) == 1
        assert result[0].raw_name == "Mr K P Acheampong"

    def test_pension_term_suppressed(self):
        rec = _make_record(raw_name="pension", normalized_value="pension")
        result = validate_extracted_records([rec])
        assert len(result) == 0

    def test_company_ltd_suppressed(self):
        rec = _make_record(raw_name="Acme Corp Ltd", normalized_value="Acme Corp Ltd")
        result = validate_extracted_records([rec])
        assert len(result) == 0


# ---------------------------------------------------------------------------
# Multiple flags
# ---------------------------------------------------------------------------


class TestMultipleFlags:
    def test_multiple_flags_on_one_record(self):
        """A record can have multiple validation flags."""
        rec = _make_record(
            raw_dob="not a date",
            raw_email="bad-email",
            raw_government_id="INVALID",
            entity_types_found=("NI_NUMBER",),
        )
        result = validate_extracted_records([rec])
        assert len(result) == 1
        flags = result[0].validation_flags
        assert "dob_format_unrecognized" in flags
        assert "email_format_invalid" in flags
        assert "gov_id_format_mismatch:NI_NUMBER" in flags

    def test_clean_record_no_flags(self):
        rec = _make_record(
            raw_name="Mr John Smith",
            raw_dob="15-Jan-1970",
            raw_email="john@example.com",
            raw_government_id="AB123456C",
            raw_address={"raw": "123 High Street, London, EC1A 1BB"},
            entity_types_found=("NI_NUMBER", "PERSON"),
        )
        result = validate_extracted_records([rec])
        assert len(result) == 1
        assert result[0].validation_flags == ()


# ---------------------------------------------------------------------------
# Protocol model config
# ---------------------------------------------------------------------------


class TestProtocolModelConfig:
    def test_all_protocols_have_vision_model(self):
        from app.core.constants import PROTOCOL_LLM_CONFIG
        for proto, cfg in PROTOCOL_LLM_CONFIG.items():
            assert "vision_model" in cfg, f"{proto} missing vision_model"
            assert "vision_page_dpi" in cfg, f"{proto} missing vision_page_dpi"

    def test_hipaa_vision_model(self):
        from app.core.constants import PROTOCOL_LLM_CONFIG
        assert PROTOCOL_LLM_CONFIG["hipaa"]["vision_model"] == "llama3.2-vision:latest"

    def test_gdpr_vision_model(self):
        from app.core.constants import PROTOCOL_LLM_CONFIG
        assert PROTOCOL_LLM_CONFIG["gdpr"]["vision_model"] == "llama3.2-vision:latest"

    def test_vision_dpi_defaults(self):
        from app.core.constants import PROTOCOL_LLM_CONFIG
        for proto, cfg in PROTOCOL_LLM_CONFIG.items():
            assert cfg["vision_page_dpi"] == 150, f"{proto} has non-standard DPI"


# ---------------------------------------------------------------------------
# Validation patterns themselves
# ---------------------------------------------------------------------------


class TestPatterns:
    def test_ni_number_pattern(self):
        assert VALIDATION_PATTERNS["NI_NUMBER"].match("NE724362D")
        assert VALIDATION_PATTERNS["NI_NUMBER"].match("AB123456C")
        assert not VALIDATION_PATTERNS["NI_NUMBER"].match("123456")
        assert not VALIDATION_PATTERNS["NI_NUMBER"].match("ABCDEFGHI")

    def test_us_ssn_pattern(self):
        assert VALIDATION_PATTERNS["US_SSN"].match("123-45-6789")
        assert not VALIDATION_PATTERNS["US_SSN"].match("123456789")

    def test_date_patterns(self):
        # All 4 formats
        assert any(p.search("10-Aug-1959") for p in DATE_PATTERNS)
        assert any(p.search("10/08/1959") for p in DATE_PATTERNS)
        assert any(p.search("1959-08-10") for p in DATE_PATTERNS)
        assert any(p.search("10 August 1959") for p in DATE_PATTERNS)
        # Not a date
        assert not any(p.search("hello world") for p in DATE_PATTERNS)


# ---------------------------------------------------------------------------
# PIIRecord validation_flags field
# ---------------------------------------------------------------------------


class TestPIIRecordField:
    def test_validation_flags_default_empty(self):
        rec = _make_record()
        assert rec.validation_flags == ()

    def test_validation_flags_preserved(self):
        rec = _make_record(validation_flags=("test_flag",))
        assert rec.validation_flags == ("test_flag",)
