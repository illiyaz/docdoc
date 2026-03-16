"""Tests for coordinate-based extraction and reconciliation (Step 21b-c).

Tests:
- CoordinateExtractor: anchor finding, region computation, field extraction,
  multi-word anchors, skip/value patterns, failed pages, full pipeline
- ExtractionReconciler: prompt building, response parsing, LLM integration,
  error handling
"""
from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from app.llm.client import OllamaClient
from app.pipeline.coordinate_extractor import CoordinateExtractor, _FIELD_TO_RAW, _normalize_field_type
from app.pipeline.reconciliation import (
    ExtractionReconciler,
    _build_reconciliation_prompt,
)
from app.rra.entity_resolver import PIIRecord
from app.structure.document_schema import FieldMapping


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_field_map() -> list[FieldMapping]:
    """Create a standard field map for testing."""
    return [
        FieldMapping(
            field_type="PERSON",
            anchor_text="Client",
            spatial_relationship="same_line_right",
        ),
        FieldMapping(
            field_type="GOVERNMENT_ID",
            anchor_text="Tax No",
            spatial_relationship="line_below",
            value_pattern=r"\d{3}-\d{2}-\d{4}",
        ),
        FieldMapping(
            field_type="DATE_OF_BIRTH",
            anchor_text="DOB",
            spatial_relationship="same_line_right",
        ),
    ]


def _make_word(
    x0: float, y0: float, x1: float, y1: float, text: str,
    block_no: int = 0, line_no: int = 0, word_no: int = 0,
) -> tuple:
    """Create a PyMuPDF-style word tuple."""
    return (x0, y0, x1, y1, text, block_no, line_no, word_no)


def _make_page_words_with_person() -> list[tuple]:
    """Create word tuples simulating a fixed-layout page with PII."""
    return [
        # "Client:" label at (50, 100, 100, 115) + value "John Smith" at (110, 100, 250, 115)
        _make_word(50, 100, 100, 115, "Client:"),
        _make_word(110, 100, 170, 115, "John"),
        _make_word(175, 100, 250, 115, "Smith"),
        # "Tax No" label at (50, 140, 110, 155), value "123-45-6789" below
        _make_word(50, 140, 80, 155, "Tax"),
        _make_word(85, 140, 110, 155, "No"),
        _make_word(50, 160, 150, 175, "123-45-6789"),
        # "DOB:" label at (300, 100, 340, 115) + value "15/03/1980"
        _make_word(300, 100, 340, 115, "DOB:"),
        _make_word(345, 100, 440, 115, "15/03/1980"),
    ]


def _make_page_words_without_person() -> list[tuple]:
    """Create word tuples without a PERSON field (missing Client label)."""
    return [
        _make_word(50, 140, 80, 155, "Tax"),
        _make_word(85, 140, 110, 155, "No"),
        _make_word(50, 160, 150, 175, "123-45-6789"),
        _make_word(300, 100, 340, 115, "DOB:"),
        _make_word(345, 100, 440, 115, "15/03/1980"),
    ]


# ---------------------------------------------------------------------------
# CoordinateExtractor — unit tests
# ---------------------------------------------------------------------------


class TestAnchorFinding:
    """Tests for _find_anchor()."""

    def test_single_word_anchor(self):
        words = [_make_word(50, 100, 100, 115, "Client:")]
        result = CoordinateExtractor._find_anchor(words, "Client")
        assert result is not None
        assert len(result) == 1
        assert result[0][4] == "Client:"

    def test_single_word_anchor_case_insensitive(self):
        words = [_make_word(50, 100, 100, 115, "CLIENT:")]
        result = CoordinateExtractor._find_anchor(words, "client")
        assert result is not None

    def test_multi_word_anchor(self):
        words = [
            _make_word(50, 140, 80, 155, "Tax"),
            _make_word(85, 140, 110, 155, "No"),
        ]
        result = CoordinateExtractor._find_anchor(words, "Tax No")
        assert result is not None
        assert len(result) == 2

    def test_anchor_not_found(self):
        words = [_make_word(50, 100, 100, 115, "Other:")]
        result = CoordinateExtractor._find_anchor(words, "Client")
        assert result is None

    def test_empty_anchor(self):
        words = [_make_word(50, 100, 100, 115, "Client:")]
        assert CoordinateExtractor._find_anchor(words, "") is None
        assert CoordinateExtractor._find_anchor(words, "   ") is None

    def test_multi_word_anchor_different_lines(self):
        """Multi-word anchor words on different lines should NOT match."""
        words = [
            _make_word(50, 100, 80, 115, "Tax"),  # line at y=100
            _make_word(50, 200, 80, 215, "No"),   # line at y=200
        ]
        result = CoordinateExtractor._find_anchor(words, "Tax No")
        assert result is None


