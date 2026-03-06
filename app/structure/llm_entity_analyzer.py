"""LLM-assisted entity relationship analyzer.

Reads document content + sample PII detections and uses Ollama to understand
which PII items belong to which person, propose entity groups, and explain
relationships.

Gated behind ``llm_assist_enabled``.  All calls are audit-logged.
Graceful fallback: if LLM fails, returns None (Presidio-only detection proceeds).
"""
from __future__ import annotations

import json
import logging

from sqlalchemy.orm import Session

from app.core.settings import get_settings
from app.llm.client import LLMDisabledError, OllamaClient
from app.llm.prompts import ANALYZE_ENTITY_RELATIONSHIPS, SYSTEM_PROMPT
from app.pii.presidio_engine import DetectionResult
from app.readers.base import ExtractedBlock
from app.structure.entity_groups import (
    EntityGroup,
    EntityGroupMember,
    EntityRelationship,
    EntityRelationshipAnalysis,
)
from app.structure.masking import mask_text_for_llm

logger = logging.getLogger(__name__)

_MAX_EXCERPT_CHARS = 4000
_MAX_BLOCKS = 40
_MAX_PII_ITEMS = 50


class LLMEntityAnalyzer:
    """Analyzes entity relationships in a document using LLM.

    Parameters
    ----------
    db_session:
        SQLAlchemy session for audit logging.  When ``None``, LLM calls
        are not persisted (useful for testing).
    """

    def __init__(self, db_session: Session | None = None) -> None:
        self.client = OllamaClient(db_session=db_session)

    def analyze(
        self,
        blocks: list[ExtractedBlock],
        sample_detections: list[DetectionResult],
        structure_analysis: dict | None,
        document_id: str,
        onset_page: int | str = 0,
        document_schema: dict | None = None,
    ) -> EntityRelationshipAnalysis | None:
        """Run LLM entity relationship analysis.

        Parameters
        ----------
        document_schema:
            Optional DocumentSchema dict (from Phase 14b LLM Document
            Understanding).  When provided, pre-seeds the analysis with
            schema context for better entity grouping.

        Returns ``None`` if LLM is disabled or the call fails.
        Never raises — failures are logged and swallowed.
        """
        try:
            return self._do_analyze(blocks, sample_detections, structure_analysis, document_id, onset_page, document_schema)
        except LLMDisabledError:
            logger.debug("LLM assist is disabled; skipping entity analysis")
            return None
        except Exception:
            logger.exception("LLM entity analysis failed for document %s", document_id)
            return None

    def _do_analyze(
        self,
        blocks: list[ExtractedBlock],
        sample_detections: list[DetectionResult],
        structure_analysis: dict | None,
        document_id: str,
        onset_page: int | str,
        document_schema: dict | None = None,
    ) -> EntityRelationshipAnalysis:
        """Internal analysis logic — may raise."""
        settings = get_settings()
        masking_on = settings.pii_masking_enabled

        # Build document excerpt
        excerpt = self._build_excerpt(blocks, masking_on)

        # Build PII detections summary
        pii_summary = self._build_pii_summary(sample_detections, masking_on)

        # Extract structure info
        doc_type = "unknown"
        structure_summary = "No structure analysis available"
        if structure_analysis:
            doc_type = structure_analysis.get("document_type", "unknown")
            sections = structure_analysis.get("sections", [])
            if sections:
                section_types = [s.get("section_type", "unknown") for s in sections]
                structure_summary = f"Document type: {doc_type}. Sections: {', '.join(section_types)}"
            else:
                structure_summary = f"Document type: {doc_type}"

        # Enrich with DocumentSchema context (Phase 14c)
        if document_schema:
            schema_type = document_schema.get("document_type", "")
            if schema_type and schema_type != "unknown":
                doc_type = schema_type
            issuer = document_schema.get("issuing_entity", "")
            people = document_schema.get("people", [])
            if people:
                people_info = ", ".join(
                    f"{p.get('name', '?')} ({p.get('role', '?')})"
                    for p in people[:5]
                )
                structure_summary += f". People: {people_info}"
            if issuer:
                structure_summary += f". Issuing entity: {issuer}"

        # Build prompt
        prompt = ANALYZE_ENTITY_RELATIONSHIPS.format(
            document_type=doc_type,
            structure_summary=structure_summary,
            onset_page=onset_page,
            document_excerpt=excerpt,
            pii_detections=pii_summary,
        )

        response_text = self.client.generate(
            prompt,
            system=SYSTEM_PROMPT,
            use_case="analyze_entity_relationships",
            document_id=document_id,
        )

        return self._parse_response(response_text, document_id)

    def _build_excerpt(self, blocks: list[ExtractedBlock], masking_on: bool) -> str:
        """Build excerpt from document blocks."""
        parts: list[str] = []
        total_chars = 0

        for i, block in enumerate(blocks[:_MAX_BLOCKS]):
            text = mask_text_for_llm(block.text) if masking_on else block.text
            line = f"[Page {block.page_or_sheet}]: {text}"
            if total_chars + len(line) > _MAX_EXCERPT_CHARS:
                break
            parts.append(line)
            total_chars += len(line)

        return "\n".join(parts)

    def _build_pii_summary(self, detections: list[DetectionResult], masking_on: bool) -> str:
        """Build a formatted list of PII detections for the LLM prompt."""
        if not detections:
            return "No PII detections found on this page."

        lines: list[str] = []
        for i, det in enumerate(detections[:_MAX_PII_ITEMS]):
            raw_text = det.block.text[det.start:det.end] if hasattr(det, "block") else ""
            if masking_on and raw_text:
                if len(raw_text) <= 4:
                    display = "*" * len(raw_text)
                else:
                    display = f"{'*' * (len(raw_text) - 4)}{raw_text[-4:]}"
            else:
                display = raw_text

            page = det.block.page_or_sheet if hasattr(det, "block") else "?"
            lines.append(
                f"  {i+1}. Type: {det.entity_type}, Value: \"{display}\", "
                f"Page: {page}, Confidence: {det.score:.2f}"
            )

        return "\n".join(lines)

    def _parse_response(
        self,
        response_text: str,
        document_id: str,
    ) -> EntityRelationshipAnalysis:
        """Parse the LLM JSON response into EntityRelationshipAnalysis."""
        cleaned = response_text.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            lines = [ln for ln in lines if not ln.strip().startswith("```")]
            cleaned = "\n".join(lines)

        data = json.loads(cleaned)

        # Parse entity groups
        entity_groups: list[EntityGroup] = []
        for g_data in data.get("entity_groups", []):
            members: list[EntityGroupMember] = []
            for m_data in g_data.get("members", []):
                members.append(EntityGroupMember(
                    pii_type=m_data.get("pii_type", ""),
                    value_ref=m_data.get("value_ref", ""),
                    page=m_data.get("page"),
                    confidence=float(g_data.get("confidence", 0.0)),
                ))

            role = g_data.get("role", "unknown")
            if role not in ("primary_subject", "institutional", "provider", "secondary_contact", "unknown"):
                role = "unknown"

            entity_groups.append(EntityGroup(
                group_id=g_data.get("group_id", f"G{len(entity_groups)+1}"),
                label=g_data.get("label", "Unknown"),
                role=role,
                confidence=float(g_data.get("confidence", 0.0)),
                members=members,
                rationale=g_data.get("rationale", ""),
                detected_by="llm",
            ))

        # Parse relationships
        relationships: list[EntityRelationship] = []
        for r_data in data.get("relationships", []):
            relationships.append(EntityRelationship(
                from_group=r_data.get("from_group", r_data.get("from", "")),
                to_group=r_data.get("to_group", r_data.get("to", "")),
                relationship_type=r_data.get("relationship_type", r_data.get("type", "")),
                confidence=float(r_data.get("confidence", 0.0)),
            ))

        return EntityRelationshipAnalysis(
            document_id=document_id,
            document_summary=data.get("document_summary", ""),
            entity_groups=entity_groups,
            relationships=relationships,
            estimated_unique_individuals=int(data.get("estimated_unique_individuals", 0)),
            extraction_guidance=data.get("extraction_guidance", ""),
        )
