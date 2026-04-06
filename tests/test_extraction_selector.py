"""Tests for the feedback-driven extraction selector (A0).

Tests Phase 1 (DocumentProfile), Phase 2 (quality scoring, method competition),
and edge cases (scanned PDFs, late onset, hallucination detection).
"""

import pytest
from dataclasses import dataclass
from unittest.mock import MagicMock, patch


# ============================================================
# Minimal stubs (avoid importing heavy dependencies)
# ============================================================

@dataclass
class FakeBlock:
    text: str
    page_or_sheet: int | str
    source_path: str = "/fake/doc.pdf"
    file_type: str = "pdf"
    block_type: str = "prose"
    bbox: tuple | None = None
    row: int | None = None
    column: int | None = None
    table_id: str | None = None


@dataclass
class FakeRecord:
    record_id: str = "r1"
    entity_type: str = "PERSON"
    normalized_value: str = ""
    raw_name: str | None = None
    raw_address: dict | None = None
    raw_phone: str | None = None
    raw_email: str | None = None
    raw_dob: str | None = None
    raw_government_id: str | None = None
    country: str = "US"
    source_document_id: str = ""
    page_or_sheet: int | str = 0
    entity_role: str | None = None
    page_range: str = ""
    entity_types_found: tuple = ()
    validation_flags: tuple = ()


# ============================================================
# Phase 1: DocumentProfile tests
# ============================================================

class TestDocumentProfile:
    """Test the UNDERSTAND phase."""

    def test_estimate_density_regex_fallback(self):
        """Density estimation works without Presidio engine."""
        from app.pipeline.extraction_selector import estimate_density

        blocks = [
            FakeBlock(
                text="John Smith 123-45-6789\nJane Doe 987-65-4321\nBob Johnson 555-12-3456",
                page_or_sheet=0,
            ),
        ]
        info = estimate_density(blocks, onset_page=0, engine=None)
        assert info.persons_per_page >= 2  # Should find name patterns
        assert info.has_ssn is True

    def test_estimate_density_empty_page(self):
        """Empty page returns zero density."""
        from app.pipeline.extraction_selector import estimate_density

        info = estimate_density([], onset_page=0, engine=None)
        assert info.persons_per_page == 0

    def test_detect_zones_with_onset(self):
        """Zones correctly split at onset page."""
        from app.pipeline.extraction_selector import detect_zones

        zones = detect_zones([], onset_page=10, total_pages=100)
        assert len(zones) == 2
        assert zones[0].zone_type == "cover"
        assert zones[0].start == 0
        assert zones[0].end == 9
        assert zones[1].zone_type == "data"
        assert zones[1].start == 10
        assert zones[1].end == 99

    def test_detect_zones_onset_zero(self):
        """Onset at page 0 = no cover zone."""
        from app.pipeline.extraction_selector import detect_zones

        zones = detect_zones([], onset_page=0, total_pages=50)
        assert len(zones) == 1
        assert zones[0].zone_type == "data"

    def test_pick_sample_pages_basic(self):
        """Sample pages start from onset."""
        from app.pipeline.extraction_selector import (
            DocumentProfile, DensityInfo, Zone, pick_sample_pages,
        )

        profile = DocumentProfile(
            total_pages=100,
            onset_page=10,
            zones=[
                Zone(start=0, end=9, zone_type="cover", page_count=10),
                Zone(start=10, end=99, zone_type="data", page_count=90),
            ],
        )
        blocks = [FakeBlock(text=f"page {i}", page_or_sheet=i) for i in range(100)]
        pages = pick_sample_pages(profile, blocks, n=5)
        assert len(pages) == 5
        assert all(p >= 10 for p in pages)  # All from data zone

    def test_pick_sample_pages_large_doc_spreads(self):
        """For large docs, samples are spread across the zone."""
        from app.pipeline.extraction_selector import (
            DocumentProfile, Zone, pick_sample_pages,
        )

        profile = DocumentProfile(
            total_pages=1000,
            onset_page=0,
            zones=[Zone(start=0, end=999, zone_type="data", page_count=1000)],
        )
        blocks = [FakeBlock(text=f"page {i}", page_or_sheet=i) for i in range(1000)]
        pages = pick_sample_pages(profile, blocks, n=5)
        assert len(pages) == 5
        # Pages should be spread across the range, not just 0-4
        assert pages[-1] > 100

    def test_build_document_profile_non_pdf(self):
        """Profile building works for non-PDF files."""
        from app.pipeline.extraction_selector import build_document_profile

        blocks = [
            FakeBlock(text="John Smith", page_or_sheet=0, file_type="xlsx"),
            FakeBlock(text="Jane Doe", page_or_sheet=1, file_type="xlsx"),
        ]
        profile = build_document_profile(
            doc_path="/fake/file.xlsx",
            blocks=blocks,
            file_type="xlsx",
            file_name="file.xlsx",
            onset_page=0,
        )
        assert profile.total_pages == 2
        assert profile.text_ratio == 1.0  # Non-PDF defaults
        assert profile.onset_page == 0


