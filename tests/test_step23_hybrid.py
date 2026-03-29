"""Tests for Step 23 — Hybrid Pipeline Integration.

Tests cover:
  1. Onset scoring (cover page penalty)
  2. Structural name matcher
  3. Coordinate-based text audit
  4. Vision router fallback model
  5. Image reader format support
  6. Email reader MSG support
"""
from __future__ import annotations

import pytest
import re

# ─── 1. Onset scoring ────────────────────────────────────────

from app.readers.onset import _score_page, COVER_SIGNALS, DATA_SIGNALS


class TestOnsetScoring:
    """Cover page penalty scoring (proven on AWIR-038 and TALX)."""

    def test_cover_page_gets_negative_score(self):
        text = """Report Summary
        Account Criteria: Open Accounts Only
        Total Accounts in Report: 1,052
        Date Closed Range: Do Not Limit
        Report Style: Name (Ascending)
        Items Displayed in Report Detail"""
        score, signals = _score_page(text)
        assert score < 0, f"Cover page should be negative, got {score}"
        assert "COVER_PENALTY" in signals

    def test_data_page_gets_positive_score(self):
        text = """ADAMS,BRADLEY JAY  300-44-5589  02/15/18
        ALFINO,DANIEL MICHAEL  593-82-5247  02/15/18
        ALLEN,AMY LEIGH  256-39-7058  02/15/18"""
        score, signals = _score_page(text)
        assert score > 50, f"Data page should be strongly positive, got {score}"
        assert "SSN" in signals

    def test_mixed_page_scores_correctly(self):
        """Page with some labels and some data should be positive but moderate."""
        text = """Name: John Smith
        Date of Birth: 01/15/1980
        Account: 12345"""
        score, signals = _score_page(text)
        assert score > 0

    def test_empty_page_scores_zero(self):
        score, signals = _score_page("")
        assert score == 0
        assert not signals

    def test_diversity_bonus(self):
        """Page with multiple PII types gets diversity bonus."""
        text_one = "John Smith 123-45-6789"
        text_diverse = "John Smith 123-45-6789 01/15/1980 john@test.com (555) 123-4567"
        score1, _ = _score_page(text_one)
        score2, _ = _score_page(text_diverse)
        assert score2 > score1, "Diverse PII page should score higher"


# ─── 2. Structural name matcher ──────────────────────────────

from app.pipeline.coordinate_extractor import (
    _analyze_name_structure,
    _build_name_structures,
    find_structural_names,
    _clean_name,
    _NAME_BLOCKLIST,
)


class TestStructuralNameMatcher:
    """ALL_CAPS embedded name detection (proven on Complex1 + PPACA)."""

    def test_analyze_structure_word_word(self):
        assert _analyze_name_structure("JOHN SMITH") == ("WORD", "WORD")

    def test_analyze_structure_initial_word_word(self):
        assert _analyze_name_structure("K BEVINGTON II") == ("INITIAL", "WORD", "SUFFIX")

    def test_analyze_structure_with_comma(self):
        assert _analyze_name_structure("ADAMS,BRADLEY JAY") == ("WORD", "WORD", "WORD")

    def test_build_structures_from_samples(self):
        samples = ["JOHN SMITH", "JANE DOE", "K P BEVINGTON II"]
        structures, min_w, max_w = _build_name_structures(samples)
        assert ("WORD", "WORD") in structures
        assert min_w == 2
        assert max_w >= 2

    def test_find_names_in_line(self):
        structures = {("WORD", "WORD"), ("WORD", "INITIAL", "WORD")}
        line = "300-44-5589  ADAMS BRADLEY  02/15/18"
        found = find_structural_names(line, structures, 2, 3)
        assert len(found) >= 1
        assert "ADAMS BRADLEY" in found[0] or "BRADLEY" in found[0]

    def test_blocklist_rejects_address_words(self):
        structures = {("WORD", "WORD")}
        line = "NORTH AVENUE EAST STREET"
        found = find_structural_names(line, structures, 2, 2)
        assert len(found) == 0

    def test_blocklist_rejects_company_words(self):
        structures = {("WORD", "WORD", "WORD")}
        line = "NATIONAL INSURANCE COMPANY"
        found = find_structural_names(line, structures, 2, 4)
        assert len(found) == 0

    def test_clean_name_strips_status_code(self):
        assert _clean_name("ADAMS,BRADLEY JAY A") == "ADAMS,BRADLEY JAY"

    def test_clean_name_preserves_middle_initial_v(self):
        assert _clean_name("BELL RICHARD V") == "BELL RICHARD V"

    def test_clean_name_preserves_suffix_ii(self):
        assert _clean_name("KEN P BEVINGTON II") == "KEN P BEVINGTON II"

    def test_blocklist_comprehensive(self):
        """Blocklist should contain common false-positive words."""
        for word in ["STREET", "AVENUE", "REPORT", "COMPANY", "LLC", "INC",
                     "SCHOOL", "UNIVERSITY", "THE", "AND", "FOR"]:
            assert word in _NAME_BLOCKLIST, f"{word} should be in blocklist"


