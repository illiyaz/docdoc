"""LLM Document Understanding: produce a DocumentSchema from document text.

Phase 14b — sends document text (masked if pii_masking_enabled) to the
local Ollama LLM, which returns a structured JSON describing what the document
is, what fields mean, and what is real PII vs. reference numbers.

Step 17 — multi-page reading: sends N pages (configurable per protocol) to
detect repeating templates where one individual's PII spans multiple pages.

The resulting ``DocumentSchema`` is used by ``SchemaFilter`` to post-process
Presidio detections.  This is a single LLM call per document — efficient even
for large breach datasets.

Gated behind ``llm_assist_enabled``.  All calls are audit-logged.
Air-gap safe: local Ollama only, no external calls.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict

from sqlalchemy.orm import Session

from app.core.constants import DEFAULT_LLM_PAGES_TO_READ, PROTOCOL_LLM_CONFIG
from app.llm.client import OllamaClient, LLMDisabledError
from app.llm.prompts import PROMPT_TEMPLATES, SYSTEM_PROMPT
from app.readers.base import ExtractedBlock
from app.structure.document_schema import (
    DateContext,
    DocumentSchema,
    DocumentTemplate,
    FieldContext,
    PageRole,
    PersonContext,
    TableColumn,
    TableSchema,
)
from app.structure.masking import mask_text_for_llm

logger = logging.getLogger(__name__)

_MAX_PAGE_TEXT_CHARS = 4000
_MAX_MULTI_PAGE_CHARS_BASE = 12000  # base budget for multi-page
_MAX_MULTI_PAGE_CHARS_PER_PAGE = 3000  # additional budget per extra page


class LLMDocumentUnderstanding:
    """Produces a DocumentSchema by sending document text to the LLM.

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
        total_pages: int = 0,
        protocol_name: str = "",
        protocol_config: dict | None = None,
    ) -> DocumentSchema | None:
        """Run LLM document understanding on document blocks.

        When multiple pages are available and the protocol requests multi-page
        reading, uses UNDERSTAND_MULTI_PAGE_DOCUMENT prompt to detect
        repeating templates.

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
                total_pages=total_pages,
                protocol_name=protocol_name,
                protocol_config=protocol_config,
            )
        except LLMDisabledError:
            logger.debug("LLM assist is disabled; skipping document understanding")
            return None
        except Exception:
            logger.exception("LLM document understanding failed")
            return None

    def _resolve_pages_to_read(
        self,
        protocol_name: str,
        protocol_config: dict | None,
        total_pages: int = 0,
    ) -> int:
        """Determine how many pages the LLM should read.

        Priority: protocol_config override → PROTOCOL_LLM_CONFIG default → 3.

        For large documents (>20 pages), auto-scales to ensure the LLM sees
        at least 2 template instances.  E.g. a 450-page doc with default 3
        pages → reads 6 pages so the LLM can detect a 3-page repeating template.
        """
        # Start with protocol default
        base_key = protocol_name.lower().replace("-", "_").replace(" ", "_")
        if base_key in PROTOCOL_LLM_CONFIG:
            base_pages = int(PROTOCOL_LLM_CONFIG[base_key]["llm_pages_to_read"])
        else:
            base_pages = DEFAULT_LLM_PAGES_TO_READ

        # Override with explicit config if valid
        if protocol_config and "llm_pages_to_read" in protocol_config:
            try:
                base_pages = max(1, int(protocol_config["llm_pages_to_read"]))
            except (TypeError, ValueError):
                pass  # keep protocol default

        # Auto-scale for large documents: read 2x pages so the LLM can see
        # at least 2 complete template instances (e.g. 3-page template → 6 pages)
        if total_pages > 20 and base_pages < 6:
            base_pages = max(base_pages, 6)
        if total_pages > 100 and base_pages < 9:
            base_pages = max(base_pages, 9)

        return min(base_pages, 15)  # hard cap

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
        total_pages: int,
        protocol_name: str,
        protocol_config: dict | None,
    ) -> DocumentSchema:
        """Internal logic — may raise."""
        pages_to_read = self._resolve_pages_to_read(protocol_name, protocol_config, total_pages)
        use_multi_page = pages_to_read > 1 and total_pages > 1

        if use_multi_page:
            pages_text = self._build_multi_page_text(blocks, onset_page, pages_to_read)
            prompt_template = PROMPT_TEMPLATES["understand_multi_page_document"]
            prompt = prompt_template.format(
                file_name=file_name,
                file_type=file_type,
                total_pages=total_pages,
                protocol_name=protocol_name or "general",
                pages_text=pages_text,
            )
            use_case = "understand_multi_page_document"
        else:
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
            use_case = "understand_document"

        response_text = self.client.generate(
            prompt,
            system=SYSTEM_PROMPT,
            use_case=use_case,
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

    def _build_multi_page_text(
        self,
        blocks: list[ExtractedBlock],
        onset_page: int | str,
        pages_to_read: int,
    ) -> str:
        """Build masked multi-page text from blocks starting at onset_page.

        Groups blocks by page, includes up to ``pages_to_read`` pages starting
        from the onset page, with per-page headers.
        """
        # Scale char budget based on pages_to_read
        char_budget = _MAX_MULTI_PAGE_CHARS_BASE + max(0, pages_to_read - 3) * _MAX_MULTI_PAGE_CHARS_PER_PAGE

        # Collect distinct pages in order
        page_order: list[int | str] = []
        page_blocks: dict[int | str, list[ExtractedBlock]] = defaultdict(list)
        seen: set[int | str] = set()
        for b in blocks:
            if b.page_or_sheet not in seen:
                seen.add(b.page_or_sheet)
                page_order.append(b.page_or_sheet)
            page_blocks[b.page_or_sheet].append(b)

        # Find onset page index
        try:
            start_idx = page_order.index(onset_page)
        except ValueError:
            start_idx = 0

        target_pages = page_order[start_idx : start_idx + pages_to_read]

        parts: list[str] = []
        total_chars = 0
        for page in target_pages:
            header = f"--- PAGE {page} ---"
            parts.append(header)
            total_chars += len(header) + 1

            for block in page_blocks[page]:
                masked = mask_text_for_llm(block.text)
                if total_chars + len(masked) + 1 > char_budget:
                    remaining = char_budget - total_chars
                    if remaining > 10:
                        parts.append(masked[:remaining])
                    total_chars = char_budget
                    break
                parts.append(masked)
                total_chars += len(masked) + 1

            if total_chars >= char_budget:
                break

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

        # Parse template (Step 17)
        template = DocumentSchema._parse_template(data.get("template"))

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
            template=template,
        )
