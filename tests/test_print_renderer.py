"""Tests for app/notification/print_renderer.py — Phase 3.

WeasyPrint is mocked — no real PDF rendering needed.
"""
from __future__ import annotations

import csv
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.db.models import NotificationList, NotificationSubject
from app.notification.print_renderer import (
    LetterManifestEntry,
    PrintRenderer,
    _load_template,
    _render_html,
)
from app.protocols.protocol import Protocol


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

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


_NO_ADDRESS = object()  # sentinel for "explicitly no address"


def _subject(
    *,
    name: str = "Alice Smith",
    email: str | None = "alice@example.com",
    address: dict | None | object = _NO_ADDRESS,
    pii_types: list[str] | None = None,
) -> NotificationSubject:
    if address is _NO_ADDRESS:
        resolved_address: dict | None = {
            "street": "123 Main St",
            "city": "Springfield",
            "state": "IL",
            "zip": "62701",
            "country": "US",
        }
    else:
        resolved_address = address  # type: ignore[assignment]
    return NotificationSubject(
        subject_id=uuid4(),
        canonical_name=name,
        canonical_email=email,
        canonical_address=resolved_address,
        pii_types_found=pii_types or ["US_SSN"],
        notification_required=True,
        review_status="AI_PENDING",
    )


def _notification_list() -> NotificationList:
    return NotificationList(
        notification_list_id=uuid4(),
        job_id="job-1",
        protocol_id="hipaa_breach_rule",
        subject_ids=[],
        status="PENDING",
    )


# ===========================================================================
# _load_template
# ===========================================================================

class TestLoadTemplate:
    def test_loads_protocol_specific_template(self, tmp_path):
        (tmp_path / "hipaa_breach_rule_letter.html").write_text("HIPAA letter")
        result = _load_template(tmp_path, "hipaa_breach_rule")
        assert result == "HIPAA letter"

    def test_falls_back_to_default(self, tmp_path):
        (tmp_path / "default_letter.html").write_text("Default letter")
        result = _load_template(tmp_path, "unknown_protocol")
        assert result == "Default letter"

    def test_raises_when_no_template(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="No letter template"):
            _load_template(tmp_path, "missing")


# ===========================================================================
# _render_html
# ===========================================================================

class TestRenderHtml:
    def test_substitutes_placeholders(self):
        tmpl = "Dear ${subject_name}, PII: ${pii_types}, Ref: ${regulatory_framework}"
        subj = _subject(pii_types=["US_SSN", "PHI_MRN"])
        html = _render_html(tmpl, subj, _hipaa())
        assert "Alice Smith" in html
        assert "US_SSN, PHI_MRN" in html
        assert "45 CFR" in html

    def test_missing_name_uses_fallback(self):
        tmpl = "Dear ${subject_name}"
        subj = _subject(name=None)
        html = _render_html(tmpl, subj, _hipaa())
        assert "Affected Individual" in html


# ===========================================================================
# render_letter
# ===========================================================================

class TestRenderLetter:
    @patch("app.notification.print_renderer.weasyprint", create=True)
    def test_renders_pdf_successfully(self, mock_wp, tmp_path):
        template_dir = tmp_path / "templates"
        template_dir.mkdir()
        (template_dir / "default_letter.html").write_text("Dear ${subject_name}")

        output_dir = tmp_path / "output"
        renderer = PrintRenderer(output_dir=output_dir, template_dir=template_dir)

        # Mock the weasyprint import inside render_letter
        with patch.dict("sys.modules", {"weasyprint": mock_wp}):
            mock_html_instance = MagicMock()
            mock_wp.HTML.return_value = mock_html_instance

            subj = _subject()
            entry = renderer.render_letter(subj, _hipaa())

        assert entry.status == "RENDERED"
        assert entry.subject_id == str(subj.subject_id)
        assert entry.letter_filename == f"{subj.subject_id}_letter.pdf"
        assert entry.canonical_name == "Alice Smith"
        assert entry.canonical_address is not None

    def test_skips_when_no_address(self, tmp_path):
        template_dir = tmp_path / "templates"
        template_dir.mkdir()
        (template_dir / "default_letter.html").write_text("Dear ${subject_name}")

        output_dir = tmp_path / "output"
        renderer = PrintRenderer(output_dir=output_dir, template_dir=template_dir)

        subj = _subject(address=None)
        entry = renderer.render_letter(subj, _hipaa())

        assert entry.status == "SKIPPED"
        assert entry.letter_filename == ""
        assert entry.canonical_address is None

    def test_returns_failed_on_exception(self, tmp_path):
        template_dir = tmp_path / "templates"
        template_dir.mkdir()
        (template_dir / "default_letter.html").write_text("Dear ${subject_name}")

        output_dir = tmp_path / "output"
        renderer = PrintRenderer(output_dir=output_dir, template_dir=template_dir)

        subj = _subject()
        # weasyprint not installed — import will fail naturally
        entry = renderer.render_letter(subj, _hipaa())

        assert entry.status == "FAILED"
        assert entry.error is not None
        assert entry.subject_id == str(subj.subject_id)

    def test_subject_id_only_in_logs(self, tmp_path, caplog):
        template_dir = tmp_path / "templates"
        template_dir.mkdir()
        (template_dir / "default_letter.html").write_text("Hi")

        output_dir = tmp_path / "output"
        renderer = PrintRenderer(output_dir=output_dir, template_dir=template_dir)

        subj = _subject(name="Secret Person", address=None)
        with caplog.at_level("DEBUG"):
            renderer.render_letter(subj, _hipaa())

        assert "Secret Person" not in caplog.text
        assert "123 Main St" not in caplog.text

    @patch("app.notification.print_renderer.weasyprint", create=True)
    def test_uses_hipaa_template_when_available(self, mock_wp, tmp_path):
        template_dir = tmp_path / "templates"
        template_dir.mkdir()
        (template_dir / "hipaa_breach_rule_letter.html").write_text(
            "HIPAA: ${subject_name} PHI: ${phi_types}"
        )

        output_dir = tmp_path / "output"
        renderer = PrintRenderer(output_dir=output_dir, template_dir=template_dir)

        with patch.dict("sys.modules", {"weasyprint": mock_wp}):
            mock_wp.HTML.return_value = MagicMock()
            subj = _subject(pii_types=["PHI_MRN"])
            entry = renderer.render_letter(subj, _hipaa())

        assert entry.status == "RENDERED"

    @patch("app.notification.print_renderer.weasyprint", create=True)
    def test_falls_back_to_default_template(self, mock_wp, tmp_path):
        template_dir = tmp_path / "templates"
        template_dir.mkdir()
        (template_dir / "default_letter.html").write_text("Default: ${subject_name}")

        output_dir = tmp_path / "output"
        renderer = PrintRenderer(output_dir=output_dir, template_dir=template_dir)

        unknown = Protocol(
            protocol_id="unknown",
            name="Unknown",
            jurisdiction="TEST",
            triggering_entity_types=["EMAIL"],
            notification_threshold=1,
            notification_deadline_days=30,
            required_notification_content=["desc"],
            regulatory_framework="Test Framework",
        )

        with patch.dict("sys.modules", {"weasyprint": mock_wp}):
            mock_wp.HTML.return_value = MagicMock()
            subj = _subject()
            entry = renderer.render_letter(subj, unknown)

        assert entry.status == "RENDERED"


