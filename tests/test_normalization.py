"""Tests for the app/normalization package."""
from __future__ import annotations

import pytest

from app.normalization.phone_normalizer import normalize_phone
from app.normalization.email_normalizer import normalize_email
from app.normalization.name_normalizer import normalize_name, is_western_reversed
from app.normalization.address_normalizer import normalize_address, detect_country


# ---------------------------------------------------------------------------
# Phone normalizer
# ---------------------------------------------------------------------------


class TestPhoneNormalizerUSFormats:
    """Five common US formats should all produce the same E.164 string."""

    _E164 = "+12125551234"

    def test_parenthesized_area_code(self) -> None:
        assert normalize_phone("(212) 555-1234") == self._E164

    def test_dashes(self) -> None:
        assert normalize_phone("212-555-1234") == self._E164

    def test_dots(self) -> None:
        assert normalize_phone("212.555.1234") == self._E164

    def test_no_separator(self) -> None:
        assert normalize_phone("2125551234") == self._E164

    def test_international_prefix(self) -> None:
        assert normalize_phone("+1 212 555 1234") == self._E164


class TestPhoneNormalizerInternational:
    def test_india_plus91(self) -> None:
        result = normalize_phone("+91 98765 43210")
        assert result == "+919876543210"

    def test_uk_plus44(self) -> None:
        result = normalize_phone("+44 7911 123456")
        assert result == "+447911123456"


class TestPhoneNormalizerEdgeCases:
    def test_unparseable_string_returns_none(self) -> None:
        assert normalize_phone("not-a-phone") is None

    def test_empty_string_returns_none(self) -> None:
        assert normalize_phone("") is None

    def test_whitespace_only_returns_none(self) -> None:
        assert normalize_phone("   ") is None

    def test_letters_only_returns_none(self) -> None:
        assert normalize_phone("abcdefghij") is None

    def test_too_short_returns_none(self) -> None:
        # 5 digits is not a valid NANP number
        assert normalize_phone("12345") is None

    def test_default_region_us(self) -> None:
        # Without a country prefix, US is assumed
        assert normalize_phone("8005551234") == "+18005551234"

    def test_explicit_region_gb(self) -> None:
        # UK local format resolved with explicit region
        result = normalize_phone("07911 123456", default_region="GB")
        assert result == "+447911123456"

    def test_returns_none_never_raises(self) -> None:
        # Garbage input must never raise
        result = normalize_phone("!!!###$$$")
        assert result is None


# ---------------------------------------------------------------------------
# Email normalizer
# ---------------------------------------------------------------------------


class TestEmailNormalizerBasic:
    def test_already_lowercase(self) -> None:
        assert normalize_email("user@example.com") == "user@example.com"

    def test_uppercase_lowercased(self) -> None:
        assert normalize_email("User@Example.COM") == "user@example.com"

    def test_leading_trailing_whitespace_stripped(self) -> None:
        assert normalize_email("  user@example.com  ") == "user@example.com"

    def test_empty_string_returns_empty(self) -> None:
        assert normalize_email("") == ""

    def test_whitespace_only_returns_empty(self) -> None:
        assert normalize_email("   ") == ""


class TestEmailNormalizerGmail:
    """Gmail dot variants must all collapse to the same canonical address."""

    _CANONICAL = "john@gmail.com"

    def test_dots_in_local_part_removed(self) -> None:
        assert normalize_email("j.o.h.n@gmail.com") == self._CANONICAL

    def test_single_dot_removed(self) -> None:
        assert normalize_email("jo.hn@gmail.com") == self._CANONICAL

    def test_already_no_dots(self) -> None:
        assert normalize_email("john@gmail.com") == self._CANONICAL

    def test_uppercase_gmail_normalized(self) -> None:
        assert normalize_email("J.O.H.N@GMAIL.COM") == self._CANONICAL

    def test_googlemail_domain_also_normalized(self) -> None:
        assert normalize_email("j.o.h.n@googlemail.com") == "john@googlemail.com"

    def test_plus_tag_preserved(self) -> None:
        # Sub-address tags are kept — dots before the + are still removed
        assert normalize_email("j.o.h.n+tag@gmail.com") == "john+tag@gmail.com"


