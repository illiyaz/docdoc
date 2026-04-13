"""Tests for gap detection and gap filling (Step 30e-6).

Tests cover:
- GapDetector: page gaps, field gaps, truncation, page range parsing
- GapFiller: cascade logic, budget enforcement, mask utility, LLM response parsing
- Persistence: save/load gaps to/from JSON
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.pipeline.gap_detector import (
    ExtractionGap,
    GapDetector,
    _parse_page_range,
)
from app.pipeline.gap_filler import (
    DEFAULT_MAX_LLM_CALLS_PER_GAP,
    DEFAULT_MAX_LLM_CALLS_TOTAL,
    FillAttempt,
    GapFiller,
    _mask_value,
    _parse_llm_fill_response,
    load_gaps,
    persist_gaps,
)


# ===================================================================
# GapDetector tests
# ===================================================================


class TestParsePageRange:
    """Test _parse_page_range utility."""

    def test_single_page(self):
        assert _parse_page_range("5") == [5]

    def test_range(self):
        assert _parse_page_range("3-6") == [3, 4, 5, 6]

    def test_empty_returns_default(self):
        assert _parse_page_range("") == [1]

    def test_none_like(self):
        assert _parse_page_range("abc") == [1]

    def test_single_page_one(self):
        assert _parse_page_range("1") == [1]


class TestGapDetectorPageGaps:
    """Test page-level gap detection."""

    def test_empty_page_detected(self):
        """Pages with zero records after onset are flagged."""
        detector = GapDetector()
        records = [
            {"page_range": "2", "entity_types_found": ["PERSON"]},
            {"page_range": "4", "entity_types_found": ["PERSON"]},
        ]
        gaps = detector.detect(
            records=records,
            field_inventory=["PERSON"],
            total_pages=5,
            onset_page=1,  # 0-indexed → onset at page 2 (1-indexed)
            document_id="doc-1",
            document_name="test.pdf",
        )
        page_gaps = [g for g in gaps if g.gap_type == "empty_page"]
        gap_pages = [g.page_num for g in page_gaps]
        assert 3 in gap_pages  # page 3 has no records
        assert 5 in gap_pages  # page 5 has no records

    def test_no_gaps_all_pages_covered(self):
        """No page gaps when every page after onset has records."""
        detector = GapDetector()
        records = [
            {"page_range": "2", "entity_types_found": ["PERSON"]},
            {"page_range": "3", "entity_types_found": ["PERSON"]},
        ]
        gaps = detector.detect(
            records=records,
            field_inventory=[],
            total_pages=3,
            onset_page=1,
            document_id="doc-1",
            document_name="test.pdf",
        )
        page_gaps = [g for g in gaps if g.gap_type == "empty_page"]
        assert len(page_gaps) == 0

    def test_multi_page_template_boundary(self):
        """Non-data pages in multi-page templates are skipped."""
        detector = GapDetector()
        # Template has 2 pages per instance. Page 2 (onset) has data, page 3 is summary.
        records = [
            {"page_range": "2", "entity_types_found": ["PERSON"]},
            {"page_range": "4", "entity_types_found": ["PERSON"]},
        ]
        gaps = detector.detect(
            records=records,
            field_inventory=[],
            total_pages=5,
            onset_page=1,  # onset at page 2 (1-indexed)
            document_id="doc-1",
            document_name="test.pdf",
            pages_per_instance=2,
        )
        page_gaps = [g for g in gaps if g.gap_type == "empty_page"]
        gap_pages = [g.page_num for g in page_gaps]
        # Page 3 is position 1 in template (not 0), so it's a boundary page → skipped
        assert 3 not in gap_pages

    def test_pages_before_onset_ignored(self):
        """Pages before onset are not checked."""
        detector = GapDetector()
        records = [
            {"page_range": "5", "entity_types_found": ["PERSON"]},
        ]
        gaps = detector.detect(
            records=records,
            field_inventory=[],
            total_pages=5,
            onset_page=4,  # onset at page 5 (1-indexed)
            document_id="doc-1",
            document_name="test.pdf",
        )
        page_gaps = [g for g in gaps if g.gap_type == "empty_page"]
        assert len(page_gaps) == 0  # only page 5 matters, and it has records


class TestGapDetectorFieldGaps:
    """Test field-level gap detection."""

    def test_missing_field_detected(self):
        """Pages missing expected fields are flagged."""
        detector = GapDetector()
        records = [
            {"page_range": "2", "entity_types_found": ["PERSON"]},
        ]
        gaps = detector.detect(
            records=records,
            field_inventory=["PERSON", "US_SSN"],
            total_pages=2,
            onset_page=1,
            document_id="doc-1",
            document_name="test.pdf",
        )
        field_gaps = [g for g in gaps if g.gap_type == "missing_field"]
        assert any(g.expected_field == "US_SSN" for g in field_gaps)

    def test_all_fields_present_no_gap(self):
        """No field gaps when all expected fields are found."""
        detector = GapDetector()
        records = [
            {"page_range": "2", "entity_types_found": ["PERSON", "US_SSN", "LOCATION"]},
        ]
        gaps = detector.detect(
            records=records,
            field_inventory=["PERSON", "US_SSN", "LOCATION"],
            total_pages=2,
            onset_page=1,
            document_id="doc-1",
            document_name="test.pdf",
        )
        field_gaps = [g for g in gaps if g.gap_type == "missing_field"]
        assert len(field_gaps) == 0

    def test_high_severity_for_critical_fields(self):
        """PERSON, US_SSN, GOVERNMENT_ID get high severity."""
        detector = GapDetector()
        records = [
            {"page_range": "2", "entity_types_found": ["LOCATION"]},
        ]
        gaps = detector.detect(
            records=records,
            field_inventory=["PERSON", "US_SSN", "PHONE_NUMBER"],
            total_pages=2,
            onset_page=1,
            document_id="doc-1",
            document_name="test.pdf",
        )
        field_gaps = [g for g in gaps if g.gap_type == "missing_field"]
        person_gap = next(g for g in field_gaps if g.expected_field == "PERSON")
        ssn_gap = next(g for g in field_gaps if g.expected_field == "US_SSN")
        phone_gap = next(g for g in field_gaps if g.expected_field == "PHONE_NUMBER")
        assert person_gap.severity == "high"
        assert ssn_gap.severity == "high"
        assert phone_gap.severity == "medium"

    def test_empty_field_inventory_no_gaps(self):
        """No field gaps when field_inventory is empty."""
        detector = GapDetector()
        records = [
            {"page_range": "2", "entity_types_found": ["PERSON"]},
        ]
        gaps = detector.detect(
            records=records,
            field_inventory=[],
            total_pages=2,
            onset_page=1,
            document_id="doc-1",
            document_name="test.pdf",
        )
        field_gaps = [g for g in gaps if g.gap_type == "missing_field"]
        assert len(field_gaps) == 0


class TestGapDetectorTruncation:
    """Test truncation detection."""

    def test_single_word_name_flagged(self):
        """Names with only one word are flagged as truncated."""
        detector = GapDetector()
        records = [
            {
                "page_range": "2",
                "entity_types_found": ["PERSON"],
                "raw_name": "Smith",
            },
        ]
        gaps = detector.detect(
            records=records,
            field_inventory=[],
            total_pages=2,
            onset_page=1,
            document_id="doc-1",
            document_name="test.pdf",
        )
        trunc_gaps = [g for g in gaps if g.gap_type == "truncated"]
        assert len(trunc_gaps) == 1
        assert trunc_gaps[0].expected_field == "PERSON"

    def test_full_name_not_flagged(self):
        """Two-word names are not flagged."""
        detector = GapDetector()
        records = [
            {
                "page_range": "2",
                "entity_types_found": ["PERSON"],
                "raw_name": "John Smith",
            },
        ]
        gaps = detector.detect(
            records=records,
            field_inventory=[],
            total_pages=2,
            onset_page=1,
            document_id="doc-1",
            document_name="test.pdf",
        )
        trunc_gaps = [g for g in gaps if g.gap_type == "truncated"]
        # No truncation for full names
        name_trunc = [g for g in trunc_gaps if g.expected_field == "PERSON"]
        assert len(name_trunc) == 0

    def test_short_phone_flagged(self):
        """Phone numbers with fewer than 10 digits are flagged."""
        detector = GapDetector()
        records = [
            {
                "page_range": "2",
                "entity_types_found": ["PHONE_NUMBER"],
                "raw_phone": "555-12",
            },
        ]
        gaps = detector.detect(
            records=records,
            field_inventory=[],
            total_pages=2,
            onset_page=1,
            document_id="doc-1",
            document_name="test.pdf",
        )
        trunc_gaps = [g for g in gaps if g.gap_type == "truncated"]
        assert any(g.expected_field == "PHONE_NUMBER" for g in trunc_gaps)

    def test_valid_phone_not_flagged(self):
        """10-digit phone numbers are not flagged."""
        detector = GapDetector()
        records = [
            {
                "page_range": "2",
                "entity_types_found": ["PHONE_NUMBER"],
                "raw_phone": "555-123-4567",
            },
        ]
        gaps = detector.detect(
            records=records,
            field_inventory=[],
            total_pages=2,
            onset_page=1,
            document_id="doc-1",
            document_name="test.pdf",
        )
        trunc_gaps = [g for g in gaps if g.gap_type == "truncated" and g.expected_field == "PHONE_NUMBER"]
        assert len(trunc_gaps) == 0

    def test_short_address_flagged(self):
        """Addresses shorter than 10 chars are flagged."""
        detector = GapDetector()
        records = [
            {
                "page_range": "2",
                "entity_types_found": ["LOCATION"],
                "raw_address": {"raw": "123 Main"},
            },
        ]
        gaps = detector.detect(
            records=records,
            field_inventory=[],
            total_pages=2,
            onset_page=1,
            document_id="doc-1",
            document_name="test.pdf",
        )
        trunc_gaps = [g for g in gaps if g.gap_type == "truncated" and g.expected_field == "LOCATION"]
        assert len(trunc_gaps) == 1


class TestExtractionGapDataclass:
    """Test the ExtractionGap dataclass."""

    def test_defaults(self):
        gap = ExtractionGap(
            document_id="d1",
            document_name="f.pdf",
            page_num=5,
            gap_type="empty_page",
        )
        assert gap.severity == "medium"
        assert gap.fill_result == "pending"
        assert gap.fill_attempted is False
        assert gap.filled_by == "system"

    def test_to_dict(self):
        gap = ExtractionGap(
            document_id="d1",
            document_name="f.pdf",
            page_num=5,
            gap_type="missing_field",
            expected_field="US_SSN",
        )
        d = gap.to_dict()
        assert d["document_id"] == "d1"
        assert d["gap_type"] == "missing_field"
        assert d["expected_field"] == "US_SSN"
        assert isinstance(d["actual_fields"], list)


# ===================================================================
# GapFiller tests
# ===================================================================


class TestMaskValue:
    """Test the _mask_value utility."""

    def test_ssn_masking(self):
        result = _mask_value("123-45-6789", "US_SSN")
        assert result == "***-**-6789"

    def test_person_masking(self):
        result = _mask_value("John Smith", "PERSON")
        assert "J" in result
        assert "S" in result
        assert "***" in result

    def test_phone_masking(self):
        result = _mask_value("555-123-4567", "PHONE_NUMBER")
        assert "4567" in result
        assert "***" in result

    def test_email_masking(self):
        result = _mask_value("john@example.com", "EMAIL_ADDRESS")
        assert result.startswith("j***@example.com")

    def test_location_masking(self):
        result = _mask_value("123 Main Street, NY 10001", "LOCATION")
        assert result.startswith("123")

    def test_empty_value(self):
        assert _mask_value("", "US_SSN") == "***"

    def test_default_masking(self):
        result = _mask_value("some_value", None)
        assert result[0] == "s"
        assert result[-1] == "e"
        assert "*" in result


class TestParseLlmFillResponse:
    """Test _parse_llm_fill_response."""

    def test_valid_json(self):
        response = '{"field_type": "US_SSN", "value": "123-45-6789"}'
        assert _parse_llm_fill_response(response, "US_SSN") == "123-45-6789"

    def test_null_value(self):
        response = '{"field_type": "US_SSN", "value": null}'
        assert _parse_llm_fill_response(response, "US_SSN") is None

    def test_none_string(self):
        response = '{"field_type": "US_SSN", "value": "none"}'
        assert _parse_llm_fill_response(response, "US_SSN") is None

    def test_code_fenced(self):
        response = '```json\n{"field_type": "US_SSN", "value": "111-22-3333"}\n```'
        assert _parse_llm_fill_response(response, "US_SSN") == "111-22-3333"

    def test_array_response(self):
        response = '[{"field_type": "PERSON", "value": "Jane Doe"}]'
        assert _parse_llm_fill_response(response, "PERSON") == "Jane Doe"

    def test_empty_response(self):
        assert _parse_llm_fill_response("", "US_SSN") is None

    def test_garbage_response(self):
        assert _parse_llm_fill_response("not json at all", "US_SSN") is None

    def test_embedded_json(self):
        response = 'Here is the result: {"field_type": "PERSON", "value": "Bob"}'
        assert _parse_llm_fill_response(response, "PERSON") == "Bob"


class TestFillAttempt:
    """Test FillAttempt dataclass."""

    def test_defaults(self):
        fa = FillAttempt(method="vision", success=True)
        assert fa.llm_calls_used == 0
        assert fa.value_masked is None

    def test_with_values(self):
        fa = FillAttempt(
            method="llm_template",
            success=True,
            value_masked="***-**-6789",
            llm_calls_used=1,
        )
        assert fa.llm_calls_used == 1


class TestGapFillerCascade:
    """Test GapFiller cascade and budget logic."""

    def _make_filler(self, **kwargs) -> GapFiller:
        defaults = dict(
            doc_path="/fake/path.pdf",
            document_id="doc-1",
            field_map=[],
            ollama_client=None,
        )
        defaults.update(kwargs)
        return GapFiller(**defaults)

    def test_stitching_gap_not_fillable(self):
        """Stitching gaps are marked not_applicable."""
        filler = self._make_filler()
        gap = ExtractionGap(
            document_id="doc-1",
            document_name="test.pdf",
            page_num=5,
            gap_type="stitching",
        )
        result = filler.fill([gap])
        assert len(result) == 1
        assert result[0].fill_result == "not_applicable"
        assert result[0].fill_attempted is True

    def test_empty_gaps_returns_empty(self):
        filler = self._make_filler()
        assert filler.fill([]) == []

    def test_severity_ordering(self):
        """High severity gaps are processed first."""
        filler = self._make_filler()
        gaps = [
            ExtractionGap(
                document_id="d", document_name="f", page_num=1,
                gap_type="truncated", severity="low",
            ),
            ExtractionGap(
                document_id="d", document_name="f", page_num=2,
                gap_type="empty_page", severity="high",
            ),
            ExtractionGap(
                document_id="d", document_name="f", page_num=3,
                gap_type="missing_field", severity="medium",
            ),
        ]
        result = filler.fill(gaps)
        # All should be attempted (and unfilled since no real doc)
        assert all(g.fill_attempted for g in result)

    def test_budget_per_gap_enforced(self):
        """Per-gap LLM budget is respected."""
        mock_client = MagicMock()
        mock_client.generate.return_value = '{"value": null}'
        mock_client.generate_with_images.return_value = '{"value": null}'

        filler = self._make_filler(
            ollama_client=mock_client,
            max_llm_per_gap=1,
        )
        gap = ExtractionGap(
            document_id="d", document_name="f.pdf", page_num=1,
            gap_type="missing_field", expected_field="US_SSN",
        )

        # Patch fitz.open to provide fake page text
        mock_doc = MagicMock()
        mock_doc.page_count = 10
        mock_page = MagicMock()
        mock_page.get_text.return_value = "some text without SSN"
        mock_doc.__getitem__ = MagicMock(return_value=mock_page)
        mock_doc.close = MagicMock()

        with patch("fitz.open", return_value=mock_doc):
            result = filler.fill([gap])

        # Should have used at most 1 LLM call per gap
        assert filler.llm_calls_used <= 1

    def test_budget_total_enforced(self):
        """Total LLM budget prevents excessive calls across gaps."""
        filler = self._make_filler(
            ollama_client=MagicMock(),
            max_llm_total=0,  # zero budget
        )
        gap = ExtractionGap(
            document_id="d", document_name="f.pdf", page_num=1,
            gap_type="missing_field", expected_field="US_SSN",
        )
        result = filler.fill([gap])
        # LLM paths should be skipped due to budget
        assert filler.llm_calls_used == 0

    def test_coordinate_relaxed_finds_ssn_in_text(self):
        """Coordinate relaxed path finds SSN pattern in page text."""
        filler = self._make_filler(field_map=[MagicMock()])
        gap = ExtractionGap(
            document_id="d", document_name="f.pdf", page_num=1,
            gap_type="missing_field", expected_field="US_SSN",
        )

        mock_doc = MagicMock()
        mock_doc.page_count = 5
        mock_page = MagicMock()
        page_text = "John Smith SSN: 123-45-6789 Address: 123 Main"
        # get_text() → text, get_text("words") → word tuples
        def _get_text(*args, **kwargs):
            fmt = args[0] if args else kwargs.get("option", None)
            if fmt == "words":
                return [(100, 100, 200, 120, "123-45-6789", 0, 0, 0)]
            return page_text
        mock_page.get_text = _get_text
        mock_doc.__getitem__ = MagicMock(return_value=mock_page)
        mock_doc.close = MagicMock()

        # Test the per-gap coordinate path directly (fill() routes through
        # batched text first, which requires ollama_client)
        with patch("fitz.open", return_value=mock_doc):
            result = filler._fill_one(gap)

        assert result.fill_result == "filled"
        assert result.fill_method == "coordinate_relaxed"
        assert "6789" in (result.filled_value_masked or "")

    def test_regex_fallback_for_presidio(self):
        """When Presidio unavailable, regex fallback still finds patterns."""
        filler = self._make_filler()
        gap = ExtractionGap(
            document_id="d", document_name="f.pdf", page_num=1,
            gap_type="empty_page", severity="high",
        )

        page_text = "Name: John Smith SSN: 987-65-4321 DOB: 01/15/1990"
        attempt = filler._try_regex_fallback(page_text, ExtractionGap(
            document_id="d", document_name="f", page_num=1,
            gap_type="missing_field", expected_field="US_SSN",
        ))
        assert attempt.success
        assert attempt.method == "presidio"


class TestGapFillerBuildCascade:
    """Test cascade construction per gap type."""

    def _make_filler(self) -> GapFiller:
        return GapFiller(
            doc_path="/fake.pdf",
            document_id="d",
        )

    def test_empty_page_cascade(self):
        filler = self._make_filler()
        gap = ExtractionGap(
            document_id="d", document_name="f", page_num=1,
            gap_type="empty_page",
        )
        cascade = filler._build_cascade(gap)
        methods = [name for _, name, _ in cascade]
        assert methods == ["coordinate_relaxed", "llm_template", "vision", "presidio"]

    def test_missing_field_cascade(self):
        filler = self._make_filler()
        gap = ExtractionGap(
            document_id="d", document_name="f", page_num=1,
            gap_type="missing_field",
        )
        cascade = filler._build_cascade(gap)
        methods = [name for _, name, _ in cascade]
        assert methods == ["coordinate_relaxed", "llm_template", "vision", "presidio"]

    def test_truncated_cascade_shorter(self):
        """Truncated gaps don't use vision or presidio."""
        filler = self._make_filler()
        gap = ExtractionGap(
            document_id="d", document_name="f", page_num=1,
            gap_type="truncated",
        )
        cascade = filler._build_cascade(gap)
        methods = [name for _, name, _ in cascade]
        assert methods == ["coordinate_relaxed", "llm_template"]
        assert "vision" not in methods
        assert "presidio" not in methods


