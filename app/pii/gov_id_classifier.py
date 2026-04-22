"""Geo-neutral government ID type classifier.

Maps a raw government-ID string (and optional ISO country hint) to a
canonical entity-type label. Replaces the previous hard-coded "US_SSN"
label which silently misclassified UK NI numbers, Indian PAN/Aadhaar,
Brazilian CPF, etc. in non-US documents.

Usage:
    from app.pii.gov_id_classifier import infer_gov_id_type
    t = infer_gov_id_type("NE724362D", country_hint="GB")  -> "UK_NINO"
    t = infer_gov_id_type("123-45-6789", country_hint="US") -> "US_SSN"
    t = infer_gov_id_type("123456789")                      -> "GOVERNMENT_ID"  (ambiguous 9 digits)

Design:
  - Patterns are compiled regexes with an ISO 3166-1 alpha-2 country code
    and a "strict" flag (alphanumeric patterns are strict; digit-only
    patterns are non-strict).
  - `infer_gov_id_type` normalizes input (strip + uppercase), then:
      1. Collects all matching patterns.
      2. If one match → return its type.
      3. If multiple matches:
         a. If country_hint matches one of them → return that one.
         b. Else if exactly one is "strict" → return that one.
         c. Else return "GOVERNMENT_ID".
      4. No match → "GOVERNMENT_ID".
  - `country_hint` is the segregation-provided country (ISO alpha-2).
    When absent, disambiguation falls back to strict-only logic.

All patterns are tested in `tests/test_gov_id_classifier.py`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Canonical fallback label — kept in sync with _GOV_ID_TYPES in
# app/rra/entity_resolver.py.
GENERIC_TYPE = "GOVERNMENT_ID"


@dataclass(frozen=True)
class GovIdPattern:
    """One country-specific government-ID format."""

    type_name: str       # canonical entity-type label (e.g. "UK_NINO")
    country: str         # ISO 3166-1 alpha-2 (e.g. "GB", "US")
    regex: re.Pattern
    strict: bool         # True for alphanumeric formats that are unambiguous


# ---------------------------------------------------------------------------
# Pattern registry — ordered roughly by strictness (alphanumeric formats
# first, since they are self-identifying even without a country hint).
# ---------------------------------------------------------------------------

_PATTERNS: tuple[GovIdPattern, ...] = (
    # --- United Kingdom ---------------------------------------------------
    # NINO: 2 prefix letters (excluding D,F,I,Q,U,V as first; O as second),
    # 6 digits, 1 suffix letter A-D. We relax prefix a bit for robustness.
    GovIdPattern("UK_NINO", "GB",
                 re.compile(r"^[A-CEGHJ-PR-TW-Z]{2}\d{6}[A-D]$"), True),
    GovIdPattern("UK_NHS", "GB",
                 re.compile(r"^\d{3}\d{3}\d{4}$"), False),  # 10 digits

    # --- Ireland ----------------------------------------------------------
    # PPS: 7 digits, 1-2 letters. Historical legacy allows W suffix.
    GovIdPattern("IE_PPS", "IE",
                 re.compile(r"^\d{7}[A-Z]{1,2}$"), True),

    # --- India ------------------------------------------------------------
    # PAN: 5 letters, 4 digits, 1 letter. 4th letter encodes taxpayer type.
    GovIdPattern("IN_PAN", "IN",
                 re.compile(r"^[A-Z]{5}\d{4}[A-Z]$"), True),
    # Aadhaar: 12 digits; first digit must be 2-9.
    GovIdPattern("IN_AADHAAR", "IN",
                 re.compile(r"^[2-9]\d{11}$"), False),
    # GSTIN: 15 alnum chars, 1st two = state code digits.
    GovIdPattern("IN_GSTIN", "IN",
                 re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][A-Z\d]Z[A-Z\d]$"), True),

    # --- Singapore / Hong Kong / Taiwan -----------------------------------
    GovIdPattern("SG_NRIC", "SG",
                 re.compile(r"^[STFGM]\d{7}[A-Z]$"), True),
    # HK_HKID requires the parenthesised check digit — otherwise a bare
    # "[A-Z]{1,2}\d{6}[A-Z]" format would shadow UK_NINO.
    GovIdPattern("HK_HKID", "HK",
                 re.compile(r"^[A-Z]{1,2}\d{6}\([A-Z\d]\)$"), True),
    GovIdPattern("TW_ID", "TW",
                 re.compile(r"^[A-Z]\d{9}$"), True),

    # --- Japan / South Korea / China --------------------------------------
    GovIdPattern("JP_MYNUMBER", "JP",
                 re.compile(r"^\d{12}$"), False),
    GovIdPattern("KR_RRN", "KR",
                 re.compile(r"^\d{6}-?\d{7}$"), True),
    # Chinese resident ID: 18 chars, 17 digits + 1 check (digit or X).
    GovIdPattern("CN_RESID", "CN",
                 re.compile(r"^\d{17}[\dXx]$"), True),

    # --- Southeast Asia ---------------------------------------------------
    GovIdPattern("TH_NID", "TH",
                 re.compile(r"^\d{13}$"), False),
    GovIdPattern("MY_IC", "MY",
                 re.compile(r"^\d{6}-?\d{2}-?\d{4}$"), False),
    GovIdPattern("ID_NIK", "ID",
                 re.compile(r"^\d{16}$"), False),
    GovIdPattern("PH_SSS", "PH",
                 re.compile(r"^\d{2}-\d{7}-\d$"), True),

    # --- North America ----------------------------------------------------
    # US SSN: 3-2-4 digits with optional dashes/spaces, area ≠ 000/666/9xx,
    # group ≠ 00, serial ≠ 0000. We keep it permissive here — validation
    # lives in app/pii/pattern_validator.py.
    GovIdPattern("US_SSN", "US",
                 re.compile(r"^\d{3}-?\d{2}-?\d{4}$"), False),
    # US Driver's License: highly variable per state. Common shapes:
    #   1-2 letters + 6-8 digits (CA, NY, FL, etc.)
    #   8-9 digits only (PA, OH, etc.)
    #   Alphanumeric mixed (MA, IL)
    # Keep permissive — ~50 state formats won't fit one regex cleanly.
    # Validation of the DL value itself is downstream.
    GovIdPattern("US_DRIVER_LICENSE", "US",
                 re.compile(r"^[A-Z]{0,2}\d{5,9}[A-Z0-9]?$"), False),
    # US Passport: 1 letter + 8 digits OR 9 digits only.
    GovIdPattern("US_PASSPORT", "US",
                 re.compile(r"^[A-Z]?\d{8,9}$"), False),
    # Canadian SIN: 3-3-3 digits.
    GovIdPattern("CA_SIN", "CA",
                 re.compile(r"^\d{3}[-\s]?\d{3}[-\s]?\d{3}$"), False),
    # Mexican CURP: 18 alnum chars with embedded date & sex code.
    GovIdPattern("MX_CURP", "MX",
                 re.compile(r"^[A-Z]{4}\d{6}[HM][A-Z]{5}[A-Z0-9]\d$"), True),
    # Mexican RFC (personal 13 / corporate 12).
    GovIdPattern("MX_RFC", "MX",
                 re.compile(r"^[A-ZÑ&]{3,4}\d{6}[A-Z0-9]{3}$"), True),

    # --- European Union ---------------------------------------------------
    # Spanish DNI (8 digits + letter) or NIE (X/Y/Z + 7 digits + letter).
    GovIdPattern("ES_DNI", "ES",
                 re.compile(r"^\d{8}[A-Z]$"), True),
    GovIdPattern("ES_NIE", "ES",
                 re.compile(r"^[XYZ]\d{7}[A-Z]$"), True),
    # Italian Codice Fiscale: 16 alnum chars.
    GovIdPattern("IT_CF", "IT",
                 re.compile(r"^[A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z]$"), True),
    # German Steuer-ID (Tax-ID): 11 digits, first non-zero.
    GovIdPattern("DE_IDNR", "DE",
                 re.compile(r"^[1-9]\d{10}$"), False),
    # French NIR (social security): 13 digits or 15 digits with control.
    GovIdPattern("FR_NIR", "FR",
                 re.compile(r"^[12]\d{2}(?:0[1-9]|1[0-2]|2\d|3\d|4\d|5\d|6\d|7\d|8\d|9[0-5])\d{5}\d{3}(?:\d{2})?$"), False),
    # Dutch BSN: 9 digits (11-test validation lives elsewhere).
    GovIdPattern("NL_BSN", "NL",
                 re.compile(r"^\d{9}$"), False),
    # Belgian NRN: 11 digits with optional separators; YY.MM.DD-NNN.CD.
    GovIdPattern("BE_NRN", "BE",
                 re.compile(r"^\d{2}\.?\d{2}\.?\d{2}-?\d{3}\.?\d{2}$"), True),
    # Polish PESEL: 11 digits.
    GovIdPattern("PL_PESEL", "PL",
                 re.compile(r"^\d{11}$"), False),
    # Swedish personnummer: YYMMDD-NNNN or YYYYMMDDNNNN.
    GovIdPattern("SE_PNR", "SE",
                 re.compile(r"^(?:\d{2})?\d{6}[-+]?\d{4}$"), True),
    # Norwegian fødselsnummer: 11 digits (DDMMYY-NNNNN).
    GovIdPattern("NO_FNR", "NO",
                 re.compile(r"^\d{6}\s?\d{5}$"), False),
    # Finnish HETU: DDMMYYCNNNX (C = separator, X = check char).
    GovIdPattern("FI_HETU", "FI",
                 re.compile(r"^\d{6}[-+A]\d{3}[0-9A-Y]$"), True),
    # Austrian SVNR: 10 digits.
    GovIdPattern("AT_SVNR", "AT",
                 re.compile(r"^\d{10}$"), False),

    # --- Oceania ----------------------------------------------------------
    # Australian TFN: 8 or 9 digits.
    GovIdPattern("AU_TFN", "AU",
                 re.compile(r"^\d{3}\s?\d{3}\s?\d{2,3}$"), False),
    # Australian Medicare: 10 or 11 digits.
    GovIdPattern("AU_MEDICARE", "AU",
                 re.compile(r"^\d{4}\s?\d{5}\s?\d{1,2}$"), False),
    # Australian Business Number: 11 digits.
    GovIdPattern("AU_ABN", "AU",
                 re.compile(r"^\d{2}\s?\d{3}\s?\d{3}\s?\d{3}$"), False),
    # New Zealand IRD: 8 digits in 2-3-3 format. Plain 9-digit IRDs exist
    # but are ambiguous with US_SSN/NL_BSN/IL_ID and require a country hint.
    GovIdPattern("NZ_IRD", "NZ",
                 re.compile(r"^\d{2}[-\s]?\d{3}[-\s]?\d{3}$"), False),

    # --- Middle East ------------------------------------------------------
    # Israeli Teudat Zehut: 9 digits (validation separately).
    GovIdPattern("IL_ID", "IL",
                 re.compile(r"^\d{9}$"), False),
    # Saudi national ID / Iqama: 10 digits.
    GovIdPattern("SA_ID", "SA",
                 re.compile(r"^[12]\d{9}$"), True),
    # UAE Emirates ID: 784-YYYY-NNNNNNN-N (15 digits with hyphens).
    GovIdPattern("AE_EID", "AE",
                 re.compile(r"^784-?\d{4}-?\d{7}-?\d$"), True),
    # Turkish TCKN: 11 digits, first non-zero.
    GovIdPattern("TR_TCK", "TR",
                 re.compile(r"^[1-9]\d{10}$"), False),
    # Egyptian NID: 14 digits.
    GovIdPattern("EG_NID", "EG",
                 re.compile(r"^[23]\d{13}$"), True),

    # --- South America ----------------------------------------------------
    # Brazilian CPF: 11 digits, often formatted 000.000.000-00.
    GovIdPattern("BR_CPF", "BR",
                 re.compile(r"^\d{3}\.?\d{3}\.?\d{3}-?\d{2}$"), True),
    # Brazilian CNPJ: 14 digits, often formatted 00.000.000/0000-00.
    GovIdPattern("BR_CNPJ", "BR",
                 re.compile(r"^\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}$"), True),
    # Argentine DNI: 7-8 digits (sometimes with dots).
    GovIdPattern("AR_DNI", "AR",
                 re.compile(r"^\d{1,2}\.?\d{3}\.?\d{3}$"), False),
    # Chilean RUT: up to 8 digits + mandatory dash + check digit (0-9 or K).
    # Without the dash, a bare 9-digit string shouldn't self-identify as RUT.
    GovIdPattern("CL_RUT", "CL",
                 re.compile(r"^\d{1,2}\.?\d{3}\.?\d{3}-[\dK]$"), True),
    # Colombian Cédula: 6-10 digits.
    GovIdPattern("CO_CC", "CO",
                 re.compile(r"^\d{6,10}$"), False),
    # Peruvian DNI: 8 digits.
    GovIdPattern("PE_DNI", "PE",
                 re.compile(r"^\d{8}$"), False),

    # --- Africa -----------------------------------------------------------
    # South African ID: 13 digits with embedded DOB.
    GovIdPattern("ZA_ID", "ZA",
                 re.compile(r"^\d{13}$"), False),
    # Nigerian NIN: 11 digits.
    GovIdPattern("NG_NIN", "NG",
                 re.compile(r"^\d{11}$"), False),
    # Kenyan national ID: 7-8 digits.
    GovIdPattern("KE_ID", "KE",
                 re.compile(r"^\d{7,8}$"), False),

    # --- CIS / Russia -----------------------------------------------------
    # SNILS: 11 digits formatted XXX-XXX-XXX YY.
    GovIdPattern("RU_SNILS", "RU",
                 re.compile(r"^\d{3}-?\d{3}-?\d{3}[\s-]?\d{2}$"), True),
    # INN (personal 12 / corporate 10).
    GovIdPattern("RU_INN", "RU",
                 re.compile(r"^\d{10}(?:\d{2})?$"), False),
)


# Canonical set of all type labels produced by this module.
SUPPORTED_TYPES: frozenset[str] = frozenset(p.type_name for p in _PATTERNS) | {GENERIC_TYPE}


# Legacy / alternative labels that other code emits for the same concept.
# Maps each alias → canonical type name. Used by other modules to
# recognize their own labels against this classifier's canonical set.
#
# How it stays in sync: when a new country/gov-ID is added above, its
# common aliases get added here. Consumers (dedup filter, record
# validator, etc.) import EXPANDED_KNOWN_TYPES which unions the
# canonical set + all aliases — so emitting either the canonical
# ("UK_NINO") or the alias ("NI_NUMBER") still counts as recognized.
ALIAS_TO_CANONICAL: dict[str, str] = {
    # UK
    "NI_NUMBER": "UK_NINO",
    "NATIONAL_INSURANCE_UK": "UK_NINO",
    "UK_NHS_NUMBER": "UK_NHS",
    # India
    "AADHAAR": "IN_AADHAAR",
    "AADHAR": "IN_AADHAAR",
    "PAN_CARD": "IN_PAN",
    "PAN": "IN_PAN",
    "GSTIN": "IN_GSTIN",
    # US
    "DRIVER_LICENSE": "US_DRIVER_LICENSE",
    "DRIVERS_LICENSE": "US_DRIVER_LICENSE",
    "PASSPORT": "US_PASSPORT",           # default assumption; country_hint overrides
    "PASSPORT_ICAO": "US_PASSPORT",
    # Other generic labels that consumers emit
    "NATIONAL_ID": GENERIC_TYPE,          # doesn't disambiguate country
    "TAX_ID": GENERIC_TYPE,
    "OTHER_ID": GENERIC_TYPE,
    "GOV_ID": GENERIC_TYPE,
    "GOVERNMENT_ID": GENERIC_TYPE,
    "IDENTIFICATION": GENERIC_TYPE,
}


# Expanded set including aliases — use this when checking whether a
# label (from anywhere in the codebase) represents a known government
# ID or its alias. Strict canonical-only checks should still use
# SUPPORTED_TYPES.
EXPANDED_KNOWN_TYPES: frozenset[str] = SUPPORTED_TYPES | frozenset(ALIAS_TO_CANONICAL.keys())


def is_known_gov_id_label(label: str | None) -> bool:
    """True when *label* is a canonical gov-ID type or a known alias.

    Used by other modules (deduplicator, record_validator, gap_filler)
    to check whether an entity_type label represents a government ID,
    without each module maintaining its own allowlist.
    """
    if not label:
        return False
    normalized = label.strip().upper()
    return normalized in EXPANDED_KNOWN_TYPES


def _normalize(value: str) -> str:
    """Strip surrounding whitespace and uppercase."""
    return value.strip().upper() if value else ""


def infer_gov_id_type(raw: str | None, country_hint: str | None = None) -> str:
    """Infer the canonical government-ID type for ``raw``.

    Parameters
    ----------
    raw:
        The raw ID string as extracted from the document.
    country_hint:
        Optional ISO 3166-1 alpha-2 country code (e.g. "GB", "US", "IN").
        Used to disambiguate digit-only formats that match multiple
        countries (9-digit → US_SSN vs IL_ID vs NL_BSN, etc.).

    Returns
    -------
    str
        A canonical type label (e.g. "UK_NINO", "US_SSN") or
        ``"GOVERNMENT_ID"`` if the format doesn't match any known
        country pattern or matches multiple with no way to disambiguate.
    """
    if not raw:
        return GENERIC_TYPE

    value = _normalize(raw)
    if not value:
        return GENERIC_TYPE

    hint = country_hint.strip().upper() if country_hint else None

    matches: list[GovIdPattern] = [p for p in _PATTERNS if p.regex.match(value)]

    if not matches:
        return GENERIC_TYPE

    if len(matches) == 1:
        return matches[0].type_name

    # Multiple matches — try country hint first.
    if hint:
        hinted = [p for p in matches if p.country == hint]
        if len(hinted) == 1:
            return hinted[0].type_name
        if len(hinted) > 1:
            # Hint matches multiple (e.g. BR_CPF and BR_CNPJ both for BR);
            # prefer the first strict match, else GENERIC.
            strict_hinted = [p for p in hinted if p.strict]
            if len(strict_hinted) == 1:
                return strict_hinted[0].type_name
            return GENERIC_TYPE

    # No hint or hint didn't narrow it — prefer a single strict match.
    strict = [p for p in matches if p.strict]
    if len(strict) == 1:
        return strict[0].type_name

    return GENERIC_TYPE
