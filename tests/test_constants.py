"""Tests for app.core.constants — entity type to data category mapping.

Covers:
- Every custom entity type from layer1_patterns has a mapping
- All mapped categories are members of VALID_CATEGORIES
- Multi-category mappings work correctly
- get_entity_categories() returns correct results for known types
- Unmapped entity types default to ["PII"]
- Case-insensitive lookup
- Presidio built-in entity types are mapped
"""
from __future__ import annotations

import pytest

from app.core.constants import (
    DATA_CATEGORIES,
    ENTITY_CATEGORY_MAP,
    VALID_CATEGORIES,
    get_entity_categories,
)
from app.pii.layer1_patterns import CUSTOM_PATTERNS


# ===========================================================================
# Every custom pattern entity type has a mapping
# ===========================================================================


class TestCustomPatternCoverage:
    """Every entity type in our custom patterns must have an entry in ENTITY_CATEGORY_MAP."""

    def test_all_custom_entity_types_mapped(self) -> None:
        """All entity types from layer1_patterns.CUSTOM_PATTERNS must appear in the map."""
        custom_types = {p.entity_type for p in CUSTOM_PATTERNS}
        mapped_types = set(ENTITY_CATEGORY_MAP.keys())
        missing = custom_types - mapped_types
        assert missing == set(), f"Entity types missing from ENTITY_CATEGORY_MAP: {missing}"

    @pytest.mark.parametrize(
        "entity_type",
        sorted({p.entity_type for p in CUSTOM_PATTERNS}),
    )
    def test_each_custom_entity_type_has_entry(self, entity_type: str) -> None:
        """Parametrized: each custom entity type individually confirmed in the map."""
        assert entity_type in ENTITY_CATEGORY_MAP, (
            f"{entity_type} not found in ENTITY_CATEGORY_MAP"
        )


# ===========================================================================
# All mapped categories are valid
# ===========================================================================


class TestCategoryValidity:
    """Every category referenced in ENTITY_CATEGORY_MAP must be in VALID_CATEGORIES."""

    def test_all_mapped_categories_are_valid(self) -> None:
        """No category string in the map is outside the defined set."""
        for entity_type, categories in ENTITY_CATEGORY_MAP.items():
            for cat in categories:
                assert cat in VALID_CATEGORIES, (
                    f"Category '{cat}' for entity type '{entity_type}' "
                    f"is not in VALID_CATEGORIES"
                )

    def test_valid_categories_matches_data_categories(self) -> None:
        """VALID_CATEGORIES is derived from DATA_CATEGORIES keys."""
        assert VALID_CATEGORIES == frozenset(DATA_CATEGORIES.keys())

    def test_data_categories_has_label_and_regulation(self) -> None:
        """Every DATA_CATEGORIES entry has 'label' and 'regulation' keys."""
        for cat, meta in DATA_CATEGORIES.items():
            assert "label" in meta, f"Missing 'label' for category {cat}"
            assert "regulation" in meta, f"Missing 'regulation' for category {cat}"

    def test_all_eight_categories_present(self) -> None:
        """All 8 expected categories are defined."""
        expected = {"PII", "SPII", "PHI", "PFI", "PCI", "NPI", "FTI", "CREDENTIALS"}
        assert expected == VALID_CATEGORIES

    def test_every_category_has_at_least_one_entity_type(self) -> None:
        """Each category is used by at least one entity type in the map."""
        used_categories: set[str] = set()
        for categories in ENTITY_CATEGORY_MAP.values():
            used_categories.update(categories)
        for cat in VALID_CATEGORIES:
            assert cat in used_categories, (
                f"Category '{cat}' is defined but not used by any entity type"
            )


# ===========================================================================
# Multi-category mappings
# ===========================================================================


class TestMultiCategoryMappings:
    """Entity types that map to multiple categories."""

    def test_ssn_is_pii_and_spii(self) -> None:
        cats = get_entity_categories("SSN")
        assert "PII" in cats
        assert "SPII" in cats

    def test_credit_card_is_pfi_and_pci(self) -> None:
        cats = get_entity_categories("CREDIT_CARD")
        assert "PFI" in cats
        assert "PCI" in cats

    def test_us_bank_number_is_pfi_and_npi(self) -> None:
        cats = get_entity_categories("US_BANK_NUMBER")
        assert "PFI" in cats
        assert "NPI" in cats

    def test_us_itin_is_pii_and_fti(self) -> None:
        cats = get_entity_categories("US_ITIN")
        assert "PII" in cats
        assert "FTI" in cats

    def test_password_is_credentials(self) -> None:
        cats = get_entity_categories("PASSWORD")
        assert cats == ["CREDENTIALS"]

    def test_ein_is_pii_and_fti(self) -> None:
        cats = get_entity_categories("EIN")
        assert "PII" in cats
        assert "FTI" in cats

    def test_iban_is_pfi_and_npi(self) -> None:
        cats = get_entity_categories("IBAN")
        assert "PFI" in cats
        assert "NPI" in cats

    def test_biometric_identifier_is_pii_and_spii(self) -> None:
        cats = get_entity_categories("BIOMETRIC_IDENTIFIER")
        assert "PII" in cats
        assert "SPII" in cats

    def test_nrp_is_pii_and_spii(self) -> None:
        """Nationality/religious/political group is sensitive PII."""
        cats = get_entity_categories("NRP")
        assert "PII" in cats
        assert "SPII" in cats

    def test_mrn_is_phi_only(self) -> None:
        cats = get_entity_categories("MRN")
        assert cats == ["PHI"]

    def test_financial_account_pair_is_pfi_and_npi(self) -> None:
        cats = get_entity_categories("FINANCIAL_ACCOUNT_PAIR")
        assert "PFI" in cats
        assert "NPI" in cats

    def test_multi_category_returns_list_copy(self) -> None:
        """get_entity_categories returns a copy, not the original list."""
        cats1 = get_entity_categories("SSN")
        cats2 = get_entity_categories("SSN")
        assert cats1 == cats2
        assert cats1 is not cats2  # must be a copy


# ===========================================================================
# get_entity_categories() — default and case-insensitive behavior
# ===========================================================================


class TestGetEntityCategories:
    """Verify the lookup function behavior."""

    def test_unmapped_entity_type_defaults_to_pii(self) -> None:
        assert get_entity_categories("TOTALLY_UNKNOWN_TYPE") == ["PII"]

    def test_empty_string_defaults_to_pii(self) -> None:
        assert get_entity_categories("") == ["PII"]

    def test_case_insensitive_lookup(self) -> None:
        """Lowercase input should still resolve correctly."""
        cats = get_entity_categories("ssn")
        assert "PII" in cats
        assert "SPII" in cats

    def test_exact_match_preferred(self) -> None:
        """When exact key exists, use it directly."""
        cats = get_entity_categories("CREDIT_CARD")
        assert "PFI" in cats
        assert "PCI" in cats

    def test_no_empty_category_lists(self) -> None:
        """Every entry in the map must have at least one category."""
        for entity_type, categories in ENTITY_CATEGORY_MAP.items():
            assert len(categories) > 0, (
                f"Entity type '{entity_type}' has empty category list"
            )
