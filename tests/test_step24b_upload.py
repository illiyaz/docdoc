"""Tests for Step 24b — file upload endpoint for all formats.

Tests cover:
- SUPPORTED_EXTENSIONS covers all registry formats + archives
- extract_archive: ZIP extraction, nested archives, bad zip handling
- extract_email_attachments: EML attachment extraction
- process_uploaded_file: routing logic (archive vs email vs normal)
- POST /jobs/{job_id}/upload endpoint (source-level verification)
- POST /jobs/upload endpoint (archive + email handling)
"""
from __future__ import annotations

import email
import tempfile
import zipfile
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# SUPPORTED_EXTENSIONS coverage
# ---------------------------------------------------------------------------

from app.api.upload_helpers import (
    ARCHIVE_EXTENSIONS,
    EMAIL_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    extract_archive,
    extract_email_attachments,
    is_supported,
    process_uploaded_file,
    should_skip,
)


class TestSupportedExtensions:
    """SUPPORTED_EXTENSIONS must cover all reader registry formats + archives."""

    # All extensions registered in app/readers/registry.py
    REGISTRY_EXTENSIONS = {
        ".pdf", ".xlsx", ".xls", ".xlsm", ".docx", ".csv", ".tsv",
        ".html", ".htm", ".xml", ".eml", ".msg",
        ".parquet", ".avro",
        ".jpg", ".jpeg", ".png", ".bmp", ".webp", ".gif",
        ".heic", ".heif", ".tif", ".tiff",
    }

    def test_all_registry_formats_supported(self):
        for ext in self.REGISTRY_EXTENSIONS:
            assert ext in SUPPORTED_EXTENSIONS, f"{ext} missing from SUPPORTED_EXTENSIONS"

    def test_archive_extensions_supported(self):
        for ext in ARCHIVE_EXTENSIONS:
            assert ext in SUPPORTED_EXTENSIONS

    def test_image_formats_supported(self):
        for ext in (".jpg", ".jpeg", ".png", ".heic", ".tiff", ".webp"):
            assert ext in SUPPORTED_EXTENSIONS

    def test_tsv_xlsm_supported(self):
        assert ".tsv" in SUPPORTED_EXTENSIONS
        assert ".xlsm" in SUPPORTED_EXTENSIONS

    def test_archive_extensions_set(self):
        assert ".zip" in ARCHIVE_EXTENSIONS
        assert ".7z" in ARCHIVE_EXTENSIONS

    def test_email_extensions_set(self):
        assert ".eml" in EMAIL_EXTENSIONS
        assert ".msg" in EMAIL_EXTENSIONS


class TestIsSupported:
    """Test the is_supported helper with new formats."""

    def test_pdf(self):
        assert is_supported("report.pdf")

    def test_jpg(self):
        assert is_supported("scan.jpg")

    def test_heic(self):
        assert is_supported("photo.HEIC")

    def test_zip(self):
        assert is_supported("data.zip")

    def test_tsv(self):
        assert is_supported("data.tsv")

    def test_xlsm(self):
        assert is_supported("macro.xlsm")

    def test_unsupported(self):
        assert not is_supported("readme.txt")
        assert not is_supported("Makefile")


class TestShouldSkip:
    def test_ds_store(self):
        assert should_skip(".DS_Store")

    def test_hidden_file(self):
        assert should_skip(".hidden_config")

    def test_normal_file(self):
        assert not should_skip("report.pdf")


# ---------------------------------------------------------------------------
# Archive extraction
# ---------------------------------------------------------------------------

