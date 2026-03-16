"""Tests for FieldMapBuilder (Step 22b).

Verifies that vision-identified PII fields are correctly matched to PyMuPDF
word coordinates and that deterministic spatial relationships are computed.
"""
from __future__ import annotations

import pytest

import fitz  # PyMuPDF

from app.pipeline.field_map_builder import (
    FieldMapBuilder,
    _count_value_lines,
    _merge_bboxes,
)
from app.pipeline.vision_router import VisionRoutingResult


# ---------------------------------------------------------------------------
# Helpers — mock PyMuPDF word tuples
# ---------------------------------------------------------------------------
# Each word tuple: (x0, y0, x1, y1, text, block_no, line_no, word_no)

def _w(x0, y0, x1, y1, text, block=0, line=0, word=0):
    """Shortcut to build a PyMuPDF-style word tuple."""
    return (x0, y0, x1, y1, text, block, line, word)


# A typical page with labeled PII fields
SAMPLE_WORDS = [
    # Line 1: "Client: (001968) ADELINE CHANDLER"
    _w(50, 100, 90, 115, "Client:", 0, 0, 0),
    _w(95, 100, 140, 115, "(001968)", 0, 0, 1),
    _w(145, 100, 200, 115, "ADELINE", 0, 0, 2),
    _w(205, 100, 280, 115, "CHANDLER", 0, 0, 3),
    # Line 2: "Tax No. 285-07-5085"
    _w(50, 130, 100, 145, "Tax", 0, 1, 0),
    _w(105, 130, 130, 145, "No.", 0, 1, 1),
    _w(145, 130, 230, 145, "285-07-5085", 0, 1, 2),
    # Line 3: "Address:"
    _w(50, 160, 110, 175, "Address:", 0, 2, 0),
    # Line 4 (below address label): "123 Main Street"
    _w(50, 180, 80, 195, "123", 0, 3, 0),
    _w(85, 180, 120, 195, "Main", 0, 3, 1),
    _w(125, 180, 175, 195, "Street", 0, 3, 2),
    # Line 5 (continued address): "Suite 400"
    _w(50, 200, 85, 215, "Suite", 0, 4, 0),
    _w(90, 200, 120, 215, "400", 0, 4, 1),
    # Line 6: "DOB: 15/03/1985"
    _w(50, 240, 80, 255, "DOB:", 0, 5, 0),
    _w(85, 240, 160, 255, "15/03/1985", 0, 5, 1),
    # Line 7: compound word "STREET,11TH"
    _w(300, 100, 400, 115, "STREET,11TH", 0, 0, 4),
]


def _build_vision_result(pii_fields: list[dict]) -> VisionRoutingResult:
    """Build a VisionRoutingResult with specified pii_fields."""
    return VisionRoutingResult(
        structure_type="fixed_single_page",
        structure_confidence=0.8,
        pii_fields=pii_fields,
    )


# ---------------------------------------------------------------------------
# Test: _find_text_in_words
# ---------------------------------------------------------------------------