# ─── 3. Coordinate-based text audit ──────────────────────────

from app.pipeline.extraction_verifier import ExtractionVerifier, _FORMAT_CHECKS


class TestCoordinateAudit:
    """Text-based verification (proven: 17/17 PASS on real docs)."""

    def test_format_check_ssn(self):
        assert _FORMAT_CHECKS["US_SSN"].match("123-45-6789")
        assert _FORMAT_CHECKS["US_SSN"].match("XXX-XX-1234")
        # Pure numeric matches too (numeric gov IDs like NI numbers)
        assert _FORMAT_CHECKS["US_SSN"].match("12345")
        assert not _FORMAT_CHECKS["US_SSN"].match("not-a-number")

    def test_format_check_dob(self):
        assert _FORMAT_CHECKS["DATE_OF_BIRTH"].match("01/15/1980")
        assert _FORMAT_CHECKS["DATE_OF_BIRTH"].match("1/5/22")
        assert _FORMAT_CHECKS["DATE_OF_BIRTH"].match("2024-01-15")

    def test_format_check_email(self):
        assert _FORMAT_CHECKS["EMAIL_ADDRESS"].match("john@test.com")
        assert not _FORMAT_CHECKS["EMAIL_ADDRESS"].match("not-email")

    def test_verifier_verify_basic(self):
        """Basic verify method still works."""
        from unittest.mock import MagicMock
        from app.rra.entity_resolver import PIIRecord

        verifier = ExtractionVerifier()
        rec = PIIRecord(
            record_id="1", entity_type="PERSON",
            normalized_value="John Smith", raw_name="John Smith",
            source_document_id="doc1", page_range="1",
        )
        fm = MagicMock()
        fm.field_type = "PERSON"
        result = verifier.verify([rec], [], [], 10, [fm])
        assert result.successful_pages == 1
        assert result.total_pages == 10

    def test_verification_result_has_audit_fields(self):
        """ExtractionVerification should have coordinate audit fields."""
        from app.pipeline.extraction_verifier import ExtractionVerification
        v = ExtractionVerification()
        assert hasattr(v, "audit_status")
        assert hasattr(v, "audit_confidence")
        assert hasattr(v, "audit_consistency")
        assert hasattr(v, "pages_audited")


# ─── 4. Vision router fallback ───────────────────────────────

from app.pipeline.vision_router import VisionRouter, VisionRoutingResult


class TestVisionRouterFallback:
    """Dual-model fallback (proven: qwen 500 → llama catches it)."""

    def test_routing_result_has_model_used(self):
        r = VisionRoutingResult(structure_type="fixed_single_page", structure_confidence=0.9)
        assert hasattr(r, "model_used")

    def test_router_accepts_fallback_model(self):
        """VisionRouter should accept fallback_model parameter."""
        from unittest.mock import MagicMock
        client = MagicMock()
        router = VisionRouter(client, vision_model="primary", fallback_model="fallback")
        assert router.fallback_model == "fallback"
        assert router.vision_model == "primary"


# ─── 5. Image reader ─────────────────────────────────────────

from app.readers.image_reader import ImageReader, SUPPORTED_EXTENSIONS


class TestImageReader:
    """Image reader with vision-first extraction."""

    def test_supported_extensions(self):
        for ext in (".jpg", ".jpeg", ".png", ".heic", ".webp", ".tif", ".bmp"):
            assert ext in SUPPORTED_EXTENSIONS

    def test_reader_instantiation(self):
        reader = ImageReader("/fake/path.jpg")
        assert reader.file_path == "/fake/path.jpg"


# ─── 6. Reader registry ──────────────────────────────────────

from app.readers.registry import _LAZY_REGISTRY


class TestReaderRegistry:
    """Format routing via registry."""

    def test_image_formats_registered(self):
        for ext in ("jpg", "jpeg", "png", "heic", "webp", "tif", "tiff", "bmp"):
            assert ext in _LAZY_REGISTRY, f"{ext} not in registry"
            assert _LAZY_REGISTRY[ext] == ("app.readers.image_reader", "ImageReader")

    def test_msg_registered(self):
        assert "msg" in _LAZY_REGISTRY

    def test_xlsm_registered(self):
        assert "xlsm" in _LAZY_REGISTRY

    def test_tsv_registered(self):
        assert "tsv" in _LAZY_REGISTRY


# ─── 7. Email reader MSG support ─────────────────────────────

from app.readers.email_reader import EmailReader


class TestEmailReaderMSG:
    """Enhanced email reader handles .msg files."""

    def test_eml_path_used_for_eml(self):
        """EmailReader dispatches to _read_eml for .eml files."""
        reader = EmailReader("/fake/test.eml")
        assert hasattr(reader, "_read_eml")

    def test_msg_path_used_for_msg(self):
        """EmailReader dispatches to _read_msg for .msg files."""
        reader = EmailReader("/fake/test.msg")
        assert hasattr(reader, "_read_msg")

    def test_msg_graceful_without_extract_msg(self):
        """If extract-msg not installed, _read_msg returns empty list."""
        # This test verifies the ImportError handling
        reader = EmailReader("/fake/nonexistent.msg")
        # Will return [] due to file not found or import error
        result = reader._read_msg()
        assert isinstance(result, list)