class TestExtractArchive:
    """Test extract_archive with real ZIP files."""

    def test_zip_with_supported_files(self, tmp_path):
        """ZIP containing PDFs and CSVs should extract them."""
        zip_path = tmp_path / "data.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("report.pdf", b"%PDF-1.4 fake pdf content")
            zf.writestr("data.csv", b"name,ssn\nJohn,123-45-6789\n")
            zf.writestr("readme.txt", b"This should be skipped")
            zf.writestr(".DS_Store", b"skip this too")

        dest = tmp_path / "output"
        dest.mkdir()
        result = extract_archive(zip_path, dest)

        names = [r["name"] for r in result]
        assert "report.pdf" in names
        assert "data.csv" in names
        assert "readme.txt" not in names
        assert ".DS_Store" not in names
        assert len(result) == 2

    def test_zip_with_images(self, tmp_path):
        """ZIP with image files should extract them."""
        zip_path = tmp_path / "scans.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("page1.jpg", b"\xff\xd8\xff fake jpeg")
            zf.writestr("page2.png", b"\x89PNG fake png")
            zf.writestr("page3.tiff", b"fake tiff")

        dest = tmp_path / "output"
        dest.mkdir()
        result = extract_archive(zip_path, dest)
        assert len(result) == 3
        exts = {r["extension"] for r in result}
        assert exts == {".jpg", ".png", ".tiff"}

    def test_nested_zip(self, tmp_path):
        """ZIP inside a ZIP should be extracted recursively."""
        inner_zip = tmp_path / "inner.zip"
        with zipfile.ZipFile(inner_zip, "w") as zf:
            zf.writestr("nested.pdf", b"%PDF nested content")

        outer_zip = tmp_path / "outer.zip"
        with zipfile.ZipFile(outer_zip, "w") as zf:
            zf.write(inner_zip, "inner.zip")
            zf.writestr("top_level.csv", b"col1,col2\na,b\n")

        dest = tmp_path / "output"
        dest.mkdir()
        result = extract_archive(outer_zip, dest)

        names = [r["name"] for r in result]
        assert "nested.pdf" in names
        assert "top_level.csv" in names
        # inner.zip should have been removed after extraction
        assert not (dest / "inner.zip").exists()

    def test_empty_zip(self, tmp_path):
        zip_path = tmp_path / "empty.zip"
        with zipfile.ZipFile(zip_path, "w"):
            pass

        dest = tmp_path / "output"
        dest.mkdir()
        result = extract_archive(zip_path, dest)
        assert result == []

    def test_bad_zip(self, tmp_path):
        """Corrupt ZIP should return empty list, not crash."""
        bad_zip = tmp_path / "bad.zip"
        bad_zip.write_bytes(b"this is not a zip file")

        dest = tmp_path / "output"
        dest.mkdir()
        result = extract_archive(bad_zip, dest)
        assert result == []

    def test_zip_with_directories(self, tmp_path):
        """Directories inside ZIP should be skipped."""
        zip_path = tmp_path / "dirs.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("subdir/report.pdf", b"%PDF content")

        dest = tmp_path / "output"
        dest.mkdir()
        result = extract_archive(zip_path, dest)
        assert len(result) == 1
        assert result[0]["name"] == "report.pdf"

    def test_zip_preserves_size(self, tmp_path):
        content = b"%PDF-1.4 " + b"x" * 1000
        zip_path = tmp_path / "sized.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("doc.pdf", content)

        dest = tmp_path / "output"
        dest.mkdir()
        result = extract_archive(zip_path, dest)
        assert result[0]["size_bytes"] == len(content)


# ---------------------------------------------------------------------------
# Email attachment extraction
# ---------------------------------------------------------------------------

def _make_eml_with_attachment(
    body_text: str,
    attachment_name: str,
    attachment_content: bytes,
) -> bytes:
    """Build a multipart EML with one text body and one attachment."""
    msg = MIMEMultipart()
    msg["Subject"] = "Test breach notification"
    msg["From"] = "sender@example.com"
    msg["To"] = "recipient@example.com"

    msg.attach(MIMEText(body_text, "plain"))

    att = MIMEApplication(attachment_content, Name=attachment_name)
    att["Content-Disposition"] = f'attachment; filename="{attachment_name}"'
    msg.attach(att)

    return msg.as_bytes()


class TestExtractEmailAttachments:
    """Test extract_email_attachments with real EML files."""

    def test_eml_with_pdf_attachment(self, tmp_path):
        eml_bytes = _make_eml_with_attachment(
            "Dear Sir, see attached breach data.",
            "records.pdf",
            b"%PDF-1.4 fake pdf",
        )
        eml_path = tmp_path / "notification.eml"
        eml_path.write_bytes(eml_bytes)

        result = extract_email_attachments(eml_path, tmp_path)
        assert len(result) == 1
        assert result[0]["name"] == "records.pdf"
        assert result[0]["extension"] == ".pdf"
        assert "source" in result[0]
        assert (tmp_path / "records.pdf").exists()

    def test_eml_with_unsupported_attachment(self, tmp_path):
        eml_bytes = _make_eml_with_attachment(
            "See attached notes.",
            "notes.txt",
            b"Just some text notes",
        )
        eml_path = tmp_path / "email.eml"
        eml_path.write_bytes(eml_bytes)

        result = extract_email_attachments(eml_path, tmp_path)
        assert result == []

    def test_eml_with_multiple_attachments(self, tmp_path):
        msg = MIMEMultipart()
        msg["Subject"] = "Multi-attachment"
        msg.attach(MIMEText("Body text", "plain"))

        for name, content in [
            ("data.xlsx", b"fake xlsx"),
            ("scan.jpg", b"\xff\xd8 fake jpg"),
            ("readme.txt", b"skip me"),
        ]:
            att = MIMEApplication(content, Name=name)
            att["Content-Disposition"] = f'attachment; filename="{name}"'
            msg.attach(att)

        eml_path = tmp_path / "multi.eml"
        eml_path.write_bytes(msg.as_bytes())

        result = extract_email_attachments(eml_path, tmp_path)
        names = [r["name"] for r in result]
        assert "data.xlsx" in names
        assert "scan.jpg" in names
        assert "readme.txt" not in names
        assert len(result) == 2

    def test_eml_no_attachments(self, tmp_path):
        msg = MIMEText("Plain email body with no attachments")
        msg["Subject"] = "No attachments"

        eml_path = tmp_path / "plain.eml"
        eml_path.write_bytes(msg.as_bytes())

        result = extract_email_attachments(eml_path, tmp_path)
        assert result == []

    def test_corrupt_eml(self, tmp_path):
        eml_path = tmp_path / "bad.eml"
        eml_path.write_bytes(b"\x00\x01\x02 not an email")

        result = extract_email_attachments(eml_path, tmp_path)
        assert result == []