class TestRegionComputation:
    """Tests for _compute_region()."""

    @staticmethod
    def _mock_page(width=600, height=800, rotation=0):
        page = MagicMock()
        page.rotation = rotation
        page.rect = MagicMock()
        page.rect.width = width
        page.rect.height = height
        return page

    def test_same_line_right(self):
        anchor = (50, 100, 100, 115)
        fm = FieldMapping(field_type="PERSON", anchor_text="X", spatial_relationship="same_line_right")
        region = CoordinateExtractor._compute_region(anchor, fm, self._mock_page())
        assert region is not None
        # Region should start to the right of anchor
        assert region[0] > anchor[2]
        # Region y should roughly match anchor y
        assert region[1] <= anchor[1]
        assert region[3] >= anchor[3]

    def test_line_below(self):
        anchor = (50, 100, 100, 115)
        fm = FieldMapping(field_type="GOVERNMENT_ID", anchor_text="X", spatial_relationship="line_below")
        region = CoordinateExtractor._compute_region(anchor, fm, self._mock_page())
        assert region is not None
        # Region should start below anchor
        assert region[1] >= anchor[3]

    def test_lines_below_n(self):
        anchor = (50, 100, 100, 115)
        fm = FieldMapping(
            field_type="LOCATION", anchor_text="X",
            spatial_relationship="lines_below_4", line_count=4,
        )
        region = CoordinateExtractor._compute_region(anchor, fm, self._mock_page())
        assert region is not None
        # Region should extend further below for 4 lines
        line_height = (anchor[3] - anchor[1]) or 15
        assert region[3] > anchor[3] + line_height * 3

    def test_region_right(self):
        anchor = (50, 100, 100, 115)
        fm = FieldMapping(
            field_type="LOCATION", anchor_text="X",
            spatial_relationship="region_right", line_count=3,
        )
        region = CoordinateExtractor._compute_region(anchor, fm, self._mock_page())
        assert region is not None
        assert region[0] > anchor[2]

    def test_unknown_relationship_fallback(self):
        anchor = (50, 100, 100, 115)
        fm = FieldMapping(field_type="PERSON", anchor_text="X", spatial_relationship="unknown_type")
        region = CoordinateExtractor._compute_region(anchor, fm, self._mock_page())
        # Falls back to same_line_right
        assert region is not None
        assert region[0] > anchor[2]


class TestWordsToText:
    """Tests for _words_to_text()."""

    def test_single_line(self):
        words = [
            _make_word(50, 100, 80, 115, "John"),
            _make_word(85, 100, 130, 115, "Smith"),
        ]
        result = CoordinateExtractor._words_to_text(words, line_count=1)
        assert result == "John Smith"

    def test_multi_line(self):
        words = [
            _make_word(50, 100, 130, 115, "123"),
            _make_word(135, 100, 250, 115, "Main St"),
            _make_word(50, 120, 130, 135, "London"),
            _make_word(135, 120, 250, 135, "EC1A 1BB"),
        ]
        result = CoordinateExtractor._words_to_text(words, line_count=2)
        assert "123" in result
        assert "London" in result

    def test_line_count_limit(self):
        words = [
            _make_word(50, 100, 130, 115, "Line1"),
            _make_word(50, 120, 130, 135, "Line2"),
            _make_word(50, 140, 130, 155, "Line3"),
        ]
        result = CoordinateExtractor._words_to_text(words, line_count=1)
        assert "Line1" in result
        assert "Line2" not in result

    def test_empty_words(self):
        assert CoordinateExtractor._words_to_text([], line_count=1) == ""


class TestInRegion:
    """Tests for _in_region()."""

    def test_word_inside_region(self):
        word = _make_word(110, 100, 170, 115, "Value")
        region = (100, 90, 200, 120)
        assert CoordinateExtractor._in_region(word, region) is True

    def test_word_outside_region(self):
        word = _make_word(300, 300, 400, 315, "Value")
        region = (100, 90, 200, 120)
        assert CoordinateExtractor._in_region(word, region) is False


class TestFieldExtraction:
    """Tests for _extract_field() with various patterns."""

    @staticmethod
    def _mock_page(width=600, height=800):
        page = MagicMock()
        page.rect = MagicMock()
        page.rect.width = width
        page.rect.height = height
        return page

    def test_extract_with_skip_pattern(self):
        """Skip pattern should remove matching text from the extracted value."""
        fm = FieldMapping(
            field_type="PERSON",
            anchor_text="Client",
            spatial_relationship="same_line_right",
            skip_pattern=r"\(\d+\)",
        )
        words = [
            _make_word(50, 100, 100, 115, "Client:"),
            _make_word(110, 100, 170, 115, "(001968)"),
            _make_word(180, 100, 280, 115, "Smith"),
        ]
        ext = CoordinateExtractor([], "", "")
        value = ext._extract_field(words, fm, self._mock_page())
        assert value is not None
        assert "(001968)" not in value
        assert "Smith" in value

    def test_extract_with_value_pattern_match(self):
        fm = FieldMapping(
            field_type="GOVERNMENT_ID",
            anchor_text="SSN",
            spatial_relationship="same_line_right",
            value_pattern=r"\d{3}-\d{2}-\d{4}",
        )
        words = [
            _make_word(50, 100, 100, 115, "SSN:"),
            _make_word(110, 100, 220, 115, "123-45-6789"),
        ]
        ext = CoordinateExtractor([], "", "")
        value = ext._extract_field(words, fm, self._mock_page())
        assert value == "123-45-6789"

    def test_extract_with_value_pattern_no_match(self):
        fm = FieldMapping(
            field_type="GOVERNMENT_ID",
            anchor_text="SSN",
            spatial_relationship="same_line_right",
            value_pattern=r"\d{3}-\d{2}-\d{4}",
        )
        words = [
            _make_word(50, 100, 100, 115, "SSN:"),
            _make_word(110, 100, 220, 115, "N/A"),
        ]
        ext = CoordinateExtractor([], "", "")
        value = ext._extract_field(words, fm, self._mock_page())
        assert value is None


