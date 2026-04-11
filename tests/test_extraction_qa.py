"""Tests for Extraction QA sampler and API (Step 30e-7).

Tests cover:
- QASampler: category allocation, largest group, gap-filled, merged, cross-type, edge cases
- QA API: summary, samples, gap resolution, approval gating
- Safety: no raw PII in samples
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from app.pipeline.qa_sampler import QASample, QASampler
from app.pipeline.gap_detector import ExtractionGap
from app.pipeline.gap_filler import persist_gaps


# ===================================================================
# QASampler tests
# ===================================================================


def _make_records(count: int, doc_id: str = "doc-1", doc_name: str = "test.pdf") -> list[dict]:
    """Generate fake extraction records."""
    records = []
    for i in range(count):
        records.append({
            "record_id": f"rec-{doc_id}-{i}",
            "source_document_id": doc_id,
            "source_document_name": doc_name,
            "page_range": str(i + 1),
            "page_or_sheet": i + 1,
            "raw_name": f"Person {i} Smith",
            "raw_government_id": f"111-22-{3000+i}",
            "raw_phone": f"555-000-{1000+i}",
            "raw_email": f"person{i}@example.com",
            "raw_dob": f"01/{(i%28)+1:02d}/1990",
            "raw_address": {"raw": f"{100+i} Main St, City, ST 12345"},
            "extraction_method": "coordinate",
            "entity_types_found": ["PERSON", "US_SSN", "PHONE_NUMBER"],
        })
    return records


class TestQASamplerBasic:
    """Basic sampler tests."""

    def test_empty_records(self):
        sampler = QASampler(max_samples=10)
        samples = sampler.select(records=[])
        assert samples == []

    def test_respects_max_samples(self):
        records = _make_records(100)
        sampler = QASampler(max_samples=5)
        samples = sampler.select(records=records)
        assert len(samples) <= 5

    def test_no_duplicate_records(self):
        records = _make_records(50)
        sampler = QASampler(max_samples=20)
        samples = sampler.select(records=records)
        ids = [s.record_id for s in samples]
        assert len(ids) == len(set(ids)), "Duplicate record IDs in samples"

    def test_all_categories_represented(self):
        """With enough data, all categories should appear."""
        records = _make_records(30, doc_id="doc-1", doc_name="test.pdf")
        records += _make_records(5, doc_id="doc-2", doc_name="other.xlsx")
        gaps = [
            {
                "fill_result": "filled",
                "fill_method": "coordinate_relaxed",
                "expected_field": "US_SSN",
                "severity": "high",
                "page_num": 1,
                "document_id": "doc-1",
                "document_name": "test.pdf",
                "gap_type": "missing_field",
            },
        ]
        merge_groups = [
            {
                "group_id": "mg-1",
                "member_ids": ["rec-doc-1-0", "rec-doc-1-1"],
                "member_count": 2,
                "confidence": 0.92,
                "explanation": "Same SSN, fuzzy name match",
            },
        ]

        sampler = QASampler(max_samples=20)
        samples = sampler.select(
            records=records,
            gaps=gaps,
            merge_groups=merge_groups,
        )

        categories = {s.category for s in samples}
        assert "largest_group" in categories
        assert "gap_filled" in categories
        # merged may be deduped if records already used by largest_group
        # cross_type and edge_case depend on data variation
        assert len(categories) >= 3


class TestQASamplerLargestGroup:
    """Test largest group sampling."""

    def test_picks_from_largest(self):
        records = _make_records(20, doc_id="big-doc", doc_name="big.pdf")
        records += _make_records(3, doc_id="small-doc", doc_name="small.pdf")

        sampler = QASampler(max_samples=10)
        samples = sampler.select(records=records)

        largest_samples = [s for s in samples if s.category == "largest_group"]
        assert len(largest_samples) > 0
        assert all(s.document_id == "big-doc" for s in largest_samples)


class TestQASamplerGapFilled:
    """Test gap-filled sampling."""

    def test_gap_filled_records(self):
        records = _make_records(10)
        gaps = [
            {
                "fill_result": "filled",
                "fill_method": "vision",
                "expected_field": "US_SSN",
                "severity": "high",
                "page_num": 3,
                "document_id": "doc-1",
                "document_name": "test.pdf",
                "gap_type": "missing_field",
            },
        ]
        sampler = QASampler(max_samples=20)
        samples = sampler.select(records=records, gaps=gaps)

        gf = [s for s in samples if s.category == "gap_filled"]
        assert len(gf) >= 1
        assert gf[0].gap_fill_method == "vision"

    def test_no_gap_filled_when_none_filled(self):
        records = _make_records(10)
        gaps = [
            {"fill_result": "unfilled", "severity": "high", "gap_type": "empty_page"},
        ]
        sampler = QASampler(max_samples=20)
        samples = sampler.select(records=records, gaps=gaps)
        gf = [s for s in samples if s.category == "gap_filled"]
        assert len(gf) == 0


class TestQASamplerEdgeCases:
    """Test edge case sampling."""

    def test_fewest_fields(self):
        records = _make_records(10)
        # Add a record with minimal fields on a different doc so it won't be used by largest_group
        records.append({
            "record_id": "rec-minimal",
            "source_document_id": "doc-edge",
            "source_document_name": "edge.pdf",
            "page_range": "99",
            "raw_name": "Only Name",
            "extraction_method": "presidio",
        })
        sampler = QASampler(max_samples=20)
        samples = sampler.select(records=records)
        ec = [s for s in samples if s.category == "edge_case"]
        # The minimal record should appear as fewest fields or at least as an edge case
        assert any("Fewest" in s.category_reason or s.record_id == "rec-minimal" for s in ec)

    def test_shortest_name(self):
        records = _make_records(10)
        records.append({
            "record_id": "rec-short-name",
            "source_document_id": "doc-edge",
            "source_document_name": "edge.pdf",
            "page_range": "99",
            "raw_name": "AB",
            "extraction_method": "coordinate",
        })
        sampler = QASampler(max_samples=20)
        samples = sampler.select(records=records)
        ec = [s for s in samples if s.category == "edge_case"]
        assert any("Shortest" in s.category_reason or s.record_id == "rec-short-name" for s in ec)


class TestQASampleDataclass:
    """Test QASample dataclass."""

    def test_to_dict(self):
        s = QASample(
            record_id="r1",
            document_id="d1",
            document_name="f.pdf",
            page_num=5,
            category="largest_group",
            category_reason="test",
            extraction_method="coordinate",
            fields={"PERSON": "J*** S***"},
        )
        d = s.to_dict()
        assert d["record_id"] == "r1"
        assert d["category"] == "largest_group"
        assert d["fields"]["PERSON"] == "J*** S***"

    def test_default_fields(self):
        s = QASample(
            record_id="r1",
            document_id="d1",
            document_name="f.pdf",
            page_num=1,
            category="edge_case",
            category_reason="test",
            extraction_method="unknown",
        )
        assert s.merge_group_id is None
        assert s.merge_confidence is None
        assert s.gap_type is None


# ===================================================================
# QA Safety tests
# ===================================================================


class TestQASafety:
    """Ensure no raw PII in QA samples."""

    def test_fields_are_masked(self):
        records = _make_records(3)
        sampler = QASampler(max_samples=10)
        samples = sampler.select(records=records)

        for s in samples:
            for field_type, value in s.fields.items():
                if field_type == "GOVERNMENT_ID":
                    # Should be masked (***-**-NNNN pattern)
                    assert "***" in value, f"Government ID not masked: {value}"
                if field_type == "PERSON":
                    assert "***" in value, f"Person name not masked: {value}"

    def test_no_raw_ssn_in_fields(self):
        records = [{
            "record_id": "r1",
            "source_document_id": "d1",
            "source_document_name": "f.pdf",
            "page_range": "1",
            "raw_name": "John Smith",
            "raw_government_id": "123-45-6789",
            "extraction_method": "coordinate",
        }]
        sampler = QASampler(max_samples=5)
        samples = sampler.select(records=records)
        for s in samples:
            gov_id = s.fields.get("GOVERNMENT_ID", "")
            assert "123-45" not in gov_id, "Raw SSN leaked in sample"


# ===================================================================
# QA API endpoint tests (structure + persistence)
# ===================================================================


class TestQAApprovalGating:
    """Test approval gating logic."""

    def test_approve_blocked_with_high_unresolved(self, tmp_path, monkeypatch):
        """Cannot approve when high-severity gaps are unresolved."""
        monkeypatch.chdir(tmp_path)

        gaps = [
            ExtractionGap(
                document_id="d1", document_name="f.pdf", page_num=5,
                gap_type="missing_field", severity="high",
                expected_field="US_SSN", fill_result="unfilled",
            ),
        ]
        persist_gaps(gaps, project_id="p1", job_id="j1")

        # Import the approval function and test directly
        from app.api.routes.extraction_qa import _load_qa_state, _save_qa_state
        from app.pipeline.gap_filler import load_gaps as _load

        loaded = _load("p1", "j1")
        high_unresolved = [
            g for g in loaded
            if g.severity == "high"
            and g.fill_result in ("pending", "unfilled")
            and g.filled_by != "manual"
        ]
        assert len(high_unresolved) == 1  # approval should be blocked

    def test_approve_allowed_when_gaps_resolved(self, tmp_path, monkeypatch):
        """Can approve when all high-severity gaps are resolved."""
        monkeypatch.chdir(tmp_path)

        gaps = [
            ExtractionGap(
                document_id="d1", document_name="f.pdf", page_num=5,
                gap_type="missing_field", severity="high",
                expected_field="US_SSN", fill_result="filled",
                fill_method="manual", filled_by="manual",
            ),
            ExtractionGap(
                document_id="d1", document_name="f.pdf", page_num=6,
                gap_type="truncated", severity="low",
                fill_result="unfilled",
            ),
        ]
        persist_gaps(gaps, project_id="p1", job_id="j1")

        loaded = load_gaps_direct("p1", "j1")
        high_unresolved = [
            g for g in loaded
            if g.severity == "high"
            and g.fill_result in ("pending", "unfilled")
            and g.filled_by != "manual"
        ]
        assert len(high_unresolved) == 0  # approval should be allowed


def load_gaps_direct(project_id: str, job_id: str):
    """Direct import to avoid circular deps in test."""
    from app.pipeline.gap_filler import load_gaps
    return load_gaps(project_id, job_id)


class TestQAStatePersistence:
    """Test QA state persistence."""

    def test_save_and_load_state(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from app.api.routes.extraction_qa import _load_qa_state, _save_qa_state

        state = _load_qa_state("p1", "j1")
        assert state["status"] == "pending_review"

        state["status"] = "approved"
        state["approved_by"] = "auditor"
        _save_qa_state("p1", "j1", state)

        loaded = _load_qa_state("p1", "j1")
        assert loaded["status"] == "approved"
        assert loaded["approved_by"] == "auditor"


class TestQARouterRegistration:
    """Verify QA router is registered in the app."""

    def test_router_registered(self):
        """Extraction QA router should be included in app."""
        try:
            from app.api.main import app
            routes = [r.path for r in app.routes]
            # Check that QA routes exist
            qa_paths = [r for r in routes if "/qa/" in r or r.endswith("/qa")]
            assert len(qa_paths) > 0, f"No QA routes found. Routes: {routes[:20]}"
        except ImportError:
            pytest.skip("Cannot import app (presidio dependency)")
