"""Tests for tiered page sampling: compute_sample_pages, get_pdf_page_count, PDFReader.read_pages.

Tests across classes:
- TestComputeSamplePages (14 tests)
- TestGetPdfPageCount (3 tests)
- TestPDFReaderReadPages (6 tests)
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.pipeline.content_onset import compute_sample_pages


# ---------------------------------------------------------------------------
# TestComputeSamplePages
# ---------------------------------------------------------------------------
class TestComputeSamplePages:
    """Tests for compute_sample_pages()."""

    def test_empty_returns_empty(self):
        assert compute_sample_pages(0) == []

    def test_negative_returns_empty(self):
        assert compute_sample_pages(-5) == []

    def test_single_page(self):
        assert compute_sample_pages(1) == [0]

    def test_ten_pages_returns_all(self):
        """Documents with ≤10 pages should return all pages."""
        assert compute_sample_pages(10) == list(range(10))

    def test_eleven_pages_samples(self):
        """11 pages → first 5 + last 2 + 3 random middle = ~10."""
        result = compute_sample_pages(11)
        # First 5 pages must be included
        assert all(p in result for p in range(5))
        # Last 2 pages must be included
        assert 9 in result
        assert 10 in result
        # Total should be ≤ 5 + 2 + 3 = 10
        assert len(result) <= 10

    def test_fifty_pages(self):
        """50 pages → first 5 + last 2 + 3 random middle = ~10."""
        result = compute_sample_pages(50)
        assert all(p in result for p in range(5))
        assert all(p in result for p in range(48, 50))
        assert len(result) <= 10

    def test_hundred_pages(self):
        """100 pages (51-200 tier) → first 5 + last 2 + 8 random middle = ~15."""
        result = compute_sample_pages(100)
        assert all(p in result for p in range(5))
        assert all(p in result for p in range(98, 100))
        assert len(result) <= 15

    def test_two_hundred_pages(self):
        """200 pages (51-200 tier) → ~15."""
        result = compute_sample_pages(200)
        assert all(p in result for p in range(5))
        assert all(p in result for p in range(198, 200))
        assert len(result) <= 15

    def test_three_hundred_pages(self):
        """300 pages (201-500 tier) → first 7 + last 3 + 10 random middle = ~20."""
        result = compute_sample_pages(300)
        assert all(p in result for p in range(7))
        assert all(p in result for p in range(297, 300))
        assert len(result) <= 20

    def test_five_hundred_pages(self):
        """500 pages (201-500 tier) → ~20."""
        result = compute_sample_pages(500)
        assert all(p in result for p in range(7))
        assert all(p in result for p in range(497, 500))
        assert len(result) <= 20

    def test_four_thousand_two_hundred_pages(self):
        """4200 pages (500+ tier) → first 8 + last 3 + 14 random middle = ~25."""
        result = compute_sample_pages(4200)
        assert all(p in result for p in range(8))
        assert all(p in result for p in range(4197, 4200))
        assert len(result) <= 25

    def test_first_and_last_always_included(self):
        """Page 0 and the last page must always be in the sample."""
        for total in [15, 100, 500, 1000, 5000]:
            result = compute_sample_pages(total)
            assert 0 in result, f"Page 0 missing for total={total}"
            assert total - 1 in result, f"Last page missing for total={total}"

    def test_sorted_output(self):
        """Output must be sorted ascending."""
        for total in [20, 100, 500, 2000]:
            result = compute_sample_pages(total)
            assert result == sorted(result), f"Not sorted for total={total}"

    def test_no_duplicates(self):
        """Output must have no duplicate pages."""
        for total in [11, 50, 200, 500, 4200]:
            result = compute_sample_pages(total)
            assert len(result) == len(set(result)), f"Duplicates for total={total}"

    def test_deterministic(self):
        """Same total_pages must produce same sample (deterministic seed)."""
        a = compute_sample_pages(4200)
        b = compute_sample_pages(4200)
        assert a == b

    def test_valid_range(self):
        """All sampled pages must be in [0, total_pages)."""
        for total in [11, 50, 200, 500, 4200]:
            result = compute_sample_pages(total)
            assert all(0 <= p < total for p in result), f"Out of range for total={total}"


# ---------------------------------------------------------------------------
# TestGetPdfPageCount
# ---------------------------------------------------------------------------
class TestGetPdfPageCount:
    """Tests for get_pdf_page_count()."""

    def test_correct_count(self):
        """Should return page count from PyMuPDF without reading content."""
        from app.readers.pdf_reader import get_pdf_page_count

        mock_doc = MagicMock()
        mock_doc.page_count = 42
        with patch("app.readers.pdf_reader.fitz") as mock_fitz:
            mock_fitz.open.return_value = mock_doc
            result = get_pdf_page_count("/fake/path.pdf")
        assert result == 42
        mock_doc.close.assert_called_once()

    def test_error_returns_zero(self):
        """Should return 0 on error (missing file, corrupt PDF, etc.)."""
        from app.readers.pdf_reader import get_pdf_page_count

        with patch("app.readers.pdf_reader.fitz") as mock_fitz:
            mock_fitz.open.side_effect = RuntimeError("corrupt")
            result = get_pdf_page_count("/fake/corrupt.pdf")
        assert result == 0

    def test_no_content_read(self):
        """Should not call load_page or get_text — only page_count."""
        from app.readers.pdf_reader import get_pdf_page_count

        mock_doc = MagicMock()
        mock_doc.page_count = 10
        with patch("app.readers.pdf_reader.fitz") as mock_fitz:
            mock_fitz.open.return_value = mock_doc
            get_pdf_page_count("/fake/path.pdf")
        mock_doc.load_page.assert_not_called()


# ---------------------------------------------------------------------------
# TestPDFReaderReadPages
# ---------------------------------------------------------------------------
class TestPDFReaderReadPages:
    """Tests for PDFReader.read_pages()."""

    def test_empty_list_returns_empty(self):
        """Passing empty page list should return no blocks."""
        from app.readers.pdf_reader import PDFReader

        reader = PDFReader("/fake/path.pdf")
        with patch("app.readers.pdf_reader.fitz") as mock_fitz, \
             patch("app.readers.pdf_reader.pdfplumber"):
            mock_doc = MagicMock()
            mock_doc.__len__ = lambda self: 10
            mock_fitz.open.return_value = mock_doc
            result = reader.read_pages([])
        assert result == []

    def test_only_requested_pages_processed(self):
        """Only the specified pages should be loaded and processed."""
        from app.readers.pdf_reader import PDFReader

        reader = PDFReader("/fake/path.pdf")
        processed_pages = []

        original_process = reader._process_page

        def mock_process(page, page_num, plumber_doc, stitcher, ocr_engine):
            processed_pages.append(page_num)
            from app.readers.base import ExtractedBlock
            block = ExtractedBlock(
                text=f"Page {page_num}", page_or_sheet=page_num,
                source_path="/fake/path.pdf", file_type="pdf",
            )
            return [block], ocr_engine

        mock_doc = MagicMock()
        mock_doc.__len__ = lambda self: 100

        with patch("app.readers.pdf_reader.fitz") as mock_fitz, \
             patch("app.readers.pdf_reader.pdfplumber") as mock_plumber, \
             patch.object(reader, "_process_page", side_effect=mock_process):
            mock_fitz.open.return_value = mock_doc
            mock_plumber_doc = MagicMock()
            mock_plumber.open.return_value.__enter__ = lambda s: mock_plumber_doc
            mock_plumber.open.return_value.__exit__ = lambda s, *a: None
            result = reader.read_pages([0, 5, 99])

        assert processed_pages == [0, 5, 99]
        assert len(result) == 3

    def test_out_of_range_skipped(self):
        """Page numbers >= doc length should be silently skipped."""
        from app.readers.pdf_reader import PDFReader

        reader = PDFReader("/fake/path.pdf")
        processed_pages = []

        def mock_process(page, page_num, plumber_doc, stitcher, ocr_engine):
            processed_pages.append(page_num)
            from app.readers.base import ExtractedBlock
            block = ExtractedBlock(
                text=f"Page {page_num}", page_or_sheet=page_num,
                source_path="/fake/path.pdf", file_type="pdf",
            )
            return [block], ocr_engine

        mock_doc = MagicMock()
        mock_doc.__len__ = lambda self: 5

        with patch("app.readers.pdf_reader.fitz") as mock_fitz, \
             patch("app.readers.pdf_reader.pdfplumber") as mock_plumber, \
             patch.object(reader, "_process_page", side_effect=mock_process):
            mock_fitz.open.return_value = mock_doc
            mock_plumber_doc = MagicMock()
            mock_plumber.open.return_value.__enter__ = lambda s: mock_plumber_doc
            mock_plumber.open.return_value.__exit__ = lambda s, *a: None
            result = reader.read_pages([0, 2, 100, 200])

        # Only pages 0 and 2 should be processed (100 and 200 are out of range)
        assert processed_pages == [0, 2]
        assert len(result) == 2

    def test_negative_page_skipped(self):
        """Negative page numbers should be silently skipped."""
        from app.readers.pdf_reader import PDFReader

        reader = PDFReader("/fake/path.pdf")
        processed_pages = []

        def mock_process(page, page_num, plumber_doc, stitcher, ocr_engine):
            processed_pages.append(page_num)
            from app.readers.base import ExtractedBlock
            block = ExtractedBlock(
                text=f"Page {page_num}", page_or_sheet=page_num,
                source_path="/fake/path.pdf", file_type="pdf",
            )
            return [block], ocr_engine

        mock_doc = MagicMock()
        mock_doc.__len__ = lambda self: 5

        with patch("app.readers.pdf_reader.fitz") as mock_fitz, \
             patch("app.readers.pdf_reader.pdfplumber") as mock_plumber, \
             patch.object(reader, "_process_page", side_effect=mock_process):
            mock_fitz.open.return_value = mock_doc
            mock_plumber_doc = MagicMock()
            mock_plumber.open.return_value.__enter__ = lambda s: mock_plumber_doc
            mock_plumber.open.return_value.__exit__ = lambda s, *a: None
            result = reader.read_pages([-1, 0, 2])

        assert processed_pages == [0, 2]

    def test_correct_page_or_sheet(self):
        """Blocks should have the correct page_or_sheet from requested pages."""
        from app.readers.pdf_reader import PDFReader

        reader = PDFReader("/fake/path.pdf")

        def mock_process(page, page_num, plumber_doc, stitcher, ocr_engine):
            from app.readers.base import ExtractedBlock
            block = ExtractedBlock(
                text=f"Content of page {page_num}", page_or_sheet=page_num,
                source_path="/fake/path.pdf", file_type="pdf",
            )
            return [block], ocr_engine

        mock_doc = MagicMock()
        mock_doc.__len__ = lambda self: 50

        with patch("app.readers.pdf_reader.fitz") as mock_fitz, \
             patch("app.readers.pdf_reader.pdfplumber") as mock_plumber, \
             patch.object(reader, "_process_page", side_effect=mock_process):
            mock_fitz.open.return_value = mock_doc
            mock_plumber_doc = MagicMock()
            mock_plumber.open.return_value.__enter__ = lambda s: mock_plumber_doc
            mock_plumber.open.return_value.__exit__ = lambda s, *a: None
            result = reader.read_pages([10, 20, 30])

        assert [b.page_or_sheet for b in result] == [10, 20, 30]

    def test_forget_page_called(self):
        """_forget_page should be called for each processed page."""
        from app.readers.pdf_reader import PDFReader

        reader = PDFReader("/fake/path.pdf")

        def mock_process(page, page_num, plumber_doc, stitcher, ocr_engine):
            from app.readers.base import ExtractedBlock
            block = ExtractedBlock(
                text=f"Page {page_num}", page_or_sheet=page_num,
                source_path="/fake/path.pdf", file_type="pdf",
            )
            return [block], ocr_engine

        mock_doc = MagicMock()
        mock_doc.__len__ = lambda self: 50

        with patch("app.readers.pdf_reader.fitz") as mock_fitz, \
             patch("app.readers.pdf_reader.pdfplumber") as mock_plumber, \
             patch.object(reader, "_process_page", side_effect=mock_process):
            mock_fitz.open.return_value = mock_doc
            mock_plumber_doc = MagicMock()
            mock_plumber.open.return_value.__enter__ = lambda s: mock_plumber_doc
            mock_plumber.open.return_value.__exit__ = lambda s, *a: None
            reader.read_pages([5, 15, 25])

        # _forget_page should have been called for each of the 3 pages
        assert mock_doc._forget_page.call_count == 3
        mock_doc._forget_page.assert_any_call(5)
        mock_doc._forget_page.assert_any_call(15)
        mock_doc._forget_page.assert_any_call(25)