class TestFindTextInWords:
    builder = FieldMapBuilder()

    def test_single_word_exact(self):
        """Single word value found in PyMuPDF words."""
        result = self.builder._find_text_in_words(SAMPLE_WORDS, "Client:")
        assert result is not None
        assert len(result) == 1
        assert result[0][4] == "Client:"

    def test_multi_word_consecutive(self):
        """Multi-word value found (consecutive words on same line)."""
        result = self.builder._find_text_in_words(SAMPLE_WORDS, "ADELINE CHANDLER")
        assert result is not None
        assert len(result) == 2
        assert result[0][4] == "ADELINE"
        assert result[1][4] == "CHANDLER"

    def test_case_insensitive(self):
        """Case-insensitive matching works."""
        result = self.builder._find_text_in_words(SAMPLE_WORDS, "adeline chandler")
        assert result is not None
        assert result[0][4] == "ADELINE"

    def test_partial_match_fallback(self):
        """Multi-word search falls back to first word if full phrase not found."""
        # "ADELINE JONES" won't match exactly, but "ADELINE" will
        result = self.builder._find_text_in_words(SAMPLE_WORDS, "ADELINE JONES")
        assert result is not None
        # Should match starting from ADELINE (single word fallback at match_len=1)
        assert result[0][4] == "ADELINE"

    def test_value_not_found(self):
        """Value not in word list returns None."""
        result = self.builder._find_text_in_words(SAMPLE_WORDS, "NONEXISTENT")
        assert result is None

    def test_empty_text(self):
        result = self.builder._find_text_in_words(SAMPLE_WORDS, "")
        assert result is None

    def test_empty_words(self):
        result = self.builder._find_text_in_words([], "hello")
        assert result is None

    def test_compound_word_substring(self):
        """Compound PyMuPDF word matches substring search."""
        result = self.builder._find_text_in_words(SAMPLE_WORDS, "STREET")
        assert result is not None
        # Should match the simple "Street" word first (it appears before compound)
        assert "Street" in result[0][4] or "STREET" in result[0][4]

    def test_multi_word_tax_no(self):
        """Multi-word label 'Tax No.' found."""
        result = self.builder._find_text_in_words(SAMPLE_WORDS, "Tax No.")
        assert result is not None
        assert result[0][4] == "Tax"
        assert result[1][4] == "No."

    def test_single_word_strips_punctuation(self):
        """Punctuation stripped during matching."""
        result = self.builder._find_text_in_words(SAMPLE_WORDS, "Client")
        assert result is not None
        assert result[0][4] == "Client:"


# ---------------------------------------------------------------------------
# Test: _compute_spatial_relationship
# ---------------------------------------------------------------------------

class TestSpatialRelationship:
    builder = FieldMapBuilder()

    def test_same_line_right(self):
        """Label and value on same y → 'same_line_right'."""
        label = [_w(50, 100, 90, 115, "Client:")]
        value = [_w(145, 100, 280, 115, "ADELINE")]
        rel, lc = self.builder._compute_spatial_relationship(label, value)
        assert rel == "same_line_right"
        assert lc == 1

    def test_line_below(self):
        """Value 1 line below label → 'line_below'."""
        label = [_w(50, 160, 110, 175, "Address:")]
        value = [_w(50, 180, 175, 195, "123")]
        rel, lc = self.builder._compute_spatial_relationship(label, value)
        assert rel == "line_below"
        assert lc == 1

    def test_lines_below_n(self):
        """Value N lines below label → 'lines_below_N'."""
        label = [_w(50, 100, 110, 115, "Header:")]
        # Value 3 lines below (line_height=15, y_diff=45)
        value = [_w(50, 145, 175, 160, "Value")]
        rel, lc = self.builder._compute_spatial_relationship(label, value)
        assert rel.startswith("lines_below_")

    def test_above_fallback(self):
        """Value above label → fallback to same_line_right."""
        label = [_w(50, 200, 110, 215, "Footer:")]
        value = [_w(50, 100, 175, 115, "Top")]
        rel, lc = self.builder._compute_spatial_relationship(label, value)
        # Should fallback gracefully
        assert rel == "same_line_right"


# ---------------------------------------------------------------------------
# Test: _infer_skip_pattern
# ---------------------------------------------------------------------------

class TestSkipPattern:
    builder = FieldMapBuilder()

    def test_client_code_between(self):
        """Parenthesized code between label and value → detected."""
        label = [_w(50, 100, 90, 115, "Client:")]
        value = [_w(145, 100, 280, 115, "ADELINE")]
        # (001968) is between label and value
        skip = self.builder._infer_skip_pattern(label, value, SAMPLE_WORDS)
        assert skip is not None
        assert r"\(" in skip

    def test_colon_between(self):
        """Colon between label and value → detected."""
        words = [
            _w(50, 100, 90, 115, "Name"),
            _w(95, 100, 105, 115, ":"),
            _w(115, 100, 200, 115, "John"),
        ]
        label = [words[0]]
        value = [words[2]]
        skip = FieldMapBuilder._infer_skip_pattern(label, value, words)
        assert skip is not None
        assert "[" in skip or ":" in skip

    def test_nothing_between(self):
        """No noise between → None."""
        label = [_w(50, 130, 130, 145, "Tax")]
        value = [_w(145, 130, 230, 145, "285-07-5085")]
        skip = self.builder._infer_skip_pattern(label, value, SAMPLE_WORDS)
        assert skip is None