class TestMergeBboxes:
    """Tests for _merge_bboxes()."""

    def test_merge_two_words(self):
        words = [
            _make_word(50, 100, 80, 115, "Tax"),
            _make_word(85, 100, 110, 115, "No"),
        ]
        bbox = CoordinateExtractor._merge_bboxes(words)
        assert bbox == (50, 100, 110, 115)


class TestFieldToRawMapping:
    """Tests that _FIELD_TO_RAW maps entity types to correct PIIRecord fields."""

    def test_person_maps_to_raw_name(self):
        assert _FIELD_TO_RAW["PERSON"] == "raw_name"

    def test_address_maps_correctly(self):
        assert _FIELD_TO_RAW["LOCATION"] == "raw_address"

    def test_gov_id_maps_correctly(self):
        assert _FIELD_TO_RAW["US_SSN"] == "raw_government_id"

    def test_dob_maps_correctly(self):
        assert _FIELD_TO_RAW["DATE_OF_BIRTH"] == "raw_dob"


# ---------------------------------------------------------------------------
# CoordinateExtractor — integration tests with mock PDF
# ---------------------------------------------------------------------------


class TestExtractAllPages:
    """Integration tests using a real PDF created with PyMuPDF."""

    @staticmethod
    def _create_test_pdf(pages_data: list[list[tuple[str, float, float]]]) -> str:
        """Create a temporary PDF with text at specified positions.

        Parameters
        ----------
        pages_data:
            List of pages.  Each page is a list of ``(text, x, y)`` tuples.

        Returns
        -------
        str
            Path to the created temporary PDF.
        """
        doc = __import__("fitz").open()
        for page_items in pages_data:
            page = doc.new_page(width=612, height=792)
            for text, x, y in page_items:
                page.insert_text((x, y), text, fontsize=11)
        path = os.path.join(tempfile.gettempdir(), f"test_coord_{id(pages_data)}.pdf")
        doc.save(path)
        doc.close()
        return path

    def test_extract_fixed_layout_page(self):
        """Extract PII from a page with labeled fields."""
        pdf_path = self._create_test_pdf([
            [
                ("Client: John Smith", 50, 120),
                ("DOB: 15/03/1980", 300, 120),
                ("Tax No", 50, 160),
                ("123-45-6789", 50, 180),
            ],
        ])
        try:
            field_map = _make_field_map()
            ext = CoordinateExtractor(field_map, pdf_path, "doc-1")
            records, failed = ext.extract_all_pages()
            assert len(records) == 1
            assert len(failed) == 0
            rec = records[0]
            assert rec.raw_name is not None
            assert "John" in rec.raw_name or "Smith" in rec.raw_name
            assert rec.source_document_id == "doc-1"
            assert rec.page_range == "1"
            assert "PERSON" in rec.entity_types_found
        finally:
            os.unlink(pdf_path)

    def test_missing_person_is_failure(self):
        """Page without a PERSON field should be in failed_pages."""
        pdf_path = self._create_test_pdf([
            [
                ("Tax No", 50, 160),
                ("123-45-6789", 50, 180),
            ],
        ])
        try:
            field_map = _make_field_map()
            ext = CoordinateExtractor(field_map, pdf_path, "doc-2")
            records, failed = ext.extract_all_pages()
            assert len(records) == 0
            assert 0 in failed
        finally:
            os.unlink(pdf_path)

    def test_multiple_pages(self):
        """Extract from a multi-page PDF."""
        pdf_path = self._create_test_pdf([
            [("Client: Alice Brown", 50, 120), ("DOB: 01/01/1970", 300, 120)],
            [("Client: Bob White", 50, 120), ("DOB: 02/02/1980", 300, 120)],
            [("Some other content", 50, 120)],  # no Client label → fail
        ])
        try:
            field_map = [
                FieldMapping(field_type="PERSON", anchor_text="Client", spatial_relationship="same_line_right"),
                FieldMapping(field_type="DATE_OF_BIRTH", anchor_text="DOB", spatial_relationship="same_line_right"),
            ]
            ext = CoordinateExtractor(field_map, pdf_path, "doc-3")
            records, failed = ext.extract_all_pages()
            assert len(records) == 2
            assert 2 in failed  # page index 2
        finally:
            os.unlink(pdf_path)

    def test_page_range_filter(self):
        """Only process specified pages."""
        pdf_path = self._create_test_pdf([
            [("Client: Page1 Person", 50, 120)],
            [("Client: Page2 Person", 50, 120)],
            [("Client: Page3 Person", 50, 120)],
        ])
        try:
            field_map = [
                FieldMapping(field_type="PERSON", anchor_text="Client", spatial_relationship="same_line_right"),
            ]
            ext = CoordinateExtractor(field_map, pdf_path, "doc-4")
            records, failed = ext.extract_all_pages(page_range=[0, 2])
            assert len(records) == 2  # pages 0 and 2 only
        finally:
            os.unlink(pdf_path)

    def test_out_of_range_page(self):
        """Out-of-range page number should be in failed_pages."""
        pdf_path = self._create_test_pdf([
            [("Client: Someone", 50, 120)],
        ])
        try:
            field_map = [
                FieldMapping(field_type="PERSON", anchor_text="Client", spatial_relationship="same_line_right"),
            ]
            ext = CoordinateExtractor(field_map, pdf_path, "doc-5")
            records, failed = ext.extract_all_pages(page_range=[0, 5])
            assert len(records) == 1
            assert 5 in failed
        finally:
            os.unlink(pdf_path)


