"""Tests for app/pii/layer1_patterns.py and app/pii/presidio_engine.py.

presidio_analyzer and spaCy are injected into sys.modules before any imports
so that missing packages never cause ImportError in the test environment.

layer1_patterns.py tests (no mocking needed — pure Python):
  - PatternDefinition has all 6 required fields
  - get_all_patterns() with no args returns all patterns
  - get_all_patterns(["US","GLOBAL"]) excludes IN/UK/EU/CA/AU
  - get_all_patterns(["IN","GLOBAL"]) includes Aadhaar and PAN
  - get_pattern_geographies() returns sorted list of all geography codes
  - luhn_check passes for well-known valid card numbers
  - luhn_check fails for invalid numbers
  - luhn_check strips spaces/dashes before checking
  - Each GLOBAL pattern matches its canonical input
  - Each country-specific pattern matches its canonical input
  - Aadhaar: first digit 1 → no match (UIDAI spec)
  - PAN: "12345ABCDE" → no match (starts with digits)
  - NI: "BG123456A" → no match (BG in exclusion list)
  - Codice Fiscale: "RSSMRA85T10A562S" matches
  - Each pattern does NOT match obviously wrong input

presidio_engine.py tests (Presidio and spaCy fully mocked):
  - PresidioEngine.analyze() returns list[DetectionResult]
  - score >= 0.75 → needs_layer2=False
  - score < 0.75 → needs_layer2=True
  - DetectionResult carries geography and regulatory_framework from pattern map
  - extraction_layer == "layer_1_pattern" on every result
  - No raw PII values appear in log output during analysis
  - Empty block list → empty result list
  - Multiple blocks → results from all blocks combined
"""
from __future__ import annotations

import re
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub Presidio and spaCy BEFORE importing presidio_engine
# ---------------------------------------------------------------------------
_PA_STUB = MagicMock(name="presidio_analyzer")
_PA_NLP_STUB = MagicMock(name="presidio_analyzer.nlp_engine")
_PA_PAT_STUB = MagicMock(name="presidio_analyzer.pattern")
_PA_PR_STUB = MagicMock(name="presidio_analyzer.pattern_recognizer")
_SPACY_STUB = MagicMock(name="spacy")

for _mod, _stub in [
    ("presidio_analyzer", _PA_STUB),
    ("presidio_analyzer.nlp_engine", _PA_NLP_STUB),
    ("presidio_analyzer.pattern", _PA_PAT_STUB),
    ("presidio_analyzer.pattern_recognizer", _PA_PR_STUB),
    ("spacy", _SPACY_STUB),
]:
    sys.modules.setdefault(_mod, _stub)

from app.pii.layer1_patterns import (  # noqa: E402
    CUSTOM_PATTERNS,
    GEOGRAPHY_AU,
    GEOGRAPHY_CA,
    GEOGRAPHY_EU,
    GEOGRAPHY_GLOBAL,
    GEOGRAPHY_IN,
    GEOGRAPHY_UK,
    GEOGRAPHY_US,
    PatternDefinition,
    get_all_patterns,
    get_pattern_geographies,
    luhn_check,
)
from app.pii.presidio_engine import DetectionResult, PresidioEngine  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pattern(entity_type: str) -> PatternDefinition:
    """Find a pattern by entity_type; raises if not found."""
    result = next((p for p in CUSTOM_PATTERNS if p.entity_type == entity_type), None)
    assert result is not None, f"No pattern with entity_type={entity_type!r}"
    return result


def _matches(entity_type: str, text: str) -> bool:
    """Return True if the pattern's regex matches anywhere in text."""
    pat = _pattern(entity_type)
    return bool(re.search(pat.regex, text))


def _make_block(text: str = "test text") -> MagicMock:
    from app.readers.base import ExtractedBlock
    return ExtractedBlock(
        text=text,
        page_or_sheet=0,
        source_path="test.pdf",
        file_type="pdf",
    )


def _make_presidio_hit(entity_type: str, start: int, end: int, score: float):
    hit = MagicMock()
    hit.entity_type = entity_type
    hit.start = start
    hit.end = end
    hit.score = score
    return hit