class TestEmailNormalizerNonGmail:
    """Non-Gmail domains: only lowercase + strip, no dot removal."""

    def test_dots_preserved_for_other_domain(self) -> None:
        assert normalize_email("j.o.h.n@outlook.com") == "j.o.h.n@outlook.com"

    def test_dots_preserved_for_yahoo(self) -> None:
        assert normalize_email("first.last@yahoo.com") == "first.last@yahoo.com"

    def test_uppercase_lowercased(self) -> None:
        assert normalize_email("First.Last@Company.org") == "first.last@company.org"

    def test_subdomain_preserved(self) -> None:
        assert normalize_email("user@mail.company.co.uk") == "user@mail.company.co.uk"


# ---------------------------------------------------------------------------
# Name normalizer
# ---------------------------------------------------------------------------


class TestNameNormalizerReversed:
    def test_lowercase_reversed(self) -> None:
        assert normalize_name("smith, john") == "John Smith"

    def test_mixed_case_reversed(self) -> None:
        assert normalize_name("SMITH, JOHN") == "John Smith"

    def test_apostrophe_reversed(self) -> None:
        assert normalize_name("O'Brien, Mary") == "Mary O'Brien"

    def test_reversed_with_middle_name(self) -> None:
        assert normalize_name("Smith, John Michael") == "John Michael Smith"


class TestNameNormalizerHonorifics:
    def test_dr_with_period(self) -> None:
        assert normalize_name("DR. Jane Smith") == "Jane Smith"

    def test_dr_without_period(self) -> None:
        assert normalize_name("Dr Jane Smith") == "Jane Smith"

    def test_mr_stripped(self) -> None:
        assert normalize_name("Mr. John Smith") == "John Smith"

    def test_mrs_stripped(self) -> None:
        assert normalize_name("Mrs. Jane Smith") == "Jane Smith"

    def test_ms_stripped(self) -> None:
        assert normalize_name("Ms Jane Smith") == "Jane Smith"

    def test_prof_stripped(self) -> None:
        assert normalize_name("Prof. Alan Turing") == "Alan Turing"


class TestNameNormalizerWhitespace:
    def test_internal_whitespace_collapsed(self) -> None:
        assert normalize_name("  John   Smith  ") == "John Smith"

    def test_empty_string_returns_empty(self) -> None:
        assert normalize_name("") == ""

    def test_whitespace_only_returns_empty(self) -> None:
        assert normalize_name("   ") == ""


class TestNameNormalizerTitleCase:
    def test_all_lowercase_title_cased(self) -> None:
        assert normalize_name("jane doe") == "Jane Doe"

    def test_all_uppercase_title_cased(self) -> None:
        assert normalize_name("JANE DOE") == "Jane Doe"

    def test_apostrophe_preserved_in_title_case(self) -> None:
        # Python title() correctly handles O'Brien
        result = normalize_name("mary o'brien")
        assert result == "Mary O'Brien"


# ---------------------------------------------------------------------------
# Address normalizer
# ---------------------------------------------------------------------------


class TestAddressNormalizerBasic:
    def test_standard_us_address(self) -> None:
        result = normalize_address("123 Main St, Springfield, CA 90210")
        assert result is not None
        assert result["street"] == "123 main st"
        assert result["city"] == "springfield"
        assert result["state"] == "CA"
        assert result["zip"] == "90210"
        assert result["country"] == "US"

    def test_full_state_name_normalized_to_abbrev(self) -> None:
        result = normalize_address("123 Main St, Springfield, California 90210")
        assert result is not None
        assert result["state"] == "CA"
        assert result["city"] == "springfield"

    def test_zip_plus4_stripped_to_5_digits(self) -> None:
        result = normalize_address("123 Main St, Springfield, CA 90210-1234")
        assert result is not None
        assert result["zip"] == "90210"

    def test_full_state_name_with_zip_plus4(self) -> None:
        result = normalize_address("123 Main St, Springfield, California 90210-1234")
        assert result is not None
        assert result["state"] == "CA"
        assert result["zip"] == "90210"