# ---------------------------------------------------------------------------
# ExtractionReconciler — tests
# ---------------------------------------------------------------------------


class TestBuildReconciliationPrompt:
    """Tests for _build_reconciliation_prompt()."""

    def test_prompt_includes_field_types(self):
        field_map = _make_field_map()
        prompt = _build_reconciliation_prompt("Some page text", field_map)
        assert "PERSON" in prompt
        assert "GOVERNMENT_ID" in prompt
        assert "DATE_OF_BIRTH" in prompt
        assert "Client" in prompt
        assert "Tax No" in prompt

    def test_prompt_includes_page_text(self):
        prompt = _build_reconciliation_prompt("Hello world", [])
        assert "Hello world" in prompt

    def test_prompt_truncates_long_text(self):
        long_text = "A" * 5000
        prompt = _build_reconciliation_prompt(long_text, [])
        # Should contain truncated text (3000 chars max)
        assert len(prompt) < 5000 + 500  # prompt overhead + truncated text

    def test_prompt_includes_value_pattern(self):
        field_map = [
            FieldMapping(
                field_type="US_SSN", anchor_text="SSN",
                spatial_relationship="same_line_right",
                value_pattern=r"\d{3}-\d{2}-\d{4}",
            ),
        ]
        prompt = _build_reconciliation_prompt("text", field_map)
        assert "\\d{3}-\\d{2}-\\d{4}" in prompt


class TestReconcilerParseResponse:
    """Tests for ExtractionReconciler._parse_response()."""

    def test_valid_json_response(self):
        response = json.dumps({
            "PERSON": "Jane Doe",
            "DATE_OF_BIRTH": "01/01/1990",
            "LOCATION": "456 Oak Ave, NY",
        })
        rec = ExtractionReconciler._parse_response(response, "doc-1", 5, [])
        assert rec is not None
        assert rec.raw_name == "Jane Doe"
        assert rec.raw_dob == "01/01/1990"
        assert rec.raw_address == {"full": "456 Oak Ave, NY"}
        assert rec.page_range == "6"  # 0-based → 1-based

    def test_json_with_code_fences(self):
        response = '```json\n{"PERSON": "John"}\n```'
        rec = ExtractionReconciler._parse_response(response, "doc-1", 0, [])
        assert rec is not None
        assert rec.raw_name == "John"

    def test_missing_person_returns_none(self):
        response = json.dumps({"DATE_OF_BIRTH": "01/01/1990"})
        rec = ExtractionReconciler._parse_response(response, "doc-1", 0, [])
        assert rec is None

    def test_invalid_json_returns_none(self):
        rec = ExtractionReconciler._parse_response("not json at all", "doc-1", 0, [])
        assert rec is None

    def test_embedded_json_extraction(self):
        response = 'Here is the data: {"PERSON": "Alice", "NI_NUMBER": "AB123456C"}'
        rec = ExtractionReconciler._parse_response(response, "doc-1", 0, [])
        assert rec is not None
        assert rec.raw_name == "Alice"
        assert rec.raw_government_id == "AB123456C"

    def test_entity_types_found_populated(self):
        response = json.dumps({
            "PERSON": "Bob",
            "US_SSN": "123-45-6789",
            "EMAIL_ADDRESS": "bob@test.com",
        })
        rec = ExtractionReconciler._parse_response(response, "doc-1", 3, [])
        assert rec is not None
        assert "PERSON" in rec.entity_types_found
        assert "US_SSN" in rec.entity_types_found
        assert "EMAIL_ADDRESS" in rec.entity_types_found

    def test_extraction_method_set(self):
        """Records from reconciliation should have no extraction_method by default
        (caller sets it)."""
        response = json.dumps({"PERSON": "Test"})
        rec = ExtractionReconciler._parse_response(response, "doc-1", 0, [])
        assert rec is not None
        # Caller is responsible for setting extraction_method


