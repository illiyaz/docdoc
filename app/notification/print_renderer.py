"""Print-ready PDF renderer — Phase 3.

Generates print-ready PDF letters for postal delivery using WeasyPrint.
Output is written to ``{output_dir}/{subject_id}_letter.pdf`` with a
``manifest.csv`` mapping subject IDs to letter filenames.

Safety: address and name values are never logged — only ``subject_id``.
"""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from string import Template
from typing import Literal

from app.db.models import NotificationList, NotificationSubject
from app.protocols.protocol import Protocol

logger = logging.getLogger(__name__)

_MANIFEST_COLUMNS = [
    "subject_id",
    "canonical_name",
    "street",
    "city",
    "state",
    "zip",
    "country",
    "letter_filename",
    "status",
    "error",
]


# ---------------------------------------------------------------------------
# LetterManifestEntry
# ---------------------------------------------------------------------------

@dataclass
class LetterManifestEntry:
    """Record of a single letter rendering attempt."""

    subject_id: str
    canonical_name: str
    canonical_address: dict | None
    letter_filename: str
    status: Literal["RENDERED", "FAILED", "SKIPPED"]
    error: str | None = None


# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------

def _load_template(template_dir: Path, protocol_id: str) -> str:
    protocol_path = template_dir / f"{protocol_id}_letter.html"
    if protocol_path.is_file():
        return protocol_path.read_text(encoding="utf-8")

    default_path = template_dir / "default_letter.html"
    if default_path.is_file():
        return default_path.read_text(encoding="utf-8")

    raise FileNotFoundError(
        f"No letter template for {protocol_id!r} "
        f"and no default_letter.html in {template_dir}"
    )


def _render_html(
    template_html: str,
    subject: NotificationSubject,
    protocol: Protocol,
) -> str:
    addr = subject.canonical_address or {}
    pii_types = ", ".join(subject.pii_types_found or [])
    return Template(template_html).safe_substitute(
        subject_name=subject.canonical_name or "Affected Individual",
        street=addr.get("street", ""),
        city=addr.get("city", ""),
        state=addr.get("state", ""),
        zip=addr.get("zip", ""),
        country=addr.get("country", ""),
        breach_date=datetime.now(timezone.utc).strftime("%B %d, %Y"),
        pii_types=pii_types,
        phi_types=pii_types,
        contact_info=subject.canonical_email or "",
        regulatory_framework=protocol.regulatory_framework,
    )


# ---------------------------------------------------------------------------
# PrintRenderer
# ---------------------------------------------------------------------------

class PrintRenderer:
    """Render print-ready PDF notification letters via WeasyPrint."""

    def __init__(
        self,
        output_dir: str | Path,
        template_dir: str | Path,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.template_dir = Path(template_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    # -- single render ------------------------------------------------------

    def render_letter(
        self,
        subject: NotificationSubject,
        protocol: Protocol,
    ) -> LetterManifestEntry:
        """Render one PDF letter for *subject*."""
        sid = str(subject.subject_id)
        name = subject.canonical_name or ""
        filename = f"{sid}_letter.pdf"

        if not subject.canonical_address:
            logger.info("Subject %s has no address — skipping letter", sid)
            return LetterManifestEntry(
                subject_id=sid,
                canonical_name=name,
                canonical_address=None,
                letter_filename="",
                status="SKIPPED",
            )

        try:
            template_html = _load_template(self.template_dir, protocol.protocol_id)
            html_content = _render_html(template_html, subject, protocol)

            import weasyprint  # lazy import — optional dependency

            out_path = self.output_dir / filename
            weasyprint.HTML(string=html_content).write_pdf(str(out_path))

            logger.info("Rendered letter for subject %s", sid)
            return LetterManifestEntry(
                subject_id=sid,
                canonical_name=name,
                canonical_address=subject.canonical_address,
                letter_filename=filename,
                status="RENDERED",
            )
        except Exception as exc:
            logger.error("Failed to render letter for subject %s: %s", sid, exc)
            return LetterManifestEntry(
                subject_id=sid,
                canonical_name=name,
                canonical_address=subject.canonical_address,
                letter_filename=filename,
                status="FAILED",
                error=str(exc),
            )

    # -- batch render -------------------------------------------------------

    def render_all(
        self,
        notification_list: NotificationList,
        subjects: list[NotificationSubject],
        protocol: Protocol,
    ) -> list[LetterManifestEntry]:
        """Render letters for all *subjects*."""
        return [self.render_letter(s, protocol) for s in subjects]

    # -- manifest -----------------------------------------------------------

    def write_manifest(
        self,
        entries: list[LetterManifestEntry],
        job_id: str,
    ) -> Path:
        """Write manifest CSV and return its path."""
        manifest_path = self.output_dir / f"{job_id}_manifest.csv"
        with open(manifest_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=_MANIFEST_COLUMNS)
            writer.writeheader()
            for e in entries:
                addr = e.canonical_address or {}
                writer.writerow({
                    "subject_id": e.subject_id,
                    "canonical_name": e.canonical_name,
                    "street": addr.get("street", ""),
                    "city": addr.get("city", ""),
                    "state": addr.get("state", ""),
                    "zip": addr.get("zip", ""),
                    "country": addr.get("country", ""),
                    "letter_filename": e.letter_filename,
                    "status": e.status,
                    "error": e.error or "",
                })
        return manifest_path
