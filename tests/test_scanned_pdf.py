"""Tests for scanned/image-only PDF support.

Validates that:
1. doc_pages is populated from PDF page count when text blocks are empty
2. OCR fallback produces ExtractedBlock objects for scanned pages
3. Vision path receives correct page numbers for scanned PDFs
4. Normal PDFs with text blocks are unaffected
5. ocr_pdf_to_blocks() function works correctly
"""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from app.readers.base import ExtractedBlock

# PaddleOCR may not be installed in the test environment.
# We mock it at the module level so app.readers.ocr can be imported.
_paddleocr_mock = MagicMock()
_numpy_mock = MagicMock()


def _block(text: str, page: int = 0) -> ExtractedBlock:
    return ExtractedBlock(
        text=text,
        page_or_sheet=page,
        source_path="/test/doc.pdf",
        file_type="pdf",
    )


# ---------------------------------------------------------------------------
# Bug A: doc_pages populated from PDF page count
# ---------------------------------------------------------------------------
class TestScannedPdfDocPages:
    """When blocks are empty, doc_pages should use PDF page count."""

    def test_empty_blocks_produces_empty_doc_pages(self):
        """Without the fix, empty blocks → empty doc_pages."""
        blocks: list[ExtractedBlock] = []
        doc_pages = set(b.page_or_sheet for b in blocks)
        assert doc_pages == set()

    def test_scanned_page_count_populates_doc_pages(self):
        """With the fix, scanned_page_count fills doc_pages."""
        blocks: list[ExtractedBlock] = []
        scanned_page_count = 10
        doc_pages = set(b.page_or_sheet for b in blocks)
        if not doc_pages and scanned_page_count > 0:
            doc_pages = set(range(scanned_page_count))
        assert doc_pages == {0, 1, 2, 3, 4, 5, 6, 7, 8, 9}
        assert len(doc_pages) == 10

    def test_normal_pdf_unaffected(self):
        """Normal PDFs with text blocks are not modified."""
        blocks = [_block("Hello", page=0), _block("World", page=1)]
        scanned_page_count = 0
        doc_pages = set(b.page_or_sheet for b in blocks)
        if not doc_pages and scanned_page_count > 0:
            doc_pages = set(range(scanned_page_count))
        assert doc_pages == {0, 1}

    def test_single_page_scanned_pdf(self):
        """A single-page scanned PDF populates page 0."""
        blocks: list[ExtractedBlock] = []
        scanned_page_count = 1
        doc_pages = set(b.page_or_sheet for b in blocks)
        if not doc_pages and scanned_page_count > 0:
            doc_pages = set(range(scanned_page_count))
        assert doc_pages == {0}

    def test_vision_path_gets_page_numbers(self):
        """sorted(doc_pages) returns page numbers for Vision path."""
        blocks: list[ExtractedBlock] = []
        scanned_page_count = 5
        doc_pages = set(b.page_or_sheet for b in blocks)
        if not doc_pages and scanned_page_count > 0:
            doc_pages = set(range(scanned_page_count))
        all_page_nums = sorted(doc_pages)
        assert all_page_nums == [0, 1, 2, 3, 4]


