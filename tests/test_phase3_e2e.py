"""End-to-end smoke test for the full Phase 3 pipeline.

Only SMTP and WeasyPrint are mocked (I/O boundaries).
All business logic runs real.
"""
from __future__ import annotations

import csv
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import NotificationList, NotificationSubject
from app.notification.email_sender import EmailSender
from app.notification.list_builder import build_notification_list, get_notification_subjects
from app.notification.print_renderer import PrintRenderer
from app.protocols.protocol import Protocol


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TEMPLATE_DIR = "app/notification/templates"


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with Session() as session:
        yield session


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


# ---------------------------------------------------------------------------
# E2E scenario
# ---------------------------------------------------------------------------

class TestPhase3EndToEnd:
    """Full pipeline: build list → send emails → render letters → manifest."""

    @patch("app.notification.email_sender.time.sleep")
    @patch("app.notification.email_sender.smtplib.SMTP")
    def test_hipaa_three_subjects(
        self, mock_smtp_cls, mock_sleep, db_session, tmp_path,
    ):
        # -- Arrange: 3 subjects in DB ------------------------------------------
        mock_server = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        subject_a = NotificationSubject(
            subject_id=uuid4(),
            canonical_name="Alice Anderson",
            canonical_email="alice@example.com",
            canonical_address={
                "street": "100 Main St",
                "city": "Springfield",
                "state": "IL",
                "zip": "62701",
                "country": "US",
            },
            pii_types_found=["US_SSN"],
            notification_required=False,
            review_status="AI_PENDING",
        )
        subject_b = NotificationSubject(
            subject_id=uuid4(),
            canonical_name="Bob Baker",
            canonical_email="bob@example.com",
            canonical_address={
                "street": "200 Oak Ave",
                "city": "Chicago",
                "state": "IL",
                "zip": "60601",
                "country": "US",
            },
            pii_types_found=["FERPA_STUDENT_ID"],
            notification_required=False,
            review_status="AI_PENDING",
        )
        subject_c = NotificationSubject(
            subject_id=uuid4(),
            canonical_name="Carol Chen",
            canonical_email=None,
            canonical_address={
                "street": "300 Pine Rd",
                "city": "Peoria",
                "state": "IL",
                "zip": "61602",
                "country": "US",
            },
            pii_types_found=["US_SSN"],
            notification_required=False,
            review_status="AI_PENDING",
        )

        for s in (subject_a, subject_b, subject_c):
            db_session.add(s)
        db_session.flush()

        protocol = _hipaa()

        # -- Step 1: build_notification_list ------------------------------------
        nl = build_notification_list(
            "e2e-job-1", protocol, [subject_a, subject_b, subject_c], db_session,
        )
        db_session.commit()

        # A and C triggered (US_SSN), B not triggered (FERPA_STUDENT_ID)
        assert len(nl.subject_ids) == 2
        triggered_ids = set(nl.subject_ids)
        assert str(subject_a.subject_id) in triggered_ids
        assert str(subject_c.subject_id) in triggered_ids
        assert str(subject_b.subject_id) not in triggered_ids

        # Subject B notification_required == False
        db_session.refresh(subject_b)
        assert subject_b.notification_required is False

        # Subject A and C notification_required == True
        db_session.refresh(subject_a)
        db_session.refresh(subject_c)
        assert subject_a.notification_required is True
        assert subject_c.notification_required is True

        assert nl.status == "PENDING"

        # -- Step 2: get subjects from list ------------------------------------
        notified_subjects = get_notification_subjects(nl, db_session)
        assert len(notified_subjects) == 2

        # -- Step 3: send emails -----------------------------------------------
        sender = EmailSender(
            smtp_host="localhost",
            smtp_port=25,
            rate_limit_per_minute=100,
        )
        receipts = sender.send_all(
            nl, notified_subjects, protocol, TEMPLATE_DIR,
        )

        assert len(receipts) == 2
        statuses = {r.subject_id: r.status for r in receipts}
        assert statuses[str(subject_a.subject_id)] == "SENT"
        assert statuses[str(subject_c.subject_id)] == "SKIPPED"  # no email

        assert nl.status == "DELIVERED"

        # -- Step 4: render letters (mock WeasyPrint) --------------------------
        output_dir = tmp_path / "letters"
        renderer = PrintRenderer(
            output_dir=output_dir,
            template_dir=TEMPLATE_DIR,
        )

        mock_wp = MagicMock()
        mock_wp.HTML.return_value = MagicMock()
        with patch.dict("sys.modules", {"weasyprint": mock_wp}):
            entries = renderer.render_all(nl, notified_subjects, protocol)

        assert len(entries) == 2
        letter_statuses = {e.subject_id: e.status for e in entries}
        assert letter_statuses[str(subject_a.subject_id)] == "RENDERED"
        assert letter_statuses[str(subject_c.subject_id)] == "RENDERED"

        # -- Step 5: write manifest -------------------------------------------
        manifest_path = renderer.write_manifest(entries, "e2e-job-1")
        assert manifest_path.exists()
        assert manifest_path.name == "e2e-job-1_manifest.csv"

        with open(manifest_path, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

        assert len(rows) == 2
        manifest_ids = {r["subject_id"] for r in rows}
        assert str(subject_a.subject_id) in manifest_ids
        assert str(subject_c.subject_id) in manifest_ids

        # Both RENDERED
        assert all(r["status"] == "RENDERED" for r in rows)

        # -- Step 6: review_status unchanged by delivery -----------------------
        db_session.refresh(subject_a)
        assert subject_a.review_status == "AI_PENDING"
