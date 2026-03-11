"""Export schema definitions for auditor-ready CSV export (Step 18).

Three export schemas:
- ``auditor`` (default): 15 columns, one row per individual, gov ID masked
- ``minimal``: 3 columns (name, notification_required, review_status)
- ``full``: auditor + raw_name, raw_email, raw_phone, raw_address
  (only available in INVESTIGATION mode)
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExportColumn:
    """A single column in an export schema."""

    name: str                          # CSV header name
    source_field: str                  # field on SubjectRow / NotificationSubject
    mask_strategy: str | None = None   # "email", "phone", "address", "gov_id", or None
    required: bool = False


# ---------------------------------------------------------------------------
# Auditor schema — 15 columns
# ---------------------------------------------------------------------------

AUDITOR_EXPORT_COLUMNS: list[ExportColumn] = [
    ExportColumn(name="individual_id", source_field="individual_id", required=True),
    ExportColumn(name="name", source_field="canonical_name", required=True),
    ExportColumn(name="address", source_field="canonical_address", mask_strategy="address"),
    ExportColumn(name="date_of_birth", source_field="date_of_birth"),
    ExportColumn(name="government_id", source_field="government_id", mask_strategy="gov_id"),
    ExportColumn(name="government_id_type", source_field="government_id_type"),
    ExportColumn(name="email", source_field="canonical_email", mask_strategy="email"),
    ExportColumn(name="phone", source_field="canonical_phone", mask_strategy="phone"),
    ExportColumn(name="pii_types_found", source_field="pii_types_list"),
    ExportColumn(name="source_document", source_field="source_document_name"),
    ExportColumn(name="source_pages", source_field="source_page_range"),
    ExportColumn(name="extraction_confidence", source_field="extraction_confidence"),
    ExportColumn(name="merge_confidence", source_field="merge_confidence"),
    ExportColumn(name="review_status", source_field="review_status", required=True),
    ExportColumn(name="notification_required", source_field="notification_required", required=True),
]


# ---------------------------------------------------------------------------
# Minimal schema — 3 columns
# ---------------------------------------------------------------------------

MINIMAL_EXPORT_COLUMNS: list[ExportColumn] = [
    ExportColumn(name="name", source_field="canonical_name", required=True),
    ExportColumn(name="notification_required", source_field="notification_required", required=True),
    ExportColumn(name="review_status", source_field="review_status", required=True),
]


# ---------------------------------------------------------------------------
# Full schema — auditor + raw fields (INVESTIGATION mode only)
# ---------------------------------------------------------------------------

FULL_EXPORT_COLUMNS: list[ExportColumn] = AUDITOR_EXPORT_COLUMNS + [
    ExportColumn(name="raw_name", source_field="raw_name"),
    ExportColumn(name="raw_email", source_field="raw_email"),
    ExportColumn(name="raw_phone", source_field="raw_phone"),
    ExportColumn(name="raw_address", source_field="raw_address"),
]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

EXPORT_SCHEMAS: dict[str, list[ExportColumn]] = {
    "auditor": AUDITOR_EXPORT_COLUMNS,
    "minimal": MINIMAL_EXPORT_COLUMNS,
    "full": FULL_EXPORT_COLUMNS,
}
