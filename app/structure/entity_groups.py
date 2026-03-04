"""Entity group data models for LLM entity relationship analysis.

Represents the LLM's understanding of how detected PII items relate
to each other and to real-world entities (people, organizations).

These dataclasses are serialized to JSON and stored on the Document
record (``documents.entity_analysis`` column).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class EntityGroupMember:
    """A single PII detection that belongs to an entity group."""

    pii_type: str
    value_ref: str  # masked or raw depending on pii_masking_enabled
    page: int | None = None
    confidence: float = 0.0

    def to_dict(self) -> dict:
        return {
            "pii_type": self.pii_type,
            "value_ref": self.value_ref,
            "page": self.page,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: dict) -> EntityGroupMember:
        return cls(
            pii_type=data.get("pii_type", ""),
            value_ref=data.get("value_ref", ""),
            page=data.get("page"),
            confidence=data.get("confidence", 0.0),
        )


@dataclass
class EntityGroup:
    """A group of PII detections that belong to the same entity."""

    group_id: str
    label: str  # human-readable label (e.g. "John Smith (Employee)")
    role: str  # primary_subject | institutional | provider | secondary_contact | unknown
    confidence: float
    members: list[EntityGroupMember] = field(default_factory=list)
    rationale: str = ""
    detected_by: str = "llm"  # "llm" | "heuristic" | "llm+heuristic"

    def to_dict(self) -> dict:
        return {
            "group_id": self.group_id,
            "label": self.label,
            "role": self.role,
            "confidence": self.confidence,
            "members": [m.to_dict() for m in self.members],
            "rationale": self.rationale,
            "detected_by": self.detected_by,
        }

    @classmethod
    def from_dict(cls, data: dict) -> EntityGroup:
        return cls(
            group_id=data.get("group_id", ""),
            label=data.get("label", ""),
            role=data.get("role", "unknown"),
            confidence=data.get("confidence", 0.0),
            members=[EntityGroupMember.from_dict(m) for m in data.get("members", [])],
            rationale=data.get("rationale", ""),
            detected_by=data.get("detected_by", "llm"),
        )


@dataclass
class EntityRelationship:
    """A relationship between two entity groups."""

    from_group: str  # group_id
    to_group: str  # group_id
    relationship_type: str  # employed_by | patient_of | parent_of | emergency_contact_for
    confidence: float = 0.0

    def to_dict(self) -> dict:
        return {
            "from_group": self.from_group,
            "to_group": self.to_group,
            "relationship_type": self.relationship_type,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: dict) -> EntityRelationship:
        return cls(
            from_group=data.get("from_group", ""),
            to_group=data.get("to_group", ""),
            relationship_type=data.get("relationship_type", ""),
            confidence=data.get("confidence", 0.0),
        )


@dataclass
class EntityRelationshipAnalysis:
    """Complete LLM analysis of entity relationships in a document."""

    document_id: str
    document_summary: str
    entity_groups: list[EntityGroup] = field(default_factory=list)
    relationships: list[EntityRelationship] = field(default_factory=list)
    estimated_unique_individuals: int = 0
    extraction_guidance: str = ""

    def to_dict(self) -> dict:
        return {
            "document_id": self.document_id,
            "document_summary": self.document_summary,
            "entity_groups": [g.to_dict() for g in self.entity_groups],
            "relationships": [r.to_dict() for r in self.relationships],
            "estimated_unique_individuals": self.estimated_unique_individuals,
            "extraction_guidance": self.extraction_guidance,
        }

    @classmethod
    def from_dict(cls, data: dict) -> EntityRelationshipAnalysis:
        return cls(
            document_id=data.get("document_id", ""),
            document_summary=data.get("document_summary", ""),
            entity_groups=[EntityGroup.from_dict(g) for g in data.get("entity_groups", [])],
            relationships=[EntityRelationship.from_dict(r) for r in data.get("relationships", [])],
            estimated_unique_individuals=data.get("estimated_unique_individuals", 0),
            extraction_guidance=data.get("extraction_guidance", ""),
        )