# ---------------------------------------------------------------------------
# 1. PatternDefinition dataclass
# ---------------------------------------------------------------------------

def test_pattern_definition_has_six_fields():
    fields = {f.name for f in PatternDefinition.__dataclass_fields__.values()}
    assert fields == {"name", "entity_type", "regex", "score", "geography", "regulatory_framework"}


def test_pattern_definition_is_dataclass():
    from dataclasses import is_dataclass
    assert is_dataclass(PatternDefinition)


def test_pattern_definition_instantiation():
    pd = PatternDefinition(
        name="test",
        entity_type="TEST",
        regex=r"\btest\b",
        score=0.80,
        geography=GEOGRAPHY_GLOBAL,
        regulatory_framework="TEST-REG",
    )
    assert pd.score == 0.80
    assert pd.geography == GEOGRAPHY_GLOBAL


# ---------------------------------------------------------------------------
# 2. Geography constants
# ---------------------------------------------------------------------------

def test_geography_constants():
    assert GEOGRAPHY_GLOBAL == "GLOBAL"
    assert GEOGRAPHY_US == "US"
    assert GEOGRAPHY_IN == "IN"
    assert GEOGRAPHY_UK == "UK"
    assert GEOGRAPHY_EU == "EU"
    assert GEOGRAPHY_CA == "CA"
    assert GEOGRAPHY_AU == "AU"


def test_all_geographies_present_in_patterns():
    geos = {p.geography for p in CUSTOM_PATTERNS}
    for expected in {GEOGRAPHY_GLOBAL, GEOGRAPHY_US, GEOGRAPHY_IN,
                     GEOGRAPHY_UK, GEOGRAPHY_EU, GEOGRAPHY_CA, GEOGRAPHY_AU}:
        assert expected in geos, f"No pattern with geography={expected!r}"


# ---------------------------------------------------------------------------
# 3. get_all_patterns() filtering
# ---------------------------------------------------------------------------

def test_get_all_patterns_no_args_returns_all():
    assert get_all_patterns() == CUSTOM_PATTERNS


def test_get_all_patterns_none_returns_all():
    assert get_all_patterns(None) == CUSTOM_PATTERNS


def test_get_all_patterns_us_global_excludes_other_geos():
    result = get_all_patterns(["US", "GLOBAL"])
    for p in result:
        assert p.geography in {"US", "GLOBAL"}, f"Unexpected geography: {p.geography}"


def test_get_all_patterns_us_global_includes_global():
    result = get_all_patterns(["US", "GLOBAL"])
    global_types = {p.entity_type for p in result if p.geography == "GLOBAL"}
    assert "EMAIL" in global_types


def test_get_all_patterns_us_global_includes_us():
    result = get_all_patterns(["US", "GLOBAL"])
    us_types = {p.entity_type for p in result if p.geography == "US"}
    assert "SSN" in us_types


def test_get_all_patterns_us_global_excludes_in():
    result = get_all_patterns(["US", "GLOBAL"])
    geos = {p.geography for p in result}
    assert "IN" not in geos


def test_get_all_patterns_in_global_includes_aadhaar():
    result = get_all_patterns(["IN", "GLOBAL"])
    types = {p.entity_type for p in result}
    assert "AADHAAR" in types


def test_get_all_patterns_in_global_includes_pan():
    result = get_all_patterns(["IN", "GLOBAL"])
    types = {p.entity_type for p in result}
    assert "PAN" in types


def test_get_all_patterns_in_global_excludes_us():
    result = get_all_patterns(["IN", "GLOBAL"])
    geos = {p.geography for p in result}
    assert "US" not in geos


def test_get_all_patterns_global_only():
    result = get_all_patterns(["GLOBAL"])
    for p in result:
        assert p.geography == "GLOBAL"


def test_get_pattern_geographies_returns_all_seven():
    geos = get_pattern_geographies()
    assert set(geos) == {"GLOBAL", "US", "IN", "UK", "EU", "CA", "AU"}


def test_get_pattern_geographies_is_sorted():
    geos = get_pattern_geographies()
    assert geos == sorted(geos)


