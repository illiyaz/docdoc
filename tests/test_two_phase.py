"""Tests for two-phase pipeline: content onset, auto-approve, verified onset, entity groups.

Tests across classes:
- TestFindContentOnsetFromBlocks (7 tests)
- TestFilterSampleBlocks (4 tests)
- TestShouldAutoApprove (10 tests)
- TestGetHeuristicCandidatePages (4 tests)
- TestFindVerifiedOnset (7 tests)
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from app.readers.base import ExtractedBlock
from app.pipeline.content_onset import (
    _get_heuristic_candidate_pages,
    find_content_onset_from_blocks,
    find_verified_onset,
    filter_sample_blocks,
)
from app.pipeline.auto_approve import should_auto_approve


def _block(text: str, page: int | str = 0, file_type: str = "pdf") -> ExtractedBlock:
    """Helper to create an ExtractedBlock with minimal required fields."""
    return ExtractedBlock(
        text=text,
        page_or_sheet=page,
        source_path="/test",
        file_type=file_type,
    )


# ---------------------------------------------------------------------------
# TestFindContentOnsetFromBlocks
# ---------------------------------------------------------------------------
class TestFindContentOnsetFromBlocks:
    """Tests for find_content_onset_from_blocks()."""

    def test_csv_always_returns_onset_zero(self):
        """CSV files always return onset 0 regardless of block content."""
        blocks = [
            _block("some header", page=0, file_type="csv"),
            _block("Name: John SSN: 123-45-6789", page=1, file_type="csv"),
        ]
        assert find_content_onset_from_blocks(blocks, "csv") == 0

    def test_xlsx_always_returns_onset_zero(self):
        """XLSX files always return onset 0 regardless of block content."""
        blocks = [
            _block("cover page", page=0, file_type="xlsx"),
            _block("SSN: 123-45-6789", page=1, file_type="xlsx"),
        ]
        assert find_content_onset_from_blocks(blocks, "xlsx") == 0

    def test_pdf_finds_onset_signal_on_correct_page(self):
        """PDF onset detection finds the first page with a signal pattern."""
        blocks = [
            _block("Table of Contents", page=0),
            _block("Legal Disclaimer", page=1),
            _block("Name: John Doe, SSN: 123-45-6789", page=2),
            _block("More data here", page=3),
        ]
        result = find_content_onset_from_blocks(blocks, "pdf")
        assert result == 2

    def test_pdf_returns_zero_when_no_signals(self):
        """PDF returns onset 0 when no blocks contain signal patterns."""
        blocks = [
            _block("Introduction to the report", page=0),
            _block("Chapter 1: Overview", page=1),
            _block("Summary of findings", page=2),
        ]
        assert find_content_onset_from_blocks(blocks, "pdf") == 0

    def test_docx_finds_onset_signal(self):
        """DOCX prose format finds onset from signal patterns in blocks."""
        blocks = [
            _block("Cover Page - Confidential", page=0, file_type="docx"),
            _block("Patient Name and Date of Birth listed below", page=1, file_type="docx"),
        ]
        result = find_content_onset_from_blocks(blocks, "docx")
        assert result == 1

    def test_html_finds_onset_signal(self):
        """HTML prose format finds onset from signal patterns in blocks."""
        blocks = [
            _block("Website header navigation", page=0, file_type="html"),
            _block("Account number: 12345", page=2, file_type="html"),
        ]
        result = find_content_onset_from_blocks(blocks, "html")
        assert result == 2

    def test_empty_blocks_returns_zero(self):
        """Empty block list returns onset 0 for any file type."""
        assert find_content_onset_from_blocks([], "pdf") == 0
        assert find_content_onset_from_blocks([], "docx") == 0
        assert find_content_onset_from_blocks([], "csv") == 0


# ---------------------------------------------------------------------------
# TestFilterSampleBlocks
# ---------------------------------------------------------------------------
class TestFilterSampleBlocks:
    """Tests for filter_sample_blocks()."""

    def test_pdf_filter_returns_only_onset_page_blocks(self):
        """PDF filtering returns all blocks on the onset page only."""
        blocks = [
            _block("page 0 content", page=0),
            _block("page 1 block A", page=1),
            _block("page 1 block B", page=1),
            _block("page 2 content", page=2),
        ]
        result = filter_sample_blocks(blocks, onset_page=1, file_type="pdf")
        assert len(result) == 2
        assert all(b.page_or_sheet == 1 for b in result)

    def test_csv_filter_limits_to_max_tabular_rows(self):
        """CSV filtering respects max_tabular_rows limit."""
        blocks = [_block(f"row {i}", page=0, file_type="csv") for i in range(100)]
        result = filter_sample_blocks(
            blocks, onset_page=0, file_type="csv", max_tabular_rows=10
        )
        assert len(result) == 10

    def test_docx_filter_limits_to_max_prose_blocks(self):
        """DOCX filtering respects max_prose_blocks limit."""
        blocks = [_block(f"para {i}", page=0, file_type="docx") for i in range(50)]
        result = filter_sample_blocks(
            blocks, onset_page=0, file_type="docx", max_prose_blocks=5
        )
        assert len(result) == 5

    def test_prose_starts_from_onset_page(self):
        """Prose filtering only includes blocks from onset page onward."""
        blocks = [
            _block("before onset", page=0, file_type="docx"),
            _block("still before", page=1, file_type="docx"),
            _block("onset block A", page=2, file_type="docx"),
            _block("onset block B", page=2, file_type="docx"),
            _block("after onset", page=3, file_type="docx"),
        ]
        result = filter_sample_blocks(
            blocks, onset_page=2, file_type="docx", max_prose_blocks=20
        )
        assert len(result) == 3
        assert result[0].text == "onset block A"
        assert result[1].text == "onset block B"
        assert result[2].text == "after onset"


# ---------------------------------------------------------------------------
# TestShouldAutoApprove
# ---------------------------------------------------------------------------
class TestShouldAutoApprove:
    """Tests for should_auto_approve()."""

    def test_high_confidence_auto_approves(self):
        """High average confidence scores result in auto-approval."""
        approved, reason = should_auto_approve([0.95, 0.90, 0.92])
        assert approved is True
        assert "auto-approved" in reason

    def test_low_confidence_rejects(self):
        """Low average confidence scores are rejected."""
        approved, reason = should_auto_approve([0.50, 0.60, 0.55])
        assert approved is False
        assert "below threshold" in reason

    def test_no_extractions_rejects(self):
        """Empty confidence list is rejected (no PII found)."""
        approved, reason = should_auto_approve([])
        assert approved is False
        assert "no PII entities found" in reason

    def test_too_few_entities_rejects(self):
        """Fewer entities than min_sample_entities threshold is rejected."""
        approved, reason = should_auto_approve([0.95, 0.90])
        assert approved is False
        assert "only 2 entities found" in reason

    def test_disabled_config_rejects(self):
        """Auto-approve disabled in config always rejects."""
        config = {"auto_approve": {"enabled": False}}
        approved, reason = should_auto_approve(
            [0.99, 0.99, 0.99, 0.99], protocol_config=config
        )
        assert approved is False
        assert "disabled" in reason

    def test_protocol_override_rejects(self):
        """Protocol listed in require_review_for_protocols is rejected."""
        config = {
            "auto_approve": {
                "require_review_for_protocols": ["hipaa_breach_rule"],
            }
        }
        approved, reason = should_auto_approve(
            [0.99, 0.99, 0.99],
            protocol_config=config,
            base_protocol_id="hipaa_breach_rule",
        )
        assert approved is False
        assert "requires human review" in reason

    def test_protocol_not_in_override_list_approves(self):
        """Protocol NOT in require_review_for_protocols can be auto-approved."""
        config = {
            "auto_approve": {
                "require_review_for_protocols": ["hipaa_breach_rule"],
            }
        }
        approved, reason = should_auto_approve(
            [0.95, 0.90, 0.92],
            protocol_config=config,
            base_protocol_id="ccpa",
        )
        assert approved is True
        assert "auto-approved" in reason

    def test_custom_threshold_works(self):
        """Custom min_confidence threshold is respected."""
        config = {"auto_approve": {"min_confidence": 0.70}}
        approved, reason = should_auto_approve(
            [0.75, 0.80, 0.72], protocol_config=config
        )
        assert approved is True
        assert "auto-approved" in reason

    def test_none_config_uses_defaults(self):
        """None protocol_config uses default thresholds."""
        approved, reason = should_auto_approve(
            [0.90, 0.95, 0.88], protocol_config=None
        )
        assert approved is True
        assert "auto-approved" in reason

    def test_empty_config_uses_defaults(self):
        """Empty dict protocol_config uses default thresholds."""
        approved, reason = should_auto_approve(
            [0.90, 0.95, 0.88], protocol_config={}
        )
        assert approved is True
        assert "auto-approved" in reason


# ---------------------------------------------------------------------------
# TestGetHeuristicCandidatePages
# ---------------------------------------------------------------------------
class TestGetHeuristicCandidatePages:
    """Tests for _get_heuristic_candidate_pages()."""

    def test_finds_pages_with_onset_signals(self):
        """Returns pages that contain onset signal keywords."""
        blocks = [
            _block("Cover page - Disclaimer", page=0),
            _block("Table of Contents", page=1),
            _block("Name: John Doe SSN: 123-45-6789", page=2),
            _block("Account details follow", page=3),
        ]
        result = _get_heuristic_candidate_pages(blocks)
        assert 2 in result
        assert 3 in result

    def test_returns_at_most_five_candidates(self):
        """Returns at most 5 candidate pages even if more match."""
        blocks = [
            _block(f"Name on page {i}", page=i) for i in range(10)
        ]
        result = _get_heuristic_candidate_pages(blocks)
        assert len(result) <= 5

    def test_returns_empty_for_no_signals(self):
        """Returns empty list when no blocks contain onset signals."""
        blocks = [
            _block("Introduction", page=0),
            _block("Chapter 1", page=1),
        ]
        assert _get_heuristic_candidate_pages(blocks) == []

    def test_deduplicates_pages(self):
        """Returns distinct pages even if multiple blocks on same page match."""
        blocks = [
            _block("Name: Alice", page=0),
            _block("SSN: 123-45-6789", page=0),
            _block("Address: 123 Main St", page=1),
        ]
        result = _get_heuristic_candidate_pages(blocks)
        assert result.count(0) == 1


# ---------------------------------------------------------------------------
# TestFindVerifiedOnset
# ---------------------------------------------------------------------------


class _FakeDetection:
    """Minimal detection mock with a score attribute."""
    def __init__(self, score: float):
        self.score = score


class _MockPresidioEngine:
    """Mock engine that returns PII detections for specific page texts."""

    def __init__(self, pii_pages: set[int | str]):
        """pii_pages: set of page_or_sheet values that should yield PII detections."""
        self.pii_pages = pii_pages
        self.analyzed_pages: list[int | str] = []

    def analyze(self, blocks: list[ExtractedBlock]) -> list:
        pages = {b.page_or_sheet for b in blocks}
        self.analyzed_pages.extend(pages)
        results = []
        for b in blocks:
            if b.page_or_sheet in self.pii_pages:
                results.append(_FakeDetection(score=0.95))
        return results


class TestFindVerifiedOnset:
    """Tests for find_verified_onset() — two-pass PII verification."""

    def test_tabular_always_returns_zero(self):
        """CSV/Excel always returns 0 regardless of content."""
        engine = _MockPresidioEngine(set())
        blocks = [_block("Name: John", page=0, file_type="csv")]
        assert find_verified_onset(blocks, "csv", engine) == 0

    def test_empty_blocks_returns_zero(self):
        """Empty block list returns 0."""
        engine = _MockPresidioEngine(set())
        assert find_verified_onset([], "pdf", engine) == 0

    def test_heuristic_candidate_confirmed_by_presidio(self):
        """When heuristic finds a candidate and Presidio confirms PII, returns that page."""
        blocks = [
            _block("Cover page", page=0),
            _block("Legal disclaimer", page=1),
            _block("Name: John Doe SSN: 123-45-6789", page=2),
            _block("More data", page=3),
        ]
        engine = _MockPresidioEngine(pii_pages={2})
        result = find_verified_onset(blocks, "pdf", engine)
        assert result == 2

    def test_heuristic_candidate_not_confirmed_falls_to_next_page(self):
        """When heuristic page has no real PII but next page does, returns next page."""
        blocks = [
            _block("Cover page", page=0),
            # Page 1 has keyword "account" in legal text but no real PII
            _block("This account overview is for reference only", page=1),
            _block("John Doe 123-45-6789", page=2),  # actual PII here
        ]
        engine = _MockPresidioEngine(pii_pages={2})
        result = find_verified_onset(blocks, "pdf", engine)
        assert result == 2

    def test_no_heuristic_matches_sequential_scan_finds_pii(self):
        """When no onset signals match, sequential scan finds the first page with PII."""
        blocks = [
            _block("Random text no keywords", page=0),
            _block("Still no keywords here", page=1),
            _block("This page has real data", page=2),
        ]
        # Page 2 has PII but no onset signal keywords
        engine = _MockPresidioEngine(pii_pages={2})
        result = find_verified_onset(blocks, "pdf", engine)
        assert result == 2

    def test_pii_on_late_page(self):
        """PII that starts on a late page (e.g. page 5) is correctly identified."""
        blocks = [
            _block("Cover", page=0),
            _block("TOC", page=1),
            _block("Disclaimer", page=2),
            _block("Introduction", page=3),
            _block("Background", page=4),
            _block("Name: John Doe SSN 123-45-6789", page=5),
            _block("More data", page=6),
        ]
        engine = _MockPresidioEngine(pii_pages={5})
        result = find_verified_onset(blocks, "pdf", engine)
        assert result == 5

    def test_no_pii_anywhere_returns_zero(self):
        """When no PII is found on any page, returns 0."""
        blocks = [
            _block("Introduction", page=0),
            _block("Overview", page=1),
            _block("Summary", page=2),
        ]
        engine = _MockPresidioEngine(pii_pages=set())
        result = find_verified_onset(blocks, "pdf", engine)
        assert result == 0


# ---------------------------------------------------------------------------
# TestEntityGroups
# ---------------------------------------------------------------------------
class TestEntityGroups:
    """Tests for entity group data models serialization."""

    def test_entity_group_roundtrip(self):
        """EntityGroup serializes to dict and back."""
        from app.structure.entity_groups import EntityGroup, EntityGroupMember

        member = EntityGroupMember(pii_type="US_SSN", value_ref="***-**-6789", page=3, confidence=0.95)
        group = EntityGroup(
            group_id="G1",
            label="John Smith (Employee)",
            role="primary_subject",
            confidence=0.92,
            members=[member],
            rationale="Name and SSN on same row",
            detected_by="llm",
        )

        d = group.to_dict()
        assert d["group_id"] == "G1"
        assert d["role"] == "primary_subject"
        assert len(d["members"]) == 1
        assert d["members"][0]["pii_type"] == "US_SSN"

        restored = EntityGroup.from_dict(d)
        assert restored.group_id == "G1"
        assert restored.label == "John Smith (Employee)"
        assert len(restored.members) == 1
        assert restored.members[0].value_ref == "***-**-6789"

    def test_entity_relationship_analysis_roundtrip(self):
        """EntityRelationshipAnalysis serializes to dict and back."""
        from app.structure.entity_groups import (
            EntityGroup, EntityGroupMember, EntityRelationship,
            EntityRelationshipAnalysis,
        )

        analysis = EntityRelationshipAnalysis(
            document_id="doc-123",
            document_summary="Payroll records for 2 employees",
            entity_groups=[
                EntityGroup(
                    group_id="G1", label="Alice (Employee)", role="primary_subject",
                    confidence=0.9, members=[
                        EntityGroupMember(pii_type="PERSON", value_ref="Alice", page=1),
                    ], rationale="Name found on page 1",
                ),
            ],
            relationships=[
                EntityRelationship(from_group="G1", to_group="G2", relationship_type="employed_by", confidence=0.95),
            ],
            estimated_unique_individuals=2,
            extraction_guidance="One employee per page",
        )

        d = analysis.to_dict()
        assert d["estimated_unique_individuals"] == 2
        assert len(d["entity_groups"]) == 1
        assert len(d["relationships"]) == 1

        restored = EntityRelationshipAnalysis.from_dict(d)
        assert restored.document_summary == "Payroll records for 2 employees"
        assert restored.entity_groups[0].label == "Alice (Employee)"
        assert restored.relationships[0].relationship_type == "employed_by"

    def test_empty_analysis_roundtrip(self):
        """Empty EntityRelationshipAnalysis roundtrips correctly."""
        from app.structure.entity_groups import EntityRelationshipAnalysis

        analysis = EntityRelationshipAnalysis(document_id="doc-0", document_summary="Empty doc")
        d = analysis.to_dict()
        restored = EntityRelationshipAnalysis.from_dict(d)
        assert restored.entity_groups == []
        assert restored.relationships == []
        assert restored.estimated_unique_individuals == 0


# ---------------------------------------------------------------------------
# TestLLMEntityAnalyzer
# ---------------------------------------------------------------------------
class TestLLMEntityAnalyzer:
    """Tests for LLMEntityAnalyzer._parse_response()."""

    def test_parse_valid_llm_response(self):
        """Parser correctly handles a well-formed LLM JSON response."""
        from app.structure.llm_entity_analyzer import LLMEntityAnalyzer

        analyzer = LLMEntityAnalyzer.__new__(LLMEntityAnalyzer)
        response = json.dumps({
            "document_summary": "Payroll records for 3 employees",
            "entity_groups": [
                {
                    "group_id": "G1",
                    "label": "Kristin Aleshire (Employee)",
                    "role": "primary_subject",
                    "confidence": 0.92,
                    "members": [
                        {"pii_type": "PERSON", "value_ref": "Kristin Aleshire", "page": 3},
                        {"pii_type": "US_SSN", "value_ref": "***-**-6789", "page": 3},
                    ],
                    "rationale": "Name and SSN in same record section",
                },
                {
                    "group_id": "G2",
                    "label": "Acme Corp (Employer)",
                    "role": "institutional",
                    "confidence": 0.98,
                    "members": [
                        {"pii_type": "ORGANIZATION", "value_ref": "Acme Corp", "page": 1},
                    ],
                    "rationale": "Company name on letterhead",
                },
            ],
            "relationships": [
                {"from_group": "G1", "to_group": "G2", "relationship_type": "employed_by", "confidence": 0.95},
            ],
            "estimated_unique_individuals": 3,
            "extraction_guidance": "Each page has one employee record",
        })

        result = analyzer._parse_response(response, "doc-123")
        assert result.document_summary == "Payroll records for 3 employees"
        assert len(result.entity_groups) == 2
        assert result.entity_groups[0].group_id == "G1"
        assert result.entity_groups[0].role == "primary_subject"
        assert len(result.entity_groups[0].members) == 2
        assert result.entity_groups[1].role == "institutional"
        assert len(result.relationships) == 1
        assert result.relationships[0].relationship_type == "employed_by"
        assert result.estimated_unique_individuals == 3

    def test_parse_response_with_markdown_fences(self):
        """Parser strips markdown code fences from LLM response."""
        from app.structure.llm_entity_analyzer import LLMEntityAnalyzer

        analyzer = LLMEntityAnalyzer.__new__(LLMEntityAnalyzer)
        response = '```json\n{"document_summary": "Test", "entity_groups": [], "relationships": [], "estimated_unique_individuals": 0, "extraction_guidance": ""}\n```'

        result = analyzer._parse_response(response, "doc-0")
        assert result.document_summary == "Test"
        assert result.entity_groups == []

    def test_parse_response_invalid_role_defaults_unknown(self):
        """Invalid entity role defaults to 'unknown'."""
        from app.structure.llm_entity_analyzer import LLMEntityAnalyzer

        analyzer = LLMEntityAnalyzer.__new__(LLMEntityAnalyzer)
        response = json.dumps({
            "document_summary": "Test",
            "entity_groups": [
                {"group_id": "G1", "label": "X", "role": "banana", "confidence": 0.5, "members": [], "rationale": "test"},
            ],
            "relationships": [],
            "estimated_unique_individuals": 1,
            "extraction_guidance": "",
        })

        result = analyzer._parse_response(response, "doc-0")
        assert result.entity_groups[0].role == "unknown"