class TestReconcilerIntegration:
    """Integration tests for ExtractionReconciler.reconcile()."""

    def test_reconcile_success(self):
        """LLM successfully extracts from failed page."""
        mock_client = MagicMock(spec=OllamaClient)
        mock_client.generate.return_value = json.dumps({
            "PERSON": "Recovered Person",
            "DATE_OF_BIRTH": "25/12/1985",
        })

        pdf_path = TestExtractAllPages._create_test_pdf([
            [("Some text on page", 50, 120)],
        ])
        try:
            reconciler = ExtractionReconciler()
            records = reconciler.reconcile(
                failed_pages=[0],
                doc_path=pdf_path,
                doc_id="doc-r1",
                field_map=_make_field_map(),
                ollama_client=mock_client,
            )
            assert len(records) == 1
            assert records[0].raw_name == "Recovered Person"
            assert records[0].raw_dob == "25/12/1985"
            mock_client.generate.assert_called_once()
        finally:
            os.unlink(pdf_path)

    def test_reconcile_llm_failure(self):
        """LLM raises exception — page stays failed."""
        mock_client = MagicMock(spec=OllamaClient)
        mock_client.generate.side_effect = Exception("LLM error")

        pdf_path = TestExtractAllPages._create_test_pdf([
            [("Some text", 50, 120)],
        ])
        try:
            reconciler = ExtractionReconciler()
            records = reconciler.reconcile(
                failed_pages=[0],
                doc_path=pdf_path,
                doc_id="doc-r2",
                field_map=_make_field_map(),
                ollama_client=mock_client,
            )
            assert len(records) == 0
        finally:
            os.unlink(pdf_path)

    def test_reconcile_bad_json(self):
        """LLM returns non-parseable response — page stays failed."""
        mock_client = MagicMock(spec=OllamaClient)
        mock_client.generate.return_value = "I don't understand"

        pdf_path = TestExtractAllPages._create_test_pdf([
            [("Some text", 50, 120)],
        ])
        try:
            reconciler = ExtractionReconciler()
            records = reconciler.reconcile(
                failed_pages=[0],
                doc_path=pdf_path,
                doc_id="doc-r3",
                field_map=_make_field_map(),
                ollama_client=mock_client,
            )
            assert len(records) == 0
        finally:
            os.unlink(pdf_path)

    def test_reconcile_empty_failed_pages(self):
        """No failed pages → no LLM calls, empty result."""
        mock_client = MagicMock(spec=OllamaClient)
        reconciler = ExtractionReconciler()
        records = reconciler.reconcile(
            failed_pages=[],
            doc_path="/nonexistent",
            doc_id="doc-r4",
            field_map=[],
            ollama_client=mock_client,
        )
        assert records == []
        mock_client.generate.assert_not_called()

    def test_reconcile_out_of_range_page(self):
        """Out-of-range page number → still_failed, no crash."""
        mock_client = MagicMock(spec=OllamaClient)

        pdf_path = TestExtractAllPages._create_test_pdf([
            [("Text", 50, 120)],
        ])
        try:
            reconciler = ExtractionReconciler()
            records = reconciler.reconcile(
                failed_pages=[99],
                doc_path=pdf_path,
                doc_id="doc-r5",
                field_map=_make_field_map(),
                ollama_client=mock_client,
            )
            assert len(records) == 0
            mock_client.generate.assert_not_called()
        finally:
            os.unlink(pdf_path)


# ---------------------------------------------------------------------------
# Field mapping coverage
# ---------------------------------------------------------------------------


class TestFieldMapping:
    """Ensure _FIELD_TO_RAW covers all common entity types."""

    def test_person_mapped(self):
        assert _FIELD_TO_RAW["PERSON"] == "raw_name"

    def test_location_mapped(self):
        assert _FIELD_TO_RAW["LOCATION"] == "raw_address"

    def test_gov_id_types_mapped(self):
        gov_types = ["US_SSN", "NI_NUMBER", "AADHAAR", "US_DRIVER_LICENSE"]
        for t in gov_types:
            assert _FIELD_TO_RAW[t] == "raw_government_id"

    def test_contact_types_mapped(self):
        assert _FIELD_TO_RAW["EMAIL_ADDRESS"] == "raw_email"
        assert _FIELD_TO_RAW["PHONE_NUMBER"] == "raw_phone"

    def test_dob_types_mapped(self):
        for t in ["DATE_OF_BIRTH", "DATE_OF_BIRTH_DMY", "DATE_OF_BIRTH_MDY", "DATE_OF_BIRTH_ISO"]:
            assert _FIELD_TO_RAW[t] == "raw_dob"


# ---------------------------------------------------------------------------
# Rotation awareness tests (BUG 1 fix)
# ---------------------------------------------------------------------------


