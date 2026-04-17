"""Tests for geo-neutral government-ID type classifier."""
from __future__ import annotations

import pytest

from app.pii.gov_id_classifier import (
    GENERIC_TYPE,
    SUPPORTED_TYPES,
    infer_gov_id_type,
)


class TestUnambiguousStrictFormats:
    """Formats with letters/checksums identify themselves without a hint."""

    def test_uk_nino(self):
        assert infer_gov_id_type("NE724362D") == "UK_NINO"

    def test_uk_nino_with_hint(self):
        assert infer_gov_id_type("NH828286A", country_hint="GB") == "UK_NINO"

    def test_in_pan(self):
        assert infer_gov_id_type("ABCPK1234E") == "IN_PAN"

    def test_in_pan_lowercase_gets_normalized(self):
        assert infer_gov_id_type("abcpk1234e") == "IN_PAN"

    def test_it_codice_fiscale(self):
        assert infer_gov_id_type("RSSMRA80A01H501U") == "IT_CF"

    def test_sg_nric(self):
        assert infer_gov_id_type("S1234567A") == "SG_NRIC"

    def test_mx_curp(self):
        # 18-char CURP example.
        assert infer_gov_id_type("PEGJ850315HDFRRN02") == "MX_CURP"

    def test_br_cpf_formatted(self):
        assert infer_gov_id_type("123.456.789-09") == "BR_CPF"

    def test_br_cpf_no_separators(self):
        assert infer_gov_id_type("12345678909") in ("BR_CPF", GENERIC_TYPE)

    def test_es_dni(self):
        assert infer_gov_id_type("12345678Z") == "ES_DNI"

    def test_es_nie(self):
        assert infer_gov_id_type("X1234567L") == "ES_NIE"

    def test_kr_rrn(self):
        assert infer_gov_id_type("900101-1234567") == "KR_RRN"

    def test_cn_resid_with_x_check(self):
        assert infer_gov_id_type("11010519491231002X") == "CN_RESID"


class TestDigitOnlyDisambiguation:
    """Digit-only patterns require a country hint to classify."""

    def test_nine_digits_no_hint_returns_generic(self):
        # 9 digits matches US_SSN, IL_ID, NL_BSN — ambiguous.
        assert infer_gov_id_type("123456789") == GENERIC_TYPE

    def test_nine_digits_with_us_hint(self):
        assert infer_gov_id_type("123456789", country_hint="US") == "US_SSN"

    def test_nine_digits_with_nl_hint(self):
        assert infer_gov_id_type("123456789", country_hint="NL") == "NL_BSN"

    def test_nine_digits_with_il_hint(self):
        assert infer_gov_id_type("123456789", country_hint="IL") == "IL_ID"

    def test_us_ssn_with_dashes(self):
        # With dashes the format 3-2-4 only matches US_SSN.
        assert infer_gov_id_type("123-45-6789") == "US_SSN"

    def test_ca_sin_with_dashes(self):
        # 3-3-3 format only matches CA_SIN.
        assert infer_gov_id_type("123-456-789") == "CA_SIN"

    def test_twelve_digits_aadhaar_with_hint(self):
        # Aadhaar requires first digit 2-9.
        assert infer_gov_id_type("234567890123", country_hint="IN") == "IN_AADHAAR"

    def test_twelve_digits_jp_with_hint(self):
        # JP MyNumber has no first-digit restriction.
        assert infer_gov_id_type("123456789012", country_hint="JP") == "JP_MYNUMBER"


class TestNoMatch:
    def test_empty_string(self):
        assert infer_gov_id_type("") == GENERIC_TYPE

    def test_none(self):
        assert infer_gov_id_type(None) == GENERIC_TYPE

    def test_garbage_characters(self):
        assert infer_gov_id_type("!!!") == GENERIC_TYPE

    def test_random_short_string(self):
        assert infer_gov_id_type("abc") == GENERIC_TYPE


class TestCountryHintPrecedence:
    def test_hint_narrows_from_multiple(self):
        # "123456789" matches US_SSN, IL_ID, NL_BSN. Hint should pick one.
        assert infer_gov_id_type("123456789", country_hint="US") == "US_SSN"
        assert infer_gov_id_type("123456789", country_hint="IL") == "IL_ID"
        assert infer_gov_id_type("123456789", country_hint="NL") == "NL_BSN"

    def test_hint_that_matches_nothing_in_candidates(self):
        # 9 digits doesn't have a GB digit-only pattern → fall back to strict-only
        # logic, which finds none → GENERIC.
        assert infer_gov_id_type("123456789", country_hint="GB") == GENERIC_TYPE

    def test_hint_with_unambiguous_strict_still_returns_same(self):
        # UK_NINO is already unambiguous; hint is redundant but consistent.
        assert infer_gov_id_type("NE724362D", country_hint="GB") == "UK_NINO"
        # Wrong hint doesn't corrupt unambiguous strict match.
        assert infer_gov_id_type("NE724362D", country_hint="US") == "UK_NINO"


class TestCoverage:
    """Sanity: the public API exposes the types and they're all strings."""

    def test_supported_types_includes_generic(self):
        assert GENERIC_TYPE in SUPPORTED_TYPES

    def test_supported_types_is_large(self):
        # We claim support for 40+ country-specific ID formats.
        assert len(SUPPORTED_TYPES) >= 40

    def test_all_types_are_uppercase_labels(self):
        for t in SUPPORTED_TYPES:
            assert t.isupper() or "_" in t


@pytest.mark.parametrize(
    "raw, hint, expected",
    [
        # The benchmark case that drove this fix.
        ("NE724362D", "GB", "UK_NINO"),
        # Real IDs across geographies.
        ("123-45-6789", "US", "US_SSN"),
        ("234567890123", "IN", "IN_AADHAAR"),
        ("ABCDE1234F", "IN", "IN_PAN"),
        ("123.456.789-09", "BR", "BR_CPF"),
        ("S1234567A", "SG", "SG_NRIC"),
        ("900101-1234567", "KR", "KR_RRN"),
        ("PEGJ850315HDFRRN02", "MX", "MX_CURP"),
        ("12345678Z", "ES", "ES_DNI"),
        ("RSSMRA80A01H501U", "IT", "IT_CF"),
    ],
)
def test_real_world_examples(raw: str, hint: str, expected: str):
    assert infer_gov_id_type(raw, country_hint=hint) == expected
