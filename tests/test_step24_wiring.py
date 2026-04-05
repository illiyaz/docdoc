"""Tests for Step 24a — static_filter + template_cache wiring.

Tests cover:
- static_filter.py: value frequency detection, threshold logic, never-filter fields,
  audit trail, min_pages guard
- template_cache.py: key computation, hit/miss, eviction, cache stats,
  variable stripping, reconstruction from cache
- two_phase.py integration: static filter placement, template cache placement
"""
from __future__ import annotations

import hashlib
import re
import tempfile
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# static_filter tests
# ---------------------------------------------------------------------------

from app.pipeline.static_filter import filter_static_values, _NEVER_FILTER, _LIKELY_STATIC


class TestStaticFilterBasic:
    """Core filter logic."""

    def test_below_min_pages_no_filtering(self):
        """Documents with fewer than min_pages should not be filtered."""
        page_records = {
            0: [{"PERSON": "Alice", "DATE_OF_BIRTH": "01/01/2020"}],
            1: [{"PERSON": "Bob", "DATE_OF_BIRTH": "01/01/2020"}],
            2: [{"PERSON": "Carol", "DATE_OF_BIRTH": "01/01/2020"}],
        }
        cleaned, removed = filter_static_values(page_records, min_pages=5)
        assert cleaned == page_records
        assert removed == {}

    def test_static_dob_removed(self):
        """A DOB appearing on >50% of pages should be removed."""
        page_records = {
            i: [{"PERSON": f"Person{i}", "DATE_OF_BIRTH": "03/15/2026"}]
            for i in range(10)
        }
        cleaned, removed = filter_static_values(page_records, threshold=0.5, min_pages=5)
        assert "DATE_OF_BIRTH" in removed
        assert "03/15/2026" in removed["DATE_OF_BIRTH"]
        # All records should still exist (PERSON is preserved)
        assert len(cleaned) == 10
        for recs in cleaned.values():
            assert recs[0].get("DATE_OF_BIRTH") is None or "DATE_OF_BIRTH" not in recs[0]

    def test_person_filtered_at_high_threshold(self):
        """PERSON is now filtered at 80% threshold (was never-filtered before quality fix)."""
        page_records = {
            i: [{"PERSON": "Same Name", "US_SSN": "123-45-6789"}]
            for i in range(10)
        }
        cleaned, removed = filter_static_values(page_records)
        # Same Name on 100% of pages → filtered (above 80% threshold)
        assert "PERSON" in removed
        # US_SSN is still never-filtered
        assert "US_SSN" not in removed

    def test_ssn_never_filtered(self):
        """US_SSN and GOVERNMENT_ID should never be filtered."""
        page_records = {
            i: [{"US_SSN": "999-88-7777", "PERSON": f"P{i}"}]
            for i in range(10)
        }
        cleaned, removed = filter_static_values(page_records)
        assert "US_SSN" not in removed

    def test_phone_static_removed(self):
        """A company phone number on every page should be removed."""
        page_records = {
            i: [{"PERSON": f"Person{i}", "PHONE_NUMBER": "(800) 555-0100"}]
            for i in range(20)
        }
        cleaned, removed = filter_static_values(page_records)
        assert "PHONE_NUMBER" in removed
        assert "(800) 555-0100" in removed["PHONE_NUMBER"]

    def test_mixed_values_only_static_removed(self):
        """Only the static value is removed; unique values stay."""
        page_records = {}
        for i in range(10):
            dob = "03/15/2026" if i < 8 else f"01/{i}/1990"  # 8/10 = 80% static
            page_records[i] = [{"PERSON": f"P{i}", "DATE_OF_BIRTH": dob}]
        cleaned, removed = filter_static_values(page_records, threshold=0.5, min_pages=5)
        assert "DATE_OF_BIRTH" in removed
        # Pages 8 and 9 should still have their unique DOBs
        assert "DATE_OF_BIRTH" in cleaned[8][0]
        assert "DATE_OF_BIRTH" in cleaned[9][0]

    def test_empty_input(self):
        cleaned, removed = filter_static_values({})
        assert cleaned == {}
        assert removed == {}

    def test_record_dropped_if_only_static_fields(self):
        """Records with only filterable fields should be dropped entirely."""
        page_records = {
            i: [{"DATE_OF_BIRTH": "03/15/2026", "PHONE_NUMBER": "555-0100"}]
            for i in range(10)
        }
        cleaned, removed = filter_static_values(page_records, threshold=0.5, min_pages=5)
        # No PERSON or SSN → records should be empty after filtering
        assert len(cleaned) == 0

    def test_threshold_edge(self):
        """Value appearing on exactly 50% should be filtered at threshold=0.5."""
        page_records = {
            i: [{"PERSON": f"P{i}", "EMAIL_ADDRESS": "report@co.com" if i < 5 else f"p{i}@mail.com"}]
            for i in range(10)
        }
        cleaned, removed = filter_static_values(page_records, threshold=0.5, min_pages=5)
        assert "EMAIL_ADDRESS" in removed
        assert "report@co.com" in removed["EMAIL_ADDRESS"]