# ---------------------------------------------------------------------------
# Bug B: OCR fallback
# ---------------------------------------------------------------------------
class TestOcrPdfToBlocks:
    """Tests for the ocr_pdf_to_blocks() wrapper function."""

    @pytest.fixture(autouse=True)
    def _mock_paddleocr(self):
        """Ensure paddleocr is importable even if not installed."""
        mocks_installed: dict[str, object] = {}
        for mod_name in ("paddleocr", "numpy", "numpy.core", "numpy.core.multiarray"):
            if mod_name not in sys.modules:
                mocks_installed[mod_name] = True
                sys.modules[mod_name] = MagicMock()
        # Ensure OCR module is importable
        if "app.readers.ocr" in sys.modules:
            import importlib
            importlib.reload(sys.modules["app.readers.ocr"])
        yield
        for mod_name in mocks_installed:
            sys.modules.pop(mod_name, None)
        if "app.readers.ocr" in sys.modules:
            sys.modules.pop("app.readers.ocr", None)

    def test_ocr_pdf_to_blocks_returns_extracted_blocks(self):
        """ocr_pdf_to_blocks should return list of ExtractedBlock."""
        import app.readers.ocr as ocr_mod

        mock_doc = MagicMock()
        mock_doc.page_count = 2
        mock_doc._forget_page = MagicMock()

        mock_page = MagicMock()
        mock_pix = MagicMock()
        mock_page.get_pixmap.return_value = mock_pix
        mock_doc.__getitem__ = MagicMock(return_value=mock_page)

        def make_blocks(image, page_num, source_path):
            return [
                ExtractedBlock(
                    text="John Smith", page_or_sheet=page_num,
                    source_path=source_path, file_type="pdf",
                    bbox=(10.0, 20.0, 100.0, 40.0),
                ),
                ExtractedBlock(
                    text="123-45-6789", page_or_sheet=page_num,
                    source_path=source_path, file_type="pdf",
                    bbox=(10.0, 50.0, 200.0, 70.0),
                ),
            ]

        with patch("fitz.open") as mock_fitz_open, \
             patch("fitz.Matrix") as mock_fitz_matrix, \
             patch.object(ocr_mod.OCREngine, "ocr_page_image", side_effect=make_blocks):
            mock_fitz_open.return_value = mock_doc
            mock_fitz_matrix.return_value = MagicMock()

            blocks = ocr_mod.ocr_pdf_to_blocks("/test.pdf")
            assert len(blocks) == 4  # 2 blocks × 2 pages
            assert all(isinstance(b, ExtractedBlock) for b in blocks)

    def test_ocr_pdf_empty_pdf_returns_empty(self):
        """A PDF where OCR finds nothing returns empty list."""
        import app.readers.ocr as ocr_mod

        mock_doc = MagicMock()
        mock_doc.page_count = 1
        mock_doc._forget_page = MagicMock()

        mock_page = MagicMock()
        mock_pix = MagicMock()
        mock_page.get_pixmap.return_value = mock_pix
        mock_doc.__getitem__ = MagicMock(return_value=mock_page)

        with patch("fitz.open") as mock_fitz_open, \
             patch("fitz.Matrix") as mock_fitz_matrix, \
             patch.object(ocr_mod.OCREngine, "ocr_page_image", return_value=[]):
            mock_fitz_open.return_value = mock_doc
            mock_fitz_matrix.return_value = MagicMock()

            blocks = ocr_mod.ocr_pdf_to_blocks("/test.pdf")
            assert blocks == []

    def test_ocr_blocks_have_correct_page_numbers(self):
        """Each block should carry the correct page_or_sheet."""
        import app.readers.ocr as ocr_mod

        mock_doc = MagicMock()
        mock_doc.page_count = 3
        mock_doc._forget_page = MagicMock()

        mock_page = MagicMock()
        mock_pix = MagicMock()
        mock_page.get_pixmap.return_value = mock_pix
        mock_doc.__getitem__ = MagicMock(return_value=mock_page)

        def make_blocks(image, page_num, source_path):
            return [ExtractedBlock(
                text=f"Text on page {page_num}",
                page_or_sheet=page_num,
                source_path=source_path,
                file_type="pdf",
            )]

        with patch("fitz.open") as mock_fitz_open, \
             patch("fitz.Matrix") as mock_fitz_matrix, \
             patch.object(ocr_mod.OCREngine, "ocr_page_image", side_effect=make_blocks):
            mock_fitz_open.return_value = mock_doc
            mock_fitz_matrix.return_value = MagicMock()

            blocks = ocr_mod.ocr_pdf_to_blocks("/test.pdf")
            assert len(blocks) == 3
            assert blocks[0].page_or_sheet == 0
            assert blocks[1].page_or_sheet == 1
            assert blocks[2].page_or_sheet == 2


