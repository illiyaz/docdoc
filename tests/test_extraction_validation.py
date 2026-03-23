"""Tests for extraction validation: DOB context, email URL rejection,
entity_types_found accuracy, and pattern_validator enhancements.
"""
from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from app.pii.pattern_validator import (
    _build_entity_types_found,
    _looks_like_business,
    validate_dob,
    validate_email,
    validate_extracted_records,
    validate_person_name,
)
from app.rra.entity_resolver import PIIRecord


# ---------------------------------------------------------------------------
# validate_dob() — transaction date rejection
# ---------------------------------------------------------------------------


class TestValidateDob:
    """Tests for DOB context validation."""

    def test_transaction_date_fee_slip(self):
        """Fee Slip Dated 04/05/2022 → NOT a DOB."""
        page_text = "Fee Slip #194654 Dated 04/05/2022\nKaren Craft"
        assert validate_dob("04/05/2022", page_text) is False

    def test_statement_date_rejected(self):
        """Statement Date: 05/04/2022 → NOT a DOB."""
        page_text = "Statement Date: 05/04/2022\nAccount Summary"
        assert validate_dob("05/04/2022", page_text) is False

    def test_service_date_rejected(self):
        """Service Date 01/15/2023 → NOT a DOB."""
        page_text = "Service Date 01/15/2023\nPatient: John Doe"
        assert validate_dob("01/15/2023", page_text) is False

    def test_invoice_date_rejected(self):
        """Invoice Date: 03/01/2024 → NOT a DOB."""
        page_text = "Invoice Date: 03/01/2024\nBill To: Alice"
        assert validate_dob("03/01/2024", page_text) is False

    def test_due_date_rejected(self):
        """Due Date 12/31/2023 → NOT a DOB."""
        page_text = "Payment Due Date 12/31/2023\nAmount: $500"
        assert validate_dob("12/31/2023", page_text) is False

    def test_real_dob_with_label(self):
        """Date of Birth: 03/15/1985 → IS a DOB."""
        page_text = "Patient Name: Karen Craft\nDate of Birth: 03/15/1985"
        assert validate_dob("03/15/1985", page_text) is True

    def test_dob_label_accepted(self):
        """DOB: 10-Aug-1959 → IS a DOB."""
        page_text = "Member Details\nDOB: 10-Aug-1959\nAddress: 123 Main St"
        assert validate_dob("10-Aug-1959", page_text) is True

    def test_born_label_accepted(self):
        """Born: 1990-01-01 → IS a DOB."""
        page_text = "Name: Jane Doe\nBorn: 1990-01-01"
        assert validate_dob("1990-01-01", page_text) is True

    def test_recent_date_rejected_without_context(self):
        """A date from last year with no context → NOT a DOB (too recent)."""
        assert validate_dob("01/01/2025") is False

    def test_old_date_accepted_without_context(self):
        """A date from 1985 with no context → plausible DOB."""
        assert validate_dob("03/15/1985") is True

    def test_empty_date_rejected(self):
        assert validate_dob("") is False
        assert validate_dob("", "some text") is False

    def test_transaction_keyword_elsewhere_on_page(self):
        """Transaction date keyword on page but no DOB label → rejected."""
        page_text = (
            "BILLING STATEMENT\n"
            "Statement Date: 01/01/2024\n"
            "Patient: John\n"
            "Some other date: 06/15/1970"
        )
        assert validate_dob("06/15/1970", page_text) is False

    def test_dob_label_overrides_transaction_keywords(self):
        """If both DOB label and transaction keywords exist, DOB label wins."""
        page_text = (
            "Statement Date: 01/01/2024\n"
            "Patient: John\n"
            "Date of Birth: 06/15/1970"
        )
        assert validate_dob("06/15/1970", page_text) is True


# ---------------------------------------------------------------------------
# validate_email() — URL and invalid email rejection
# ---------------------------------------------------------------------------


class TestValidateEmail:
    """Tests for email validation."""

    def test_valid_email(self):
        assert validate_email("user@example.com") is True

    def test_url_rejected(self):
        assert validate_email("www.candrvision.com") is False

    def test_https_url_rejected(self):
        assert validate_email("https://example.com") is False

    def test_http_url_rejected(self):
        assert validate_email("http://example.com") is False

    def test_no_at_sign_rejected(self):
        assert validate_email("not-an-email") is False

    def test_empty_rejected(self):
        assert validate_email("") is False

    def test_none_rejected(self):
        # validate_email expects str but should handle gracefully
        assert validate_email("") is False

    def test_valid_with_subdomain(self):
        assert validate_email("user@mail.example.co.uk") is True


# ---------------------------------------------------------------------------
# entity_types_found accuracy
# ---------------------------------------------------------------------------