class TestRotationAwareness:
    """Tests for rotation-aware region computation and anchor finding."""

    def _mock_page(self, width: float = 612, height: float = 792, rotation: int = 0):
        """Create a mock page with rect and rotation."""
        page = MagicMock()
        page.rotation = rotation
        page.rect = MagicMock()
        page.rect.width = width
        page.rect.height = height
        return page

    def test_rotation_0_same_line_right_standard(self):
        """Rotation 0: same_line_right → region to the right (+x)."""
        anchor = (50, 100, 100, 115)
        fm = FieldMapping(field_type="PERSON", anchor_text="X", spatial_relationship="same_line_right")
        page = self._mock_page(rotation=0)
        region = CoordinateExtractor._compute_region(anchor, fm, page, rotation=0)
        assert region is not None
        # Region starts to the right of anchor
        assert region[0] > anchor[2]
        # Region y roughly matches anchor y
        assert region[1] <= anchor[1]
        assert region[3] >= anchor[3]

    def test_rotation_270_same_line_right_y_increasing(self):
        """Rotation 270: same_line_right → region below in y (+y) at tight x band."""
        anchor = (50, 100, 70, 200)  # narrow x band, tall y
        fm = FieldMapping(field_type="PERSON", anchor_text="X", spatial_relationship="same_line_right")
        page = self._mock_page(rotation=270)
        region = CoordinateExtractor._compute_region(anchor, fm, page, rotation=270)
        assert region is not None
        # Region y starts after anchor y1
        assert region[1] > anchor[3]
        # Region x band is tight around anchor (within ~2pt padding)
        assert region[0] >= anchor[0] - 3
        assert region[2] <= anchor[2] + 3

    def test_rotation_90_same_line_right_y_decreasing(self):
        """Rotation 90: same_line_right → region above in y (-y) at tight x band."""
        anchor = (50, 400, 70, 500)
        fm = FieldMapping(field_type="PERSON", anchor_text="X", spatial_relationship="same_line_right")
        page = self._mock_page(rotation=90)
        region = CoordinateExtractor._compute_region(anchor, fm, page, rotation=90)
        assert region is not None
        # Region y ends before anchor y0
        assert region[3] < anchor[1]
        # Region x band is tight around anchor (within ~2pt padding)
        assert region[0] >= anchor[0] - 3
        assert region[2] <= anchor[2] + 3

    def test_rotation_180_same_line_right_x_decreasing(self):
        """Rotation 180: same_line_right → region to the left (-x)."""
        anchor = (300, 100, 400, 115)
        fm = FieldMapping(field_type="PERSON", anchor_text="X", spatial_relationship="same_line_right")
        page = self._mock_page(rotation=180)
        region = CoordinateExtractor._compute_region(anchor, fm, page, rotation=180)
        assert region is not None
        # Region x1 < anchor x0 (to the left)
        assert region[2] < anchor[0]

    def test_rotation_270_line_below(self):
        """Rotation 270: line_below → region shifts in -x direction (visual below)."""
        anchor = (50, 100, 70, 200)
        fm = FieldMapping(field_type="GOVERNMENT_ID", anchor_text="X", spatial_relationship="line_below")
        page = self._mock_page(rotation=270)
        region = CoordinateExtractor._compute_region(anchor, fm, page, rotation=270)
        assert region is not None
        # Region x1 ends at anchor x0 (shifted left = visual "below" for 270)
        assert region[2] <= anchor[0]

    def test_rotation_0_lines_below_n(self):
        """Rotation 0: lines_below_3 produces a region below anchor."""
        anchor = (50, 100, 100, 115)
        fm = FieldMapping(field_type="LOCATION", anchor_text="X", spatial_relationship="lines_below_3", line_count=3)
        page = self._mock_page(rotation=0)
        region = CoordinateExtractor._compute_region(anchor, fm, page, rotation=0)
        assert region is not None
        assert region[1] >= anchor[3]  # starts below
        line_height = (anchor[3] - anchor[1]) or 15
        assert region[3] > anchor[3] + line_height * 2  # extends for multiple lines

    def test_rotation_270_region_right(self):
        """Rotation 270: region_right extends in +y and +x."""
        anchor = (50, 100, 70, 200)
        fm = FieldMapping(field_type="LOCATION", anchor_text="X", spatial_relationship="region_right", line_count=3)
        page = self._mock_page(rotation=270)
        region = CoordinateExtractor._compute_region(anchor, fm, page, rotation=270)
        assert region is not None
        # Should extend in y direction (past anchor y1)
        assert region[1] > anchor[3]

    def test_anchor_finding_uses_x_for_same_line_on_270(self):
        """On 270-rotated pages, multi-word anchors match by x proximity (not y)."""
        # Words on same x band but different y (which is "same visual line" for 270)
        words = [
            _make_word(50, 100, 70, 150, "Tax"),   # same x ≈ 50-70
            _make_word(50, 160, 70, 210, "No"),     # same x ≈ 50-70
        ]
        result = CoordinateExtractor._find_anchor(words, "Tax No", rotation=270)
        assert result is not None
        assert len(result) == 2

    def test_anchor_finding_uses_y_for_same_line_on_0(self):
        """On standard pages, multi-word anchors match by y proximity."""
        words = [
            _make_word(50, 100, 80, 115, "Tax"),
            _make_word(85, 100, 110, 115, "No"),
        ]
        result = CoordinateExtractor._find_anchor(words, "Tax No", rotation=0)
        assert result is not None

    def test_words_to_text_groups_by_x_on_270(self):
        """On 270-rotated pages, lines are grouped by x, sorted within by y."""
        words = [
            _make_word(50, 100, 70, 130, "John"),
            _make_word(50, 140, 70, 170, "Smith"),
        ]
        result = CoordinateExtractor._words_to_text(words, line_count=1, rotation=270)
        assert "John" in result
        assert "Smith" in result  # same x band → same line


