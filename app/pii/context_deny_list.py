"""Context deny-list: suppress common false-positive PII detections.

Phase 14a — deterministic false positive reduction without LLM.

Provides:
- COMMON_WORD_DENY_LIST: English words that trigger STUDENT_ID / VAT_EU matches
- REFERENCE_LABELS: field labels indicating adjacent value is a reference number
- is_likely_false_positive(): heuristic FP check for post-filtering Presidio output
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Common English words that trigger false positive entity matches
# ---------------------------------------------------------------------------
# These words match patterns like STUDENT_ID (r"\bS[A-Z0-9\-]{4,12}\b")
# or VAT_EU (r"\b[A-Z]{2}[\dA-Z]{8,12}\b") but are clearly not PII.

COMMON_WORD_DENY_LIST = frozenset({
    "statement", "summary", "description", "street", "transactions",
    "balance", "payment", "opening", "amount", "total", "period",
    "account", "reference", "number", "date", "page", "report",
    "invoice", "receipt", "credit", "debit", "transfer", "other",
    "schedule", "subtotal", "category", "status", "address",
    "telephone", "services", "information", "department", "division",
    "customer", "supplier", "purchase", "discount", "quantity",
    "interest", "commission", "settlement", "withdrawal", "deposit",
    "processing", "outstanding", "previous", "current", "closing",
    "narrative", "particulars", "details", "comments", "notes",
    "clearance", "reversal", "adjustment", "reconciliation",
})

# ---------------------------------------------------------------------------
# Labels that indicate the adjacent value is a reference number, not PII
# ---------------------------------------------------------------------------
# When these appear near a detected value, short numeric strings should not
# be classified as government IDs (DRIVER_LICENSE, COMPANY_NUMBER, etc.)

REFERENCE_LABELS = frozenset({
    "ref", "ref.", "reference", "ref no", "ref no.", "ref number",
    "statement nr", "statement nr.", "statement no", "statement no.",
    "invoice no", "invoice no.", "invoice number",
    "account no", "account no.", "account number", "acct no",
    "client no", "client no.", "client number", "client",
    "case no", "case no.", "case id", "case number",
    "file no", "file no.", "file number",
    "policy no", "policy no.", "policy number",
    "claim no", "claim no.", "claim number",
    "order no", "order no.", "order number",
    "receipt no", "receipt no.", "receipt number",
    "confirmation no", "confirmation no.", "confirmation number",
    "tracking no", "tracking no.", "tracking number",
    "contract no", "contract no.", "contract number",
    "voucher no", "voucher no.", "voucher number",
    "transaction id", "transaction no", "trans id",
    "folio no", "folio no.", "folio",
    "batch no", "batch no.",
})

# Pre-compile for fast lookup in surrounding text
_REFERENCE_LABEL_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(lbl) for lbl in sorted(REFERENCE_LABELS, key=len, reverse=True)) + r")[\s:]*$",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Entity types that are prone to false positives on common words / short nums
# ---------------------------------------------------------------------------

_FP_PRONE_ENTITY_TYPES = frozenset({
    "STUDENT_ID",
    "VAT_EU",
    "COMPANY_NUMBER_UK",
    "DRIVER_LICENSE_US",
    "DATE_OF_BIRTH_DMY",
    "DATE_OF_BIRTH_MDY",
    "DATE_OF_BIRTH_ISO",
    "PHONE_NUMBER",
})

# Date entity types that need birth/DOB context
_DATE_ENTITY_TYPES = frozenset({
    "DATE_OF_BIRTH_DMY",
    "DATE_OF_BIRTH_MDY",
    "DATE_OF_BIRTH_ISO",
})

# Context keywords that CONFIRM a date is a DOB (must appear within ~80 chars)
_DOB_CONFIRM_KEYWORDS = re.compile(
    r"\b(?:dob|d\.o\.b|date\s+of\s+birth|born|birthday|birth\s+date|birthdate)\b",
    re.IGNORECASE,
)

# Context keywords that SUPPRESS a date (it's transactional, not DOB)
_DATE_SUPPRESS_KEYWORDS = re.compile(
    r"\b(?:transaction|period|statement|invoice|posted|effective|due\s+date|"
    r"payment\s+date|filing|issued|expir|maturity|settlement|created|processed|"
    r"from|to|ending|beginning|through)\b",
    re.IGNORECASE,
)

# Company context keywords (must appear within ~120 chars for COMPANY_NUMBER_UK)
_COMPANY_CONTEXT_KEYWORDS = re.compile(
    r"\b(?:company|registration|registered|companies\s+house|ltd|plc|limited|"
    r"inc|incorporated|corp|corporation|llp|reg\s+no|crn|company\s+no)\b",
    re.IGNORECASE,
)

# Driver license context keywords
_LICENSE_CONTEXT_KEYWORDS = re.compile(
    r"(?:\bdriver|\blicense|\blicence|\bdriving|\bpermit|\bdmv|"
    r"\bmotor\s+vehicle|\blicen[cs]e\s+no|\blicen[cs]e\s+number|"
    r"\bDL\s*#|\bDL\s+no|\bDL\b)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Currency / financial amount patterns (Phase 14c)
# ---------------------------------------------------------------------------
# Detects values that look like financial amounts, not phone numbers.
# Patterns: "153.84 160.00" (adjacent decimals), "$1,234.56", "1,153.84"

_CURRENCY_PATTERN = re.compile(
    r"(?:"
    r"[$\u00a3\u20ac\u00a5]\s*[\d,]+(?:\.\d{2})?"  # $1,234.56, £50.00, €100
    r"|[\d,]+(?:\.\d{2})\s+[\d,]+(?:\.\d{2})"       # 153.84 160.00 (adjacent pairs)
    r"|[\d]{1,3}(?:,\d{3})+(?:\.\d{2})?"             # 1,153.84 (comma thousands)
    r"|\d+\.\d{2}\s+\d+\.\d{2}"                     # 0.30 0.60 (decimal pairs)
    r")"
)


def is_currency_pattern(text: str) -> bool:
    """Return True if text matches a currency / financial amount pattern.

    Used to suppress PHONE_NUMBER false positives on financial documents
    where adjacent decimal amounts like "153.84 160.00" or comma-separated
    values like "1,153.84" trigger the phone number recognizer.
    """
    return bool(_CURRENCY_PATTERN.fullmatch(text.strip()))


# ---------------------------------------------------------------------------
# Main API
# ---------------------------------------------------------------------------

def is_likely_false_positive(
    detected_text: str,
    entity_type: str,
    surrounding_text: str,
) -> tuple[bool, str]:
    """Check whether a Presidio detection is likely a false positive.

    Parameters
    ----------
    detected_text:
        The text span that Presidio matched.
    entity_type:
        The Presidio entity type (e.g. "STUDENT_ID", "VAT_EU").
    surrounding_text:
        Context window around the detection (~80-120 chars before/after).

    Returns
    -------
    tuple[bool, str]
        (is_fp, reason) — True if the detection should be suppressed,
        with a human-readable reason for the audit log.
    """
    if entity_type not in _FP_PRONE_ENTITY_TYPES:
        return False, ""

    text_lower = detected_text.strip().lower()

    # ------------------------------------------------------------------
    # 1. Common-word deny-list (STUDENT_ID, VAT_EU)
    # ------------------------------------------------------------------
    if entity_type in ("STUDENT_ID", "VAT_EU"):
        if text_lower in COMMON_WORD_DENY_LIST:
            return True, f"deny_list: '{detected_text}' is a common English word"

    # ------------------------------------------------------------------
    # 2. STUDENT_ID: require at least 1 digit
    # ------------------------------------------------------------------
    if entity_type == "STUDENT_ID":
        if not any(c.isdigit() for c in detected_text):
            return True, f"student_id_no_digit: '{detected_text}' has no digits"

    # ------------------------------------------------------------------
    # 3. Reference label proximity (COMPANY_NUMBER_UK, DRIVER_LICENSE_US)
    # ------------------------------------------------------------------
    if entity_type in ("COMPANY_NUMBER_UK", "DRIVER_LICENSE_US"):
        # Check if a reference label appears just before the detection
        if _REFERENCE_LABEL_PATTERN.search(surrounding_text[:len(surrounding_text) // 2 + 20] if surrounding_text else ""):
            return True, f"reference_label: value near reference label, not a government ID"

    # ------------------------------------------------------------------
    # 4. COMPANY_NUMBER_UK: require exactly 8 chars for bare numeric,
    #    or company context keywords nearby
    # ------------------------------------------------------------------
    if entity_type == "COMPANY_NUMBER_UK":
        digits_only = all(c.isdigit() for c in detected_text.strip())
        if digits_only and len(detected_text.strip()) < 8:
            # Short bare numeric — suppress unless company context present
            if not _COMPANY_CONTEXT_KEYWORDS.search(surrounding_text):
                return True, f"company_number_short: '{detected_text}' is < 8 digits without company context"

    # ------------------------------------------------------------------
    # 5. DRIVER_LICENSE_US: require license context keywords nearby
    # ------------------------------------------------------------------
    if entity_type == "DRIVER_LICENSE_US":
        if not _LICENSE_CONTEXT_KEYWORDS.search(surrounding_text):
            return True, f"driver_license_no_context: no license-related keywords near '{detected_text}'"

    # ------------------------------------------------------------------
    # 6. VAT_EU: reject common English words that match [A-Z]{2}[A-Z0-9]{8,12}
    # ------------------------------------------------------------------
    if entity_type == "VAT_EU":
        # Real VAT numbers have a 2-letter country code + mostly digits
        # Reject if the match is all letters (common words like "DESCRIPTION")
        stripped = detected_text.strip()
        if stripped.isalpha():
            return True, f"vat_all_alpha: '{detected_text}' is all letters, not a VAT number"

    # ------------------------------------------------------------------
    # 7. DATE_OF_BIRTH: require birth/DOB context, suppress near
    #    transaction/statement/period keywords
    # ------------------------------------------------------------------
    if entity_type in _DATE_ENTITY_TYPES:
        has_dob_context = bool(_DOB_CONFIRM_KEYWORDS.search(surrounding_text))
        has_suppress_context = bool(_DATE_SUPPRESS_KEYWORDS.search(surrounding_text))

        if has_suppress_context and not has_dob_context:
            return True, f"date_transactional: date near transaction/statement context, not DOB"
        if not has_dob_context and not has_suppress_context:
            # No confirming context at all — suppress with lower confidence
            return True, f"date_no_dob_context: no birth/DOB keywords near '{detected_text}'"

    # ------------------------------------------------------------------
    # 8. PHONE_NUMBER: suppress if value matches currency pattern
    # ------------------------------------------------------------------
    if entity_type == "PHONE_NUMBER":
        if is_currency_pattern(detected_text):
            return True, f"currency_pattern: '{detected_text}' is a financial amount, not a phone number"

    return False, ""
