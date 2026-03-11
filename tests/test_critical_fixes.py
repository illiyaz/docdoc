"""Tests for critical fixes: address formatting, org suppression, template scaling.

Covers:
- format_address(): dict with "raw" key, structured dict, string passthrough, None
- is_likely_organization(): corporate suffixes, normal names, firm patterns
- PERSON suppression for org names via is_likely_false_positive()
- LLM page auto-scaling for large documents
"""
from __future__ import annotations

import pytest

from app.export.csv_exporter import format_address
from app.pii.context_deny_list import (
    ORGANIZATION_SUFFIXES,
    is_likely_false_positive,
    is_likely_organization,
)


# ===========================================================================
# format_address
# ===========================================================================


class TestFormatAddress:
    def test_dict_with_raw_key(self):
        assert format_address({"raw": "85 Waltings Gardens"}) == "85 Waltings Gardens"

    def test_dict_with_structured_fields(self):
        addr = {"street": "85 Waltings Gardens", "city": "London", "postcode": "NW2 3UD"}
        result = format_address(addr)
        assert result == "85 Waltings Gardens, London, NW2 3UD"

    def test_string_passthrough(self):
        assert format_address("85 Waltings Gardens, London NW2 3UD") == "85 Waltings Gardens, London NW2 3UD"

    def test_none_returns_empty(self):
        assert format_address(None) == ""

    def test_dict_with_state_zip(self):
        addr = {"street": "123 Main St", "city": "DC", "state": "DC", "zip": "20001"}
        result = format_address(addr)
        assert result == "123 Main St, DC, DC, 20001"

    def test_empty_dict(self):
        result = format_address({})
        assert result == "{}"

    def test_raw_key_preferred_over_structured(self):
        addr = {"raw": "Full Address", "street": "123 Main"}
        assert format_address(addr) == "Full Address"


# ===========================================================================
# is_likely_organization
# ===========================================================================


class TestIsLikelyOrganization:
    def test_limited(self):
        assert is_likely_organization("William M Mercer Limited") is True

    def test_ltd(self):
        assert is_likely_organization("Acme Ltd") is True

    def test_ltd_with_dot(self):
        assert is_likely_organization("Acme Ltd.") is True

    def test_plc(self):
        assert is_likely_organization("BP Plc") is True

    def test_llp(self):
        assert is_likely_organization("Ernst & Young LLP") is True

    def test_normal_name(self):
        assert is_likely_organization("K P Acheampong") is False

    def test_boosey_and_hawkes(self):
        # "Boosey & Hawkes" has & as middle word but only 3 words, last word is "Hawkes" not a suffix
        # However the & pattern: len>=3 and words[-2]=="&" → True
        assert is_likely_organization("Boosey & Hawkes") is True

    def test_ampersand_firm(self):
        assert is_likely_organization("Smith & Jones") is True

    def test_empty(self):
        assert is_likely_organization("") is False

    def test_none(self):
        assert is_likely_organization(None) is False

    def test_single_word(self):
        assert is_likely_organization("John") is False

    def test_inc(self):
        assert is_likely_organization("Apple Inc") is True

    def test_inc_with_dot(self):
        assert is_likely_organization("Apple Inc.") is True

    def test_gmbh(self):
        assert is_likely_organization("Siemens GmbH") is True

    def test_services(self):
        assert is_likely_organization("Healthcare Services") is True

    def test_group(self):
        assert is_likely_organization("CMG Group") is True


# ===========================================================================
# PERSON suppression via is_likely_false_positive
# ===========================================================================


class TestPersonOrgSuppression:
    def test_person_with_limited_suffix_suppressed(self):
        is_fp, reason = is_likely_false_positive(
            "William M Mercer Limited", "PERSON", ""
        )
        assert is_fp is True
        assert "organization_name" in reason

    def test_person_with_ltd_suppressed(self):
        is_fp, reason = is_likely_false_positive("Acme Ltd", "PERSON", "")
        assert is_fp is True

    def test_person_normal_name_not_suppressed(self):
        is_fp, reason = is_likely_false_positive("K P Acheampong", "PERSON", "")
        assert is_fp is False

    def test_person_with_llp_suppressed(self):
        is_fp, reason = is_likely_false_positive(
            "Ernst & Young LLP", "PERSON", ""
        )
        assert is_fp is True

    def test_non_person_entity_not_affected(self):
        """Org suffix check only applies to PERSON entity type."""
        is_fp, _ = is_likely_false_positive(
            "Acme Limited", "ORGANIZATION", ""
        )
        assert is_fp is False

    def test_person_with_inc_suppressed(self):
        is_fp, _ = is_likely_false_positive("Apple Inc.", "PERSON", "")
        assert is_fp is True


# ===========================================================================
# LLM page auto-scaling
# ===========================================================================


class TestLLMPageScaling:
    def test_small_doc_uses_default(self):
        from app.structure.llm_document_understanding import LLMDocumentUnderstanding
        du = LLMDocumentUnderstanding(db_session=None)
        pages = du._resolve_pages_to_read("gdpr", None, total_pages=6)
        assert pages == 3  # GDPR default

    def test_medium_doc_scales_to_6(self):
        from app.structure.llm_document_understanding import LLMDocumentUnderstanding
        du = LLMDocumentUnderstanding(db_session=None)
        pages = du._resolve_pages_to_read("gdpr", None, total_pages=50)
        assert pages >= 6

    def test_large_doc_scales_to_9(self):
        from app.structure.llm_document_understanding import LLMDocumentUnderstanding
        du = LLMDocumentUnderstanding(db_session=None)
        pages = du._resolve_pages_to_read("gdpr", None, total_pages=450)
        assert pages >= 9

    def test_protocol_config_override_respected(self):
        from app.structure.llm_document_understanding import LLMDocumentUnderstanding
        du = LLMDocumentUnderstanding(db_session=None)
        # Explicit protocol config override should still work
        pages = du._resolve_pages_to_read("gdpr", {"llm_pages_to_read": 10}, total_pages=450)
        assert pages == 10

    def test_hard_cap_15(self):
        from app.structure.llm_document_understanding import LLMDocumentUnderstanding
        du = LLMDocumentUnderstanding(db_session=None)
        pages = du._resolve_pages_to_read("gdpr", {"llm_pages_to_read": 20}, total_pages=1000)
        assert pages == 15


# ===========================================================================
# Organization suffixes collection
# ===========================================================================


class TestOrganizationSuffixes:
    def test_common_suffixes_present(self):
        for suffix in ("limited", "ltd", "inc", "corp", "llc", "llp", "plc", "gmbh"):
            assert suffix in ORGANIZATION_SUFFIXES, f"{suffix} missing"

    def test_no_common_names(self):
        """Suffixes should not include common person name parts."""
        for word in ("john", "smith", "jane", "doe"):
            assert word not in ORGANIZATION_SUFFIXES