class TestBuildEntityTypesFound:
    """Test _build_entity_types_found only includes types with values."""

    def test_only_name(self):
        types = _build_entity_types_found(
            raw_name="John", raw_address=None, raw_dob=None,
            raw_government_id=None, raw_email=None, raw_phone=None,
            original_types=(),
        )
        assert types == ("PERSON",)

    def test_name_and_address(self):
        types = _build_entity_types_found(
            raw_name="John", raw_address={"raw": "123 Main St"},
            raw_dob=None, raw_government_id=None, raw_email=None,
            raw_phone=None, original_types=(),
        )
        assert set(types) == {"PERSON", "LOCATION"}

    def test_all_fields(self):
        types = _build_entity_types_found(
            raw_name="John", raw_address={"raw": "123 Main St"},
            raw_dob="1985-01-01", raw_government_id="123-45-6789",
            raw_email="j@test.com", raw_phone="555-1234",
            original_types=("US_SSN",),
        )
        assert "PERSON" in types
        assert "LOCATION" in types
        assert "DATE_OF_BIRTH" in types
        assert "US_SSN" in types
        assert "EMAIL_ADDRESS" in types
        assert "PHONE_NUMBER" in types

    def test_gov_id_preserves_specific_type(self):
        types = _build_entity_types_found(
            raw_name="John", raw_address=None, raw_dob=None,
            raw_government_id="AB123456C", raw_email=None, raw_phone=None,
            original_types=("NI_NUMBER", "PERSON"),
        )
        assert "NI_NUMBER" in types
        assert "GOVERNMENT_ID" not in types

    def test_gov_id_falls_back_to_generic(self):
        types = _build_entity_types_found(
            raw_name="John", raw_address=None, raw_dob=None,
            raw_government_id="12345", raw_email=None, raw_phone=None,
            original_types=("PERSON",),
        )
        assert "GOVERNMENT_ID" in types

    def test_null_dob_not_in_types(self):
        """If DOB is None (e.g., rejected by validate_dob), not in types."""
        types = _build_entity_types_found(
            raw_name="John", raw_address=None, raw_dob=None,
            raw_government_id=None, raw_email=None, raw_phone=None,
            original_types=("DATE_OF_BIRTH", "PERSON"),
        )
        assert "DATE_OF_BIRTH" not in types

    def test_null_email_not_in_types(self):
        """If email is None (e.g., rejected URL), not in types."""
        types = _build_entity_types_found(
            raw_name="John", raw_address=None, raw_dob=None,
            raw_government_id=None, raw_email=None, raw_phone=None,
            original_types=("EMAIL_ADDRESS", "PERSON"),
        )
        assert "EMAIL_ADDRESS" not in types

    def test_empty_address_not_in_types(self):
        """Empty address dict → not in types."""
        types = _build_entity_types_found(
            raw_name="John", raw_address={"raw": ""},
            raw_dob=None, raw_government_id=None, raw_email=None,
            raw_phone=None, original_types=(),
        )
        assert "LOCATION" not in types


# ---------------------------------------------------------------------------
# validate_extracted_records — DOB/email rejection + entity_types_found
# ---------------------------------------------------------------------------


class TestValidateExtractedRecordsEnhanced:
    """Tests for validate_extracted_records with DOB/email fixes."""

    @staticmethod
    def _make_record(**kwargs) -> PIIRecord:
        defaults = dict(
            record_id="r1",
            entity_type="PERSON",
            normalized_value="Karen Craft",
            raw_name="Karen Craft",
            entity_types_found=("DATE_OF_BIRTH", "EMAIL_ADDRESS",
                                "LOCATION", "NI_NUMBER", "PERSON",
                                "PHONE_NUMBER"),
        )
        defaults.update(kwargs)
        return PIIRecord(**defaults)

    def test_transaction_date_stripped_from_record(self):
        """DOB that is actually a transaction date → raw_dob becomes None."""
        rec = self._make_record(raw_dob="04/05/2022")
        result = validate_extracted_records([rec])
        assert len(result) == 1
        assert result[0].raw_dob is None
        assert "dob_likely_transaction_date" in result[0].validation_flags

    def test_real_dob_preserved(self):
        """A plausible DOB from 1985 → preserved."""
        rec = self._make_record(raw_dob="03/15/1985")
        result = validate_extracted_records([rec])
        assert len(result) == 1
        assert result[0].raw_dob == "03/15/1985"

    def test_url_email_stripped(self):
        """URL masquerading as email → raw_email becomes None."""
        rec = self._make_record(raw_email="www.candrvision.com")
        result = validate_extracted_records([rec])
        assert len(result) == 1
        assert result[0].raw_email is None
        assert "email_format_invalid" in result[0].validation_flags

    def test_valid_email_preserved(self):
        """Real email → preserved."""
        rec = self._make_record(raw_email="karen@example.com")
        result = validate_extracted_records([rec])
        assert len(result) == 1
        assert result[0].raw_email == "karen@example.com"

    def test_entity_types_found_cleaned(self):
        """entity_types_found only includes types with non-null values."""
        rec = self._make_record(
            raw_dob="04/05/2022",    # will be rejected (recent)
            raw_email="www.test.com",  # will be rejected (URL)
            raw_phone=None,
            raw_address={"raw": "123 Main St"},
        )
        result = validate_extracted_records([rec])
        assert len(result) == 1
        types = result[0].entity_types_found
        # Should have PERSON and LOCATION
        assert "PERSON" in types
        assert "LOCATION" in types
        # Should NOT have rejected types
        assert "DATE_OF_BIRTH" not in types
        assert "EMAIL_ADDRESS" not in types
        assert "PHONE_NUMBER" not in types
        # NI_NUMBER should not be present (no raw_government_id set)
        assert "NI_NUMBER" not in types