class TestPersonValuePatternSkip:
    """Tests that PERSON fields skip value_pattern validation (BUG 3 fix)."""

    def test_person_field_ignores_value_pattern(self):
        """PERSON field with value_pattern should still extract names."""
        fm = FieldMapping(
            field_type="PERSON",
            anchor_text="Client",
            spatial_relationship="same_line_right",
            value_pattern=r"[A-Z]+ [A-Z]+",  # would reject "(001968) ADELINE CHANDLER"
        )
        words = [
            _make_word(50, 100, 100, 115, "Client:"),
            _make_word(110, 100, 180, 115, "(001968)"),
            _make_word(185, 100, 280, 115, "ADELINE"),
            _make_word(285, 100, 380, 115, "CHANDLER"),
        ]
        ext = CoordinateExtractor([], "", "")
        page = MagicMock()
        page.rect = MagicMock()
        page.rect.width = 600
        page.rect.height = 800
        value = ext._extract_field(words, fm, page, rotation=0)
        assert value is not None
        assert "ADELINE" in value or "CHANDLER" in value

    def test_person_default_cleanup_strips_client_code(self):
        """PERSON fields auto-strip parenthesized client codes even without skip_pattern."""
        fm = FieldMapping(
            field_type="PERSON",
            anchor_text="Client",
            spatial_relationship="same_line_right",
            # No skip_pattern set — built-in cleanup should handle it
        )
        words = [
            _make_word(50, 100, 100, 115, "Client:"),
            _make_word(110, 100, 180, 115, "(001968)"),
            _make_word(185, 100, 280, 115, "ADELINE"),
            _make_word(285, 100, 380, 115, "CHANDLER"),
        ]
        ext = CoordinateExtractor([], "", "")
        page = MagicMock()
        page.rect = MagicMock()
        page.rect.width = 600
        page.rect.height = 800
        value = ext._extract_field(words, fm, page, rotation=0)
        assert value is not None
        assert "(001968)" not in value
        assert "ADELINE" in value
        assert "CHANDLER" in value

    def test_person_cleanup_no_code_unaffected(self):
        """PERSON values without client codes are not modified by cleanup."""
        fm = FieldMapping(
            field_type="PERSON",
            anchor_text="Client",
            spatial_relationship="same_line_right",
        )
        words = [
            _make_word(50, 100, 100, 115, "Client:"),
            _make_word(110, 100, 200, 115, "Jane"),
            _make_word(205, 100, 290, 115, "Doe"),
        ]
        ext = CoordinateExtractor([], "", "")
        page = MagicMock()
        page.rect = MagicMock()
        page.rect.width = 600
        page.rect.height = 800
        value = ext._extract_field(words, fm, page, rotation=0)
        assert value == "Jane Doe"

    def test_gov_id_still_validates_pattern(self):
        """Non-PERSON fields should still use value_pattern."""
        fm = FieldMapping(
            field_type="GOVERNMENT_ID",
            anchor_text="SSN",
            spatial_relationship="same_line_right",
            value_pattern=r"\d{3}-\d{2}-\d{4}",
        )
        words = [
            _make_word(50, 100, 100, 115, "SSN:"),
            _make_word(110, 100, 220, 115, "N/A"),
        ]
        ext = CoordinateExtractor([], "", "")
        page = MagicMock()
        page.rect = MagicMock()
        page.rect.width = 600
        page.rect.height = 800
        value = ext._extract_field(words, fm, page, rotation=0)
        assert value is None  # "N/A" doesn't match SSN pattern


# ---------------------------------------------------------------------------
# Address fallback tests
# ---------------------------------------------------------------------------