# ---------------------------------------------------------------------------
# 4. luhn_check
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("card_number", [
    "4111111111111111",   # Visa test number
    "4532015112830366",   # Visa
    "5425233430109903",   # Mastercard
    "371449635398431",    # Amex (15 digits)
    "6011111111111117",   # Discover
])
def test_luhn_check_valid_cards(card_number):
    assert luhn_check(card_number) is True


@pytest.mark.parametrize("card_number", [
    "4111111111111112",   # last digit changed
    "1234567890123456",   # random digits
    "0000000000000001",   # fails Luhn
])
def test_luhn_check_invalid_cards(card_number):
    assert luhn_check(card_number) is False


def test_luhn_check_strips_spaces():
    # Same as "4111111111111111" with spaces
    assert luhn_check("4111 1111 1111 1111") is True


def test_luhn_check_strips_dashes():
    assert luhn_check("4111-1111-1111-1111") is True


def test_luhn_check_empty_string():
    assert luhn_check("") is False


def test_luhn_check_no_digits():
    assert luhn_check("ABCD-EFGH") is False


# ---------------------------------------------------------------------------
# 5. GLOBAL pattern — match / no-match
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("entity_type,good,bad", [
    ("EMAIL",             "user@example.com",              "not-an-email"),
    ("PHONE_INTL",        "+1 (555) 123-4567",             "12345"),
    ("CREDIT_CARD",       "4111111111111111",              "1234"),
    ("IBAN",              "GB29NWBK60161331926819",        "NOTIBAN"),
    ("IPV4",              "192.168.1.1",                   "999.999.999.999"),
    ("IPV6",              "2001:0db8:85a3:0000:0000:8a2e:0370:7334", "not:ipv6"),
    ("DATE_OF_BIRTH_DMY", "15/06/1985",                   "99/99/9999"),
    ("DATE_OF_BIRTH_MDY", "06/15/1985",                   "99/99/9999"),
    ("DATE_OF_BIRTH_ISO", "1985-06-15",                   "abcd-ef-gh"),
    ("GPS_COORDINATES",   "51.5074,0.1278",               "999,999"),
    ("PASSPORT_ICAO",     "P1234567",                     "12345"),
])
def test_global_pattern_match(entity_type, good, bad):
    assert _matches(entity_type, good), f"{entity_type}: expected match on {good!r}"
    assert not _matches(entity_type, bad), f"{entity_type}: expected no match on {bad!r}"


# ---------------------------------------------------------------------------
# 6. US patterns
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("entity_type,good,bad", [
    ("SSN",                    "123-45-6789",   "123456789"),
    ("PHONE_US",               "(555) 867-5309", "123"),
    ("EIN",                    "12-3456789",    "123456789"),
    ("MEDICARE_BENEFICIARY_ID","1AB1CD1EF23",   "123456"),
])
def test_us_pattern_match(entity_type, good, bad):
    assert _matches(entity_type, good), f"{entity_type}: expected match on {good!r}"
    assert not _matches(entity_type, bad), f"{entity_type}: expected no match on {bad!r}"


def test_ssn_nodash_matches_nine_consecutive_digits():
    # Valid SSN range — not starting with 000, 666, or 9xx
    assert _matches("SSN_NODASH", "123456789")


def test_ssn_nodash_excludes_000_prefix():
    assert not _matches("SSN_NODASH", "000123456")


def test_ssn_nodash_excludes_666_prefix():
    assert not _matches("SSN_NODASH", "666123456")


def test_ssn_nodash_excludes_900_series():
    assert not _matches("SSN_NODASH", "900123456")


# ---------------------------------------------------------------------------
# 7. India patterns
# ---------------------------------------------------------------------------

def test_aadhaar_matches_valid():
    # First digit 2 ✓
    assert _matches("AADHAAR", "2345 6789 0123")


def test_aadhaar_rejects_first_digit_one():
    # First digit 1 is never issued by UIDAI
    assert not _matches("AADHAAR", "1234 5678 9012")


def test_aadhaar_matches_hyphenated():
    assert _matches("AADHAAR", "2345-6789-0123")


def test_pan_matches_valid():
    # 4th char 'P' (Person) is in [ABCFGHLJPTF] ✓
    assert _matches("PAN", "ABCPE1234F")


