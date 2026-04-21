"""Direct CSV extractor for structured CSV files.

Runs as a fast-path before the LLM strategies when file_type == "csv".
A CSV is inherently structured — parsing it as text and sending to an
LLM wastes time and frequently fails (run #1 of employees.csv took
6 min and returned 0 records because Strategy B got confused by the
"type=business" prompt hint).

Approach:
  1. Read the CSV with the stdlib csv module
  2. Detect which columns map to which PII types using header keywords
     (case-insensitive, underscore/space-insensitive)
  3. For each row, emit one PIIRecord with all detected fields
  4. Rows that produce no PII fields are skipped (not suppressed-later)

If the header doesn't contain recognizable keywords (e.g. obfuscated
`field_1`, `field_2`), the extractor returns an empty list and the
caller falls back to the existing LLM paths — that's how
X_hidden_columns.csv still works.
"""
from __future__ import annotations

import csv
import logging
import re
from pathlib import Path
from uuid import uuid4

from app.rra.entity_resolver import PIIRecord


logger = logging.getLogger(__name__)


# Map of PII field → list of header keywords that identify the column.
# Comparisons use EXACT normalised-string equality (lowercase, non-alphanum
# stripped). Substring matching caused false hits ("name" matched
# "firstname" and stole the first-name column).
_COLUMN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "full_name": ("fullname", "employeename", "customername", "patientname",
                  "personname", "subjectname", "applicantname", "clientname"),
    "first_name": ("firstname", "fname", "givenname"),
    "last_name": ("lastname", "lname", "surname", "familyname"),
    "ssn": ("ssn", "socialsecurity", "socialsecuritynumber", "taxid",
            "taxidnumber", "nationalid", "govid", "governmentid"),
    "dob": ("dob", "dateofbirth", "birthdate", "birthday", "birth"),
    "email": ("email", "emailaddress", "emailadd", "mail"),
    "phone": ("phone", "phonenumber", "phoneno", "mobile", "mobilephone",
              "cell", "cellphone", "telephone", "tel"),
    "address": ("address", "homeaddress", "streetaddress", "mailingaddress"),
    "street": ("street", "street1", "streetaddress1", "addressline1"),
    "city": ("city", "town"),
    "state": ("state", "province"),
    "zip": ("zip", "zipcode", "postalcode", "postcode"),
}

# Allow the loose "name" column to match the full_name slot ONLY when there
# are no separate first/last columns. Handled specially inside
# _build_column_map.
_LOOSE_NAME_TOKEN = "name"