class TestAddressNormalizerStates:
    def test_texas_abbreviation(self) -> None:
        result = normalize_address("456 Oak Ave, Austin, TX 78701")
        assert result is not None
        assert result["state"] == "TX"

    def test_new_york_full_name(self) -> None:
        result = normalize_address("456 Oak Ave, Albany, New York 10001")
        assert result is not None
        assert result["state"] == "NY"
        assert result["city"] == "albany"

    def test_new_york_abbreviation(self) -> None:
        result = normalize_address("1 Broadway, New York City, NY 10004")
        assert result is not None
        assert result["state"] == "NY"


class TestAddressNormalizerNone:
    def test_unrecognizable_string_returns_none(self) -> None:
        assert normalize_address("hello world") is None

    def test_empty_string_returns_none(self) -> None:
        assert normalize_address("") is None

    def test_whitespace_only_returns_none(self) -> None:
        assert normalize_address("   ") is None

    def test_letters_only_no_digits_returns_none(self) -> None:
        assert normalize_address("Main Street Springfield California") is None


class TestAddressNormalizerAllValuesLowercased:
    def test_street_is_lowercased(self) -> None:
        result = normalize_address("123 MAIN ST, Springfield, CA 90210")
        assert result is not None
        assert result["street"] == "123 main st"

    def test_city_is_lowercased(self) -> None:
        result = normalize_address("123 Main St, SPRINGFIELD, CA 90210")
        assert result is not None
        assert result["city"] == "springfield"

    def test_country_us_iso_code(self) -> None:
        result = normalize_address("123 Main St, Springfield, CA 90210")
        assert result is not None
        assert result["country"] == "US"


# ---------------------------------------------------------------------------
# Multi-geography: is_western_reversed
# ---------------------------------------------------------------------------


class TestIsWesternReversed:
    def test_simple_western_name_true(self) -> None:
        assert is_western_reversed("Smith, John") is True

    def test_apostrophe_surname_true(self) -> None:
        assert is_western_reversed("O'Brien, Mary") is True

    def test_with_middle_name_true(self) -> None:
        assert is_western_reversed("Smith, John Michael") is True

    def test_city_country_false(self) -> None:
        assert is_western_reversed("Mumbai, India") is False

    def test_address_with_digit_false(self) -> None:
        assert is_western_reversed("123, Main St") is False

    def test_no_comma_false(self) -> None:
        assert is_western_reversed("Zhang Wei") is False

    def test_two_commas_false(self) -> None:
        assert is_western_reversed("Smith, John, Jr") is False

    def test_known_city_false(self) -> None:
        assert is_western_reversed("London, England") is False


# ---------------------------------------------------------------------------
# Multi-geography: expanded honorifics
# ---------------------------------------------------------------------------


class TestNameHonorificsExpanded:
    def test_indian_smt_with_period(self) -> None:
        assert normalize_name("Smt. Priya Sharma") == "Priya Sharma"

    def test_indian_smt_without_period(self) -> None:
        assert normalize_name("Smt Priya Sharma") == "Priya Sharma"

    def test_indian_shri(self) -> None:
        assert normalize_name("Shri Ramesh Kumar") == "Ramesh Kumar"

    def test_indian_sri(self) -> None:
        assert normalize_name("Sri Venkat Rao") == "Venkat Rao"

    def test_german_herr(self) -> None:
        assert normalize_name("Herr Klaus Müller") == "Klaus Müller"

    def test_german_frau(self) -> None:
        assert normalize_name("Frau Anna Weber") == "Anna Weber"

    def test_french_mme(self) -> None:
        assert normalize_name("Mme. Claire Dupont") == "Claire Dupont"

    def test_spanish_srta(self) -> None:
        assert normalize_name("Srta. Maria García") == "Maria García"

    def test_universal_sir(self) -> None:
        assert normalize_name("Sir Arthur Conan") == "Arthur Conan"

    def test_universal_lord(self) -> None:
        assert normalize_name("Lord Byron") == "Byron"


# ---------------------------------------------------------------------------
# Multi-geography: non-Latin pass-through
# ---------------------------------------------------------------------------