# ---------------------------------------------------------------------------
# process_uploaded_file routing
# ---------------------------------------------------------------------------

class TestProcessUploadedFile:
    """Test the unified file processing router."""

    def test_normal_pdf(self, tmp_path):
        content = b"%PDF-1.4 test content"
        result = process_uploaded_file(content, "report.pdf", tmp_path)
        assert len(result) == 1
        assert result[0]["name"] == "report.pdf"
        assert (tmp_path / "report.pdf").exists()

    def test_image_file(self, tmp_path):
        content = b"\xff\xd8\xff fake jpeg"
        result = process_uploaded_file(content, "scan.jpg", tmp_path)
        assert len(result) == 1
        assert result[0]["extension"] == ".jpg"

    def test_archive_extracts_and_removes(self, tmp_path):
        """ZIP should be extracted and the ZIP itself removed."""
        zip_buf = tmp_path / "temp.zip"
        with zipfile.ZipFile(zip_buf, "w") as zf:
            zf.writestr("inside.pdf", b"%PDF content")
            zf.writestr("data.csv", b"a,b\n1,2\n")
        content = zip_buf.read_bytes()

        dest = tmp_path / "upload"
        dest.mkdir()
        result = process_uploaded_file(content, "bundle.zip", dest)

        names = [r["name"] for r in result]
        assert "inside.pdf" in names
        assert "data.csv" in names
        # ZIP itself should be gone
        assert not (dest / "bundle.zip").exists()

    def test_email_keeps_body_and_extracts_attachments(self, tmp_path):
        """EML should be kept (for body reading) AND attachments extracted."""
        eml_bytes = _make_eml_with_attachment(
            "Please review attached records.",
            "records.xlsx",
            b"fake excel content",
        )
        dest = tmp_path / "upload"
        dest.mkdir()
        result = process_uploaded_file(eml_bytes, "breach.eml", dest)

        names = [r["name"] for r in result]
        # Email itself is kept
        assert "breach.eml" in names
        # Attachment is extracted
        assert "records.xlsx" in names
        assert len(result) == 2

    def test_duplicate_filename_gets_suffix(self, tmp_path):
        """Uploading same filename twice should create _1 suffix."""
        content = b"%PDF content"
        process_uploaded_file(content, "report.pdf", tmp_path)
        result = process_uploaded_file(content, "report.pdf", tmp_path)
        assert result[0]["name"] == "report_1.pdf"


# ---------------------------------------------------------------------------
# Endpoint wiring verification (source-level)
# ---------------------------------------------------------------------------

class TestEndpointWiring:
    """Verify the new endpoint exists in jobs.py source."""

    @pytest.fixture(autouse=True)
    def _load_source(self):
        import pathlib
        self.jobs_src = (pathlib.Path(__file__).parent.parent / "app" / "api" / "routes" / "jobs.py").read_text()
        self.helpers_src = (pathlib.Path(__file__).parent.parent / "app" / "api" / "upload_helpers.py").read_text()

    def test_job_upload_endpoint_exists(self):
        assert '/{job_id}/upload' in self.jobs_src

    def test_job_upload_is_post(self):
        assert '@router.post("/{job_id}/upload"' in self.jobs_src

    def test_upload_uses_process_helper(self):
        assert "process_uploaded_file" in self.jobs_src

    def test_upload_checks_job_status(self):
        """Endpoint should guard against uploading to running/completed jobs."""
        assert "run.status" in self.jobs_src

    def test_archive_extensions_constant(self):
        assert "ARCHIVE_EXTENSIONS" in self.helpers_src

    def test_email_extensions_constant(self):
        assert "EMAIL_EXTENSIONS" in self.helpers_src

    def test_extract_archive_function(self):
        assert "def extract_archive(" in self.helpers_src

    def test_extract_email_attachments_function(self):
        assert "def extract_email_attachments(" in self.helpers_src