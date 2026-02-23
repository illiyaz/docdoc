"""Phone number normalizer — Phase 2.

Converts any recognized phone string to E.164 format
(e.g. ``+12125551234``).  Country-code inference uses *default_region*
when no international prefix is present in the raw string.

Safety rule: raw values are never logged.
"""
from __future__ import annotations

import logging

import phonenumbers

logger = logging.getLogger(__name__)

_DEFAULT_REGION = "US"


def normalize_phone(raw: str, *, default_region: str = _DEFAULT_REGION) -> str | None:
    """Return *raw* in E.164 format, or ``None`` if it cannot be parsed.

    Parameters
    ----------
    raw:
        Raw phone string extracted from source text.
    default_region:
        ISO-3166-1 alpha-2 country code assumed when *raw* carries no
        international dialling prefix.  Defaults to ``"US"``.

    Returns
    -------
    str | None
        E.164 string (e.g. ``"+12125551234"``) on success, or ``None``
        when the input is empty, whitespace-only, or not a recognisable
        phone number.  Never raises.
    """
    if not raw or not raw.strip():
        return None

    try:
        parsed = phonenumbers.parse(raw, default_region)
    except phonenumbers.NumberParseException:
        # SAFETY: do not log raw value
        logger.debug("phone_normalizer: could not parse input (length=%d)", len(raw))
        return None

    if not phonenumbers.is_valid_number(parsed):
        logger.debug("phone_normalizer: parsed but invalid number")
        return None

    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