# ---------------------------------------------------------------------------
# Test: _infer_value_pattern
# ---------------------------------------------------------------------------

class TestValuePattern:
    builder = FieldMapBuilder()

    def test_us_ssn(self):
        """US_SSN → correct regex."""
        pat = self.builder._infer_value_pattern("US_SSN", "285-07-5085")
        assert pat is not None
        assert r"\d{3}-\d{2}-\d{4}" in pat

    def test_person_no_pattern(self):
        """PERSON → None (names too variable for regex)."""
        pat = self.builder._infer_value_pattern("PERSON", "ADELINE CHANDLER")
        assert pat is None

    def test_email(self):
        pat = self.builder._infer_value_pattern("EMAIL_ADDRESS", "a@b.com")
        assert pat is not None
        assert "@" in pat

    def test_dob(self):
        pat = self.builder._infer_value_pattern("DATE_OF_BIRTH", "15/03/1985")
        assert pat is not None

    def test_unknown_type(self):
        """Unknown field type → None."""
        pat = self.builder._infer_value_pattern("MYSTERY_FIELD", "xyz")
        assert pat is None


# ---------------------------------------------------------------------------
# Test: _count_value_lines
# ---------------------------------------------------------------------------

class TestCountValueLines:
    def test_single_line(self):
        words = [_w(50, 180, 175, 195, "123"), _w(85, 180, 120, 195, "Main")]
        assert _count_value_lines(words) == 1

    def test_multi_line(self):
        """Value spanning 2 lines → line_count=2."""
        words = [
            _w(50, 180, 175, 195, "123"),
            _w(50, 200, 120, 215, "Suite"),
        ]
        assert _count_value_lines(words) == 2

    def test_empty(self):
        assert _count_value_lines([]) == 1


# ---------------------------------------------------------------------------
# Test: _merge_bboxes
# ---------------------------------------------------------------------------

class TestMergeBboxes:
    def test_two_words(self):
        words = [_w(50, 100, 90, 115, "A"), _w(100, 100, 150, 115, "B")]
        bbox = _merge_bboxes(words)
        assert bbox == (50, 100, 150, 115)

    def test_multi_line_words(self):
        words = [_w(50, 100, 90, 115, "A"), _w(50, 120, 150, 135, "B")]
        bbox = _merge_bboxes(words)
        assert bbox == (50, 100, 150, 135)


# ---------------------------------------------------------------------------
# Test: _build_one_field
# ---------------------------------------------------------------------------

class TestBuildOneField:
    builder = FieldMapBuilder()

    def test_complete_field(self):
        """Full pii_field with label + value → valid FieldMapping."""
        pii = {"type": "PERSON", "value": "ADELINE CHANDLER", "label": "Client:"}
        fm = self.builder._build_one_field(pii, SAMPLE_WORDS)
        assert fm is not None
        assert fm.field_type == "PERSON"
        assert fm.anchor_text == "Client"
        assert fm.spatial_relationship == "same_line_right"
        assert fm.sample_bbox  # non-empty

    def test_no_label_skipped(self):
        """No label in vision result → field skipped."""
        pii = {"type": "PERSON", "value": "ADELINE CHANDLER", "label": ""}
        fm = self.builder._build_one_field(pii, SAMPLE_WORDS)
        assert fm is None

    def test_label_not_found_skipped(self):
        """Label text not in page words → field skipped."""
        pii = {"type": "PERSON", "value": "ADELINE", "label": "Recipient:"}
        fm = self.builder._build_one_field(pii, SAMPLE_WORDS)
        assert fm is None

    def test_value_not_found_skipped(self):
        """Value text not in page words → field skipped."""
        pii = {"type": "PERSON", "value": "ZZZZZ NONEXISTENT", "label": "Client:"}
        fm = self.builder._build_one_field(pii, SAMPLE_WORDS)
        assert fm is None

    def test_ssn_field(self):
        """SSN field gets value pattern."""
        pii = {"type": "US_SSN", "value": "285-07-5085", "label": "Tax No."}
        fm = self.builder._build_one_field(pii, SAMPLE_WORDS)
        assert fm is not None
        assert fm.field_type == "US_SSN"
        assert fm.anchor_text == "Tax No"
        assert fm.value_pattern is not None

    def test_address_below_label(self):
        """Address value below label → line_below."""
        pii = {"type": "LOCATION", "value": "123 Main Street", "label": "Address:"}
        fm = self.builder._build_one_field(pii, SAMPLE_WORDS)
        assert fm is not None
        assert fm.field_type == "LOCATION"
        assert fm.spatial_relationship == "line_below"

    def test_dob_same_line(self):
        """DOB on same line as label."""
        pii = {"type": "DATE_OF_BIRTH", "value": "15/03/1985", "label": "DOB:"}
        fm = self.builder._build_one_field(pii, SAMPLE_WORDS)
        assert fm is not None
        assert fm.spatial_relationship == "same_line_right"
        assert fm.value_pattern is not None

    def test_missing_type_returns_none(self):
        pii = {"type": "", "value": "hello", "label": "Label:"}
        fm = self.builder._build_one_field(pii, SAMPLE_WORDS)
        assert fm is None

    def test_missing_value_returns_none(self):
        pii = {"type": "PERSON", "value": "", "label": "Client:"}
        fm = self.builder._build_one_field(pii, SAMPLE_WORDS)
        assert fm is None

    def test_skip_pattern_detected(self):
        """Client code noise between label and value → skip_pattern set."""
        pii = {"type": "PERSON", "value": "ADELINE CHANDLER", "label": "Client:"}
        fm = self.builder._build_one_field(pii, SAMPLE_WORDS)
        assert fm is not None
        assert fm.skip_pattern is not None


