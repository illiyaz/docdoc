"""E2: Regression Test Suite for Extraction Accuracy.

Reads synthetic test files through the reader registry and Presidio
engine, then verifies extraction results match the expected manifest.

Tests cover:
  - Reader coverage: each format produces non-empty blocks
  - PII detection: Presidio finds expected entity types
  - Name extraction: all expected person names are found
  - False positive filtering: org metadata is NOT extracted as subjects
  - Smart header detection: XLS/XLSX with title rows work correctly
  - Email parsing: body PII is extracted, sender is not

Run: pytest tests/test_regression_extraction.py -v
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


FIXTURES_DIR = Path(__file__).parent / "fixtures"
MANIFEST_PATH = FIXTURES_DIR / "manifest.json"


def _load_manifest() -> list[dict]:
    if not MANIFEST_PATH.exists():
        pytest.skip("Fixtures not generated. Run: python -m tests.generate_synthetic")
    with open(MANIFEST_PATH) as f:
        return json.load(f)


def _get_fixture(filename: str) -> Path:
    """Find a fixture file, handling fallbacks."""
    path = FIXTURES_DIR / filename
    if path.exists():
        return path
    # Try fallback name
    fallback = FIXTURES_DIR / filename.replace(".xls", "_fallback.csv")
    if fallback.exists():
        return fallback
    pytest.skip(f"Fixture {filename} not available")


# ============================================================
# Reader coverage tests
# ============================================================

class TestReaderCoverage:
    """Every supported format produces non-empty ExtractedBlocks."""

    @pytest.fixture
    def manifest(self):
        return _load_manifest()

    def test_csv_produces_blocks(self):
        from app.readers.registry import get_reader
        reader = get_reader(str(_get_fixture("synthetic_pii.csv")))
        blocks = reader.read()
        assert len(blocks) > 0
        # CSV should have header + 8 data rows × 6 columns = 54 blocks
        assert len(blocks) >= 48

    def test_xlsx_produces_blocks(self):
        from app.readers.registry import get_reader
        reader = get_reader(str(_get_fixture("synthetic_pii.xlsx")))
        blocks = reader.read()
        assert len(blocks) > 0
        # Multiple sheets, should have plenty of blocks
        assert len(blocks) >= 20

    def test_xlsx_hidden_sheet_skipped(self):
        """Hidden sheet 'Internal Notes' should NOT produce blocks."""
        from app.readers.registry import get_reader
        reader = get_reader(str(_get_fixture("synthetic_pii.xlsx")))
        blocks = reader.read()
        # No block should come from the hidden sheet
        hidden_blocks = [b for b in blocks if getattr(b, "page_or_sheet", "") == "Internal Notes"]
        assert len(hidden_blocks) == 0

    def test_docx_produces_blocks(self):
        from app.readers.registry import get_reader
        reader = get_reader(str(_get_fixture("synthetic_breach_report.docx")))
        blocks = reader.read()
        assert len(blocks) > 0

    def test_html_produces_blocks(self):
        from app.readers.registry import get_reader
        reader = get_reader(str(_get_fixture("synthetic_breach_table.html")))
        blocks = reader.read()
        assert len(blocks) > 0

    def test_eml_produces_blocks(self):
        from app.readers.registry import get_reader
        reader = get_reader(str(_get_fixture("synthetic_breach_email.eml")))
        blocks = reader.read()
        assert len(blocks) > 0

    @pytest.mark.xfail(reason="TXT falls through to TikaReader (not implemented)")
    def test_txt_produces_blocks(self):
        from app.readers.registry import get_reader
        reader = get_reader(str(_get_fixture("synthetic_pii_list.txt")))
        blocks = reader.read()
        assert len(blocks) > 0


# ============================================================
# PII detection tests
# ============================================================

class TestPIIDetection:
    """Presidio finds expected PII types in synthetic files."""

    @pytest.fixture(scope="class")
    def engine(self):
        try:
            from app.pii.presidio_engine import PresidioEngine
            return PresidioEngine()
        except ImportError:
            pytest.skip("Presidio not available")

    def _extract_text(self, filename: str) -> str:
        from app.readers.registry import get_reader
        reader = get_reader(str(_get_fixture(filename)))
        blocks = reader.read()
        return "\n".join(b.text for b in blocks)

    def test_csv_detects_ssns(self, engine):
        text = self._extract_text("synthetic_pii.csv")
        detections = engine.analyze(text)
        ssn_detections = [d for d in detections if d.entity_type in ("US_SSN", "SSN")]
        assert len(ssn_detections) >= 6, f"Expected >=6 SSNs, found {len(ssn_detections)}"

    def test_csv_detects_persons(self, engine):
        text = self._extract_text("synthetic_pii.csv")
        detections = engine.analyze(text)
        person_detections = [d for d in detections if d.entity_type == "PERSON"]
        assert len(person_detections) >= 4, f"Expected >=4 persons, found {len(person_detections)}"

    def test_csv_detects_emails(self, engine):
        text = self._extract_text("synthetic_pii.csv")
        detections = engine.analyze(text)
        email_detections = [d for d in detections if d.entity_type == "EMAIL_ADDRESS"]
        assert len(email_detections) >= 6, f"Expected >=6 emails, found {len(email_detections)}"

    def test_txt_detects_phones(self, engine):
        text = self._extract_text("synthetic_pii_list.txt")
        detections = engine.analyze(text)
        phone_detections = [d for d in detections if d.entity_type == "PHONE_NUMBER"]
        assert len(phone_detections) >= 5, f"Expected >=5 phones, found {len(phone_detections)}"


# ============================================================
# Name extraction accuracy tests
# ============================================================

class TestNameExtraction:
    """All expected person names are found in reader output."""

    EXPECTED_NAMES = [
        "John Michael Smith", "Maria Elena Garcia", "Robert James Wilson",
        "Sarah Ann Johnson", "David Lee Chen", "Jennifer Rose Brown",
        "Michael Anthony Davis", "Emily Kate Martinez",
    ]

    def _all_text(self, filename: str) -> str:
        from app.readers.registry import get_reader
        reader = get_reader(str(_get_fixture(filename)))
        blocks = reader.read()
        return "\n".join(b.text for b in blocks)

    def test_csv_contains_all_names(self):
        text = self._all_text("synthetic_pii.csv")
        for name in self.EXPECTED_NAMES:
            assert name in text, f"Missing name: {name}"

    def test_xlsx_contains_all_names(self):
        text = self._all_text("synthetic_pii.xlsx")
        for name in self.EXPECTED_NAMES:
            assert name in text, f"Missing name: {name}"

    @pytest.mark.xfail(reason="TXT falls through to TikaReader (not implemented)")
    def test_txt_contains_all_names(self):
        text = self._all_text("synthetic_pii_list.txt")
        for name in self.EXPECTED_NAMES:
            assert name in text, f"Missing name: {name}"

    def test_html_contains_5_names(self):
        text = self._all_text("synthetic_breach_table.html")
        found = sum(1 for name in self.EXPECTED_NAMES if name in text)
        assert found >= 5, f"Expected >=5 names in HTML, found {found}"

    def test_docx_contains_3_names(self):
        text = self._all_text("synthetic_breach_report.docx")
        found = sum(1 for name in self.EXPECTED_NAMES[:3] if name in text)
        assert found >= 3, f"Expected >=3 names in DOCX, found {found}"

    def test_eml_contains_3_names(self):
        text = self._all_text("synthetic_breach_email.eml")
        found = sum(1 for name in self.EXPECTED_NAMES[:3] if name in text)
        assert found >= 3, f"Expected >=3 names in EML, found {found}"


# ============================================================
# Smart header detection (D3)
# ============================================================

class TestSmartHeaderDetection:
    """XLSX with title rows correctly identifies the header row."""

    def test_xlsx_sheet2_finds_correct_headers(self):
        """Sheet 'Report Summary' has title row 0, blank row 1, headers row 2."""
        from app.readers.registry import get_reader
        reader = get_reader(str(_get_fixture("synthetic_pii.xlsx")))
        blocks = reader.read()

        # Get header blocks from Sheet 2
        sheet2_headers = [
            b for b in blocks
            if getattr(b, "page_or_sheet", "") == "Report Summary"
            and getattr(b, "block_type", "") == "table_header"
        ]

        header_texts = {b.text for b in sheet2_headers}
        # Should find the real headers, not the title row
        assert "Name" in header_texts or "Government ID" in header_texts, \
            f"Expected real headers, got: {header_texts}"
        assert "Acme Healthcare" not in " ".join(header_texts), \
            "Title row should NOT be detected as headers"


# ============================================================
# False positive filtering
# ============================================================

class TestFalsePositiveFiltering:
    """Org metadata should be identified as non-subject PII."""

    def test_label_deny_list_catches_labels(self):
        """Labels like 'Emergency Contact' are rejected as PERSON names."""
        from app.pii.context_deny_list import is_label_as_person

        assert is_label_as_person("emergency contact")[0] is True
        assert is_label_as_person("billing address")[0] is True
        assert is_label_as_person("John Smith")[0] is False

    def test_email_sender_context_detects_from(self):
        """Names near 'From:' are flagged as sender metadata."""
        from app.pii.context_deny_list import is_email_sender_context

        result = is_email_sender_context(
            "Dr. Patricia Williams", "PERSON",
            "From: Dr. Patricia Williams <privacy@acmehealthcare.com>",
        )
        assert result[0] is True

    def test_email_sender_context_does_not_flag_subjects(self):
        """Names in body text (not near sender labels) are NOT flagged."""
        from app.pii.context_deny_list import is_email_sender_context

        result = is_email_sender_context(
            "John Michael Smith", "PERSON",
            "The following individuals were affected: John Michael Smith, SSN: 123-45-6789",
        )
        assert result[0] is False


# ============================================================
# Quality scorer regression
# ============================================================

class TestQualityScorerRegression:
    """Quality scorer produces consistent scores on known inputs."""

    def test_perfect_extraction_scores_high(self):
        from tests.test_extraction_selector import FakeRecord, FakeBlock
        from app.pipeline.quality_scorer import score_quality
        from app.pipeline.extraction_selector import DocumentProfile, DensityInfo

        profile = DocumentProfile(density=DensityInfo(persons_per_page=2))
        blocks = [
            FakeBlock(text="John Smith 123-45-6789 john@example.com", page_or_sheet=0),
            FakeBlock(text="Jane Doe 987-65-4321 jane@example.com", page_or_sheet=1),
        ]
        records = [
            FakeRecord(raw_name="John Smith", raw_government_id="123-45-6789",
                       raw_email="john@example.com", page_or_sheet=0, page_range="0"),
            FakeRecord(raw_name="Jane Doe", raw_government_id="987-65-4321",
                       raw_email="jane@example.com", page_or_sheet=1, page_range="1"),
        ]
        qs = score_quality(records, profile, [0, 1], blocks)
        assert qs.total >= 70
        assert qs.hallucination_count == 0

    def test_empty_extraction_scores_zero(self):
        from app.pipeline.quality_scorer import score_quality
        from app.pipeline.extraction_selector import DocumentProfile, DensityInfo

        profile = DocumentProfile(density=DensityInfo(persons_per_page=5))
        qs = score_quality([], profile, [0, 1, 2], [])
        assert qs.total == 0