class TestStaticFilterNeverFilterSet:
    """Verify _NEVER_FILTER contents."""

    def test_person_not_in_never_filter(self):
        """PERSON was moved out of _NEVER_FILTER (quality fix — filtered at 80%)."""
        assert "PERSON" not in _NEVER_FILTER

    def test_ssn_in_never_filter(self):
        assert "US_SSN" in _NEVER_FILTER

    def test_gov_id_in_never_filter(self):
        assert "GOVERNMENT_ID" in _NEVER_FILTER


class TestStaticFilterLikelyStatic:
    """Verify _LIKELY_STATIC contents."""

    def test_dob_in_likely_static(self):
        assert "DATE_OF_BIRTH" in _LIKELY_STATIC

    def test_phone_in_likely_static(self):
        assert "PHONE_NUMBER" in _LIKELY_STATIC

    def test_email_in_likely_static(self):
        assert "EMAIL_ADDRESS" in _LIKELY_STATIC


# ---------------------------------------------------------------------------
# template_cache tests
# ---------------------------------------------------------------------------

from app.pipeline.template_cache import TemplateCache, _STRIP_PATTERNS


class TestTemplateCacheKeyComputation:
    """Test fingerprint computation."""

    def test_same_structure_same_key(self):
        """Two PDFs with same labels but different values → same key."""
        cache = TemplateCache()
        # Create two temp PDFs with same structure but different values
        import fitz
        doc1 = fitz.open()
        page1 = doc1.new_page()
        page1.insert_text((50, 50), "Client: John Smith\nTax No: 123-45-6789\nDOB: 01/15/1980")
        path1 = tempfile.mktemp(suffix=".pdf")
        doc1.save(path1)
        doc1.close()

        doc2 = fitz.open()
        page2 = doc2.new_page()
        page2.insert_text((50, 50), "Client: Jane Doe\nTax No: 987-65-4321\nDOB: 03/22/1975")
        path2 = tempfile.mktemp(suffix=".pdf")
        doc2.save(path2)
        doc2.close()

        key1 = cache._compute_key(path1, 0)
        key2 = cache._compute_key(path2, 0)
        assert key1 is not None
        assert key2 is not None
        assert key1 == key2

    def test_different_structure_different_key(self):
        """PDFs with different labels → different key."""
        cache = TemplateCache()
        import fitz

        doc1 = fitz.open()
        page1 = doc1.new_page()
        page1.insert_text((50, 50), "Client: John Smith\nTax No: 123-45-6789")
        path1 = tempfile.mktemp(suffix=".pdf")
        doc1.save(path1)
        doc1.close()

        doc2 = fitz.open()
        page2 = doc2.new_page()
        page2.insert_text((50, 50), "Employee Name: Jane Doe\nSocial Security: 987-65-4321\nDepartment: Engineering")
        path2 = tempfile.mktemp(suffix=".pdf")
        doc2.save(path2)
        doc2.close()

        key1 = cache._compute_key(path1, 0)
        key2 = cache._compute_key(path2, 0)
        assert key1 != key2

    def test_short_text_returns_none(self):
        """Pages with <50 chars of text should not be cached."""
        cache = TemplateCache()
        import fitz
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((50, 50), "Hi")
        path = tempfile.mktemp(suffix=".pdf")
        doc.save(path)
        doc.close()
        assert cache._compute_key(path, 0) is None

    def test_invalid_page_returns_none(self):
        cache = TemplateCache()
        import fitz
        doc = fitz.open()
        doc.new_page()
        path = tempfile.mktemp(suffix=".pdf")
        doc.save(path)
        doc.close()
        assert cache._compute_key(path, 99) is None

    def test_invalid_path_returns_none(self):
        cache = TemplateCache()
        assert cache._compute_key("/nonexistent/file.pdf", 0) is None


