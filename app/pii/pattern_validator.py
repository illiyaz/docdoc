"""Pattern validation for vision/LLM-extracted PII records (Step 20).

Validates extracted values against known patterns (NI numbers, SSNs, dates,
emails, etc.).  Flags invalid formats for human review.  Suppresses names
that are financial terms or organizations.

Does NOT remove records with flagged fields — only suppresses names that are
clearly not people (financial terms, org names).  Flagged records continue
through the pipeline with validation_flags set for auditor review.
"""
from __future__ import annotations

import re
from dataclasses import replace

from app.pii.context_deny_list import FINANCIAL_TERM_DENY_LIST, is_likely_organization
from app.rra.entity_resolver import PIIRecord

# ---------------------------------------------------------------------------
# Validation patterns for government IDs
# ---------------------------------------------------------------------------

VALIDATION_PATTERNS: dict[str, re.Pattern] = {
    "NI_NUMBER": re.compile(r"^[A-Z]{2}\d{6}[A-Z]$"),
    "US_SSN": re.compile(r"^\d{3}-\d{2}-\d{4}$"),
    "AADHAAR": re.compile(r"^\d{12}$"),
    "PAN_CARD": re.compile(r"^[A-Z]{5}\d{4}[A-Z]$"),
    "EMAIL_ADDRESS": re.compile(r"^[^@]+@[^@]+\.[^@]+$"),
    "UK_POSTCODE": re.compile(r"[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}", re.IGNORECASE),
}

# ---------------------------------------------------------------------------
# Date format patterns
# ---------------------------------------------------------------------------

DATE_PATTERNS: list[re.Pattern] = [
    re.compile(r"\d{1,2}-[A-Za-z]{3}-\d{4}"),       # 10-Aug-1959
    re.compile(r"\d{1,2}/\d{1,2}/\d{4}"),            # 10/08/1959
    re.compile(r"\d{4}-\d{2}-\d{2}"),                 # 1959-08-10
    re.compile(r"\d{1,2}\s+[A-Za-z]+\s+\d{4}"),      # 10 August 1959
]


def validate_extracted_records(records: list[PIIRecord]) -> list[PIIRecord]:
    """Validate LLM/vision-extracted values against known patterns.

    Sets ``validation_flags`` on each record.  Suppresses names that are
    financial terms or organization names (sets ``raw_name`` to None).
    Returns only records that still have a name after validation.

    PIIRecord is frozen, so we use ``dataclasses.replace()`` to create
    modified copies.
    """
    validated: list[PIIRecord] = []

    for rec in records:
        flags: list[str] = []

        # --- Government ID format ---
        if rec.raw_government_id:
            # Determine the gov ID type from entity_types_found
            gov_type = _resolve_gov_id_type(rec)
            if gov_type:
                pattern = VALIDATION_PATTERNS.get(gov_type)
                if pattern and not pattern.match(rec.raw_government_id.strip()):
                    flags.append(f"gov_id_format_mismatch:{gov_type}")

        # --- Date of birth format ---
        if rec.raw_dob:
            if not any(p.search(rec.raw_dob) for p in DATE_PATTERNS):
                flags.append("dob_format_unrecognized")

        # --- Email format ---
        if rec.raw_email:
            if not VALIDATION_PATTERNS["EMAIL_ADDRESS"].match(rec.raw_email.strip()):
                flags.append("email_format_invalid")

        # --- Address structure ---
        if rec.raw_address:
            addr_text = rec.raw_address.get("raw", "") if isinstance(rec.raw_address, dict) else str(rec.raw_address)
            if len(addr_text.split()) < 3:
                flags.append("address_too_short")

        # --- Name: financial term or organization ---
        raw_name = rec.raw_name
        if raw_name:
            lower_name = raw_name.lower().strip()
            if lower_name in FINANCIAL_TERM_DENY_LIST:
                flags.append("name_is_financial_term")
                raw_name = None
            elif is_likely_organization(raw_name):
                flags.append("name_is_organization")
                raw_name = None

        # Build updated record
        if raw_name is not None:
            validated.append(replace(
                rec,
                raw_name=raw_name,
                validation_flags=tuple(flags),
            ))
        # else: record suppressed (no name)

    return validated


def _resolve_gov_id_type(rec: PIIRecord) -> str | None:
    """Determine the government ID type from entity_types_found."""
    gov_id_types = {"NI_NUMBER", "US_SSN", "AADHAAR", "PAN_CARD"}
    for et in rec.entity_types_found:
        if et in gov_id_types:
            return et
    return None
