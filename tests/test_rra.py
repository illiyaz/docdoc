"""Tests for app/rra/fuzzy.py — Phase 2 entity resolver utilities."""
from __future__ import annotations

import pytest

from app.rra.fuzzy import (
    soundex,
    jaro_winkler,
    names_match,
    addresses_match,
    government_ids_match,
    normalize_dob,
    dobs_match,
    _edit_distance_one,
)


# ===========================================================================
# soundex
# ===========================================================================

class TestSoundex:
    def test_standard_robert(self):
        assert soundex("Robert") == "R163"

    def test_rupert_matches_robert(self):
        # Classic Soundex property: similar-sounding surnames share a code
        assert soundex("Rupert") == soundex("Robert")

    def test_ashcraft_ashcroft(self):
        # NIST example
        assert soundex("Ashcraft") == soundex("Ashcroft")

    def test_empty_string(self):
        assert soundex("") == "0000"

    def test_whitespace_only(self):
        assert soundex("   ") == "0000"

    def test_non_alpha(self):
        assert soundex("123") == "0000"

    def test_single_letter(self):
        result = soundex("A")
        assert result == "A000"

    def test_padding_to_four_chars(self):
        result = soundex("Lee")
        assert len(result) == 4

    def test_truncate_to_four_chars(self):
        result = soundex("Washington")
        assert len(result) == 4

    def test_case_insensitive(self):
        assert soundex("smith") == soundex("SMITH")

    def test_adjacent_same_codes_deduplicated(self):
        # 'ck' → both map to '2'; only one '2' should appear
        assert soundex("Jackson") == "J250"

    def test_vowel_separates_same_code(self):
        # If a vowel appears between two letters of the same code, the code
        # is NOT deduplicated (Pfister example from Soundex spec)
        # P=1(skip), f=1, i=vowel(sep), s=2, t=3
        result = soundex("Pfister")
        assert result[0] == "P"
        assert len(result) == 4


# ===========================================================================
# jaro_winkler
# ===========================================================================

class TestJaroWinkler:
    def test_identical_strings(self):
        assert jaro_winkler("hello", "hello") == 1.0

    def test_empty_both(self):
        # Both empty — treated as identical
        assert jaro_winkler("", "") == 1.0

    def test_one_empty(self):
        assert jaro_winkler("hello", "") == 0.0
        assert jaro_winkler("", "hello") == 0.0

    def test_case_insensitive(self):
        assert jaro_winkler("JOHN", "john") == 1.0

    def test_completely_different(self):
        score = jaro_winkler("AAAAAA", "BBBBBB")
        assert score == 0.0

    def test_martha_marhta_high_score(self):
        # Classic Jaro example — transposition, score should be high
        score = jaro_winkler("martha", "marhta")
        assert score > 0.95

    def test_prefix_bonus_applied(self):
        # Strings with shared prefix should score higher than Jaro alone
        score_jw = jaro_winkler("JOHNATHAN", "JONATHAN")
        assert score_jw > 0.90

    def test_score_in_range(self):
        score = jaro_winkler("abc", "xyz")
        assert 0.0 <= score <= 1.0

    def test_symmetric(self):
        s1, s2 = "richard", "richarde"
        assert abs(jaro_winkler(s1, s2) - jaro_winkler(s2, s1)) < 1e-9


# ===========================================================================
# names_match
# ===========================================================================

class TestNamesMatch:
    # --- Latin exact / near-exact ---
    def test_exact_match(self):
        matched, conf = names_match("John Smith", "John Smith")
        assert matched is True
        assert conf == 1.0

    def test_case_insensitive_exact(self):
        matched, conf = names_match("john smith", "JOHN SMITH")
        assert matched is True
        assert conf == 1.0

    def test_high_jw_match(self):
        matched, conf = names_match("Jonathan Williams", "Johnathan Williams")
        assert matched is True
        assert conf >= 0.90

    def test_soundex_match(self):
        # Robert / Rupert share soundex and have JW > 0.80
        matched, conf = names_match("Robert Brown", "Rupert Brown")
        assert matched is True

    def test_completely_different_names_no_match(self):
        matched, conf = names_match("Alice Zhang", "Bob Johnson")
        assert matched is False

    def test_empty_name1_no_match(self):
        matched, conf = names_match("", "John Smith")
        assert matched is False
        assert conf == 0.0

    def test_empty_name2_no_match(self):
        matched, conf = names_match("John Smith", "")
        assert matched is False
        assert conf == 0.0

    def test_one_char_typo_high_conf(self):
        matched, conf = names_match("Sarah Connor", "Sarah Conner")
        assert matched is True
        assert conf >= 0.92

    # --- Non-Latin path ---
    def test_non_latin_identical(self):
        matched, conf = names_match("محمد علي", "محمد علي")
        assert matched is True
        assert conf >= 0.88

    def test_non_latin_different(self):
        matched, conf = names_match("محمد", "علي")
        assert matched is False

    def test_non_latin_close(self):
        # Same name, one extra space collapsed
        matched, conf = names_match("王小明", "王小明")
        assert matched is True

    def test_confidence_is_float(self):
        _, conf = names_match("Alice", "Alicia")
        assert isinstance(conf, float)
        assert 0.0 <= conf <= 1.0


