"""Fuzzy matching utilities for entity resolution — Phase 2.

Provides phonetic and edit-distance similarity functions used by the RRA
entity resolver to match names, addresses, dates of birth, and government IDs
across records that may contain OCR errors, formatting variations, or
transliteration differences.

Design constraints
------------------
* No network calls, no external model inference.
* Pure Python + standard library only.
* All comparisons are case-insensitive.
* Raw values are never logged.

Safety rule: raw values are never logged.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, date

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Non-Latin detection (reuse same ranges as name_normalizer)
# ---------------------------------------------------------------------------

_NON_LATIN_RANGES: tuple[tuple[int, int], ...] = (
    (0x0600, 0x06FF),  # Arabic
    (0x0900, 0x097F),  # Devanagari
    (0x0E00, 0x0E7F),  # Thai
    (0x3040, 0x30FF),  # Japanese Hiragana + Katakana
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0xAC00, 0xD7AF),  # Korean Hangul syllables
)


def _has_non_latin_chars(text: str) -> bool:
    for char in text:
        cp = ord(char)
        for start, end in _NON_LATIN_RANGES:
            if start <= cp <= end:
                return True
    return False


# ---------------------------------------------------------------------------
# Soundex
# ---------------------------------------------------------------------------

_SOUNDEX_TABLE: dict[str, str] = {
    "b": "1", "f": "1", "p": "1", "v": "1",
    "c": "2", "g": "2", "j": "2", "k": "2", "q": "2", "s": "2", "x": "2", "z": "2",
    "d": "3", "t": "3",
    "l": "4",
    "m": "5", "n": "5",
    "r": "6",
}

_SOUNDEX_SKIP = frozenset("aeiouyhw")


def soundex(name: str) -> str:
    """Return the American Soundex code for *name*.

    Returns a 4-character string: one uppercase letter followed by three
    digits (e.g. ``"R163"``).  Returns ``"0000"`` for empty or non-alphabetic
    input.

    Parameters
    ----------
    name:
        The input string (typically a surname).  Non-alphabetic characters
        are ignored.  Case-insensitive.
    """
    if not name or not name.strip():
        return "0000"

    # Keep only ASCII letters (Soundex is a Latin-alphabet algorithm)
    letters = re.sub(r"[^a-zA-Z]", "", name.strip())
    if not letters:
        return "0000"

    letters = letters.lower()
    code = letters[0].upper()
    prev_digit = _SOUNDEX_TABLE.get(letters[0], "")

    for ch in letters[1:]:
        if ch in _SOUNDEX_SKIP:
            prev_digit = ""  # vowel acts as separator
            continue
        digit = _SOUNDEX_TABLE.get(ch, "")
        if digit and digit != prev_digit:
            code += digit
            if len(code) == 4:
                break
        prev_digit = digit

    return code.ljust(4, "0")


# ---------------------------------------------------------------------------
# Jaro-Winkler similarity
# ---------------------------------------------------------------------------

def jaro(s1: str, s2: str) -> float:
    """Return the Jaro similarity between *s1* and *s2* (case-insensitive)."""
    s1 = s1.lower()
    s2 = s2.lower()
    if s1 == s2:
        return 1.0
    len1, len2 = len(s1), len(s2)
    if len1 == 0 or len2 == 0:
        return 0.0

    match_dist = max(len1, len2) // 2 - 1
    if match_dist < 0:
        match_dist = 0

    s1_matches = [False] * len1
    s2_matches = [False] * len2
    matches = 0
    transpositions = 0

    for i in range(len1):
        start = max(0, i - match_dist)
        end = min(i + match_dist + 1, len2)
        for j in range(start, end):
            if s2_matches[j] or s1[i] != s2[j]:
                continue
            s1_matches[i] = True
            s2_matches[j] = True
            matches += 1
            break

    if matches == 0:
        return 0.0

    k = 0
    for i in range(len1):
        if not s1_matches[i]:
            continue
        while not s2_matches[k]:
            k += 1
        if s1[i] != s2[k]:
            transpositions += 1
        k += 1

    m = matches
    return (m / len1 + m / len2 + (m - transpositions / 2) / m) / 3


def jaro_winkler(s1: str, s2: str, *, p: float = 0.1) -> float:
    """Return the Jaro-Winkler similarity between *s1* and *s2*.

    Parameters
    ----------
    s1, s2:
        Input strings.  Comparison is case-insensitive.
    p:
        Prefix scaling factor.  Standard value is 0.1 (default).  The
        result is clamped to [0.0, 1.0].

    Returns
    -------
    float
        Similarity score in [0.0, 1.0].  1.0 means identical.
    """
    j = jaro(s1, s2)
    # Shared prefix length, up to 4 characters
    prefix = 0
    for c1, c2 in zip(s1.lower()[:4], s2.lower()[:4]):
        if c1 == c2:
            prefix += 1
        else:
            break
    return min(1.0, j + prefix * p * (1 - j))


# Keep original stub name for backward compatibility with existing tests
jaro_winkler_similarity = jaro_winkler


# ---------------------------------------------------------------------------
# Name matching
# ---------------------------------------------------------------------------

def names_match(name1: str, name2: str) -> tuple[bool, float]:
    """Return ``(matched, confidence)`` for two name strings.

    Rules
    -----
    * Non-Latin names: Jaro-Winkler only, threshold 0.88.
    * Latin names (checked in order):
      1. Exact match (after lowercasing) → confidence 1.0
      2. Jaro-Winkler ≥ 0.92 → confidence = JW score
      3. Same Soundex AND Jaro-Winkler ≥ 0.80 → confidence = JW score
    * Otherwise: no match, confidence = JW score.

    Parameters
    ----------
    name1, name2:
        Canonical name strings (output of ``normalize_name``).

    Returns
    -------
    tuple[bool, float]
        ``(True, confidence)`` when the names are considered the same
        person; ``(False, confidence)`` otherwise.
    """
    if not name1 or not name2:
        return False, 0.0

    jw = jaro_winkler(name1, name2)

    # Non-Latin path
    if _has_non_latin_chars(name1) or _has_non_latin_chars(name2):
        matched = jw >= 0.88
        return matched, round(jw, 4)

    # Latin path
    if name1.lower() == name2.lower():
        return True, 1.0

    if jw >= 0.92:
        return True, round(jw, 4)

    if soundex(name1) == soundex(name2) and jw >= 0.80:
        return True, round(jw, 4)

    return False, round(jw, 4)


# ---------------------------------------------------------------------------
# Address matching
# ---------------------------------------------------------------------------

def addresses_match(
    addr1: dict[str, str | None] | None,
    addr2: dict[str, str | None] | None,
) -> tuple[bool, float]:
    """Return ``(matched, confidence)`` for two address dicts.

    Address dicts are the output of ``normalize_address`` (keys: ``street``,
    ``city``, ``state``, ``zip``, ``country``).

    Rules
    -----
    * Either input is ``None`` → ``(False, 0.0)``
    * Different (non-None) ``country`` values → ``(False, 0.0)``
    * Same postal code (``zip``) AND exact street match → confidence 0.90
    * Same postal code AND fuzzy street match (Jaro-Winkler ≥ 0.85) →
      confidence 0.75
    * Same postal code, no street data available → confidence 0.60
    * No postal code match → ``(False, 0.0)``
    """
    if addr1 is None or addr2 is None:
        return False, 0.0

    country1 = addr1.get("country")
    country2 = addr2.get("country")
    if country1 and country2 and country1.upper() != country2.upper():
        return False, 0.0

    zip1 = addr1.get("zip")
    zip2 = addr2.get("zip")

    # Normalise postal codes for comparison (strip spaces, uppercase)
    def _norm_zip(z: str | None) -> str | None:
        return z.replace(" ", "").upper() if z else None

    zip1_n = _norm_zip(zip1)
    zip2_n = _norm_zip(zip2)

    if not zip1_n or not zip2_n or zip1_n != zip2_n:
        return False, 0.0

    street1 = addr1.get("street")
    street2 = addr2.get("street")

    if not street1 or not street2:
        return True, 0.60

    if street1.lower() == street2.lower():
        return True, 0.90

    jw = jaro_winkler(street1, street2)
    if jw >= 0.85:
        return True, 0.75

    return False, 0.0


# ---------------------------------------------------------------------------
# Government ID matching
# ---------------------------------------------------------------------------

def government_ids_match(
    id1_type: str,
    id1_value: str,
    id2_type: str,
    id2_value: str,
) -> tuple[bool, float]:
    """Return ``(matched, confidence)`` for two government ID values.

    Rules
    -----
    * Different ``id_type`` values (case-insensitive) → ``(False, 0.0)``
    * Exact value match → confidence 0.95
    * Values differ by exactly one character (OCR near-miss) → confidence 0.75
    * Otherwise → ``(False, 0.0)``

    The one-character OCR near-miss is detected by Levenshtein edit distance
    (substitution, insertion, or deletion of a single character).
    """
    if not id1_type or not id2_type:
        return False, 0.0

    if id1_type.lower() != id2_type.lower():
        return False, 0.0

    v1 = id1_value.strip()
    v2 = id2_value.strip()

    if v1 == v2:
        return True, 0.95

    if _edit_distance_one(v1, v2):
        return True, 0.75

    return False, 0.0


def _edit_distance_one(a: str, b: str) -> bool:
    """Return True if *a* and *b* differ by exactly one edit operation."""
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False

    if la == lb:
        # Exactly one substitution
        diffs = sum(1 for x, y in zip(a, b) if x != y)
        return diffs == 1

    # One insertion/deletion — ensure shorter string is `a`
    if la > lb:
        a, b = b, a
        la, lb = lb, la

    # la == lb - 1: check if skipping one char in b gives a
    i = 0
    skipped = False
    for j in range(lb):
        if i < la and a[i] == b[j]:
            i += 1
        elif skipped:
            return False
        else:
            skipped = True
    return True


# ---------------------------------------------------------------------------
# Date-of-birth normalization and matching
# ---------------------------------------------------------------------------

# Supported input formats (tried in order)
_DOB_FORMATS: list[str] = [
    "%Y-%m-%d",      # ISO 8601 — unambiguous, always tried first
    "%d/%m/%Y",      # DD/MM/YYYY
    "%m/%d/%Y",      # MM/DD/YYYY
    "%d-%m-%Y",      # DD-MM-YYYY
    "%m-%d-%Y",      # MM-DD-YYYY
    "%d.%m.%Y",      # DD.MM.YYYY (European)
    "%Y/%m/%d",      # YYYY/MM/DD
    "%d %b %Y",      # 01 Jan 1990
    "%d %B %Y",      # 01 January 1990
    "%B %d, %Y",     # January 01, 1990
    "%b %d, %Y",     # Jan 01, 1990
]

# Countries where the default ambiguous order is MM/DD (US, Philippines …)
_MMDD_COUNTRIES: frozenset[str] = frozenset({"US", "PH"})


def normalize_dob(raw: str, country: str = "US") -> str | None:
    """Return *raw* date-of-birth as an ISO 8601 string (``YYYY-MM-DD``).

    Parameters
    ----------
    raw:
        Free-text date string (e.g. ``"01/15/1990"``, ``"15 Jan 1990"``).
    country:
        ISO-3166-1 alpha-2 code controlling ambiguous date resolution.
        ``"US"`` (default) and ``"PH"`` interpret ambiguous ``d/m`` as
        ``MM/DD``.  All other countries use ``DD/MM``.

    Returns
    -------
    str | None
        ISO 8601 date string on success, or ``None`` if the input cannot
        be parsed or produces an out-of-range date.  Never raises.
    """
    if not raw or not raw.strip():
        return None

    text = raw.strip()
    use_mmdd = country.upper() in _MMDD_COUNTRIES

    # Try ISO 8601 first (unambiguous)
    try:
        d = datetime.strptime(text, "%Y-%m-%d").date()
        return d.isoformat()
    except ValueError:
        pass

    # Try unambiguous named-month formats
    for fmt in ["%d %b %Y", "%d %B %Y", "%B %d, %Y", "%b %d, %Y"]:
        try:
            d = datetime.strptime(text, fmt).date()
            return d.isoformat()
        except ValueError:
            continue

    # Try numeric formats — separator-agnostic via normalisation
    # Normalise separators to "/"
    normalised = re.sub(r"[-./]", "/", text)
    parts = normalised.split("/")

    if len(parts) == 3:
        p0, p1, p2 = parts[0].strip(), parts[1].strip(), parts[2].strip()

        # If the first part is 4 digits it's YYYY/MM/DD
        if len(p0) == 4 and p0.isdigit():
            try:
                d = date(int(p0), int(p1), int(p2))
                return d.isoformat()
            except (ValueError, OverflowError):
                return None

        # Ambiguous: 2-digit first and second parts
        if use_mmdd:
            month_str, day_str = p0, p1
        else:
            day_str, month_str = p0, p1

        year_str = p2

        try:
            # Year 2-digit expansion: 00-29 → 2000s, 30-99 → 1900s
            year = int(year_str)
            if len(year_str) == 2:
                year = 2000 + year if year < 30 else 1900 + year
            d = date(year, int(month_str), int(day_str))
            return d.isoformat()
        except (ValueError, OverflowError):
            return None

    return None


def dobs_match(
    raw1: str,
    country1: str,
    raw2: str,
    country2: str,
) -> tuple[bool, float]:
    """Return ``(matched, confidence)`` for two raw date-of-birth strings.

    Both strings are normalised to ISO 8601 via ``normalize_dob`` first.
    An exact ISO 8601 match yields confidence 0.95.  If either date cannot
    be parsed, returns ``(False, 0.0)``.

    Parameters
    ----------
    raw1, raw2:
        Raw date strings.
    country1, country2:
        ISO country codes passed to ``normalize_dob`` for ambiguous date
        resolution.
    """
    iso1 = normalize_dob(raw1, country1)
    iso2 = normalize_dob(raw2, country2)

    if iso1 is None or iso2 is None:
        return False, 0.0

    if iso1 == iso2:
        return True, 0.95

    return False, 0.0


# ---------------------------------------------------------------------------
# Legacy stub name kept for backward compatibility
# ---------------------------------------------------------------------------

def is_likely_same_person(
    name1: str,
    name2: str,
    *,
    threshold: float = 0.85,
) -> bool:
    """Return True if *name1* and *name2* likely refer to the same person.

    Delegates to :func:`names_match` and applies *threshold* to the
    returned confidence score.
    """
    matched, confidence = names_match(name1, name2)
    return matched or confidence >= threshold
