"""CSV export for NotificationSubjects.

Step 18 rewrite: schema-driven auditor-ready export with lineage columns.

Three export schemas:
- ``auditor`` (default): 15 columns, one row per individual, gov ID masked
- ``minimal``: 3 columns (name, notification_required, review_status)
- ``full``: auditor + raw fields (INVESTIGATION mode only)

Backward-compatible: old ``resolve_export_fields`` and ``DEFAULT_EXPORT_FIELDS``
are preserved for existing callers.
"""
from __future__ import annotations

import csv
import io
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ExportJob, NotificationSubject, ProtocolConfig
from app.export.export_schema import EXPORT_SCHEMAS, ExportColumn

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Legacy constants (backward compat for existing callers / tests)
# ---------------------------------------------------------------------------

#: Default columns when no export_fields are configured.
DEFAULT_EXPORT_FIELDS: list[str] = [
    "canonical_name",
    "canonical_email",
    "canonical_phone",
    "pii_types_found",
    "merge_confidence",
    "review_status",
]

#: All columns that are safe to export (no raw PII).
ALLOWED_EXPORT_FIELDS: frozenset[str] = frozenset({
    "subject_id",
    "canonical_name",
    "canonical_email",
    "canonical_phone",
    "canonical_address",
    "pii_types_found",
    "source_records",
    "merge_confidence",
    "notification_required",
    "review_status",
})


# ---------------------------------------------------------------------------
# Pure masking helpers
# ---------------------------------------------------------------------------


def _mask_email(email: str | None) -> str:
    """Mask an email address for export.  Returns '***@***.***' or empty."""
    if not email:
        return ""
    return "***@***.***"


def _mask_phone(phone: str | None) -> str:
    """Mask a phone number — show last 4 digits only."""
    if not phone:
        return ""
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) >= 4:
        return f"***-***-{digits[-4:]}"
    return "***"


def format_address(addr: Any) -> str:
    """Convert address dict/JSON to a readable string.

    Handles multiple formats:
    - ``{"raw": "85 Waltings Gardens"}`` → ``"85 Waltings Gardens"``
    - ``{"street": "85 Waltings", "city": "London", "postcode": "NW2 3UD"}``
      → ``"85 Waltings, London, NW2 3UD"``
    - ``"85 Waltings Gardens"`` (already a string) → passthrough
    - ``None`` → ``""``
    """
    if addr is None:
        return ""
    if isinstance(addr, str):
        return addr
    if isinstance(addr, dict):
        if "raw" in addr:
            return str(addr["raw"])
        parts: list[str] = []
        for key in ("street", "city", "county", "state", "postcode", "zip", "country"):
            val = addr.get(key)
            if val:
                parts.append(str(val))
        return ", ".join(parts) if parts else str(addr)
    return str(addr)


def _mask_address(addr: dict | str | None) -> str:
    """Mask address — show only state/county and postcode/zip."""
    if addr is None:
        return ""
    if isinstance(addr, str):
        return "***"
    if isinstance(addr, dict):
        parts: list[str] = []
        for key in ("state", "county", "postcode", "zip"):
            val = addr.get(key)
            if val:
                parts.append(str(val))
        return ", ".join(parts) if parts else "***"
    return "***"


def _mask_gov_id(value: str | None) -> str:
    """Mask a government ID — show first 3 and last 2 chars.

    Examples:
        "NE724362D" → "NE7****2D"
        "AB1"       → "***"
        ""          → "***"
    """
    if not value or len(value) < 5:
        return "***"
    return value[:3] + "*" * (len(value) - 5) + value[-2:]


# ---------------------------------------------------------------------------
# Legacy format_value (used by old build_csv_content path)
# ---------------------------------------------------------------------------