# ============================================================
# Phase 2: Quality Scoring tests
# ============================================================

class TestQualityScorer:
    """Test the quality scoring function."""

    def test_empty_records_score_zero(self):
        """No records = score 0."""
        from app.pipeline.quality_scorer import score_quality
        from app.pipeline.extraction_selector import DocumentProfile, DensityInfo

        profile = DocumentProfile(density=DensityInfo(persons_per_page=10))
        qs = score_quality([], profile, [0, 1, 2], [])
        assert qs.total == 0

    def test_perfect_extraction(self):
        """Records with full fields, matching source text, correct formats score high."""
        from app.pipeline.quality_scorer import score_quality
        from app.pipeline.extraction_selector import DocumentProfile, DensityInfo

        profile = DocumentProfile(density=DensityInfo(persons_per_page=2))

        blocks = [
            FakeBlock(text="John Smith 123-45-6789 john@example.com 01/15/1990", page_or_sheet=0),
            FakeBlock(text="Jane Doe 987-65-4321 jane@example.com 03/22/1985", page_or_sheet=1),
        ]

        records = [
            FakeRecord(
                raw_name="John Smith", raw_government_id="123-45-6789",
                raw_email="john@example.com", raw_dob="01/15/1990",
                page_or_sheet=0, page_range="0",
            ),
            FakeRecord(
                raw_name="Jane Doe", raw_government_id="987-65-4321",
                raw_email="jane@example.com", raw_dob="03/22/1985",
                page_or_sheet=1, page_range="1",
            ),
        ]

        qs = score_quality(records, profile, [0, 1], blocks)
        assert qs.total >= 70  # Should score very high
        assert qs.coverage == 25.0  # All sample pages have records
        assert qs.anchor_validation > 0  # Names found in source text
        assert qs.hallucination_count == 0

    def test_hallucinated_names_score_low(self):
        """Names not in source text get penalized."""
        from app.pipeline.quality_scorer import score_quality
        from app.pipeline.extraction_selector import DocumentProfile, DensityInfo

        profile = DocumentProfile(density=DensityInfo(persons_per_page=1))

        # Source text has different names than extracted
        blocks = [
            FakeBlock(text="Alice Johnson lives at 123 Main St", page_or_sheet=0),
        ]

        records = [
            FakeRecord(raw_name="FABRICATED NAME", page_or_sheet=0, page_range="0"),
            FakeRecord(raw_name="ANOTHER FAKE", page_or_sheet=0, page_range="0"),
        ]

        qs = score_quality(records, profile, [0], blocks)
        assert qs.hallucination_count == 2
        assert qs.anchor_validation == 0  # Negative scores clamped to 0

    def test_format_consistency_ssn(self):
        """SSN format validation contributes to score."""
        from app.pipeline.quality_scorer import score_quality
        from app.pipeline.extraction_selector import DocumentProfile, DensityInfo

        profile = DocumentProfile(density=DensityInfo(persons_per_page=1))
        blocks = [FakeBlock(text="John Smith 123-45-6789", page_or_sheet=0)]

        # Good SSN format
        good = [FakeRecord(raw_name="John Smith", raw_government_id="123-45-6789", page_or_sheet=0, page_range="0")]
        qs_good = score_quality(good, profile, [0], blocks)

        # Bad SSN format
        bad = [FakeRecord(raw_name="John Smith", raw_government_id="garbage", page_or_sheet=0, page_range="0")]
        qs_bad = score_quality(bad, profile, [0], blocks)

        assert qs_good.format_consistency > qs_bad.format_consistency

    def test_density_mismatch_reduces_score(self):
        """If we find fewer records than expected, density score drops."""
        from app.pipeline.quality_scorer import score_quality
        from app.pipeline.extraction_selector import DocumentProfile, DensityInfo

        # Expect 20 per page, only find 1
        profile = DocumentProfile(density=DensityInfo(persons_per_page=20))
        blocks = [FakeBlock(text="John Smith", page_or_sheet=0)]
        records = [FakeRecord(raw_name="John Smith", page_or_sheet=0, page_range="0")]

        qs = score_quality(records, profile, [0], blocks)
        assert qs.density_match < 5  # Way below expected density


# ============================================================
# Method applicability tests
# ============================================================

