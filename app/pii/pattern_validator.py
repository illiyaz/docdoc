"""Pattern validation for vision/LLM-extracted PII records (Step 20).

Validates extracted values against known patterns (NI numbers, SSNs, dates,
emails, etc.).  Flags invalid formats for human review.  Suppresses names
that are financial terms or organizations.

Also provides standalone validators for use during record construction:
- ``validate_dob()``: reject transaction/service dates misclassified as DOB
- ``validate_email()``: reject URLs and non-email strings

Does NOT remove records with flagged fields — only suppresses names that are
clearly not people (financial terms, org names).  Flagged records continue
through the pipeline with validation_flags set for auditor review.
"""
from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timedelta

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


# ---------------------------------------------------------------------------
# Transaction/service date keywords that indicate NOT a DOB
# ---------------------------------------------------------------------------

_TRANSACTION_DATE_RE = re.compile(
    r"(?:fee\s+slip|invoice|statement|receipt|billing)[\s\S]{0,30}?dated"
    r"|(?:transaction|service|payment|due|statement|invoice|effective|"
    r"expiration|exp\.?|billing)\s+date"
    r"|dated\s+\d"
    r"|date\s*:\s*\d",
    re.IGNORECASE,
)

# DOB label keywords (must appear near the date for it to be a DOB)
_DOB_LABEL_RE = re.compile(
    r"(?:date\s+of\s+birth|d\.?o\.?b\.?|birth\s*date|born|birthday)",
    re.IGNORECASE,
)

# Minimum age in years — dates more recent than this are not plausible DOBs
# for adult-context documents (medical bills, financial statements)
_MIN_DOB_AGE_YEARS = 5


def validate_dob(date_str: str, page_text: str = "") -> bool:
    """Check if a date is plausibly a date of birth, not a transaction date.

    Returns ``True`` if the date should be kept as a DOB, ``False`` if it
    should be rejected (likely a transaction/service/statement date).

    Checks:
    1. If page_text is available, look for transaction-date keywords near
       the date value.  If found, reject.
    2. If page_text has DOB label keywords near the date, accept.
    3. Parse the date and reject if it is within the last 5 years
       (implausible as a DOB for adult-context documents).
    """
    if not date_str:
        return False

    # Check page text context if available
    if page_text:
        # Find the date string in the page text
        date_pos = page_text.lower().find(date_str.lower())

        if date_pos >= 0:
            # Check 80 chars before the date for context
            context_start = max(0, date_pos - 80)
            context_end = min(len(page_text), date_pos + len(date_str) + 20)
            context = page_text[context_start:context_end]

            # If DOB label is near THIS date → accept it
            if _DOB_LABEL_RE.search(context):
                return True

            # If transaction keyword is near THIS date → reject it
            if _TRANSACTION_DATE_RE.search(context):
                return False

        # Page-level fallback: if DOB label exists anywhere, accept
        if _DOB_LABEL_RE.search(page_text):
            return True

        # If transaction keywords exist but no DOB label → reject
        if _TRANSACTION_DATE_RE.search(page_text):
            return False

    # Try to parse and check recency
    parsed = _try_parse_date(date_str)
    if parsed is not None:
        cutoff = datetime.now() - timedelta(days=_MIN_DOB_AGE_YEARS * 365)
        if parsed > cutoff:
            return False

    return True


def _try_parse_date(date_str: str) -> datetime | None:
    """Try common date formats and return a datetime or None."""
    formats = [
        "%m/%d/%Y", "%d/%m/%Y",  # 04/05/2022
        "%Y-%m-%d",               # 2022-04-05
        "%d-%b-%Y",               # 05-Apr-2022
        "%d %B %Y",               # 5 April 2022
        "%d %b %Y",               # 5 Apr 2022
    ]
    clean = date_str.strip()
    for fmt in formats:
        try:
            return datetime.strptime(clean, fmt)
        except ValueError:
            continue
    return None


