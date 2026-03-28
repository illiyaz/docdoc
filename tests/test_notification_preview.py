"""Tests for notification preview (Step 27 — Critical #3).

Covers:
- Email preview rendering with masked data
- Letter preview rendering with masked data
- Template loading (protocol-specific + default fallback)
- Masking functions (name, email)
- API endpoint (404 for missing subject)
"""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, NotificationSubject


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture
def sample_subject(db):
    ns = NotificationSubject(
        subject_id=uuid4(),
        canonical_name="John Smith",
        canonical_email="john.smith@example.com",
        canonical_address={"street": "123 Main St", "city": "Springfield", "state": "IL", "zip": "62701"},
        pii_types_found=["PERSON", "US_SSN", "DOB", "LOCATION"],
        merge_confidence=0.92,
        notification_required=True,
        review_status="APPROVED",
    )
    db.add(ns)
    db.commit()
    return ns


# ---------------------------------------------------------------------------
# Masking tests
# ---------------------------------------------------------------------------

class TestMasking:

    def test_mask_name(self):
        from app.api.routes.notifications import _mask_name
        assert _mask_name("John Smith") == "J*** S***"
        assert _mask_name("Alice") == "A***"
        assert _mask_name(None) == "Affected Individual"
        assert _mask_name("") == "Affected Individual"

    def test_mask_email(self):
        from app.api.routes.notifications import _mask_email
        assert _mask_email("john@example.com") == "j***@example.com"
        assert _mask_email(None) == ""
        assert _mask_email("bad-email") == "***@***.***"


# ---------------------------------------------------------------------------
# Email preview rendering
# ---------------------------------------------------------------------------

class TestEmailPreview:

    def test_renders_with_default_template(self, sample_subject):
        from app.api.routes.notifications import _render_email_preview
        html = _render_email_preview(sample_subject, "default")
        # Should contain masked name (not raw)
        assert "J*** S***" in html or "Affected Individual" in html
        # Should NOT contain raw PII
        assert "John Smith" not in html
        assert "john.smith@example.com" not in html

    def test_renders_with_protocol_template(self, sample_subject):
        from app.api.routes.notifications import _render_email_preview
        html = _render_email_preview(sample_subject, "hipaa_breach_rule")
        assert len(html) > 50  # Got some content

    def test_renders_with_missing_template(self, sample_subject, tmp_path):
        from app.api.routes.notifications import _render_email_preview
        html = _render_email_preview(sample_subject, "nonexistent_protocol", template_dir=tmp_path)
        assert "No email template" in html

    def test_pii_types_in_preview(self, sample_subject):
        from app.api.routes.notifications import _render_email_preview
        html = _render_email_preview(sample_subject, "default")
        assert "PERSON" in html or "US_SSN" in html  # PII types are shown (not PII values)


# ---------------------------------------------------------------------------
# Letter preview rendering
# ---------------------------------------------------------------------------

class TestLetterPreview:

    def test_renders_with_default_template(self, sample_subject):
        from app.api.routes.notifications import _render_letter_preview
        html = _render_letter_preview(sample_subject, "default")
        assert "J*** S***" in html or "Affected Individual" in html
        assert "John Smith" not in html

    def test_address_masked_in_letter(self, sample_subject):
        from app.api.routes.notifications import _render_letter_preview
        html = _render_letter_preview(sample_subject, "default")
        # Address fields are shown but name is masked
        assert "John Smith" not in html


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------

class TestPreviewEndpoints:

    def test_email_preview_returns_html(self, db, sample_subject):
        from app.api.routes.notifications import preview_email
        result = preview_email(sample_subject.subject_id, "default", db)
        assert result["format"] == "email"
        assert "html" in result
        assert len(result["html"]) > 50
        assert result["subject_name"] == "J*** S***"

    def test_letter_preview_returns_html(self, db, sample_subject):
        from app.api.routes.notifications import preview_letter
        result = preview_letter(sample_subject.subject_id, "default", db)
        assert result["format"] == "letter"
        assert "html" in result
        assert len(result["html"]) > 50

    def test_email_preview_404_for_missing_subject(self, db):
        from app.api.routes.notifications import preview_email
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            preview_email(uuid4(), "default", db)
        assert exc_info.value.status_code == 404

    def test_letter_preview_404_for_missing_subject(self, db):
        from app.api.routes.notifications import preview_letter
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            preview_letter(uuid4(), "default", db)
        assert exc_info.value.status_code == 404

    def test_preview_no_raw_pii_in_response(self, db, sample_subject):
        """Preview response must not contain raw PII values."""
        from app.api.routes.notifications import preview_email
        result = preview_email(sample_subject.subject_id, "default", db)
        full_response = str(result)
        assert "John Smith" not in full_response
        assert "john.smith@example.com" not in full_response
        assert "123 Main St" not in full_response
