"""Tests for Document Structure Analysis (DSA).

Covers: document type classification, section detection, entity role
assignment, protocol relevance, multi-person documents, masking,
LLM analyzer (mocked), confidence adjustment, RRA cross-role prevention,
and schema columns.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, inspect

from app.db.base import Base
from app.db import models as _models  # noqa: F401 — register ORM models
from app.readers.base import ExtractedBlock
from app.structure.models import (
    DocumentStructureAnalysis,
    EntityRoleAnnotation,
    SectionAnnotation,
    VALID_DOCUMENT_TYPES,
    VALID_ENTITY_ROLES,
    VALID_SECTION_TYPES,
)
from app.structure.heuristics import (
    DOCUMENT_TYPE_SIGNALS,
    HeuristicAnalyzer,
    SECTION_TO_ROLE,
)
from app.structure.protocol_relevance import (
    PROTOCOL_TARGET_ROLES,
    get_role_relevance,
    TARGET,
    DEPRIORITIZE,
    NON_TARGET,
)
from app.structure.masking import mask_text_for_llm
from app.structure.llm_analyzer import LLMStructureAnalyzer, merge_analyses
from app.tasks.structure_analysis import StructureAnalysisTask
from app.tasks.detection import DetectionResult, annotate_results_with_structure
from app.pii.layer2_context import Layer2ContextClassifier
from app.pii.presidio_engine import DetectionResult as PresidioDetectionResult
from app.rra.entity_resolver import PIIRecord, build_confidence, EntityResolver


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_block(
    text: str,
    page: int = 1,
    col_header: str | None = None,
    block_type: str = "prose",
) -> ExtractedBlock:
    return ExtractedBlock(
        text=text,
        page_or_sheet=page,
        source_path="/test/doc.pdf",
        file_type="pdf",
        block_type=block_type,
        col_header=col_header,
    )


def _make_presidio_result(
    block: ExtractedBlock,
    entity_type: str = "PERSON",
    score: float = 0.60,
) -> PresidioDetectionResult:
    return PresidioDetectionResult(
        block=block,
        entity_type=entity_type,
        start=0,
        end=len(block.text),
        score=score,
        pattern_used="test",
        geography="US",
        regulatory_framework="hipaa",
    )


# ===================================================================
# TestDocumentTypeClassification
# ===================================================================

class TestDocumentTypeClassification:
    """Keyword density classification for document types."""

    def test_medical_record_detected(self):
        blocks = [
            _make_block("Patient Name: John Doe"),
            _make_block("Diagnosis: Type 2 Diabetes"),
            _make_block("Medical Record Number: MRN-12345"),
            _make_block("Physician: Dr. Smith at General Hospital"),
            _make_block("Treatment plan and HIPAA notice"),
        ]
        analyzer = HeuristicAnalyzer()
        result = analyzer.analyze(blocks, "doc-1")
        assert result.document_type == "medical_record"
        assert result.document_type_confidence >= 0.3

    def test_student_file_detected(self):
        blocks = [
            _make_block("Student Information Form"),
            _make_block("Student Name: Alice Johnson"),
            _make_block("School: Lincoln High School"),
            _make_block("Grade: 10, Enrollment Date: 2024-09-01"),
            _make_block("Parent/Guardian: Robert Johnson"),
            _make_block("FERPA Notice: This record is protected"),
        ]
        analyzer = HeuristicAnalyzer()
        result = analyzer.analyze(blocks, "doc-2")
        assert result.document_type == "student_file"
        assert result.document_type_confidence >= 0.3

    def test_financial_statement_detected(self):
        blocks = [
            _make_block("Account Statement"),
            _make_block("Account Balance: $5,432.10"),
            _make_block("Transaction History"),
            _make_block("Bank of America, Routing Number: 021000021"),
            _make_block("Deposit on 01/15/2025"),
        ]
        analyzer = HeuristicAnalyzer()
        result = analyzer.analyze(blocks, "doc-3")
        assert result.document_type == "financial_statement"

    def test_employment_record_detected(self):
        blocks = [
            _make_block("Employee Record"),
            _make_block("Employee Name: Jane Smith"),
            _make_block("Employer: Acme Corp"),
            _make_block("Hire Date: 2020-03-15, Salary: $85,000"),
            _make_block("Department: Engineering, W-2 attached"),
        ]
        analyzer = HeuristicAnalyzer()
        result = analyzer.analyze(blocks, "doc-4")
        assert result.document_type == "employment_record"

    def test_unknown_type_for_ambiguous_document(self):
        blocks = [
            _make_block("Lorem ipsum dolor sit amet"),
            _make_block("consectetur adipiscing elit"),
        ]
        analyzer = HeuristicAnalyzer()
        result = analyzer.analyze(blocks, "doc-5")
        assert result.document_type == "unknown"
        assert result.document_type_confidence == 0.0

    def test_empty_blocks_returns_unknown(self):
        analyzer = HeuristicAnalyzer()
        result = analyzer.analyze([], "doc-6")
        assert result.document_type == "unknown"
        assert result.document_type_confidence == 0.0

    def test_confidence_capped_at_095(self):
        # Even with many keywords, heuristic confidence maxes at 0.95
        blocks = [_make_block(" ".join(DOCUMENT_TYPE_SIGNALS["medical_record"] * 10))]
        analyzer = HeuristicAnalyzer()
        result = analyzer.analyze(blocks, "doc-7")
        assert result.document_type_confidence <= 0.95


# ===================================================================
# TestSectionDetection
# ===================================================================

class TestSectionDetection:
    """Section detection via heading patterns and column headers."""

    def test_patient_information_heading_detected(self):
        blocks = [
            _make_block("Patient Information"),
            _make_block("Name: John Doe"),
            _make_block("DOB: 01/01/1980"),
        ]
        analyzer = HeuristicAnalyzer()
        result = analyzer.analyze(blocks, "doc-1")
        assert len(result.sections) >= 1
        assert result.sections[0].section_type == "patient_information"
        assert 0 in result.sections[0].block_indices

    def test_multiple_sections_detected(self):
        blocks = [
            _make_block("Patient Information"),
            _make_block("Name: John Doe"),
            _make_block("Provider Information"),
            _make_block("Dr. Smith, NPI: 1234567890"),
            _make_block("Emergency Contact"),
            _make_block("Jane Doe, 555-1234"),
        ]
        analyzer = HeuristicAnalyzer()
        result = analyzer.analyze(blocks, "doc-2")
        section_types = [s.section_type for s in result.sections]
        assert "patient_information" in section_types
        assert "provider_information" in section_types
        assert "emergency_contact" in section_types

    def test_column_header_section_detection(self):
        blocks = [
            _make_block("Alice", col_header="Student Name", block_type="table_cell"),
            _make_block("10", col_header="Grade", block_type="table_cell"),
        ]
        analyzer = HeuristicAnalyzer()
        result = analyzer.analyze(blocks, "doc-3")
        assert len(result.sections) >= 1
        assert result.sections[0].section_type == "student_information"

    def test_multi_page_section(self):
        blocks = [
            _make_block("Patient Information", page=1),
            _make_block("Name: John Doe", page=1),
            _make_block("Address: 123 Main St", page=2),
            _make_block("Provider Information", page=3),
        ]
        analyzer = HeuristicAnalyzer()
        result = analyzer.analyze(blocks, "doc-4")
        patient_section = next(
            s for s in result.sections if s.section_type == "patient_information"
        )
        assert patient_section.page_start == 1
        assert patient_section.page_end == 2

    def test_section_confidence_is_reasonable(self):
        blocks = [
            _make_block("Student Information"),
            _make_block("Name: Alice"),
        ]
        analyzer = HeuristicAnalyzer()
        result = analyzer.analyze(blocks, "doc-5")
        for section in result.sections:
            assert 0.0 < section.confidence <= 1.0


# ===================================================================
# TestEntityRoleAssignment
# ===================================================================

class TestEntityRoleAssignment:
    """Section → role mapping and default behavior."""

    def test_patient_section_maps_to_primary_subject(self):
        blocks = [
            _make_block("Patient Information"),
            _make_block("Name: John Doe"),
        ]
        analyzer = HeuristicAnalyzer()
        result = analyzer.analyze(blocks, "doc-1")
        roles = {er.block_index: er.entity_role for er in result.entity_roles}
        # Block 0 and 1 should be primary_subject (in patient_information section)
        assert roles[0] == "primary_subject"
        assert roles[1] == "primary_subject"

    def test_provider_section_maps_to_provider(self):
        blocks = [
            _make_block("Provider Information"),
            _make_block("Dr. Smith"),
        ]
        analyzer = HeuristicAnalyzer()
        result = analyzer.analyze(blocks, "doc-2")
        roles = {er.block_index: er.entity_role for er in result.entity_roles}
        assert roles[0] == "provider"

    def test_unassigned_blocks_get_unknown_role(self):
        blocks = [
            _make_block("Some random text without section heading"),
        ]
        analyzer = HeuristicAnalyzer()
        result = analyzer.analyze(blocks, "doc-3")
        assert len(result.entity_roles) == 1
        assert result.entity_roles[0].entity_role == "unknown"
        assert result.entity_roles[0].confidence == 0.0

    def test_all_section_types_have_role_mapping(self):
        """Every valid section type must map to a valid entity role."""
        for section_type in VALID_SECTION_TYPES:
            role = SECTION_TO_ROLE.get(section_type, "unknown")
            assert role in VALID_ENTITY_ROLES, f"No role mapping for {section_type}"

    def test_emergency_contact_is_secondary(self):
        blocks = [
            _make_block("Emergency Contact"),
            _make_block("Jane Doe, 555-1234"),
        ]
        analyzer = HeuristicAnalyzer()
        result = analyzer.analyze(blocks, "doc-4")
        roles = {er.block_index: er.entity_role for er in result.entity_roles}
        assert roles[0] == "secondary_contact"


# ===================================================================
# TestProtocolRelevance
# ===================================================================

class TestProtocolRelevance:
    """Protocol → entity role relevance mapping."""

    def test_hipaa_primary_is_target(self):
        assert get_role_relevance("hipaa_breach_rule", "primary_subject") == TARGET

    def test_hipaa_provider_is_non_target(self):
        assert get_role_relevance("hipaa_breach_rule", "provider") == NON_TARGET

    def test_hipaa_institutional_is_non_target(self):
        assert get_role_relevance("hipaa_breach_rule", "institutional") == NON_TARGET

    def test_ferpa_secondary_is_deprioritized(self):
        assert get_role_relevance("ferpa", "secondary_contact") == DEPRIORITIZE

    def test_ccpa_primary_is_target(self):
        assert get_role_relevance("ccpa", "primary_subject") == TARGET

    def test_gdpr_secondary_is_target(self):
        # GDPR protects all data subjects
        assert get_role_relevance("gdpr", "secondary_contact") == TARGET

    def test_unknown_protocol_defaults_to_target(self):
        assert get_role_relevance("nonexistent_protocol", "primary_subject") == TARGET
        assert get_role_relevance("nonexistent_protocol", "institutional") == TARGET

    def test_unknown_role_defaults_to_target(self):
        assert get_role_relevance("hipaa_breach_rule", "unknown") == TARGET

    def test_all_protocols_have_primary_as_target(self):
        for protocol_id, roles in PROTOCOL_TARGET_ROLES.items():
            assert roles.get("primary_subject") == TARGET, (
                f"Protocol {protocol_id} does not target primary_subject"
            )


# ===================================================================
# TestMultiPersonDocument
# ===================================================================

class TestMultiPersonDocument:
    """Documents containing PII from multiple person roles."""

    def test_student_and_parent_in_same_document(self):
        blocks = [
            _make_block("Student Information"),
            _make_block("Student Name: Alice Johnson"),
            _make_block("Student ID: 12345"),
            _make_block("Parent/Guardian Information"),
            _make_block("Parent Name: Robert Johnson"),
            _make_block("Parent Phone: 555-0123"),
        ]
        analyzer = HeuristicAnalyzer()
        result = analyzer.analyze(blocks, "doc-1")

        roles = {er.block_index: er.entity_role for er in result.entity_roles}
        # Student blocks → primary_subject
        assert roles[0] == "primary_subject"
        assert roles[1] == "primary_subject"
        assert roles[2] == "primary_subject"
        # Parent blocks → secondary_contact
        assert roles[3] == "secondary_contact"
        assert roles[4] == "secondary_contact"
        assert roles[5] == "secondary_contact"

    def test_patient_and_provider_in_same_document(self):
        blocks = [
            _make_block("Patient Information"),
            _make_block("Patient: Jane Doe, SSN: 123-45-6789"),
            _make_block("Provider Information"),
            _make_block("Attending Physician: Dr. Smith, NPI: 1234567890"),
        ]
        analyzer = HeuristicAnalyzer()
        result = analyzer.analyze(blocks, "doc-2")

        roles = {er.block_index: er.entity_role for er in result.entity_roles}
        assert roles[0] == "primary_subject"
        assert roles[1] == "primary_subject"
        assert roles[2] == "provider"
        assert roles[3] == "provider"


# ===================================================================
# TestMasking
# ===================================================================

class TestMasking:
    """PII masking for LLM prompts."""

    def test_ssn_masked(self):
        result = mask_text_for_llm("SSN: 123-45-6789")
        assert "123-45-6789" not in result
        assert "[SSN]" in result

    def test_email_masked(self):
        result = mask_text_for_llm("Email: john@example.com")
        assert "john@example.com" not in result
        assert "[EMAIL]" in result

    def test_phone_masked(self):
        result = mask_text_for_llm("Phone: 555-123-4567")
        assert "555-123-4567" not in result
        assert "[PHONE]" in result

    def test_credit_card_masked(self):
        result = mask_text_for_llm("Card: 4111 1111 1111 1111")
        assert "4111 1111 1111 1111" not in result
        assert "[CREDIT_CARD]" in result

    def test_clean_text_unchanged(self):
        text = "Hello, this is a normal sentence about document analysis."
        assert mask_text_for_llm(text) == text

    def test_multiple_pii_in_one_string(self):
        text = "SSN: 123-45-6789, Email: test@example.com"
        result = mask_text_for_llm(text)
        assert "[SSN]" in result
        assert "[EMAIL]" in result
        assert "123-45-6789" not in result


# ===================================================================
# TestLLMAnalyzer
# ===================================================================

class TestLLMAnalyzer:
    """LLM structure analyzer with mocked Ollama."""

    def test_governance_gate_returns_none_when_disabled(self):
        """When LLM is disabled, analyze() should return None."""
        with patch("app.structure.llm_analyzer.OllamaClient") as mock_cls:
            from app.llm.client import LLMDisabledError
            instance = mock_cls.return_value
            instance.generate.side_effect = LLMDisabledError("disabled")

            analyzer = LLMStructureAnalyzer()
            analyzer.client = instance
            result = analyzer.analyze([_make_block("test")], "doc-1")
            assert result is None

    def test_successful_llm_response_parsed(self):
        """Valid JSON response is parsed into DocumentStructureAnalysis."""
        llm_response = json.dumps({
            "document_type": "medical_record",
            "confidence": 0.85,
            "sections": [
                {
                    "section_type": "patient_information",
                    "page_start": 1,
                    "page_end": 2,
                    "block_indices": [0, 1],
                    "confidence": 0.9,
                }
            ],
            "entity_roles": [
                {
                    "block_index": 0,
                    "entity_role": "primary_subject",
                    "confidence": 0.85,
                    "section_type": "patient_information",
                }
            ],
        })

        with patch("app.structure.llm_analyzer.OllamaClient") as mock_cls:
            instance = mock_cls.return_value
            instance.generate.return_value = llm_response

            analyzer = LLMStructureAnalyzer()
            analyzer.client = instance
            blocks = [_make_block("Patient Name"), _make_block("John Doe")]
            result = analyzer._do_analyze(blocks, "doc-1")

            assert result.document_type == "medical_record"
            assert result.document_type_confidence == 0.85
            assert len(result.sections) == 1
            assert result.sections[0].section_type == "patient_information"
            assert len(result.entity_roles) == 1

    def test_invalid_json_returns_none(self):
        """Invalid JSON causes analyze() to return None (not raise)."""
        with patch("app.structure.llm_analyzer.OllamaClient") as mock_cls:
            instance = mock_cls.return_value
            instance.generate.return_value = "not valid json at all"

            analyzer = LLMStructureAnalyzer()
            analyzer.client = instance
            result = analyzer.analyze([_make_block("test")], "doc-1")
            assert result is None

    def test_merge_heuristic_wins_on_document_type(self):
        """Heuristic document type wins when not unknown."""
        heuristic = DocumentStructureAnalysis(
            document_id="doc-1",
            document_type="medical_record",
            document_type_confidence=0.7,
            detected_by="heuristic",
        )
        llm = DocumentStructureAnalysis(
            document_id="doc-1",
            document_type="student_file",
            document_type_confidence=0.9,
            detected_by="llm",
        )
        merged = merge_analyses(heuristic, llm)
        assert merged.document_type == "medical_record"
        assert merged.detected_by == "heuristic+llm"

    def test_merge_llm_fills_unknown_document_type(self):
        """LLM document type used when heuristic is 'unknown'."""
        heuristic = DocumentStructureAnalysis(
            document_id="doc-1",
            document_type="unknown",
            document_type_confidence=0.0,
            detected_by="heuristic",
        )
        llm = DocumentStructureAnalysis(
            document_id="doc-1",
            document_type="financial_statement",
            document_type_confidence=0.8,
            detected_by="llm",
        )
        merged = merge_analyses(heuristic, llm)
        assert merged.document_type == "financial_statement"

    def test_merge_llm_none_returns_heuristic(self):
        heuristic = DocumentStructureAnalysis(
            document_id="doc-1",
            document_type="medical_record",
            document_type_confidence=0.7,
            detected_by="heuristic",
        )
        merged = merge_analyses(heuristic, None)
        assert merged is heuristic

    def test_merge_adds_non_overlapping_llm_sections(self):
        heuristic = DocumentStructureAnalysis(
            document_id="doc-1",
            document_type="medical_record",
            document_type_confidence=0.7,
            detected_by="heuristic",
            sections=[
                SectionAnnotation(
                    section_type="patient_information",
                    page_start=1, page_end=1,
                    block_indices=(0, 1),
                    confidence=0.85, detected_by="heuristic",
                ),
            ],
        )
        llm = DocumentStructureAnalysis(
            document_id="doc-1",
            document_type="medical_record",
            document_type_confidence=0.9,
            detected_by="llm",
            sections=[
                SectionAnnotation(
                    section_type="provider_information",
                    page_start=2, page_end=2,
                    block_indices=(2, 3),
                    confidence=0.8, detected_by="llm",
                ),
            ],
        )
        merged = merge_analyses(heuristic, llm)
        assert len(merged.sections) == 2
        section_types = {s.section_type for s in merged.sections}
        assert "patient_information" in section_types
        assert "provider_information" in section_types

    def test_merge_does_not_add_overlapping_llm_sections(self):
        heuristic = DocumentStructureAnalysis(
            document_id="doc-1",
            document_type="medical_record",
            document_type_confidence=0.7,
            detected_by="heuristic",
            sections=[
                SectionAnnotation(
                    section_type="patient_information",
                    page_start=1, page_end=1,
                    block_indices=(0, 1, 2),
                    confidence=0.85, detected_by="heuristic",
                ),
            ],
        )
        llm = DocumentStructureAnalysis(
            document_id="doc-1",
            document_type="medical_record",
            document_type_confidence=0.9,
            detected_by="llm",
            sections=[
                SectionAnnotation(
                    section_type="emergency_contact",
                    page_start=1, page_end=1,
                    block_indices=(1, 2),  # overlaps with heuristic
                    confidence=0.8, detected_by="llm",
                ),
            ],
        )
        merged = merge_analyses(heuristic, llm)
        assert len(merged.sections) == 1  # overlapping section not added

    def test_merge_llm_fills_unknown_roles(self):
        heuristic = DocumentStructureAnalysis(
            document_id="doc-1",
            document_type="medical_record",
            document_type_confidence=0.7,
            detected_by="heuristic",
            entity_roles=[
                EntityRoleAnnotation(block_index=0, entity_role="primary_subject", confidence=0.8),
                EntityRoleAnnotation(block_index=1, entity_role="unknown", confidence=0.0),
            ],
        )
        llm = DocumentStructureAnalysis(
            document_id="doc-1",
            document_type="medical_record",
            document_type_confidence=0.9,
            detected_by="llm",
            entity_roles=[
                EntityRoleAnnotation(block_index=1, entity_role="provider", confidence=0.7),
            ],
        )
        merged = merge_analyses(heuristic, llm)
        roles = {er.block_index: er.entity_role for er in merged.entity_roles}
        assert roles[0] == "primary_subject"  # heuristic kept
        assert roles[1] == "provider"  # LLM fills unknown


# ===================================================================
# TestConfidenceAdjustment
# ===================================================================

class TestConfidenceAdjustment:
    """Layer 2 entity role confidence nudges."""

    def test_institutional_reduces_score(self):
        block = _make_block("General Hospital, 123 Medical Drive")
        result = _make_presidio_result(block, "LOCATION", 0.70)

        classifier = Layer2ContextClassifier()
        updated = classifier.classify(result, block.text, entity_role="institutional")
        assert updated.score < 0.70  # reduced by 0.15

    def test_primary_subject_boosts_score(self):
        block = _make_block("Patient address: 123 Main Street")
        result = _make_presidio_result(block, "LOCATION", 0.70)

        classifier = Layer2ContextClassifier()
        updated = classifier.classify(result, block.text, entity_role="primary_subject")
        # Should get context boost from "address" keyword + primary boost
        assert updated.score > 0.70

    def test_no_role_leaves_score_unchanged_without_signal(self):
        block = _make_block("some random text here")
        result = _make_presidio_result(block, "IP_ADDRESS", 0.70)

        classifier = Layer2ContextClassifier()
        updated = classifier.classify(result, block.text)
        # No context signal, no role → score unchanged
        assert updated.score == 0.70

    def test_institutional_score_floors_at_zero(self):
        block = _make_block("text")
        result = _make_presidio_result(block, "PERSON", 0.05)

        classifier = Layer2ContextClassifier()
        updated = classifier.classify(result, block.text, entity_role="institutional")
        assert updated.score >= 0.0


# ===================================================================
# TestRRACrossRolePrevention
# ===================================================================

class TestRRACrossRolePrevention:
    """Cross-role merge prevention in entity resolution."""

    def test_primary_and_institutional_never_merge(self):
        r1 = PIIRecord(
            record_id="r1", entity_type="PERSON",
            normalized_value="john doe",
            raw_name="John Doe", raw_email="john@example.com",
            entity_role="primary_subject",
        )
        r2 = PIIRecord(
            record_id="r2", entity_type="PERSON",
            normalized_value="john doe",
            raw_name="John Doe", raw_email="john@example.com",
            entity_role="institutional",
        )
        conf = build_confidence(r1, r2)
        assert conf == 0.0

    def test_primary_and_provider_never_merge(self):
        r1 = PIIRecord(
            record_id="r1", entity_type="SSN",
            normalized_value="123456789",
            raw_name="Jane Smith",
            entity_role="primary_subject",
        )
        r2 = PIIRecord(
            record_id="r2", entity_type="SSN",
            normalized_value="123456789",
            raw_name="Jane Smith",
            entity_role="provider",
        )
        conf = build_confidence(r1, r2)
        assert conf == 0.0

    def test_same_role_records_can_merge(self):
        r1 = PIIRecord(
            record_id="r1", entity_type="SSN",
            normalized_value="123456789",
            raw_name="John Doe",
            entity_role="primary_subject",
        )
        r2 = PIIRecord(
            record_id="r2", entity_type="SSN",
            normalized_value="123456789",
            raw_name="John Doe",
            entity_role="primary_subject",
        )
        conf = build_confidence(r1, r2)
        assert conf > 0.0

    def test_none_role_does_not_prevent_merge(self):
        """Records without entity_role should still merge normally."""
        r1 = PIIRecord(
            record_id="r1", entity_type="SSN",
            normalized_value="123456789",
            raw_name="John Doe",
            entity_role=None,
        )
        r2 = PIIRecord(
            record_id="r2", entity_type="SSN",
            normalized_value="123456789",
            raw_name="John Doe",
            entity_role=None,
        )
        conf = build_confidence(r1, r2)
        assert conf > 0.0

    def test_resolver_separates_cross_role_records(self):
        """EntityResolver should not merge primary + institutional."""
        records = [
            PIIRecord(
                record_id="r1", entity_type="PERSON",
                normalized_value="john doe",
                raw_name="John Doe", raw_email="john@example.com",
                entity_role="primary_subject",
            ),
            PIIRecord(
                record_id="r2", entity_type="PERSON",
                normalized_value="john doe",
                raw_name="John Doe", raw_email="john@example.com",
                entity_role="institutional",
            ),
        ]
        resolver = EntityResolver()
        groups = resolver.resolve(records)
        # Should be 2 separate groups
        assert len(groups) == 2

    def test_secondary_and_primary_can_still_merge(self):
        """secondary_contact and primary_subject are not blocked."""
        r1 = PIIRecord(
            record_id="r1", entity_type="SSN",
            normalized_value="123456789",
            raw_name="Alice Smith",
            entity_role="primary_subject",
        )
        r2 = PIIRecord(
            record_id="r2", entity_type="SSN",
            normalized_value="123456789",
            raw_name="Alice Smith",
            entity_role="secondary_contact",
        )
        conf = build_confidence(r1, r2)
        # Not blocked — they can merge (same SSN + name)
        assert conf > 0.0


# ===================================================================
# TestAnnotateResults
# ===================================================================

class TestAnnotateResults:
    """annotate_results_with_structure integration."""

    def test_results_annotated_with_roles(self):
        blocks = [
            _make_block("Patient Name: John"),
            _make_block("Provider: Dr. Smith"),
        ]
        structure = DocumentStructureAnalysis(
            document_id="doc-1",
            document_type="medical_record",
            document_type_confidence=0.8,
            detected_by="heuristic",
            entity_roles=[
                EntityRoleAnnotation(block_index=0, entity_role="primary_subject", confidence=0.8),
                EntityRoleAnnotation(block_index=1, entity_role="provider", confidence=0.7),
            ],
        )
        results = [
            DetectionResult(
                entity_type="PERSON", value="John", confidence=0.9,
                extraction_layer="layer_1_pattern", pattern_used="test",
                source_block=blocks[0], start_char=0, end_char=4,
            ),
            DetectionResult(
                entity_type="PERSON", value="Dr. Smith", confidence=0.85,
                extraction_layer="layer_1_pattern", pattern_used="test",
                source_block=blocks[1], start_char=0, end_char=9,
            ),
        ]

        annotated = annotate_results_with_structure(results, blocks, structure)
        assert annotated[0].entity_role == "primary_subject"
        assert annotated[0].entity_role_confidence == 0.8
        assert annotated[1].entity_role == "provider"
        assert annotated[1].entity_role_confidence == 0.7

    def test_none_structure_leaves_results_unchanged(self):
        blocks = [_make_block("test")]
        results = [
            DetectionResult(
                entity_type="PERSON", value="test", confidence=0.9,
                extraction_layer="layer_1_pattern", pattern_used="test",
                source_block=blocks[0], start_char=0, end_char=4,
            ),
        ]
        annotated = annotate_results_with_structure(results, blocks, None)
        assert annotated[0].entity_role is None


# ===================================================================
# TestStructureAnalysisTask
# ===================================================================

class TestStructureAnalysisTask:
    """Pipeline task integration."""

    def test_heuristic_only_when_llm_disabled(self):
        blocks = [
            _make_block("Patient Information"),
            _make_block("Name: John Doe"),
        ]
        with patch("app.tasks.structure_analysis.get_settings") as mock_settings:
            mock_settings.return_value.llm_assist_enabled = False
            task = StructureAnalysisTask()
            result = task.run(blocks, "doc-1")
            assert result.detected_by == "heuristic"
            assert result.document_type != ""


# ===================================================================
# TestSerialization
# ===================================================================

class TestSerialization:
    """DocumentStructureAnalysis to_dict / from_dict round-trip."""

    def test_round_trip(self):
        original = DocumentStructureAnalysis(
            document_id="doc-1",
            document_type="medical_record",
            document_type_confidence=0.85,
            detected_by="heuristic",
            sections=[
                SectionAnnotation(
                    section_type="patient_information",
                    page_start=1, page_end=2,
                    block_indices=(0, 1, 2),
                    confidence=0.9, detected_by="heuristic",
                ),
            ],
            entity_roles=[
                EntityRoleAnnotation(
                    block_index=0,
                    entity_role="primary_subject",
                    confidence=0.8,
                    section_type="patient_information",
                ),
            ],
        )
        d = original.to_dict()
        restored = DocumentStructureAnalysis.from_dict(d)

        assert restored.document_id == original.document_id
        assert restored.document_type == original.document_type
        assert restored.document_type_confidence == original.document_type_confidence
        assert len(restored.sections) == 1
        assert restored.sections[0].section_type == "patient_information"
        assert restored.sections[0].block_indices == (0, 1, 2)
        assert len(restored.entity_roles) == 1
        assert restored.entity_roles[0].entity_role == "primary_subject"

    def test_json_serializable(self):
        analysis = DocumentStructureAnalysis(
            document_id="doc-1",
            document_type="unknown",
            document_type_confidence=0.0,
            detected_by="heuristic",
        )
        # Should not raise
        json_str = json.dumps(analysis.to_dict())
        assert isinstance(json_str, str)


# ===================================================================
# TestSchemaColumns
# ===================================================================

class TestSchemaColumns:
    """New columns exist on documents and extractions tables."""

    def test_structure_analysis_column_on_documents(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(bind=engine)
        inspector = inspect(engine)
        cols = {c["name"]: c for c in inspector.get_columns("documents")}
        assert "structure_analysis" in cols
        assert cols["structure_analysis"]["nullable"] is True

    def test_entity_role_columns_on_extractions(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(bind=engine)
        inspector = inspect(engine)
        cols = {c["name"]: c for c in inspector.get_columns("extractions")}
        assert "entity_role" in cols
        assert cols["entity_role"]["nullable"] is True
        assert "entity_role_confidence" in cols
        assert cols["entity_role_confidence"]["nullable"] is True


# ===================================================================
# TestValidTypes
# ===================================================================

class TestValidTypes:
    """Validation sets are comprehensive."""

    def test_all_document_types_are_valid(self):
        for doc_type in DOCUMENT_TYPE_SIGNALS:
            assert doc_type in VALID_DOCUMENT_TYPES

    def test_all_section_to_role_sections_are_valid(self):
        for section_type in SECTION_TO_ROLE:
            assert section_type in VALID_SECTION_TYPES

    def test_all_section_to_role_values_are_valid(self):
        for role in SECTION_TO_ROLE.values():
            assert role in VALID_ENTITY_ROLES

    def test_get_role_for_block_default(self):
        analysis = DocumentStructureAnalysis(
            document_id="doc-1",
            document_type="unknown",
            document_type_confidence=0.0,
            detected_by="heuristic",
        )
        assert analysis.get_role_for_block(999) == "unknown"
        assert analysis.get_role_confidence_for_block(999) == 0.0