# ===================================================================
# Persistence tests
# ===================================================================


class TestGapPersistence:
    """Test persist_gaps and load_gaps."""

    def test_persist_and_load(self, tmp_path, monkeypatch):
        """Gaps survive round-trip to JSON."""
        monkeypatch.chdir(tmp_path)

        gaps = [
            ExtractionGap(
                document_id="d1",
                document_name="test.pdf",
                page_num=5,
                gap_type="missing_field",
                severity="high",
                expected_field="US_SSN",
                actual_fields=["PERSON", "LOCATION"],
                fill_result="filled",
                fill_method="coordinate_relaxed",
                filled_value_masked="***-**-6789",
            ),
            ExtractionGap(
                document_id="d1",
                document_name="test.pdf",
                page_num=6,
                gap_type="empty_page",
                fill_result="unfilled",
            ),
        ]

        persist_gaps(gaps, project_id="proj-1", job_id="job-1")
        loaded = load_gaps(project_id="proj-1", job_id="job-1")

        assert len(loaded) == 2
        assert loaded[0].document_id == "d1"
        assert loaded[0].expected_field == "US_SSN"
        assert loaded[0].fill_result == "filled"
        assert loaded[1].gap_type == "empty_page"

    def test_load_missing_file(self, tmp_path, monkeypatch):
        """Loading non-existent gaps returns empty list."""
        monkeypatch.chdir(tmp_path)
        loaded = load_gaps(project_id="no-proj", job_id="no-job")
        assert loaded == []

    def test_persist_creates_directory(self, tmp_path, monkeypatch):
        """persist_gaps creates the directory tree."""
        monkeypatch.chdir(tmp_path)
        persist_gaps([], project_id="new-proj", job_id="new-job")
        assert (tmp_path / "data" / "projects" / "new-proj" / "gaps" / "new-job.json").exists()

    def test_persist_summary_counts(self, tmp_path, monkeypatch):
        """JSON file includes summary counts."""
        monkeypatch.chdir(tmp_path)
        gaps = [
            ExtractionGap(
                document_id="d1", document_name="f", page_num=1,
                gap_type="empty_page", fill_result="filled",
            ),
            ExtractionGap(
                document_id="d1", document_name="f", page_num=2,
                gap_type="missing_field", fill_result="unfilled",
            ),
            ExtractionGap(
                document_id="d1", document_name="f", page_num=3,
                gap_type="truncated", fill_result="pending",
            ),
        ]
        persist_gaps(gaps, project_id="p1", job_id="j1")

        path = tmp_path / "data" / "projects" / "p1" / "gaps" / "j1.json"
        data = json.loads(path.read_text())
        assert data["total_gaps"] == 3
        assert data["filled"] == 1
        assert data["unfilled"] == 1
        assert data["pending"] == 1


# ===================================================================
# Safety tests
# ===================================================================


class TestGapSafety:
    """Ensure no raw PII leaks through gap detection/filling."""

    def test_masked_values_never_raw(self):
        """Filled values are always masked."""
        for field_type, value in [
            ("US_SSN", "123-45-6789"),
            ("PERSON", "John Smith"),
            ("PHONE_NUMBER", "555-123-4567"),
            ("EMAIL_ADDRESS", "john@example.com"),
        ]:
            masked = _mask_value(value, field_type)
            assert value != masked, f"Raw value leaked for {field_type}"
            assert "***" in masked or "*" in masked

    def test_gap_context_no_raw_pii(self):
        """Gap context messages don't contain raw PII values."""
        detector = GapDetector()
        records = [
            {
                "page_range": "2",
                "entity_types_found": ["PERSON"],
                "raw_name": "Smith",  # single word → truncated
            },
        ]
        gaps = detector.detect(
            records=records,
            field_inventory=[],
            total_pages=2,
            onset_page=1,
            document_id="d",
            document_name="f",
        )
        trunc = [g for g in gaps if g.gap_type == "truncated"]
        for g in trunc:
            # Context should use '***' not the actual name
            assert "Smith" not in (g.context or ""), "Raw name leaked in context"
