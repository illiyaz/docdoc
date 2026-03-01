"""Heuristic document structure analyzer — deterministic, no LLM.

Classifies document type via keyword density, detects sections via heading
patterns and column headers, and assigns entity roles per section.

This is the primary analyzer.  LLM analysis (when enabled) is additive
and merges into the heuristic result.
"""
from __future__ import annotations

import re
import logging
from collections import Counter

from app.readers.base import ExtractedBlock
from app.structure.models import (
    DocumentStructureAnalysis,
    DocumentType,
    EntityRole,
    EntityRoleAnnotation,
    SectionAnnotation,
    SectionType,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Document type keyword signals
# ---------------------------------------------------------------------------

DOCUMENT_TYPE_SIGNALS: dict[DocumentType, list[str]] = {
    "medical_record": [
        "patient", "diagnosis", "medical record", "physician",
        "hospital", "treatment", "mrn", "npi", "hipaa", "health plan",
        "prescription", "medication", "clinical", "prognosis",
    ],
    "student_file": [
        "student", "school", "grade", "enrollment", "ferpa",
        "transcript", "parent/guardian", "academic", "pupil",
        "semester", "gpa", "course",
    ],
    "financial_statement": [
        "account", "balance", "transaction", "bank",
        "routing number", "deposit", "credit card", "statement",
        "interest", "debit", "wire transfer",
    ],
    "employment_record": [
        "employee", "employer", "hire date", "salary",
        "department", "w-2", "payroll", "compensation",
        "benefits", "termination", "position",
    ],
    "insurance_document": [
        "policy", "premium", "insured", "beneficiary",
        "claim", "coverage", "deductible", "underwriter",
        "policyholder",
    ],
    "legal_document": [
        "plaintiff", "defendant", "court", "jurisdiction",
        "hereby", "whereas", "stipulation", "affidavit",
        "subpoena", "attorney",
    ],
    "correspondence": [
        "dear", "sincerely", "regards", "re:",
        "attached", "enclosed", "to whom it may concern",
    ],
    "form_fillable": [
        "fill in", "check one", "please print", "signature line",
        "date:", "name:", "applicant",
    ],
}

# ---------------------------------------------------------------------------
# Section heading patterns
# ---------------------------------------------------------------------------

SECTION_HEADING_PATTERNS: dict[SectionType, list[re.Pattern[str]]] = {
    "patient_information": [
        re.compile(r"patient\s+information", re.IGNORECASE),
        re.compile(r"patient\s+details", re.IGNORECASE),
        re.compile(r"patient\s+data", re.IGNORECASE),
        re.compile(r"patient\s+demographics", re.IGNORECASE),
    ],
    "provider_information": [
        re.compile(r"provider\s+information", re.IGNORECASE),
        re.compile(r"attending\s+physician", re.IGNORECASE),
        re.compile(r"physician\s+information", re.IGNORECASE),
        re.compile(r"provider\s+details", re.IGNORECASE),
        re.compile(r"referring\s+physician", re.IGNORECASE),
    ],
    "emergency_contact": [
        re.compile(r"emergency\s+contact", re.IGNORECASE),
        re.compile(r"next\s+of\s+kin", re.IGNORECASE),
        re.compile(r"in\s+case\s+of\s+emergency", re.IGNORECASE),
    ],
    "student_information": [
        re.compile(r"student\s+information", re.IGNORECASE),
        re.compile(r"pupil\s+record", re.IGNORECASE),
        re.compile(r"student\s+details", re.IGNORECASE),
        re.compile(r"student\s+data", re.IGNORECASE),
    ],
    "parent_guardian_information": [
        re.compile(r"parent\s*[/&]\s*guardian", re.IGNORECASE),
        re.compile(r"parent\s+information", re.IGNORECASE),
        re.compile(r"guardian\s+information", re.IGNORECASE),
        re.compile(r"parent\s+details", re.IGNORECASE),
    ],
    "school_information": [
        re.compile(r"school\s+information", re.IGNORECASE),
        re.compile(r"institution\s+information", re.IGNORECASE),
        re.compile(r"school\s+details", re.IGNORECASE),
    ],
    "employee_information": [
        re.compile(r"employee\s+information", re.IGNORECASE),
        re.compile(r"employee\s+details", re.IGNORECASE),
        re.compile(r"employee\s+data", re.IGNORECASE),
        re.compile(r"worker\s+information", re.IGNORECASE),
    ],
    "employer_information": [
        re.compile(r"employer\s+information", re.IGNORECASE),
        re.compile(r"employer\s+details", re.IGNORECASE),
        re.compile(r"company\s+information", re.IGNORECASE),
    ],
    "account_holder_information": [
        re.compile(r"account\s+holder", re.IGNORECASE),
        re.compile(r"cardholder\s+information", re.IGNORECASE),
        re.compile(r"customer\s+information", re.IGNORECASE),
    ],
    "financial_institution": [
        re.compile(r"financial\s+institution", re.IGNORECASE),
        re.compile(r"bank\s+information", re.IGNORECASE),
        re.compile(r"issuing\s+bank", re.IGNORECASE),
    ],
    "header_footer": [
        re.compile(r"page\s+\d+\s+of\s+\d+", re.IGNORECASE),
        re.compile(r"confidential", re.IGNORECASE),
    ],
    "legal_boilerplate": [
        re.compile(r"terms\s+and\s+conditions", re.IGNORECASE),
        re.compile(r"privacy\s+notice", re.IGNORECASE),
        re.compile(r"disclaimer", re.IGNORECASE),
    ],
}

# Column header keywords → section type (for tabular documents)
COLUMN_HEADER_TO_SECTION: dict[str, SectionType] = {
    "student name": "student_information",
    "student id": "student_information",
    "pupil": "student_information",
    "patient name": "patient_information",
    "patient id": "patient_information",
    "mrn": "patient_information",
    "employee name": "employee_information",
    "employee id": "employee_information",
    "parent name": "parent_guardian_information",
    "guardian": "parent_guardian_information",
    "provider": "provider_information",
    "physician": "provider_information",
    "school name": "school_information",
    "institution": "school_information",
    "employer": "employer_information",
    "company": "employer_information",
    "account holder": "account_holder_information",
    "cardholder": "account_holder_information",
    "bank name": "financial_institution",
}

# ---------------------------------------------------------------------------
# Section → entity role mapping
# ---------------------------------------------------------------------------

SECTION_TO_ROLE: dict[SectionType, EntityRole] = {
    "patient_information": "primary_subject",
    "student_information": "primary_subject",
    "employee_information": "primary_subject",
    "account_holder_information": "primary_subject",
    "provider_information": "provider",
    "school_information": "institutional",
    "employer_information": "institutional",
    "financial_institution": "institutional",
    "parent_guardian_information": "secondary_contact",
    "emergency_contact": "secondary_contact",
    "header_footer": "institutional",
    "legal_boilerplate": "institutional",
    "unknown": "unknown",
}

# Number of leading pages to scan for document type classification
_DOC_TYPE_SCAN_PAGES = 5


# ---------------------------------------------------------------------------
# HeuristicAnalyzer
# ---------------------------------------------------------------------------

class HeuristicAnalyzer:
    """Deterministic document structure analyzer.

    Classifies document type, detects sections, and assigns entity roles
    using keyword density and heading pattern matching only.
    """

    def analyze(
        self,
        blocks: list[ExtractedBlock],
        document_id: str,
    ) -> DocumentStructureAnalysis:
        """Run full heuristic analysis on the given blocks.

        Parameters
        ----------
        blocks:
            All ExtractedBlocks from the document, in order.
        document_id:
            UUID string for the document being analyzed.

        Returns
        -------
        DocumentStructureAnalysis
            Complete analysis with document type, sections, and entity roles.
        """
        doc_type, doc_type_confidence = self._classify_document_type(blocks)
        sections = self._detect_sections(blocks)
        entity_roles = self._assign_entity_roles(blocks, sections)

        logger.debug(
            "Heuristic analysis: doc_type=%s confidence=%.2f sections=%d roles=%d",
            doc_type, doc_type_confidence, len(sections), len(entity_roles),
        )

        return DocumentStructureAnalysis(
            document_id=document_id,
            document_type=doc_type,
            document_type_confidence=doc_type_confidence,
            detected_by="heuristic",
            sections=sections,
            entity_roles=entity_roles,
        )

    # -- Document type classification -----------------------------------------

    def _classify_document_type(
        self,
        blocks: list[ExtractedBlock],
    ) -> tuple[DocumentType, float]:
        """Classify document type via keyword density on leading pages."""
        # Collect text from first N pages
        leading_text_parts: list[str] = []
        for b in blocks:
            page = b.page_or_sheet
            if isinstance(page, int) and page > _DOC_TYPE_SCAN_PAGES:
                continue
            leading_text_parts.append(b.text.lower())

        if not leading_text_parts:
            return "unknown", 0.0

        combined = " ".join(leading_text_parts)
        word_count = max(len(combined.split()), 1)

        # Count keyword hits per type
        type_scores: Counter[str] = Counter()
        for doc_type, keywords in DOCUMENT_TYPE_SIGNALS.items():
            hits = sum(1 for kw in keywords if kw in combined)
            type_scores[doc_type] = hits

        if not type_scores or type_scores.most_common(1)[0][1] == 0:
            return "unknown", 0.0

        best_type, best_hits = type_scores.most_common(1)[0]
        # Confidence = normalized hit density with margin over runner-up
        density = best_hits / word_count
        runner_up_hits = type_scores.most_common(2)[1][1] if len(type_scores) > 1 else 0
        margin = (best_hits - runner_up_hits) / max(best_hits, 1)

        # Combine density and margin into confidence (capped at 1.0)
        confidence = min(1.0, density * 50 + margin * 0.5)
        # Floor at 0.3 if we have any hits, cap at 0.95 for heuristic
        confidence = max(0.3, min(0.95, confidence))

        return best_type, confidence  # type: ignore[return-value]

    # -- Section detection ----------------------------------------------------

    def _detect_sections(
        self,
        blocks: list[ExtractedBlock],
    ) -> list[SectionAnnotation]:
        """Detect document sections via heading patterns and column headers."""
        sections: list[SectionAnnotation] = []
        current_section_type: SectionType | None = None
        current_section_start_page: int = 0
        current_section_blocks: list[int] = []

        for idx, block in enumerate(blocks):
            detected_section = self._match_section_heading(block.text)

            # Also check column headers for tabular data
            if detected_section is None and block.col_header:
                detected_section = self._match_column_header(block.col_header)

            if detected_section is not None and detected_section != current_section_type:
                # Close previous section
                if current_section_type is not None:
                    page = block.page_or_sheet if isinstance(block.page_or_sheet, int) else 0
                    end_page = page - 1 if page > current_section_start_page else current_section_start_page
                    sections.append(SectionAnnotation(
                        section_type=current_section_type,
                        page_start=current_section_start_page,
                        page_end=end_page,
                        block_indices=tuple(current_section_blocks),
                        confidence=0.85,
                        detected_by="heuristic",
                    ))

                # Open new section
                current_section_type = detected_section
                current_section_start_page = (
                    block.page_or_sheet if isinstance(block.page_or_sheet, int) else 0
                )
                current_section_blocks = [idx]
            elif current_section_type is not None:
                current_section_blocks.append(idx)

        # Close final section
        if current_section_type is not None and current_section_blocks:
            last_block = blocks[current_section_blocks[-1]]
            end_page = (
                last_block.page_or_sheet
                if isinstance(last_block.page_or_sheet, int)
                else current_section_start_page
            )
            sections.append(SectionAnnotation(
                section_type=current_section_type,
                page_start=current_section_start_page,
                page_end=end_page,
                block_indices=tuple(current_section_blocks),
                confidence=0.85,
                detected_by="heuristic",
            ))

        return sections

    def _match_section_heading(self, text: str) -> SectionType | None:
        """Check if text matches any section heading pattern."""
        for section_type, patterns in SECTION_HEADING_PATTERNS.items():
            for pat in patterns:
                if pat.search(text):
                    return section_type
        return None

    def _match_column_header(self, col_header: str) -> SectionType | None:
        """Match a column header string to a section type."""
        header_lower = col_header.lower().strip()
        for keyword, section_type in COLUMN_HEADER_TO_SECTION.items():
            if keyword in header_lower:
                return section_type
        return None

    # -- Entity role assignment -----------------------------------------------

    def _assign_entity_roles(
        self,
        blocks: list[ExtractedBlock],
        sections: list[SectionAnnotation],
    ) -> list[EntityRoleAnnotation]:
        """Assign entity roles to blocks based on their section membership."""
        # Build block_index → section mapping
        block_to_section: dict[int, SectionAnnotation] = {}
        for section in sections:
            for bi in section.block_indices:
                block_to_section[bi] = section

        roles: list[EntityRoleAnnotation] = []
        for idx in range(len(blocks)):
            section = block_to_section.get(idx)
            if section is not None:
                role = SECTION_TO_ROLE.get(section.section_type, "unknown")
                roles.append(EntityRoleAnnotation(
                    block_index=idx,
                    entity_role=role,
                    confidence=section.confidence * 0.9,  # slightly lower than section confidence
                    section_type=section.section_type,
                ))
            else:
                roles.append(EntityRoleAnnotation(
                    block_index=idx,
                    entity_role="unknown",
                    confidence=0.0,
                    section_type=None,
                ))

        return roles
