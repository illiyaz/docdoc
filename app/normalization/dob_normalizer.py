"""Date of birth normalizer — standardizes mixed date formats.

Handles common DOB formats found in breach documents:
  - DD-Mon-YYYY or DD Mon YYYY  (e.g. "15-Jan-1980", "15 Jan 1980")
  - MM/DD/YYYY or MM-DD-YYYY    (e.g. "01/15/1980")
  - DD/MM/YYYY (when day > 12)  (e.g. "15/01/1980")
  - YYYY-MM-DD (ISO)            (e.g. "1980-01-15")
  - Mon DD, YYYY                (e.g. "Jan 15, 1980")
  - M/D/YYYY or M/D/YY         (e.g. "1/5/80")

Output: ISO 8601 date string YYYY-MM-DD.

Returns None if the string cannot be parsed as a valid date.
"""
from __future__ import annotations

import re
from datetime import date, datetime

# Month name → number mapping
_MONTH_MAP: dict[str, int] = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

# DD-Mon-YYYY or DD Mon YYYY
_DMY_ALPHA_RE = re.compile(
    r"(\d{1,2})[\s./-]+([A-Za-z]{3,9})[\s./-]+(\d{2,4})",
)

# Mon DD, YYYY
_MDY_ALPHA_RE = re.compile(
    r"([A-Za-z]{3,9})[\s.]+(\d{1,2}),?\s+(\d{2,4})",
)

# YYYY-MM-DD (ISO)
_ISO_RE = re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})")

# MM/DD/YYYY or DD/MM/YYYY (ambiguous — resolve by checking if day > 12)
_NUMERIC_RE = re.compile(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})")


def _expand_year(y: int) -> int:
    """Expand 2-digit year to 4-digit: 00-29 → 2000s, 30-99 → 1900s."""
    if y < 100:
        return y + 2000 if y < 30 else y + 1900
    return y


def _valid_date(year: int, month: int, day: int) -> date | None:
    """Return a date object if valid, else None."""
    try:
        return date(year, month, day)
    except (ValueError, OverflowError):
        return None


# Countries that use DD/MM format (day-first). US/Canada/most-of-Asia
# default to MM/DD; UK/EU/India/Australia/NZ/most-of-Africa use DD/MM.
_DAY_FIRST_COUNTRIES: frozenset[str] = frozenset({
    "GB", "UK", "IE", "FR", "DE", "IT", "ES", "PT", "NL", "BE", "CH",
    "AT", "DK", "SE", "NO", "FI", "PL", "CZ", "HU", "GR", "RO",
    "IN", "PK", "BD", "LK", "NP",
    "AU", "NZ",
    "ZA", "KE", "NG",
    "BR", "AR", "CL", "CO", "PE",
})


def normalize_dob(raw: str, country: str | None = None) -> str | None:
    """Normalize a raw DOB string to ISO 8601 (YYYY-MM-DD).

    Returns None if the value cannot be parsed as a plausible date of birth
    (must be between 1900 and today).

    Parameters
    ----------
    raw:
        The raw DOB string.
    country:
        ISO-3166-1 alpha-2 country code from the record's segregation
        hint. Used to disambiguate ``01/03/1960`` style dates — the same
        string is January 3rd in US docs and March 1st in UK/EU/IN docs.
        Defaults to US when absent (matches old behavior).
    """
    if not raw:
        return None
    s = raw.strip()
    day_first = (country or "").upper() in _DAY_FIRST_COUNTRIES

    result: date | None = None

    # Try DD-Mon-YYYY
    m = _DMY_ALPHA_RE.search(s)
    if m:
        day, mon_str, year_str = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        month = _MONTH_MAP.get(mon_str[:3])
        if month:
            result = _valid_date(_expand_year(year_str), month, day)

    # Try Mon DD, YYYY
    if result is None:
        m = _MDY_ALPHA_RE.search(s)
        if m:
            mon_str, day, year_str = m.group(1).lower(), int(m.group(2)), int(m.group(3))
            month = _MONTH_MAP.get(mon_str[:3])
            if month:
                result = _valid_date(_expand_year(year_str), month, day)

    # Try ISO: YYYY-MM-DD
    if result is None:
        m = _ISO_RE.search(s)
        if m:
            year, month_n, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
            result = _valid_date(year, month_n, day)

    # Try numeric: MM/DD/YYYY or DD/MM/YYYY
    if result is None:
        m = _NUMERIC_RE.search(s)
        if m:
            a, b, c = int(m.group(1)), int(m.group(2)), int(m.group(3))
            year = _expand_year(c)
            # If first number > 12, must be DD/MM/YYYY
            if a > 12 and b <= 12:
                result = _valid_date(year, b, a)
            # If second number > 12, must be MM/DD/YYYY
            elif b > 12 and a <= 12:
                result = _valid_date(year, a, b)
            else:
                # Ambiguous — use country hint. UK/EU/IN etc. = DD/MM;
                # US and everyone else defaults to MM/DD (old behavior).
                if day_first:
                    result = _valid_date(year, b, a)
                else:
                    result = _valid_date(year, a, b)

    if result is None:
        return None

    # Sanity: DOB must be between 1900 and today
    today = date.today()
    if result.year < 1900 or result > today:
        return None

    return result.isoformat()