def test_pan_rejects_leading_digits():
    assert not _matches("PAN", "12345ABCDE")


def test_mobile_in_matches():
    assert _matches("MOBILE_IN", "9876543210")
    assert _matches("MOBILE_IN", "+91 9876543210")


def test_gst_matches():
    assert _matches("GST_NUMBER", "29ABCDE1234F1Z5")


@pytest.mark.parametrize("entity_type,good,bad", [
    ("PASSPORT_IN",      "A1234567",  "1ABCDEF"),
    ("VOTER_ID_IN",      "ABC1234567", "12345"),
])
def test_in_pattern_match(entity_type, good, bad):
    assert _matches(entity_type, good), f"{entity_type}: expected match on {good!r}"
    assert not _matches(entity_type, bad), f"{entity_type}: expected no match on {bad!r}"


# ---------------------------------------------------------------------------
# 8. UK patterns
# ---------------------------------------------------------------------------

def test_ni_uk_matches_valid():
    assert _matches("NATIONAL_INSURANCE_UK", "AB123456C")


def test_ni_uk_rejects_bg_prefix():
    assert not _matches("NATIONAL_INSURANCE_UK", "BG123456A")


def test_ni_uk_rejects_gb_prefix():
    assert not _matches("NATIONAL_INSURANCE_UK", "GB123456A")


def test_nhs_number_matches():
    assert _matches("NHS_NUMBER", "485 777 3456")


def test_sort_code_matches():
    assert _matches("SORT_CODE_UK", "20-12-34")


# ---------------------------------------------------------------------------
# 9. EU patterns
# ---------------------------------------------------------------------------

def test_codice_fiscale_matches():
    # Well-known test value: Rossi Mario born 10 Oct 1985 in Milan
    assert _matches("CODICE_FISCALE_IT", "RSSMRA85T10A562S")


def test_dni_nie_es_matches_dni():
    # DNI: 8 digits + letter from [A-HJ-NP-TV-Z]
    assert _matches("DNI_NIE_ES", "12345678Z")


def test_dni_nie_es_matches_nie():
    # NIE: X/Y/Z + 7 digits + letter
    assert _matches("DNI_NIE_ES", "X1234567Z")


@pytest.mark.parametrize("entity_type,good,bad", [
    ("NATIONAL_ID_DE",    "L2AB12345C",    "12345"),
    ("INSEE_FR",          "1 85 10 75 123 456 78", "NOTINSEE"),
])
def test_eu_pattern_match(entity_type, good, bad):
    assert _matches(entity_type, good), f"{entity_type}: expected match on {good!r}"
    assert not _matches(entity_type, bad), f"{entity_type}: expected no match on {bad!r}"


# ---------------------------------------------------------------------------
# 10. Canada patterns
# ---------------------------------------------------------------------------

def test_sin_ca_matches():
    assert _matches("SIN_CA", "123 456 789")
    assert _matches("SIN_CA", "123-456-789")


def test_passport_ca_matches():
    assert _matches("PASSPORT_CA", "AB123456")


# ---------------------------------------------------------------------------
# 11. Australia patterns
# ---------------------------------------------------------------------------

def test_medicare_au_matches():
    assert _matches("MEDICARE_AU", "2123456701-1")
    assert _matches("MEDICARE_AU", "2123456701/1")


def test_abn_au_matches():
    assert _matches("ABN_AU", "51 824 753 556")
    assert _matches("ABN_AU", "51824753556")


def test_passport_au_matches():
    assert _matches("PASSPORT_AU", "N12345678")


def test_tfn_au_matches():
    assert _matches("TAX_FILE_NUMBER_AU", "123 456 789")


# ---------------------------------------------------------------------------
# 12. PresidioEngine — mocked Presidio
# ---------------------------------------------------------------------------

