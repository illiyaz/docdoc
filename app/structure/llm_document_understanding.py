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

import re

from app.core.constants import DEFAULT_LLM_PAGES_TO_READ, PROTOCOL_LLM_CONFIG
from app.llm.client import OllamaClient, LLMDisabledError
from app.llm.prompts import PROMPT_TEMPLATES, SYSTEM_PROMPT
from app.readers.base import ExtractedBlock
from app.structure.document_schema import (
    DateContext,
    DocumentSchema,
    DocumentTemplate,
    FieldContext,
    FieldMapping,
    PageRole,
    PersonContext,
    TableColumn,
    TableSchema,
)
from app.structure.masking import mask_text_for_llm

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defensive parsing helpers
# ---------------------------------------------------------------------------


def _safe_parse_list(raw, parser_func, fallback_func=None):
    """Safely parse a list that may contain dicts, strings, or junk.

    *parser_func* handles dict items.  *fallback_func* (optional) handles
    string items.  Any item that raises is silently skipped.
    """
    if not isinstance(raw, list):
        return []
    results = []
    for item in raw:
        try:
            if isinstance(item, dict):
                results.append(parser_func(item))
            elif isinstance(item, str) and fallback_func is not None:
                results.append(fallback_func(item))
            # else: skip non-dict non-string items
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return results