def _format_value(field: str, value: Any) -> str:
    """Convert a NotificationSubject field value to a safe CSV string.

    Applies masking to PII-sensitive fields when pii_masking_enabled is True.
    JSON-serializable lists/dicts are rendered as compact JSON.
    """
    if value is None:
        return ""

    from app.core.settings import get_settings
    masking_on = get_settings().pii_masking_enabled

    if masking_on:
        if field == "canonical_email":
            return _mask_email(value)
        if field == "canonical_phone":
            return _mask_phone(value)
        if field == "canonical_address":
            return _mask_address(value)
    # Format addresses as readable strings even when masking is off
    if field in ("canonical_address", "raw_address") and isinstance(value, dict):
        return format_address(value)
    if isinstance(value, (list, dict)):
        return json.dumps(value, separators=(",", ":"))
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def resolve_export_fields(
    protocol_config: ProtocolConfig | None = None,
) -> list[str]:
    """Determine export columns from a protocol config, falling back to defaults.

    Only fields present in ``ALLOWED_EXPORT_FIELDS`` are returned; unknown
    fields in the config are silently dropped.
    """
    if protocol_config is not None:
        config = protocol_config.config_json or {}
        raw_fields = config.get("export_fields")
        if raw_fields and isinstance(raw_fields, list):
            validated = [f for f in raw_fields if f in ALLOWED_EXPORT_FIELDS]
            if validated:
                return validated
    return list(DEFAULT_EXPORT_FIELDS)


# ---------------------------------------------------------------------------
# SubjectRow — extended with lineage fields (Step 18)
# ---------------------------------------------------------------------------


@dataclass
class SubjectRow:
    """Lightweight projection of a NotificationSubject for export."""

    subject_id: str
    canonical_name: str | None
    canonical_email: str | None
    canonical_phone: str | None
    canonical_address: dict | None
    pii_types_found: list | None
    source_records: list | None
    merge_confidence: float | None
    notification_required: bool
    review_status: str
    # Step 18 lineage fields
    individual_id: int = 0
    source_document_name: str | None = None
    source_page_range: str | None = None
    government_id_type: str | None = None
    extraction_confidence: float | None = None
    pii_types_list: str | None = None
    date_of_birth: str | None = None
    government_id: str | None = None
    # Full schema raw fields (INVESTIGATION only)
    raw_name: str | None = None
    raw_email: str | None = None
    raw_phone: str | None = None
    raw_address: str | None = None

    @classmethod
    def from_orm(cls, ns: NotificationSubject, *, individual_id: int = 0) -> SubjectRow:
        return cls(
            subject_id=str(ns.subject_id),
            canonical_name=ns.canonical_name,
            canonical_email=ns.canonical_email,
            canonical_phone=ns.canonical_phone,
            canonical_address=ns.canonical_address,
            pii_types_found=ns.pii_types_found,
            source_records=ns.source_records,
            merge_confidence=ns.merge_confidence,
            notification_required=ns.notification_required,
            review_status=ns.review_status,
            individual_id=individual_id,
            source_document_name=ns.source_document_name,
            source_page_range=ns.source_page_range,
            government_id_type=ns.government_id_type,
            extraction_confidence=ns.extraction_confidence,
            pii_types_list=ns.pii_types_list,
        )

    def get(self, field: str) -> Any:
        return getattr(self, field, None)


# ---------------------------------------------------------------------------
# Schema-driven CSV building (Step 18)
# ---------------------------------------------------------------------------


def _get_field_value(row: SubjectRow, col: ExportColumn) -> Any:
    """Retrieve the value for a column from a SubjectRow."""
    return getattr(row, col.source_field, None)


def _apply_mask(value: Any, col: ExportColumn) -> str:
    """Apply masking strategy to a value for CSV output."""
    if value is None:
        return ""

    # Format addresses and phones before masking
    if col.mask_strategy == "address" or col.source_field in ("canonical_address", "raw_address"):
        value = format_address(value)
    if isinstance(value, dict) and col.source_field in ("canonical_phone",):
        value = str(value.get("raw", value)) if "raw" in value else str(value)

    from app.core.settings import get_settings
    masking_on = get_settings().pii_masking_enabled

    if masking_on and col.mask_strategy:
        if col.mask_strategy == "email":
            return _mask_email(value)
        if col.mask_strategy == "phone":
            return _mask_phone(value)
        if col.mask_strategy == "address":
            return _mask_address(value)
        if col.mask_strategy == "gov_id":
            return _mask_gov_id(value)

    if isinstance(value, (list, dict)):
        return json.dumps(value, separators=(",", ":"))
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def build_csv_content_v2(
    rows: list[SubjectRow],
    columns: list[ExportColumn],
) -> str:
    """Build schema-driven CSV content.  Pure function — no DB or IO."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([c.name for c in columns])
    for row in rows:
        writer.writerow([
            _apply_mask(_get_field_value(row, c), c) for c in columns
        ])
    return buf.getvalue()


# Legacy build_csv_content — kept for backward compat
def build_csv_content(
    rows: list[SubjectRow],
    fields: list[str],
) -> str:
    """Build CSV content as a string.  Pure function — no DB or IO.

    Returns a string containing the CSV header + data rows with masked PII.
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(fields)
    for row in rows:
        writer.writerow([_format_value(f, row.get(f)) for f in fields])
    return buf.getvalue()