def _norm(s: str) -> str:
    """Lowercase + strip non-alphanumerics — so 'First Name' == 'first_name'."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _build_column_map(headers: list[str]) -> dict[str, int]:
    """Return {pii_field: column_index} for columns that match known keywords.

    Uses exact (normalised) string equality — avoids the pitfall where a
    substring match on "name" steals the first-name column.
    """
    mapping: dict[str, int] = {}
    normalised = [_norm(h) for h in headers]

    # First pass: exact matches.
    for field, keywords in _COLUMN_KEYWORDS.items():
        for idx, norm_h in enumerate(normalised):
            if norm_h and norm_h in keywords:
                mapping[field] = idx
                break

    # Second pass: loose "name" column for the full_name slot, only if no
    # first_name or last_name was found (otherwise a plain "name" header
    # with separate first/last doesn't exist in practice).
    if "full_name" not in mapping and "first_name" not in mapping and "last_name" not in mapping:
        for idx, norm_h in enumerate(normalised):
            if norm_h == _LOOSE_NAME_TOKEN:
                mapping["full_name"] = idx
                break

    return mapping


# Value-based column inference for obfuscated headers (field_1, field_2, ...).
# Each entry: (pii_field, regex, min_match_ratio).
_VALUE_PATTERNS: tuple[tuple[str, re.Pattern[str], float], ...] = (
    ("ssn", re.compile(r"^\s*\d{3}-?\d{2}-?\d{4}\s*$"), 0.6),
    ("email", re.compile(r"^[\w.+-]+@[\w-]+\.[\w.-]+$"), 0.6),
    ("phone", re.compile(r"^[\s()+\-]*\d[\d\s()\-]{7,}\d\s*$"), 0.6),
    ("dob", re.compile(r"^\s*\d{1,4}[-/]\d{1,2}[-/]\d{1,4}\s*$"), 0.6),
    ("zip", re.compile(r"^\s*\d{5}(?:-\d{4})?\s*$"), 0.6),
)

# Name inference: two or more capitalized tokens, no digits, reasonable length.
_NAME_LIKE = re.compile(r"^[A-Z][A-Za-z'\-]{1,}(?:\s+[A-Z][A-Za-z'.\-]{1,}){1,3}$")


def _infer_columns_from_values(
    headers: list[str],
    data_rows: list[list[str]],
    already_mapped: dict[str, int],
    sample_size: int = 25,
) -> dict[str, int]:
    """Score each column against value patterns; assign unused columns.

    Returns updates to merge into the header-based mapping. Columns already
    taken by the header pass are skipped. Name inference runs last so it
    only claims columns not claimed by stricter patterns.
    """
    if not data_rows:
        return {}

    num_cols = len(headers)
    taken_cols = set(already_mapped.values())
    taken_fields = set(already_mapped.keys())
    updates: dict[str, int] = {}

    samples: list[list[str]] = [[] for _ in range(num_cols)]
    for row in data_rows[:sample_size]:
        for c in range(num_cols):
            if c < len(row):
                val = (row[c] or "").strip()
                if val:
                    samples[c].append(val)

    # Strict value-based patterns (SSN, email, phone, DOB, zip).
    for field, pattern, min_ratio in _VALUE_PATTERNS:
        if field in taken_fields:
            continue
        best_col = -1
        best_ratio = 0.0
        for c in range(num_cols):
            if c in taken_cols or not samples[c]:
                continue
            hits = sum(1 for v in samples[c] if pattern.match(v))
            ratio = hits / len(samples[c])
            if ratio >= min_ratio and ratio > best_ratio:
                best_ratio = ratio
                best_col = c
        if best_col >= 0:
            updates[field] = best_col
            taken_cols.add(best_col)
            taken_fields.add(field)

    # Name inference: only if no name column has been assigned yet.
    has_name = any(k in taken_fields for k in ("full_name", "first_name", "last_name"))
    if not has_name:
        best_col = -1
        best_ratio = 0.0
        for c in range(num_cols):
            if c in taken_cols or not samples[c]:
                continue
            hits = sum(1 for v in samples[c] if _NAME_LIKE.match(v))
            ratio = hits / len(samples[c])
            if ratio >= 0.5 and ratio > best_ratio:
                best_ratio = ratio
                best_col = c
        if best_col >= 0:
            updates["full_name"] = best_col

    return updates


def _assemble_name(row: list[str], cols: dict[str, int]) -> str | None:
    """Return the subject's name, preferring full_name over first+last."""
    if "full_name" in cols and row[cols["full_name"]].strip():
        return row[cols["full_name"]].strip()
    first = row[cols["first_name"]].strip() if "first_name" in cols and cols["first_name"] < len(row) else ""
    last = row[cols["last_name"]].strip() if "last_name" in cols and cols["last_name"] < len(row) else ""
    name = f"{first} {last}".strip()
    return name or None


def _assemble_address(row: list[str], cols: dict[str, int]) -> dict | None:
    """Assemble the row's address — prefer a single address column, else
    stitch street+city+state+zip."""
    if "address" in cols and cols["address"] < len(row):
        val = row[cols["address"]].strip()
        if val:
            return {"raw": val}
    parts: list[str] = []
    for key in ("street", "city", "state", "zip"):
        if key in cols and cols[key] < len(row):
            v = row[cols[key]].strip()
            if v:
                parts.append(v)
    if not parts:
        return None
    return {"raw": ", ".join(parts)}


