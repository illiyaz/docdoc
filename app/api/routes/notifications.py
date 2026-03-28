"""Notification preview and approval routes (Step 27 — Critical #3).

Allows auditors to preview rendered email/letter notifications with masked
subject data before approving batch delivery.  No notifications are sent
from these endpoints — they only render previews.

Phase 6: add ``Depends(get_current_user)`` + APPROVER role check.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from string import Template
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.models import NotificationSubject

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/notifications", tags=["notifications"])

# Default template directory
_DEFAULT_TEMPLATE_DIR = Path(__file__).resolve().parent.parent.parent / "notification" / "templates"


def _get_db():
    """Placeholder DB dependency — overridden in tests."""
    from app.db.repositories import get_session

    db = get_session()
    try:
        yield db
    finally:
        db.close()


def _mask_name(name: str | None) -> str:
    """Mask a name for preview: show first initial + last name initial."""
    if not name:
        return "Affected Individual"
    parts = name.split()
    if len(parts) == 1:
        return parts[0][0] + "***"
    return parts[0][0] + "*** " + parts[-1][0] + "***"


def _mask_email(email: str | None) -> str:
    """Mask an email for preview."""
    if not email:
        return ""
    parts = email.split("@")
    if len(parts) != 2:
        return "***@***.***"
    local = parts[0]
    return local[0] + "***@" + parts[1]


def _render_email_preview(
    subject: NotificationSubject,
    protocol_id: str,
    template_dir: Path | None = None,
) -> str:
    """Render a notification email with masked subject data. Returns HTML string."""
    tpl_dir = template_dir or _DEFAULT_TEMPLATE_DIR

    # Load template (protocol-specific or default)
    protocol_path = tpl_dir / f"{protocol_id}_email.html"
    default_path = tpl_dir / "default_email.html"
    if protocol_path.is_file():
        template_html = protocol_path.read_text(encoding="utf-8")
    elif default_path.is_file():
        template_html = default_path.read_text(encoding="utf-8")
    else:
        return f"<p>No email template found for protocol {protocol_id}</p>"

    pii_types = ", ".join(subject.pii_types_found or [])
    return Template(template_html).safe_substitute(
        subject_name=_mask_name(subject.canonical_name),
        breach_date=datetime.now(timezone.utc).strftime("%B %d, %Y"),
        pii_types=pii_types,
        phi_types=pii_types,
        contact_info=_mask_email(subject.canonical_email),
        regulatory_framework=protocol_id.replace("_", " ").title(),
    )


def _render_letter_preview(
    subject: NotificationSubject,
    protocol_id: str,
    template_dir: Path | None = None,
) -> str:
    """Render a notification letter with masked subject data. Returns HTML string."""
    tpl_dir = template_dir or _DEFAULT_TEMPLATE_DIR

    protocol_path = tpl_dir / f"{protocol_id}_letter.html"
    default_path = tpl_dir / "default_letter.html"
    if protocol_path.is_file():
        template_html = protocol_path.read_text(encoding="utf-8")
    elif default_path.is_file():
        template_html = default_path.read_text(encoding="utf-8")
    else:
        return f"<p>No letter template found for protocol {protocol_id}</p>"

    addr = subject.canonical_address or {}
    pii_types = ", ".join(subject.pii_types_found or [])
    return Template(template_html).safe_substitute(
        subject_name=_mask_name(subject.canonical_name),
        street=addr.get("street", "***") if addr else "***",
        city=addr.get("city", "***") if addr else "***",
        state=addr.get("state", "**") if addr else "**",
        zip=addr.get("zip", "*****") if addr else "*****",
        country=addr.get("country", "") if addr else "",
        breach_date=datetime.now(timezone.utc).strftime("%B %d, %Y"),
        pii_types=pii_types,
        phi_types=pii_types,
        contact_info=_mask_email(subject.canonical_email),
        regulatory_framework=protocol_id.replace("_", " ").title(),
    )


# ---------------------------------------------------------------------------
# Email preview
# ---------------------------------------------------------------------------

@router.get("/preview/email")
def preview_email(
    subject_id: UUID,
    protocol_id: str = "default",
    db: Session = Depends(_get_db),
):
    """Render a notification email with masked subject data for preview."""
    subject = db.query(NotificationSubject).filter(
        NotificationSubject.subject_id == subject_id
    ).first()
    if not subject:
        raise HTTPException(status_code=404, detail="Subject not found")

    html = _render_email_preview(subject, protocol_id)
    return {
        "subject_id": str(subject.subject_id),
        "subject_name": _mask_name(subject.canonical_name),
        "protocol_id": protocol_id,
        "format": "email",
        "html": html,
    }


# ---------------------------------------------------------------------------
# Letter preview
# ---------------------------------------------------------------------------

@router.get("/preview/letter")
def preview_letter(
    subject_id: str,
    protocol_id: str = "default",
    db: Session = Depends(_get_db),
):
    """Render a notification letter with masked subject data for preview."""
    subject = db.query(NotificationSubject).filter(
        NotificationSubject.subject_id == subject_id
    ).first()
    if not subject:
        raise HTTPException(status_code=404, detail="Subject not found")

    html = _render_letter_preview(subject, protocol_id)
    return {
        "subject_id": str(subject.subject_id),
        "subject_name": _mask_name(subject.canonical_name),
        "protocol_id": protocol_id,
        "format": "letter",
        "html": html,
    }