# ---------------------------------------------------------------------------
# Vision extractor _data_to_record validation
# ---------------------------------------------------------------------------


class TestVisionDataToRecordValidation:
    """Tests that VisionDocumentExtractor._data_to_record validates fields."""

    def test_transaction_date_not_mapped_as_dob(self):
        """Vision model returns a recent date → not mapped to raw_dob."""
        from app.structure.vision_extractor import VisionDocumentExtractor
        from app.llm.client import OllamaClient

        mock_client = MagicMock(spec=OllamaClient)
        ext = VisionDocumentExtractor(mock_client)

        data = {
            "PERSON": "Karen Craft",
            "LOCATION": "18 High Point TRL, Fairport, NY",
            "DATE_OF_BIRTH": "04/05/2022",  # transaction date
        }
        rec = ext._data_to_record(data, "doc-1", [0])
        assert rec is not None
        assert rec.raw_name == "Karen Craft"
        assert rec.raw_dob is None  # rejected
        assert "DATE_OF_BIRTH" not in rec.entity_types_found

    def test_real_dob_mapped(self):
        """Vision model returns a plausible DOB → mapped correctly."""
        from app.structure.vision_extractor import VisionDocumentExtractor
        from app.llm.client import OllamaClient

        mock_client = MagicMock(spec=OllamaClient)
        ext = VisionDocumentExtractor(mock_client)

        data = {
            "PERSON": "Karen Craft",
            "DATE_OF_BIRTH": "03/15/1985",
        }
        rec = ext._data_to_record(data, "doc-1", [0])
        assert rec is not None
        assert rec.raw_dob == "03/15/1985"
        assert "DATE_OF_BIRTH" in rec.entity_types_found

    def test_url_not_mapped_as_email(self):
        """Vision model returns URL → not mapped to raw_email."""
        from app.structure.vision_extractor import VisionDocumentExtractor
        from app.llm.client import OllamaClient

        mock_client = MagicMock(spec=OllamaClient)
        ext = VisionDocumentExtractor(mock_client)

        data = {
            "PERSON": "Karen Craft",
            "EMAIL_ADDRESS": "www.candrvision.com",
        }
        rec = ext._data_to_record(data, "doc-1", [0])
        assert rec is not None
        assert rec.raw_email is None
        assert "EMAIL_ADDRESS" not in rec.entity_types_found

    def test_entity_types_only_populated_fields(self):
        """entity_types_found only lists types with actual values."""
        from app.structure.vision_extractor import VisionDocumentExtractor
        from app.llm.client import OllamaClient

        mock_client = MagicMock(spec=OllamaClient)
        ext = VisionDocumentExtractor(mock_client)

        data = {
            "PERSON": "Karen Craft",
            "LOCATION": "18 High Point TRL",
            "DATE_OF_BIRTH": None,
            "PHONE_NUMBER": None,
            "NI_NUMBER": None,
        }
        rec = ext._data_to_record(data, "doc-1", [0])
        assert rec is not None
        assert set(rec.entity_types_found) == {"PERSON", "LOCATION"}


# ---------------------------------------------------------------------------
# LLM Template extractor _data_to_record validation
# ---------------------------------------------------------------------------


class TestLLMTemplateDataToRecordValidation:
    """Tests that LLMTemplateExtractor._data_to_record validates fields."""

    def test_transaction_date_rejected(self):
        from app.structure.llm_template_extractor import LLMTemplateExtractor
        from app.llm.client import OllamaClient

        mock_client = MagicMock(spec=OllamaClient)
        ext = LLMTemplateExtractor(mock_client)

        data = {
            "PERSON": "John Doe",
            "DATE_OF_BIRTH": "01/15/2024",  # too recent
        }
        rec = ext._data_to_record(data, "doc-1", [0])
        assert rec is not None
        assert rec.raw_dob is None
        assert "DATE_OF_BIRTH" not in rec.entity_types_found

    def test_url_email_rejected(self):
        from app.structure.llm_template_extractor import LLMTemplateExtractor
        from app.llm.client import OllamaClient

        mock_client = MagicMock(spec=OllamaClient)
        ext = LLMTemplateExtractor(mock_client)

        data = {
            "PERSON": "John Doe",
            "EMAIL_ADDRESS": "https://example.com",
        }
        rec = ext._data_to_record(data, "doc-1", [0])
        assert rec is not None
        assert rec.raw_email is None
        assert "EMAIL_ADDRESS" not in rec.entity_types_found


# ---------------------------------------------------------------------------
# _looks_like_business() — Layer 1 heuristic
# ---------------------------------------------------------------------------