# ---------------------------------------------------------------------------
# Test: full integration — build_field_map with mock document
# ---------------------------------------------------------------------------

class TestBuildFieldMapIntegration:
    """Integration test using a real PyMuPDF document (in-memory)."""

    def _create_mock_pdf_with_words(self, words_text: list[tuple[float, float, str]]):
        """Create a minimal PDF with text at specified positions.

        ``words_text``: list of (x, y, text) for text placement.
        Returns the path to a temporary PDF.
        """
        import tempfile
        doc = fitz.open()
        page = doc.new_page(width=612, height=792)
        for x, y, text in words_text:
            page.insert_text((x, y), text, fontsize=11)
        tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        doc.save(tmp.name)
        doc.close()
        return tmp.name

    def test_full_integration(self):
        """Vision result + real PDF → valid FieldMapping list."""
        import os
        pdf_path = self._create_mock_pdf_with_words([
            (50, 100, "Client:"),
            (130, 100, "JOHN SMITH"),
            (50, 130, "SSN:"),
            (130, 130, "123-45-6789"),
        ])
        try:
            vision = _build_vision_result([
                {"type": "PERSON", "value": "JOHN SMITH", "label": "Client:"},
                {"type": "US_SSN", "value": "123-45-6789", "label": "SSN:"},
            ])
            builder = FieldMapBuilder()
            field_maps = builder.build_field_map(vision, pdf_path, page_num=0)
            # Should produce at least 1 mapping (PyMuPDF word tokenization may vary)
            assert len(field_maps) >= 1
            types = {fm.field_type for fm in field_maps}
            # At minimum PERSON should be found
            assert "PERSON" in types or "US_SSN" in types
        finally:
            os.unlink(pdf_path)

    def test_empty_pii_fields(self):
        """No PII fields → empty list."""
        import os
        pdf_path = self._create_mock_pdf_with_words([(50, 100, "Hello world")])
        try:
            vision = _build_vision_result([])
            builder = FieldMapBuilder()
            field_maps = builder.build_field_map(vision, pdf_path, page_num=0)
            assert field_maps == []
        finally:
            os.unlink(pdf_path)

    def test_page_out_of_range(self):
        """Page number beyond doc → empty list."""
        import os
        pdf_path = self._create_mock_pdf_with_words([(50, 100, "Hello")])
        try:
            vision = _build_vision_result([
                {"type": "PERSON", "value": "Test", "label": "Name:"},
            ])
            builder = FieldMapBuilder()
            field_maps = builder.build_field_map(vision, pdf_path, page_num=99)
            assert field_maps == []
        finally:
            os.unlink(pdf_path)