def _make_engine_with_hits(hits: list) -> PresidioEngine:
    """Return a PresidioEngine whose Presidio analyzer is fully mocked."""
    with (
        patch("app.pii.presidio_engine.NlpEngineProvider") as MockProvider,
        patch("app.pii.presidio_engine.RecognizerRegistry") as MockRegistry,
        patch("app.pii.presidio_engine.PatternRecognizer"),
        patch("app.pii.presidio_engine.Pattern"),
        patch("app.pii.presidio_engine.AnalyzerEngine") as MockAnalyzer,
    ):
        MockProvider.return_value.create_engine.return_value = MagicMock()
        mock_analyzer_instance = MagicMock()
        mock_analyzer_instance.analyze.return_value = hits
        MockAnalyzer.return_value = mock_analyzer_instance
        engine = PresidioEngine()
    return engine


def test_analyze_returns_list():
    engine = _make_engine_with_hits([_make_presidio_hit("SSN", 0, 11, 0.90)])
    results = engine.analyze([_make_block("123-45-6789")])
    assert isinstance(results, list)


def test_analyze_returns_detection_results():
    engine = _make_engine_with_hits([_make_presidio_hit("SSN", 0, 11, 0.90)])
    results = engine.analyze([_make_block("123-45-6789")])
    assert all(isinstance(r, DetectionResult) for r in results)


def test_analyze_high_score_needs_layer2_false():
    engine = _make_engine_with_hits([_make_presidio_hit("SSN", 0, 11, 0.90)])
    results = engine.analyze([_make_block("123-45-6789")])
    assert results[0].needs_layer2 is False


def test_analyze_low_score_needs_layer2_true():
    engine = _make_engine_with_hits([_make_presidio_hit("SSN_NODASH", 0, 9, 0.60)])
    results = engine.analyze([_make_block("123456789")])
    assert results[0].needs_layer2 is True


def test_analyze_score_exactly_075_is_not_layer2():
    engine = _make_engine_with_hits([_make_presidio_hit("PHONE_INTL", 0, 15, 0.75)])
    results = engine.analyze([_make_block("+1 555 123 4567")])
    assert results[0].needs_layer2 is False


def test_analyze_score_just_below_075_is_layer2():
    engine = _make_engine_with_hits([_make_presidio_hit("PASSPORT_UK", 0, 9, 0.74)])
    results = engine.analyze([_make_block("123456789")])
    assert results[0].needs_layer2 is True


def test_analyze_carries_entity_type():
    engine = _make_engine_with_hits([_make_presidio_hit("EMAIL", 0, 17, 0.85)])
    results = engine.analyze([_make_block("user@example.com")])
    assert results[0].entity_type == "EMAIL"


def test_analyze_carries_start_end():
    engine = _make_engine_with_hits([_make_presidio_hit("EMAIL", 5, 22, 0.85)])
    results = engine.analyze([_make_block("From: user@example.com")])
    assert results[0].start == 5
    assert results[0].end == 22


def test_analyze_extraction_layer_is_layer1():
    engine = _make_engine_with_hits([_make_presidio_hit("SSN", 0, 11, 0.90)])
    results = engine.analyze([_make_block("123-45-6789")])
    assert results[0].extraction_layer == "layer_1_pattern"


def test_analyze_carries_geography_from_pattern_map():
    """entity_type 'SSN' maps to geography 'US' via the pattern map."""
    engine = _make_engine_with_hits([_make_presidio_hit("SSN", 0, 11, 0.90)])
    results = engine.analyze([_make_block("123-45-6789")])
    assert results[0].geography == "US"


def test_analyze_carries_regulatory_framework():
    engine = _make_engine_with_hits([_make_presidio_hit("SSN", 0, 11, 0.90)])
    results = engine.analyze([_make_block("123-45-6789")])
    assert "HIPAA" in results[0].regulatory_framework


def test_analyze_unknown_entity_type_uses_global_geography():
    """Presidio built-in results (not in our pattern_map) get GLOBAL geography."""
    engine = _make_engine_with_hits([_make_presidio_hit("PERSON", 0, 5, 0.85)])
    results = engine.analyze([_make_block("Alice")])
    assert results[0].geography == "GLOBAL"


def test_analyze_empty_blocks_returns_empty():
    engine = _make_engine_with_hits([])
    assert engine.analyze([]) == []


def test_analyze_multiple_blocks_combined():
    hits = [_make_presidio_hit("EMAIL", 0, 16, 0.85)]
    engine = _make_engine_with_hits(hits)
    blocks = [_make_block("a@b.com"), _make_block("c@d.com")]
    results = engine.analyze(blocks)
    assert len(results) == 2  # one hit per block