class TestAddressFallback:
    """Tests for address extraction when LOCATION is missing from field_map."""

    def _make_page_words_with_address(self):
        """Page with 'In Account with' anchor, name on same line, address below."""
        return [
            # Anchor: "In Account with :"
            _make_word(50, 100, 80, 115, "In"),
            _make_word(85, 100, 130, 115, "Account"),
            _make_word(135, 100, 165, 115, "with"),
            _make_word(170, 100, 180, 115, ":"),
            # Name on same line (after colon)
            _make_word(185, 100, 230, 115, "(001968)"),
            _make_word(235, 100, 310, 115, "ADELINE"),
            _make_word(315, 100, 400, 115, "CHANDLER"),
            # Address lines below
            _make_word(50, 130, 100, 145, "3708"),
            _make_word(105, 130, 160, 145, "GRAHAM"),
            _make_word(165, 130, 210, 145, "ROAD"),
            _make_word(50, 150, 110, 165, "ROCK"),
            _make_word(115, 150, 165, 165, "CREEK"),
            _make_word(50, 170, 70, 185, "OH"),
            _make_word(75, 170, 120, 185, "44084"),
            _make_word(50, 190, 80, 205, "USA"),
        ]

    def test_address_fallback_when_no_location_field(self):
        """Address extracted below PERSON anchor when field_map has no LOCATION."""
        field_map = [
            FieldMapping(
                field_type="PERSON",
                anchor_text="In Account with",
                spatial_relationship="same_line_right",
            ),
            FieldMapping(
                field_type="US_SSN",
                anchor_text="Tax No",
                spatial_relationship="same_line_right",
                value_pattern=r"\d{3}-\d{2}-\d{4}",
            ),
        ]
        ext = CoordinateExtractor(field_map, "", "doc1")
        words = self._make_page_words_with_address()
        page = MagicMock()
        page.rect = MagicMock()
        page.rect.width = 600
        page.rect.height = 800
        page.rotation = 0

        # Simulate extraction on one page
        rotation = 0
        fields: dict = {}
        entity_types: list = []
        person_anchor_fm = None
        for fm in field_map:
            norm_type = _normalize_field_type(fm.field_type)
            value = ext._extract_field(words, fm, page, rotation, norm_type)
            if value:
                raw_field = _FIELD_TO_RAW.get(norm_type)
                if raw_field:
                    fields[raw_field] = value
                    entity_types.append(norm_type)
                if norm_type == "PERSON":
                    person_anchor_fm = fm

        # Name should be extracted
        assert "raw_name" in fields
        assert "ADELINE" in fields["raw_name"]

        # Address should NOT be in fields yet (no LOCATION in field_map)
        assert "raw_address" not in fields

        # Now test the fallback
        assert person_anchor_fm is not None
        assert not ext._has_location_field()
        addr = ext._extract_address_below_person(words, person_anchor_fm, page, rotation)
        assert addr is not None
        assert "GRAHAM" in addr
        assert "ROAD" in addr

    def test_no_fallback_when_location_field_exists(self):
        """No fallback when field_map already has a LOCATION field."""
        field_map = [
            FieldMapping(
                field_type="PERSON",
                anchor_text="Client",
                spatial_relationship="same_line_right",
            ),
            FieldMapping(
                field_type="LOCATION",
                anchor_text="Address",
                spatial_relationship="lines_below_4",
                line_count=4,
            ),
        ]
        ext = CoordinateExtractor(field_map, "", "doc1")
        assert ext._has_location_field()

    def test_no_fallback_when_text_is_not_address(self):
        """No fallback when content below PERSON anchor doesn't look like an address."""
        field_map = [
            FieldMapping(
                field_type="PERSON",
                anchor_text="Client",
                spatial_relationship="same_line_right",
            ),
        ]
        ext = CoordinateExtractor(field_map, "", "doc1")
        words = [
            _make_word(50, 100, 100, 115, "Client:"),
            _make_word(110, 100, 200, 115, "Jane"),
            _make_word(205, 100, 290, 115, "Doe"),
            # Below: transaction data, not an address
            _make_word(50, 130, 120, 145, "Invoice"),
            _make_word(125, 130, 180, 145, "Number:"),
            _make_word(185, 130, 260, 145, "INV-9876"),
        ]
        page = MagicMock()
        page.rect = MagicMock()
        page.rect.width = 600
        page.rect.height = 800
        fm = field_map[0]
        addr = ext._extract_address_below_person(words, fm, page, rotation=0)
        assert addr is None

    def test_address_fallback_with_uk_postcode(self):
        """Fallback detects UK postcodes as address content."""
        field_map = [
            FieldMapping(
                field_type="PERSON",
                anchor_text="Client",
                spatial_relationship="same_line_right",
            ),
        ]
        ext = CoordinateExtractor(field_map, "", "doc1")
        words = [
            _make_word(50, 100, 100, 115, "Client:"),
            _make_word(110, 100, 200, 115, "John"),
            _make_word(205, 100, 290, 115, "Smith"),
            _make_word(50, 130, 150, 145, "14"),
            _make_word(155, 130, 250, 145, "Harrow"),
            _make_word(255, 130, 330, 145, "Road"),
            _make_word(50, 150, 120, 165, "London"),
            _make_word(50, 170, 120, 185, "SW1A"),
            _make_word(125, 170, 170, 185, "2AA"),
        ]
        page = MagicMock()
        page.rect = MagicMock()
        page.rect.width = 600
        page.rect.height = 800
        fm = field_map[0]
        addr = ext._extract_address_below_person(words, fm, page, rotation=0)
        assert addr is not None
        assert "Harrow" in addr

    def test_address_fallback_alias_field_type(self):
        """Fallback works when field_map has ADDRESS alias (normalizes to LOCATION)."""
        field_map = [
            FieldMapping(
                field_type="PERSON",
                anchor_text="Client",
                spatial_relationship="same_line_right",
            ),
            FieldMapping(
                field_type="ADDRESS",  # alias for LOCATION
                anchor_text="Address",
                spatial_relationship="lines_below_4",
                line_count=4,
            ),
        ]
        ext = CoordinateExtractor(field_map, "", "doc1")
        # ADDRESS normalizes to LOCATION, so _has_location_field should be True
        assert ext._has_location_field()


class TestLooksLikeAddress:
    """Tests for the _looks_like_address helper."""

    def test_us_street_address(self):
        from app.pipeline.coordinate_extractor import _looks_like_address
        assert _looks_like_address("3708 GRAHAM ROAD\nROCK CREEK\nOH 44084\nUSA")

    def test_zip_code(self):
        from app.pipeline.coordinate_extractor import _looks_like_address
        assert _looks_like_address("Some Place\n12345")

    def test_uk_postcode(self):
        from app.pipeline.coordinate_extractor import _looks_like_address
        assert _looks_like_address("London\nSW1A 2AA")

    def test_state_abbreviation(self):
        from app.pipeline.coordinate_extractor import _looks_like_address
        assert _looks_like_address("SPRINGFIELD\nIL")

    def test_country_name(self):
        from app.pipeline.coordinate_extractor import _looks_like_address
        assert _looks_like_address("Some Place\nUnited Kingdom")

    def test_street_keyword(self):
        from app.pipeline.coordinate_extractor import _looks_like_address
        assert _looks_like_address("14 Harrow Avenue")

    def test_not_address(self):
        from app.pipeline.coordinate_extractor import _looks_like_address
        assert not _looks_like_address("Invoice Number: INV-9876")

    def test_empty_string(self):
        from app.pipeline.coordinate_extractor import _looks_like_address
        assert not _looks_like_address("")

    def test_none(self):
        from app.pipeline.coordinate_extractor import _looks_like_address
        assert not _looks_like_address(None)