class TestLooksLikeBusiness:
    """Tests for _looks_like_business heuristic."""

    # --- Clear businesses ---

    def test_suffix_inc(self):
        assert _looks_like_business("Apple Inc.") is True

    def test_suffix_llc(self):
        assert _looks_like_business("Smith Holdings LLC") is True

    def test_suffix_ltd(self):
        assert _looks_like_business("Barclays Ltd") is True

    def test_suffix_corp(self):
        assert _looks_like_business("Mega Corp") is True

    def test_suffix_plc(self):
        assert _looks_like_business("British Telecom PLC") is True

    def test_keyword_technologies(self):
        assert _looks_like_business("Daikin Comfort Technologies") is True

    def test_keyword_supply(self):
        assert _looks_like_business("JOHNSTONE SUPPLY") is True

    def test_keyword_hospital(self):
        assert _looks_like_business("St Mary Hospital") is True

    def test_keyword_services(self):
        assert _looks_like_business("Allied Health Services") is True

    def test_keyword_university(self):
        assert _looks_like_business("Stanford University") is True

    def test_keyword_bank(self):
        assert _looks_like_business("First National Bank") is True

    def test_keyword_insurance(self):
        assert _looks_like_business("State Farm Insurance") is True

    def test_store_number(self):
        assert _looks_like_business("JOHNSTONE SUPPLY #576") is True

    def test_store_number_with_space(self):
        assert _looks_like_business("STORE # 4521") is True

    def test_ampersand_pattern(self):
        assert _looks_like_business("Smith & Wesson Firearms") is True

    def test_multi_word_credit_union(self):
        assert _looks_like_business("Pacific Credit Union") is True

    def test_multi_word_comfort_technologies(self):
        assert _looks_like_business("Daikin Comfort Technologies North America") is True

    def test_alfred_knopf_inc(self):
        """'ALFRED A. KNOPF, INC.' should be ORG."""
        assert _looks_like_business("ALFRED A. KNOPF, INC.") is True

    # --- Clear persons (should NOT be business) ---

    def test_person_simple(self):
        assert _looks_like_business("Karen Craft") is False

    def test_person_three_names(self):
        assert _looks_like_business("John Michael Smith") is False

    def test_person_single_name(self):
        assert _looks_like_business("Madonna") is False

    def test_person_with_middle_initial(self):
        assert _looks_like_business("James T. Kirk") is False

    def test_estate_of_person(self):
        """'Estate of Karen Craft' should NOT be flagged as business."""
        assert _looks_like_business("Estate of Karen Craft") is False

    def test_in_the_matter_of(self):
        assert _looks_like_business("In the Matter of John Doe") is False

    # --- Edge cases ---

    def test_empty_string(self):
        assert _looks_like_business("") is False

    def test_whitespace_only(self):
        assert _looks_like_business("   ") is False

    def test_trailing_period_suffix(self):
        """'Inc.' with trailing period should still match."""
        assert _looks_like_business("Acme Inc.") is True


# ---------------------------------------------------------------------------
# validate_person_name() — combined Layer 1 + Layer 2
# ---------------------------------------------------------------------------


class TestValidatePersonName:
    """Tests for validate_person_name (heuristic + optional spaCy)."""

    def test_valid_person(self):
        is_valid, reason = validate_person_name("Karen Craft")
        assert is_valid is True
        assert reason == ""

    def test_valid_person_three_names(self):
        is_valid, reason = validate_person_name("John Michael Smith")
        assert is_valid is True
        assert reason == ""

    def test_business_suffix(self):
        is_valid, reason = validate_person_name("Apple Inc.")
        assert is_valid is False
        assert reason == "name_is_business"

    def test_business_keyword(self):
        is_valid, reason = validate_person_name("Daikin Comfort Technologies")
        assert is_valid is False
        assert reason == "name_is_business"

    def test_business_store_number(self):
        is_valid, reason = validate_person_name("JOHNSTONE SUPPLY #576")
        assert is_valid is False
        assert reason == "name_is_business"

    def test_estate_of_is_person(self):
        is_valid, reason = validate_person_name("Estate of Karen Craft")
        assert is_valid is True
        assert reason == ""

    def test_empty_name(self):
        is_valid, reason = validate_person_name("")
        assert is_valid is False
        assert reason == "empty_name"

    def test_none_like_empty(self):
        is_valid, reason = validate_person_name("   ")
        assert is_valid is False
        assert reason == "empty_name"

    def test_knopf_inc(self):
        is_valid, reason = validate_person_name("ALFRED A. KNOPF, INC.")
        assert is_valid is False
        assert reason == "name_is_business"

    def test_manufacturing(self):
        is_valid, reason = validate_person_name("Acme Manufacturing")
        assert is_valid is False
        assert reason == "name_is_business"

    def test_foundation(self):
        is_valid, reason = validate_person_name("Bill Gates Foundation")
        assert is_valid is False
        assert reason == "name_is_business"


# ---------------------------------------------------------------------------
# validate_extracted_records — organization name suppression
# ---------------------------------------------------------------------------


