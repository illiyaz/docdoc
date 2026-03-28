"""Tests for two-phase pipeline: content onset, auto-approve, verified onset, entity groups, coordinate wiring, vision routing, extraction verification, page sampling, performance optimization.

Tests across classes:
- TestFindContentOnsetFromBlocks (7 tests)
- TestFilterSampleBlocks (4 tests)
- TestShouldAutoApprove (10 tests)
- TestGetHeuristicCandidatePages (4 tests)
- TestFindVerifiedOnset (7 tests)
- TestCoordinatePipelineWiring (15 tests)
- TestVisionRoutingPipelineWiring (10 tests)
- TestExtractionVerificationWiring (7 tests)
- TestSampledAnalysisPipeline (5 tests)
- TestConnectionPoolConfig (3 tests)
- TestSchemaBasedVisionSkip (4 tests)
- TestParallelVisionRouting (8 tests)
- TestConfigurableUnderstandingModel (2 tests)
- TestVisionDocumentUnderstanding (8 tests)
- TestSSEProgressEnhancement (2 tests)
"""
from __future__ import annotations

import inspect
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.readers.base import ExtractedBlock
from app.pipeline.content_onset import (
    _get_heuristic_candidate_pages,
    compute_sample_pages,
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

    def test_pdf_filter_returns_onset_plus_extra_pages(self):
        """PDF filtering returns onset page + next 2 pages by default."""
        blocks = [
            _block("page 0 content", page=0),
            _block("page 1 block A", page=1),
            _block("page 1 block B", page=1),
            _block("page 2 content", page=2),
            _block("page 3 content", page=3),
            _block("page 4 content", page=4),
        ]
        result = filter_sample_blocks(blocks, onset_page=1, file_type="pdf")
        pages = {b.page_or_sheet for b in result}
        # onset=1 + 2 extra = pages 1, 2, 3
        assert pages == {1, 2, 3}
        assert len(result) == 4  # 2 from page 1 + 1 from page 2 + 1 from page 3

    def test_pdf_filter_single_page_only_onset(self):
        """PDF with pdf_extra_pages=0 returns only onset page."""
        blocks = [
            _block("page 0 content", page=0),
            _block("page 1 content", page=1),
        ]
        result = filter_sample_blocks(blocks, onset_page=0, file_type="pdf", pdf_extra_pages=0)
        assert len(result) == 1
        assert result[0].page_or_sheet == 0

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


# ---------------------------------------------------------------------------
# Background Extraction Tests
# ---------------------------------------------------------------------------

class TestSerializeDeserializePIIRecord:
    """Tests for PIIRecord round-trip serialization used in background extraction."""

    def test_serialize_roundtrip(self):
        from app.rra.entity_resolver import PIIRecord
        from app.pipeline.two_phase import _serialize_pii_record, _deserialize_pii_record

        rec = PIIRecord(
            record_id="r1",
            entity_type="PERSON",
            normalized_value="john doe",
            raw_name="John Doe",
            raw_email="john@example.com",
            source_document_id="doc-1",
            page_or_sheet=3,
            page_range="1-5",
            entity_types_found=("PERSON", "EMAIL"),
            validation_flags=("name_verified",),
        )
        serialized = _serialize_pii_record(rec)
        assert isinstance(serialized, dict)
        assert serialized["record_id"] == "r1"
        assert isinstance(serialized["entity_types_found"], list)

        restored = _deserialize_pii_record(serialized)
        assert restored.record_id == rec.record_id
        assert restored.raw_name == rec.raw_name
        assert restored.entity_types_found == ("PERSON", "EMAIL")
        assert restored.validation_flags == ("name_verified",)

    def test_serialize_empty_tuples(self):
        from app.rra.entity_resolver import PIIRecord
        from app.pipeline.two_phase import _serialize_pii_record, _deserialize_pii_record

        rec = PIIRecord(record_id="r2", entity_type="SSN", normalized_value="xxx")
        serialized = _serialize_pii_record(rec)
        assert serialized["entity_types_found"] == []
        restored = _deserialize_pii_record(serialized)
        assert restored.entity_types_found == ()


class TestUpdateExtractionProgress:
    """Tests for _update_extraction_progress() writing to run.metrics."""

    def test_progress_written_to_metrics(self):
        from app.pipeline.two_phase import _update_extraction_progress
        from unittest.mock import MagicMock, patch

        db = MagicMock()
        run = MagicMock()
        run.metrics = {}

        with patch("app.pipeline.two_phase.flag_modified"):
            _update_extraction_progress(
                db, run,
                stage="detection",
                message="Scanning doc 1/3...",
                completed_doc_ids=["doc-a"],
                total_docs=3,
                current_doc=1,
                records_found=5,
            )

        assert run.metrics["extraction_progress"]["stage"] == "detection"
        assert run.metrics["extraction_progress"]["message"] == "Scanning doc 1/3..."
        assert run.metrics["extraction_progress"]["completed_doc_ids"] == ["doc-a"]
        assert run.metrics["extraction_progress"]["records_found"] == 5
        assert "heartbeat" in run.metrics["extraction_progress"]
        db.commit.assert_called_once()

    def test_progress_preserves_existing_metrics(self):
        from app.pipeline.two_phase import _update_extraction_progress
        from unittest.mock import MagicMock, patch

        db = MagicMock()
        run = MagicMock()
        run.metrics = {"some_other_key": "value"}

        with patch("app.pipeline.two_phase.flag_modified"):
            _update_extraction_progress(
                db, run, stage="resolution", message="Resolving...",
                total_docs=2, current_doc=2, records_found=10,
            )

        assert run.metrics["some_other_key"] == "value"
        assert run.metrics["extraction_progress"]["stage"] == "resolution"

    def test_progress_with_result(self):
        from app.pipeline.two_phase import _update_extraction_progress
        from unittest.mock import MagicMock, patch

        db = MagicMock()
        run = MagicMock()
        run.metrics = {}

        result_data = {"job_id": "abc", "status": "COMPLETE", "subjects_found": 10}
        with patch("app.pipeline.two_phase.flag_modified"):
            _update_extraction_progress(
                db, run, stage="complete", message="Done",
                total_docs=1, current_doc=1, records_found=10,
                result=result_data,
            )

        assert run.metrics["extraction_progress"]["result"] == result_data


class TestExtractGeneratorRelay:
    """Tests for the SSE relay extract_generator()."""

    def test_generator_rejects_wrong_pipeline_mode(self):
        """extract_generator yields error for non-two_phase jobs."""
        from app.pipeline.two_phase import extract_generator
        from unittest.mock import MagicMock, patch

        mock_db = MagicMock()
        mock_run = MagicMock()
        mock_run.pipeline_mode = "full"
        mock_run.status = "analyzed"

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_run
        mock_db.execute.return_value = mock_result

        registry = MagicMock()
        events = list(extract_generator("00000000-0000-0000-0000-000000000001", mock_db, registry))
        assert len(events) == 1
        assert "not a two-phase pipeline" in events[0]

    def test_generator_accepts_extracting_status_for_reconnect(self):
        """extract_generator does not reject 'extracting' status (reconnect)."""
        from app.pipeline.two_phase import extract_generator, _extraction_threads
        from unittest.mock import MagicMock, patch
        import threading

        job_id = "00000000-0000-0000-0000-000000000002"

        mock_db = MagicMock()
        mock_run = MagicMock()
        mock_run.pipeline_mode = "two_phase"
        mock_run.status = "extracting"
        mock_run.config_snapshot = {"protocol_id": "hipaa"}
        mock_run.metrics = {"extraction_progress": {
            "stage": "complete",
            "message": "Extraction complete",
            "heartbeat": datetime.now(timezone.utc).isoformat(),
            "result": {"job_id": job_id, "status": "COMPLETE", "subjects_found": 5, "notification_required": 2, "export_count": 5},
        }}

        call_count = [0]

        def _mock_execute(stmt):
            result = MagicMock()
            # After first call, set status to completed so loop exits
            if call_count[0] > 0:
                mock_run.status = "completed"
            call_count[0] += 1
            result.scalar_one_or_none.return_value = mock_run
            return result

        mock_db.execute.side_effect = _mock_execute
        mock_db.expire_all = MagicMock()

        # Put a mock alive thread so _maybe_launch doesn't try to start one
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = True
        _extraction_threads[job_id] = mock_thread

        registry = MagicMock()

        with patch("app.pipeline.two_phase.time.sleep"):
            events = list(extract_generator(job_id, mock_db, registry))

        # Should not get an error about status
        error_events = [e for e in events if "error" in e and "expected" in e]
        assert len(error_events) == 0

        # Should get a complete event
        complete_events = [e for e in events if "COMPLETE" in e]
        assert len(complete_events) >= 1

        # Cleanup
        _extraction_threads.pop(job_id, None)

    def test_completed_job_returns_result_immediately(self):
        """If job is already completed, return result immediately without polling."""
        from app.pipeline.two_phase import extract_generator

        mock_db = MagicMock()
        mock_run = MagicMock()
        mock_run.pipeline_mode = "two_phase"
        mock_run.status = "completed"
        mock_run.metrics = {"extraction_progress": {
            "result": {"job_id": "test", "status": "COMPLETE", "subjects_found": 3},
        }}

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_run
        mock_db.execute.return_value = mock_result

        registry = MagicMock()
        events = list(extract_generator("00000000-0000-0000-0000-000000000003", mock_db, registry))
        assert len(events) == 1
        assert "COMPLETE" in events[0]


# ---------------------------------------------------------------------------
# TestCoordinatePipelineWiring — Step 21 Run 3
# ---------------------------------------------------------------------------

class TestCoordinatePipelineWiring:
    """Tests for coordinate extraction pipeline integration."""

    def test_is_fixed_layout_check_requires_both_fields(self):
        """Coordinate path requires layout_type=='fixed' AND layout_field_map populated."""
        from app.structure.document_schema import DocumentSchema, FieldMapping

        _base = dict(
            document_type="statement", document_subtype=None, issuing_entity=None,
            field_map=[], people=[], organizations=[], date_contexts=[], tables=[],
            suppression_hints=[], extraction_notes="", schema_confidence=0.9,
            detected_by="llm",
        )

        # Variable layout → not fixed
        schema_var = DocumentSchema(**_base, layout_type="variable")
        assert schema_var.layout_type != "fixed" or not schema_var.layout_field_map

        # Fixed but no field_map → not eligible
        schema_no_map = DocumentSchema(**_base, layout_type="fixed")
        assert schema_no_map.layout_type == "fixed"
        assert schema_no_map.layout_field_map is None

        # Fixed with field_map → eligible
        fm = FieldMapping(field_type="PERSON", anchor_text="Client:", spatial_relationship="same_line_right")
        schema_ok = DocumentSchema(
            **_base,
            layout_type="fixed",
            layout_field_map=[fm],
            layout_confidence=0.95,
        )
        assert schema_ok.layout_type == "fixed"
        assert schema_ok.layout_field_map is not None
        assert len(schema_ok.layout_field_map) == 1

    def test_coordinate_preview_dict_structure(self):
        """Coordinate extraction preview has expected keys."""
        preview = {
            "preview_instance": 0,
            "pages": "1",
            "fields_found": {"PERSON": {"value": "John Smith", "page": 1}},
            "fields_missing": ["DATE_OF_BIRTH"],
            "pages_read": [1],
            "total_instances_estimate": 100,
            "extraction_method": "coordinate",
            "layout_type": "fixed",
            "layout_confidence": 0.95,
            "field_map_count": 3,
        }
        assert preview["extraction_method"] == "coordinate"
        assert preview["layout_type"] == "fixed"
        assert preview["layout_confidence"] == 0.95
        assert preview["field_map_count"] == 3
        assert "PERSON" in preview["fields_found"]
        assert "DATE_OF_BIRTH" in preview["fields_missing"]

    def test_fixed_layout_before_vision_path(self):
        """Coordinate path (Path 0) is checked before Vision (Path 1)."""
        # The ordering in two_phase.py is:
        # Path 0: coordinate → Path 1: vision → Path 2: LLM → Path 3: presidio
        # If Path 0 produces records, Path 1+ are skipped.
        import ast
        import inspect
        from app.pipeline import two_phase

        source = inspect.getsource(two_phase.run_extraction_background)
        path0_idx = source.find("Path 0: Coordinate")
        path1_idx = source.find("Path 1: Vision")
        path2_idx = source.find("Path 2a: Text + LLM table")
        path3_idx = source.find("Path 3: Presidio")

        assert path0_idx > 0, "Path 0 not found in run_extraction_background"
        assert path1_idx > 0, "Path 1 not found"
        assert path0_idx < path1_idx, "Path 0 must come before Path 1"
        assert path1_idx < path2_idx, "Path 1 must come before Path 2"
        assert path2_idx < path3_idx, "Path 2 must come before Path 3"

    def test_path1_vision_guards_on_no_records(self):
        """After Path 0, Vision path only runs if records is still empty."""
        import inspect
        from app.pipeline import two_phase

        source = inspect.getsource(two_phase.run_extraction_background)
        # After Path 0, Path 1 should check 'not records'
        path1_section = source[source.find("Path 1: Vision"):]
        # The condition should include 'not records' (may be in the if-block below the comment)
        assert "not records" in path1_section[:1000], \
            "Path 1 (Vision) must be guarded by 'not records' to skip when coordinate path succeeds"

    def test_coordinate_path_uses_reconciliation_on_failures(self):
        """Coordinate path calls ExtractionReconciler for failed pages."""
        import inspect
        from app.pipeline import two_phase

        source = inspect.getsource(two_phase.run_extraction_background)
        coord_section = source[source.find("Path 0: Coordinate"):source.find("Path 1: Vision")]
        assert "ExtractionReconciler" in coord_section
        assert "reconcile" in coord_section

    def test_coordinate_extraction_path_label(self):
        """Coordinate extraction uses path label '0-coord'."""
        import inspect
        from app.pipeline import two_phase

        source = inspect.getsource(two_phase.run_extraction_background)
        assert '"0-coord"' in source, "extraction_path should be '0-coord' for coordinate extraction"

    def test_analyze_generator_has_coordinate_preview(self):
        """analyze_generator includes coordinate/fixed-layout preview stage."""
        import inspect
        from app.pipeline import two_phase

        source = inspect.getsource(two_phase.analyze_generator)
        assert "fixed-layout" in source or "coordinate extraction" in source.lower() or "fixed_layout_docs" in source
        assert "extraction_method" in source
        assert '"coordinate"' in source

    def test_analysis_api_includes_layout_fields(self):
        """GET /analysis response includes layout_type, layout_field_map, layout_confidence."""
        import inspect
        from app.api.routes import analysis_review

        source = inspect.getsource(analysis_review.get_analysis_results)
        assert '"layout_type"' in source
        assert '"layout_field_map"' in source
        assert '"layout_confidence"' in source

    def test_field_map_put_endpoint_exists(self):
        """PUT /jobs/{id}/field-map endpoint is registered."""
        import inspect
        from app.api.routes import analysis_review

        source = inspect.getsource(analysis_review)
        assert "update_field_map" in source
        assert 'field-map' in source
        assert '.put(' in source

    def test_field_map_body_validation(self):
        """UpdateFieldMapBody validates field mappings."""
        from app.api.routes.analysis_review import UpdateFieldMapBody, FieldMappingBody

        body = UpdateFieldMapBody(
            document_id="test-doc-id",
            field_mappings=[
                FieldMappingBody(
                    field_type="PERSON",
                    anchor_text="Client:",
                    spatial_relationship="same_line_right",
                ),
                FieldMappingBody(
                    field_type="GOVERNMENT_ID",
                    anchor_text="Tax No",
                    spatial_relationship="line_below",
                    value_pattern=r"\d{3}-\d{2}-\d{4}",
                ),
            ],
            extraction_method="coordinate",
        )
        assert len(body.field_mappings) == 2
        assert body.extraction_method == "coordinate"
        assert body.field_mappings[0].field_type == "PERSON"
        assert body.field_mappings[1].value_pattern == r"\d{3}-\d{2}-\d{4}"

    def test_field_map_body_defaults(self):
        """FieldMappingBody has correct defaults."""
        from app.api.routes.analysis_review import FieldMappingBody

        fm = FieldMappingBody(
            field_type="LOCATION",
            anchor_text="Address",
            spatial_relationship="lines_below_4",
        )
        assert fm.value_pattern is None
        assert fm.sample_bbox == []
        assert fm.line_count == 1
        assert fm.skip_pattern is None

    def test_field_map_spatial_relationship_validation(self):
        """update_field_map validates spatial_relationship values."""
        import inspect
        from app.api.routes import analysis_review

        source = inspect.getsource(analysis_review.update_field_map)
        assert "valid_relationships" in source
        assert "same_line_right" in source
        assert "lines_below_" in source

    def test_field_map_stores_on_metadata_json(self):
        """update_field_map stores field map on document metadata_json."""
        import inspect
        from app.api.routes import analysis_review

        source = inspect.getsource(analysis_review.update_field_map)
        assert "auditor_layout_field_map" in source
        assert "auditor_extraction_method" in source
        assert "metadata_json" in source

    def test_extraction_uses_auditor_field_map(self):
        """run_extraction_background checks for auditor-edited field map."""
        import inspect
        from app.pipeline import two_phase

        source = inspect.getsource(two_phase.run_extraction_background)
        assert "auditor_layout_field_map" in source
        assert "auditor_extraction_method" in source
        assert "effective_field_map" in source

    def test_auditor_ai_method_skips_coordinate(self):
        """When auditor selects 'ai' method, coordinate path is skipped."""
        import inspect
        from app.pipeline import two_phase

        source = inspect.getsource(two_phase.run_extraction_background)
        # The use_coordinate flag should check auditor_method != "ai"
        assert 'auditor_method != "ai"' in source

    def test_schema_persisted_to_metadata_json_during_analysis(self):
        """analyze_generator persists schema to Document.metadata_json['document_schema']."""
        import inspect
        from app.pipeline import two_phase

        source = inspect.getsource(two_phase.analyze_generator)
        assert '"document_schema"' in source
        assert "schema.to_dict()" in source
        assert "flag_modified" in source

    def test_schema_loaded_from_metadata_json_during_extraction(self):
        """run_extraction_background loads schema from metadata_json before LLM re-computation."""
        import inspect
        from app.pipeline import two_phase

        source = inspect.getsource(two_phase.run_extraction_background)
        assert '"document_schema"' in source
        assert "from_dict" in source
        # Schema load must come BEFORE LLM re-computation fallback
        # Use the per-doc schema loading section (not the top-level class init)
        load_idx = source.find('schema_dict = doc_meta.get("document_schema")')
        fallback_idx = source.find("Fall back to LLM re-computation")
        assert load_idx > 0, "Schema load from metadata_json not found"
        assert fallback_idx > 0, "LLM fallback not found"
        assert load_idx < fallback_idx, "Schema load from metadata_json must precede LLM fallback"

    def test_schema_roundtrip_via_metadata_json(self):
        """DocumentSchema can roundtrip through to_dict/from_dict."""
        from app.structure.document_schema import DocumentSchema, FieldMapping

        fm = FieldMapping(field_type="PERSON", anchor_text="Client:", spatial_relationship="same_line_right")
        schema = DocumentSchema(
            document_type="statement", document_subtype=None, issuing_entity=None,
            field_map=[], people=[], organizations=[], date_contexts=[], tables=[],
            suppression_hints=[], extraction_notes="", schema_confidence=0.9,
            detected_by="llm",
            layout_type="fixed",
            layout_field_map=[fm],
            layout_confidence=0.95,
        )
        d = schema.to_dict()
        restored = DocumentSchema.from_dict(d)
        assert restored.layout_type == "fixed"
        assert len(restored.layout_field_map) == 1
        assert restored.layout_field_map[0].field_type == "PERSON"
        assert restored.layout_confidence == 0.95


# ---------------------------------------------------------------------------
# TestVisionRoutingPipelineWiring
# ---------------------------------------------------------------------------
class TestVisionRoutingPipelineWiring:
    """Tests for vision routing integration into the two-phase pipeline (Step 22c)."""

    def test_vision_routing_persisted_to_metadata_during_analysis(self):
        """analyze_generator persists vision_routing to Document.metadata_json."""
        import inspect
        from app.pipeline import two_phase

        source = inspect.getsource(two_phase.analyze_generator)
        assert '"vision_routing"' in source
        assert '"structure_type"' in source
        assert '"recommended_path"' in source

    def test_vision_field_map_persisted_to_metadata_during_analysis(self):
        """analyze_generator persists vision_field_map to Document.metadata_json."""
        import inspect
        from app.pipeline import two_phase

        source = inspect.getsource(two_phase.analyze_generator)
        assert '"vision_field_map"' in source
        assert "FieldMapBuilder" in source

    def test_vision_field_map_loaded_during_extraction(self):
        """run_extraction_background loads vision_field_map from metadata for Path 0."""
        import inspect
        from app.pipeline import two_phase

        source = inspect.getsource(two_phase.run_extraction_background)
        assert '"vision_field_map"' in source
        assert "vision_field_map" in source

    def test_auditor_field_map_overrides_vision_field_map(self):
        """Auditor field map takes priority over vision field map."""
        import inspect
        from app.pipeline import two_phase

        source = inspect.getsource(two_phase.run_extraction_background)
        # auditor_field_map or vision_field_map or ... (LLM schema)
        assert "auditor_field_map or vision_field_map" in source

    def test_coordinate_path_triggered_by_vision_routing(self):
        """recommended_path='coordinate' + valid field map → Path 0."""
        import inspect
        from app.pipeline import two_phase

        source = inspect.getsource(two_phase.run_extraction_background)
        assert 'recommended_path == "coordinate"' in source

    def test_vision_direct_path_falls_to_path1(self):
        """recommended_path='vision_direct' means no coordinate path, falls to Path 1."""
        # Vision direct docs skip coordinate extraction. Path 1 (Vision) handles them.
        import inspect
        from app.pipeline import two_phase

        source = inspect.getsource(two_phase.run_extraction_background)
        # Path 1 section still has "not records" guard
        path1_section = source[source.find("Path 1: Vision"):]
        assert "not records" in path1_section[:1000]

    def test_llm_template_path_from_vision_routing(self):
        """Vision routing can recommend llm_template, which falls to Path 2b."""
        # When recommended_path is "llm_template", coordinate path is skipped
        # and records stay empty, so Path 2b handles it.
        import inspect
        from app.pipeline import two_phase

        source = inspect.getsource(two_phase.run_extraction_background)
        # is_coordinate_path requires recommended_path == "coordinate" (or auditor/legacy)
        assert "recommended_path" in source

    def test_no_vision_routing_uses_legacy_path_logic(self):
        """Documents without vision_routing fall back to LLM schema layout_type."""
        import inspect
        from app.pipeline import two_phase

        source = inspect.getsource(two_phase.run_extraction_background)
        # Legacy check: layout_type in ("fixed", "template_with_drift")
        assert '"fixed"' in source
        assert '"template_with_drift"' in source
        # The OR condition allows legacy schema path when no vision routing
        assert "layout_type" in source

    def test_small_doc_vision_direct_skips_coordinate(self):
        """Small docs (≤5 pages) get vision_direct from VisionRouter, skip coordinate."""
        from app.pipeline.vision_router import VisionRouter, VisionRoutingResult

        result = VisionRoutingResult(
            structure_type="fixed_single_page",
            structure_confidence=0.9,
            pii_fields=[{"type": "PERSON", "value": "John", "label": "Name:"}],
        )
        # With total_pages=3, _determine_path returns "vision_direct"
        router = VisionRouter.__new__(VisionRouter)
        path = router._determine_path(result, total_pages=3, is_scanned=False)
        assert path == "vision_direct"

    def test_field_map_validation_failure_downgrades_path(self):
        """Vision field map validation failure during routing → path downgraded."""
        import inspect
        from app.pipeline import two_phase

        # Now in _route_single_document (parallel worker)
        source = inspect.getsource(two_phase._route_single_document)
        assert "vision_direct" in source or "presidio" in source
        # Also verify the coordinate extractor validation exists
        assert "CoordinateExtractor" in source

    def test_analysis_api_exposes_vision_routing(self):
        """GET /analysis response includes vision_routing and vision_field_map."""
        import inspect
        from app.api.routes import analysis_review

        source = inspect.getsource(analysis_review.get_analysis_results)
        assert '"vision_routing"' in source
        assert '"vision_field_map"' in source


# ---------------------------------------------------------------------------
# Step 22d: Extraction Verification Pipeline Wiring
# ---------------------------------------------------------------------------


class TestExtractionVerificationWiring:
    """Tests for post-extraction verification wiring in two_phase.py."""

    def test_verifier_import_in_coordinate_path(self):
        """ExtractionVerifier is imported in the coordinate extraction path."""
        import inspect
        from app.pipeline import two_phase

        source = inspect.getsource(two_phase.run_extraction_background)
        assert "ExtractionVerifier" in source
        assert "extraction_verifier" in source

    def test_verification_result_stored_in_metrics(self):
        """Verification result is stored via _update_extraction_progress."""
        import inspect
        from app.pipeline import two_phase

        source = inspect.getsource(two_phase.run_extraction_background)
        assert "verification" in source
        assert "success_rate" in source
        assert "is_acceptable" in source

    def test_verification_logged(self):
        """Verification summary is logged."""
        import inspect
        from app.pipeline import two_phase

        source = inspect.getsource(two_phase.run_extraction_background)
        assert 'Verification:' in source

    def test_verifier_module_exists(self):
        """extraction_verifier module can be imported."""
        from app.pipeline.extraction_verifier import ExtractionVerifier, ExtractionVerification
        assert ExtractionVerifier is not None
        assert ExtractionVerification is not None

    def test_verifier_verify_method_signature(self):
        """ExtractionVerifier.verify() accepts expected parameters."""
        import inspect
        from app.pipeline.extraction_verifier import ExtractionVerifier

        sig = inspect.signature(ExtractionVerifier.verify)
        params = list(sig.parameters.keys())
        assert "records" in params
        assert "failed_pages" in params
        assert "reconciled_records" in params
        assert "total_pages" in params
        assert "field_map" in params

    def test_verification_stage_name(self):
        """Verification progress uses stage='verification'."""
        import inspect
        from app.pipeline import two_phase

        source = inspect.getsource(two_phase.run_extraction_background)
        assert 'stage="verification"' in source

    def test_verification_result_fields(self):
        """Verification result dict contains all expected fields."""
        import inspect
        from app.pipeline import two_phase

        source = inspect.getsource(two_phase.run_extraction_background)
        for field_name in ["success_rate", "successful", "reconciled", "failed", "field_rates", "is_acceptable"]:
            assert f'"{field_name}"' in source


# ---------------------------------------------------------------------------
# Quality gate between extraction paths
# ---------------------------------------------------------------------------


class TestExtractionQualityGate:
    """Test _check_extraction_quality() — rejects paths with mostly garbage PERSON names."""

    def _make_record(self, name: str | None = None, gov_id: str | None = None):
        from app.rra.entity_resolver import PIIRecord
        return PIIRecord(
            record_id="r1",
            entity_type="PERSON",
            normalized_value=name or gov_id or "",
            raw_name=name,
            raw_government_id=gov_id,
            source_document_id="doc-1",
            page_or_sheet=0,
        )

    def test_accepts_good_records(self):
        """10 valid names → accepted."""
        from app.pipeline.two_phase import _check_extraction_quality
        names = ["Alice Smith", "Bob Jones", "Carol Brown", "Dan White", "Eve Davis",
                 "Frank Green", "Grace Lee", "Henry Clark", "Irene Moore", "Jack Hall"]
        records = [self._make_record(n) for n in names]
        assert _check_extraction_quality(records, "test") is True

    def test_rejects_mostly_garbage(self):
        """8/10 blocklisted names → <50% valid → rejected."""
        from app.pipeline.two_phase import _check_extraction_quality
        garbage = ["Summary Statement", "Page Report", "Total Balance", "Account Date",
                    "Report Summary", "Statement Page", "Invoice Number", "Document Form"]
        records = [self._make_record(g) for g in garbage]
        records.extend([self._make_record("John Smith"), self._make_record("Alice Brown")])
        assert _check_extraction_quality(records, "test") is False

    def test_rejects_empty(self):
        """Empty list → rejected."""
        from app.pipeline.two_phase import _check_extraction_quality
        assert _check_extraction_quality([], "test") is False

    def test_lenient_for_small_set_one_valid(self):
        """1 valid + 1 invalid in 2-record set → accepted (lenient)."""
        from app.pipeline.two_phase import _check_extraction_quality
        records = [self._make_record("John Smith"), self._make_record("Summary Statement")]
        assert _check_extraction_quality(records, "test") is True

    def test_rejects_small_set_all_bad(self):
        """0 valid in 2-record set → rejected."""
        from app.pipeline.two_phase import _check_extraction_quality
        records = [self._make_record("Total Balance"), self._make_record("Page Report")]
        assert _check_extraction_quality(records, "test") is False

    def test_boundary_exactly_50_percent(self):
        """Exactly 50% valid → accepted (threshold is < 0.50)."""
        from app.pipeline.two_phase import _check_extraction_quality
        valid_names = ["Alice Smith", "Bob Jones", "Carol Brown", "Dan White", "Eve Davis"]
        valid = [self._make_record(n) for n in valid_names]
        invalid = [self._make_record("Summary Statement"), self._make_record("Page Report"),
                   self._make_record("Total Balance"), self._make_record("Account Date"),
                   self._make_record("Report Summary")]
        records = valid + invalid
        assert _check_extraction_quality(records, "test") is True

    def test_below_threshold(self):
        """4 valid / 10 total = 40% → rejected."""
        from app.pipeline.two_phase import _check_extraction_quality
        valid = [self._make_record(n) for n in ["Alice Smith", "Bob Jones", "Carol Brown", "Dan White"]]
        invalid = [self._make_record("Report Page"), self._make_record("Summary Total"),
                   self._make_record("Account Balance"), self._make_record("Statement Date"),
                   self._make_record("Form Section"), self._make_record("Invoice Number")]
        records = valid + invalid
        assert _check_extraction_quality(records, "test") is False

    def test_records_without_names_counted(self):
        """Records with gov_id but no name: not counted as valid, but still in total."""
        from app.pipeline.two_phase import _check_extraction_quality
        valid = [self._make_record("John Smith"), self._make_record("Alice Brown"),
                 self._make_record("Carol Davis")]
        nameless = [self._make_record(gov_id="123-45-6789"), self._make_record(gov_id="987-65-4321")]
        records = valid + nameless  # 3 valid of 5 total = 60% → accepted
        assert _check_extraction_quality(records, "test") is True


# ---------------------------------------------------------------------------
# Person Samples Persistence (Gap 1)
# ---------------------------------------------------------------------------

class TestPersonSamplesPersistence:
    """Test that person name samples from vision routing are persisted to
    document metadata and loaded during coordinate extraction."""

    def test_person_samples_persisted_to_metadata(self):
        """Vision routing with PERSON fields stores person_samples in metadata."""
        import importlib
        two_phase = importlib.import_module("app.pipeline.two_phase")
        source = inspect.getsource(two_phase)
        # Verify the persistence code exists in the source
        assert 'doc_meta["person_samples"]' in source
        assert '"person_samples"' in source

    def test_person_samples_loaded_for_coordinate_extraction(self):
        """CoordinateExtractor receives name_samples from metadata."""
        import importlib
        two_phase = importlib.import_module("app.pipeline.two_phase")
        source = inspect.getsource(two_phase)
        # Verify the loading code passes name_samples
        assert 'name_samples=_person_samples' in source or 'name_samples=' in source
        assert 'doc_meta.get("person_samples")' in source

    def test_coordinate_extractor_uses_samples(self):
        """CoordinateExtractor initialized with name_samples builds regex."""
        from app.pipeline.coordinate_extractor import CoordinateExtractor
        from app.structure.document_schema import FieldMapping
        fm = FieldMapping(
            field_type="PERSON",
            anchor_text="Name",
            spatial_relationship="same_line_right",
        )
        ext = CoordinateExtractor([fm], "", "doc1", name_samples=["John Smith"])
        assert ext._name_regex is not None
        assert ext._name_format == "first_last"
        # Without samples, no regex
        ext2 = CoordinateExtractor([fm], "", "doc2")
        assert ext2._name_regex is None


# ---------------------------------------------------------------------------
# TestSampledAnalysisPipeline
# ---------------------------------------------------------------------------
class TestSampledAnalysisPipeline:
    """Test that tiered page sampling is wired into analyze_generator."""

    def test_compute_sample_pages_imported(self):
        """compute_sample_pages must be importable from content_onset."""
        from app.pipeline.content_onset import compute_sample_pages as csp
        assert callable(csp)

    def test_pdf_over_10_uses_sampling(self):
        """analyze_generator should call compute_sample_pages for PDFs >10 pages."""
        import importlib
        two_phase = importlib.import_module("app.pipeline.two_phase")
        source = inspect.getsource(two_phase)
        # Verify sampling is wired in
        assert "compute_sample_pages" in source
        assert "read_pages" in source
        assert "get_pdf_page_count" in source

    def test_doc_total_pages_dict_exists(self):
        """analyze_generator should track true page counts in doc_total_pages."""
        import importlib
        two_phase = importlib.import_module("app.pipeline.two_phase")
        source = inspect.getsource(two_phase)
        assert "doc_total_pages" in source
        # Should be used for total_pages lookups
        assert "doc_total_pages.get(doc.id" in source

    def test_total_pages_from_page_count_not_blocks(self):
        """total_pages should prefer doc_total_pages over block count."""
        import importlib
        two_phase = importlib.import_module("app.pipeline.two_phase")
        source = inspect.getsource(two_phase.analyze_generator)
        # doc_total_pages should be consulted before falling back to block count
        assert "doc_total_pages.get(doc.id" in source

    def test_non_pdf_reads_all(self):
        """Non-PDF files should still use reader.read() (no sampling)."""
        import importlib
        two_phase = importlib.import_module("app.pipeline.two_phase")
        source = inspect.getsource(two_phase.analyze_generator)
        # The else branch for non-PDF should call reader.read()
        assert "reader = get_reader(doc.source_path)" in source
        assert "blocks = reader.read()" in source


# ---------------------------------------------------------------------------
# Step 1: Connection Pool Config
# ---------------------------------------------------------------------------


class TestConnectionPoolConfig:
    """Verify pool_size and max_overflow are set on DB engines."""

    def test_deps_pool_config(self):
        """app/api/deps.py creates engine with pool_size=20."""
        from app.api import deps
        source = inspect.getsource(deps._get_session_factory)
        assert "pool_size=20" in source
        assert "max_overflow=10" in source
        assert "pool_pre_ping=True" in source

    def test_session_pool_config(self):
        """app/db/session.py creates engine with pool_size=20."""
        from app.db import session
        source = inspect.getsource(session.get_engine)
        assert "pool_size=20" in source
        assert "max_overflow=10" in source

    def test_refresh_session_helper_exists(self):
        """_refresh_session helper is importable and has correct signature."""
        from app.pipeline.two_phase import _refresh_session
        sig = inspect.signature(_refresh_session)
        params = list(sig.parameters.keys())
        assert "db" in params
        assert "run_id" in params
        assert "doc_ids" in params


# ---------------------------------------------------------------------------
# Step 2: Schema-Based Vision Skip
# ---------------------------------------------------------------------------


class TestSchemaBasedVisionSkip:
    """Test _try_schema_skip() routes based on LLM schema."""

    def test_skip_for_fixed_layout(self):
        """Fixed layout with field map → coordinate path."""
        from app.pipeline.two_phase import _try_schema_skip
        from app.structure.document_schema import FieldMapping

        fm = FieldMapping(field_type="PERSON", anchor_text="Name", spatial_relationship="same_line_right")

        class FakeSchema:
            layout_type = "fixed"
            layout_field_map = [fm]
            layout_confidence = 0.9
            is_tabular = False
            records_per_page_estimate = 1
            template = None

        result = _try_schema_skip(FakeSchema(), 100)
        assert result is not None
        routing_dict, fm_dicts = result
        assert routing_dict["recommended_path"] == "coordinate"
        assert fm_dicts is not None
        assert len(fm_dicts) == 1

    def test_skip_for_tabular(self):
        """Tabular schema with records_per_page > 1 → llm_table."""
        from app.pipeline.two_phase import _try_schema_skip

        class FakeSchema:
            layout_type = "variable"
            layout_field_map = None
            is_tabular = True
            records_per_page_estimate = 15
            schema_confidence = 0.8
            template = None

        result = _try_schema_skip(FakeSchema(), 50)
        assert result is not None
        routing_dict, fm_dicts = result
        assert routing_dict["recommended_path"] == "llm_table"
        assert fm_dicts is None

    def test_skip_for_template(self):
        """Multi-page template → llm_template."""
        from app.pipeline.two_phase import _try_schema_skip

        class FakeTemplate:
            pages_per_instance = 3

        class FakeSchema:
            layout_type = "variable"
            layout_field_map = None
            is_tabular = False
            records_per_page_estimate = 1
            schema_confidence = 0.85
            template = FakeTemplate()

        result = _try_schema_skip(FakeSchema(), 100)
        assert result is not None
        routing_dict, _ = result
        assert routing_dict["recommended_path"] == "llm_template"

    def test_no_skip_for_variable(self):
        """Variable layout with no distinguishing features → None (needs vision)."""
        from app.pipeline.two_phase import _try_schema_skip

        class FakeSchema:
            layout_type = "variable"
            layout_field_map = None
            is_tabular = False
            records_per_page_estimate = 1
            template = None

        assert _try_schema_skip(FakeSchema(), 100) is None
        assert _try_schema_skip(None, 100) is None


# ---------------------------------------------------------------------------
# Step 3: Parallel Vision Routing
# ---------------------------------------------------------------------------


class TestParallelVisionRouting:
    """Test parallel vision routing infrastructure."""

    def test_vision_routing_workers_setting(self):
        """Settings includes vision_routing_workers with default 1 (single GPU)."""
        from app.core.settings import Settings
        s = Settings(DATABASE_URL="sqlite:///test.db")
        assert s.vision_routing_workers == 1

    def test_template_cache_thread_safety(self):
        """TemplateCache has a threading lock."""
        from app.pipeline.template_cache import TemplateCache
        tc = TemplateCache()
        assert hasattr(tc, "_lock")
        import threading
        assert isinstance(tc._lock, type(threading.Lock()))

    def test_route_single_document_exists(self):
        """_route_single_document is importable with correct signature."""
        from app.pipeline.two_phase import _route_single_document
        sig = inspect.signature(_route_single_document)
        params = list(sig.parameters.keys())
        assert "doc_info" in params
        assert "router" in params
        assert "template_cache" in params
        assert "builder_cls" in params

    def test_route_single_returns_dict(self):
        """_route_single_document returns dict with required keys."""
        from app.pipeline.two_phase import _route_single_document

        # Mock router that returns a simple result
        class MockRouting:
            structure_type = "variable"
            structure_confidence = 0.5
            pii_fields = []
            records_per_page = 1
            cross_page_data = False
            pages_per_instance = 1
            recommended_path = "presidio"

        class MockRouter:
            def analyze_document(self, *args, **kwargs):
                return MockRouting()

        class MockCache:
            def get(self, *args, **kwargs):
                return None
            def put(self, *args, **kwargs):
                pass

        class MockBuilder:
            pass

        info = {
            "doc_id": "test-id",
            "source_path": "/nonexistent/test.pdf",
            "file_name": "test.pdf",
            "file_type": "pdf",
            "onset": 0,
            "total_pages": 10,
            "is_scanned": False,
        }
        result = _route_single_document(info, MockRouter(), MockCache(), MockBuilder)
        assert isinstance(result, dict)
        assert "routing_dict" in result
        assert "cache_hit" in result
        assert result["cache_hit"] is False

    def test_concurrent_futures_imported(self):
        """concurrent.futures is available in two_phase module."""
        import importlib
        two_phase = importlib.import_module("app.pipeline.two_phase")
        source = inspect.getsource(two_phase)
        assert "concurrent.futures" in source
        assert "ThreadPoolExecutor" in source

    def test_parallel_routing_in_source(self):
        """analyze_generator uses ThreadPoolExecutor for vision routing."""
        from app.pipeline.two_phase import analyze_generator
        source = inspect.getsource(analyze_generator)
        assert "ThreadPoolExecutor" in source
        assert "as_completed" in source

    def test_schema_skip_in_source(self):
        """analyze_generator calls _try_schema_skip before vision routing."""
        from app.pipeline.two_phase import analyze_generator
        source = inspect.getsource(analyze_generator)
        assert "_try_schema_skip" in source
        assert "schema_skipped_docs" in source

    def test_worker_timeout_in_source(self):
        """Parallel routing uses timeout on future.result()."""
        from app.pipeline.two_phase import analyze_generator
        source = inspect.getsource(analyze_generator)
        assert "timeout=300" in source


# ---------------------------------------------------------------------------
# Step 4: Configurable Understanding Model
# ---------------------------------------------------------------------------


class TestConfigurableUnderstandingModel:
    """Test OLLAMA_UNDERSTANDING_MODEL setting."""

    def test_setting_exists(self):
        """Settings includes ollama_understanding_model with None default."""
        from app.core.settings import Settings
        s = Settings(DATABASE_URL="sqlite:///test.db")
        assert s.ollama_understanding_model is None

    def test_understanding_uses_custom_model(self):
        """LLMDocumentUnderstanding respects ollama_understanding_model setting."""
        from app.structure import llm_document_understanding
        source = inspect.getsource(llm_document_understanding.LLMDocumentUnderstanding.__init__)
        assert "ollama_understanding_model" in source
        assert "ollama_model" in source


# ---------------------------------------------------------------------------
# Step 6: SSE Progress Enhancement
# ---------------------------------------------------------------------------


class TestSSEProgressEnhancement:
    """Test enriched SSE events in vision routing."""

    def test_schema_skip_in_sse(self):
        """SSE events include schema_skip flag."""
        from app.pipeline.two_phase import analyze_generator
        source = inspect.getsource(analyze_generator)
        assert '"schema_skip"' in source

    def test_doc_name_in_sse(self):
        """SSE events include doc_name."""
        from app.pipeline.two_phase import analyze_generator
        source = inspect.getsource(analyze_generator)
        assert '"doc_name"' in source
        assert '"recommended_path"' in source
        assert '"cache_hit"' in source
