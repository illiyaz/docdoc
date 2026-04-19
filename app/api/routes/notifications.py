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

    def _first_doc_id(s: NotificationSubject) -> str | None:
        """Pull the first source_document_id from source_records JSON so the
        UI can open a DocumentViewer modal directly from the row."""
        try:
            records = s.source_records or []
            if isinstance(records, list) and records:
                first = records[0]
                if isinstance(first, dict):
                    did = first.get("source_document_id")
                    return str(did) if did else None
        except Exception:
            pass
        return None

    def _first_page(s: NotificationSubject) -> int | None:
        """Extract the first page number from source_page_range ("17" or "3, 5, 9")."""
        if not s.source_page_range:
            return None
        first = s.source_page_range.split(",")[0].strip()
        if first.isdigit():
            return int(first)
        return None

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
            "source_document_id": _first_doc_id(s),
            "source_page": _first_page(s),
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
    """Fallback Protocol when the project has no active ProtocolConfig.

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


def _protocol_for_project(db: "Session", project_id: "UUID") -> "object":
    """Return the Protocol bound to *project_id*'s active ProtocolConfig.

    Lookup order:
      1. ProtocolConfig where project_id matches AND status == 'active'
      2. Any ProtocolConfig for the project (first one found)
      3. Fallback to `_default_protocol()` with generic template

    When a ProtocolConfig is found, its ``base_protocol_id`` is used to
    resolve the Protocol from the global registry (loaded from
    config/protocols/*.yaml). The ``config_json`` overlay is merged into
    the base Protocol's attributes so project-level tweaks stick.
    """
    from app.db.models import ProtocolConfig

    pc = (
        db.query(ProtocolConfig)
        .filter(ProtocolConfig.project_id == project_id)
        .order_by(ProtocolConfig.status.desc(), ProtocolConfig.updated_at.desc())
        .first()
    )
    if pc is None or not pc.base_protocol_id:
        return _default_protocol()

    # Resolve the base Protocol from the registry (YAML-defined).
    try:
        from app.protocols.registry import ProtocolRegistry
        registry = ProtocolRegistry.default()
        base = registry.get(pc.base_protocol_id)
    except (KeyError, Exception) as e:
        logger.warning(
            "Project %s references unknown base_protocol_id %r: %s — using default",
            project_id, pc.base_protocol_id, e,
        )
        return _default_protocol()

    # Merge ProtocolConfig.config_json overrides on top of the base.
    # Only a few fields are meaningfully configurable per project; ignore
    # unknown keys rather than erroring.
    overrides = pc.config_json or {}
    import dataclasses
    if isinstance(overrides, dict) and dataclasses.is_dataclass(base):
        merged = dataclasses.replace(
            base,
            **{k: v for k, v in overrides.items()
               if k in {"name", "jurisdiction", "notification_threshold",
                        "notification_deadline_days", "regulatory_framework"}},
        )
        return merged
    return base


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

    protocol = _protocol_for_project(db, subj.project_id) if subj.project_id else _default_protocol()
    receipt = sender.send_notification(
        subject=subj,
        protocol=protocol,
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


class BatchStatusUpdate(BaseModel):
    subject_ids: list[UUID]
    review_status: str
    reviewer_id: str | None = None


@router.post("/subjects/batch-update")
def batch_update_status(
    body: BatchStatusUpdate,
    db: Session = Depends(get_db),
):
    """Apply the same ``review_status`` to a list of subjects.

    Used by the Notification tab's bulk-action bar to approve or reject a
    selected set of subjects in one call. Missing IDs are reported in
    ``not_found``; the remainder are updated and persisted together.
    """
    status = body.review_status.upper()
    if status not in _VALID_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid review_status {status!r}. "
                   f"Must be one of: {sorted(_VALID_STATUSES)}",
        )
    if not body.subject_ids:
        return {"updated": 0, "not_found": []}

    subjects = db.query(NotificationSubject).filter(
        NotificationSubject.subject_id.in_(body.subject_ids)
    ).all()
    found_ids = {str(s.subject_id) for s in subjects}

    for s in subjects:
        s.review_status = status
    db.commit()

    not_found = [str(sid) for sid in body.subject_ids if str(sid) not in found_ids]
    return {
        "updated": len(subjects),
        "review_status": status,
        "not_found": not_found,
    }


class BatchSendBody(BaseModel):
    subject_ids: list[UUID]


@router.post("/subjects/batch-send")
def batch_send_selected(
    body: BatchSendBody,
    db: Session = Depends(get_db),
):
    """Send notification emails to a user-selected list of subjects.

    Only subjects in APPROVED state are actually sent — others are
    reported as SKIPPED so the UI can surface why each row didn't flip
    to NOTIFIED. Respects the EmailSender rate limit.
    """
    from app.core.settings import get_settings
    from app.notification.email_sender import EmailSender

    if not body.subject_ids:
        return {"total": 0, "sent": 0, "failed": 0, "skipped": 0, "receipts": []}

    subjects = db.query(NotificationSubject).filter(
        NotificationSubject.subject_id.in_(body.subject_ids)
    ).all()

    settings = get_settings()
    sender = EmailSender(
        smtp_host=settings.smtp_host,
        smtp_port=settings.smtp_port,
    )
    # Prefer the project's configured protocol; batch endpoint has
    # project_id directly, single-select endpoint resolves from subject.
    first_project_id = subjects[0].project_id if subjects else None
    protocol = (
        _protocol_for_project(db, first_project_id) if first_project_id
        else _default_protocol()
    )

    receipts = []
    for subj in subjects:
        if subj.review_status != "APPROVED":
            receipts.append({
                "subject_id": str(subj.subject_id),
                "status": "SKIPPED",
                "smtp_response": f"not APPROVED (state={subj.review_status})",
                "attempt_count": 0,
            })
            continue
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
        "total": len(receipts),
        "sent": sent,
        "failed": failed,
        "skipped": skipped,
        "receipts": receipts,
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
    # Prefer the project's configured protocol; batch endpoint has
    # project_id directly, single-select endpoint resolves from subject.
    first_project_id = subjects[0].project_id if subjects else None
    protocol = (
        _protocol_for_project(db, first_project_id) if first_project_id
        else _default_protocol()
    )

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
