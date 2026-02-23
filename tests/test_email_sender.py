"""Tests for app/notification/email_sender.py — Phase 3.

All SMTP calls are mocked — no real server needed.
"""
from __future__ import annotations

import email as email_mod
import smtplib
from unittest.mock import MagicMock, patch, call
from uuid import uuid4

import pytest

from app.db.models import NotificationList, NotificationSubject
from app.notification.email_sender import EmailSender, DeliveryReceipt
from app.protocols.protocol import Protocol


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

TEMPLATE_DIR = "app/notification/templates"


def _hipaa() -> Protocol:
    return Protocol(
        protocol_id="hipaa_breach_rule",
        name="HIPAA Breach Notification",
        jurisdiction="US-FEDERAL",
        triggering_entity_types=["US_SSN", "PHI_MRN"],
        notification_threshold=1,
        notification_deadline_days=60,
        required_notification_content=["desc"],
        regulatory_framework="45 CFR §164.400-414",
    )


def _subject(
    *,
    email: str | None = "alice@example.com",
    name: str = "Alice Smith",
    pii_types: list[str] | None = None,
) -> NotificationSubject:
    ns = NotificationSubject(
        subject_id=uuid4(),
        canonical_name=name,
        canonical_email=email,
        pii_types_found=pii_types or ["US_SSN"],
        notification_required=True,
        review_status="AI_PENDING",
    )
    return ns


def _notification_list(subject_ids: list[str] | None = None) -> NotificationList:
    return NotificationList(
        notification_list_id=uuid4(),
        job_id="job-1",
        protocol_id="hipaa_breach_rule",
        subject_ids=subject_ids or [],
        status="PENDING",
    )


# ===========================================================================
# send_notification
# ===========================================================================

class TestSendNotification:
    @patch("app.notification.email_sender.smtplib.SMTP")
    def test_subject_with_email_returns_sent(self, mock_smtp_cls):
        mock_server = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        sender = EmailSender(smtp_host="localhost", smtp_port=25)
        subj = _subject()
        receipt = sender.send_notification(subj, _hipaa(), TEMPLATE_DIR)

        assert receipt.status == "SENT"
        assert receipt.subject_id == str(subj.subject_id)
        assert receipt.attempt_count == 1
        assert receipt.smtp_response == "250 OK"

    def test_subject_without_email_returns_skipped(self):
        sender = EmailSender(smtp_host="localhost")
        subj = _subject(email=None)
        receipt = sender.send_notification(subj, _hipaa(), TEMPLATE_DIR)

        assert receipt.status == "SKIPPED"
        assert receipt.attempt_count == 0
        assert receipt.email == ""

    @patch("app.notification.email_sender.time.sleep")
    @patch("app.notification.email_sender.smtplib.SMTP")
    def test_retry_succeeds_on_second_attempt(self, mock_smtp_cls, mock_sleep):
        mock_server = MagicMock()
        mock_server.sendmail.side_effect = [
            smtplib.SMTPException("temporary error"),
            None,  # succeeds on 2nd
        ]
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        sender = EmailSender(smtp_host="localhost")
        receipt = sender.send_notification(_subject(), _hipaa(), TEMPLATE_DIR)

        assert receipt.status == "SENT"
        assert receipt.attempt_count == 2
        mock_sleep.assert_called_once_with(1)  # 1s backoff after 1st failure

    @patch("app.notification.email_sender.time.sleep")
    @patch("app.notification.email_sender.smtplib.SMTP")
    def test_three_failures_returns_failed(self, mock_smtp_cls, mock_sleep):
        mock_server = MagicMock()
        mock_server.sendmail.side_effect = smtplib.SMTPException("permanent error")
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        sender = EmailSender(smtp_host="localhost")
        receipt = sender.send_notification(_subject(), _hipaa(), TEMPLATE_DIR)

        assert receipt.status == "FAILED"
        assert receipt.attempt_count == 3
        assert "permanent error" in receipt.smtp_response
        # Backoff: 1s after attempt 1, 2s after attempt 2; no sleep after attempt 3
        assert mock_sleep.call_args_list == [call(1), call(2)]

    @patch("app.notification.email_sender.smtplib.SMTP")
    def test_canonical_email_not_in_logs(self, mock_smtp_cls, caplog):
        mock_server = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        sender = EmailSender(smtp_host="localhost")
        subj = _subject(email="secret.address@example.com")

        with caplog.at_level("DEBUG"):
            sender.send_notification(subj, _hipaa(), TEMPLATE_DIR)

        assert "secret.address@example.com" not in caplog.text

    @patch("app.notification.email_sender.smtplib.SMTP")
    def test_uses_hipaa_template(self, mock_smtp_cls):
        """HIPAA protocol should use hipaa_breach_rule_email.html."""
        mock_server = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        sender = EmailSender(smtp_host="localhost")
        subj = _subject(pii_types=["PHI_MRN", "US_SSN"])
        sender.send_notification(subj, _hipaa(), TEMPLATE_DIR)

        # Verify sendmail was called and body contains HIPAA-specific text
        raw = mock_server.sendmail.call_args[0][2]
        msg = email_mod.message_from_string(raw)
        body = msg.get_payload(0).get_payload(decode=True).decode()
        assert "protected health information" in body.lower()

    @patch("app.notification.email_sender.smtplib.SMTP")
    def test_falls_back_to_default_template(self, mock_smtp_cls):
        """Unknown protocol falls back to default_email.html."""
        mock_server = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        unknown_protocol = Protocol(
            protocol_id="unknown_protocol",
            name="Unknown",
            jurisdiction="TEST",
            triggering_entity_types=["EMAIL"],
            notification_threshold=1,
            notification_deadline_days=30,
            required_notification_content=["desc"],
            regulatory_framework="Test Framework",
        )
        sender = EmailSender(smtp_host="localhost")
        subj = _subject()
        receipt = sender.send_notification(subj, unknown_protocol, TEMPLATE_DIR)

        assert receipt.status == "SENT"
        raw = mock_server.sendmail.call_args[0][2]
        msg = email_mod.message_from_string(raw)
        body = msg.get_payload(0).get_payload(decode=True).decode()
        assert "Important Notice Regarding Your Personal Information" in body


