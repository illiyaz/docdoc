"""CSV export for NotificationSubjects.

Takes a project_id and optional configuration (export_fields from a protocol
config, confidence threshold, entity type filters) and writes a CSV file
containing canonical/normalized/masked data.  Never exports raw PII.

Pure logic is separated from ORM so it can be unit-tested without a database.
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

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
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
# Pure helpers
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


def _mask_address(addr: dict | None) -> str:
    """Mask address — show only state and zip."""
    if addr is None:
        return ""
    parts: list[str] = []
    if addr.get("state"):
        parts.append(str(addr["state"]))
    if addr.get("zip"):
        parts.append(str(addr["zip"]))
    return ", ".join(parts) if parts else "***"


def _format_value(field: str, value: Any) -> str:
    """Convert a NotificationSubject field value to a safe CSV string.

    Applies masking to PII-sensitive fields.  JSON-serializable lists/dicts
    are rendered as compact JSON.
    """
    if value is None:
        return ""
    if field == "canonical_email":
        return _mask_email(value)
    if field == "canonical_phone":
        return _mask_phone(value)
    if field == "canonical_address":
        return _mask_address(value)
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

    @classmethod
    def from_orm(cls, ns: NotificationSubject) -> SubjectRow:
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
        )

    def get(self, field: str) -> Any:
        return getattr(self, field, None)


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
            protocol_config_id=optional_uuid,
            filters=optional_dict,
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
    ) -> ExportJob:
        """Execute the export and return the completed ExportJob record."""
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
            # 2. Resolve export fields.
            protocol_config: ProtocolConfig | None = None
            if protocol_config_id is not None:
                protocol_config = self._db.get(ProtocolConfig, protocol_config_id)

            fields = resolve_export_fields(protocol_config)

            # 3. Query subjects.
            stmt = select(NotificationSubject).where(
                NotificationSubject.project_id == project_id,
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
                if "entity_types" in filters:
                    # JSON array containment is DB-specific; for simplicity
                    # we filter in Python after fetching.
                    pass

            subjects = self._db.execute(stmt).scalars().all()

            # In-Python filter for entity_types (works with SQLite + Postgres).
            if filters and "entity_types" in filters:
                wanted = set(filters["entity_types"])
                subjects = [
                    s for s in subjects
                    if s.pii_types_found and wanted.intersection(s.pii_types_found)
                ]

            rows = [SubjectRow.from_orm(s) for s in subjects]

            # 4. Build CSV content.
            csv_content = build_csv_content(rows, fields)

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