def validate_email(value: str) -> bool:
    """Check if a value is a valid email (not a URL or garbage).

    Returns ``True`` if it looks like a real email, ``False`` if it is
    a URL, lacks @, or is otherwise invalid.
    """
    if not value:
        return False
    v = value.strip().lower()
    # Reject URLs misclassified as emails
    if v.startswith(("http://", "https://", "www.")):
        return False
    if "@" not in v:
        return False
    # Basic structure: local@domain.tld
    if not VALIDATION_PATTERNS["EMAIL_ADDRESS"].match(v):
        return False
    return True


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

        # --- Date of birth: format + context validation ---
        raw_dob = rec.raw_dob
        if raw_dob:
            if not any(p.search(raw_dob) for p in DATE_PATTERNS):
                flags.append("dob_format_unrecognized")
            if not validate_dob(raw_dob):
                flags.append("dob_likely_transaction_date")
                raw_dob = None

        # --- Email: format + URL rejection ---
        raw_email = rec.raw_email
        if raw_email:
            if not validate_email(raw_email):
                flags.append("email_format_invalid")
                raw_email = None

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

        # Build updated record with cleaned fields
        if raw_name is not None:
            # Rebuild entity_types_found based on actually-present fields
            actual_types = _build_entity_types_found(
                raw_name=raw_name,
                raw_address=rec.raw_address,
                raw_dob=raw_dob,
                raw_government_id=rec.raw_government_id,
                raw_email=raw_email,
                raw_phone=rec.raw_phone,
                original_types=rec.entity_types_found,
            )
            validated.append(replace(
                rec,
                raw_name=raw_name,
                raw_dob=raw_dob,
                raw_email=raw_email,
                entity_types_found=actual_types,
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


# Map from PIIRecord field → entity type to use in entity_types_found
_FIELD_TO_ENTITY_TYPE: dict[str, str] = {
    "raw_name": "PERSON",
    "raw_address": "LOCATION",
    "raw_dob": "DATE_OF_BIRTH",
    "raw_email": "EMAIL_ADDRESS",
    "raw_phone": "PHONE_NUMBER",
}

# Gov ID types that should be preserved from original entity_types_found
_GOV_ID_ENTITY_TYPES: frozenset[str] = frozenset({
    "US_SSN", "US_EIN", "NI_NUMBER", "AADHAAR", "US_DRIVER_LICENSE",
    "US_PASSPORT", "PAN_CARD", "NHS_NUMBER", "GOVERNMENT_ID",
    "IDENTIFICATION_NUMBER", "NATIONAL_INSURANCE_UK",
})


def _build_entity_types_found(
    *,
    raw_name: str | None,
    raw_address: dict | None,
    raw_dob: str | None,
    raw_government_id: str | None,
    raw_email: str | None,
    raw_phone: str | None,
    original_types: tuple[str, ...],
) -> tuple[str, ...]:
    """Build entity_types_found based on actually-present PIIRecord fields.

    Only includes entity types where the corresponding field has a non-null,
    non-empty value.  For government IDs, preserves the specific type from
    the original entity_types_found (e.g., US_SSN, NI_NUMBER).
    """
    types: list[str] = []
    if raw_name:
        types.append("PERSON")
    if raw_address:
        addr_val = raw_address.get("raw", "") if isinstance(raw_address, dict) else str(raw_address)
        if addr_val.strip():
            types.append("LOCATION")
    if raw_dob:
        types.append("DATE_OF_BIRTH")
    if raw_government_id:
        # Use the specific gov ID type from original types if available
        gov_type_found = False
        for et in original_types:
            if et in _GOV_ID_ENTITY_TYPES:
                types.append(et)
                gov_type_found = True
                break
        if not gov_type_found:
            types.append("GOVERNMENT_ID")
    if raw_email:
        types.append("EMAIL_ADDRESS")
    if raw_phone:
        types.append("PHONE_NUMBER")

    return tuple(sorted(set(types)))