# ===========================================================================
# render_all
# ===========================================================================

class TestRenderAll:
    def test_renders_all_subjects(self, tmp_path):
        template_dir = tmp_path / "templates"
        template_dir.mkdir()
        (template_dir / "default_letter.html").write_text("Hi ${subject_name}")

        output_dir = tmp_path / "output"
        renderer = PrintRenderer(output_dir=output_dir, template_dir=template_dir)

        s1 = _subject(name="A")
        s2 = _subject(name="B", address=None)
        s3 = _subject(name="C")
        nl = _notification_list()

        entries = renderer.render_all(nl, [s1, s2, s3], _hipaa())

        assert len(entries) == 3
        statuses = [e.status for e in entries]
        # s1, s3 will FAIL (no weasyprint installed), s2 SKIPPED
        assert statuses.count("SKIPPED") == 1
        # s1 and s3 attempt render — FAILED because weasyprint not installed
        assert statuses.count("FAILED") == 2


# ===========================================================================
# write_manifest
# ===========================================================================

class TestWriteManifest:
    def test_writes_csv_with_correct_columns(self, tmp_path):
        renderer = PrintRenderer(output_dir=tmp_path, template_dir=tmp_path)
        entries = [
            LetterManifestEntry(
                subject_id="id-1",
                canonical_name="Alice",
                canonical_address={"street": "1 Main", "city": "NY", "state": "NY", "zip": "10001", "country": "US"},
                letter_filename="id-1_letter.pdf",
                status="RENDERED",
            ),
            LetterManifestEntry(
                subject_id="id-2",
                canonical_name="Bob",
                canonical_address=None,
                letter_filename="",
                status="SKIPPED",
            ),
        ]
        path = renderer.write_manifest(entries, "job-42")

        assert path.name == "job-42_manifest.csv"
        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        assert len(rows) == 2
        assert rows[0]["subject_id"] == "id-1"
        assert rows[0]["street"] == "1 Main"
        assert rows[0]["status"] == "RENDERED"
        assert rows[1]["status"] == "SKIPPED"
        assert rows[1]["street"] == ""

    def test_manifest_columns_match_spec(self, tmp_path):
        renderer = PrintRenderer(output_dir=tmp_path, template_dir=tmp_path)
        entry = LetterManifestEntry(
            subject_id="x",
            canonical_name="X",
            canonical_address=None,
            letter_filename="",
            status="SKIPPED",
        )
        path = renderer.write_manifest([entry], "j")
        with open(path, encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader)
        expected = ["subject_id", "canonical_name", "street", "city", "state", "zip", "country", "letter_filename", "status", "error"]
        assert header == expected

    def test_error_field_written(self, tmp_path):
        renderer = PrintRenderer(output_dir=tmp_path, template_dir=tmp_path)
        entry = LetterManifestEntry(
            subject_id="id-f",
            canonical_name="Fail",
            canonical_address={"street": "x", "city": "y", "state": "z", "zip": "0", "country": "US"},
            letter_filename="id-f_letter.pdf",
            status="FAILED",
            error="weasyprint not found",
        )
        path = renderer.write_manifest([entry], "job-err")
        with open(path, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert rows[0]["error"] == "weasyprint not found"
