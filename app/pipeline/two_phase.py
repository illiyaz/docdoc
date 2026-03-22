"""Two-phase pipeline generators: analyze and extract.

Phase 1 (analyze): discovery, cataloging, structure analysis, sample
extraction on the onset page, and auto-approve decision.  Yields SSE
events at each stage.

Phase 2 (extract): full PII detection, entity resolution, deduplication,
and notification list building for all approved documents.

Both generators follow the exact same SSE pattern as
``app.api.routes.jobs._pipeline_generator()``.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.core.policies import StorageMode, StoragePolicyConfig
from app.core.security import SecurityService
from app.core.settings import get_settings
from app.db.models import Document, DocumentAnalysisReview, IngestionRun, NotificationSubject
from app.db.repositories import ExtractionRepository
from app.pipeline.auto_approve import should_auto_approve
from app.pipeline.record_mapper import (
    detection_to_pii_record,
    extract_with_template,
)
from app.core.constants import DEFAULT_EXTRACTION_BATCH_SIZE, PROTOCOL_LLM_CONFIG
from app.pipeline.content_onset import (
    filter_sample_blocks,
    find_content_onset_from_blocks,
    find_verified_onset,
    find_verified_onset_pdf,
)
from app.core.constants import PROTOCOL_DEFAULT_ENTITIES
from app.protocols.registry import ProtocolRegistry
from app.tasks.discovery import DiscoveryTask, FilesystemConnector

logger = logging.getLogger(__name__)


def _resolve_target_entities(
    protocol_config: dict | None,
    protocol_id: str | None,
) -> list[str] | None:
    """Resolve target entity types from protocol config or protocol defaults.

    Precedence:
    1. ``target_entity_types`` explicitly set in ``protocol_config`` → use it
    2. ``base_protocol_id`` in config → look up ``PROTOCOL_DEFAULT_ENTITIES``
    3. ``protocol_id`` (job-level) → look up ``PROTOCOL_DEFAULT_ENTITIES``
    4. None → run all recognizers (backward compatible)
    """
    if protocol_config:
        explicit = protocol_config.get("target_entity_types")
        if explicit:
            return list(explicit)
        base = protocol_config.get("base_protocol_id")
        if base and base in PROTOCOL_DEFAULT_ENTITIES:
            return list(PROTOCOL_DEFAULT_ENTITIES[base])

    if protocol_id and protocol_id in PROTOCOL_DEFAULT_ENTITIES:
        return list(PROTOCOL_DEFAULT_ENTITIES[protocol_id])

    return None


# ---------------------------------------------------------------------------
# SSE helper (same pattern as jobs.py)
# ---------------------------------------------------------------------------

def _sse(data: dict) -> str:
    """Format a dict as an SSE data line."""
    return f"data: {json.dumps(data)}\n\n"


# Mapping from LLM field names to canonical preview field names
_PREVIEW_FIELD_MAP: dict[str, str] = {
    "PERSON": "PERSON",
    "LOCATION": "LOCATION",
    "DATE_OF_BIRTH": "DATE_OF_BIRTH",
    "DATE_OF_BIRTH_DMY": "DATE_OF_BIRTH",
    "DATE_OF_BIRTH_MDY": "DATE_OF_BIRTH",
    "DATE_OF_BIRTH_ISO": "DATE_OF_BIRTH",
    "EMAIL_ADDRESS": "EMAIL",
    "EMAIL": "EMAIL",
    "PHONE_NUMBER": "PHONE",
    "PHONE_US": "PHONE",
    "PHONE_INTL": "PHONE",
    "US_SSN": "GOVERNMENT_ID",
    "NI_NUMBER": "GOVERNMENT_ID",
    "AADHAAR": "GOVERNMENT_ID",
    "US_DRIVER_LICENSE": "GOVERNMENT_ID",
    "US_PASSPORT": "GOVERNMENT_ID",
    "PAN_CARD": "GOVERNMENT_ID",
    "NHS_NUMBER": "GOVERNMENT_ID",
    "GOVERNMENT_ID": "GOVERNMENT_ID",
    "IDENTIFICATION_NUMBER": "GOVERNMENT_ID",
    "NATIONAL_INSURANCE_UK": "GOVERNMENT_ID",
}


def _parse_preview_response(
    response_text: str,
    valid_pages: list[int],
) -> dict[str, dict]:
    """Parse LLM preview response with per-field page numbers.

    Expects JSON like::

        {"PERSON": {"value": "John Smith", "page": 1}, ...}

    Also handles flat format (value only, no page) as fallback::

        {"PERSON": "John Smith", ...}

    Returns a dict of canonical field name → ``{"value": str, "page": int}``.
    """
    cleaned = response_text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = [ln for ln in lines if not ln.strip().startswith("```")]
        cleaned = "\n".join(lines).strip()

    data: dict | None = None
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        for start_char in ("{", "["):
            idx = cleaned.find(start_char)
            if idx >= 0:
                try:
                    data = json.loads(cleaned[idx:])
                    break
                except json.JSONDecodeError:
                    continue

    if data is None:
        return {}

    # Handle array response — take first element
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        return {}

    default_page = valid_pages[0] if valid_pages else 1
    fields_found: dict[str, dict] = {}

    for raw_field, raw_val in data.items():
        if raw_val is None or raw_val == "" or raw_val == "null":
            continue

        canonical = _PREVIEW_FIELD_MAP.get(raw_field, raw_field)

        # Already have a value for this canonical field — skip duplicates
        if canonical in fields_found:
            continue

        if isinstance(raw_val, dict):
            value = raw_val.get("value")
            page = raw_val.get("page")
            if value is None or value == "" or value == "null":
                continue
            value_str = str(value).strip()
            if not value_str:
                continue
            # Validate page number
            if isinstance(page, (int, float)) and int(page) in valid_pages:
                page_num = int(page)
            else:
                page_num = default_page
            fields_found[canonical] = {"value": value_str, "page": page_num}
        else:
            # Flat format fallback
            value_str = str(raw_val).strip()
            if not value_str:
                continue
            fields_found[canonical] = {"value": value_str, "page": default_page}

    return fields_found


# ---------------------------------------------------------------------------
# Phase 1: Analyze generator
# ---------------------------------------------------------------------------

def analyze_generator(
    body,  # CreateJobBody from app.api.routes.jobs
    db: Session | None,
    registry: ProtocolRegistry,
) -> Generator[str, None, None]:
    """Run the analysis phase of the two-phase pipeline, yielding SSE events.

    Stages: discovery -> cataloging -> structure_analysis ->
    verified_onset -> sample_extraction -> entity_analysis ->
    auto_approve -> complete.

    If *db* is None, a session is created lazily from the default
    session factory (same pattern as ``_pipeline_generator``).
    """
    job_id = body.job_id or str(uuid4())
    job_uuid = UUID(job_id) if body.job_id else uuid4()
    settings = get_settings()
    owns_db = False
    run: IngestionRun | None = None

    # --- Acquire DB session ---
    try:
        if db is None:
            from app.api.deps import _get_session_factory
            db = _get_session_factory()()
            owns_db = True
    except Exception as exc:
        yield _sse({"stage": "error", "message": f"Database connection failed: {type(exc).__name__}"})
        return

    # --- Resolve source directory ---
    if body.upload_id:
        upload_path = Path(settings.upload_dir) / body.upload_id
        if not upload_path.is_dir():
            yield _sse({"stage": "error", "message": f"Upload {body.upload_id!r} not found or expired"})
            return
        source_directory = str(upload_path)
    else:
        source_directory = body.source_directory  # type: ignore[assignment]

    # --- Load protocol ---
    try:
        protocol = registry.get(body.protocol_id)
    except KeyError:
        yield _sse({"stage": "error", "message": f"Protocol not found: {body.protocol_id!r}"})
        return

    # --- Resolve project_id ---
    project_uuid = None
    if body.project_id:
        try:
            project_uuid = UUID(body.project_id)
        except (ValueError, AttributeError):
            pass

    # --- Load protocol config if specified ---
    protocol_config: dict | None = None
    if body.protocol_config_id:
        try:
            from app.db.models import ProtocolConfig
            pc_uuid = UUID(body.protocol_config_id)
            pc = db.get(ProtocolConfig, pc_uuid)
            if pc is not None:
                protocol_config = pc.config_json
        except Exception:
            pass  # best-effort; proceed without config

    # --- Create IngestionRun record ---
    run = IngestionRun(
        id=job_uuid,
        project_id=project_uuid,
        source_path=source_directory,
        pipeline_mode="two_phase",
        config_hash="",
        code_version="0.1.0",
        initiated_by="api",
        status="running",
        started_at=datetime.now(timezone.utc),
        config_snapshot={
            "protocol_id": body.protocol_id,
            "protocol_config_id": body.protocol_config_id,
        },
    )
    db.add(run)
    db.commit()

    try:
        # --- Stage 1: Discovery ---
        yield _sse({"stage": "discovery", "status": "running", "message": "Discovering documents..."})
        connector = FilesystemConnector(source_directory)
        discovery = DiscoveryTask()
        docs = discovery.run([connector])
        yield _sse({
            "stage": "discovery", "status": "complete",
            "message": f"Found {len(docs)} document(s)",
            "detail": {"document_count": len(docs)},
        })

        # --- Stage 1.5: Create Document records ---
        doc_records: list[Document] = []
        for doc_info in docs:
            src = Path(doc_info["source_path"])
            try:
                sha = hashlib.sha256(src.read_bytes()).hexdigest()
            except Exception:
                sha = hashlib.sha256(str(src).encode()).hexdigest()
            doc = Document(
                ingestion_run_id=run.id,
                source_path=doc_info["source_path"],
                file_name=doc_info.get("file_name", src.name),
                file_type=doc_info.get("file_type", src.suffix.lstrip(".") or "unknown"),
                size_bytes=doc_info.get("size_bytes"),
                sha256=sha,
            )
            db.add(doc)
            doc_records.append(doc)
        db.commit()

        # --- Stage 2: Cataloging ---
        yield _sse({"stage": "cataloging", "status": "running", "message": "Classifying documents..."})
        try:
            from app.tasks.cataloger import CatalogerTask
            cataloger = CatalogerTask(db)
            cataloger.run(doc_records)
        except Exception:
            pass  # cataloger is best-effort; don't fail the pipeline
        yield _sse({
            "stage": "cataloging", "status": "complete",
            "message": f"Cataloged {len(doc_records)} document(s)",
        })

        # --- Stage 3: Structure Analysis ---
        yield _sse({
            "stage": "structure_analysis", "status": "running",
            "message": "Analyzing document structure...",
            "detail": {"total": len(doc_records), "current": 0},
        })

        from app.readers.registry import get_reader
        from app.tasks.structure_analysis import StructureAnalysisTask

        structure_task = StructureAnalysisTask()
        doc_blocks_cache: dict[UUID, list] = {}  # cache blocks for sample_extraction

        for i, doc in enumerate(doc_records, 1):
            yield _sse({
                "stage": "structure_analysis", "status": "running",
                "message": f"Analyzing structure of document {i}/{len(doc_records)}...",
                "detail": {"total": len(doc_records), "current": i},
            })
            try:
                reader = get_reader(doc.source_path)
                blocks = reader.read()

                # Scanned PDF fallback: run OCR when no text blocks found
                if not blocks and (doc.file_type or "").lower() in ("pdf", ".pdf", "application/pdf") and doc.source_path:
                    try:
                        from app.readers.ocr import ocr_pdf_to_blocks
                        blocks = ocr_pdf_to_blocks(doc.source_path)
                        if blocks:
                            logger.info(
                                "OCR produced %d blocks for scanned PDF %s (analyze phase)",
                                len(blocks), doc.file_name,
                            )
                    except ImportError:
                        logger.warning("PaddleOCR not available for %s", doc.file_name)
                    except Exception:
                        logger.warning("OCR failed for %s in analyze phase", doc.file_name, exc_info=True)

                doc_blocks_cache[doc.id] = blocks

                result = structure_task.run(blocks, str(doc.id), db_session=db)
                doc.structure_analysis = result.to_dict()
            except Exception as e:
                logger.warning("Structure analysis failed for doc %s: %s", doc.file_name, type(e).__name__)

        db.commit()
        yield _sse({
            "stage": "structure_analysis", "status": "complete",
            "message": f"Analyzed structure of {len(doc_records)} document(s)",
        })

        # --- Stage 4: Verified Onset + Sample Extraction ---
        yield _sse({
            "stage": "verified_onset", "status": "running",
            "message": "Detecting PII-verified content onset...",
            "detail": {"total": len(doc_records), "current": 0},
        })

        from app.pii.presidio_engine import PresidioEngine

        engine = PresidioEngine()
        target_entities = _resolve_target_entities(protocol_config, body.protocol_id)
        doc_confidences: dict[UUID, list[float]] = {}
        doc_detections: dict[UUID, list] = {}  # cache for entity_analysis stage

        # Setup for storing sample extractions via STRICT policy
        security = SecurityService()
        extraction_repo = ExtractionRepository(db)
        strict_policy = StoragePolicyConfig(
            mode=StorageMode.STRICT,
            mask_normalized_in_strict=settings.pii_masking_enabled,
        )
        tenant_salt = settings.tenant_salt

        for i, doc in enumerate(doc_records, 1):
            yield _sse({
                "stage": "verified_onset", "status": "running",
                "message": f"Verifying onset for document {i}/{len(doc_records)}...",
                "detail": {"total": len(doc_records), "current": i},
            })

            try:
                blocks = doc_blocks_cache.get(doc.id)
                if blocks is None:
                    reader = get_reader(doc.source_path)
                    blocks = reader.read()
                    # Scanned PDF fallback (same as structure_analysis stage)
                    if not blocks and (doc.file_type or "").lower() in ("pdf", ".pdf", "application/pdf") and doc.source_path:
                        try:
                            from app.readers.ocr import ocr_pdf_to_blocks
                            blocks = ocr_pdf_to_blocks(doc.source_path)
                        except Exception:
                            pass
                    doc_blocks_cache[doc.id] = blocks

                # PII-verified onset detection
                onset_page: int | str = 0

                if (doc.file_type or "").lower() == "pdf":
                    try:
                        import fitz
                        fitz_doc = fitz.open(doc.source_path)
                        onset_page = find_verified_onset_pdf(fitz_doc, engine)
                        fitz_doc.close()
                    except Exception:
                        onset_page = find_verified_onset(blocks, doc.file_type or "pdf", engine)
                else:
                    onset_page = find_verified_onset(blocks, doc.file_type or "unknown", engine)

                # Filter to sample blocks on verified onset page
                sample_blocks = filter_sample_blocks(blocks, onset_page, doc.file_type or "unknown")

                # Run Presidio on sample blocks (filtered by protocol entity types)
                detections = engine.analyze(
                    sample_blocks, target_entity_types=target_entities,
                ) if sample_blocks else []
                confidences = [det.score for det in detections]
                doc_confidences[doc.id] = confidences
                doc_detections[doc.id] = detections

                # Store sample extractions as Extraction records (is_sample=True)
                masking_on = settings.pii_masking_enabled
                for det in detections:
                    raw_text = det.block.text[det.start:det.end] if hasattr(det, "block") else ""
                    if not raw_text:
                        continue
                    if masking_on:
                        if len(raw_text) <= 4:
                            masked = "*" * len(raw_text)
                        else:
                            masked = f"{'*' * (len(raw_text) - 4)}{raw_text[-4:]}"
                    else:
                        masked = raw_text
                    try:
                        extraction_repo.create_with_policy(
                            raw_value=raw_text,
                            normalized_value=None,
                            tenant_salt=tenant_salt,
                            security=security,
                            policy_config=strict_policy,
                            document_id=doc.id,
                            pii_type=det.entity_type,
                            sensitivity="high",
                            confidence_score=det.score,
                            evidence_page=det.block.page_or_sheet if hasattr(det, "block") else None,
                            evidence_text_start=det.start,
                            evidence_text_end=det.end,
                            is_sample=True,
                            masked_value=masked,
                        )
                    except Exception:
                        pass  # best-effort; don't fail pipeline for storage error

                # Update document with sample results
                doc.sample_onset_page = int(onset_page) if isinstance(onset_page, (int, float)) else 0
                doc.sample_extraction_count = len(detections)
                doc.analysis_phase_status = "sample_extracted"

            except Exception as e:
                logger.warning("Sample extraction failed for doc %s: %s", doc.file_name, type(e).__name__)
                doc_confidences[doc.id] = []
                doc_detections[doc.id] = []
                doc.analysis_phase_status = "sample_failed"

        db.commit()

        yield _sse({
            "stage": "verified_onset", "status": "complete",
            "message": f"Verified onset for {len(doc_records)} document(s)",
        })

        yield _sse({
            "stage": "sample_extraction", "status": "complete",
            "message": f"Sampled {len(doc_records)} document(s)",
            "detail": {
                "total_detections": sum(len(c) for c in doc_confidences.values()),
            },
        })

        # --- Stage 4.5: Document Understanding (LLM) + Schema Filter ---
        yield _sse({
            "stage": "document_understanding", "status": "running",
            "message": "Analyzing document semantics...",
            "detail": {"total": len(doc_records), "current": 0},
        })

        doc_schemas: dict[UUID, object] = {}  # UUID → DocumentSchema | None
        understanding_count = 0
        schema_filter_suppressed = 0

        if settings.llm_assist_enabled:
            try:
                from app.structure.llm_document_understanding import LLMDocumentUnderstanding
                from app.pii.schema_filter import SchemaFilter

                doc_understanding = LLMDocumentUnderstanding(db_session=db)

                for i, doc in enumerate(doc_records, 1):
                    yield _sse({
                        "stage": "document_understanding", "status": "running",
                        "message": f"Understanding document {i}/{len(doc_records)}...",
                        "detail": {"total": len(doc_records), "current": i},
                    })

                    blocks = doc_blocks_cache.get(doc.id, [])
                    onset_page = doc.sample_onset_page or 0
                    sample_blocks = filter_sample_blocks(blocks, onset_page, doc.file_type or "unknown")

                    # Get heuristic doc type from structure analysis
                    heuristic_doc_type = "unknown"
                    if doc.structure_analysis and isinstance(doc.structure_analysis, dict):
                        heuristic_doc_type = doc.structure_analysis.get("document_type", "unknown")

                    # Compute total_pages for multi-page template detection
                    all_blocks = doc_blocks_cache.get(doc.id, [])
                    total_pages = len(set(b.page_or_sheet for b in all_blocks)) if all_blocks else 0

                    schema = doc_understanding.understand(
                        sample_blocks,
                        heuristic_doc_type=heuristic_doc_type,
                        file_name=doc.file_name or "",
                        file_type=doc.file_type or "",
                        structure_class=doc.structure_class or "",
                        onset_page=onset_page,
                        document_id=str(doc.id),
                        total_pages=total_pages,
                        protocol_name=body.protocol_id,
                        protocol_config=protocol_config,
                    )

                    doc_schemas[doc.id] = schema

                    if schema is not None:
                        understanding_count += 1
                        # Persist schema to metadata_json for extraction phase
                        doc.metadata_json = doc.metadata_json or {}

                        # Don't downgrade from "fixed" to "variable" — LLM non-determinism
                        existing_schema_dict = doc.metadata_json.get("document_schema")
                        if existing_schema_dict:
                            existing_lt = existing_schema_dict.get("layout_type", "variable")
                            new_lt = getattr(schema, "layout_type", "variable")
                            if existing_lt in ("fixed", "template_with_drift") and new_lt == "variable":
                                logger.warning(
                                    "Keeping existing '%s' layout for %s (LLM said 'variable' this run)",
                                    existing_lt, doc.file_name,
                                )
                                try:
                                    from app.structure.document_schema import DocumentSchema as _DSKeep
                                    schema = _DSKeep.from_dict(existing_schema_dict)
                                    doc_schemas[doc.id] = schema
                                except Exception:
                                    pass  # fall through with new schema

                        doc.metadata_json["document_schema"] = schema.to_dict()
                        flag_modified(doc, "metadata_json")
                        # Apply SchemaFilter to this doc's detections
                        detections = doc_detections.get(doc.id, [])
                        if detections:
                            sf = SchemaFilter(schema)
                            result = sf.filter_detections(detections)
                            # Replace detections with filtered set
                            doc_detections[doc.id] = result.kept
                            # Update confidences to match filtered detections
                            doc_confidences[doc.id] = [d.score for d in result.kept]
                            schema_filter_suppressed += len(result.suppressed)
                            # Update extraction count
                            doc.sample_extraction_count = len(result.kept)

                db.commit()
            except Exception as e:
                logger.warning("Document understanding stage failed: %s", type(e).__name__)
        else:
            logger.info("LLM assist disabled; skipping document understanding")

        yield _sse({
            "stage": "document_understanding", "status": "complete",
            "message": f"Understood {understanding_count} document(s), "
                       f"suppressed {schema_filter_suppressed} false positive(s)",
            "detail": {
                "understood": understanding_count,
                "suppressed": schema_filter_suppressed,
            },
        })

        # --- Stage 4b-0: Vision-based routing for extraction path ---
        # Uses vision model to classify document structure and identify PII
        # fields. Replaces LLM layout_type classification as primary routing.

        doc_previews: dict[UUID, dict] = {}
        preview_count = 0
        vision_routed_docs: set[UUID] = set()

        if settings.llm_assist_enabled and settings.use_vision_extraction:
            try:
                from app.llm.client import OllamaClient
                from app.pipeline.vision_router import VisionRouter, VisionRoutingResult
                from app.pipeline.field_map_builder import FieldMapBuilder

                vision_client = OllamaClient(db_session=db)
                vision_model_override = None
                vision_fallback_model = None

                # Check per-protocol vision model override
                if protocol_config and isinstance(protocol_config, dict):
                    vision_model_override = protocol_config.get("vision_model")
                    vision_fallback_model = protocol_config.get("vision_fallback_model")
                if not vision_model_override:
                    base_key = body.protocol_id.lower().replace("-", "_").replace(" ", "_")
                    if base_key in PROTOCOL_LLM_CONFIG:
                        vision_model_override = PROTOCOL_LLM_CONFIG[base_key].get("vision_model")
                        if not vision_fallback_model:
                            vision_fallback_model = PROTOCOL_LLM_CONFIG[base_key].get("vision_fallback_model")

                # Final fallback to settings defaults
                if not vision_model_override:
                    vision_model_override = settings.ollama_vision_model
                if not vision_fallback_model:
                    vision_fallback_model = getattr(settings, "ollama_vision_fallback_model", None)

                if vision_client.is_vision_available(model_override=vision_model_override):
                    router = VisionRouter(
                        vision_client,
                        vision_model=vision_model_override,
                        fallback_model=vision_fallback_model,
                    )

                    from app.pipeline.template_cache import TemplateCache
                    template_cache = TemplateCache()

                    yield _sse({
                        "stage": "vision_routing", "status": "running",
                        "message": "Analyzing document structure with vision model...",
                        "detail": {"total": len(doc_records), "current": 0},
                    })

                    for doc in doc_records:
                        if not doc.source_path:
                            continue

                        # Determine if scanned
                        blocks = doc_blocks_cache.get(doc.id, [])
                        is_scanned = (
                            len(blocks) == 0
                            and (doc.file_type or "").lower() in ("pdf", ".pdf", "application/pdf")
                        )

                        # Get onset page and total pages
                        onset = doc.sample_onset_page or 0
                        total_pages = len(set(b.page_or_sheet for b in blocks)) if blocks else 0

                        # For scanned docs, get page count from PyMuPDF
                        if is_scanned and doc.source_path:
                            try:
                                import fitz
                                pdf_doc = fitz.open(doc.source_path)
                                total_pages = pdf_doc.page_count
                                pdf_doc.close()
                            except Exception:
                                pass

                        try:
                            # --- Template cache (Step 23e): skip vision on repeat layouts ---
                            cached_entry = template_cache.get(doc.source_path, onset)
                            cache_hit = False

                            if cached_entry:
                                # Reconstruct routing + field_map from cache
                                rd = cached_entry.routing_dict
                                routing = VisionRoutingResult(
                                    structure_type=rd.get("structure_type", "variable"),
                                    structure_confidence=rd.get("structure_confidence", 0.8),
                                    pii_fields=rd.get("pii_fields", []),
                                    records_per_page=rd.get("records_per_page", 1),
                                    cross_page_data=rd.get("cross_page_data", False),
                                    pages_per_instance=rd.get("pages_per_instance", 1),
                                    recommended_path=rd.get("recommended_path", "presidio"),
                                )
                                field_map = None
                                if cached_entry.field_map_dicts:
                                    from app.structure.document_schema import FieldMapping
                                    field_map = [
                                        FieldMapping(
                                            field_type=fm["field_type"],
                                            anchor_text=fm["anchor_text"],
                                            spatial_relationship=fm["spatial_relationship"],
                                            value_pattern=fm.get("value_pattern"),
                                            sample_bbox=fm.get("sample_bbox", []),
                                            line_count=fm.get("line_count", 1),
                                            skip_pattern=fm.get("skip_pattern"),
                                        )
                                        for fm in cached_entry.field_map_dicts
                                    ]
                                cache_hit = True
                                logger.info(
                                    "Template cache hit for %s — skipping vision call",
                                    doc.file_name,
                                )
                            else:
                                routing = router.analyze_document(
                                    doc.source_path, onset_page=onset,
                                    total_pages=total_pages, is_scanned=is_scanned,
                                )

                            # --- Gate 1: Validate PERSON fields from vision ---
                            # Vision may misidentify page headers as names
                            # (e.g., "January Statement" as PERSON). Validate
                            # against blocklist, fall back to text discovery.
                            if not cache_hit and routing.pii_fields:
                                try:
                                    from app.pipeline.person_discovery import (
                                        is_likely_name,
                                        discover_person_from_text,
                                    )
                                    person_fields = [
                                        f for f in routing.pii_fields
                                        if f.get("type") == "PERSON"
                                    ]
                                    valid_persons = [
                                        f for f in person_fields
                                        if is_likely_name(f.get("value", ""))
                                    ]
                                    invalid_persons = [
                                        f for f in person_fields
                                        if not is_likely_name(f.get("value", ""))
                                    ]

                                    if invalid_persons:
                                        logger.warning(
                                            "Vision PERSON validation: %d/%d rejected for %s: %s",
                                            len(invalid_persons), len(person_fields),
                                            doc.file_name,
                                            [f.get("value", "")[:30] for f in invalid_persons],
                                        )

                                    if person_fields and not valid_persons:
                                        # All PERSON fields invalid — try text discovery
                                        logger.info(
                                            "All PERSON fields invalid for %s, trying text discovery",
                                            doc.file_name,
                                        )
                                        discovered, best_page = discover_person_from_text(
                                            doc.source_path, onset,
                                        )
                                        if discovered:
                                            # Replace invalid PERSON fields with discovered ones
                                            routing.pii_fields = [
                                                f for f in routing.pii_fields
                                                if f.get("type") != "PERSON"
                                            ] + discovered
                                            onset = best_page
                                            logger.info(
                                                "Text discovery found %d PERSON on page %d for %s",
                                                len(discovered), best_page, doc.file_name,
                                            )
                                        else:
                                            # No persons found at all — downgrade path
                                            logger.warning(
                                                "No valid PERSON found for %s, downgrading to presidio",
                                                doc.file_name,
                                            )
                                            routing.recommended_path = "presidio"
                                    elif invalid_persons and valid_persons:
                                        # Keep only valid persons
                                        routing.pii_fields = [
                                            f for f in routing.pii_fields
                                            if f.get("type") != "PERSON"
                                        ] + valid_persons
                                except Exception:
                                    logger.warning(
                                        "PERSON validation failed for %s",
                                        doc.file_name, exc_info=True,
                                    )

                            # Build field map if coordinate path recommended (skip on cache hit)
                            if not cache_hit:
                                field_map = None
                            if not cache_hit and routing.recommended_path == "coordinate":
                                builder = FieldMapBuilder()
                                field_map = builder.build_field_map(
                                    routing, doc.source_path, page_num=onset,
                                )

                                # Validate field map with sample extraction
                                if field_map:
                                    try:
                                        from app.pipeline.coordinate_extractor import CoordinateExtractor
                                        test_ext = CoordinateExtractor(field_map, doc.source_path, "validation")
                                        test_records, test_failed = test_ext.extract_all_pages(page_range=[onset])

                                        if not test_records or not test_records[0].raw_name:
                                            logger.warning(
                                                "Vision field map failed validation for %s, downgrading to %s",
                                                doc.file_name,
                                                "vision_direct" if total_pages <= 5 else "presidio",
                                            )
                                            field_map = None
                                            routing.recommended_path = "vision_direct" if total_pages <= 5 else "presidio"
                                        else:
                                            logger.info(
                                                "Vision field map validated: %s → '%s' on page %d",
                                                doc.file_name, test_records[0].raw_name, onset,
                                            )
                                    except Exception:
                                        logger.warning(
                                            "Vision field map validation failed for %s",
                                            doc.file_name, exc_info=True,
                                        )
                                        field_map = None
                                        routing.recommended_path = "vision_direct" if total_pages <= 5 else "presidio"

                                # Store in template cache on successful vision routing
                                try:
                                    routing_dict = {
                                        "structure_type": routing.structure_type,
                                        "structure_confidence": routing.structure_confidence,
                                        "pii_fields": routing.pii_fields,
                                        "records_per_page": routing.records_per_page,
                                        "cross_page_data": routing.cross_page_data,
                                        "pages_per_instance": routing.pages_per_instance,
                                        "recommended_path": routing.recommended_path,
                                    }
                                    fm_dicts = None
                                    if field_map:
                                        fm_dicts = [
                                            {
                                                "field_type": fm.field_type,
                                                "anchor_text": fm.anchor_text,
                                                "spatial_relationship": fm.spatial_relationship,
                                                "value_pattern": fm.value_pattern,
                                                "sample_bbox": fm.sample_bbox,
                                                "line_count": fm.line_count,
                                                "skip_pattern": fm.skip_pattern,
                                            }
                                            for fm in field_map
                                        ]
                                    name_samples = [
                                        f.get("value", "")
                                        for f in routing.pii_fields
                                        if f.get("type") == "PERSON"
                                    ]
                                    template_cache.put(
                                        doc.source_path, onset, routing_dict, fm_dicts, name_samples,
                                    )
                                except Exception:
                                    logger.debug("Template cache store failed", exc_info=True)

                            # Persist routing result and field map to document metadata
                            doc_meta = doc.metadata_json or {}
                            doc_meta["vision_routing"] = {
                                "structure_type": routing.structure_type,
                                "recommended_path": routing.recommended_path,
                                "pii_field_count": len(routing.pii_fields),
                                "records_per_page": routing.records_per_page,
                                "cross_page_data": routing.cross_page_data,
                                "template_cache_hit": cache_hit,
                            }
                            if field_map:
                                doc_meta["vision_field_map"] = [
                                    {
                                        "field_type": fm.field_type,
                                        "anchor_text": fm.anchor_text,
                                        "spatial_relationship": fm.spatial_relationship,
                                        "value_pattern": fm.value_pattern,
                                        "sample_bbox": fm.sample_bbox,
                                        "line_count": fm.line_count,
                                        "skip_pattern": fm.skip_pattern,
                                    }
                                    for fm in field_map
                                ]
                            doc.metadata_json = doc_meta
                            flag_modified(doc, "metadata_json")

                            # Build preview for the analysis review panel
                            preview = {
                                "extraction_method": routing.recommended_path,
                                "structure_type": routing.structure_type,
                                "pii_fields": [f.get("type", "") for f in routing.pii_fields],
                                "field_map_count": len(field_map) if field_map else 0,
                                "total_instances_estimate": (
                                    total_pages if routing.records_per_page == 1
                                    else total_pages * routing.records_per_page
                                ),
                                "sample_values": {
                                    f.get("type", ""): f.get("value", "")[:50]
                                    for f in routing.pii_fields[:5]
                                },
                            }
                            doc_previews[doc.id] = preview
                            vision_routed_docs.add(doc.id)

                        except Exception:
                            logger.warning("Vision routing failed for %s", doc.file_name, exc_info=True)

                    db.commit()

                    yield _sse({
                        "stage": "vision_routing", "status": "complete",
                        "message": f"Vision-routed {len(vision_routed_docs)} document(s)",
                        "detail": {"routed": len(vision_routed_docs)},
                    })
            except ImportError:
                logger.info("Vision routing not available (missing dependencies)")
            except Exception:
                logger.warning("Vision routing stage failed", exc_info=True)

        # --- Stage 4b-0-legacy: Extraction Preview (coordinate / fixed-layout docs) ---
        # For docs NOT already routed by vision, fall back to LLM schema field map.
        fixed_layout_docs = [
            doc for doc in doc_records
            if doc.id not in vision_routed_docs
            and doc.id in doc_schemas
            and doc_schemas[doc.id] is not None
            and getattr(doc_schemas[doc.id], "layout_type", "variable") in ("fixed", "template_with_drift")
            and getattr(doc_schemas[doc.id], "layout_field_map", None)
        ]

        if fixed_layout_docs:
            yield _sse({
                "stage": "extraction_preview", "status": "running",
                "message": f"Previewing coordinate extraction for {len(fixed_layout_docs)} fixed-layout document(s)...",
                "detail": {"total": len(fixed_layout_docs), "current": 0},
            })

            for idx, doc in enumerate(fixed_layout_docs, 1):
                schema = doc_schemas[doc.id]
                field_map = schema.layout_field_map

                # Try coordinate extraction on first content page as sample
                sample_records: list = []
                if doc.source_path and (doc.file_type or "").lower() in ("pdf", ".pdf", "application/pdf"):
                    try:
                        from app.pipeline.coordinate_extractor import CoordinateExtractor

                        onset = doc.sample_onset_page or 0
                        coord_ext = CoordinateExtractor(field_map, doc.source_path, str(doc.id))
                        sample_records, _ = coord_ext.extract_all_pages(page_range=[onset])
                    except Exception:
                        logger.warning("Coordinate preview failed for %s", doc.file_name, exc_info=True)

                # Validate preview quality — reject garbage extractions
                preview_valid = True
                if sample_records and sample_records[0].raw_name:
                    if not _is_likely_name(sample_records[0].raw_name):
                        logger.warning(
                            "Coordinate preview for %s extracted bad name '%s', field map may be wrong",
                            doc.file_name, sample_records[0].raw_name,
                        )
                        preview_valid = False

                # Build preview dict
                fields_found: dict[str, dict] = {}
                if sample_records and preview_valid:
                    rec = sample_records[0]
                    onset_pg = (doc.sample_onset_page or 0) + 1
                    if rec.raw_name:
                        fields_found["PERSON"] = {"value": rec.raw_name, "page": onset_pg}
                    if rec.raw_dob:
                        fields_found["DATE_OF_BIRTH"] = {"value": rec.raw_dob, "page": onset_pg}
                    if rec.raw_government_id:
                        fields_found["GOVERNMENT_ID"] = {"value": rec.raw_government_id, "page": onset_pg}
                    if rec.raw_address and isinstance(rec.raw_address, dict):
                        fields_found["LOCATION"] = {"value": rec.raw_address.get("full", ""), "page": onset_pg}
                    if rec.raw_email:
                        fields_found["EMAIL"] = {"value": rec.raw_email, "page": onset_pg}
                    if rec.raw_phone:
                        fields_found["PHONE"] = {"value": rec.raw_phone, "page": onset_pg}

                # Estimate total pages from blocks cache
                blocks = doc_blocks_cache.get(doc.id, [])
                total_pages_coord = len(set(b.page_or_sheet for b in blocks)) if blocks else 0

                preview = {
                    "preview_instance": 0,
                    "pages": str((doc.sample_onset_page or 0) + 1),
                    "fields_found": fields_found,
                    "fields_missing": sorted(
                        {fm.field_type for fm in field_map} - set(fields_found.keys())
                    ),
                    "pages_read": [(doc.sample_onset_page or 0) + 1],
                    "total_instances_estimate": total_pages_coord,
                    "extraction_method": "coordinate",
                    "layout_type": "fixed",
                    "layout_confidence": schema.layout_confidence,
                    "field_map_count": len(field_map),
                }
                doc_previews[doc.id] = preview
                preview_count += 1

                logger.info(
                    "Coordinate preview for %s: %d fields found, %d total pages, layout_confidence=%.2f",
                    doc.file_name, len(fields_found), total_pages_coord, schema.layout_confidence,
                )

            yield _sse({
                "stage": "extraction_preview", "status": "complete",
                "message": f"Previewed coordinate extraction for {len(fixed_layout_docs)} document(s)",
                "detail": {"previewed": len(fixed_layout_docs)},
            })

        # --- Stage 4b: Extraction Preview (LLM template docs only) ---
        template_docs = [
            doc for doc in doc_records
            if doc.id in doc_schemas
            and doc_schemas[doc.id] is not None
            and getattr(doc_schemas[doc.id], "template", None) is not None
            and doc_schemas[doc.id].template.pages_per_instance >= 2
            and doc.id not in doc_previews  # don't double-preview coordinate docs
        ]

        if template_docs and settings.llm_assist_enabled:
            yield _sse({
                "stage": "extraction_preview", "status": "running",
                "message": f"Previewing LLM extraction for {len(template_docs)} template document(s)...",
                "detail": {"total": len(template_docs), "current": 0},
            })

            try:
                from app.llm.client import OllamaClient

                llm_client = OllamaClient()

                for idx, doc in enumerate(template_docs, 1):
                    yield _sse({
                        "stage": "extraction_preview", "status": "running",
                        "message": f"Previewing extraction for document {idx}/{len(template_docs)}...",
                        "detail": {"total": len(template_docs), "current": idx},
                    })

                    schema = doc_schemas[doc.id]
                    template = schema.template
                    blocks = doc_blocks_cache.get(doc.id, [])

                    # Build page_texts from ALL blocks (full document)
                    page_texts: dict[int, str] = {}
                    for b in blocks:
                        pg = b.page_or_sheet
                        if pg not in page_texts:
                            page_texts[pg] = b.text
                        else:
                            page_texts[pg] += "\n" + b.text

                    # Filter to content pages (at or after onset) so
                    # instances don't include header/cover pages.
                    onset = doc.sample_onset_page or 0
                    if isinstance(onset, str):
                        try:
                            onset = int(onset)
                        except (ValueError, TypeError):
                            onset = 0
                    content_page_texts = {
                        pg: t for pg, t in page_texts.items() if pg >= onset
                    }

                    # Always prefer marker-based boundaries for accurate count
                    if template.instance_marker:
                        instances = template.find_instance_boundaries(content_page_texts)
                    else:
                        content_pages_sorted = sorted(content_page_texts.keys())
                        ppi = template.pages_per_instance
                        instances = [
                            content_pages_sorted[i:i + ppi]
                            for i in range(0, len(content_pages_sorted), ppi)
                        ]

                    if not instances:
                        continue

                    # Extract from ALL pages of instance 0 (not just identity page)
                    first_instance = instances[0]
                    pages_1indexed = sorted(int(p) + 1 for p in first_instance)
                    instance_texts = [
                        page_texts.get(p, "")[:3000] for p in first_instance
                    ]

                    if not any(t.strip() for t in instance_texts):
                        continue

                    # Build preview prompt that asks for per-field page numbers
                    from app.llm.extraction_prompts import (
                        ALWAYS_EXTRACT_IF_PRESENT,
                        build_preview_extraction_prompt,
                    )

                    preview_prompt = build_preview_extraction_prompt(
                        page_texts=instance_texts,
                        page_numbers_1indexed=pages_1indexed,
                        page_roles=template.page_roles,
                        document_type=schema.document_type,
                    )

                    try:
                        response = llm_client.generate(
                            preview_prompt,
                            system="You extract personal information from documents. "
                            "Respond ONLY with valid JSON.",
                            use_case="extraction_preview",
                            document_id=str(doc.id),
                        )
                        fields_found = _parse_preview_response(response, pages_1indexed)
                    except Exception:
                        logger.warning(
                            "Extraction preview failed for %s",
                            doc.file_name, exc_info=True,
                        )
                        fields_found = {}

                    # Determine expected fields from schema
                    expected_fields: set[str] = set()
                    if template.page_roles:
                        for role in template.page_roles:
                            expected_fields.update(role.pii_fields_expected)
                    expected_fields.update(ALWAYS_EXTRACT_IF_PRESENT)

                    fields_missing = sorted(expected_fields - set(fields_found.keys()))

                    page_range_str = (
                        f"{pages_1indexed[0]}-{pages_1indexed[-1]}"
                        if len(pages_1indexed) > 1
                        else str(pages_1indexed[0])
                    )

                    preview = {
                        "preview_instance": 0,
                        "pages": page_range_str,
                        "fields_found": fields_found,
                        "fields_missing": fields_missing,
                        "pages_read": pages_1indexed,
                        "total_instances_estimate": len(instances),
                        "extraction_method": "llm_template",
                        "pages_per_instance": template.pages_per_instance,
                    }
                    doc_previews[doc.id] = preview
                    preview_count += 1

                    logger.info(
                        "Extraction preview for %s: %d fields found, %d missing, %d instances",
                        doc.file_name, len(fields_found), len(fields_missing), len(instances),
                    )
            except Exception as e:
                logger.warning("Extraction preview stage failed: %s", type(e).__name__)

            yield _sse({
                "stage": "extraction_preview", "status": "complete",
                "message": f"Previewed extraction for {preview_count} document(s)",
                "detail": {"previewed": preview_count},
            })

        # --- Stage 4c: Table Preview (tabular docs) ---
        tabular_docs = [
            doc for doc in doc_records
            if doc.id in doc_schemas
            and doc_schemas[doc.id] is not None
            and getattr(doc_schemas[doc.id], "is_tabular", False)
            and doc_schemas[doc.id].records_per_page_estimate > 1
            and doc.id not in doc_previews  # don't double-preview
        ]

        if tabular_docs and settings.llm_assist_enabled:
            try:
                from app.llm.client import OllamaClient
                from app.structure.llm_template_extractor import LLMTemplateExtractor

                llm_client = OllamaClient()
                table_extractor = LLMTemplateExtractor(llm_client, batch_size=1)

                for doc in tabular_docs:
                    schema = doc_schemas[doc.id]
                    blocks = doc_blocks_cache.get(doc.id, [])

                    # Build page_texts and extract from first page only
                    page_texts_preview: dict[int, str] = {}
                    for b in blocks:
                        pg = b.page_or_sheet
                        if pg not in page_texts_preview:
                            page_texts_preview[pg] = b.text
                        else:
                            page_texts_preview[pg] += "\n" + b.text

                    if not page_texts_preview:
                        continue

                    first_page = min(page_texts_preview.keys())
                    preview_page_texts = {first_page: page_texts_preview[first_page]}

                    try:
                        sample_records = table_extractor.extract_table_pages(
                            schema, preview_page_texts, str(doc.id),
                        )
                    except Exception:
                        sample_records = []

                    # Build tabular preview
                    total_pages_tab = len(page_texts_preview)
                    rpp = schema.records_per_page_estimate

                    sample_rows: list[dict[str, str]] = []
                    for rec in sample_records[:5]:
                        row: dict[str, str] = {}
                        if rec.raw_name:
                            row["PERSON"] = rec.raw_name
                        if rec.raw_address:
                            addr = rec.raw_address
                            row["LOCATION"] = addr.get("raw", str(addr)) if isinstance(addr, dict) else str(addr)
                        if rec.raw_dob:
                            row["DATE_OF_BIRTH"] = rec.raw_dob
                        if rec.raw_government_id:
                            row["GOVERNMENT_ID"] = rec.raw_government_id
                        if rec.raw_email:
                            row["EMAIL"] = rec.raw_email
                        if row:
                            sample_rows.append(row)

                    preview = {
                        "preview_instance": 0,
                        "pages": str(first_page + 1),
                        "fields_found": {
                            k: {"value": v, "page": first_page + 1}
                            for row in sample_rows[:1]
                            for k, v in row.items()
                        } if sample_rows else {},
                        "fields_missing": [],
                        "pages_read": [first_page + 1],
                        "total_instances_estimate": rpp * total_pages_tab,
                        "extraction_method": "llm_table",
                        "pages_per_instance": 1,
                        "is_tabular": True,
                        "records_per_page_estimate": rpp,
                        "sample_rows": sample_rows,
                    }
                    doc_previews[doc.id] = preview
                    preview_count += 1

                    logger.info(
                        "Table preview for %s: %d sample rows, ~%d per page, %d pages",
                        doc.file_name, len(sample_rows), rpp, total_pages_tab,
                    )
            except Exception:
                logger.warning("Table preview stage failed", exc_info=True)

        # --- Stage 5: Entity Analysis (LLM) ---
        yield _sse({
            "stage": "entity_analysis", "status": "running",
            "message": "Analyzing entity relationships...",
            "detail": {"total": len(doc_records), "current": 0},
        })

        entity_analysis_count = 0
        if settings.llm_assist_enabled:
            try:
                from app.structure.llm_entity_analyzer import LLMEntityAnalyzer
                entity_analyzer = LLMEntityAnalyzer(db_session=db)

                for i, doc in enumerate(doc_records, 1):
                    yield _sse({
                        "stage": "entity_analysis", "status": "running",
                        "message": f"Analyzing entities in document {i}/{len(doc_records)}...",
                        "detail": {"total": len(doc_records), "current": i},
                    })

                    detections = doc_detections.get(doc.id, [])
                    blocks = doc_blocks_cache.get(doc.id, [])

                    if not detections or not blocks:
                        continue

                    # Get sample blocks for the onset page
                    onset_page = doc.sample_onset_page or 0
                    sample_blocks = filter_sample_blocks(blocks, onset_page, doc.file_type or "unknown")

                    analysis = entity_analyzer.analyze(
                        blocks=sample_blocks,
                        sample_detections=detections,
                        structure_analysis=doc.structure_analysis,
                        document_id=str(doc.id),
                        onset_page=onset_page,
                    )

                    if analysis is not None:
                        doc.entity_analysis = analysis.to_dict()
                        entity_analysis_count += 1
                        logger.info(
                            "Entity analysis for %s: %d groups, %d individuals",
                            doc.file_name,
                            len(analysis.entity_groups),
                            analysis.estimated_unique_individuals,
                        )

                db.commit()
            except Exception as e:
                logger.warning("Entity analysis stage failed: %s", type(e).__name__)
        else:
            logger.info("LLM assist disabled; skipping entity analysis")

        # Free the blocks cache now that analysis is done
        doc_blocks_cache.clear()
        doc_detections.clear()

        yield _sse({
            "stage": "entity_analysis", "status": "complete",
            "message": f"Analyzed entities in {entity_analysis_count} document(s)",
            "detail": {"analyzed": entity_analysis_count},
        })

        # --- Stage 5: Auto-Approve ---
        yield _sse({
            "stage": "auto_approve", "status": "running",
            "message": "Evaluating auto-approve decisions...",
        })

        approved_count = 0
        review_count = 0

        for doc in doc_records:
            confidences = doc_confidences.get(doc.id, [])
            approved, reason = should_auto_approve(
                confidences,
                protocol_config,
                body.protocol_id,
            )

            # Create DocumentAnalysisReview record
            review = DocumentAnalysisReview(
                document_id=doc.id,
                ingestion_run_id=run.id,
                status="auto_approved" if approved else "pending_review",
                auto_approve_reason=reason,
                sample_confidence_avg=(
                    sum(confidences) / len(confidences) if confidences else None
                ),
                sample_confidence_min=min(confidences) if confidences else None,
                extraction_preview=doc_previews.get(doc.id),
            )
            db.add(review)

            if approved:
                doc.analysis_phase_status = "approved"
                approved_count += 1
            else:
                doc.analysis_phase_status = "pending_review"
                review_count += 1

        db.commit()

        yield _sse({
            "stage": "auto_approve", "status": "complete",
            "message": f"{approved_count} auto-approved, {review_count} pending review",
            "detail": {
                "approved": approved_count,
                "pending_review": review_count,
            },
        })

        # --- Mark run as analyzed ---
        run.status = "analyzed"
        run.analysis_completed_at = datetime.now(timezone.utc)
        db.commit()

        # --- Complete ---
        yield _sse({
            "stage": "complete",
            "result": {
                "job_id": str(job_uuid),
                "status": "analyzed",
                # Include JobResult-compatible fields so frontend doesn't show undefined
                "subjects_found": 0,
                "notification_required": 0,
                # Analyze-specific fields
                "documents_found": len(doc_records),
                "auto_approved": approved_count,
                "pending_review": review_count,
            },
        })

    except Exception as exc:
        logger.error("Job %s failed at analyze phase: %s", str(job_uuid), type(exc).__name__)
        if run is not None:
            run.status = "failed"
            run.error_summary = str(type(exc).__name__)
            run.completed_at = datetime.now(timezone.utc)
            try:
                db.commit()
            except Exception:
                db.rollback()
        yield _sse({"stage": "error", "message": f"Pipeline failed: {type(exc).__name__}"})

    finally:
        if owns_db and db is not None:
            try:
                db.commit()
            except Exception:
                db.rollback()
            finally:
                db.close()


# ---------------------------------------------------------------------------
# Phase 2: Extract generator
# ---------------------------------------------------------------------------

def _serialize_pii_record(rec) -> dict:
    """Serialize a PIIRecord dataclass to a JSON-safe dict."""
    d = dataclasses.asdict(rec)
    # Convert tuples to lists for JSON
    for k, v in d.items():
        if isinstance(v, tuple):
            d[k] = list(v)
    return d


def _deserialize_pii_record(d: dict):
    """Deserialize a dict back into a PIIRecord."""
    from app.rra.entity_resolver import PIIRecord
    # Convert lists back to tuples for tuple fields
    for k in ("entity_types_found", "validation_flags"):
        if k in d and isinstance(d[k], list):
            d[k] = tuple(d[k])
    return PIIRecord(**d)


def _update_extraction_progress(
    db: Session,
    run: IngestionRun,
    *,
    stage: str,
    message: str,
    completed_doc_ids: list[str] | None = None,
    total_docs: int = 0,
    current_doc: int = 0,
    records_found: int = 0,
    detail: dict | None = None,
    result: dict | None = None,
) -> None:
    """Write extraction progress into run.metrics and commit."""
    metrics = dict(run.metrics or {})
    progress = {
        "stage": stage,
        "message": message,
        "heartbeat": datetime.now(timezone.utc).isoformat(),
        "total_docs": total_docs,
        "current_doc": current_doc,
        "records_found": records_found,
    }
    if completed_doc_ids is not None:
        progress["completed_doc_ids"] = completed_doc_ids
    if detail is not None:
        progress["detail"] = detail
    if result is not None:
        progress["result"] = result
    metrics["extraction_progress"] = progress
    run.metrics = metrics
    # Force SQLAlchemy to detect the JSON mutation
    flag_modified(run, "metrics")
    db.commit()


def _is_likely_name(name: str) -> bool:
    """Check if a string looks like a real person name (not header/boilerplate).

    Ported from standalone scripts' is_likely_name() — proven on 34 documents.
    Uses the comprehensive _NAME_BLOCKLIST from coordinate_extractor.
    """
    if not name:
        return False
    t = name.strip()
    if len(t) < 3 or len(t) > 80:
        return False
    # Digits in names = not a person
    if any(c.isdigit() for c in t):
        return False
    words = t.split()
    # At least 2 words (first + last name)
    if len(words) < 2:
        return False
    # At least one word has 3+ chars
    if not any(len(w) >= 3 for w in words):
        return False
    # Check against the comprehensive blocklist
    try:
        from app.pipeline.coordinate_extractor import _NAME_BLOCKLIST
    except ImportError:
        _NAME_BLOCKLIST = frozenset()  # type: ignore[assignment]
    upper_words = [w.upper().rstrip(",.;:") for w in words]
    # If ALL words are in the blocklist, reject
    if all(w in _NAME_BLOCKLIST for w in upper_words if len(w) >= 2):
        return False
    # If the first word (likely surname) is in blocklist, reject
    if upper_words and upper_words[0] in _NAME_BLOCKLIST:
        return False
    return True


def _validate_field_map(field_map: list, doc_path: str) -> bool:
    """Validate field map quality by sampling up to 3 pages.

    Extracts from pages [0, mid, near_end] and checks that at least
    2 of 3 produce valid PERSON names.  Uses the full blocklist and
    ``_is_likely_name()`` for robust validation.

    Rejects field maps that produce garbage (header text, boilerplate,
    single-word names, digits) before they're applied to the entire document.
    """
    if not field_map:
        return False

    # Must have at least a PERSON-mapped field
    try:
        from app.pipeline.coordinate_extractor import _normalize_field_type
    except ImportError:
        return False

    has_person = any(
        _normalize_field_type(fm.field_type) == "PERSON"
        for fm in field_map
    )
    if not has_person:
        return False

    try:
        import fitz
        from app.pipeline.coordinate_extractor import CoordinateExtractor

        pdf_doc = fitz.open(doc_path)
        page_count = pdf_doc.page_count
        pdf_doc.close()

        # Sample up to 3 pages: first, middle, near-end
        sample_pages = [0]
        if page_count > 2:
            sample_pages.append(page_count // 2)
        if page_count > 5:
            sample_pages.append(page_count - 2)

        extractor = CoordinateExtractor(field_map, doc_path, "validation")
        records, _failed = extractor.extract_all_pages(page_range=sample_pages)

        if not records:
            logger.warning("Field map validation: 0 records from %d sample pages", len(sample_pages))
            return False

        # Count how many sample pages produced valid names
        valid_count = 0
        for rec in records:
            if rec.raw_name and _is_likely_name(rec.raw_name):
                valid_count += 1
            elif rec.raw_name:
                logger.debug(
                    "Field map validation: rejected name '%s' on page %s",
                    rec.raw_name, rec.page_range,
                )

        # Need at least 2 valid names, or all if only 1-2 pages
        min_required = min(2, len(sample_pages))
        if valid_count < min_required:
            logger.warning(
                "Field map validation failed: only %d/%d sample pages produced valid names",
                valid_count, len(sample_pages),
            )
            return False

        return True
    except Exception:
        logger.debug("Field map validation error", exc_info=True)
        return False


def run_extraction_background(job_id: str, registry: ProtocolRegistry) -> None:
    """Run extraction in a background thread with its own DB session.

    Progress is written to ``IngestionRun.metrics["extraction_progress"]``
    after each document, allowing the SSE relay to poll and forward events.
    Extracted records are persisted to ``Document.metadata_json["extracted_records"]``
    after each document for crash-resume support.
    """
    from app.api.deps import _get_session_factory

    db = _get_session_factory()()
    run: IngestionRun | None = None

    try:
        job_uuid = UUID(job_id)
        run = db.execute(
            select(IngestionRun).where(IngestionRun.id == job_uuid)
        ).scalar_one_or_none()

        if run is None:
            return

        # --- Load protocol ---
        config_snapshot = run.config_snapshot or {}
        protocol_id = config_snapshot.get("protocol_id", "")
        try:
            protocol = registry.get(protocol_id)
        except KeyError:
            run.status = "failed"
            run.error_summary = f"Protocol not found: {protocol_id!r}"
            run.completed_at = datetime.now(timezone.utc)
            db.commit()
            return

        # --- Load protocol config for entity filtering ---
        protocol_config: dict | None = None
        protocol_config_id = config_snapshot.get("protocol_config_id")
        if protocol_config_id:
            try:
                from app.db.models import ProtocolConfig
                pc = db.get(ProtocolConfig, UUID(protocol_config_id))
                if pc is not None:
                    protocol_config = pc.config_json
            except Exception:
                pass

        target_entities = _resolve_target_entities(protocol_config, protocol_id)

        # --- Resolve dedup anchors from protocol config ---
        dedup_anchors: list[str] | None = None
        if protocol_config and isinstance(protocol_config, dict):
            dedup_anchors = protocol_config.get("dedup_anchors")
            if dedup_anchors is not None and not isinstance(dedup_anchors, list):
                dedup_anchors = None

        # --- Load approved documents ---
        approved_docs = (
            db.execute(
                select(Document).where(
                    Document.ingestion_run_id == run.id,
                    Document.analysis_phase_status == "approved",
                )
            )
            .scalars()
            .all()
        )

        if not approved_docs:
            run.status = "failed"
            run.error_summary = "No approved documents found for extraction"
            run.completed_at = datetime.now(timezone.utc)
            db.commit()
            return

        # --- Resume support: load previously completed docs ---
        existing_progress = (run.metrics or {}).get("extraction_progress", {})
        completed_doc_ids: list[str] = list(existing_progress.get("completed_doc_ids", []))
        completed_set = set(completed_doc_ids)

        from app.rra.entity_resolver import PIIRecord

        all_records: list[PIIRecord] = []

        # Reload records from previously completed docs
        for doc in approved_docs:
            if str(doc.id) in completed_set:
                stored = (doc.metadata_json or {}).get("extracted_records", [])
                for rd in stored:
                    all_records.append(_deserialize_pii_record(rd))

        _update_extraction_progress(
            db, run,
            stage="detection", message="Starting PII detection...",
            completed_doc_ids=completed_doc_ids,
            total_docs=len(approved_docs), current_doc=len(completed_set),
            records_found=len(all_records),
            detail={"total": len(approved_docs), "current": len(completed_set), "status": "running"},
        )

        # --- Check cancellation helper ---
        def _is_cancelled() -> bool:
            db.expire(run)
            return run.status == "cancelled"

        from app.pii.presidio_engine import PresidioEngine
        from app.readers.registry import get_reader

        settings = get_settings()
        engine = PresidioEngine()

        schema_filter_cls = None
        doc_understanding_cls = None
        if settings.llm_assist_enabled:
            try:
                from app.pii.schema_filter import SchemaFilter as _SF
                from app.structure.llm_document_understanding import LLMDocumentUnderstanding as _LDU
                schema_filter_cls = _SF
                doc_understanding_cls = _LDU
            except Exception:
                pass

        # Build per-document selected entity types
        doc_selected_types: dict[UUID, list[str] | None] = {}
        for doc in approved_docs:
            review = db.query(DocumentAnalysisReview).filter(
                DocumentAnalysisReview.document_id == doc.id,
                DocumentAnalysisReview.ingestion_run_id == run.id,
            ).first()
            if review and review.selected_entity_types:
                doc_selected_types[doc.id] = review.selected_entity_types
            else:
                doc_selected_types[doc.id] = None

        for i, doc in enumerate(approved_docs, 1):
            if str(doc.id) in completed_set:
                continue  # already extracted (resume)

            if _is_cancelled():
                return

            _update_extraction_progress(
                db, run,
                stage="detection", message=f"Scanning document {i}/{len(approved_docs)}...",
                completed_doc_ids=completed_doc_ids,
                total_docs=len(approved_docs), current_doc=i,
                records_found=len(all_records),
                detail={"total": len(approved_docs), "current": i, "status": "running"},
            )

            try:
                doc_targets = doc_selected_types.get(doc.id) or target_entities
                reader = get_reader(doc.source_path)
                blocks = reader.read()

                # --- Scanned PDF fallback ---
                # If no text blocks but file is a PDF, get page count from
                # PyMuPDF and run OCR to produce text blocks for all paths.
                is_pdf = (doc.file_type or "").lower() in ("pdf", ".pdf", "application/pdf")
                scanned_page_count = 0
                if not blocks and is_pdf and doc.source_path:
                    try:
                        import fitz
                        pdf_doc = fitz.open(doc.source_path)
                        scanned_page_count = pdf_doc.page_count
                        pdf_doc.close()
                        logger.info(
                            "Scanned PDF detected: %s (%d pages, 0 text blocks)",
                            doc.file_name, scanned_page_count,
                        )
                    except Exception:
                        pass

                    # Run OCR to produce text blocks for all extraction paths
                    if scanned_page_count > 0:
                        try:
                            from app.readers.ocr import ocr_pdf_to_blocks
                            blocks = ocr_pdf_to_blocks(doc.source_path)
                            if blocks:
                                logger.info(
                                    "OCR produced %d text blocks for scanned PDF %s",
                                    len(blocks), doc.file_name,
                                )
                        except ImportError:
                            logger.warning("PaddleOCR not available for %s, falling back to Vision path", doc.file_name)
                        except Exception:
                            logger.warning("OCR failed for %s, falling back to Vision path", doc.file_name, exc_info=True)

                schema = None
                # Try loading schema persisted during analysis phase
                doc_meta = doc.metadata_json or {}
                schema_dict = doc_meta.get("document_schema")
                if schema_dict:
                    try:
                        from app.structure.document_schema import DocumentSchema as _DS
                        schema = _DS.from_dict(schema_dict)
                    except Exception:
                        logger.warning("Failed to load persisted schema for %s", doc.file_name)

                # Fall back to LLM re-computation if no persisted schema
                if schema is None and doc_understanding_cls is not None and schema_filter_cls is not None:
                    try:
                        onset_page = doc.sample_onset_page or 0
                        heuristic_doc_type = "unknown"
                        if doc.structure_analysis and isinstance(doc.structure_analysis, dict):
                            heuristic_doc_type = doc.structure_analysis.get("document_type", "unknown")
                        doc_pages = set(b.page_or_sheet for b in blocks)
                        if not doc_pages and scanned_page_count > 0:
                            doc_pages = set(range(scanned_page_count))
                        total_pages = len(doc_pages)
                        du = doc_understanding_cls(db_session=db)
                        schema = du.understand(
                            blocks,
                            heuristic_doc_type=heuristic_doc_type,
                            file_name=doc.file_name or "",
                            file_type=doc.file_type or "",
                            structure_class=doc.structure_class or "",
                            onset_page=onset_page,
                            document_id=str(doc.id),
                            total_pages=total_pages,
                            protocol_name=protocol_id,
                            protocol_config=protocol_config,
                        )
                    except Exception:
                        pass

                is_template = (
                    schema is not None
                    and schema.template
                    and schema.template.pages_per_instance >= 2
                )
                is_tabular = (
                    schema is not None
                    and schema.is_tabular
                    and schema.records_per_page_estimate > 1
                )
                doc_pages = set(b.page_or_sheet for b in blocks)
                # For scanned PDFs with no text blocks (OCR may also have
                # failed or not been available), populate from PDF page count
                # so Vision path has page numbers to process.
                if not doc_pages and scanned_page_count > 0:
                    doc_pages = set(range(scanned_page_count))
                total_pg = len(doc_pages)

                records: list[PIIRecord] = []
                extraction_path = "3"

                batch_size = DEFAULT_EXTRACTION_BATCH_SIZE
                base_key = protocol_id.lower().replace("-", "_").replace(" ", "_")
                if base_key in PROTOCOL_LLM_CONFIG:
                    batch_size = int(PROTOCOL_LLM_CONFIG[base_key].get(
                        "extraction_batch_size", batch_size,
                    ))

                vision_model = None
                vision_dpi = settings.vision_page_dpi
                if protocol_config and isinstance(protocol_config, dict):
                    vision_model = protocol_config.get("vision_model")
                    if "vision_page_dpi" in protocol_config:
                        vision_dpi = int(protocol_config["vision_page_dpi"])
                if not vision_model and base_key in PROTOCOL_LLM_CONFIG:
                    vision_model = PROTOCOL_LLM_CONFIG[base_key].get("vision_model")
                    if not vision_dpi and "vision_page_dpi" in PROTOCOL_LLM_CONFIG[base_key]:
                        vision_dpi = int(PROTOCOL_LLM_CONFIG[base_key]["vision_page_dpi"])

                instances: list[list[int]] | None = None
                if is_template:
                    if doc.source_path:
                        try:
                            from app.pipeline.instance_detector import find_instance_boundaries as _find_bounds
                            marker = getattr(schema.template, "instance_marker", None)
                            instances = _find_bounds(doc.source_path, marker)
                        except Exception:
                            pass
                    if not instances:
                        page_texts_for_bounds: dict[int, str] = {}
                        for b in blocks:
                            pg = b.page_or_sheet
                            if pg not in page_texts_for_bounds:
                                page_texts_for_bounds[pg] = ""
                            page_texts_for_bounds[pg] += b.text + "\n"
                        if schema.template.instance_marker:
                            instances = schema.template.find_instance_boundaries(page_texts_for_bounds)
                        else:
                            instances = schema.template.get_instance_pages(total_pg)

                # Heartbeat callback — keeps SSE relay from thinking thread is dead
                def _heartbeat_cb(batch_idx: int, total_batches: int, records_so_far: int) -> None:
                    _update_extraction_progress(
                        db, run,
                        stage="detection",
                        message=f"Extracting {doc.file_name}: batch {batch_idx}/{total_batches} ({records_so_far} records)",
                        completed_doc_ids=completed_doc_ids,
                        total_docs=len(approved_docs), current_doc=i,
                        records_found=len(all_records) + records_so_far,
                        detail={"total": len(approved_docs), "current": i, "status": "running",
                                "batch": batch_idx, "total_batches": total_batches},
                    )

                # --- Path 0: Coordinate extraction (vision-routed) ---
                # Load vision routing from analysis phase
                doc_meta = doc.metadata_json or {}
                vision_routing = doc_meta.get("vision_routing", {})
                recommended_path = vision_routing.get("recommended_path", "")

                # Load field map: auditor override > vision field map > LLM field map
                auditor_field_map = None
                auditor_method = None
                if doc_meta.get("auditor_layout_field_map"):
                    from app.structure.document_schema import FieldMapping as _FM
                    auditor_field_map = [
                        _FM(**fm_dict) for fm_dict in doc_meta["auditor_layout_field_map"]
                    ]
                    auditor_method = doc_meta.get("auditor_extraction_method", "coordinate")

                vision_field_map = None
                if doc_meta.get("vision_field_map"):
                    from app.structure.document_schema import FieldMapping as _FM
                    vision_field_map = [
                        _FM(**fm_dict) for fm_dict in doc_meta["vision_field_map"]
                    ]

                # Priority: auditor > vision > LLM schema
                effective_field_map = auditor_field_map or vision_field_map or (
                    getattr(schema, "layout_field_map", None) if schema else None
                )
                use_coordinate = auditor_method != "ai" if auditor_method else True

                # Coordinate path eligibility:
                # - Must have a field map and not be overridden to AI
                # - Either vision recommended "coordinate", OR auditor explicitly set field map,
                #   OR legacy LLM schema says "fixed"/"template_with_drift"
                is_coordinate_path = (
                    effective_field_map is not None
                    and use_coordinate
                    and doc.source_path
                    and (
                        recommended_path == "coordinate"
                        or auditor_field_map is not None
                        or (
                            schema is not None
                            and getattr(schema, "layout_type", "variable") in ("fixed", "template_with_drift")
                        )
                    )
                )

                if is_coordinate_path:
                    # Validate field map quality before full extraction
                    if not _validate_field_map(effective_field_map, doc.source_path):
                        logger.warning(
                            "Field map validation failed for %s, skipping coordinate path",
                            doc.file_name,
                        )
                        is_coordinate_path = False

                if is_coordinate_path:
                    try:
                        from app.pipeline.coordinate_extractor import CoordinateExtractor
                        from app.pipeline.reconciliation import ExtractionReconciler

                        coord_ext = CoordinateExtractor(
                            effective_field_map, doc.source_path, str(doc.id),
                        )
                        coord_records, failed_pages = coord_ext.extract_all_pages()

                        if failed_pages and settings.llm_assist_enabled:
                            from app.llm.client import OllamaClient
                            reconciler = ExtractionReconciler()
                            client = OllamaClient(db_session=db, timeout_s=120)
                            recovered = reconciler.reconcile(
                                failed_pages, doc.source_path, str(doc.id),
                                effective_field_map, client,
                            )
                            coord_records.extend(recovered)

                        # --- Static filter (Step 23d): remove report-wide values ---
                        if coord_records and len(coord_records) >= 5:
                            try:
                                from app.pipeline.static_filter import filter_static_values

                                page_recs_sf: dict[int, list[dict]] = {}
                                for _idx, rec in enumerate(coord_records):
                                    pg = int(rec.page_range) - 1 if rec.page_range and rec.page_range.isdigit() else 0
                                    rec_dict: dict[str, str] = {}
                                    if rec.raw_name: rec_dict["PERSON"] = rec.raw_name
                                    if rec.raw_government_id: rec_dict["US_SSN"] = rec.raw_government_id
                                    if rec.raw_dob: rec_dict["DATE_OF_BIRTH"] = rec.raw_dob
                                    if rec.raw_email: rec_dict["EMAIL_ADDRESS"] = rec.raw_email
                                    if rec.raw_phone: rec_dict["PHONE_NUMBER"] = rec.raw_phone
                                    if rec_dict:
                                        page_recs_sf.setdefault(pg, []).append(rec_dict)

                                cleaned_sf, removed_static = filter_static_values(page_recs_sf)
                                if removed_static:
                                    logger.info("Static filter removed: %s", removed_static)
                                    # Null out static values on the actual PIIRecord objects
                                    _static_set = {(ft, v) for ft, vals in removed_static.items() for v in vals}
                                    for rec in coord_records:
                                        # PIIRecord is frozen — use object.__setattr__
                                        if rec.raw_name and ("PERSON", rec.raw_name) in _static_set:
                                            object.__setattr__(rec, "raw_name", None)
                                        if rec.raw_dob and ("DATE_OF_BIRTH", rec.raw_dob) in _static_set:
                                            object.__setattr__(rec, "raw_dob", None)
                                        if rec.raw_phone and ("PHONE_NUMBER", rec.raw_phone) in _static_set:
                                            object.__setattr__(rec, "raw_phone", None)
                                        if rec.raw_email and ("EMAIL_ADDRESS", rec.raw_email) in _static_set:
                                            object.__setattr__(rec, "raw_email", None)
                                    # Remove records that lost their PERSON (header-only records)
                                    if "PERSON" in removed_static:
                                        before = len(coord_records)
                                        coord_records = [
                                            r for r in coord_records
                                            if r.raw_name or r.raw_government_id
                                        ]
                                        logger.info(
                                            "Static PERSON filter: %d → %d records",
                                            before, len(coord_records),
                                        )
                            except Exception:
                                logger.warning("Static filter failed", exc_info=True)

                        if coord_records:
                            records = coord_records
                            extraction_path = "0-coord"
                            logger.info(
                                "Path 0 (Coordinate/Vision) for %s: %d records (%d failed pages)",
                                doc.file_name, len(records), len(failed_pages),
                            )

                            # Post-extraction verification (Step 22d)
                            # Uses both count-based AND coordinate-based text audit
                            try:
                                from app.pipeline.extraction_verifier import ExtractionVerifier
                                verifier = ExtractionVerifier()
                                recovered_list = recovered if 'recovered' in dir() else []
                                coord_only = [r for r in records if r not in recovered_list] if recovered_list else records
                                verification = verifier.verify(
                                    records=coord_only,
                                    failed_pages=failed_pages,
                                    reconciled_records=recovered_list,
                                    total_pages=total_pg,
                                    field_map=effective_field_map,
                                )
                                
                                # Coordinate-based text audit: verify values exist in source
                                # Builds page_records dict from PIIRecords for the verifier
                                if doc.source_path and records:
                                    page_recs_for_audit: dict[int, list[dict]] = {}
                                    for rec in records:
                                        pg = int(rec.page_range) - 1 if rec.page_range and rec.page_range.isdigit() else -1
                                        if pg < 0:
                                            continue
                                        rec_dict: dict[str, str] = {}
                                        if rec.raw_name: rec_dict["PERSON"] = rec.raw_name
                                        if rec.raw_government_id: rec_dict["US_SSN"] = rec.raw_government_id
                                        if rec.raw_dob: rec_dict["DATE_OF_BIRTH"] = rec.raw_dob
                                        if rec.raw_email: rec_dict["EMAIL_ADDRESS"] = rec.raw_email
                                        if rec.raw_phone: rec_dict["PHONE_NUMBER"] = rec.raw_phone
                                        if rec_dict:
                                            page_recs_for_audit.setdefault(pg, []).append(rec_dict)
                                    
                                    if page_recs_for_audit:
                                        coord_audit = verifier.verify_by_coordinates(
                                            doc.source_path, page_recs_for_audit, sample_size=10,
                                        )
                                        verification.audit_status = coord_audit.audit_status
                                        verification.audit_confidence = coord_audit.audit_confidence
                                        verification.audit_consistency = coord_audit.audit_consistency
                                        verification.pages_audited = coord_audit.pages_audited
                                        # Update summary with audit results
                                        verification.summary += f"\n{coord_audit.summary}"
                                        logger.info(
                                            "Coordinate audit for %s: %s (%d%% confidence)",
                                            doc.file_name, coord_audit.audit_status, coord_audit.audit_confidence,
                                        )
                                
                                logger.info("Verification: %s", verification.summary)
                                _update_extraction_progress(
                                    db, run,
                                    stage="verification",
                                    message=verification.summary,
                                    completed_doc_ids=completed_doc_ids,
                                    total_docs=len(approved_docs), current_doc=i,
                                    records_found=len(all_records) + len(records),
                                    result={
                                        "success_rate": verification.success_rate,
                                        "successful": verification.successful_pages,
                                        "reconciled": verification.reconciled_pages,
                                        "failed": verification.failed_pages,
                                        "field_rates": verification.field_rates,
                                        "is_acceptable": verification.is_acceptable,
                                        # Coordinate audit (Step 23)
                                        "audit_status": verification.audit_status,
                                        "audit_confidence": verification.audit_confidence,
                                        "audit_consistency": verification.audit_consistency,
                                        "pages_audited": verification.pages_audited,
                                        # Static filter (Step 23d)
                                        "removed_static": removed_static if 'removed_static' in dir() else {},
                                    },
                                )
                            except Exception:
                                logger.warning("Post-extraction verification failed", exc_info=True)
                    except Exception:
                        logger.warning(
                            "Path 0 (Coordinate) failed for %s, trying other paths",
                            doc.file_name, exc_info=True,
                        )

                # --- Path 1: Vision direct (small docs or scanned) ---
                # Respects vision routing: if recommended_path is "vision_direct",
                # or if no records yet and vision is available.
                if (
                    not records
                    and settings.use_vision_extraction
                    and settings.llm_assist_enabled
                    and doc.source_path
                    and (doc.file_type or "").lower() in ("pdf", ".pdf", "application/pdf")
                ):
                    try:
                        from app.llm.client import OllamaClient
                        from app.structure.vision_extractor import VisionDocumentExtractor
                        client = OllamaClient(db_session=db)
                        if client.is_vision_available(model_override=vision_model):
                            vision_ext = VisionDocumentExtractor(
                                client, batch_size=batch_size, dpi=vision_dpi, vision_model=vision_model,
                            )
                            if is_template and instances:
                                records = vision_ext.extract_template_instances(
                                    doc.source_path, schema, instances, str(doc.id),
                                    progress_callback=_heartbeat_cb,
                                )
                            elif is_tabular:
                                all_page_nums = sorted(doc_pages)
                                records = vision_ext.extract_table_pages(
                                    doc.source_path, all_page_nums, str(doc.id), schema,
                                    progress_callback=_heartbeat_cb,
                                )
                            else:
                                all_page_nums = sorted(doc_pages)
                                records = vision_ext.extract_pages(
                                    doc.source_path, all_page_nums, str(doc.id), schema,
                                    progress_callback=_heartbeat_cb,
                                )
                            if records:
                                extraction_path = "1"
                                logger.info("Path 1 (Vision) for %s: %d records", doc.file_name, len(records))
                    except Exception:
                        logger.warning("Path 1 (Vision) failed for %s, trying Path 2", doc.file_name, exc_info=True)

                # --- Path 2a: Text + LLM table ---
                if not records and settings.llm_assist_enabled and is_tabular:
                    try:
                        from app.llm.client import OllamaClient
                        from app.structure.llm_template_extractor import LLMTemplateExtractor
                        page_texts_tab: dict[int, str] = {}
                        for b in blocks:
                            pg = b.page_or_sheet
                            if pg not in page_texts_tab:
                                page_texts_tab[pg] = ""
                            page_texts_tab[pg] += b.text + "\n"
                        client = OllamaClient(db_session=db, timeout_s=120)
                        text_extractor = LLMTemplateExtractor(client, batch_size=batch_size)
                        table_records = text_extractor.extract_table_pages(
                            schema, page_texts_tab, str(doc.id),
                            progress_callback=_heartbeat_cb,
                        )
                        if table_records:
                            records = table_records
                            extraction_path = "2-table"
                            logger.info("Path 2a (Text+LLM table) for %s: %d records", doc.file_name, len(records))
                    except Exception:
                        logger.warning("Path 2a (Text+LLM table) failed for %s", doc.file_name, exc_info=True)

                # --- Path 2b: Text + LLM template ---
                if not records and settings.llm_assist_enabled and is_template and instances:
                    try:
                        from app.llm.client import OllamaClient
                        from app.structure.llm_template_extractor import LLMTemplateExtractor
                        page_texts: dict[int, str] = {}
                        for b in blocks:
                            pg = b.page_or_sheet
                            if pg not in page_texts:
                                page_texts[pg] = ""
                            page_texts[pg] += b.text + "\n"
                        client = OllamaClient(db_session=db, timeout_s=120)
                        text_extractor = LLMTemplateExtractor(client, batch_size=batch_size)
                        llm_records = text_extractor.extract_all_instances(
                            schema, page_texts, str(doc.id), total_pg,
                            active_anchors=dedup_anchors,
                            progress_callback=_heartbeat_cb,
                        )
                        if llm_records:
                            records = llm_records
                            extraction_path = "2"
                            logger.info("Path 2 (Text+LLM) for %s: %d records", doc.file_name, len(records))
                    except Exception:
                        logger.warning("Path 2 (Text+LLM) failed for %s, falling back to Path 3", doc.file_name, exc_info=True)

                # --- Path 3: Presidio only ---
                if not records:
                    detections = engine.analyze(blocks, target_entity_types=doc_targets)
                    if schema is not None and schema_filter_cls is not None:
                        try:
                            sf = schema_filter_cls(schema)
                            result = sf.filter_detections(detections)
                            detections = result.kept
                        except Exception:
                            pass
                    if is_template and instances:
                        records = extract_with_template(detections, schema, str(doc.id), total_pg)
                    else:
                        records = [detection_to_pii_record(det, str(doc.id)) for det in detections]
                    extraction_path = "3"
                    logger.info("Path 3 (Presidio) for %s: %d records", doc.file_name, len(records))

                # --- Static filter for non-coordinate paths ---
                # Path 0 has its own static filter; apply to Paths 1/2/3 here.
                if extraction_path != "0-coord" and records and len(records) >= 5:
                    try:
                        from app.pipeline.static_filter import filter_static_values

                        page_recs_sf_all: dict[int, list[dict]] = {}
                        for rec in records:
                            pg = int(rec.page_range) - 1 if rec.page_range and rec.page_range.isdigit() else 0
                            rec_dict: dict[str, str] = {}
                            if rec.raw_name: rec_dict["PERSON"] = rec.raw_name
                            if rec.raw_government_id: rec_dict["US_SSN"] = rec.raw_government_id
                            if rec.raw_dob: rec_dict["DATE_OF_BIRTH"] = rec.raw_dob
                            if rec.raw_email: rec_dict["EMAIL_ADDRESS"] = rec.raw_email
                            if rec.raw_phone: rec_dict["PHONE_NUMBER"] = rec.raw_phone
                            if rec_dict:
                                page_recs_sf_all.setdefault(pg, []).append(rec_dict)

                        _cleaned_sf, removed_static_all = filter_static_values(page_recs_sf_all)
                        if removed_static_all:
                            logger.info("Static filter (Path %s) removed: %s", extraction_path, removed_static_all)
                            _static_set_all = {(ft, v) for ft, vals in removed_static_all.items() for v in vals}
                            for rec in records:
                                if rec.raw_name and ("PERSON", rec.raw_name) in _static_set_all:
                                    object.__setattr__(rec, "raw_name", None)
                                if rec.raw_dob and ("DATE_OF_BIRTH", rec.raw_dob) in _static_set_all:
                                    object.__setattr__(rec, "raw_dob", None)
                                if rec.raw_phone and ("PHONE_NUMBER", rec.raw_phone) in _static_set_all:
                                    object.__setattr__(rec, "raw_phone", None)
                                if rec.raw_email and ("EMAIL_ADDRESS", rec.raw_email) in _static_set_all:
                                    object.__setattr__(rec, "raw_email", None)
                            # Remove records that lost their PERSON
                            if "PERSON" in removed_static_all:
                                before_sf = len(records)
                                records = [r for r in records if r.raw_name or r.raw_government_id]
                                if len(records) < before_sf:
                                    logger.info("Static PERSON filter (Path %s): %d → %d records", extraction_path, before_sf, len(records))
                    except Exception:
                        logger.warning("Static filter failed for Path %s", extraction_path, exc_info=True)

                # --- Inline PERSON validation for all paths ---
                if records:
                    before_name_val = len(records)
                    valid_records = []
                    for rec in records:
                        if rec.raw_name and not _is_likely_name(rec.raw_name):
                            # Null out the bad name but keep record if it has gov ID
                            if rec.raw_government_id:
                                object.__setattr__(rec, "raw_name", None)
                                object.__setattr__(rec, "normalized_value", rec.raw_government_id)
                                valid_records.append(rec)
                            # else: drop the record entirely
                        else:
                            valid_records.append(rec)
                    records = valid_records
                    if len(records) < before_name_val:
                        logger.info(
                            "Inline PERSON validation: %d → %d records for %s",
                            before_name_val, len(records), doc.file_name,
                        )

                # --- Persist records immediately (before validation) ---
                all_records.extend(records)

                # Persist to doc metadata for resume support
                meta = dict(doc.metadata_json or {})
                meta["extracted_records"] = [_serialize_pii_record(r) for r in records]
                doc.metadata_json = meta
                flag_modified(doc, "metadata_json")

                # --- Pattern validation (wrapped separately) ---
                try:
                    if records:
                        from app.pii.pattern_validator import validate_extracted_records
                        pre_count = len(records)
                        validated = validate_extracted_records(records)
                        if len(validated) < pre_count:
                            logger.info("Validation suppressed %d/%d records for %s", pre_count - len(validated), pre_count, doc.file_name)
                        # Replace in all_records with validated versions
                        all_records[-len(records):] = validated
                        records = validated
                except Exception:
                    logger.warning("Pattern validation failed for %s, using raw records", doc.file_name, exc_info=True)

                completed_doc_ids.append(str(doc.id))
                _update_extraction_progress(
                    db, run,
                    stage="detection",
                    message=f"Extracted {len(records)} record(s) from {doc.file_name} (Path {extraction_path})",
                    completed_doc_ids=completed_doc_ids,
                    total_docs=len(approved_docs), current_doc=i,
                    records_found=len(all_records),
                    detail={"extraction_path": extraction_path, "records": len(records), "total": len(approved_docs), "current": i, "status": "running"},
                )

            except Exception as e:
                logger.warning("Detection failed for doc %s: %s", doc.file_name, type(e).__name__, exc_info=True)

        if _is_cancelled():
            return

        _update_extraction_progress(
            db, run,
            stage="detection",
            message=f"Detected {len(all_records)} PII record(s) across {len(approved_docs)} document(s)",
            completed_doc_ids=completed_doc_ids,
            total_docs=len(approved_docs), current_doc=len(approved_docs),
            records_found=len(all_records),
            detail={"records_found": len(all_records), "status": "complete"},
        )

        # --- Stage 2: Entity Resolution ---
        _update_extraction_progress(
            db, run, stage="resolution", message="Resolving entities...",
            completed_doc_ids=completed_doc_ids,
            total_docs=len(approved_docs), current_doc=len(approved_docs),
            records_found=len(all_records),
            detail={"status": "running"},
        )

        from app.rra.entity_resolver import EntityResolver

        resolver = EntityResolver()
        groups = resolver.resolve(all_records, active_anchors=dedup_anchors)

        _update_extraction_progress(
            db, run, stage="resolution",
            message=f"Resolved into {len(groups)} group(s)",
            completed_doc_ids=completed_doc_ids,
            total_docs=len(approved_docs), current_doc=len(approved_docs),
            records_found=len(all_records),
            detail={"status": "complete"},
        )

        # --- Stage 3: Deduplication ---
        _update_extraction_progress(
            db, run, stage="deduplication", message="Building notification subjects...",
            completed_doc_ids=completed_doc_ids,
            total_docs=len(approved_docs), current_doc=len(approved_docs),
            records_found=len(all_records),
            detail={"status": "running"},
        )

        if run.project_id is not None:
            old_count = db.query(NotificationSubject).filter(
                NotificationSubject.project_id == run.project_id,
            ).delete()
            db.commit()
            if old_count:
                logger.info("Cleared %d old notification subjects for project %s", old_count, run.project_id)

        from app.rra.deduplicator import Deduplicator

        dedup = Deduplicator(db)
        subjects = dedup.build_subjects(groups)

        for subj in subjects:
            if subj.project_id is None and run.project_id is not None:
                subj.project_id = run.project_id

        review_count = 0
        try:
            from app.review.queue_manager import QueueManager
            qm = QueueManager(db)
            for idx, group in enumerate(groups):
                if idx >= len(subjects):
                    break
                subj = subjects[idx]
                sid = str(subj.subject_id)
                if group.merge_confidence < 0.60:
                    qm.create_task("escalation", sid)
                    review_count += 1
                elif group.merge_confidence < 0.80 or group.needs_human_review:
                    qm.create_task("low_confidence", sid)
                    review_count += 1
        except Exception:
            pass

        db.commit()

        _update_extraction_progress(
            db, run, stage="deduplication",
            message=f"Built {len(subjects)} subject(s), {review_count} for review",
            completed_doc_ids=completed_doc_ids,
            total_docs=len(approved_docs), current_doc=len(approved_docs),
            records_found=len(all_records),
            detail={"status": "complete"},
        )

        # --- Stage 4: Notification ---
        _update_extraction_progress(
            db, run, stage="notification", message="Building notification list...",
            completed_doc_ids=completed_doc_ids,
            total_docs=len(approved_docs), current_doc=len(approved_docs),
            records_found=len(all_records),
            detail={"status": "running"},
        )

        from app.notification.list_builder import build_notification_list

        nl = build_notification_list(str(job_uuid), protocol, subjects, db)
        notif_count = sum(1 for s in subjects if s.notification_required)

        _update_extraction_progress(
            db, run, stage="notification",
            message=f"{notif_count} notification(s) required",
            completed_doc_ids=completed_doc_ids,
            total_docs=len(approved_docs), current_doc=len(approved_docs),
            records_found=len(all_records),
            detail={"status": "complete"},
        )

        # --- Auto-export CSV ---
        export_count = 0
        if subjects and run.project_id:
            try:
                from app.export.csv_exporter import CSVExporter

                export_dir = Path("/tmp/docdoc_exports") / str(run.project_id)
                export_dir.mkdir(parents=True, exist_ok=True)

                exporter = CSVExporter(db)
                export_job = exporter.run(
                    project_id=run.project_id,
                    output_dir=export_dir,
                    export_schema="auditor",
                )
                export_count = export_job.row_count or 0
            except Exception:
                logger.debug("Auto-export failed (best-effort)", exc_info=True)

        # --- Mark run as completed ---
        run.status = "completed"
        run.completed_at = datetime.now(timezone.utc)
        _update_extraction_progress(
            db, run, stage="complete",
            message="Extraction complete",
            completed_doc_ids=completed_doc_ids,
            total_docs=len(approved_docs), current_doc=len(approved_docs),
            records_found=len(all_records),
            result={
                "job_id": str(job_uuid),
                "status": "COMPLETE",
                "subjects_found": len(subjects),
                "notification_required": notif_count,
                "export_count": export_count,
            },
        )

    except Exception as exc:
        logger.error("Job %s failed at extract phase: %s", str(job_uuid), type(exc).__name__, exc_info=True)
        if run is not None:
            run.status = "failed"
            run.error_summary = str(type(exc).__name__)
            run.completed_at = datetime.now(timezone.utc)
            try:
                metrics = dict(run.metrics or {})
                metrics["extraction_progress"] = {
                    "stage": "error",
                    "message": f"Pipeline failed: {type(exc).__name__}",
                    "heartbeat": datetime.now(timezone.utc).isoformat(),
                }
                run.metrics = metrics
                flag_modified(run, "metrics")
                db.commit()
            except Exception:
                db.rollback()

    finally:
        try:
            db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()


# Currently active background extraction threads, keyed by job_id
_extraction_threads: dict[str, threading.Thread] = {}


def extract_generator(
    job_id: str,
    db: Session | None,
    registry: ProtocolRegistry,
) -> Generator[str, None, None]:
    """SSE relay for extraction — polls background thread progress.

    If the job is in ``analyzed`` status, starts a background extraction thread.
    If the job is already ``extracting`` (reconnect), just polls.
    Yields SSE events in the same format as the previous inline implementation.
    """
    owns_db = False

    try:
        if db is None:
            from app.api.deps import _get_session_factory
            db = _get_session_factory()()
            owns_db = True
    except Exception as exc:
        yield _sse({"stage": "error", "message": f"Database connection failed: {type(exc).__name__}"})
        return

    try:
        job_uuid = UUID(job_id)
    except (ValueError, AttributeError):
        yield _sse({"stage": "error", "message": f"Invalid job_id format: {job_id!r}"})
        return

    run = db.execute(
        select(IngestionRun).where(IngestionRun.id == job_uuid)
    ).scalar_one_or_none()

    if run is None:
        yield _sse({"stage": "error", "message": f"Job {job_id!r} not found"})
        return

    if run.pipeline_mode != "two_phase":
        yield _sse({"stage": "error", "message": f"Job {job_id!r} is not a two-phase pipeline job"})
        return

    # Accept both "analyzed" (start) and "extracting" (reconnect)
    if run.status not in ("analyzed", "extracting"):
        # If already completed/failed, return final state immediately
        if run.status in ("completed", "failed"):
            progress = (run.metrics or {}).get("extraction_progress", {})
            if progress.get("result"):
                yield _sse({"stage": "complete", "result": progress["result"]})
            elif run.status == "failed":
                yield _sse({"stage": "error", "message": progress.get("message", "Pipeline failed")})
            else:
                yield _sse({"stage": "complete", "result": {"job_id": job_id, "status": "COMPLETE"}})
            return
        yield _sse({
            "stage": "error",
            "message": f"Job {job_id!r} status is {run.status!r}, expected 'analyzed' or 'extracting'",
        })
        return

    # --- Start background thread if needed ---
    if run.status == "analyzed":
        run.status = "extracting"
        db.commit()

    _maybe_launch_extraction(job_id, registry)

    # --- Poll loop ---
    last_message = ""
    stale_count = 0
    POLL_INTERVAL = 2  # seconds
    STALE_THRESHOLD = 30  # polls (~60s) before considering heartbeat stale

    while True:
        time.sleep(POLL_INTERVAL)

        # Refresh run from DB to pick up background thread's commits
        db.expire_all()
        run = db.execute(
            select(IngestionRun).where(IngestionRun.id == job_uuid)
        ).scalar_one_or_none()

        if run is None:
            yield _sse({"stage": "error", "message": "Job disappeared"})
            return

        progress = (run.metrics or {}).get("extraction_progress", {})
        stage = progress.get("stage", "")
        message = progress.get("message", "")
        detail = progress.get("detail", {})
        result = progress.get("result")

        # Yield progress if it changed
        if message and message != last_message:
            stale_count = 0
            event: dict = {"stage": stage, "message": message}
            if detail:
                status = detail.get("status", "running")
                event["status"] = status
                event["detail"] = {k: v for k, v in detail.items() if k != "status"}
            if result:
                event["result"] = result
            yield _sse(event)
            last_message = message

        # Check for terminal states
        if run.status == "completed":
            if result:
                yield _sse({"stage": "complete", "result": result})
            else:
                yield _sse({"stage": "complete", "result": {"job_id": job_id, "status": "COMPLETE"}})
            return

        if run.status == "failed":
            yield _sse({"stage": "error", "message": progress.get("message", "Pipeline failed")})
            return

        if run.status == "cancelled":
            yield _sse({"stage": "error", "message": "Extraction cancelled"})
            return

        # Stale heartbeat detection
        heartbeat = progress.get("heartbeat", "")
        if heartbeat:
            try:
                hb_time = datetime.fromisoformat(heartbeat)
                age = (datetime.now(timezone.utc) - hb_time).total_seconds()
                if age > 60:
                    stale_count += 1
                else:
                    stale_count = 0
            except (ValueError, TypeError):
                stale_count += 1
        else:
            stale_count += 1

        if stale_count >= STALE_THRESHOLD:
            _maybe_launch_extraction(job_id, registry)
            stale_count = 0

    # Unreachable, but for clarity
    if owns_db and db is not None:
        db.close()


def _maybe_launch_extraction(job_id: str, registry: ProtocolRegistry) -> None:
    """Launch extraction background thread if not already running."""
    existing = _extraction_threads.get(job_id)
    if existing is not None and existing.is_alive():
        logger.debug("Extraction thread for %s still alive, skipping re-launch", job_id[:8])
        return

    logger.warning("Launching extraction thread for job %s", job_id[:8])
    t = threading.Thread(
        target=run_extraction_background,
        args=(job_id, registry),
        daemon=True,
        name=f"extract-{job_id[:8]}",
    )
    _extraction_threads[job_id] = t
    t.start()