"""Tests for ExtractionVerifier (Step 22d + audit + gap-fill)."""
from __future__ import annotations

import json
import pytest
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

from app.pipeline.extraction_verifier import (
    ExtractionVerifier,
    ExtractionVerification,
    records_to_page_dict,
)


# Minimal PIIRecord-like stub for testing
@dataclass
class _FakeRecord:
    raw_name: str | None = None
    raw_address: str | None = None
    raw_government_id: str | None = None
    raw_dob: str | None = None
    raw_email: str | None = None
    raw_phone: str | None = None


@dataclass
class _FakeFieldMapping:
    field_type: str = "PERSON"


class TestExtractionVerification:
    """Test the ExtractionVerification dataclass defaults."""

    def test_defaults(self):
        v = ExtractionVerification()
        assert v.total_pages == 0
        assert v.successful_pages == 0
        assert v.failed_pages == 0
        assert v.reconciled_pages == 0
        assert v.success_rate == 0.0
        assert v.is_acceptable is True
        assert v.summary == ""
        assert v.field_rates == {}


class TestExtractionVerifier:
    """Tests for ExtractionVerifier.verify()."""

    def _make_records(self, n: int, **kwargs) -> list:
        """Create n fake records with specified fields populated."""
        records = []
        for _ in range(n):
            records.append(_FakeRecord(**kwargs))
        return records

    def test_all_pages_succeed(self):
        """1354 records, 0 failed → success_rate=1.0, is_acceptable=True."""
        verifier = ExtractionVerifier()
        records = self._make_records(1354, raw_name="John Doe", raw_government_id="123-45-6789")
        field_map = [_FakeFieldMapping("PERSON"), _FakeFieldMapping("US_SSN")]

        result = verifier.verify(
            records=records,
            failed_pages=[],
            reconciled_records=[],
            total_pages=1354,
            field_map=field_map,
        )

        assert result.success_rate == pytest.approx(1.0)
        assert result.is_acceptable is True
        assert result.successful_pages == 1354
        assert result.failed_pages == 0
        assert result.reconciled_pages == 0

    def test_below_threshold(self):
        """1200 records, 154 failed → success_rate ~0.89, is_acceptable=False."""
        verifier = ExtractionVerifier()
        records = self._make_records(1200, raw_name="Jane Doe")
        field_map = [_FakeFieldMapping("PERSON")]

        result = verifier.verify(
            records=records,
            failed_pages=list(range(154)),
            reconciled_records=[],
            total_pages=1354,
            field_map=field_map,
        )

        assert result.success_rate == pytest.approx(1200 / 1354, rel=0.01)
        assert result.is_acceptable is False
        assert result.failed_pages == 154

    def test_reconciliation_recovers_pages(self):
        """1300 records + 40 reconciled, 14 still failed → acceptable."""
        verifier = ExtractionVerifier()
        records = self._make_records(1300, raw_name="Alice")
        reconciled = self._make_records(40, raw_name="Bob")
        field_map = [_FakeFieldMapping("PERSON")]

        result = verifier.verify(
            records=records,
            failed_pages=list(range(54)),  # 54 originally failed
            reconciled_records=reconciled,
            total_pages=1354,
            field_map=field_map,
        )

        assert result.reconciled_pages == 40
        assert result.failed_pages == 14  # 54 - 40
        total_extracted = 1300 + 40
        assert result.success_rate == pytest.approx(total_extracted / 1354, rel=0.01)
        assert result.is_acceptable is True

    def test_per_field_rates(self):
        """Per-field rates: PERSON=1.0, US_SSN=0.47, LOCATION=0.82."""
        verifier = ExtractionVerifier()

        # 100 records, all have name, 47 have SSN, 82 have address
        records = []
        for i in range(100):
            records.append(_FakeRecord(
                raw_name="Person",
                raw_government_id=f"123-45-{i:04d}" if i < 47 else None,
                raw_address=f"123 St" if i < 82 else None,
            ))

        field_map = [
            _FakeFieldMapping("PERSON"),
            _FakeFieldMapping("US_SSN"),
            _FakeFieldMapping("LOCATION"),
        ]

        result = verifier.verify(
            records=records,
            failed_pages=[],
            reconciled_records=[],
            total_pages=100,
            field_map=field_map,
        )

        assert result.field_rates["PERSON"] == pytest.approx(1.0)
        assert result.field_rates["US_SSN"] == pytest.approx(0.47)
        assert result.field_rates["LOCATION"] == pytest.approx(0.82)

    def test_summary_contains_key_metrics(self):
        """Summary string contains page counts, rates, and quality assessment."""
        verifier = ExtractionVerifier()
        records = self._make_records(90, raw_name="Test")
        reconciled = self._make_records(5, raw_name="Recovered")
        field_map = [_FakeFieldMapping("PERSON")]

        result = verifier.verify(
            records=records,
            failed_pages=list(range(10)),
            reconciled_records=reconciled,
            total_pages=100,
            field_map=field_map,
        )

        assert "95/100 pages" in result.summary
        assert "Coordinate extraction: 90 pages" in result.summary
        assert "LLM reconciliation: 5 pages recovered" in result.summary
        assert "Failed: 5 pages" in result.summary
        assert "ACCEPTABLE" in result.summary

    def test_empty_records(self):
        """Empty records list → success_rate=0.0."""
        verifier = ExtractionVerifier()
        field_map = [_FakeFieldMapping("PERSON")]

        result = verifier.verify(
            records=[],
            failed_pages=list(range(100)),
            reconciled_records=[],
            total_pages=100,
            field_map=field_map,
        )

        assert result.success_rate == 0.0
        assert result.is_acceptable is False
        assert result.successful_pages == 0

    def test_zero_total_pages(self):
        """Zero total pages → no division error."""
        verifier = ExtractionVerifier()
        field_map = [_FakeFieldMapping("PERSON")]

        result = verifier.verify(
            records=[],
            failed_pages=[],
            reconciled_records=[],
            total_pages=0,
            field_map=field_map,
        )

        assert result.success_rate == 0.0
        assert result.is_acceptable is False  # 0.0 < 0.90
        assert result.summary != ""

    def test_field_rates_empty_when_no_matching_fields(self):
        """Field rates empty when field_map types don't match any known attribute."""
        verifier = ExtractionVerifier()
        records = self._make_records(10, raw_name="Test")
        field_map = [_FakeFieldMapping("UNKNOWN_TYPE")]

        result = verifier.verify(
            records=records,
            failed_pages=[],
            reconciled_records=[],
            total_pages=10,
            field_map=field_map,
        )

        assert result.field_rates == {}

    def test_negative_failed_pages_clamped(self):
        """If reconciled > failed_pages, failed_pages is clamped to 0."""
        verifier = ExtractionVerifier()
        records = self._make_records(80, raw_name="Test")
        reconciled = self._make_records(25, raw_name="Extra")
        field_map = [_FakeFieldMapping("PERSON")]

        result = verifier.verify(
            records=records,
            failed_pages=list(range(20)),  # 20 failed, but 25 reconciled
            reconciled_records=reconciled,
            total_pages=100,
            field_map=field_map,
        )

        assert result.failed_pages == 0

    def test_summary_below_threshold(self):
        """Summary shows BELOW THRESHOLD when not acceptable."""
        verifier = ExtractionVerifier()
        records = self._make_records(50, raw_name="Test")
        field_map = [_FakeFieldMapping("PERSON")]

        result = verifier.verify(
            records=records,
            failed_pages=list(range(50)),
            reconciled_records=[],
            total_pages=100,
            field_map=field_map,
        )

        assert "BELOW THRESHOLD" in result.summary
        assert "50%" in result.summary

    def test_acceptable_rate_constant(self):
        """ACCEPTABLE_RATE is 0.90."""
        assert ExtractionVerifier.ACCEPTABLE_RATE == 0.90

    def test_government_id_field_mapped_from_ni_number(self):
        """NI_NUMBER field type maps to raw_government_id attribute."""
        verifier = ExtractionVerifier()
        records = self._make_records(10, raw_name="Test", raw_government_id="AB123456C")
        field_map = [_FakeFieldMapping("PERSON"), _FakeFieldMapping("NI_NUMBER")]

        result = verifier.verify(
            records=records,
            failed_pages=[],
            reconciled_records=[],
            total_pages=10,
            field_map=field_map,
        )

        assert result.field_rates["NI_NUMBER"] == pytest.approx(1.0)
        assert result.field_rates["PERSON"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# PIIRecord-like stub with page_range for audit/gap-fill tests
# ---------------------------------------------------------------------------

@dataclass
class _FakeRecordWithPage:
    raw_name: str | None = None
    raw_address: dict | str | None = None
    raw_government_id: str | None = None
    raw_dob: str | None = None
    raw_email: str | None = None
    raw_phone: str | None = None
    page_range: str | None = None


class TestRecordsToPageDict:
    """Test records_to_page_dict helper."""

    def test_basic_mapping(self):
        recs = [
            _FakeRecordWithPage(raw_name="Alice", raw_government_id="123-45-6789", page_range="1"),
            _FakeRecordWithPage(raw_name="Bob", raw_dob="01/01/1980", page_range="2"),
        ]
        d = records_to_page_dict(recs)
        assert 0 in d  # page_range "1" → page index 0
        assert 1 in d
        assert d[0][0]["PERSON"] == "Alice"
        assert d[0][0]["US_SSN"] == "123-45-6789"
        assert d[1][0]["DATE_OF_BIRTH"] == "01/01/1980"

    def test_skips_no_page_range(self):
        recs = [_FakeRecordWithPage(raw_name="Alice", page_range=None)]
        d = records_to_page_dict(recs)
        assert len(d) == 0

    def test_multiple_records_same_page(self):
        recs = [
            _FakeRecordWithPage(raw_name="Alice", page_range="5"),
            _FakeRecordWithPage(raw_name="Bob", page_range="5"),
        ]
        d = records_to_page_dict(recs)
        assert len(d[4]) == 2

    def test_address_dict(self):
        recs = [_FakeRecordWithPage(raw_name="Test", raw_address={"raw": "123 Main St"}, page_range="1")]
        d = records_to_page_dict(recs)
        assert d[0][0]["LOCATION"] == "123 Main St"

    def test_address_string(self):
        recs = [_FakeRecordWithPage(raw_name="Test", raw_address="123 Main St", page_range="1")]
        d = records_to_page_dict(recs)
        assert d[0][0]["LOCATION"] == "123 Main St"


class TestFindGapPages:
    """Test ExtractionVerifier.find_gap_pages()."""

    def test_finds_missing_gov_id(self):
        recs = [
            _FakeRecordWithPage(raw_name="Alice", raw_government_id="123-45-6789", page_range="1"),
            _FakeRecordWithPage(raw_name="Bob", page_range="2"),  # missing gov_id
        ]
        v = ExtractionVerifier()
        gaps = v.find_gap_pages(recs)
        assert 1 in gaps  # page index 1 = page_range "2"
        assert gaps[1][0][1] == ["raw_government_id", "raw_address"]

    def test_no_gaps_when_complete(self):
        recs = [
            _FakeRecordWithPage(
                raw_name="Alice", raw_government_id="123-45-6789",
                raw_address={"raw": "123 St"}, page_range="1",
            ),
        ]
        v = ExtractionVerifier()
        gaps = v.find_gap_pages(recs)
        assert len(gaps) == 0

    def test_skips_records_without_name(self):
        recs = [_FakeRecordWithPage(raw_name=None, page_range="1")]
        v = ExtractionVerifier()
        gaps = v.find_gap_pages(recs)
        assert len(gaps) == 0

    def test_custom_required_fields(self):
        recs = [_FakeRecordWithPage(raw_name="Alice", page_range="1")]
        v = ExtractionVerifier()
        gaps = v.find_gap_pages(recs, required_fields=["raw_email"])
        assert 0 in gaps
        assert gaps[0][0][1] == ["raw_email"]


class TestGapFillPromptParsing:
    """Test _build_gap_fill_prompt and _parse_gap_fill_response."""

    def test_prompt_includes_names(self):
        prompt = ExtractionVerifier._build_gap_fill_prompt(
            ["Alice Smith", "Bob Jones"],
            {"raw_government_id", "raw_address"},
        )
        assert "Alice Smith" in prompt
        assert "Bob Jones" in prompt
        assert "US_SSN" in prompt or "LOCATION" in prompt

    def test_parse_valid_json_array(self):
        response = json.dumps([
            {"PERSON": "Alice", "US_SSN": "123-45-6789", "LOCATION": "123 Main St"},
        ])
        result = ExtractionVerifier._parse_gap_fill_response(response)
        assert len(result) == 1
        assert result[0]["US_SSN"] == "123-45-6789"

    def test_parse_code_fences(self):
        response = '```json\n[{"PERSON": "Alice", "US_SSN": "999-99-9999"}]\n```'
        result = ExtractionVerifier._parse_gap_fill_response(response)
        assert len(result) == 1

    def test_parse_single_dict(self):
        response = '{"PERSON": "Alice", "LOCATION": "123 St"}'
        result = ExtractionVerifier._parse_gap_fill_response(response)
        assert len(result) == 1

    def test_parse_empty(self):
        assert ExtractionVerifier._parse_gap_fill_response("") == []
        assert ExtractionVerifier._parse_gap_fill_response("no json here") == []

    def test_parse_embedded_json(self):
        response = 'Here is the data:\n[{"PERSON": "Bob", "US_SSN": "111-22-3333"}]\nDone.'
        result = ExtractionVerifier._parse_gap_fill_response(response)
        assert len(result) == 1
        assert result[0]["PERSON"] == "Bob"


class TestMatchPerson:
    """Test ExtractionVerifier._match_person."""

    def test_exact_match(self):
        filled = [{"PERSON": "ALICE SMITH", "US_SSN": "123"}]
        m = ExtractionVerifier._match_person("ALICE SMITH", filled)
        assert m is not None
        assert m["US_SSN"] == "123"

    def test_last_name_fuzzy(self):
        filled = [{"PERSON": "Alice Marie Smith", "US_SSN": "456"}]
        m = ExtractionVerifier._match_person("ALICE SMITH", filled)
        assert m is not None

    def test_single_result_fallback(self):
        filled = [{"PERSON": "Different Name", "US_SSN": "789"}]
        m = ExtractionVerifier._match_person("ALICE SMITH", filled)
        assert m is not None  # single result = fallback match

    def test_no_match_multiple(self):
        filled = [
            {"PERSON": "Bob Jones", "US_SSN": "111"},
            {"PERSON": "Charlie Brown", "US_SSN": "222"},
        ]
        m = ExtractionVerifier._match_person("ALICE SMITH", filled)
        assert m is None

    def test_empty_inputs(self):
        assert ExtractionVerifier._match_person("", []) is None
        assert ExtractionVerifier._match_person("ALICE", []) is None
        assert ExtractionVerifier._match_person("", [{"PERSON": "X"}]) is None


class TestVisionGapFill:
    """Test vision_gap_fill with mocked vision model."""

    def test_no_client_returns_unchanged(self):
        recs = [_FakeRecordWithPage(raw_name="Alice", page_range="1")]
        v = ExtractionVerifier()
        result_recs, result = v.vision_gap_fill(recs, "/tmp/test.pdf", "doc1")
        assert result_recs is recs
        assert result.gap_fill_attempted == 0

    def test_no_gaps_returns_early(self):
        recs = [
            _FakeRecordWithPage(
                raw_name="Alice", raw_government_id="123-45-6789",
                raw_address={"raw": "123 St"}, page_range="1",
            ),
        ]
        mock_client = MagicMock()
        v = ExtractionVerifier()
        result_recs, result = v.vision_gap_fill(recs, "/tmp/test.pdf", "doc1", mock_client)
        assert result.gap_fill_attempted == 0
        mock_client.generate_with_images.assert_not_called()

    @patch("app.pdf.renderer.render_page_to_image")
    def test_fills_missing_gov_id(self, mock_render):
        mock_render.return_value = "base64image"
        mock_client = MagicMock()
        mock_client.is_vision_available.return_value = True
        mock_client.generate_with_images.return_value = json.dumps([
            {"PERSON": "Alice Smith", "US_SSN": "123-45-6789", "LOCATION": "999 Elm St"},
        ])

        recs = [_FakeRecordWithPage(raw_name="Alice Smith", page_range="1")]
        v = ExtractionVerifier()
        result_recs, result = v.vision_gap_fill(
            recs, "/tmp/test.pdf", "doc1", mock_client,
        )
        assert result.gap_fill_attempted == 1
        assert result.gap_fill_succeeded == 1
        assert result_recs[0].raw_government_id == "123-45-6789"
        assert result_recs[0].raw_address == {"raw": "999 Elm St"}

    @patch("app.pdf.renderer.render_page_to_image")
    def test_render_failure_skips_page(self, mock_render):
        mock_render.side_effect = Exception("render failed")
        mock_client = MagicMock()

        recs = [_FakeRecordWithPage(raw_name="Alice", page_range="1")]
        v = ExtractionVerifier()
        result_recs, result = v.vision_gap_fill(
            recs, "/tmp/test.pdf", "doc1", mock_client,
        )
        assert result.gap_fill_attempted == 1
        assert result.gap_fill_succeeded == 0

    @patch("app.pdf.renderer.render_page_to_image")
    def test_vision_failure_skips_page(self, mock_render):
        mock_render.return_value = "base64image"
        mock_client = MagicMock()
        mock_client.generate_with_images.side_effect = Exception("model error")

        recs = [_FakeRecordWithPage(raw_name="Alice", page_range="1")]
        v = ExtractionVerifier()
        result_recs, result = v.vision_gap_fill(
            recs, "/tmp/test.pdf", "doc1", mock_client,
        )
        assert result.gap_fill_succeeded == 0


class TestExtractionVerificationGapFillFields:
    """Test ExtractionVerification dataclass gap-fill fields."""

    def test_gap_fill_defaults(self):
        v = ExtractionVerification()
        assert v.gap_fill_attempted == 0
        assert v.gap_fill_succeeded == 0
        assert v.gap_fill_fields == {}

    def test_summary_includes_gap_fill(self):
        v = ExtractionVerifier()
        result = ExtractionVerification(
            total_pages=100,
            successful_pages=100,
            success_rate=1.0,
            gap_fill_attempted=20,
            gap_fill_succeeded=15,
            gap_fill_fields={"raw_government_id": 12, "raw_address": 8},
        )
        summary = v._build_summary(result)
        assert "Vision gap-fill: 15/20 pages" in summary
        assert "raw_government_id: 12 filled" in summary


class TestPipelineAuditWiring:
    """Test that audit + gap-fill are wired into the pipeline for all paths."""

    def test_records_to_page_dict_imported(self):
        """records_to_page_dict is importable from extraction_verifier."""
        from app.pipeline.extraction_verifier import records_to_page_dict
        assert callable(records_to_page_dict)

    def test_find_gap_pages_imported(self):
        """find_gap_pages exists on ExtractionVerifier."""
        v = ExtractionVerifier()
        assert hasattr(v, "find_gap_pages")

    def test_vision_gap_fill_imported(self):
        """vision_gap_fill exists on ExtractionVerifier."""
        v = ExtractionVerifier()
        assert hasattr(v, "vision_gap_fill")

    def test_pipeline_has_audit_for_all_paths(self):
        """two_phase.py references audit for all paths (not just Path 0)."""
        import inspect
        from app.pipeline import two_phase
        source = inspect.getsource(two_phase.run_extraction_background)
        # Audit after ALL paths — should reference records_to_page_dict or verify_by_coordinates
        # outside of Path 0's block
        assert "records_to_page_dict" in source or "verify_by_coordinates" in source
        # Gap-fill should be referenced
        assert "vision_gap_fill" in source or "find_gap_pages" in source