# ===========================================================================
# addresses_match
# ===========================================================================

class TestAddressesMatch:
    def _addr(
        self,
        street="123 main st",
        city="springfield",
        state="IL",
        zip_code="62701",
        country="US",
    ) -> dict:
        return {
            "street": street,
            "city": city,
            "state": state,
            "zip": zip_code,
            "country": country,
        }

    def test_none_addr1(self):
        matched, conf = addresses_match(None, self._addr())
        assert matched is False
        assert conf == 0.0

    def test_none_addr2(self):
        matched, conf = addresses_match(self._addr(), None)
        assert matched is False
        assert conf == 0.0

    def test_both_none(self):
        matched, conf = addresses_match(None, None)
        assert matched is False

    def test_different_country_no_match(self):
        a1 = self._addr(country="US")
        a2 = self._addr(country="GB")
        matched, conf = addresses_match(a1, a2)
        assert matched is False
        assert conf == 0.0

    def test_exact_street_and_zip_high_conf(self):
        a1 = self._addr()
        a2 = self._addr()
        matched, conf = addresses_match(a1, a2)
        assert matched is True
        assert conf == 0.90

    def test_fuzzy_street_same_zip_medium_conf(self):
        a1 = self._addr(street="123 main street")
        a2 = self._addr(street="123 main st")
        matched, conf = addresses_match(a1, a2)
        assert matched is True
        assert conf == 0.75

    def test_zip_only_no_street_low_conf(self):
        a1 = self._addr(street=None)
        a2 = self._addr(street=None)
        matched, conf = addresses_match(a1, a2)
        assert matched is True
        assert conf == 0.60

    def test_different_zip_no_match(self):
        a1 = self._addr(zip_code="10001")
        a2 = self._addr(zip_code="90210")
        matched, conf = addresses_match(a1, a2)
        assert matched is False

    def test_zip_normalisation_spaces(self):
        # UK postcodes with/without space should still match
        a1 = self._addr(zip_code="SW1A 1AA", country="GB", state=None)
        a2 = self._addr(zip_code="SW1A1AA", country="GB", state=None)
        matched, conf = addresses_match(a1, a2)
        assert matched is True

    def test_completely_different_street_no_match(self):
        a1 = self._addr(street="123 main st")
        a2 = self._addr(street="999 oak avenue")
        matched, conf = addresses_match(a1, a2)
        assert matched is False


# ===========================================================================
# government_ids_match
# ===========================================================================

class TestGovernmentIdsMatch:
    def test_exact_match_same_type(self):
        matched, conf = government_ids_match("SSN", "123-45-6789", "SSN", "123-45-6789")
        assert matched is True
        assert conf == 0.95

    def test_different_type_no_match(self):
        matched, conf = government_ids_match("SSN", "123-45-6789", "DL", "123-45-6789")
        assert matched is False
        assert conf == 0.0

    def test_type_case_insensitive(self):
        matched, conf = government_ids_match("ssn", "123456789", "SSN", "123456789")
        assert matched is True

    def test_one_char_substitution(self):
        matched, conf = government_ids_match("SSN", "123-45-6789", "SSN", "123-45-6780")
        assert matched is True
        assert conf == 0.75

    def test_one_char_deletion(self):
        matched, conf = government_ids_match("DL", "A1234567", "DL", "A123456")
        assert matched is True
        assert conf == 0.75

    def test_one_char_insertion(self):
        matched, conf = government_ids_match("DL", "A123456", "DL", "A1234567")
        assert matched is True
        assert conf == 0.75

    def test_two_char_diff_no_match(self):
        matched, conf = government_ids_match("SSN", "123-45-6789", "SSN", "123-45-6700")
        assert matched is False
        assert conf == 0.0

    def test_empty_type_no_match(self):
        matched, conf = government_ids_match("", "123", "SSN", "123")
        assert matched is False

    def test_completely_different_values(self):
        matched, conf = government_ids_match("SSN", "111-11-1111", "SSN", "999-99-9999")
        assert matched is False


