"""PII masking for LLM prompts.

Replaces common PII patterns with type-labelled placeholders before
sending text to the LLM.  Uses the same regex patterns as the LLM
client safety check, plus additional patterns for emails and phones.

Example::

    >>> mask_text_for_llm("Call John at 555-123-4567, SSN 123-45-6789")
    'Call John at [PHONE], SSN [SSN]'
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Masking patterns — order matters (most specific first)
# ---------------------------------------------------------------------------

_MASKING_RULES: list[tuple[re.Pattern[str], str]] = [
    # US SSN (XXX-XX-XXXX)
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[SSN]"),
    # Credit card (16 digits, possibly separated)
    (re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b"), "[CREDIT_CARD]"),
    # Email address
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"), "[EMAIL]"),
    # US phone (various formats)
    (re.compile(r"\b(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b"), "[PHONE]"),
    # 9-digit number (potential SSN without dashes)
    (re.compile(r"\b\d{9}\b"), "[SSN]"),
]


def mask_text_for_llm(text: str) -> str:
    """Replace PII patterns in *text* with bracketed placeholders.

    When ``pii_masking_enabled`` is ``False`` (testing / development),
    returns the original text unchanged so the LLM can analyze and
    classify actual PII values.

    Returns the (possibly masked) text.  Does not modify the input string.
    """
    from app.core.settings import get_settings

    if not get_settings().pii_masking_enabled:
        return text

    result = text
    for pattern, replacement in _MASKING_RULES:
        result = pattern.sub(replacement, result)
    return result
