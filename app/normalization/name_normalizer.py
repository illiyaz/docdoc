"""Name normalizer — Phase 2.

Converts a raw person name string to a canonical "Firstname Lastname"
(title-case) form used by the RRA entity resolver for fuzzy matching.

Rules applied in order
----------------------
1. Strip leading / trailing whitespace.
2. Detect non-Latin script (Chinese, Arabic, Devanagari, …) — pass
   through unchanged except for whitespace collapsing, because
   title-case mangles non-Latin glyphs.
3. Remove honorifics from the front of the string (English, Indian,
   German, French, Spanish, universal — with or without period).
4. Detect reversed "Last, First" format via ``is_western_reversed()``
   and reorder to "First Last".
5. Collapse internal runs of whitespace.
6. Apply title-case.

Safety rule: raw values are never logged.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Non-Latin script detection
# ---------------------------------------------------------------------------

# Unicode ranges whose presence indicates a non-Latin writing system.
# Latin Extended blocks (ü, é, ñ …) are deliberately excluded — they are
# Latin-alphabet text and should be title-cased normally.
_NON_LATIN_RANGES: tuple[tuple[int, int], ...] = (
    (0x0600, 0x06FF),  # Arabic
    (0x0900, 0x097F),  # Devanagari (Hindi, Sanskrit, Marathi …)
    (0x0E00, 0x0E7F),  # Thai
    (0x3040, 0x30FF),  # Japanese Hiragana + Katakana
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs (Chinese, Japanese Kanji)
    (0xAC00, 0xD7AF),  # Korean Hangul syllables
)


def _has_non_latin_chars(text: str) -> bool:
    """Return True if *text* contains any character from a non-Latin script."""
    for char in text:
        cp = ord(char)
        for start, end in _NON_LATIN_RANGES:
            if start <= cp <= end:
                return True
    return False


# ---------------------------------------------------------------------------
# Honorific patterns — expanded for multi-geography use
# ---------------------------------------------------------------------------

# Stripped only from the *leading* position.  The mandatory trailing \s+
# prevents stripping "Dr" when it appears mid-name.
# Alternatives ordered longest-first to prevent prefix shadowing
# (e.g. "miss" before "ms" so "miss" doesn't partially match as "ms").
_HONORIFIC_RE = re.compile(
    r"^(?:"
    # English
    r"miss|mrs|mr|ms|prof|rev|dr|sir|lord|lady|jr|sr|"
    # Indian
    r"shri|kumari|smt|sri|"
    # German
    r"herr|frau|"
    # French — require explicit period for single-letter 'm' to avoid
    # matching first initials like "M Smith" as honorific
    r"mme|mlle|m\.|"
    # Spanish — longest first
    r"srta|sra"
    r")\.?\s+",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Known geographic tokens that disqualify "Last, First" reversal
# ---------------------------------------------------------------------------

# If either comma-segment exactly matches one of these, the string is an
# address / location fragment, not a reversed personal name.
_GEO_TOKENS: frozenset[str] = frozenset({
    # Countries
    "india", "united states", "united kingdom", "uk", "us", "usa",
    "canada", "australia", "germany", "france", "china", "japan",
    "brazil", "mexico", "italy", "spain", "russia", "pakistan",
    "bangladesh", "nigeria", "egypt", "indonesia", "iran", "turkey",
    "ukraine", "poland", "netherlands", "belgium", "sweden", "norway",
    "denmark", "finland", "switzerland", "austria", "portugal", "greece",
    # Major cities that could appear as second segment after comma
    "mumbai", "delhi", "kolkata", "chennai", "bangalore", "hyderabad",
    "london", "paris", "berlin", "rome", "madrid",
    "beijing", "shanghai", "tokyo", "dubai", "singapore",
    # Indian states that commonly appear in "City, State" patterns
    "maharashtra", "karnataka", "gujarat", "rajasthan", "punjab",
    "kerala", "tamilnadu", "tamil nadu", "andhra pradesh", "telangana",
    "uttar pradesh", "west bengal", "bihar", "odisha",
})


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def is_western_reversed(raw: str) -> bool:
    """Return ``True`` if *raw* looks like a ``"Last, First"`` reversed name.

    Conditions that must ALL hold:
    * Exactly one comma.
    * Both segments contain no digit characters.
    * Both segments are entirely Latin-alphabet text (no CJK, Arabic, etc.).
    * Neither segment matches a known country / city / region token.

    This prevents city–country strings such as ``"Mumbai, India"`` from
    being reversed as if they were personal names.
    """
    if raw.count(",") != 1:
        return False

    parts = [p.strip() for p in raw.split(",")]

    for part in parts:
        if not part:
            return False
        # Any digit suggests an address component, not a name
        if any(c.isdigit() for c in part):
            return False
        # Non-Latin glyphs → not a Western reversed-name pattern
        if _has_non_latin_chars(part):
            return False
        # Known geographic token → location fragment, not a name
        if part.lower() in _GEO_TOKENS:
            return False

    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def normalize_name(raw: str) -> str:
    """Return *raw* in canonical ``"Firstname Lastname"`` (title-case) form.

    Parameters
    ----------
    raw:
        Raw person name as extracted from source text.

    Returns
    -------
    str
        Canonical form, or ``""`` for empty / whitespace-only input.
        Non-Latin script names are returned with whitespace collapsed but
        otherwise unchanged — title-case is never applied to them.
    """
    if not raw or not raw.strip():
        return ""

    text = raw.strip()

    # 1. Non-Latin names — collapse whitespace only; do not mangle glyphs
    if _has_non_latin_chars(text):
        return " ".join(text.split())

    # 2. Strip leading honorific
    text = _HONORIFIC_RE.sub("", text).strip()

    # 3. Resolve "Last, First [Middle]" reversed format (Latin names only)
    if is_western_reversed(text):
        last, _, first_rest = text.partition(",")
        last = last.strip()
        first_rest = first_rest.strip()
        # Strip any honorific that was attached to the first-name segment
        first_rest = _HONORIFIC_RE.sub("", first_rest).strip()
        text = f"{first_rest} {last}"

    # 4. Collapse internal whitespace
    text = " ".join(text.split())

    # 5. Title-case (handles apostrophes correctly: o'brien → O'Brien)
    return text.title()