# ===========================================================================
# _edit_distance_one (internal helper)
# ===========================================================================

class TestEditDistanceOne:
    def test_identical(self):
        assert _edit_distance_one("abc", "abc") is False  # 0 edits, not 1

    def test_one_substitution(self):
        assert _edit_distance_one("abc", "axc") is True

    def test_one_deletion(self):
        assert _edit_distance_one("abc", "ac") is True

    def test_one_insertion(self):
        assert _edit_distance_one("ac", "abc") is True

    def test_two_substitutions(self):
        assert _edit_distance_one("abc", "axz") is False

    def test_empty_vs_one_char(self):
        assert _edit_distance_one("", "a") is True

    def test_empty_vs_empty(self):
        assert _edit_distance_one("", "") is False


# ===========================================================================
# normalize_dob
# ===========================================================================

class TestNormalizeDob:
    # --- ISO input (unambiguous) ---
    def test_iso_format_passthrough(self):
        assert normalize_dob("1990-01-15") == "1990-01-15"

    def test_iso_format_us_region(self):
        assert normalize_dob("1990-01-15", "US") == "1990-01-15"

    # --- US (MM/DD/YYYY) ---
    def test_us_slash_format(self):
        assert normalize_dob("01/15/1990", "US") == "1990-01-15"

    def test_us_dash_format(self):
        assert normalize_dob("01-15-1990", "US") == "1990-01-15"

    # --- Non-US (DD/MM/YYYY) ---
    def test_gb_slash_format(self):
        assert normalize_dob("15/01/1990", "GB") == "1990-01-15"

    def test_in_slash_format(self):
        assert normalize_dob("15/01/1990", "IN") == "1990-01-15"

    def test_au_slash_format(self):
        assert normalize_dob("15/01/1990", "AU") == "1990-01-15"

    # --- Named-month formats (unambiguous) ---
    def test_named_month_full(self):
        assert normalize_dob("15 January 1990") == "1990-01-15"

    def test_named_month_abbrev(self):
        assert normalize_dob("15 Jan 1990") == "1990-01-15"

    def test_named_month_us_style(self):
        assert normalize_dob("January 15, 1990") == "1990-01-15"

    def test_named_month_abbrev_us(self):
        assert normalize_dob("Jan 15, 1990") == "1990-01-15"

    # --- YYYY/MM/DD ---
    def test_yyyy_mm_dd_slashes(self):
        assert normalize_dob("1990/01/15") == "1990-01-15"

    # --- Invalid inputs ---
    def test_empty_returns_none(self):
        assert normalize_dob("") is None

    def test_whitespace_only_returns_none(self):
        assert normalize_dob("   ") is None

    def test_invalid_date_returns_none(self):
        assert normalize_dob("32/01/1990", "GB") is None

    def test_nonsense_returns_none(self):
        assert normalize_dob("not a date") is None

    # --- 2-digit year expansion ---
    def test_two_digit_year_2000s(self):
        assert normalize_dob("15/01/90", "GB") == "1990-01-15"

    def test_two_digit_year_1900s(self):
        assert normalize_dob("15/01/30", "GB") == "1930-01-15"


# ===========================================================================
# dobs_match
# ===========================================================================

class TestDobsMatch:
    def test_identical_iso(self):
        matched, conf = dobs_match("1990-01-15", "US", "1990-01-15", "US")
        assert matched is True
        assert conf == 0.95

    def test_same_date_different_formats_us(self):
        matched, conf = dobs_match("01/15/1990", "US", "1990-01-15", "US")
        assert matched is True
        assert conf == 0.95

    def test_same_date_us_vs_gb(self):
        # US: "01/15/1990" = Jan 15; GB: "15/01/1990" = Jan 15
        matched, conf = dobs_match("01/15/1990", "US", "15/01/1990", "GB")
        assert matched is True
        assert conf == 0.95

    def test_different_dates_no_match(self):
        matched, conf = dobs_match("1990-01-15", "US", "1990-02-15", "US")
        assert matched is False
        assert conf == 0.0

    def test_unparseable_date1_no_match(self):
        matched, conf = dobs_match("not a date", "US", "1990-01-15", "US")
        assert matched is False
        assert conf == 0.0

    def test_unparseable_date2_no_match(self):
        matched, conf = dobs_match("1990-01-15", "US", "garbage", "GB")
        assert matched is False
        assert conf == 0.0

    def test_empty_inputs_no_match(self):
        matched, conf = dobs_match("", "US", "", "US")
        assert matched is False
        assert conf == 0.0
