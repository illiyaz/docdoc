"""Single-file diagnostic route — upload a file, get PII extraction preview.

POST /diagnostic/file accepts multipart/form-data with a file and protocol_id.
The uploaded file is saved to a temp file, run through the reader registry and
Presidio engine, then deleted.  No data is persisted.

Safety: raw PII values are never returned — only masked snippets with the
detected span replaced by [REDACTED].
"""
from __future__ import annotations

import os
import re
import tempfile
from collections import Counter
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.api.deps import get_protocol_registry
from app.protocols.registry import ProtocolRegistry
from app.readers.registry import get_reader

router = APIRouter(prefix="/diagnostic", tags=["diagnostic"])


def _create_presidio_engine():
    """Factory for PresidioEngine — lazy import, easily mockable in tests."""
    from app.pii.presidio_engine import PresidioEngine
    return PresidioEngine()

# Extensions supported by the reader registry
_SUPPORTED_EXTENSIONS = frozenset({
    "pdf", "docx", "xlsx", "xls", "csv", "html", "htm", "xml",
    "eml", "msg", "parquet", "avro",
})


def _mask_snippet(text: str, start: int, end: int, context: int = 30) -> str:
    """Return a context window around [start:end] with the PII replaced by [REDACTED].

    Any secondary PII (SSN, email, phone, credit-card) in the surrounding
    context is also redacted so the middleware PII filter never fires.
    When PII masking is disabled, the raw context is returned as-is.
    """
    from app.core.settings import get_settings

    snippet_start = max(0, start - context)
    snippet_end = min(len(text), end + context)

    if not get_settings().pii_masking_enabled:
        return text[snippet_start:snippet_end]

    from app.core.logging import PII_PATTERNS

    before = text[snippet_start:start]
    after = text[end:snippet_end]
    snippet = f"{before}[REDACTED]{after}"
    # Scrub any other PII that leaked into the context window
    for pat in PII_PATTERNS:
        snippet = pat.sub("[REDACTED]", snippet)
    return snippet


def _mask_value(text: str, start: int, end: int) -> str:
    """Return the matched value with most characters replaced by asterisks.

    When PII masking is disabled, the raw value is returned.
    """
    raw = text[start:end]

    from app.core.settings import get_settings
    if not get_settings().pii_masking_enabled:
        return raw

    if len(raw) <= 4:
        return "***"
    return raw[:1] + "*" * (len(raw) - 2) + raw[-1:]


def _classify_page_type(block_file_type: str, page: int | str) -> str:
    """Classify page type from block metadata."""
    if block_file_type in ("csv", "xlsx", "xls", "parquet", "avro"):
        return "sheet"
    return "digital"


@router.post("/file", summary="Run diagnostic extraction on a single file")
async def diagnostic_file(
    file: UploadFile = File(...),
    protocol_id: str = Form(...),
    registry: ProtocolRegistry = Depends(get_protocol_registry),
):
    # Validate protocol
    try:
        registry.get(protocol_id)
    except KeyError:
        raise HTTPException(status_code=400, detail=f"Protocol not found: {protocol_id!r}")

    # Validate file type
    filename = file.filename or "unknown"
    ext = Path(filename).suffix.lstrip(".").lower()
    if not ext or ext not in _SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {ext!r}. Supported: {sorted(_SUPPORTED_EXTENSIONS)}",
        )

    # Save to temp file
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}")
    try:
        content = await file.read()
        tmp.write(content)
        tmp.close()

        # Read blocks
        try:
            reader = get_reader(tmp.name)
            blocks = reader.read()
        except (ImportError, ModuleNotFoundError) as exc:
            raise HTTPException(
                status_code=503,
                detail=f"Reader dependency not installed for .{ext} files: {exc}",
            )

        # Run PII detection
        try:
            engine = _create_presidio_engine()
            detections = engine.analyze(blocks)
        except (ImportError, ModuleNotFoundError) as exc:
            raise HTTPException(
                status_code=503,
                detail=f"PII engine dependency not installed: {exc}",
            )
    finally:
        os.unlink(tmp.name)

    # Build page-level results
    pages_seen: dict[int | str, dict] = {}
    for block in blocks:
        page_key = block.page_or_sheet
        if page_key not in pages_seen:
            pages_seen[page_key] = {
                "page_number": page_key if isinstance(page_key, int) else 0,
                "page_type": _classify_page_type(block.file_type, page_key),
                "blocks_extracted": 0,
                "skipped_by_onset": False,
                "ocr_used": False,
                "pii_hits": [],
            }
        pages_seen[page_key]["blocks_extracted"] += 1

    # Attach detections to pages
    entity_counter: Counter[str] = Counter()
    layer_counter: Counter[str] = Counter()
    low_confidence = 0

    for det in detections:
        page_key = det.block.page_or_sheet
        if page_key not in pages_seen:
            pages_seen[page_key] = {
                "page_number": page_key if isinstance(page_key, int) else 0,
                "page_type": _classify_page_type(det.block.file_type, page_key),
                "blocks_extracted": 0,
                "skipped_by_onset": False,
                "ocr_used": False,
                "pii_hits": [],
            }

        layer = det.extraction_layer
        hit = {
            "entity_type": det.entity_type,
            "masked_value": _mask_value(det.block.text, det.start, det.end),
            "confidence": round(det.score, 4),
            "extraction_layer": layer,
            "pattern_used": det.pattern_used or "",
            "context_snippet": _mask_snippet(det.block.text, det.start, det.end),
        }
        pages_seen[page_key]["pii_hits"].append(hit)

        entity_counter[det.entity_type] += 1
        layer_counter[layer] += 1
        if det.score < 0.75:
            low_confidence += 1

    # Sort pages by page number
    pages_list = sorted(pages_seen.values(), key=lambda p: p["page_number"])

    total_pii = sum(entity_counter.values())

    return {
        "file_name": filename,
        "file_type": ext,
        "total_pages": len(pages_list),
        "onset_page": None,
        "pages": pages_list,
        "summary": {
            "total_pii_hits": total_pii,
            "by_entity_type": dict(entity_counter),
            "layer_distribution": {
                "layer_1": layer_counter.get("layer_1_pattern", 0),
                "layer_2": layer_counter.get("layer_2_context", 0),
                "layer_3": layer_counter.get("layer_3_positional", 0),
            },
            "low_confidence_hits": low_confidence,
            "pages_skipped_by_onset": sum(
                1 for p in pages_list if p["skipped_by_onset"]
            ),
            "ocr_pages": sum(1 for p in pages_list if p["ocr_used"]),
        },
    }
