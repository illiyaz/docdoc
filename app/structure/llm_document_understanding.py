"""LLM Document Understanding: produce a DocumentSchema from onset page text.

Phase 14b — sends the onset page text (masked if pii_masking_enabled) to the
local Ollama LLM, which returns a structured JSON describing what the document
is, what fields mean, and what is real PII vs. reference numbers.

The resulting ``DocumentSchema`` is used by ``SchemaFilter`` to post-process
Presidio detections.  This is a single LLM call per document — efficient even
for large breach datasets.

Gated behind ``llm_assist_enabled``.  All calls are audit-logged.
Air-gap safe: local Ollama only, no external calls.
"""
from __future__ import annotations

import json
import logging

from sqlalchemy.orm import Session

from app.llm.client import OllamaClient, LLMDisabledError
from app.llm.prompts import PROMPT_TEMPLATES, SYSTEM_PROMPT
from app.readers.base import ExtractedBlock
from app.structure.document_schema import (
    DateContext,
    DocumentSchema,
    FieldContext,
    PersonContext,
    TableColumn,
    TableSchema,
)
from app.structure.masking import mask_text_for_llm

logger = logging.getLogger(__name__)

_MAX_PAGE_TEXT_CHARS = 4000


class LLMDocumentUnderstanding:
    """Produces a DocumentSchema by sending onset page text to the LLM.

    Parameters
    ----------
    db_session:
        SQLAlchemy session for audit logging.  When ``None``, LLM calls
        are not persisted (useful for testing).
    """

    def __init__(self, db_session: Session | None = None) -> None:
        self.client = OllamaClient(db_session=db_session)

    def understand(
        self,
        blocks: list[ExtractedBlock],
        *,
        heuristic_doc_type: str = "unknown",
        file_name: str = "",
        file_type: str = "",
        structure_class: str = "",
        onset_page: int | str = 0,
        document_id: str = "",
    ) -> DocumentSchema | None:
        """Run LLM document understanding on onset-page blocks.

        Returns ``None`` if LLM is disabled, the call fails, or no blocks
        are provided.  Never raises — failures are logged and swallowed.
        """
        if not blocks:
            return None
        try:
            return self._do_understand(
                blocks,
                heuristic_doc_type=heuristic_doc_type,
                file_name=file_name,
                file_type=file_type,
                structure_class=structure_class,
                onset_page=onset_page,
                document_id=document_id,
            )
        except LLMDisabledError:
            logger.debug("LLM assist is disabled; skipping document understanding")
            return None
        except Exception:
            logger.exception("LLM document understanding failed")
            return None

    def _do_understand(
        self,
        blocks: list[ExtractedBlock],
        *,
        heuristic_doc_type: str,
        file_name: str,
        file_type: str,
        structure_class: str,
        onset_page: int | str,
        document_id: str,
    ) -> DocumentSchema:
        """Internal logic — may raise."""
        page_text = self._build_page_text(blocks)

        prompt_template = PROMPT_TEMPLATES["understand_document"]
        prompt = prompt_template.format(
            file_name=file_name,
            file_type=file_type,
            structure_class=structure_class,
            heuristic_doc_type=heuristic_doc_type,
            onset_page=onset_page,
            page_text=page_text,
        )

        response_text = self.client.generate(
            prompt,
            system=SYSTEM_PROMPT,
            use_case="understand_document",
            document_id=document_id,
        )

        return self._parse_response(response_text)

    def _build_page_text(self, blocks: list[ExtractedBlock]) -> str:
        """Build masked page text from blocks, truncated to _MAX_PAGE_TEXT_CHARS."""
        parts: list[str] = []
        total_chars = 0

        for block in blocks:
            masked = mask_text_for_llm(block.text)
            if total_chars + len(masked) + 1 > _MAX_PAGE_TEXT_CHARS:
                remaining = _MAX_PAGE_TEXT_CHARS - total_chars
                if remaining > 10:
                    parts.append(masked[:remaining])
                break
            parts.append(masked)
            total_chars += len(masked) + 1  # +1 for newline

        return "\n".join(parts)

    def _parse_response(self, response_text: str) -> DocumentSchema:
        """Parse LLM JSON response into a DocumentSchema."""
        cleaned = response_text.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            lines = [ln for ln in lines if not ln.strip().startswith("```")]
            cleaned = "\n".join(lines)

        data = json.loads(cleaned)

        # Parse field_map
        field_map: list[FieldContext] = []
        for f in data.get("field_map", []):
            try:
                field_map.append(FieldContext(
                    label=str(f.get("label", "")),
                    value_example=str(f.get("value_example", "")),
                    semantic_type=str(f.get("semantic_type", "")),
                    is_pii=bool(f.get("is_pii", False)),
                    presidio_override=f.get("presidio_override"),
                    suppress_types=list(f.get("suppress_types", [])),
                ))
            except (TypeError, ValueError):
                continue

        # Parse people
        people: list[PersonContext] = []
        for p in data.get("people", []):
            try:
                people.append(PersonContext(
                    name=str(p.get("name", "")),
                    role=str(p.get("role", "unknown")),
                    context=str(p.get("context", "")),
                    is_pii_subject=bool(p.get("is_pii_subject", False)),
                ))
            except (TypeError, ValueError):
                continue

        # Parse date_contexts
        date_contexts: list[DateContext] = []
        for d in data.get("date_contexts", []):
            try:
                date_contexts.append(DateContext(
                    value=str(d.get("value", "")),
                    semantic_type=str(d.get("semantic_type", "")),
                    is_pii=bool(d.get("is_pii", False)),
                ))
            except (TypeError, ValueError):
                continue

        # Parse tables
        tables: list[TableSchema] = []
        for t in data.get("tables", []):
            try:
                columns: list[TableColumn] = []
                for c in t.get("columns", []):
                    columns.append(TableColumn(
                        header=str(c.get("header", "")),
                        semantic_type=str(c.get("semantic_type", "")),
                        contains_pii=bool(c.get("contains_pii", False)),
                        pii_type=c.get("pii_type"),
                    ))
                tables.append(TableSchema(
                    columns=columns,
                    row_count_estimate=int(t.get("row_count_estimate", 0)),
                    table_context=str(t.get("table_context", "")),
                    table_location=t.get("table_location"),
                    has_pii_columns=bool(t.get("has_pii_columns", False)),
                ))
            except (TypeError, ValueError):
                continue

        confidence = float(data.get("schema_confidence", 0.5))
        confidence = max(0.0, min(1.0, confidence))

        return DocumentSchema(
            document_type=str(data.get("document_type", "unknown")),
            document_subtype=data.get("document_subtype"),
            issuing_entity=data.get("issuing_entity"),
            field_map=field_map,
            people=people,
            organizations=list(data.get("organizations", [])),
            date_contexts=date_contexts,
            tables=tables,
            suppression_hints=list(data.get("suppression_hints", [])),
            extraction_notes=str(data.get("extraction_notes", "")),
            schema_confidence=confidence,
            detected_by="llm",
        )