class TestMethodApplicability:
    """Test that methods are correctly filtered by document profile."""

    def test_coordinate_needs_field_map(self):
        """CoordinateMethod is inapplicable without a field map."""
        from app.pipeline.extraction_methods import CoordinateMethod
        from app.pipeline.extraction_selector import DocumentProfile

        profile = DocumentProfile(has_text_layer=True)

        no_map = CoordinateMethod(field_map=None)
        assert no_map.is_applicable(profile) is False

        empty_map = CoordinateMethod(field_map=[])
        assert empty_map.is_applicable(profile) is False

        with_map = CoordinateMethod(field_map=[{"anchor": "Name"}])
        assert with_map.is_applicable(profile) is True

    def test_vision_only_for_scanned(self):
        """VisionMethod is only applicable for docs with low text ratio."""
        from app.pipeline.extraction_methods import VisionMethod
        from app.pipeline.extraction_selector import DocumentProfile

        llm = MagicMock()

        # All text — vision not applicable
        text_profile = DocumentProfile(text_ratio=1.0, has_text_layer=True)
        assert VisionMethod(llm_client=llm).is_applicable(text_profile) is False

        # Scanned — vision applicable
        scan_profile = DocumentProfile(text_ratio=0.1, has_text_layer=False)
        assert VisionMethod(llm_client=llm).is_applicable(scan_profile) is True

    def test_get_applicable_methods(self):
        """get_applicable_methods returns correct set for a text doc."""
        from app.pipeline.extraction_methods import get_applicable_methods
        from app.pipeline.extraction_selector import DocumentProfile

        engine = MagicMock()
        llm = MagicMock()

        profile = DocumentProfile(
            has_text_layer=True,
            text_ratio=1.0,
            source_path="/fake/doc.pdf",
        )

        methods = get_applicable_methods(
            profile, field_map=None, engine=engine,
            llm_client=llm, target_entities=None,
        )
        names = [m.name for m in methods]

        assert "presidio_smart" in names
        assert "llm_template" in names
        assert "llm_table" in names
        # No coordinate (no field map), no vision (text_ratio=1.0)
        assert "coordinate" not in names
        assert "vision" not in names


# ============================================================
# Edge case tests
# ============================================================

class TestEdgeCases:
    """Test scenarios that broke the naive pipeline."""

    def test_late_onset_samples_from_data_zone(self):
        """If data starts on page 20, samples should start there."""
        from app.pipeline.extraction_selector import (
            DocumentProfile, Zone, pick_sample_pages,
        )

        profile = DocumentProfile(
            total_pages=100,
            onset_page=20,
            zones=[
                Zone(start=0, end=19, zone_type="cover", page_count=20),
                Zone(start=20, end=99, zone_type="data", page_count=80),
            ],
        )
        blocks = [FakeBlock(text=f"page {i}", page_or_sheet=i) for i in range(100)]
        pages = pick_sample_pages(profile, blocks, n=5)

        assert all(p >= 20 for p in pages)

    def test_single_page_doc(self):
        """Single-page doc should still profile and sample correctly."""
        from app.pipeline.extraction_selector import (
            DocumentProfile, Zone, pick_sample_pages,
        )

        profile = DocumentProfile(
            total_pages=1,
            onset_page=0,
            zones=[Zone(start=0, end=0, zone_type="data", page_count=1)],
        )
        blocks = [FakeBlock(text="John Smith 123-45-6789", page_or_sheet=0)]
        pages = pick_sample_pages(profile, blocks, n=5)

        assert len(pages) == 1
        assert pages[0] == 0

    def test_score_comparison_selects_better_method(self):
        """When two methods produce results, the higher-scoring one wins."""
        from app.pipeline.quality_scorer import score_quality, QualityScore
        from app.pipeline.extraction_selector import DocumentProfile, DensityInfo

        profile = DocumentProfile(density=DensityInfo(persons_per_page=2))
        blocks = [
            FakeBlock(text="John Smith 123-45-6789", page_or_sheet=0),
            FakeBlock(text="Jane Doe 987-65-4321", page_or_sheet=1),
        ]
        sample_pages = [0, 1]

        # Method A: finds both people with SSNs
        records_a = [
            FakeRecord(raw_name="John Smith", raw_government_id="123-45-6789", page_or_sheet=0, page_range="0"),
            FakeRecord(raw_name="Jane Doe", raw_government_id="987-65-4321", page_or_sheet=1, page_range="1"),
        ]

        # Method B: finds one person, no SSN
        records_b = [
            FakeRecord(raw_name="John Smith", page_or_sheet=0, page_range="0"),
        ]

        score_a = score_quality(records_a, profile, sample_pages, blocks)
        score_b = score_quality(records_b, profile, sample_pages, blocks)

        assert score_a.total > score_b.total