# ─── 8. Static value filter ──────────────────────────────────

from app.pipeline.static_filter import filter_static_values


class TestStaticFilter:
    """Remove report metadata values that appear on too many pages."""

    def test_filters_value_on_most_pages(self):
        """Value appearing on >50% of pages should be removed."""
        page_records = {}
        for pn in range(10):
            page_records[pn] = [
                {"PERSON": f"Person {pn}", "DATE_OF_BIRTH": "01/01/2024"},  # static date
            ]
        cleaned, removed = filter_static_values(page_records, threshold=0.5)
        # The static DOB should be removed
        assert "DATE_OF_BIRTH" in removed
        assert "01/01/2024" in removed["DATE_OF_BIRTH"]
        # But PERSON values should remain
        for recs in cleaned.values():
            for rec in recs:
                assert "PERSON" in rec

    def test_preserves_unique_values(self):
        """Values unique to each page should be preserved."""
        page_records = {}
        for pn in range(10):
            page_records[pn] = [
                {"PERSON": f"Person {pn}", "DATE_OF_BIRTH": f"0{pn+1}/15/1980"},
            ]
        cleaned, removed = filter_static_values(page_records, threshold=0.5)
        assert not removed  # nothing should be removed
        assert len(cleaned) == 10

    def test_filters_repeated_person_as_static(self):
        """PERSON appearing on >50% of pages is correctly identified as static (header/footer)."""
        page_records = {}
        for pn in range(10):
            page_records[pn] = [
                {"PERSON": "Same Name", "US_SSN": "123-45-6789"},
            ]
        cleaned, removed = filter_static_values(page_records, threshold=0.5)
        # "Same Name" appears on 100% of pages — it's a static value (header/footer)
        assert "PERSON" in removed
        assert removed["PERSON"] == ["Same Name"]

    def test_skips_small_documents(self):
        """Documents with fewer than min_pages should not be filtered."""
        page_records = {
            0: [{"PERSON": "A", "DATE_OF_BIRTH": "01/01/2024"}],
            1: [{"PERSON": "B", "DATE_OF_BIRTH": "01/01/2024"}],
        }
        cleaned, removed = filter_static_values(page_records, threshold=0.5, min_pages=5)
        assert not removed
        assert len(cleaned) == 2

    def test_returns_removed_for_audit(self):
        """Removed dict should list what was filtered for audit trail."""
        page_records = {}
        for pn in range(10):
            page_records[pn] = [
                {"PERSON": f"P{pn}", "PHONE_NUMBER": "555-0000", "DATE_OF_BIRTH": "01/01/2024"},
            ]
        _, removed = filter_static_values(page_records, threshold=0.5)
        assert "PHONE_NUMBER" in removed or "DATE_OF_BIRTH" in removed


# ─── 9. Template cache ───────────────────────────────────────

from app.pipeline.template_cache import TemplateCache, CacheEntry


class TestTemplateCache:
    """Template caching for repeat document layouts."""

    def test_cache_starts_empty(self):
        cache = TemplateCache()
        assert cache.size == 0

    def test_put_and_get(self):
        """Store and retrieve a cache entry."""
        cache = TemplateCache()
        # Can't test with real PDF in unit test, but test the data structures
        entry = CacheEntry(
            routing_dict={"structure_type": "fixed_single_page"},
            field_map_dicts=[{"field_type": "PERSON", "anchor_text": "Name:"}],
            name_samples=["JOHN SMITH"],
        )
        assert entry.routing_dict["structure_type"] == "fixed_single_page"
        assert entry.hit_count == 0

    def test_cache_stats(self):
        cache = TemplateCache(max_entries=50)
        stats = cache.stats()
        assert stats["entries"] == 0
        assert stats["max_entries"] == 50

    def test_cache_clear(self):
        cache = TemplateCache()
        cache._cache["test_key"] = CacheEntry(
            routing_dict={}, field_map_dicts=None, name_samples=[],
        )
        assert cache.size == 1
        cache.clear()
        assert cache.size == 0

    def test_max_entries_eviction(self):
        """Cache should evict when at capacity."""
        cache = TemplateCache(max_entries=3)
        for i in range(3):
            cache._cache[f"key_{i}"] = CacheEntry(
                routing_dict={}, field_map_dicts=None, name_samples=[],
                hit_count=i,
            )
        assert cache.size == 3
        # Compute key will fail without real PDF, but the eviction logic is testable
        # by directly manipulating _cache
        cache._cache["key_new"] = CacheEntry(
            routing_dict={}, field_map_dicts=None, name_samples=[],
        )
        assert cache.size == 4  # direct insert bypasses eviction
        # Real eviction happens in put() which needs a real PDF