class TestValidateExtractedRecordsOrgSuppression:
    """Tests for validate_extracted_records with business name suppression."""

    @staticmethod
    def _make_record(**kwargs) -> PIIRecord:
        defaults = dict(
            record_id="r1",
            entity_type="PERSON",
            normalized_value="Test",
            raw_name="Test",
            entity_types_found=("PERSON",),
        )
        defaults.update(kwargs)
        return PIIRecord(**defaults)

    def test_business_name_suppressed(self):
        """Business name → record dropped."""
        rec = self._make_record(
            raw_name="Daikin Comfort Technologies",
            normalized_value="Daikin Comfort Technologies",
        )
        result = validate_extracted_records([rec])
        assert len(result) == 0

    def test_business_with_other_fields_still_dropped(self):
        """Business name with address/phone → still dropped (no person name)."""
        rec = self._make_record(
            raw_name="JOHNSTONE SUPPLY #576",
            normalized_value="JOHNSTONE SUPPLY #576",
            raw_address={"raw": "123 Industrial Blvd"},
            raw_phone="555-1234",
        )
        result = validate_extracted_records([rec])
        assert len(result) == 0

    def test_real_person_preserved(self):
        """Real person name → preserved."""
        rec = self._make_record(
            raw_name="Karen Craft",
            normalized_value="Karen Craft",
        )
        result = validate_extracted_records([rec])
        assert len(result) == 1
        assert result[0].raw_name == "Karen Craft"

    def test_estate_of_preserved(self):
        """'Estate of John Doe' → preserved as person."""
        rec = self._make_record(
            raw_name="Estate of John Doe",
            normalized_value="Estate of John Doe",
        )
        result = validate_extracted_records([rec])
        assert len(result) == 1
        assert result[0].raw_name == "Estate of John Doe"

    def test_financial_term_still_suppressed(self):
        """Financial terms → still suppressed (existing behavior)."""
        rec = self._make_record(
            raw_name="lump sum",
            normalized_value="lump sum",
        )
        result = validate_extracted_records([rec])
        assert len(result) == 0


# ---------------------------------------------------------------------------
# Arts / culture / charitable org keyword detection
# ---------------------------------------------------------------------------


class TestArtsAndCultureOrgDetection:
    """Tests for arts/culture/charitable organization keyword detection."""

    # --- Music industry ---

    def test_music_keyword_fred_bock(self):
        assert _looks_like_business("Fred Bock Music") is True

    def test_music_keyword_round_hill(self):
        assert _looks_like_business("Round Hill Music") is True

    def test_music_keyword_hoagland(self):
        assert _looks_like_business("Hoagland Music") is True

    def test_music_keyword_kay_duke(self):
        assert _looks_like_business("Kay Duke Music") is True

    def test_music_keyword_sanga(self):
        assert _looks_like_business("Sanga Music") is True

    def test_musicians_keyword(self):
        assert _looks_like_business("Help Musicians Uk") is True

    def test_musicians_benevolent_fund(self):
        assert _looks_like_business("Musicians Benevolent Fund") is True

    def test_musica_keyword(self):
        assert _looks_like_business("Editio Musica Budapest") is True

    def test_publishing_keyword(self):
        assert _looks_like_business("Universal Music Publishing") is True

    def test_publishers_keyword(self):
        assert _looks_like_business("Associated Music Publishers") is True

    def test_conservatory_keyword(self):
        assert _looks_like_business("San Francisco Conservatory Of Music") is True

    # --- Cultural institutions ---

    def test_library_keyword(self):
        assert _looks_like_business("Trustees Of The Boston Public Library") is True

    def test_trustees_keyword(self):
        """'trustees' as keyword catches trust-related orgs."""
        assert _looks_like_business("Trustees Of The Boston Public Library") is True

    def test_museum_keyword(self):
        assert _looks_like_business("National Museum Of Art") is True

    def test_gallery_keyword(self):
        assert _looks_like_business("Tate Gallery") is True

    def test_archive_keyword(self):
        assert _looks_like_business("British Film Archive") is True

    # --- Charitable / social ---

    def test_benevolent_keyword(self):
        assert _looks_like_business("Musicians Benevolent Fund") is True

    def test_charity_keyword(self):
        assert _looks_like_business("Cancer Research Charity") is True

    def test_trust_keyword(self):
        """Standalone 'trust' keyword catches trust orgs."""
        assert _looks_like_business("Irwin A Bazelon Marital Trust") is True

    def test_fund_keyword(self):
        assert _looks_like_business("Community Development Fund") is True

    def test_fiduciary_keyword(self):
        assert _looks_like_business("Pacific Fiduciary Services") is True

    # --- Membership ---

    def test_society_keyword(self):
        assert _looks_like_business("Royal Philharmonic Society") is True

    def test_guild_keyword(self):
        assert _looks_like_business("Screen Actors Guild") is True

    def test_league_keyword(self):
        assert _looks_like_business("Urban League") is True

    def test_federation_keyword(self):
        assert _looks_like_business("International Federation Of Musicians") is True

    def test_union_keyword(self):
        assert _looks_like_business("American Civil Liberties Union") is True

    def test_club_keyword(self):
        assert _looks_like_business("Harvard Club") is True

    # --- Orchestra / performing arts ---

    def test_orchestra_keyword(self):
        assert _looks_like_business("London Symphony Orchestra") is True

    def test_philharmonic_keyword(self):
        assert _looks_like_business("New York Philharmonic") is True

    def test_symphony_keyword(self):
        assert _looks_like_business("Chicago Symphony") is True

    def test_opera_keyword(self):
        assert _looks_like_business("Royal Opera House") is True

    def test_theater_keyword(self):
        assert _looks_like_business("National Theater") is True

    def test_theatre_keyword(self):
        assert _looks_like_business("Royal Shakespeare Theatre") is True

    def test_ensemble_keyword(self):
        assert _looks_like_business("Chamber Ensemble Of London") is True

    def test_choir_keyword(self):
        assert _looks_like_business("Kings College Choir") is True

    def test_records_keyword(self):
        assert _looks_like_business("Atlantic Records") is True

    # --- Estate of / trust person protection ---

    def test_estate_of_still_person(self):
        """'Estate of John Doe' must NOT be flagged as business."""
        assert _looks_like_business("Estate of John Doe") is False

    def test_trust_does_not_block_estate(self):
        """'trust' keyword should not interfere with Estate-of check."""
        is_valid, _ = validate_person_name("Estate of John Doe")
        assert is_valid is True