def test_analyze_block_reference_preserved():
    engine = _make_engine_with_hits([_make_presidio_hit("SSN", 0, 11, 0.90)])
    block = _make_block("123-45-6789")
    results = engine.analyze([block])
    assert results[0].block is block


# ---------------------------------------------------------------------------
# 13. Safety — no raw PII in log output
# ---------------------------------------------------------------------------

def test_analyze_does_not_log_raw_text(caplog):
    import logging
    engine = _make_engine_with_hits([_make_presidio_hit("SSN", 0, 11, 0.90)])
    with caplog.at_level(logging.DEBUG, logger="app.pii.presidio_engine"):
        engine.analyze([_make_block("123-45-6789")])
    for record in caplog.records:
        assert "123-45-6789" not in record.getMessage()


def test_analyze_logs_entity_type_not_value(caplog):
    import logging
    engine = _make_engine_with_hits([_make_presidio_hit("SSN", 0, 11, 0.90)])
    with caplog.at_level(logging.DEBUG, logger="app.pii.presidio_engine"):
        engine.analyze([_make_block("123-45-6789")])
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "SSN" in messages


# ---------------------------------------------------------------------------
# 14. DetectionResult dataclass contracts
# ---------------------------------------------------------------------------

def test_detection_result_needs_layer2_not_init_param():
    """needs_layer2 must be computed from score, not set by caller."""
    import dataclasses
    init_fields = {f.name for f in dataclasses.fields(DetectionResult) if f.init}
    assert "needs_layer2" not in init_fields


def test_detection_result_extraction_layer_default():
    block = _make_block()
    r = DetectionResult(
        block=block,
        entity_type="SSN",
        start=0,
        end=11,
        score=0.90,
        pattern_used=r"\b\d{3}-\d{2}-\d{4}\b",
        geography="US",
        regulatory_framework="HIPAA/CCPA",
    )
    assert r.extraction_layer == "layer_1_pattern"
    assert r.needs_layer2 is False


def test_detection_result_low_score_post_init():
    block = _make_block()
    r = DetectionResult(
        block=block,
        entity_type="PASSPORT_UK",
        start=0,
        end=9,
        score=0.60,
        pattern_used=r"\b\d{9}\b",
        geography="UK",
        regulatory_framework="UK-GDPR",
    )
    assert r.needs_layer2 is True


# ---------------------------------------------------------------------------
# 11. PHI patterns (HIPAA)
# ---------------------------------------------------------------------------

def test_mrn_matches_with_label():
    assert _matches("MRN", "MRN: 123456")


def test_mrn_requires_label_prefix():
    # Without "MRN" prefix the pattern does not fire
    assert not _matches("MRN", "123456")


def test_mrn_label_with_hash():
    assert _matches("MRN", "MRN#7890123")


def test_dea_matches_valid():
    # 2 uppercase letters + exactly 7 digits
    assert _matches("DEA_NUMBER", "AB1234567")


def test_dea_rejects_too_short():
    assert not _matches("DEA_NUMBER", "AB123")


def test_dea_rejects_lowercase_letters():
    assert not _matches("DEA_NUMBER", "ab1234567")


def test_hicn_matches_nine_digits_plus_letter():
    assert _matches("HICN", "123456789A")


def test_hicn_rejects_all_digits():
    # 10 digits alone should not match (no trailing letter)
    assert not re.fullmatch(
        _get_regex("HICN"), "1234567890"
    )


def test_health_plan_beneficiary_matches():
    assert _matches("HEALTH_PLAN_BENEFICIARY", "HPAB12345678")


def test_health_plan_beneficiary_rejects_short():
    assert not _matches("HEALTH_PLAN_BENEFICIARY", "HPABC")


