"""Document Structure Analysis data models.

Defines the types and dataclasses used by the DSA stage to annotate
documents with structural information: document type, sections, and
entity role attribution.

All types are plain Python -- no DB dependency.  The
``DocumentStructureAnalysis`` object is serialized to JSON and stored
in ``documents.structure_analysis``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

DocumentType = Literal[
    "medical_record",
    "student_file",
    "financial_statement",
    "employment_record",
    "insurance_document",
    "legal_document",
    "correspondence",
    "form_fillable",
    "unknown",
]

VALID_DOCUMENT_TYPES: frozenset[str] = frozenset({
    "medical_record",
    "student_file",
    "financial_statement",
    "employment_record",
    "insurance_document",
    "legal_document",
    "correspondence",
    "form_fillable",
    "unknown",
})

SectionType = Literal[
    "patient_information",
    "provider_information",
    "emergency_contact",
    "student_information",
    "parent_guardian_information",
    "school_information",
    "employee_information",
    "employer_information",
    "account_holder_information",
    "financial_institution",
    "header_footer",
    "legal_boilerplate",
    "unknown",
]

VALID_SECTION_TYPES: frozenset[str] = frozenset({
    "patient_information",
    "provider_information",
    "emergency_contact",
    "student_information",
    "parent_guardian_information",
    "school_information",
    "employee_information",
    "employer_information",
    "account_holder_information",
    "financial_institution",
    "header_footer",
    "legal_boilerplate",
    "unknown",
})

EntityRole = Literal[
    "primary_subject",
    "secondary_contact",
    "institutional",
    "provider",
    "unknown",
]

VALID_ENTITY_ROLES: frozenset[str] = frozenset({
    "primary_subject",
    "secondary_contact",
    "institutional",
    "provider",
    "unknown",
})

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SectionAnnotation:
    """A detected section within a document (e.g. 'Patient Information')."""

    section_type: SectionType
    page_start: int
    page_end: int
    block_indices: tuple[int, ...]
    confidence: float
    detected_by: str  # "heuristic" | "llm"


@dataclass(frozen=True)
class EntityRoleAnnotation:
    """Role attribution for a specific block (e.g. block 3 → primary_subject)."""

    block_index: int
    entity_role: EntityRole
    confidence: float
    section_type: SectionType | None = None


@dataclass
class DocumentStructureAnalysis:
    """Complete structure analysis result for one document.

    Serialized to JSON and stored in ``documents.structure_analysis``.
    """

    document_id: str
    document_type: DocumentType
    document_type_confidence: float
    detected_by: str  # "heuristic" | "llm" | "heuristic+llm"
    sections: list[SectionAnnotation] = field(default_factory=list)
    entity_roles: list[EntityRoleAnnotation] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serialize to a JSON-compatible dict."""
        return {
            "document_id": self.document_id,
            "document_type": self.document_type,
            "document_type_confidence": self.document_type_confidence,
            "detected_by": self.detected_by,
            "sections": [
                {
                    "section_type": s.section_type,
                    "page_start": s.page_start,
                    "page_end": s.page_end,
                    "block_indices": list(s.block_indices),
                    "confidence": s.confidence,
                    "detected_by": s.detected_by,
                }
                for s in self.sections
            ],
            "entity_roles": [
                {
                    "block_index": er.block_index,
                    "entity_role": er.entity_role,
                    "confidence": er.confidence,
                    "section_type": er.section_type,
                }
                for er in self.entity_roles
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> DocumentStructureAnalysis:
        """Deserialize from a JSON-compatible dict."""
        sections = [
            SectionAnnotation(
                section_type=s["section_type"],
                page_start=s["page_start"],
                page_end=s["page_end"],
                block_indices=tuple(s["block_indices"]),
                confidence=s["confidence"],
                detected_by=s["detected_by"],
            )
            for s in data.get("sections", [])
        ]
        entity_roles = [
            EntityRoleAnnotation(
                block_index=er["block_index"],
                entity_role=er["entity_role"],
                confidence=er["confidence"],
                section_type=er.get("section_type"),
            )
            for er in data.get("entity_roles", [])
        ]
        return cls(
            document_id=data["document_id"],
            document_type=data["document_type"],
            document_type_confidence=data["document_type_confidence"],
            detected_by=data["detected_by"],
            sections=sections,
            entity_roles=entity_roles,
        )

    def get_role_for_block(self, block_index: int) -> EntityRole:
        """Return the entity role for a given block index, defaulting to 'unknown'."""
        for er in self.entity_roles:
            if er.block_index == block_index:
                return er.entity_role
        return "unknown"

    def get_role_confidence_for_block(self, block_index: int) -> float:
        """Return the role confidence for a given block index, defaulting to 0.0."""
        for er in self.entity_roles:
            if er.block_index == block_index:
                return er.confidence
        return 0.0