# ---------------------------------------------------------------------------
# Bracket / paren / C/O pattern detection
# ---------------------------------------------------------------------------


class TestBracketParenCOPatterns:
    """Tests for decorator stripping + C/O detection.

    Parenthetical/bracket suffixes are now stripped before keyword checks,
    so org detection works on the BASE name only.  This prevents false
    positives on person names like "Doreen Rao (Summary Account)".
    """

    def test_uk_summary_acc_brackets_base_is_org(self):
        """'Curtis Brown Limited [Uk Summary Acc]' → base 'Curtis Brown Limited' → suffix 'limited'."""
        assert _looks_like_business("Curtis Brown Limited [Uk Summary Acc]") is True

    def test_us_account_parens_base_is_org(self):
        """'Editio Musica Budapest (Us Account)' → base 'Editio Musica Budapest' → keyword 'musica'."""
        assert _looks_like_business("Editio Musica Budapest (Us Account)") is True

    def test_summary_account_parens_base_is_org(self):
        """'Walton Music Company (Summary Account)' → base has 'music' and 'company'."""
        assert _looks_like_business("Walton Music Company (Summary Account)") is True

    def test_person_with_summary_account_not_flagged(self):
        """'Doreen Rao (Summary Account)' → base 'Doreen Rao' → PERSON."""
        assert _looks_like_business("Doreen Rao (Summary Account)") is False

    def test_person_with_us_account_not_flagged(self):
        """'John Stravinsky [Us Account]' → base 'John Stravinsky' → PERSON."""
        assert _looks_like_business("John Stravinsky [Us Account]") is False

    def test_parenthetical_composer_not_flagged(self):
        """'John Smith (Composer)' → base 'John Smith' → PERSON."""
        assert _looks_like_business("John Smith (Composer)") is False

    def test_c_o_middle(self):
        assert _looks_like_business("Amphion C/O Bmg Ricordi") is True

    def test_c_o_start(self):
        assert _looks_like_business("C/O Amphion Bmg Ricordi") is True

    def test_c_o_case_insensitive(self):
        assert _looks_like_business("Amphion c/o BMG Ricordi") is True


# ---------------------------------------------------------------------------
# validate_person_name — new keyword coverage
# ---------------------------------------------------------------------------


class TestValidatePersonNameNewKeywords:
    """Test validate_person_name correctly rejects new org types."""

    def test_music_org(self):
        is_valid, reason = validate_person_name("Fred Bock Music")
        assert is_valid is False
        assert reason == "name_is_business"

    def test_musicians_org(self):
        is_valid, reason = validate_person_name("Help Musicians Uk")
        assert is_valid is False
        assert reason == "name_is_business"

    def test_conservatory_org(self):
        is_valid, reason = validate_person_name("San Francisco Conservatory Of Music")
        assert is_valid is False
        assert reason == "name_is_business"

    def test_library_org(self):
        is_valid, reason = validate_person_name("Trustees Of The Boston Public Library")
        assert is_valid is False
        assert reason == "name_is_business"

    def test_trust_org(self):
        is_valid, reason = validate_person_name("Irwin A Bazelon Marital Trust")
        assert is_valid is False
        assert reason == "name_is_business"

    def test_fund_org(self):
        is_valid, reason = validate_person_name("Musicians Benevolent Fund")
        assert is_valid is False
        assert reason == "name_is_business"

    def test_c_o_org(self):
        is_valid, reason = validate_person_name("Amphion C/O Bmg Ricordi")
        assert is_valid is False
        assert reason == "name_is_business"

    def test_bracket_account_org(self):
        is_valid, reason = validate_person_name("Curtis Brown Limited [Uk Summary Acc]")
        assert is_valid is False
        assert reason == "name_is_business"

    def test_paren_account_org(self):
        is_valid, reason = validate_person_name("Editio Musica Budapest (Us Account)")
        assert is_valid is False
        assert reason == "name_is_business"

    def test_publishing_with_paren(self):
        is_valid, reason = validate_person_name("Universal Music Publishing (Druckman)")
        assert is_valid is False
        assert reason == "name_is_business"

    def test_publishers_with_paren(self):
        is_valid, reason = validate_person_name("Associated Music Publishers (Harbison)")
        assert is_valid is False
        assert reason == "name_is_business"

    def test_real_person_still_passes(self):
        """Ensure real person names are not caught by new keywords."""
        is_valid, _ = validate_person_name("Karen Craft")
        assert is_valid is True

    def test_person_with_normal_parens(self):
        """Person name with non-account parenthetical should pass."""
        is_valid, _ = validate_person_name("James Purdy")
        assert is_valid is True


