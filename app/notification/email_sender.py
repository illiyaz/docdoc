"""SMTP email sender — Phase 3.

Delivers breach notification emails via a local SMTP relay.  No cloud
email APIs.  Rate limited to 100 messages per minute.  Retries up to 3
times with exponential backoff before marking as ``FAILED``.

Safety: ``canonical_email`` is never logged — only ``subject_id``.
"""
from __future__ import annotations

import logging
import smtplib
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from string import Template
from typing import Literal

from app.db.models import NotificationList, NotificationSubject
from app.protocols.protocol import Protocol

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_BACKOFF_BASE = 1  # seconds: 1, 2, 4


# ---------------------------------------------------------------------------
# DeliveryReceipt
# ---------------------------------------------------------------------------

@dataclass
class DeliveryReceipt:
    """Record of a single notification delivery attempt."""

    subject_id: str
    email: str
    status: Literal["SENT", "FAILED", "SKIPPED"]
    timestamp: datetime
    smtp_response: str | None
    attempt_count: int


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------

def _load_template(template_dir: str | Path, protocol_id: str) -> str:
    """Return the HTML template string for *protocol_id*.

    Falls back to ``default_email.html`` when a protocol-specific
    template is not found.
    """
    template_dir = Path(template_dir)
    protocol_path = template_dir / f"{protocol_id}_email.html"
    if protocol_path.is_file():
        return protocol_path.read_text(encoding="utf-8")

    default_path = template_dir / "default_email.html"
    if default_path.is_file():
        return default_path.read_text(encoding="utf-8")

    raise FileNotFoundError(
        f"No template found for protocol {protocol_id!r} "
        f"and no default_email.html in {template_dir}"
    )


def _render(
    template_html: str,
    subject: NotificationSubject,
    protocol: Protocol,
) -> str:
    """Substitute placeholders into *template_html*."""
    pii_types = ", ".join(subject.pii_types_found or [])
    return Template(template_html).safe_substitute(
        subject_name=subject.canonical_name or "Affected Individual",
        breach_date=datetime.now(timezone.utc).strftime("%B %d, %Y"),
        pii_types=pii_types,
        phi_types=pii_types,
        contact_info=subject.canonical_email or "",
        regulatory_framework=protocol.regulatory_framework,
    )


# ---------------------------------------------------------------------------
# EmailSender
# ---------------------------------------------------------------------------

class EmailSender:
    """Send breach notification emails via SMTP with rate limiting."""

    def __init__(
        self,
        smtp_host: str,
        smtp_port: int = 587,
        rate_limit_per_minute: int = 100,
    ) -> None:
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.rate_limit_per_minute = rate_limit_per_minute

    # -- single send --------------------------------------------------------

    def send_notification(
        self,
        subject: NotificationSubject,
        protocol: Protocol,
        template_dir: str | Path,
    ) -> DeliveryReceipt:
        """Render template, send via SMTP, return receipt."""
        sid = str(subject.subject_id)

        if not subject.canonical_email:
            logger.info("Subject %s has no email — skipping", sid)
            return DeliveryReceipt(
                subject_id=sid,
                email="",
                status="SKIPPED",
                timestamp=datetime.now(timezone.utc),
                smtp_response=None,
                attempt_count=0,
            )

        template_html = _load_template(template_dir, protocol.protocol_id)
        body = _render(template_html, subject, protocol)

        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Important Notice: {protocol.name}"
        msg["From"] = f"noreply@notifications.local"
        msg["To"] = subject.canonical_email
        msg.attach(MIMEText(body, "html"))

        last_error: str | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                    server.sendmail(msg["From"], [subject.canonical_email], msg.as_string())
                logger.info("Delivered notification for subject %s (attempt %d)", sid, attempt)
                return DeliveryReceipt(
                    subject_id=sid,
                    email=subject.canonical_email,
                    status="SENT",
                    timestamp=datetime.now(timezone.utc),
                    smtp_response="250 OK",
                    attempt_count=attempt,
                )
            except smtplib.SMTPException as exc:
                last_error = str(exc)
                logger.warning(
                    "SMTP error for subject %s attempt %d: %s", sid, attempt, last_error
                )
                if attempt < _MAX_RETRIES:
                    time.sleep(_BACKOFF_BASE * (2 ** (attempt - 1)))

        logger.error("Delivery failed for subject %s after %d attempts", sid, _MAX_RETRIES)
        return DeliveryReceipt(
            subject_id=sid,
            email=subject.canonical_email,
            status="FAILED",
            timestamp=datetime.now(timezone.utc),
            smtp_response=last_error,
            attempt_count=_MAX_RETRIES,
        )

    # -- batch send ---------------------------------------------------------

    def send_all(
        self,
        notification_list: NotificationList,
        subjects: list[NotificationSubject],
        protocol: Protocol,
        template_dir: str | Path,
    ) -> list[DeliveryReceipt]:
        """Send notifications to all *subjects*, respecting rate limit."""
        receipts: list[DeliveryReceipt] = []
        interval = 60.0 / self.rate_limit_per_minute if self.rate_limit_per_minute > 0 else 0

        for i, subj in enumerate(subjects):
            if i > 0 and interval > 0:
                time.sleep(interval)
            receipt = self.send_notification(subj, protocol, template_dir)
            receipts.append(receipt)

        notification_list.status = "DELIVERED"
        return receipts