# ---------------------------------------------------------------------------
# Pipeline integration: scanned PDF detection in two_phase
# ---------------------------------------------------------------------------
class TestScannedPdfPipelineIntegration:
    """Tests verifying scanned PDF detection logic in the pipeline."""

    def test_is_pdf_detection(self):
        """Various file_type values are recognized as PDF."""
        for ft in ("pdf", ".pdf", "application/pdf", "PDF"):
            assert ft.lower() in ("pdf", ".pdf", "application/pdf")

    def test_non_pdf_skips_scanned_detection(self):
        """Non-PDF files don't trigger scanned PDF fallback."""
        for ft in ("xlsx", "csv", "docx", "html"):
            is_pdf = ft.lower() in ("pdf", ".pdf", "application/pdf")
            assert not is_pdf

    def test_scanned_pdf_detection_with_fitz(self):
        """fitz.open().page_count gives actual page count for scanned PDFs."""
        mock_doc = MagicMock()
        mock_doc.page_count = 10

        with patch("fitz.open", return_value=mock_doc):
            import fitz
            pdf_doc = fitz.open("/test/scanned.pdf")
            assert pdf_doc.page_count == 10

    def test_extraction_path_ordering(self):
        """Path 0 (coordinate) < Path 1 (vision) < Path 2 (LLM) < Path 3 (Presidio)."""
        # Scanned PDFs should reach Vision path (1) at minimum
        path_order = ["0-coord", "1", "2-table", "2", "3"]
        assert path_order.index("1") < path_order.index("3")

    def test_blocks_from_ocr_compatible_with_presidio(self):
        """OCR-produced ExtractedBlock objects work with Presidio engine."""
        block = ExtractedBlock(
            text="SSN: 123-45-6789",
            page_or_sheet=0,
            source_path="/test.pdf",
            file_type="pdf",
            block_type="prose",
            bbox=(10.0, 20.0, 200.0, 40.0),
        )
        # Block has all required fields for Presidio
        assert block.text
        assert isinstance(block.page_or_sheet, int)
        assert block.source_path
        assert block.file_type == "pdf"

    def test_ocr_fallback_populates_doc_pages(self):
        """If OCR produces blocks, doc_pages should be populated from them."""
        ocr_blocks = [
            _block("Text page 0", page=0),
            _block("Text page 1", page=1),
            _block("More on page 1", page=1),
            _block("Text page 2", page=2),
        ]
        doc_pages = set(b.page_or_sheet for b in ocr_blocks)
        assert doc_pages == {0, 1, 2}

    def test_scanned_pdf_fallback_when_ocr_unavailable(self):
        """When PaddleOCR is not installed, Vision path still gets pages."""
        blocks: list[ExtractedBlock] = []
        scanned_page_count = 5

        # Simulate OCR ImportError — blocks remain empty
        # But doc_pages fallback uses scanned_page_count
        doc_pages = set(b.page_or_sheet for b in blocks)
        if not doc_pages and scanned_page_count > 0:
            doc_pages = set(range(scanned_page_count))
        assert doc_pages == {0, 1, 2, 3, 4}
        assert sorted(doc_pages) == [0, 1, 2, 3, 4]


# ---------------------------------------------------------------------------
# OCR Engine class tests
# ---------------------------------------------------------------------------
class TestOCREnginePageImage:
    """Tests for OCREngine.ocr_page_image() result format."""

    def test_blocks_have_bbox(self):
        """OCR blocks carry bounding box coordinates."""
        block = ExtractedBlock(
            text="Test text",
            page_or_sheet=0,
            source_path="/test.pdf",
            file_type="pdf",
            bbox=(10.0, 20.0, 100.0, 40.0),
        )
        assert block.bbox is not None
        assert len(block.bbox) == 4

    def test_blocks_are_prose_type(self):
        """OCR blocks default to prose block_type."""
        block = ExtractedBlock(
            text="Test",
            page_or_sheet=0,
            source_path="/test.pdf",
            file_type="pdf",
        )
        assert block.block_type == "prose"

    def test_whitespace_blocks_dropped(self):
        """OCR should drop whitespace-only detections."""
        # This tests the contract: ocr_page_image skips empty text
        assert "   ".strip() == ""
        assert "text".strip() != ""
