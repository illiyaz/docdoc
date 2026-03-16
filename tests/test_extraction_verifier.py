"""Tests for ExtractionVerifier (Step 22d)."""
from __future__ import annotations

import pytest
from dataclasses import dataclass

from app.pipeline.extraction_verifier import ExtractionVerifier, ExtractionVerification


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
