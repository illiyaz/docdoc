"""Address normalizer — Phase 2.

Rule-based, multi-geography address parser.  No external APIs — fully
air-gap safe.

Output dict keys: ``street``, ``city``, ``state``, ``zip``, ``country``.

``state`` is a 2-letter uppercase abbreviation for US addresses only.
``country`` is an ISO-3166-1 alpha-2 code returned by ``detect_country()``,
or ``"US"`` when no country signal is found.
All string field values are lowercased and stripped **except** ``state``
and ``country`` which are uppercased ISO codes.

Returns ``None`` when the input contains neither a recognisable street-number
pattern nor a recognised postal code in any supported geography.

Safety rule: raw values are never logged.
"""
from __future__ import annotations

import logging
import re
from typing import Callable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Street-number presence heuristic
# ---------------------------------------------------------------------------

# Digit run followed immediately by whitespace then a word character —
# the minimum signature of a Western street address ("123 Main").
_STREET_NUM_RE = re.compile(r"\b\d+\s+\w")

# ---------------------------------------------------------------------------
# Per-geography postal code patterns and normalizers
# ---------------------------------------------------------------------------

# Each entry: (compiled_regex_with_group_1, normalizer_callable)
# The regex must capture the postal-code token in group 1.

def _us_norm(s: str) -> str:
    return s  # group(1) already strips the +4 suffix


def _gb_norm(s: str) -> str:
    return s.upper().replace(" ", "")


def _in_norm(s: str) -> str:
    return s  # 6-digit PIN, unchanged


def _ca_norm(s: str) -> str:
    return s.upper().replace(" ", "")


def _au_norm(s: str) -> str:
    return s  # 4-digit code, unchanged


def _eu_norm(s: str) -> str:
    return s  # generic 4–5 digit, unchanged


_POSTAL_CONFIG: dict[str, tuple[re.Pattern[str], Callable[[str], str]]] = {
    "US": (re.compile(r"\b(\d{5})(?:-\d{4})?\b"), _us_norm),
    "GB": (re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b", re.IGNORECASE), _gb_norm),
    "IN": (re.compile(r"\b([1-9]\d{5})\b"), _in_norm),
    "CA": (re.compile(r"\b([A-Z]\d[A-Z]\s*\d[A-Z]\d)\b", re.IGNORECASE), _ca_norm),
    "AU": (re.compile(r"\b(\d{4})\b"), _au_norm),
    "EU": (re.compile(r"\b(\d{4,5})\b"), _eu_norm),
}

# Combined pattern used as a fast presence check (no country required yet)
_ANY_POSTAL_RE = re.compile(
    r"\b(?:"
    r"\d{5}(?:-\d{4})?"              # US ZIP / 5-digit
    r"|[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}"  # UK
    r"|[1-9]\d{5}"                   # India PIN
    r"|[A-Z]\d[A-Z]\s*\d[A-Z]\d"    # Canada
    r"|\d{4,5}"                      # AU / EU / generic
    r")\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# US state tables (unchanged from Phase 1)
# ---------------------------------------------------------------------------

_STATE_NAMES: dict[str, str] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT",
    "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME",
    "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO",
    "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND",
    "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
}

_VALID_ABBREVS: frozenset[str] = frozenset(_STATE_NAMES.values())
_SORTED_STATE_NAMES: list[str] = sorted(_STATE_NAMES, key=len, reverse=True)

# ---------------------------------------------------------------------------
# Country keyword detection table
# ---------------------------------------------------------------------------

# Maps a regex (searched case-insensitively on the full raw string) to an
# ISO-3166-1 alpha-2 code.  Ordered most-specific first.
_COUNTRY_KEYWORDS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(?:india|bharat)\b", re.IGNORECASE), "IN"),
    (re.compile(r"\b(?:united\s+kingdom|u\.k\.|uk|england|scotland|wales|great\s+britain)\b", re.IGNORECASE), "GB"),
    (re.compile(r"\b(?:canada)\b", re.IGNORECASE), "CA"),
    (re.compile(r"\b(?:australia)\b", re.IGNORECASE), "AU"),
    (re.compile(r"\b(?:germany|deutschland)\b", re.IGNORECASE), "DE"),
    (re.compile(r"\b(?:france)\b", re.IGNORECASE), "FR"),
    (re.compile(r"\b(?:united\s+states|u\.s\.a\.|u\.s\.|usa)\b", re.IGNORECASE), "US"),
]

# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def detect_country(raw: str) -> str | None:
    """Return the ISO-3166-1 alpha-2 country code detected in *raw*, or None.

    Detection order (first match wins):
    1. Country-name keywords (most reliable)
    2. UK postcode pattern (distinctive alpha-digit-alpha format)
    3. Indian 6-digit PIN code (starts with 1–9)
    4. Canadian FSA+LDU postal code (alternating letter-digit)
    5. US state full name
    6. US state 2-letter abbreviation or 5-digit ZIP

    Returns ``None`` when no signal is found.
    """
    # 1. Country keywords
    for pattern, code in _COUNTRY_KEYWORDS:
        if pattern.search(raw):
            return code

    # 2. UK postcode (must come before US ZIP — no overlap possible)
    if _POSTAL_CONFIG["GB"][0].search(raw):
        return "GB"

    # 3. Indian PIN (6-digit, first digit 1–9)
    if _POSTAL_CONFIG["IN"][0].search(raw):
        return "IN"

    # 4. Canadian postal code
    if _POSTAL_CONFIG["CA"][0].search(raw):
        return "CA"

    # 5. US: full state name
    raw_lower = raw.lower()
    for name in _SORTED_STATE_NAMES:
        if re.search(r"\b" + re.escape(name) + r"\b", raw_lower):
            return "US"

    # 6. US: 2-letter state abbreviation
    for m in re.finditer(r"\b([A-Z]{2})\b", raw):
        if m.group(1) in _VALID_ABBREVS:
            return "US"

    # 7. US: 5-digit ZIP
    if re.search(r"\b\d{5}(?:-\d{4})?\b", raw):
        return "US"

    return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_state(text: str) -> tuple[str | None, str]:
    """Detect and remove a US state token from the *end* of *text*.

    Returns ``(abbreviation, remaining_text)``.  When no state is found,
    abbreviation is ``None`` and *text* is returned unchanged.
    """
    text_lower = text.lower()

    for name in _SORTED_STATE_NAMES:
        pattern = re.compile(
            r"(?:^|[,\s]+)" + re.escape(name) + r"\s*$",
            re.IGNORECASE,
        )
        m = pattern.search(text_lower)
        if m:
            remaining = text[: m.start()].rstrip(", ")
            return _STATE_NAMES[name], remaining

    m = re.search(r"(?:^|[,\s]+)([A-Za-z]{2})\s*$", text)
    if m and m.group(1).upper() in _VALID_ABBREVS:
        remaining = text[: m.start()].rstrip(", ")
        return m.group(1).upper(), remaining

    return None, text


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def normalize_address(raw: str) -> dict[str, str | None] | None:
    """Return *raw* as a structured address dict, or ``None``.

    Parameters
    ----------
    raw:
        Free-text address string extracted from source text.

    Returns
    -------
    dict or None
        ``{"street": ..., "city": ..., "state": ..., "zip": ...,
        "country": <ISO-code>}`` on success.

        ``None`` when *raw* contains neither a recognisable street-number
        pattern (``\\d+ <word>``) nor any recognised postal code format.

    Notes
    -----
    * ``country`` is the ISO-3166-1 alpha-2 code from ``detect_country()``,
      or ``"US"`` when no geography signal is found.
    * ``state`` is a 2-letter uppercase abbreviation for US addresses only;
      ``None`` for all other countries.
    * ZIP / postal code normalisation: US → 5-digit base; UK → uppercase no
      space; Canada → uppercase no space; India / AU / EU → as-is.
    * Never raises; never logs raw values.
    """
    if not raw or not raw.strip():
        return None

    text = raw.strip()

    # --- Detect country -----------------------------------------------------
    country: str = detect_country(text) or "US"

    # --- Choose postal code pattern for detected country -------------------
    postal_pat, postal_norm = _POSTAL_CONFIG.get(country, _POSTAL_CONFIG["US"])
    zip_match = postal_pat.search(text)

    # --- Presence check: need a street number OR a postal code -------------
    if not _STREET_NUM_RE.search(text) and not zip_match:
        logger.debug(
            "normalize_address: no street number or postal code found (length=%d)",
            len(text),
        )
        return None

    # --- Extract and normalise postal code ---------------------------------
    if zip_match:
        postal_code: str | None = postal_norm(zip_match.group(1))
        pre_zip = text[: zip_match.start()].rstrip(", ")
    else:
        postal_code = None
        pre_zip = text

    # --- US-only: extract state abbreviation -------------------------------
    if country == "US":
        state, pre_state = _extract_state(pre_zip)
    else:
        state = None
        pre_state = pre_zip

    # --- Split remaining text into street / city by comma -----------------
    parts = [p.strip() for p in pre_state.split(",")]
    parts = [p for p in parts if p]

    street: str | None = parts[0].lower() if parts else None
    city: str | None = parts[1].lower() if len(parts) > 1 else None

    return {
        "street": street,
        "city": city,
        "state": state,        # uppercase abbreviation (US only)
        "zip": postal_code,
        "country": country,    # ISO-3166-1 alpha-2
    }