# ---------------------------------------------------------------------------
# validate_extracted_records — org suppression with new keywords
# ---------------------------------------------------------------------------


class TestValidateExtractedRecordsNewOrgs:
    """Tests that validate_extracted_records drops records with new org names."""

    @staticmethod
    def _make_record(**kwargs) -> PIIRecord:
        defaults = dict(
            record_id="r1",
            entity_type="PERSON",
            normalized_value="Test",
            raw_name="Test",
            entity_types_found=("PERSON",),
        )
        defaults.update(kwargs)
        return PIIRecord(**defaults)

    def test_music_org_suppressed(self):
        rec = self._make_record(
            raw_name="Fred Bock Music",
            normalized_value="Fred Bock Music",
        )
        result = validate_extracted_records([rec])
        assert len(result) == 0

    def test_trust_org_suppressed(self):
        rec = self._make_record(
            raw_name="Irwin A Bazelon Marital Trust",
            normalized_value="Irwin A Bazelon Marital Trust",
        )
        result = validate_extracted_records([rec])
        assert len(result) == 0

    def test_c_o_org_suppressed(self):
        rec = self._make_record(
            raw_name="Amphion C/O Bmg Ricordi",
            normalized_value="Amphion C/O Bmg Ricordi",
        )
        result = validate_extracted_records([rec])
        assert len(result) == 0

    def test_bracket_account_org_suppressed(self):
        rec = self._make_record(
            raw_name="Curtis Brown Limited [Uk Summary Acc]",
            normalized_value="Curtis Brown Limited [Uk Summary Acc]",
        )
        result = validate_extracted_records([rec])
        assert len(result) == 0

    def test_estate_of_still_preserved(self):
        """Estate of person must not be caught by 'trust' keyword."""
        rec = self._make_record(
            raw_name="Estate of John Doe",
            normalized_value="Estate of John Doe",
        )
        result = validate_extracted_records([rec])
        assert len(result) == 1
        assert result[0].raw_name == "Estate of John Doe"


# ---------------------------------------------------------------------------
# EIN detection and validation
# ---------------------------------------------------------------------------


class TestEINDetection:
    """Tests for US_EIN pattern validation and gov_id_is_ein flag."""

    @staticmethod
    def _make_record(**kwargs) -> PIIRecord:
        defaults = dict(
            record_id="r1",
            entity_type="PERSON",
            normalized_value="Test Corp",
            raw_name="Test Corp",
            entity_types_found=("PERSON",),
        )
        defaults.update(kwargs)
        return PIIRecord(**defaults)

    def test_ein_pattern_matches(self):
        from app.pii.pattern_validator import VALIDATION_PATTERNS
        assert VALIDATION_PATTERNS["US_EIN"].match("12-3456789")

    def test_ein_pattern_rejects_ssn(self):
        from app.pii.pattern_validator import VALIDATION_PATTERNS
        assert not VALIDATION_PATTERNS["US_EIN"].match("123-45-6789")

    def test_ein_pattern_rejects_short(self):
        from app.pii.pattern_validator import VALIDATION_PATTERNS
        assert not VALIDATION_PATTERNS["US_EIN"].match("12-345678")

    def test_resolve_gov_id_type_infers_ein(self):
        from app.pii.pattern_validator import _resolve_gov_id_type
        rec = self._make_record(
            raw_government_id="13-1234567",
            entity_types_found=("PERSON",),  # no explicit US_EIN type
        )
        assert _resolve_gov_id_type(rec) == "US_EIN"

    def test_resolve_gov_id_type_explicit_ein(self):
        from app.pii.pattern_validator import _resolve_gov_id_type
        rec = self._make_record(
            raw_government_id="13-1234567",
            entity_types_found=("PERSON", "US_EIN"),
        )
        assert _resolve_gov_id_type(rec) == "US_EIN"

    def test_resolve_gov_id_type_ssn_not_confused(self):
        from app.pii.pattern_validator import _resolve_gov_id_type
        rec = self._make_record(
            raw_government_id="123-45-6789",
            entity_types_found=("PERSON", "US_SSN"),
        )
        assert _resolve_gov_id_type(rec) == "US_SSN"

    def test_ein_flag_set_on_record(self):
        """validate_extracted_records sets gov_id_is_ein flag for EIN format."""
        rec = self._make_record(
            raw_name="Some Person",
            normalized_value="Some Person",
            raw_government_id="13-1234567",
            entity_types_found=("PERSON",),
        )
        result = validate_extracted_records([rec])
        assert len(result) == 1
        assert "gov_id_is_ein" in result[0].validation_flags

    def test_ssn_no_ein_flag(self):
        """SSN format should NOT get gov_id_is_ein flag."""
        rec = self._make_record(
            raw_name="Some Person",
            normalized_value="Some Person",
            raw_government_id="123-45-6789",
            entity_types_found=("PERSON", "US_SSN"),
        )
        result = validate_extracted_records([rec])
        assert len(result) == 1
        assert "gov_id_is_ein" not in result[0].validation_flags

    def test_us_ein_in_gov_id_entity_types(self):
        from app.pii.pattern_validator import _GOV_ID_ENTITY_TYPES
        assert "US_EIN" in _GOV_ID_ENTITY_TYPES