# ---------------------------------------------------------------------------
# ORM-integrated exporter
# ---------------------------------------------------------------------------


class CSVExporter:
    """Queries NotificationSubjects for a project and writes a CSV file.

    Usage::

        exporter = CSVExporter(db_session)
        export_job = exporter.run(
            project_id=uuid,
            output_dir=Path("/tmp/exports"),
            export_schema="auditor",
        )
    """

    def __init__(self, db: Session) -> None:
        self._db = db

    def run(
        self,
        project_id: UUID,
        output_dir: Path,
        *,
        protocol_config_id: UUID | None = None,
        filters: dict | None = None,
        export_schema: str = "auditor",
    ) -> ExportJob:
        """Execute the export and return the completed ExportJob record."""
        from app.core.settings import get_settings
        settings = get_settings()

        # Validate schema
        if export_schema not in EXPORT_SCHEMAS:
            raise ValueError(f"Unknown export schema: {export_schema!r}. Valid: {sorted(EXPORT_SCHEMAS)}")

        # Reject "full" in STRICT mode (pii_masking_enabled = True)
        if export_schema == "full" and settings.pii_masking_enabled:
            raise ValueError(
                "Export schema 'full' is not available in STRICT mode "
                "(pii_masking_enabled=True). Use 'auditor' or 'minimal'."
            )

        columns = EXPORT_SCHEMAS[export_schema]

        # 1. Create the ExportJob record (pending).
        export_job = ExportJob(
            project_id=project_id,
            protocol_config_id=protocol_config_id,
            export_type="csv",
            status="pending",
            filters_json=filters,
        )
        self._db.add(export_job)
        self._db.flush()

        try:
            # 2. Query subjects ordered by canonical_name.
            stmt = (
                select(NotificationSubject)
                .where(NotificationSubject.project_id == project_id)
                .order_by(NotificationSubject.canonical_name)
            )

            # Apply optional filters.
            if filters:
                if "confidence_threshold" in filters:
                    threshold = float(filters["confidence_threshold"])
                    stmt = stmt.where(
                        NotificationSubject.merge_confidence >= threshold,
                    )
                if "review_status" in filters:
                    stmt = stmt.where(
                        NotificationSubject.review_status == filters["review_status"],
                    )

            subjects = self._db.execute(stmt).scalars().all()

            # In-Python filter for entity_types (works with SQLite + Postgres).
            if filters and "entity_types" in filters:
                wanted = set(filters["entity_types"])
                subjects = [
                    s for s in subjects
                    if s.pii_types_found and wanted.intersection(s.pii_types_found)
                ]

            # 3. Build SubjectRows with auto-incrementing individual_id
            rows = [
                SubjectRow.from_orm(s, individual_id=i)
                for i, s in enumerate(subjects, 1)
            ]

            # 4. Build CSV content.
            csv_content = build_csv_content_v2(rows, columns)

            # 5. Write to file.
            output_dir.mkdir(parents=True, exist_ok=True)
            file_name = f"export_{export_job.id}.csv"
            file_path = output_dir / file_name
            file_path.write_text(csv_content, encoding="utf-8")

            # 6. Update job record.
            export_job.status = "completed"
            export_job.file_path = str(file_path)
            export_job.row_count = len(rows)
            export_job.completed_at = datetime.now(timezone.utc)
            self._db.flush()

        except Exception:
            export_job.status = "failed"
            self._db.flush()
            raise

        return export_job
