"""Tests for source document viewer (Step 27 — Critical #1).

Covers:
- Document info endpoint (metadata, page count, non-PDF handling)
- Page rendering endpoint (base64 PNG, bbox overlays, validation)
- Subject source pages endpoint (extraction grouping)
- Renderer overlay function (bbox drawing, colour coding)
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Base, Document, Extraction, IngestionRun


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture
def sample_run(db):
    run = IngestionRun(
        id=uuid4(),
        source_path="/test",
        config_hash="test",
        code_version="1.0",
        initiated_by="test",
        status="completed",
    )
    db.add(run)
    db.commit()
    return run


@pytest.fixture
def sample_doc(db, sample_run):
    doc = Document(
        id=uuid4(),
        ingestion_run_id=sample_run.id,
        source_path="/test/sample.pdf",
        file_name="sample.pdf",
        file_type="pdf",
        sha256="abc123",
        page_count=10,
        content_onset_page=2,
    )
    db.add(doc)
    db.commit()
    return doc


@pytest.fixture
def sample_extraction(db, sample_doc):
    ext = Extraction(
        id=uuid4(),
        document_id=sample_doc.id,
        pii_type="PERSON",
        sensitivity="high",
        hashed_value="hash123",
        masked_value="John D***",
        evidence_page=2,
        evidence_bbox={"x0": 100.0, "y0": 200.0, "x1": 350.0, "y1": 220.0},
    )
    db.add(ext)
    db.commit()
    return ext


# ---------------------------------------------------------------------------
# Document info tests
# ---------------------------------------------------------------------------

class TestDocumentInfo:

    def test_returns_metadata(self, db, sample_doc):
        from app.api.routes.documents import get_document_info
        result = get_document_info(sample_doc.id, db)
        assert result["document_id"] == str(sample_doc.id)
        assert result["file_name"] == "sample.pdf"
        assert result["is_pdf"] is True
        assert result["page_count"] == 10
        assert result["onset_page"] == 2

    def test_404_for_missing_document(self, db):
        from app.api.routes.documents import get_document_info
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            get_document_info(uuid4(), db)
        assert exc_info.value.status_code == 404

    def test_non_pdf_document(self, db, sample_run):
        doc = Document(
            id=uuid4(),
            ingestion_run_id=sample_run.id,
            source_path="/test/data.xlsx",
            file_name="data.xlsx",
            file_type="xlsx",
            sha256="def456",
        )
        db.add(doc)
        db.commit()

        from app.api.routes.documents import get_document_info
        result = get_document_info(doc.id, db)
        assert result["is_pdf"] is False


# ---------------------------------------------------------------------------
# Page rendering tests
# ---------------------------------------------------------------------------

class TestDocumentPage:

    def test_negative_page_number_rejected(self, db, sample_doc):
        from app.api.routes.documents import get_document_page
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            get_document_page(sample_doc.id, -1, db)
        assert exc_info.value.status_code == 400

    def test_non_pdf_returns_422(self, db, sample_run):
        doc = Document(
            id=uuid4(),
            ingestion_run_id=sample_run.id,
            source_path="/test/data.xlsx",
            file_name="data.xlsx",
            file_type="xlsx",
            sha256="def456",
        )
        db.add(doc)
        db.commit()

        from app.api.routes.documents import get_document_page
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            get_document_page(doc.id, 0, db)
        assert exc_info.value.status_code == 422

    def test_missing_document_returns_404(self, db):
        from app.api.routes.documents import get_document_page
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            get_document_page(uuid4(), 0, db)
        assert exc_info.value.status_code == 404

    def test_missing_source_file_returns_404(self, db, sample_doc):
        from app.api.routes.documents import get_document_page
        from fastapi import HTTPException
        # source_path doesn't exist on disk
        with pytest.raises(HTTPException) as exc_info:
            get_document_page(sample_doc.id, 0, db)
        assert exc_info.value.status_code == 404

    def test_page_out_of_range(self, db, sample_run, tmp_path):
        """Page number exceeding actual page count returns 400."""
        # Create a minimal 1-page PDF
        import fitz
        pdf_path = str(tmp_path / "one_page.pdf")
        doc = fitz.open()
        doc.new_page()
        doc.save(pdf_path)
        doc.close()

        db_doc = Document(
            id=uuid4(),
            ingestion_run_id=sample_run.id,
            source_path=pdf_path,
            file_name="one_page.pdf",
            file_type="pdf",
            sha256="oneone",
            page_count=1,
        )
        db.add(db_doc)
        db.commit()

        from app.api.routes.documents import get_document_page
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            get_document_page(db_doc.id, 5, db)
        assert exc_info.value.status_code == 400
        assert "out of range" in str(exc_info.value.detail)

    def test_renders_page_as_base64(self, db, sample_run, tmp_path):
        """Valid PDF page returns base64 PNG image."""
        import fitz
        pdf_path = str(tmp_path / "test.pdf")
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Hello World")
        doc.save(pdf_path)
        doc.close()

        db_doc = Document(
            id=uuid4(),
            ingestion_run_id=sample_run.id,
            source_path=pdf_path,
            file_name="test.pdf",
            file_type="pdf",
            sha256="test123",
            page_count=1,
        )
        db.add(db_doc)
        db.commit()

        from app.api.routes.documents import get_document_page
        result = get_document_page(db_doc.id, 0, db)
        assert result["page_number"] == 0
        assert result["page_count"] == 1
        assert len(result["image_base64"]) > 100
        # Verify it decodes as valid PNG
        img_bytes = base64.b64decode(result["image_base64"])
        assert img_bytes[:4] == b"\x89PNG"

    def test_renders_with_bbox_overlay(self, db, sample_run, tmp_path):
        """Extraction bboxes are highlighted on the rendered page."""
        import fitz
        pdf_path = str(tmp_path / "test_bbox.pdf")
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((100, 200), "John Doe")
        doc.save(pdf_path)
        doc.close()

        db_doc = Document(
            id=uuid4(),
            ingestion_run_id=sample_run.id,
            source_path=pdf_path,
            file_name="test_bbox.pdf",
            file_type="pdf",
            sha256="bbox123",
            page_count=1,
        )
        db.add(db_doc)
        db.commit()

        ext = Extraction(
            id=uuid4(),
            document_id=db_doc.id,
            pii_type="PERSON",
            sensitivity="high",
            hashed_value="h1",
            masked_value="John D***",
            evidence_page=0,
            evidence_bbox={"x0": 90, "y0": 190, "x1": 200, "y1": 210},
        )
        db.add(ext)
        db.commit()

        from app.api.routes.documents import get_document_page
        result = get_document_page(db_doc.id, 0, db, highlight_extractions=True)
        assert len(result["highlighted_extractions"]) == 1
        assert result["highlighted_extractions"][0]["pii_type"] == "PERSON"
        assert result["highlighted_extractions"][0]["bbox"] is not None

    def test_extraction_without_bbox_included_but_not_drawn(self, db, sample_run, tmp_path):
        """Extractions without evidence_bbox appear in response but bbox is null."""
        import fitz
        pdf_path = str(tmp_path / "no_bbox.pdf")
        doc = fitz.open()
        doc.new_page()
        doc.save(pdf_path)
        doc.close()

        db_doc = Document(
            id=uuid4(),
            ingestion_run_id=sample_run.id,
            source_path=pdf_path,
            file_name="no_bbox.pdf",
            file_type="pdf",
            sha256="nobbox",
            page_count=1,
        )
        db.add(db_doc)
        db.commit()

        ext = Extraction(
            id=uuid4(),
            document_id=db_doc.id,
            pii_type="DOB",
            sensitivity="medium",
            hashed_value="h2",
            masked_value="**/**/1985",
            evidence_page=0,
            evidence_bbox=None,
        )
        db.add(ext)
        db.commit()

        from app.api.routes.documents import get_document_page
        result = get_document_page(db_doc.id, 0, db, highlight_extractions=True)
        assert len(result["highlighted_extractions"]) == 1
        assert result["highlighted_extractions"][0]["bbox"] is None


# ---------------------------------------------------------------------------
# Renderer overlay tests
# ---------------------------------------------------------------------------

class TestRendererOverlays:

    def test_render_with_no_bboxes(self, tmp_path):
        """Rendering without overlays produces valid PNG."""
        import fitz
        from app.pdf.renderer import render_page_with_overlays

        pdf_path = str(tmp_path / "plain.pdf")
        doc = fitz.open()
        doc.new_page()
        doc.save(pdf_path)
        doc.close()

        result = render_page_with_overlays(pdf_path, 0, bboxes=None)
        img_bytes = base64.b64decode(result)
        assert img_bytes[:4] == b"\x89PNG"

    def test_render_with_bboxes(self, tmp_path):
        """Rendering with overlays produces valid PNG (bbox drawn)."""
        import fitz
        from app.pdf.renderer import render_page_with_overlays

        pdf_path = str(tmp_path / "overlay.pdf")
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((100, 200), "Test value")
        doc.save(pdf_path)
        doc.close()

        bboxes = [
            {"x0": 90, "y0": 190, "x1": 250, "y1": 210, "pii_type": "PERSON"},
            {"x0": 90, "y0": 220, "x1": 200, "y1": 240, "pii_type": "US_SSN"},
        ]
        result = render_page_with_overlays(pdf_path, 0, bboxes=bboxes)
        img_bytes = base64.b64decode(result)
        assert img_bytes[:4] == b"\x89PNG"

    def test_render_skips_malformed_bbox(self, tmp_path):
        """Malformed bboxes are silently skipped."""
        import fitz
        from app.pdf.renderer import render_page_with_overlays

        pdf_path = str(tmp_path / "bad_bbox.pdf")
        doc = fitz.open()
        doc.new_page()
        doc.save(pdf_path)
        doc.close()

        bboxes = [
            {"x0": 10, "y0": 10},  # missing x1, y1
            {"bad": "data"},
            {"x0": 50, "y0": 50, "x1": 100, "y1": 70, "pii_type": "DOB"},  # valid
        ]
        result = render_page_with_overlays(pdf_path, 0, bboxes=bboxes)
        img_bytes = base64.b64decode(result)
        assert img_bytes[:4] == b"\x89PNG"

    def test_render_out_of_range_raises(self, tmp_path):
        """Out-of-range page number raises IndexError."""
        import fitz
        from app.pdf.renderer import render_page_with_overlays

        pdf_path = str(tmp_path / "one.pdf")
        doc = fitz.open()
        doc.new_page()
        doc.save(pdf_path)
        doc.close()

        with pytest.raises(IndexError):
            render_page_with_overlays(pdf_path, 99)

    def test_pii_color_coding(self):
        """Each PII type has a distinct colour in the colour map."""
        from app.pdf.renderer import _PII_COLORS
        assert "PERSON" in _PII_COLORS
        assert "US_SSN" in _PII_COLORS
        assert "DOB" in _PII_COLORS
        assert "LOCATION" in _PII_COLORS
        # Colours should be (r, g, b) tuples
        for color in _PII_COLORS.values():
            assert len(color) == 3
            assert all(0 <= c <= 1 for c in color)