# ---------------------------------------------------------------------------
# Name decorator stripping + dedup matching
# ---------------------------------------------------------------------------


class TestStripNameDecorators:
    """Tests for strip_name_decorators() and its effect on name matching."""

    def test_parenthetical_editor(self):
        from app.rra.fuzzy import strip_name_decorators
        assert strip_name_decorators("Francisco Nunez (Editor)") == "Francisco Nunez"

    def test_parenthetical_composer(self):
        from app.rra.fuzzy import strip_name_decorators
        assert strip_name_decorators("Betty Bertaux (Composer)") == "Betty Bertaux"

    def test_bracket_us_account(self):
        from app.rra.fuzzy import strip_name_decorators
        assert strip_name_decorators("John Stravinsky [Us Account]") == "John Stravinsky"

    def test_trailing_roman_numeral(self):
        from app.rra.fuzzy import strip_name_decorators
        assert strip_name_decorators("Betty Bertaux Ii") == "Betty Bertaux"

    def test_combined_roman_and_paren(self):
        from app.rra.fuzzy import strip_name_decorators
        assert strip_name_decorators("Betty Bertaux Ii (Editor)") == "Betty Bertaux"

    def test_aka_stripped(self):
        from app.rra.fuzzy import strip_name_decorators
        result = strip_name_decorators("Nick Page A/K/A William R. Page Jr.")
        assert result == "Nick Page"

    def test_re_stripped(self):
        from app.rra.fuzzy import strip_name_decorators
        result = strip_name_decorators('Nick Page (Re: "Niska Banja")')
        assert result == "Nick Page"

    def test_circle_of_sound_paren(self):
        from app.rra.fuzzy import strip_name_decorators
        result = strip_name_decorators("Doreen (Circle Of Sound) Rao")
        # Middle parenthetical stays (regex only matches trailing)
        assert "Doreen" in result

    def test_frank_ohara_paren(self):
        from app.rra.fuzzy import strip_name_decorators
        result = strip_name_decorators("Maureen Granville-Smith (Frank O'Hara)")
        assert result == "Maureen Granville-Smith"

    def test_never_returns_empty(self):
        from app.rra.fuzzy import strip_name_decorators
        assert strip_name_decorators("(Editor)") != ""

    def test_plain_name_unchanged(self):
        from app.rra.fuzzy import strip_name_decorators
        assert strip_name_decorators("John Smith") == "John Smith"

    def test_stacked_suffixes(self):
        from app.rra.fuzzy import strip_name_decorators
        result = strip_name_decorators("Name (Foo) [Bar]")
        assert result == "Name"


class TestNameMatchingWithDecorators:
    """Test that names_match correctly matches names after decorator stripping."""

    def test_exact_duplicate_matches(self):
        from app.rra.fuzzy import names_match
        matched, conf = names_match("Dana Wilson", "Dana Wilson")
        assert matched is True
        assert conf == 1.0

    def test_editor_suffix_matches(self):
        from app.rra.fuzzy import names_match
        matched, _ = names_match("Francisco Nunez (Editor)", "Francisco Nunez")
        assert matched is True

    def test_composer_vs_editor_matches(self):
        """Same base name with different account descriptors should match."""
        from app.rra.fuzzy import names_match
        matched, _ = names_match("Betty Bertaux (Composer)", "Betty Bertaux Ii (Editor)")
        assert matched is True

    def test_bracket_account_matches(self):
        from app.rra.fuzzy import names_match
        matched, _ = names_match("John Stravinsky [Us Account]", "John Stravinsky")
        assert matched is True

    def test_middle_initial_matches(self):
        """'Avrahm Galper' vs 'Avrahm M. Galper' should match via JW."""
        from app.rra.fuzzy import names_match
        matched, _ = names_match("Avrahm Galper", "Avrahm M. Galper")
        assert matched is True

    def test_different_people_dont_match(self):
        """Completely different names should not match."""
        from app.rra.fuzzy import names_match
        matched, _ = names_match("John Moorhead", "Alice Moorhead Lynn")
        assert matched is False

    def test_doreen_rao_variants_match(self):
        from app.rra.fuzzy import names_match
        matched, _ = names_match(
            "Doreen (Circle Of Sound) Rao",
            "Doreen Rao (Composer/Arranger)",
        )
        assert matched is True

    def test_granville_smith_matches(self):
        from app.rra.fuzzy import names_match
        matched, _ = names_match(
            "Maureen Granville-Smith",
            "Maureen Granville-Smith (Frank O'Hara)",
        )
        assert matched is True