def extract_from_csv(
    file_path: str | Path,
    doc_id: str,
    *,
    country_hint: str | None = None,
    max_rows: int = 10_000,
) -> list[PIIRecord]:
    """Parse *file_path* as a CSV and emit PIIRecords row-by-row.

    Returns an empty list if:
      - File can't be opened
      - No PII-identifying columns are recognisable in the header
      - No rows contain any PII field values

    The caller should fall through to LLM strategies in that case
    (handles X_hidden_columns.csv style obfuscated headers).
    """
    path = Path(file_path)
    if not path.is_file():
        return []

    try:
        with path.open("r", encoding="utf-8", newline="", errors="replace") as fh:
            reader = csv.reader(fh)
            rows = list(reader)
    except Exception as e:
        logger.warning("CSV extractor: failed to read %s: %s", path.name, e)
        return []

    if len(rows) < 2:
        return []

    headers = rows[0]
    cols = _build_column_map(headers)

    # Need at least a name column and one other PII column to call this a win.
    has_name = "full_name" in cols or ("first_name" in cols and "last_name" in cols) or "first_name" in cols
    has_other = any(k in cols for k in ("ssn", "dob", "email", "phone", "address", "street", "zip"))

    # Obfuscated-header fallback: infer columns from sample values.
    # Task #38 — when headers are field_1/field_2/... the name-based map
    # comes back empty. Scan a few rows to identify PII columns by their
    # value patterns (SSN, email, phone, DOB, zip, name-like tokens).
    if not (has_name and has_other):
        inferred = _infer_columns_from_values(headers, rows[1:], cols)
        if inferred:
            cols = {**cols, **inferred}
            has_name = "full_name" in cols or ("first_name" in cols and "last_name" in cols) or "first_name" in cols
            has_other = any(k in cols for k in ("ssn", "dob", "email", "phone", "address", "street", "zip"))
            if has_name and has_other:
                logger.info(
                    "CSV extractor: %s — obfuscated headers, inferred columns "
                    "from values: %s", path.name, sorted(inferred.keys()),
                )

    if not (has_name and has_other):
        logger.info(
            "CSV extractor: header of %s does not yield recognisable PII columns "
            "(cols=%s) — falling back to LLM paths",
            path.name, sorted(cols.keys()),
        )
        return []

    logger.info(
        "CSV extractor: %s — detected columns %s, %d data rows",
        path.name, sorted(cols.keys()), len(rows) - 1,
    )

    out: list[PIIRecord] = []
    for row_idx, row in enumerate(rows[1:max_rows + 1], start=1):
        if not row or all(not cell.strip() for cell in row):
            continue

        name = _assemble_name(row, cols)
        if not name:
            # No identifiable subject — can't attach PII to anyone
            continue

        ssn = (row[cols["ssn"]].strip() if "ssn" in cols and cols["ssn"] < len(row) else "") or None
        dob = (row[cols["dob"]].strip() if "dob" in cols and cols["dob"] < len(row) else "") or None
        email = (row[cols["email"]].strip() if "email" in cols and cols["email"] < len(row) else "") or None
        phone = (row[cols["phone"]].strip() if "phone" in cols and cols["phone"] < len(row) else "") or None
        address = _assemble_address(row, cols)

        entity_types = ["PERSON"]
        if address:
            entity_types.append("LOCATION")
        if ssn and len(ssn) >= 4:
            entity_types.append("US_SSN")
        if dob and len(dob) >= 4:
            entity_types.append("DATE_OF_BIRTH")
        if email and "@" in email:
            entity_types.append("EMAIL_ADDRESS")
        if phone and len(phone) >= 7:
            entity_types.append("PHONE_NUMBER")

        out.append(PIIRecord(
            record_id=str(uuid4()),
            entity_type="PERSON",
            normalized_value=name,
            raw_name=name,
            raw_address=address,
            raw_phone=phone if phone and len(phone) >= 7 else None,
            raw_email=email if email and "@" in email else None,
            raw_dob=dob if dob and len(dob) >= 4 else None,
            raw_government_id=ssn if ssn and len(ssn) >= 4 else None,
            country=country_hint or "US",
            source_document_id=doc_id,
            page_range=str(row_idx),  # use row number as "page" for provenance
            entity_types_found=tuple(entity_types),
            entity_role="primary_subject",
        ))

    logger.info(
        "CSV extractor: produced %d records from %s", len(out), path.name,
    )
    return out
