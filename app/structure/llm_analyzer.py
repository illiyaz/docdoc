"""LLM-assisted document structure analyzer.

Sends masked document excerpts to Ollama for structure classification.
Additive only — merges with heuristic results (heuristic wins on conflict).

Gated behind ``llm_assist_enabled``.  All calls are audit-logged via
:func:`app.llm.audit.log_llm_call`.
"""
from __future__ import annotations

import json
import logging

from sqlalchemy.orm import Session

from app.llm.client import OllamaClient, LLMDisabledError
from app.llm.prompts import PROMPT_TEMPLATES, SYSTEM_PROMPT
from app.readers.base import ExtractedBlock
from app.structure.masking import mask_text_for_llm
from app.structure.models import (
    DocumentStructureAnalysis,
    DocumentType,
    EntityRoleAnnotation,
    SectionAnnotation,
    SectionType,
    VALID_DOCUMENT_TYPES,
    VALID_ENTITY_ROLES,
    VALID_SECTION_TYPES,
)

logger = logging.getLogger(__name__)

# Max characters to send to the LLM (keeps prompt size reasonable)
_MAX_EXCERPT_CHARS = 3000
_MAX_BLOCKS_FOR_EXCERPT = 30


class LLMStructureAnalyzer:
    """LLM-assisted document structure analyzer.

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
        document_id: str,
    ) -> DocumentStructureAnalysis | None:
        """Run LLM-assisted structure analysis.

        Returns ``None`` if LLM is disabled or the call fails.
        Never raises — failures are logged and swallowed.
        """
        try:
            return self._do_analyze(blocks, document_id)
        except LLMDisabledError:
            logger.debug("LLM assist is disabled; skipping structure analysis")
            return None
        except Exception:
            logger.exception("LLM structure analysis failed")
            return None

    def _do_analyze(
        self,
        blocks: list[ExtractedBlock],
        document_id: str,
    ) -> DocumentStructureAnalysis:
        """Internal analysis logic — may raise."""
        excerpt = self._build_excerpt(blocks)
        prompt_template = PROMPT_TEMPLATES["analyze_document_structure"]
        prompt = prompt_template.format(document_excerpt=excerpt)

        response_text = self.client.generate(
            prompt,
            system=SYSTEM_PROMPT,
            use_case="analyze_document_structure",
            document_id=document_id,
        )

        return self._parse_response(response_text, document_id, len(blocks))

    def _build_excerpt(self, blocks: list[ExtractedBlock]) -> str:
        """Build a masked, truncated excerpt from the document blocks."""
        parts: list[str] = []
        total_chars = 0

        for i, block in enumerate(blocks[:_MAX_BLOCKS_FOR_EXCERPT]):
            masked = mask_text_for_llm(block.text)
            header_info = ""
            if block.col_header:
                header_info = f" [col: {block.col_header}]"

            line = f"[Block {i}, page {block.page_or_sheet}{header_info}]: {masked}"
            if total_chars + len(line) > _MAX_EXCERPT_CHARS:
                break
            parts.append(line)
            total_chars += len(line)

        return "\n".join(parts)

    def _parse_response(
        self,
        response_text: str,
        document_id: str,
        num_blocks: int,
    ) -> DocumentStructureAnalysis:
        """Parse the LLM JSON response into a DocumentStructureAnalysis."""
        # Strip markdown fences if present
        cleaned = response_text.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            # Remove first and last lines (fences)
            lines = [l for l in lines if not l.strip().startswith("```")]
            cleaned = "\n".join(lines)

        data = json.loads(cleaned)

        # Validate and extract document type
        doc_type = data.get("document_type", "unknown")
        if doc_type not in VALID_DOCUMENT_TYPES:
            doc_type = "unknown"

        doc_type_confidence = float(data.get("confidence", 0.5))
        doc_type_confidence = max(0.0, min(1.0, doc_type_confidence))

        # Parse sections
        sections: list[SectionAnnotation] = []
        for s_data in data.get("sections", []):
            s_type = s_data.get("section_type", "unknown")
            if s_type not in VALID_SECTION_TYPES:
                continue
            try:
                sections.append(SectionAnnotation(
                    section_type=s_type,
                    page_start=int(s_data.get("page_start", 0)),
                    page_end=int(s_data.get("page_end", 0)),
                    block_indices=tuple(int(x) for x in s_data.get("block_indices", [])),
                    confidence=float(s_data.get("confidence", 0.5)),
                    detected_by="llm",
                ))
            except (ValueError, TypeError):
                continue

        # Parse entity roles
        entity_roles: list[EntityRoleAnnotation] = []
        for er_data in data.get("entity_roles", []):
            role = er_data.get("entity_role", "unknown")
            if role not in VALID_ENTITY_ROLES:
                continue
            try:
                bi = int(er_data.get("block_index", 0))
                if 0 <= bi < num_blocks:
                    entity_roles.append(EntityRoleAnnotation(
                        block_index=bi,
                        entity_role=role,
                        confidence=float(er_data.get("confidence", 0.5)),
                        section_type=er_data.get("section_type"),
                    ))
            except (ValueError, TypeError):
                continue

        return DocumentStructureAnalysis(
            document_id=document_id,
            document_type=doc_type,
            document_type_confidence=doc_type_confidence,
            detected_by="llm",
            sections=sections,
            entity_roles=entity_roles,
        )


def merge_analyses(
    heuristic: DocumentStructureAnalysis,
    llm: DocumentStructureAnalysis | None,
) -> DocumentStructureAnalysis:
    """Merge heuristic and LLM analyses.  Heuristic wins on conflict.

    - Document type: heuristic wins unless heuristic is "unknown"
    - Sections: heuristic sections kept; LLM sections added if they
      cover block indices not already claimed by heuristic sections
    - Entity roles: heuristic roles kept; LLM roles added only for
      blocks where heuristic assigned "unknown"
    """
    if llm is None:
        return heuristic

    # Document type: heuristic wins unless unknown
    if heuristic.document_type != "unknown":
        doc_type = heuristic.document_type
        doc_type_confidence = heuristic.document_type_confidence
    else:
        doc_type = llm.document_type
        doc_type_confidence = llm.document_type_confidence

    # Sections: keep heuristic, add non-overlapping LLM sections
    heuristic_block_indices: set[int] = set()
    for s in heuristic.sections:
        heuristic_block_indices.update(s.block_indices)

    merged_sections = list(heuristic.sections)
    for llm_section in llm.sections:
        # Only add if no overlap with heuristic sections
        if not set(llm_section.block_indices) & heuristic_block_indices:
            merged_sections.append(llm_section)
            heuristic_block_indices.update(llm_section.block_indices)

    # Entity roles: heuristic wins; LLM fills in unknowns
    heuristic_role_map: dict[int, EntityRoleAnnotation] = {
        er.block_index: er for er in heuristic.entity_roles
    }
    llm_role_map: dict[int, EntityRoleAnnotation] = {
        er.block_index: er for er in llm.entity_roles
    }

    merged_roles: list[EntityRoleAnnotation] = []
    all_indices = set(heuristic_role_map.keys()) | set(llm_role_map.keys())
    for idx in sorted(all_indices):
        h_role = heuristic_role_map.get(idx)
        l_role = llm_role_map.get(idx)

        if h_role is not None and h_role.entity_role != "unknown":
            merged_roles.append(h_role)
        elif l_role is not None:
            merged_roles.append(l_role)
        elif h_role is not None:
            merged_roles.append(h_role)

    return DocumentStructureAnalysis(
        document_id=heuristic.document_id,
        document_type=doc_type,
        document_type_confidence=doc_type_confidence,
        detected_by="heuristic+llm",
        sections=merged_sections,
        entity_roles=merged_roles,
    )