@pytest.mark.parametrize("code,should_match", [
    ("Z87.39",  True),   # valid ICD-10 with decimal
    ("A00",     True),   # valid ICD-10 without decimal
    ("B12.1234", True),  # 4 decimal digits
    ("A1",      False),  # only 1 digit after letter — does not satisfy \d{2}
    ("123",     False),  # no leading letter
])
def test_icd10_code_match(code, should_match):
    if should_match:
        assert _matches("ICD10_CODE", code), f"Expected match on {code!r}"
    else:
        assert not _matches("ICD10_CODE", code), f"Expected no match on {code!r}"


def test_phi_patterns_have_us_geography():
    phi_types = {"MRN", "NPI", "DEA_NUMBER", "HICN", "HEALTH_PLAN_BENEFICIARY", "ICD10_CODE"}
    phi_patterns = [p for p in CUSTOM_PATTERNS if p.entity_type in phi_types]
    assert len(phi_patterns) == len(phi_types), "Missing PHI pattern entries"
    for p in phi_patterns:
        assert p.geography == "US", f"{p.entity_type} has wrong geography: {p.geography}"


def test_phi_patterns_regulatory_framework_hipaa():
    phi_types = {"MRN", "NPI", "DEA_NUMBER", "HICN", "HEALTH_PLAN_BENEFICIARY", "ICD10_CODE"}
    for p in CUSTOM_PATTERNS:
        if p.entity_type in phi_types:
            assert "HIPAA" in p.regulatory_framework, (
                f"{p.entity_type} missing HIPAA in regulatory_framework"
            )


# ---------------------------------------------------------------------------
# 12. FERPA patterns
# ---------------------------------------------------------------------------

def test_student_id_stu_prefix_matches():
    assert _matches("STUDENT_ID", "STU12345")


def test_student_id_sid_prefix_matches():
    assert _matches("STUDENT_ID", "SID-A1B2C")


def test_student_id_s_prefix_matches():
    assert _matches("STUDENT_ID", "SA1B2C3D")


def test_student_id_rejects_too_short():
    # "SXY" → S prefix + "XY" = 2 chars < 4 minimum → no match
    assert not _matches("STUDENT_ID", "SXY")


def test_student_id_geography_is_us():
    p = next(p for p in CUSTOM_PATTERNS if p.entity_type == "STUDENT_ID")
    assert p.geography == "US"


def test_student_id_regulatory_framework_is_ferpa():
    p = next(p for p in CUSTOM_PATTERNS if p.entity_type == "STUDENT_ID")
    assert p.regulatory_framework == "FERPA"


def test_student_id_score_requires_layer2():
    p = next(p for p in CUSTOM_PATTERNS if p.entity_type == "STUDENT_ID")
    assert p.score < 0.75, "STUDENT_ID must have score < 0.75 (needs Layer 2/3)"


# ---------------------------------------------------------------------------
# 13. SPI patterns (CCPA/GDPR)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("phrase", [
    "fingerprint identifier",
    "retinal scan",
    "iris record",
    "biometric id",
])
def test_biometric_identifier_matches(phrase):
    assert _matches("BIOMETRIC_IDENTIFIER", phrase), f"Expected match on {phrase!r}"


def test_biometric_identifier_rejects_partial_word():
    # Just "fingerprint" alone (no second word) must not match
    assert not _matches("BIOMETRIC_IDENTIFIER", "fingerprint")


def test_biometric_geography_is_global():
    p = next(p for p in CUSTOM_PATTERNS if p.entity_type == "BIOMETRIC_IDENTIFIER")
    assert p.geography == "GLOBAL"


def test_biometric_regulatory_framework():
    p = next(p for p in CUSTOM_PATTERNS if p.entity_type == "BIOMETRIC_IDENTIFIER")
    assert "CCPA" in p.regulatory_framework
    assert "GDPR" in p.regulatory_framework


def test_financial_account_pair_matches_routing_then_account():
    # 9-digit routing followed within 20 chars by 10-digit account
    text = "routing 021000021 account 1234567890"
    assert _matches("FINANCIAL_ACCOUNT_PAIR", text)


def test_financial_account_pair_rejects_too_far_apart():
    # Gap > 20 chars between the two numbers
    text = "021000021" + " " * 30 + "1234567890"
    assert not _matches("FINANCIAL_ACCOUNT_PAIR", text)