# ===========================================================================
# send_all
# ===========================================================================

class TestSendAll:
    @patch("app.notification.email_sender.time.sleep")
    @patch("app.notification.email_sender.smtplib.SMTP")
    def test_mixed_subjects(self, mock_smtp_cls, mock_sleep):
        mock_server = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        s1 = _subject(email="a@b.com", name="A")
        s2 = _subject(email=None, name="B")
        s3 = _subject(email="c@d.com", name="C")
        nl = _notification_list([str(s1.subject_id), str(s3.subject_id)])

        sender = EmailSender(smtp_host="localhost", rate_limit_per_minute=100)
        receipts = sender.send_all(nl, [s1, s2, s3], _hipaa(), TEMPLATE_DIR)

        statuses = [r.status for r in receipts]
        assert statuses.count("SENT") == 2
        assert statuses.count("SKIPPED") == 1
        assert len(receipts) == 3

    @patch("app.notification.email_sender.time.sleep")
    @patch("app.notification.email_sender.smtplib.SMTP")
    def test_rate_limiting_calls_sleep(self, mock_smtp_cls, mock_sleep):
        mock_server = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        s1 = _subject(email="a@b.com")
        s2 = _subject(email="c@d.com")
        nl = _notification_list()

        sender = EmailSender(smtp_host="localhost", rate_limit_per_minute=100)
        sender.send_all(nl, [s1, s2], _hipaa(), TEMPLATE_DIR)

        # 60/100 = 0.6s between sends; called once (before 2nd send)
        assert mock_sleep.call_count >= 1
        sleep_val = mock_sleep.call_args_list[0][0][0]
        assert abs(sleep_val - 0.6) < 0.01

    @patch("app.notification.email_sender.time.sleep")
    @patch("app.notification.email_sender.smtplib.SMTP")
    def test_notification_list_status_set_to_delivered(self, mock_smtp_cls, mock_sleep):
        mock_server = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        s = _subject(email="a@b.com")
        nl = _notification_list()

        sender = EmailSender(smtp_host="localhost", rate_limit_per_minute=100)
        sender.send_all(nl, [s], _hipaa(), TEMPLATE_DIR)

        assert nl.status == "DELIVERED"