class TestTemplateCacheHitMiss:
    """Test cache get/put logic."""

    def test_miss_then_hit(self):
        cache = TemplateCache()
        import fitz
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((50, 50), "Client: John Smith\nAccount No: 12345\nDate of Birth: 01/15/1980\nAddress: 123 Main St")
        path = tempfile.mktemp(suffix=".pdf")
        doc.save(path)
        doc.close()

        assert cache.get(path, 0) is None

        routing_dict = {
            "structure_type": "fixed_single_page",
            "recommended_path": "coordinate",
            "pii_fields": [{"type": "PERSON", "value": "John Smith"}],
        }
        fm_dicts = [{"field_type": "PERSON", "anchor_text": "Client:", "spatial_relationship": "same_line_right"}]
        cache.put(path, 0, routing_dict, fm_dicts, ["John Smith"])

        entry = cache.get(path, 0)
        assert entry is not None
        assert entry.routing_dict["structure_type"] == "fixed_single_page"
        assert entry.field_map_dicts == fm_dicts
        assert entry.hit_count == 1

    def test_hit_count_increments(self):
        cache = TemplateCache()
        import fitz
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((50, 50), "Client: X\nTax No: 000-00-0000\nDOB: 01/01/2000\nAddr: 1 Street City ST")
        path = tempfile.mktemp(suffix=".pdf")
        doc.save(path)
        doc.close()

        cache.put(path, 0, {"structure_type": "fixed_single_page"}, None, [])
        cache.get(path, 0)
        cache.get(path, 0)
        entry = cache.get(path, 0)
        assert entry.hit_count == 3

    def test_eviction_at_max_capacity(self):
        cache = TemplateCache(max_entries=2)
        import fitz

        paths = []
        for i in range(3):
            doc = fitz.open()
            page = doc.new_page()
            # Use different structural text for each so keys differ
            page.insert_text((50, 50), f"{'Label' * 20}_{i}: Value\nField{i}: Data\nExtra{i}: More data here please")
            path = tempfile.mktemp(suffix=".pdf")
            doc.save(path)
            doc.close()
            paths.append(path)

        cache.put(paths[0], 0, {"id": 0}, None, [])
        cache.put(paths[1], 0, {"id": 1}, None, [])
        assert cache.size == 2

        cache.put(paths[2], 0, {"id": 2}, None, [])
        assert cache.size == 2  # Evicted one


class TestTemplateCacheStats:
    def test_stats_empty(self):
        cache = TemplateCache()
        s = cache.stats()
        assert s["entries"] == 0
        assert s["total_hits"] == 0

    def test_clear(self):
        cache = TemplateCache()
        import fitz
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((50, 50), "Client: X\nTax No: 000-00-0000\nAddress: 1 Street, City, ST 12345")
        path = tempfile.mktemp(suffix=".pdf")
        doc.save(path)
        doc.close()

        cache.put(path, 0, {}, None, [])
        assert cache.size == 1
        cache.clear()
        assert cache.size == 0


class TestStripPatterns:
    """Verify variable content gets stripped before hashing."""

    def test_ssn_stripped(self):
        text = "Tax No: 123-45-6789"
        for pat in _STRIP_PATTERNS:
            text = pat.sub("___", text)
        assert "123-45-6789" not in text

    def test_date_stripped(self):
        text = "DOB: 01/15/1980"
        for pat in _STRIP_PATTERNS:
            text = pat.sub("___", text)
        assert "01/15/1980" not in text

    def test_dollar_amount_stripped(self):
        text = "Balance: $1,234.56"
        for pat in _STRIP_PATTERNS:
            text = pat.sub("___", text)
        assert "$1,234.56" not in text

    def test_mixed_case_name_stripped(self):
        text = "Client: John Smith"
        for pat in _STRIP_PATTERNS:
            text = pat.sub("___", text)
        assert "John Smith" not in text


# ---------------------------------------------------------------------------
# two_phase.py integration verification (import-level checks)
# ---------------------------------------------------------------------------

class TestTwoPhaseImports:
    """Verify the wiring exists in two_phase.py source code."""

    @pytest.fixture(autouse=True)
    def _load_source(self):
        import pathlib
        self.src = (pathlib.Path(__file__).parent.parent / "app" / "pipeline" / "two_phase.py").read_text()

    def test_static_filter_import_present(self):
        assert "from app.pipeline.static_filter import filter_static_values" in self.src

    def test_template_cache_import_present(self):
        assert "from app.pipeline.template_cache import TemplateCache" in self.src

    def test_template_cache_hit_flag_in_metadata(self):
        assert '"template_cache_hit"' in self.src

    def test_vision_routing_result_import(self):
        assert "VisionRoutingResult" in self.src

    def test_static_filter_before_records_assignment(self):
        """Static filter should appear BEFORE 'records = coord_records' in normal path."""
        # Find the normal-path (non-large-doc) filter_static_values
        # The large-doc block also uses filter_static_values, so find the one after "NORMAL EXTRACTION"
        normal_start = self.src.index("NORMAL EXTRACTION PATHS")
        filter_pos = self.src.index("filter_static_values", normal_start)
        assign_pos = self.src.index("records = coord_records", normal_start)
        assert filter_pos < assign_pos

    def test_template_cache_before_analyze_document(self):
        """Template cache check should appear BEFORE router.analyze_document."""
        cache_pos = self.src.index("template_cache.get(")
        analyze_pos = self.src.index("router.analyze_document(")
        assert cache_pos < analyze_pos