def _parse_table(t: dict) -> TableSchema:
    """Parse a single table dict into a TableSchema."""
    raw_cols = t.get("columns", [])
    columns: list[TableColumn] = []
    if isinstance(raw_cols, list):
        for c in raw_cols:
            if isinstance(c, dict):
                try:
                    columns.append(TableColumn(
                        header=str(c.get("header", "")),
                        semantic_type=str(c.get("semantic_type", "")),
                        contains_pii=bool(c.get("contains_pii", False)),
                        pii_type=c.get("pii_type"),
                    ))
                except (TypeError, ValueError):
                    continue
            elif isinstance(c, str):
                columns.append(TableColumn(
                    header=c, semantic_type="unknown",
                    contains_pii=False, pii_type=None,
                ))
    return TableSchema(
        columns=columns,
        row_count_estimate=int(t.get("row_count_estimate", 0)),
        table_context=str(t.get("table_context", "")),
        table_location=t.get("table_location"),
        has_pii_columns=bool(t.get("has_pii_columns", False)),
    )


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

    def __init__(
        self,
        db_session: Session | None = None,
        llm_client: OllamaClient | None = None,
    ) -> None:
        if llm_client is not None:
            self.client = llm_client
        else:
            from app.core.settings import get_settings as _get_settings
            settings = _get_settings()
            understanding_model = settings.ollama_understanding_model or settings.ollama_model
            self.client = OllamaClient(db_session=db_session, model=understanding_model)

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

    # Renderable image formats for vision fallback
    _RENDERABLE_TYPES = frozenset({
        "pdf", "heic", "heif", "jpg", "jpeg", "png", "tiff", "tif", "bmp",
    })

    def understand_with_vision(
        self,
        doc_path: str,
        *,
        file_type: str = "",
        file_name: str = "",
        onset_page: int = 0,
        document_id: str = "",
    ) -> DocumentSchema | None:
        """Vision fallback for docs with no text blocks (scanned PDFs, images).

        Renders the onset page as an image, sends it to the vision model with
        ``UNDERSTAND_DOCUMENT_VISION`` prompt, and parses the response using
        the existing ``_parse_response()`` logic.

        Returns ``None`` on any failure.  Never raises.
        """
        ft = (file_type or "").lower().lstrip(".")
        if ft not in self._RENDERABLE_TYPES:
            return None

        try:
            import base64

            image_b64: str | None = None

            if ft == "pdf":
                from app.pdf.renderer import render_page_to_image
                image_b64 = render_page_to_image(doc_path, onset_page, dpi=200)
            elif ft in ("heic", "heif"):
                try:
                    from pillow_heif import register_heif_opener
                    register_heif_opener()
                except ImportError:
                    pass
                from PIL import Image
                import io
                img = Image.open(doc_path)
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                image_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            else:
                # jpg, png, tiff, bmp — read and encode
                from PIL import Image
                import io
                img = Image.open(doc_path)
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                image_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

            if not image_b64:
                return None

            prompt_template = PROMPT_TEMPLATES["understand_document_vision"]
            prompt = prompt_template.format(
                file_name=file_name or doc_path.split("/")[-1],
                file_type=file_type or "unknown",
            )

            response_text = self.client.generate_with_images(
                prompt=prompt,
                images=[image_b64],
                use_case="understand_document_vision",
                document_id=document_id,
            )

            return self._parse_response(response_text)

        except LLMDisabledError:
            logger.debug("LLM disabled; skipping vision document understanding")
            return None
        except Exception:
            logger.warning(
                "Vision document understanding failed for %s", doc_path, exc_info=True,
            )
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

        schema = self._parse_response(response_text)

        # Resolve masked placeholders in anchor_text back to actual values
        # so the CoordinateExtractor can match them on unmasked page text.
        if schema.layout_field_map:
            self._resolve_masked_anchors(schema, blocks, onset_page)

        return schema

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

    # -- Masked anchor resolution -------------------------------------------

    # Map of masked placeholder → regex to find the original value in raw text
    _PLACEHOLDER_PATTERNS: dict[str, re.Pattern[str]] = {
        "[PHONE]": re.compile(
            r"\b(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b"
        ),
        "[SSN]": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
        "[EMAIL]": re.compile(
            r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"
        ),
        "[CREDIT_CARD]": re.compile(
            r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b"
        ),
    }

    def _resolve_masked_anchors(
        self,
        schema: DocumentSchema,
        blocks: list[ExtractedBlock],
        onset_page: int | str,
    ) -> None:
        """Replace masked placeholders in anchor_text with actual values.

        When the LLM sees ``[PHONE]`` (a masked institutional phone number)
        and uses it as an anchor, the CoordinateExtractor needs the REAL
        phone number to find the anchor on unmasked PDF pages.  This method
        scans the onset page's raw text for the first match of each masked
        pattern and replaces the placeholder in-place.
        """
        if not schema.layout_field_map:
            return

        # Collect unmasked text from the onset page
        onset_texts: list[str] = []
        for b in blocks:
            if b.page_or_sheet == onset_page:
                onset_texts.append(b.text)

        if not onset_texts:
            return

        onset_full_text = "\n".join(onset_texts)

        # Cache resolved values so all fields sharing the same placeholder
        # get the same real value
        resolved: dict[str, str] = {}

        for fm in schema.layout_field_map:
            anchor = fm.anchor_text.strip() if fm.anchor_text else ""
            if anchor not in self._PLACEHOLDER_PATTERNS:
                continue

            if anchor not in resolved:
                pattern = self._PLACEHOLDER_PATTERNS[anchor]
                match = pattern.search(onset_full_text)
                if match:
                    resolved[anchor] = match.group(0)
                    logger.info(
                        "Resolved masked anchor %s → %r",
                        anchor,
                        resolved[anchor],
                    )
                else:
                    logger.warning(
                        "Could not resolve masked anchor %s on onset page %s",
                        anchor,
                        onset_page,
                    )
                    continue

            if anchor in resolved:
                fm.anchor_text = resolved[anchor]

    def _parse_response(self, response_text: str) -> DocumentSchema:
        """Parse LLM JSON response into a DocumentSchema.

        Defensively handles all possible LLM response formats: strings where
        dicts are expected, missing keys, unexpected types, etc.  A partial
        schema (with whatever could be parsed) is always returned — never None.
        """
        cleaned = response_text.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            lines = [ln for ln in lines if not ln.strip().startswith("```")]
            cleaned = "\n".join(lines)

        data = json.loads(cleaned)

        # Parse field_map — each entry may be dict or bare string
        field_map = _safe_parse_list(
            data.get("field_map", []),
            parser_func=lambda f: FieldContext(
                label=str(f.get("label", "")),
                value_example=str(f.get("value_example", "")),
                semantic_type=str(f.get("semantic_type", "")),
                is_pii=bool(f.get("is_pii", False)),
                presidio_override=f.get("presidio_override"),
                suppress_types=list(f.get("suppress_types", [])),
            ),
            fallback_func=lambda s: FieldContext(
                label=s, value_example="", semantic_type="unknown",
                is_pii=False, presidio_override=None, suppress_types=[],
            ),
        )

        # Parse people — each entry may be dict or bare string
        people = _safe_parse_list(
            data.get("people", []),
            parser_func=lambda p: PersonContext(
                name=str(p.get("name", "")),
                role=str(p.get("role", "unknown")),
                context=str(p.get("context", "")),
                is_pii_subject=bool(p.get("is_pii_subject", False)),
            ),
            fallback_func=lambda s: PersonContext(
                name=s, role="unknown", context="", is_pii_subject=False,
            ),
        )

        # Parse date_contexts — each entry may be dict or bare string
        date_contexts = _safe_parse_list(
            data.get("date_contexts", []),
            parser_func=lambda d: DateContext(
                value=str(d.get("value", "")),
                semantic_type=str(d.get("semantic_type", "")),
                is_pii=bool(d.get("is_pii", False)),
            ),
            fallback_func=lambda s: DateContext(
                value=s, semantic_type="unknown", is_pii=False,
            ),
        )

        # Parse tables — each entry may be dict or bare string (skip strings)
        tables = _safe_parse_list(
            data.get("tables", []),
            parser_func=lambda t: _parse_table(t),
        )

        # Parse organizations — may be list of strings, dicts, or junk
        raw_orgs = data.get("organizations", [])
        organizations: list[str] = []
        if isinstance(raw_orgs, list):
            for o in raw_orgs:
                if isinstance(o, str):
                    organizations.append(o)
                elif isinstance(o, dict):
                    organizations.append(str(o.get("name", o.get("organization", str(o)))))
                # else: skip

        # Parse suppression_hints — may be list of strings or dicts
        raw_hints = data.get("suppression_hints", [])
        suppression_hints: list[str] = []
        if isinstance(raw_hints, list):
            for h in raw_hints:
                if isinstance(h, str):
                    suppression_hints.append(h)
                elif isinstance(h, dict):
                    suppression_hints.append(str(h.get("hint", h.get("type", str(h)))))

        confidence = float(data.get("schema_confidence", 0.5))
        confidence = max(0.0, min(1.0, confidence))

        # Parse template separately — even if other fields had issues
        template = DocumentSchema._parse_template(data.get("template"))

        # Parse tabular detection
        is_tabular = bool(data.get("is_tabular", False))
        _rpp_raw = data.get("records_per_page_estimate") or 1
        records_per_page_estimate = max(1, int(_rpp_raw))

        # Parse layout assessment (Step 21)
        raw_layout_type = str(data.get("layout_type", "variable")).lower().strip()
        if raw_layout_type not in ("fixed", "template_with_drift", "variable"):
            raw_layout_type = "variable"
        layout_confidence = 0.0
        try:
            layout_confidence = max(0.0, min(1.0, float(data.get("layout_confidence", 0.0))))
        except (TypeError, ValueError):
            pass
        layout_field_map = DocumentSchema._parse_layout_field_map(
            data.get("layout_field_map")
        )
        # Safety: if layout_type is fixed/template_with_drift but no field map, downgrade
        if raw_layout_type != "variable" and not layout_field_map:
            raw_layout_type = "variable"
            layout_confidence = 0.0

        try:
            return DocumentSchema(
                document_type=str(data.get("document_type", "unknown")),
                document_subtype=data.get("document_subtype"),
                issuing_entity=data.get("issuing_entity"),
                field_map=field_map,
                people=people,
                organizations=organizations,
                date_contexts=date_contexts,
                tables=tables,
                suppression_hints=suppression_hints,
                extraction_notes=str(data.get("extraction_notes", "")),
                schema_confidence=confidence,
                detected_by="llm",
                template=template,
                is_tabular=is_tabular,
                records_per_page_estimate=records_per_page_estimate,
                layout_type=raw_layout_type,
                layout_field_map=layout_field_map,
                layout_confidence=layout_confidence,
            )
        except Exception as e:
            logger.warning("Partial schema parse failure: %s", e)
            return DocumentSchema(
                document_type=str(data.get("document_type", "unknown")),
                document_subtype=None,
                issuing_entity=data.get("issuing_entity"),
                field_map=[],
                people=[],
                organizations=organizations,
                date_contexts=[],
                tables=[],
                suppression_hints=suppression_hints,
                extraction_notes=str(data.get("extraction_notes", "")),
                schema_confidence=0.5,
                detected_by="llm",
                template=template,
                is_tabular=is_tabular,
                records_per_page_estimate=records_per_page_estimate,
            )