class TestNameNonLatin:
    def test_chinese_characters_pass_through(self) -> None:
        assert normalize_name("张伟") == "张伟"

    def test_arabic_characters_pass_through(self) -> None:
        assert normalize_name("محمد علي") == "محمد علي"

    def test_devanagari_characters_pass_through(self) -> None:
        assert normalize_name("राम प्रसाद") == "राम प्रसाद"

    def test_latin_transliteration_title_cased(self) -> None:
        # "Zhang Wei" in Latin script — title-case applied normally
        assert normalize_name("Zhang Wei") == "Zhang Wei"

    def test_non_latin_whitespace_collapsed(self) -> None:
        assert normalize_name("张   伟") == "张 伟"

    def test_mumbai_india_not_reversed(self) -> None:
        # Location string must not be treated as reversed personal name
        assert normalize_name("Mumbai, India") == "Mumbai, India"


# ---------------------------------------------------------------------------
# Multi-geography: detect_country
# ---------------------------------------------------------------------------


class TestDetectCountry:
    def test_india_keyword(self) -> None:
        assert detect_country("Mumbai, Maharashtra 400001, India") == "IN"

    def test_uk_keyword(self) -> None:
        assert detect_country("London SW1A 1AA, UK") == "GB"

    def test_united_kingdom_keyword(self) -> None:
        assert detect_country("10 Downing St, London, United Kingdom") == "GB"

    def test_canada_keyword(self) -> None:
        assert detect_country("123 King St, Toronto, Canada") == "CA"

    def test_canada_postal_no_keyword(self) -> None:
        assert detect_country("123 Main St, Toronto ON M5V 3L9") == "CA"

    def test_india_pin_no_keyword(self) -> None:
        # 6-digit PIN starting with 1–9 identifies India
        assert detect_country("Andheri, Mumbai 400069") == "IN"

    def test_us_zip(self) -> None:
        assert detect_country("123 Main St, Springfield 90210") == "US"

    def test_us_state_abbreviation(self) -> None:
        assert detect_country("123 Main St, Springfield, CA") == "US"

    def test_us_state_full_name(self) -> None:
        assert detect_country("123 Main St, Springfield, California") == "US"

    def test_no_signal_returns_none(self) -> None:
        assert detect_country("hello world") is None


# ---------------------------------------------------------------------------
# Multi-geography: normalize_address postal codes
# ---------------------------------------------------------------------------


class TestAddressNormalizerMultiGeo:
    def test_uk_postcode_no_space(self) -> None:
        result = normalize_address("10 Downing St, London SW1A 2AA, UK")
        assert result is not None
        assert result["zip"] == "SW1A2AA"
        assert result["country"] == "GB"

    def test_uk_postcode_uppercase(self) -> None:
        result = normalize_address("1 Whitehall, London sw1a 1aa, UK")
        assert result is not None
        assert result["zip"] == "SW1A1AA"

    def test_indian_pin_6_digits(self) -> None:
        result = normalize_address("Andheri East, Mumbai 400069, India")
        assert result is not None
        assert result["zip"] == "400069"
        assert result["country"] == "IN"

    def test_canadian_postal_no_space(self) -> None:
        result = normalize_address("123 Main St, Toronto ON M5V 3L9")
        assert result is not None
        assert result["zip"] == "M5V3L9"
        assert result["country"] == "CA"

    def test_canadian_postal_uppercase(self) -> None:
        result = normalize_address("123 Main St, Toronto ON m5v 3l9")
        assert result is not None
        assert result["zip"] == "M5V3L9"

    def test_india_country_detected(self) -> None:
        result = normalize_address("Mumbai, Maharashtra 400001, India")
        assert result is not None
        assert result["country"] == "IN"
        assert result["zip"] == "400001"

    def test_uk_country_detected(self) -> None:
        result = normalize_address("London SW1A 1AA, UK")
        assert result is not None
        assert result["country"] == "GB"

    def test_canada_country_detected(self) -> None:
        result = normalize_address("123 Main St, Toronto ON M5V 3L9")
        assert result is not None
        assert result["country"] == "CA"

    def test_non_us_no_state_normalization(self) -> None:
        result = normalize_address("123 Main St, Toronto ON M5V 3L9")
        assert result is not None
        assert result["state"] is None

    def test_us_still_gets_state(self) -> None:
        result = normalize_address("123 Main St, Springfield, California 90210")
        assert result is not None
        assert result["state"] == "CA"
        assert result["country"] == "US"

    def test_unrecognizable_still_none(self) -> None:
        assert normalize_address("hello world") is None