def test_financial_pair_geography_is_global():
    p = next(p for p in CUSTOM_PATTERNS if p.entity_type == "FINANCIAL_ACCOUNT_PAIR")
    assert p.geography == "GLOBAL"


def test_financial_pair_regulatory_framework():
    p = next(p for p in CUSTOM_PATTERNS if p.entity_type == "FINANCIAL_ACCOUNT_PAIR")
    assert "CCPA" in p.regulatory_framework
    assert "GDPR" in p.regulatory_framework


# ---------------------------------------------------------------------------
# 14. PPRA patterns
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("word", ["survey", "questionnaire", "response", "answer"])
def test_survey_response_keyword_matches(word):
    assert _matches("SURVEY_RESPONSE", word)


def test_survey_response_score_very_low():
    p = next(p for p in CUSTOM_PATTERNS if p.entity_type == "SURVEY_RESPONSE")
    assert p.score < 0.60, "SURVEY_RESPONSE must have score < 0.60 (Layer 3 mandatory)"


def test_survey_response_geography_is_us():
    p = next(p for p in CUSTOM_PATTERNS if p.entity_type == "SURVEY_RESPONSE")
    assert p.geography == "US"


def test_survey_response_regulatory_framework_ppra():
    p = next(p for p in CUSTOM_PATTERNS if p.entity_type == "SURVEY_RESPONSE")
    assert p.regulatory_framework == "PPRA"


# ---------------------------------------------------------------------------
# 15. get_all_patterns() count and filtering with new patterns
# ---------------------------------------------------------------------------

_ORIGINAL_COUNT = 42
_NEW_PATTERN_COUNT = 10
_EXPECTED_TOTAL = _ORIGINAL_COUNT + _NEW_PATTERN_COUNT


def test_custom_patterns_total_count():
    assert len(CUSTOM_PATTERNS) == _EXPECTED_TOTAL, (
        f"Expected {_EXPECTED_TOTAL} patterns, got {len(CUSTOM_PATTERNS)}"
    )


def test_get_all_patterns_count_matches_custom_patterns():
    assert len(get_all_patterns()) == _EXPECTED_TOTAL


def test_get_all_patterns_us_global_includes_phi():
    result = get_all_patterns(["US", "GLOBAL"])
    entity_types = {p.entity_type for p in result}
    phi_types = {"MRN", "NPI", "DEA_NUMBER", "HICN", "HEALTH_PLAN_BENEFICIARY", "ICD10_CODE"}
    for phi in phi_types:
        assert phi in entity_types, f"PHI pattern {phi} missing from US+GLOBAL result"


def test_get_all_patterns_us_global_includes_ferpa():
    result = get_all_patterns(["US", "GLOBAL"])
    entity_types = {p.entity_type for p in result}
    assert "STUDENT_ID" in entity_types


def test_get_all_patterns_us_global_includes_spi_global():
    result = get_all_patterns(["US", "GLOBAL"])
    entity_types = {p.entity_type for p in result}
    assert "BIOMETRIC_IDENTIFIER" in entity_types
    assert "FINANCIAL_ACCOUNT_PAIR" in entity_types


def test_get_all_patterns_us_global_includes_ppra():
    result = get_all_patterns(["US", "GLOBAL"])
    entity_types = {p.entity_type for p in result}
    assert "SURVEY_RESPONSE" in entity_types


def test_get_all_patterns_in_global_excludes_phi():
    # PHI is US-only; should not appear when only IN+GLOBAL requested
    result = get_all_patterns(["IN", "GLOBAL"])
    entity_types = {p.entity_type for p in result}
    assert "MRN" not in entity_types
    assert "DEA_NUMBER" not in entity_types


def test_get_all_patterns_in_global_excludes_ferpa():
    result = get_all_patterns(["IN", "GLOBAL"])
    entity_types = {p.entity_type for p in result}
    assert "STUDENT_ID" not in entity_types


# ---------------------------------------------------------------------------
# Helper (used by new tests above)
# ---------------------------------------------------------------------------

def _get_regex(entity_type: str) -> str:
    """Return the regex string for an entity_type from CUSTOM_PATTERNS."""
    pat = next(p for p in CUSTOM_PATTERNS if p.entity_type == entity_type)
    return pat.regex
