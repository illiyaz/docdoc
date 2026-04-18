"""Notification preview, approval, and delivery dashboard routes.

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

from app.api.deps import get_db
from app.db.models import NotificationSubject

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/notifications", tags=["notifications"])

# Default template directory
_DEFAULT_TEMPLATE_DIR = Path(__file__).resolve().parent.parent.parent / "notification" / "templates"


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
    db: Session = Depends(get_db),
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
    db: Session = Depends(get_db),
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


# ---------------------------------------------------------------------------
# Delivery dashboard (#10)
# ---------------------------------------------------------------------------

@router.get("/subjects/{project_id}")
def get_project_subjects(
    project_id: UUID,
    db: Session = Depends(get_db),
):
    """Return all notification subjects for a project with masked PII."""
    subjects = (
        db.query(NotificationSubject)
        .filter(NotificationSubject.project_id == project_id)
        .order_by(NotificationSubject.canonical_name)
        .all()
    )

    return [
        {
            "subject_id": str(s.subject_id),
            "name": _mask_name(s.canonical_name),
            "email": _mask_email(s.canonical_email),
            "phone": s.canonical_phone[:3] + "***" if s.canonical_phone and len(s.canonical_phone) > 3 else None,
            "review_status": s.review_status,
            "notification_required": s.notification_required,
            "merge_confidence": s.merge_confidence,
            "pii_types": s.pii_types_found or [],
            "source_document": s.source_document_name,
        }
        for s in subjects
    ]


@router.get("/delivery-status/{project_id}")
def get_delivery_status(
    project_id: UUID,
    db: Session = Depends(get_db),
):
    """Return notification delivery summary for a project.

    Uses the existing ``review_status`` field on NotificationSubject:
    APPROVED = ready to send, NOTIFIED = sent, REJECTED = not sending.
    """
    from sqlalchemy import func as sa_func

    subjects = (
        db.query(NotificationSubject)
        .filter(NotificationSubject.project_id == project_id)
        .all()
    )

    total = len(subjects)
    by_status: dict[str, int] = {}
    for s in subjects:
        by_status[s.review_status] = by_status.get(s.review_status, 0) + 1

    notif_required = sum(1 for s in subjects if s.notification_required)
    approved = by_status.get("APPROVED", 0)
    notified = by_status.get("NOTIFIED", 0)
    rejected = by_status.get("REJECTED", 0)
    pending = by_status.get("AI_PENDING", 0) + by_status.get("HUMAN_REVIEW", 0) + by_status.get("LEGAL_REVIEW", 0)

    # Subject-level detail (masked)
    subject_list = [
        {
            "subject_id": str(s.subject_id),
            "name": _mask_name(s.canonical_name),
            "review_status": s.review_status,
            "notification_required": s.notification_required,
        }
        for s in subjects
        if s.notification_required
    ]

    return {
        "project_id": str(project_id),
        "total_subjects": total,
        "notification_required": notif_required,
        "summary": {
            "approved_ready": approved,
            "notified_sent": notified,
            "rejected": rejected,
            "pending_review": pending,
        },
        "subjects": subject_list,
    }


# ---------------------------------------------------------------------------
# Status transitions + email send (Step 39 #2)
# ---------------------------------------------------------------------------

_VALID_STATUSES = {
    "AI_PENDING",
    "HUMAN_REVIEW",
    "LEGAL_REVIEW",
    "APPROVED",
    "REJECTED",
    "NOTIFIED",
}


class StatusUpdate(BaseModel):
    review_status: str
    reviewer_id: str | None = None


@router.patch("/subjects/{subject_id}/status")
def update_subject_status(
    subject_id: UUID,
    body: StatusUpdate,
    db: Session = Depends(get_db),
):
    """Set ``review_status`` on a NotificationSubject.

    Enforces the enumerated set of valid states. Used by the Notification
    tab to approve / reject / restore a subject before sending.
    """
    status = body.review_status.upper()
    if status not in _VALID_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid review_status {status!r}. "
                   f"Must be one of: {sorted(_VALID_STATUSES)}",
        )

    subj = db.query(NotificationSubject).filter(
        NotificationSubject.subject_id == subject_id
    ).one_or_none()
    if subj is None:
        raise HTTPException(status_code=404, detail=f"Subject {subject_id} not found")

    subj.review_status = status
    db.commit()

    return {
        "subject_id": str(subj.subject_id),
        "review_status": subj.review_status,
    }


def _default_protocol() -> "object":
    """Return a minimal Protocol for email rendering when project has none.

    Notification-preview already assumes a "default" protocol_id; match it
    so the same default template renders in both preview and send paths.
    """
    from app.protocols.protocol import Protocol
    return Protocol(
        protocol_id="default",
        name="Breach Notification",
        jurisdiction="unspecified",
        triggering_entity_types=[],
        notification_threshold=0,
        notification_deadline_days=60,
        required_notification_content=[],
        regulatory_framework="generic",
    )


@router.post("/subjects/{subject_id}/send-email")
def send_subject_email(
    subject_id: UUID,
    db: Session = Depends(get_db),
):
    """Send the notification email for a single subject.

    Guarded: subject must be in APPROVED state. On SENT, flips to NOTIFIED.
    Uses SMTP config from settings (mailpit in dev). Safe to call in dev —
    mailpit catches messages, no external delivery.
    """
    from app.core.settings import get_settings
    from app.notification.email_sender import EmailSender

    subj = db.query(NotificationSubject).filter(
        NotificationSubject.subject_id == subject_id
    ).one_or_none()
    if subj is None:
        raise HTTPException(status_code=404, detail=f"Subject {subject_id} not found")

    if subj.review_status != "APPROVED":
        raise HTTPException(
            status_code=409,
            detail=f"Subject is {subj.review_status!r} — only APPROVED subjects can be sent.",
        )

    if not subj.canonical_email:
        raise HTTPException(
            status_code=422,
            detail="Subject has no canonical_email — cannot send.",
        )

    settings = get_settings()
    sender = EmailSender(
        smtp_host=settings.smtp_host,
        smtp_port=settings.smtp_port,
    )

    receipt = sender.send_notification(
        subject=subj,
        protocol=_default_protocol(),
        template_dir=_DEFAULT_TEMPLATE_DIR,
    )

    if receipt.status == "SENT":
        subj.review_status = "NOTIFIED"
        db.commit()

    return {
        "subject_id": str(subj.subject_id),
        "status": receipt.status,
        "review_status": subj.review_status,
        "attempt_count": receipt.attempt_count,
        "smtp_response": receipt.smtp_response,
        "timestamp": receipt.timestamp.isoformat(),
    }


@router.post("/projects/{project_id}/send-batch")
def send_project_batch(
    project_id: UUID,
    db: Session = Depends(get_db),
):
    """Send notification emails to every APPROVED subject in the project.

    Each successful send flips the subject to NOTIFIED. Returns per-subject
    receipts so the UI can show which failed and why. Respects the
    EmailSender rate limit (100/min default).
    """
    from app.core.settings import get_settings
    from app.notification.email_sender import EmailSender

    subjects = (
        db.query(NotificationSubject)
        .filter(
            NotificationSubject.project_id == project_id,
            NotificationSubject.review_status == "APPROVED",
        )
        .all()
    )

    if not subjects:
        return {"project_id": str(project_id), "total": 0, "sent": 0, "failed": 0,
                "skipped": 0, "receipts": []}

    settings = get_settings()
    sender = EmailSender(
        smtp_host=settings.smtp_host,
        smtp_port=settings.smtp_port,
    )
    protocol = _default_protocol()

    receipts = []
    for subj in subjects:
        r = sender.send_notification(
            subject=subj,
            protocol=protocol,
            template_dir=_DEFAULT_TEMPLATE_DIR,
        )
        if r.status == "SENT":
            subj.review_status = "NOTIFIED"
        receipts.append({
            "subject_id": r.subject_id,
            "status": r.status,
            "attempt_count": r.attempt_count,
            "smtp_response": r.smtp_response,
        })
    db.commit()

    sent = sum(1 for r in receipts if r["status"] == "SENT")
    failed = sum(1 for r in receipts if r["status"] == "FAILED")
    skipped = sum(1 for r in receipts if r["status"] == "SKIPPED")

    return {
        "project_id": str(project_id),
        "total": len(receipts),
        "sent": sent,
        "failed": failed,
        "skipped": skipped,
        "receipts": receipts,
    }
