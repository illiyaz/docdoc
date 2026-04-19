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

import concurrent.futures
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
    build_composite_record,
    detection_to_pii_record,
    extract_with_template,
    _PERSON_TYPES as _PERSON_TYPES_SET,
)
from app.core.constants import DEFAULT_EXTRACTION_BATCH_SIZE, PROTOCOL_LLM_CONFIG
from app.pipeline.content_onset import (
    compute_sample_pages,
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


def _refresh_session(db: Session, run_id, doc_ids: list):
    """Close session and create a new one, reattaching ORM objects by ID.

    Releases the DB connection back to the pool during long operations
    (e.g. LLM/vision model calls) so API endpoints are not starved.
    """
    db.close()
    from app.api.deps import _get_session_factory
    new_db = _get_session_factory()()
    run = new_db.get(IngestionRun, run_id)
    docs = new_db.execute(
        select(Document).where(Document.id.in_(doc_ids))
    ).scalars().all()
    return new_db, run, {d.id: d for d in docs}


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
# Schema-based vision skip (Step 2 optimization)
# ---------------------------------------------------------------------------


def _try_schema_skip(schema, doc_total_pages: int = 0):
    """Check if LLM Document Understanding already produced enough info to skip vision.

    Returns ``(routing_dict, field_map_dicts)`` if the schema is sufficient
    to determine the extraction path, otherwise ``None``.

    ``routing_dict`` follows the same shape as ``VisionRoutingResult`` serialized
    to dict.  ``field_map_dicts`` is a list of FieldMapping dicts or ``None``.
    """
    if schema is None:
        return None

    layout_type = getattr(schema, "layout_type", "variable")
    field_map = getattr(schema, "layout_field_map", None)

    # Tabular check MUST come BEFORE fixed-layout check.
    # A doc can be "fixed" layout (every page looks the same) but also
    # tabular (multiple records per page).  Coordinate extraction only
    # handles 1 record/page, so tabular docs MUST go to llm_table.
    is_tabular = getattr(schema, "is_tabular", False)
    rpp = getattr(schema, "records_per_page_estimate", 1)
    if is_tabular and rpp > 1:
        return {
            "structure_type": "table",
            "structure_confidence": getattr(schema, "schema_confidence", 0.7),
            "pii_fields": [],
            "records_per_page": rpp,
            "cross_page_data": False,
            "pages_per_instance": 1,
            "recommended_path": "llm_table",
        }, None

    # Fixed/template_with_drift + has field map + single-record → coordinate path
    # (Only reached if NOT tabular — tabular check above takes priority)
    if layout_type in ("fixed", "template_with_drift") and field_map:
        fm_dicts = [
            {
                "field_type": fm.field_type,
                "anchor_text": fm.anchor_text,
                "spatial_relationship": fm.spatial_relationship,
                "value_pattern": fm.value_pattern,
                "sample_bbox": getattr(fm, "sample_bbox", []),
                "line_count": getattr(fm, "line_count", 1),
                "skip_pattern": getattr(fm, "skip_pattern", None),
                "entity_role": getattr(fm, "entity_role", None),
            }
            for fm in field_map
        ]
        return {
            "structure_type": "fixed_single_page",
            "structure_confidence": getattr(schema, "layout_confidence", 0.8),
            "pii_fields": [],
            "records_per_page": 1,
            "cross_page_data": False,
            "pages_per_instance": 1,
            "recommended_path": "coordinate",
        }, fm_dicts

    # Multi-page template → llm_template path
    template = getattr(schema, "template", None)
    if template is not None and getattr(template, "pages_per_instance", 1) >= 2:
        return {
            "structure_type": "multi_page_template",
            "structure_confidence": getattr(schema, "schema_confidence", 0.7),
            "pii_fields": [],
            "records_per_page": 1,
            "cross_page_data": True,
            "pages_per_instance": getattr(template, "pages_per_instance", 2),
            "recommended_path": "llm_template",
        }, None

    # Not enough info — needs vision routing
    return None


# ---------------------------------------------------------------------------
# Parallel vision routing worker (Step 3 optimization)
# ---------------------------------------------------------------------------


def _route_single_document(doc_info: dict, router, template_cache, builder_cls) -> dict:
    """Route one document via vision model.  Thread-safe, no DB access.

    Parameters
    ----------
    doc_info : dict
        Keys: doc_id, source_path, file_name, file_type, onset, total_pages, is_scanned
    router : VisionRouter
    template_cache : TemplateCache (thread-safe)
    builder_cls : FieldMapBuilder class

    Returns
    -------
    dict with keys: routing_dict, field_map_dicts, name_samples, cache_hit, error
    """
    source_path = doc_info["source_path"]
    onset = doc_info["onset"]
    total_pages = doc_info["total_pages"]
    is_scanned = doc_info["is_scanned"]

    result = {
        "doc_id": doc_info["doc_id"],
        "routing_dict": None,
        "field_map_dicts": None,
        "name_samples": [],
        "cache_hit": False,
        "error": None,
    }

    try:
        # --- Template cache check ---
        cached_entry = template_cache.get(source_path, onset)
        if cached_entry:
            result["routing_dict"] = cached_entry.routing_dict
            result["field_map_dicts"] = cached_entry.field_map_dicts
            result["name_samples"] = cached_entry.name_samples
            result["cache_hit"] = True
            return result

        # --- Vision model call ---
        from app.pipeline.vision_router import VisionRoutingResult
        routing = router.analyze_document(
            source_path, onset_page=onset,
            total_pages=total_pages, is_scanned=is_scanned,
        )

        # --- Enrich pii_fields with segregation role_map ---
        seg_role_map = doc_info.get("segregation_role_map", {})
        if seg_role_map and routing.pii_fields:
            for pf in routing.pii_fields:
                if pf.get("role"):
                    continue  # VisionRouter already assigned a role
                # Match by label or field name
                label = pf.get("label", "") or pf.get("name", "")
                matched_role = seg_role_map.get(label)
                if not matched_role:
                    # Fuzzy: try partial match
                    label_lower = label.lower()
                    for seg_name, seg_role in seg_role_map.items():
                        if seg_name.lower() in label_lower or label_lower in seg_name.lower():
                            matched_role = seg_role
                            break
                if matched_role:
                    pf["role"] = matched_role

        # --- PERSON validation ---
        try:
            from app.pipeline.person_discovery import is_likely_name, discover_person_from_text
            person_fields = [f for f in routing.pii_fields if f.get("type") == "PERSON"]
            valid_persons = [f for f in person_fields if is_likely_name(f.get("value", ""))]
            invalid_persons = [f for f in person_fields if not is_likely_name(f.get("value", ""))]

            if person_fields and not valid_persons:
                discovered, best_page = discover_person_from_text(source_path, onset)
                if discovered:
                    routing.pii_fields = [
                        f for f in routing.pii_fields if f.get("type") != "PERSON"
                    ] + discovered
                    onset = best_page
                else:
                    routing.recommended_path = "presidio"
            elif invalid_persons and valid_persons:
                routing.pii_fields = [
                    f for f in routing.pii_fields if f.get("type") != "PERSON"
                ] + valid_persons
        except Exception:
            pass  # PERSON validation is best-effort

        # --- Build field map for coordinate path ---
        field_map = None
        if routing.recommended_path == "coordinate":
            builder = builder_cls()
            field_map = builder.build_field_map(routing, source_path, page_num=onset)

            if field_map:
                try:
                    from app.pipeline.coordinate_extractor import CoordinateExtractor
                    test_ext = CoordinateExtractor(field_map, source_path, "validation")
                    test_records, _ = test_ext.extract_all_pages(page_range=[onset])
                    if not test_records or not test_records[0].raw_name:
                        field_map = None
                        routing.recommended_path = "vision_direct" if total_pages <= 5 else "presidio"
                except Exception:
                    field_map = None
                    routing.recommended_path = "vision_direct" if total_pages <= 5 else "presidio"

        # --- Serialize results ---
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
                    "entity_role": getattr(fm, "entity_role", None),
                }
                for fm in field_map
            ]
        name_samples = [
            f.get("value", "") for f in routing.pii_fields if f.get("type") == "PERSON"
        ]

        # Store in template cache
        try:
            template_cache.put(source_path, onset, routing_dict, fm_dicts, name_samples)
        except Exception:
            pass

        result["routing_dict"] = routing_dict
        result["field_map_dicts"] = fm_dicts
        result["name_samples"] = name_samples
        return result

    except Exception as exc:
        result["error"] = str(exc)
        return result


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

    # --- Create or reuse IngestionRun record ---
    # The HTTP handler may have already created the run (to avoid race
    # conditions with the SSE relay).  If so, reuse it.
    run = db.get(IngestionRun, job_uuid)
    if run is None:
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
    else:
        # Ensure status is running (handler created it as running)
        if run.status != "running":
            run.status = "running"
            run.started_at = datetime.now(timezone.utc)
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

        # --- Stage 1.7: Segregation (LLM-first file classification) ---
        # Classifies each file as PII vs non-PII, identifies document type,
        # field inventory, and role attribution.  Results stored on Document
        # metadata_json for downstream stages.  Best-effort — failures
        # don't block the pipeline.
        segregation_results: dict[str, "SegregationResult"] = {}  # doc_id → result
        if settings.llm_assist_enabled:
            yield _sse({
                "stage": "segregation", "status": "running",
                "message": f"Classifying {len(doc_records)} file(s) with LLM...",
                "detail": {"total": len(doc_records), "current": 0},
            })
            try:
                from app.pipeline.segregation import SegregationEngine

                _seg_project_id = str(project_uuid) if project_uuid else None
                seg_engine = SegregationEngine(
                    db_session=db,
                    project_id=_seg_project_id,
                )
                seg_pii_count = 0
                seg_non_pii_count = 0

                for seg_idx, doc in enumerate(doc_records, 1):
                    try:
                        seg_result = seg_engine.classify(
                            file_path=doc.source_path,
                            document_id=str(doc.id),
                        )
                        if seg_result:
                            segregation_results[str(doc.id)] = seg_result
                            # Persist segregation result on the Document record
                            if doc.metadata_json is None:
                                doc.metadata_json = {}
                            doc.metadata_json["segregation"] = seg_result.to_dict()
                            flag_modified(doc, "metadata_json")

                            if seg_result.pii_detected:
                                seg_pii_count += 1
                                # Plumb late-onset page → content_onset_page
                                # so extraction skips boilerplate pages
                                if seg_result.classification_method == "text_late_onset":
                                    import re as _onset_re
                                    _onset_match = _onset_re.search(
                                        r"page (\d+)", seg_result.summary or ""
                                    )
                                    if _onset_match:
                                        _late_page = int(_onset_match.group(1))
                                        # Set onset a few pages before the discovered page
                                        # to catch the boundary (K-1 cover page, etc.)
                                        doc.content_onset_page = max(0, _late_page - 3)
                                        logger.info(
                                            "Late onset plumbing: %s onset set to page %d (PII found at %d)",
                                            doc.file_name, doc.content_onset_page, _late_page,
                                        )
                            else:
                                seg_non_pii_count += 1

                            logger.info(
                                "Segregation: %s → pii=%s type=%s fields=%s (%.1fs)",
                                doc.file_name, seg_result.pii_detected,
                                seg_result.document_type,
                                seg_result.field_inventory,
                                seg_result.processing_time_ms / 1000,
                            )
                    except Exception:
                        logger.warning("Segregation failed for %s", doc.file_name, exc_info=True)

                    if seg_idx % 5 == 0 or seg_idx == len(doc_records):
                        yield _sse({
                            "stage": "segregation", "status": "running",
                            "message": f"Classified {seg_idx}/{len(doc_records)} files",
                            "detail": {"total": len(doc_records), "current": seg_idx},
                        })

                db.commit()
                yield _sse({
                    "stage": "segregation", "status": "complete",
                    "message": f"Segregation: {seg_pii_count} PII, {seg_non_pii_count} non-PII",
                    "detail": {"pii_count": seg_pii_count, "non_pii_count": seg_non_pii_count},
                })
            except ImportError:
                logger.warning("Segregation engine not available — skipping")
                yield _sse({
                    "stage": "segregation", "status": "complete",
                    "message": "Segregation skipped (engine not available)",
                })
            except Exception:
                logger.warning("Segregation stage failed — continuing without it", exc_info=True)
                yield _sse({
                    "stage": "segregation", "status": "complete",
                    "message": "Segregation skipped (error)",
                })
        else:
            yield _sse({
                "stage": "segregation", "status": "complete",
                "message": "Segregation skipped (LLM assist disabled)",
            })

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
        from app.readers.pdf_reader import PDFReader, get_pdf_page_count
        from app.tasks.structure_analysis import StructureAnalysisTask

        structure_task = StructureAnalysisTask()
        doc_blocks_cache: dict[UUID, list] = {}  # cache blocks for sample_extraction
        doc_total_pages: dict[UUID, int] = {}  # true page count (not derived from blocks)

        # Filter out non-PII docs — skip expensive analysis for files
        # that segregation classified as non-PII.
        _pii_docs = []
        _non_pii_skipped = 0
        for doc in doc_records:
            seg = (doc.metadata_json or {}).get("segregation", {})
            if isinstance(seg, dict) and seg.get("pii_detected") is False:
                _non_pii_skipped += 1
                logger.info("Skipping analysis for non-PII doc: %s", doc.file_name)
            else:
                _pii_docs.append(doc)
        if _non_pii_skipped:
            logger.info("Skipped %d non-PII docs from analysis", _non_pii_skipped)
            yield _sse({
                "stage": "structure_analysis", "status": "running",
                "message": f"Skipped {_non_pii_skipped} non-PII doc(s), analyzing {len(_pii_docs)}...",
            })
        doc_records = _pii_docs  # all downstream stages use filtered list

        for i, doc in enumerate(doc_records, 1):
            yield _sse({
                "stage": "structure_analysis", "status": "running",
                "message": f"Analyzing structure of document {i}/{len(doc_records)}...",
                "detail": {"total": len(doc_records), "current": i},
            })
            try:
                is_pdf = (doc.file_type or "").lower() in ("pdf", ".pdf", "application/pdf")

                # Tiered page sampling: for large PDFs, read only a sample
                if is_pdf and doc.source_path:
                    page_count = get_pdf_page_count(doc.source_path)
                    doc_total_pages[doc.id] = page_count

                    if page_count > 5:
                        sample_page_nums = compute_sample_pages(page_count)
                        reader = PDFReader(doc.source_path)
                        blocks = reader.read_pages(sample_page_nums)
                        logger.info(
                            "Sampled %d/%d pages for structure analysis of %s",
                            len(sample_page_nums), page_count, doc.file_name,
                        )
                    else:
                        reader = get_reader(doc.source_path)
                        blocks = reader.read()
                else:
                    reader = get_reader(doc.source_path)
                    blocks = reader.read()

                # Scanned PDF fallback: run OCR when no text blocks found
                if not blocks and is_pdf and doc.source_path:
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

                # Pass segregation result to skip redundant LLM call
                seg_dict = None
                if doc.metadata_json and doc.metadata_json.get("segregation"):
                    seg_dict = doc.metadata_json["segregation"]

                result = structure_task.run(
                    blocks, str(doc.id),
                    db_session=db,
                    segregation_result=seg_dict,
                )
                doc.structure_analysis = result.to_dict()
            except Exception as e:
                logger.warning("Structure analysis failed for doc %s: %s", doc.file_name, type(e).__name__)

        db.commit()

        # Release session before onset detection (may do Presidio + fitz I/O)
        all_doc_ids = [d.id for d in doc_records]
        db, run, _doc_map = _refresh_session(db, run.id, all_doc_ids)
        doc_records = [_doc_map[did] for did in all_doc_ids if did in _doc_map]

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
                # If segregation already set content_onset_page (late-onset),
                # trust it — Presidio won't find PII on early pages either.
                onset_page: int | str = 0

                if doc.content_onset_page and doc.content_onset_page > 0:
                    onset_page = doc.content_onset_page
                    logger.info(
                        "Using segregation late-onset page %d for %s (skipping Presidio onset)",
                        onset_page, doc.file_name,
                    )
                elif (doc.file_type or "").lower() == "pdf":
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

                    # Get heuristic doc type from structure analysis
                    heuristic_doc_type = "unknown"
                    if doc.structure_analysis and isinstance(doc.structure_analysis, dict):
                        heuristic_doc_type = doc.structure_analysis.get("document_type", "unknown")

                    # Use true page count when available (from PDF metadata),
                    # fall back to distinct block pages (sampled blocks undercount)
                    total_pages = doc_total_pages.get(doc.id, 0)
                    if total_pages == 0:
                        total_pages = len(set(b.page_or_sheet for b in blocks)) if blocks else 0

                    # Pass ALL blocks — _build_multi_page_text handles page
                    # slicing (onset + pages_to_read) and char budget internally.
                    # Previously filter_sample_blocks capped at onset+2 pages,
                    # starving the LLM when it needed 9+ pages for large docs.
                    schema = doc_understanding.understand(
                        blocks,
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

                    # Vision fallback: if text-based understanding returned
                    # nothing (e.g. scanned PDF, image-only doc), try vision.
                    if schema is None and doc.source_path:
                        ft = (doc.file_type or "").lower().lstrip(".")
                        if ft in doc_understanding._RENDERABLE_TYPES:
                            schema = doc_understanding.understand_with_vision(
                                doc.source_path,
                                file_type=ft,
                                file_name=doc.file_name or "",
                                onset_page=onset_page,
                                document_id=str(doc.id),
                            )
                            if schema is not None:
                                logger.info(
                                    "Vision fallback produced schema for %s",
                                    doc.file_name,
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

                        # Commit schema immediately so it survives per-doc
                        # failures in SchemaFilter or subsequent docs.
                        try:
                            db.commit()
                        except Exception:
                            db.rollback()
                            logger.warning(
                                "Failed to persist schema for %s", doc.file_name,
                                exc_info=True,
                            )

                        # Apply SchemaFilter to this doc's detections
                        detections = doc_detections.get(doc.id, [])
                        if detections:
                            try:
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
                            except Exception:
                                db.rollback()
                                logger.warning(
                                    "SchemaFilter failed for %s", doc.file_name,
                                    exc_info=True,
                                )

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

        # Flush any remaining document_schema / metadata changes before
        # releasing the session — _refresh_session closes the old session,
        # so uncommitted writes (e.g. schema set at doc.metadata_json) would
        # be silently lost.
        try:
            db.commit()
        except Exception:
            db.rollback()

        # Release session before vision routing (long model calls)
        all_doc_ids = [d.id for d in doc_records]
        db, run, _doc_map = _refresh_session(db, run.id, all_doc_ids)
        doc_records = [_doc_map[did] for did in all_doc_ids if did in _doc_map]

        # --- Stage 4b-0: Vision-based routing for extraction path ---
        # Uses vision model to classify document structure and identify PII
        # fields.  Optimized: schema-skip + parallel routing.

        doc_previews: dict[UUID, dict] = {}
        preview_count = 0
        vision_routed_docs: set[UUID] = set()
        schema_skipped_docs: set[UUID] = set()

        if settings.llm_assist_enabled and settings.use_vision_extraction:
            try:
                from app.llm.client import OllamaClient
                from app.pipeline.vision_router import VisionRouter, VisionRoutingResult
                from app.pipeline.field_map_builder import FieldMapBuilder

                vision_client = OllamaClient(db_session=db, timeout_s=300)
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

                    # --- Phase A: Schema-based skip (no vision call needed) ---
                    work_items: list[dict] = []

                    for vr_idx, doc in enumerate(doc_records, 1):
                        if not doc.source_path:
                            continue

                        blocks = doc_blocks_cache.get(doc.id, [])
                        is_scanned = (
                            len(blocks) == 0
                            and (doc.file_type or "").lower() in ("pdf", ".pdf", "application/pdf")
                        )
                        onset = doc.sample_onset_page or 0
                        total_pages = doc_total_pages.get(doc.id, 0)
                        if total_pages == 0:
                            total_pages = len(set(b.page_or_sheet for b in blocks)) if blocks else 0
                        if total_pages == 0 and is_scanned and doc.source_path:
                            try:
                                import fitz
                                pdf_doc = fitz.open(doc.source_path)
                                total_pages = pdf_doc.page_count
                                pdf_doc.close()
                            except Exception:
                                pass

                        # Try schema-based skip first
                        schema = doc_schemas.get(doc.id)
                        skip_result = _try_schema_skip(schema, total_pages)

                        if skip_result is not None:
                            routing_dict, fm_dicts = skip_result
                            schema_skipped_docs.add(doc.id)

                            # Enrich field maps with segregation role_map
                            _skip_seg = (doc.metadata_json or {}).get("segregation", {})
                            if isinstance(_skip_seg, dict) and fm_dicts:
                                # Build type→role lookup from segregation fields
                                _seg_fields = _skip_seg.get("fields", [])
                                _type_to_role: dict[str, str] = {}
                                for sf in _seg_fields:
                                    if isinstance(sf, dict) and sf.get("type") and sf.get("role"):
                                        _type_to_role[sf["type"].upper()] = sf["role"]
                                # Also use role_map (name→role) as fallback
                                _skip_role_map = _skip_seg.get("role_map", {})

                                if _type_to_role or _skip_role_map:
                                    for fm_d in fm_dicts:
                                        if fm_d.get("entity_role"):
                                            continue  # already has role from LLM
                                        # Primary match: field_type → role from segregation fields
                                        ft = fm_d.get("field_type", "").upper()
                                        if ft in _type_to_role:
                                            fm_d["entity_role"] = _type_to_role[ft]
                                        else:
                                            # Fallback: anchor text vs role_map names
                                            anchor = fm_d.get("anchor_text", "")
                                            for seg_name, seg_role in _skip_role_map.items():
                                                if seg_name.lower() in anchor.lower() or anchor.lower() in seg_name.lower():
                                                    fm_d["entity_role"] = seg_role
                                                    break

                            # Auto-correct spatial relationships using actual PDF word positions
                            if fm_dicts and doc.source_path and routing_dict.get("recommended_path") == "coordinate":
                                try:
                                    from app.pipeline.field_map_builder import auto_correct_field_map
                                    from app.structure.document_schema import FieldMapping

                                    _onset_pg = doc.sample_onset_page or 0
                                    _fm_objs = [
                                        FieldMapping(
                                            field_type=fd.get("field_type", ""),
                                            anchor_text=fd.get("anchor_text", ""),
                                            spatial_relationship=fd.get("spatial_relationship", "line_below"),
                                            value_pattern=fd.get("value_pattern"),
                                            sample_bbox=fd.get("sample_bbox", []),
                                            line_count=fd.get("line_count", 1),
                                            skip_pattern=fd.get("skip_pattern"),
                                            entity_role=fd.get("entity_role"),
                                        )
                                        for fd in fm_dicts
                                    ]
                                    _corrected = auto_correct_field_map(_fm_objs, doc.source_path, _onset_pg)
                                    fm_dicts = [
                                        {
                                            "field_type": fm.field_type,
                                            "anchor_text": fm.anchor_text,
                                            "spatial_relationship": fm.spatial_relationship,
                                            "value_pattern": fm.value_pattern,
                                            "sample_bbox": getattr(fm, "sample_bbox", []),
                                            "line_count": getattr(fm, "line_count", 1),
                                            "skip_pattern": getattr(fm, "skip_pattern", None),
                                            "entity_role": getattr(fm, "entity_role", None),
                                        }
                                        for fm in _corrected
                                    ]
                                except Exception:
                                    logger.warning(
                                        "auto_correct_field_map failed for %s", doc.file_name,
                                        exc_info=True,
                                    )

                            # Persist routing to metadata (same as vision path)
                            doc_meta = doc.metadata_json or {}
                            doc_meta["vision_routing"] = {
                                "structure_type": routing_dict["structure_type"],
                                "recommended_path": routing_dict["recommended_path"],
                                "pii_field_count": len(routing_dict.get("pii_fields", [])),
                                "records_per_page": routing_dict.get("records_per_page", 1),
                                "cross_page_data": routing_dict.get("cross_page_data", False),
                                "template_cache_hit": False,
                                "schema_skip": True,
                            }
                            if fm_dicts:
                                doc_meta["vision_field_map"] = fm_dicts
                            doc.metadata_json = doc_meta
                            flag_modified(doc, "metadata_json")

                            preview = {
                                "extraction_method": routing_dict["recommended_path"],
                                "structure_type": routing_dict["structure_type"],
                                "pii_fields": [f.get("type", "") for f in routing_dict.get("pii_fields", [])],
                                "field_map_count": len(fm_dicts) if fm_dicts else 0,
                                "total_instances_estimate": (
                                    total_pages if routing_dict.get("records_per_page", 1) == 1
                                    else total_pages * routing_dict.get("records_per_page", 1)
                                ),
                                "sample_values": {},
                                "schema_skip": True,
                            }
                            doc_previews[doc.id] = preview
                            vision_routed_docs.add(doc.id)

                            yield _sse({
                                "stage": "vision_routing", "status": "running",
                                "message": f"Schema skip for {doc.file_name} → {routing_dict['recommended_path']}",
                                "detail": {
                                    "total": len(doc_records), "current": vr_idx,
                                    "doc_name": doc.file_name,
                                    "recommended_path": routing_dict["recommended_path"],
                                    "schema_skip": True,
                                },
                            })
                        else:
                            # Needs vision routing — add to work queue
                            # Include segregation role_map if available
                            _seg_role_map: dict[str, str] = {}
                            _doc_seg = (doc.metadata_json or {}).get("segregation", {})
                            if isinstance(_doc_seg, dict):
                                _seg_role_map = _doc_seg.get("role_map", {})

                            work_items.append({
                                "doc_id": doc.id,
                                "source_path": doc.source_path,
                                "file_name": doc.file_name,
                                "file_type": doc.file_type,
                                "onset": onset,
                                "total_pages": total_pages,
                                "is_scanned": is_scanned,
                                "segregation_role_map": _seg_role_map,
                            })

                    db.commit()

                    # --- Phase B: Parallel vision routing for remaining docs ---
                    if work_items:
                        yield _sse({
                            "stage": "vision_routing", "status": "running",
                            "message": f"Vision routing {len(work_items)} document(s) "
                                       f"({len(schema_skipped_docs)} skipped via schema)...",
                            "detail": {
                                "total": len(doc_records),
                                "current": len(schema_skipped_docs),
                                "schema_skipped": len(schema_skipped_docs),
                                "needs_vision": len(work_items),
                            },
                        })

                        # Build doc lookup for post-processing
                        doc_by_id = {d.id: d for d in doc_records}

                        max_workers = min(settings.vision_routing_workers, len(work_items))
                        vision_results: dict[UUID, dict] = {}

                        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
                            future_to_item = {
                                pool.submit(
                                    _route_single_document, item, router,
                                    template_cache, FieldMapBuilder,
                                ): item
                                for item in work_items
                            }
                            routed_count = 0
                            try:
                                for future in concurrent.futures.as_completed(future_to_item, timeout=3600):
                                    item = future_to_item[future]
                                    try:
                                        res = future.result(timeout=300)
                                        vision_results[item["doc_id"]] = res
                                        routed_count += 1
                                        rec_path = (res.get("routing_dict") or {}).get("recommended_path", "unknown")
                                        yield _sse({
                                            "stage": "vision_routing", "status": "running",
                                            "message": f"Routed {item['file_name']} → {rec_path} ({routed_count}/{len(work_items)})",
                                            "detail": {
                                                "total": len(work_items), "current": routed_count,
                                                "doc_name": item["file_name"],
                                                "recommended_path": rec_path,
                                                "cache_hit": res.get("cache_hit", False),
                                            },
                                        })
                                    except Exception as exc:
                                        routed_count += 1
                                        logger.warning(
                                            "Vision routing worker failed for %s: %s",
                                            item["file_name"], exc,
                                        )
                            except TimeoutError:
                                logger.warning(
                                    "Parallel vision routing global timeout (3600s) — %d/%d completed",
                                    routed_count, len(work_items),
                                )

                        # --- Phase C: Write results to DB on main thread ---
                        for item in work_items:
                            doc_id = item["doc_id"]
                            doc = doc_by_id.get(doc_id)
                            if doc is None:
                                continue

                            res = vision_results.get(doc_id)
                            if res is None or res.get("routing_dict") is None:
                                continue

                            routing_dict = res["routing_dict"]
                            fm_dicts = res.get("field_map_dicts")
                            cache_hit = res.get("cache_hit", False)

                            # Persist to metadata
                            doc_meta = doc.metadata_json or {}
                            doc_meta["vision_routing"] = {
                                "structure_type": routing_dict.get("structure_type", "variable"),
                                "recommended_path": routing_dict.get("recommended_path", "presidio"),
                                "pii_field_count": len(routing_dict.get("pii_fields", [])),
                                "records_per_page": routing_dict.get("records_per_page", 1),
                                "cross_page_data": routing_dict.get("cross_page_data", False),
                                "template_cache_hit": cache_hit,
                            }
                            if fm_dicts:
                                doc_meta["vision_field_map"] = fm_dicts
                            # Persist person samples
                            name_samples = res.get("name_samples", [])
                            if name_samples:
                                doc_meta["person_samples"] = [
                                    s.strip() for s in name_samples if s.strip()
                                ]
                            doc.metadata_json = doc_meta
                            flag_modified(doc, "metadata_json")

                            pii_fields = routing_dict.get("pii_fields", [])
                            total_pages = item["total_pages"]
                            rpp = routing_dict.get("records_per_page", 1)
                            preview = {
                                "extraction_method": routing_dict.get("recommended_path", "presidio"),
                                "structure_type": routing_dict.get("structure_type", "variable"),
                                "pii_fields": [f.get("type", "") for f in pii_fields],
                                "field_map_count": len(fm_dicts) if fm_dicts else 0,
                                "total_instances_estimate": (
                                    total_pages if rpp == 1 else total_pages * rpp
                                ),
                                "sample_values": {
                                    f.get("type", ""): f.get("value", "")[:50]
                                    for f in pii_fields[:5]
                                },
                                "cache_hit": cache_hit,
                            }
                            doc_previews[doc.id] = preview
                            vision_routed_docs.add(doc.id)

                            yield _sse({
                                "stage": "vision_routing", "status": "running",
                                "message": f"Routed {doc.file_name} → {routing_dict.get('recommended_path', 'presidio')}",
                                "detail": {
                                    "total": len(doc_records),
                                    "current": len(vision_routed_docs),
                                    "doc_name": doc.file_name,
                                    "recommended_path": routing_dict.get("recommended_path", "presidio"),
                                    "cache_hit": cache_hit,
                                },
                            })

                        db.commit()

                    yield _sse({
                        "stage": "vision_routing", "status": "complete",
                        "message": f"Vision-routed {len(vision_routed_docs)} document(s) "
                                   f"({len(schema_skipped_docs)} schema-skipped)",
                        "detail": {
                            "routed": len(vision_routed_docs),
                            "schema_skipped": len(schema_skipped_docs),
                        },
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

                # Use true page count when available, fall back to blocks
                total_pages_coord = doc_total_pages.get(doc.id, 0)
                if total_pages_coord == 0:
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

                    # Build tabular preview — use true page count when available
                    total_pages_tab = doc_total_pages.get(doc.id, 0) or len(page_texts_preview)
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

            # If segregation already approved this doc as PII, auto-approve
            # for extraction — don't make the auditor click Approve again.
            _seg = (dict(doc.metadata_json or {})).get("segregation", {})
            if isinstance(_seg, dict) and _seg.get("pii_detected") is True:
                approved = True
                reason = (reason or "") + " | segregation-approved"

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


def _check_extraction_quality(records: list, path_label: str) -> bool:
    """Return True if records have acceptable extraction quality.

    Called after each extraction path to decide whether to keep results
    or discard and try the next path.

    Non-destructive: records that have gov IDs or emails are meaningful
    even without names.  Only reject if records are truly empty.

    For small sets (1-2 records), require at least 1 useful record.
    Path 3 (Presidio final fallback) should NOT be gated.
    """
    if not records:
        return False
    total = len(records)
    # Count records with a valid name OR meaningful PII (gov ID, email)
    useful = sum(1 for r in records if (
        (r.raw_name and _is_likely_name(r.raw_name))
        or r.raw_government_id
        or r.raw_email
    ))
    named = sum(1 for r in records if r.raw_name and _is_likely_name(r.raw_name))
    if total <= 2:
        if useful >= 1:
            return True
        logger.warning(
            "Quality gate (%s): %d/%d useful records — rejecting",
            path_label, useful, total,
        )
        return False
    ratio = useful / total
    if ratio < 0.20:
        logger.warning(
            "Quality gate (%s): %.0f%% useful (%d/%d, %d named) — rejecting",
            path_label, ratio * 100, useful, total, named,
        )
        return False
    if named < total * 0.30:
        logger.info(
            "Quality gate (%s): low name rate %.0f%% (%d/%d) but %.0f%% useful — keeping",
            path_label, named / total * 100, named, total, ratio * 100,
        )
    return True


def _count_valid_names(records: list) -> int:
    """Count records with valid PERSON names (used by field map validation)."""
    count = 0
    for rec in records:
        if rec.raw_name and _is_likely_name(rec.raw_name):
            count += 1
        elif rec.raw_name:
            logger.debug(
                "Field map validation: rejected name '%s' on page %s",
                rec.raw_name, getattr(rec, "page_range", "?"),
            )
    return count


# --- Per-doc timeout (A7: thread resilience) ---
# Hard upper bound on how long a single document can take.
# Even if the doc IS making progress, kill it after this many seconds.
# Prevents a single large doc from blocking the entire queue.
# Base timeout from settings (default 1800s = 30 min), but scales
# with page count: large docs (3000+ pages) get proportionally more time.
_DOC_HARD_TIMEOUT_BASE = 1800  # base: 30 minutes for small docs


def _compute_doc_timeout(page_count: int) -> int:
    """Scale the per-document hard timeout with page count.

    Base: 30 min for ≤200 pages.
    Scale: +2s per page beyond 200.
    Cap: 4 hours (14400s) — no doc should take longer.

    Examples:
        200 pages → 1800s (30 min)
        500 pages → 2400s (40 min)
        1000 pages → 3400s (57 min)
        3000 pages → 7400s (123 min)
    """
    from app.core.settings import get_settings
    base = get_settings().doc_hard_timeout_s or _DOC_HARD_TIMEOUT_BASE
    extra = max(0, page_count - 200) * 2  # 2s per page beyond 200
    return min(base + extra, 14400)  # cap at 4 hours


class _DocTimeoutError(Exception):
    """Raised when a single document exceeds the hard timeout."""


class _DocStallDetector:
    """Detect if per-doc extraction has stalled (no progress for N seconds).

    Instead of a hard timeout, monitors whether records/pages are advancing.
    Only triggers if the doc is < 80% done AND no progress for 5 minutes.
    Allows slow-but-progressing docs to finish.
    """

    STALL_THRESHOLD_SECONDS = 300  # 5 minutes with no progress = stalled
    COMPLETION_THRESHOLD = 0.80    # if ≥80% done, let it finish

    def __init__(self) -> None:
        self._last_progress_time = time.time()
        self._last_record_count = 0
        self._total_pages = 0

    def update(self, record_count: int, current_page: int = 0, total_pages: int = 0) -> None:
        """Call periodically with current extraction state."""
        if total_pages > 0:
            self._total_pages = total_pages
        if record_count > self._last_record_count:
            self._last_record_count = record_count
            self._last_progress_time = time.time()

    def is_stalled(self) -> bool:
        """Return True if extraction appears stalled and should be interrupted."""
        elapsed_since_progress = time.time() - self._last_progress_time
        if elapsed_since_progress < self.STALL_THRESHOLD_SECONDS:
            return False  # Made progress recently — not stalled

        # Check if nearly done — let it finish
        if self._total_pages > 0 and self._last_record_count > 0:
            # Rough estimate: if we have lots of records relative to pages, we're far along
            # Can't know exact page progress, so use record count as proxy
            pass  # Can't reliably determine % done from record count alone

        return True  # No progress for 5+ minutes = stalled

    @property
    def stall_seconds(self) -> float:
        return time.time() - self._last_progress_time


def _validate_field_map(field_map: list, doc_path: str, onset: int = 0) -> bool:
    """Validate field map quality by onset-aware page sampling.

    **Strategy 1**: Sample onset page + next 4 consecutive content pages
    (onset, onset+1, ..., onset+4).  These are virtually guaranteed to
    have data because onset detection already found PII there.
    Require 2 out of 5 valid PERSON names.

    **Strategy 2** (fallback): If strategy 1 fails, sample 3 random pages
    from the middle third of the document as a second chance.

    Only rejects the field map if BOTH strategies fail.
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

        if page_count == 0:
            return False

        # --- Strategy 1: onset + next 4 consecutive pages ---
        strategy1_pages = [
            p for p in range(onset, min(onset + 5, page_count))
        ]

        extractor = CoordinateExtractor(field_map, doc_path, "validation")
        records, _failed = extractor.extract_all_pages(page_range=strategy1_pages)

        valid_count = _count_valid_names(records)

        # Need at least 2 valid names (or 1 if only 1 page)
        min_required = min(2, len(strategy1_pages))
        if valid_count >= min_required:
            # Strategy 1 passed — continue to drift check
            pass
        else:
            logger.debug(
                "Field map validation strategy 1 (onset pages): %d/%d valid — trying strategy 2",
                valid_count, len(strategy1_pages),
            )

            # --- Strategy 2: 3 random pages from middle third ---
            import random
            mid_start = page_count // 3
            mid_end = 2 * page_count // 3
            if mid_end <= mid_start:
                mid_start = 0
                mid_end = page_count
            pool = [p for p in range(mid_start, mid_end) if p not in set(strategy1_pages)]
            strategy2_pages = random.sample(pool, min(3, len(pool))) if pool else []

            if strategy2_pages:
                records2, _ = extractor.extract_all_pages(page_range=strategy2_pages)
                valid_count2 = _count_valid_names(records2)
                if valid_count2 >= min(2, len(strategy2_pages)):
                    logger.info(
                        "Field map validation strategy 2 (random middle): %d/%d valid — accepted",
                        valid_count2, len(strategy2_pages),
                    )
                    # Use strategy 2 records for drift check below
                    records = records2
                else:
                    logger.warning(
                        "Field map validation failed: strategy 1 (%d valid) + strategy 2 (%d valid) both insufficient",
                        valid_count, valid_count2,
                    )
                    return False
            else:
                logger.warning(
                    "Field map validation failed: strategy 1 (%d valid), no pages for strategy 2",
                    valid_count,
                )
                return False

        # --- Anchor drift detection ---
        # Check if anchor positions are stable across sample pages.
        # If anchors move significantly, this is NOT a fixed-layout doc.
        DRIFT_THRESHOLD = 20.0  # points (~7mm)
        try:
            drift_map = extractor.check_anchor_stability(strategy1_pages, DRIFT_THRESHOLD)
            if drift_map:
                drifted = {ft: d for ft, d in drift_map.items() if d > DRIFT_THRESHOLD}
                if len(drifted) > len(drift_map) / 2:
                    logger.warning(
                        "Field map validation: anchor drift detected (%d/%d fields): %s",
                        len(drifted), len(drift_map),
                        {k: f"{v:.1f}pt" for k, v in drifted.items()},
                    )
                    return False
        except Exception:
            logger.debug("Anchor drift check failed", exc_info=True)

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

        # A7: Track failed docs for end-of-run summary
        _failed_docs: list[dict] = []

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
                logger.info("[%d/%d] SKIP (already extracted): %s", i, len(approved_docs), doc.file_name)
                continue  # already extracted (resume)

            if _is_cancelled():
                return

            _doc_start = time.time()
            _stall_detector = _DocStallDetector()
            _doc_meta_pre = doc.metadata_json or {}
            _doc_page_count = _doc_meta_pre.get("total_pages", 200)
            _doc_timeout = _compute_doc_timeout(int(_doc_page_count) if _doc_page_count else 200)
            _vr = _doc_meta_pre.get("vision_routing", {})
            logger.info(
                "[%d/%d] START: %s | type=%s | pages=%s | routing=%s | path=%s",
                i, len(approved_docs), doc.file_name,
                doc.file_type or "?",
                _doc_meta_pre.get("total_pages", "?"),
                _vr.get("recommended_path", "none"),
                _vr.get("structure_type", "?"),
            )

            _update_extraction_progress(
                db, run,
                stage="detection", message=f"Scanning document {i}/{len(approved_docs)}: {doc.file_name}...",
                completed_doc_ids=completed_doc_ids,
                total_docs=len(approved_docs), current_doc=i,
                records_found=len(all_records),
                detail={"total": len(approved_docs), "current": i, "status": "running",
                        "doc_name": doc.file_name, "recommended_path": _vr.get("recommended_path", "none")},
            )

            records: list[PIIRecord] = []  # A7: init before try so except can always reference it
            extraction_path = "?"

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

                # Secondary tabular detection: even if schema missed it,
                # count SSN/name patterns on a sample page.  If >1 found,
                # the doc is tabular and coordinate path would miss records.
                #
                # IMPORTANT: Names alone are unreliable — a school report card
                # has 1 student but 6-8 names (student + parents + teachers).
                # SSN count is the reliable indicator of distinct individuals.
                # When the LLM already said records_per_page=1, trust it
                # unless SSNs disagree (SSNs don't lie about individuals).
                if not is_tabular and blocks and total_pg > 5:
                    import re as _re_tab
                    _sample_pg = min(b.page_or_sheet for b in blocks if isinstance(b.page_or_sheet, int))
                    _sample_text = "\n".join(
                        b.text for b in blocks if b.page_or_sheet == _sample_pg
                    )
                    _ssn_count = len(_re_tab.findall(r'\d{3}-\d{2}-\d{4}', _sample_text))
                    _name_count = len(_re_tab.findall(
                        r'(?:^|\n)\s*[A-Z][a-z]+[, ]+[A-Z][a-z]+', _sample_text,
                    ))

                    # LLM schema is authoritative when available.
                    # Only override LLM with regex when SSNs prove multiple
                    # distinct individuals (names can't — supporting entities
                    # inflate name counts on single-record pages).
                    _llm_says_single = (
                        schema is not None
                        and schema.records_per_page_estimate <= 1
                    )

                    if _llm_says_single:
                        # Trust LLM unless multiple SSNs prove otherwise
                        if _ssn_count > 1:
                            is_tabular = True
                            logger.info(
                                "Override LLM single-record: %s has %d SSNs on page %d "
                                "(LLM said rpp=1, but SSNs prove multiple individuals)",
                                doc.file_name, _ssn_count, _sample_pg,
                            )
                        else:
                            logger.info(
                                "Trusting LLM rpp=1 for %s (names=%d but SSNs=%d — "
                                "names likely include supporting entities)",
                                doc.file_name, _name_count, _ssn_count,
                            )
                    else:
                        # No LLM or LLM already says tabular — use regex as before
                        _detected_rpp = max(_ssn_count, _name_count)
                        if _detected_rpp > 1:
                            is_tabular = True
                            logger.info(
                                "Auto-detected tabular layout for %s: %d records on sample page %d "
                                "(SSNs=%d, names=%d)",
                                doc.file_name, _detected_rpp, _sample_pg,
                                _ssn_count, _name_count,
                            )

                records: list[PIIRecord] = []
                extraction_path = "3"

                # ============================================================
                # EXTRACTION PATH SELECTION
                #
                # When USE_TEXT_LLM_BATCH=true (Step 37):
                #   Text PDFs → Text Batch (primary) → done
                #   No selector, no table, no Presidio for text docs.
                #   Coordinate is optional pre-step if field map validates.
                #
                # When USE_TEXT_LLM_BATCH=false (legacy):
                #   Selector → Coordinate → Table → Presidio
                # ============================================================
                _doc_meta_pre = doc.metadata_json or {}

                if settings.use_text_llm_batch and settings.llm_assist_enabled and blocks:
                    # ── Step 37: Auto-select Strategy A (markers) or B (full text) ──
                    try:
                        from app.pipeline.text_batch_extractor import extract_text_batch, extract_with_markers
                        from app.pipeline.repeating_unit_detector import detect_markers, detect_visual_separators
                        from app.llm.client import OllamaClient as _TBClient

                        # Build page_texts from content pages (skip pre-onset)
                        _tb_page_texts: dict[int, str] = {}

                        if (doc.file_type or "").lower() in ("pdf", ".pdf") and doc.source_path:
                            try:
                                import fitz as _tb_fitz
                                _tb_doc = _tb_fitz.open(doc.source_path)
                                _tb_onset = doc.content_onset_page or doc.sample_onset_page or 0
                                for pg_idx in range(_tb_onset, _tb_doc.page_count):
                                    text = _tb_doc[pg_idx].get_text()
                                    if text.strip():
                                        _tb_page_texts[pg_idx] = text
                                    _tb_doc._forget_page(pg_idx)
                                _tb_doc.close()
                            except Exception:
                                pass

                        # Fallback: use blocks if PDF read failed
                        if not _tb_page_texts:
                            for b in blocks:
                                pg = b.page_or_sheet
                                if isinstance(pg, int):
                                    if pg not in _tb_page_texts:
                                        _tb_page_texts[pg] = ""
                                    _tb_page_texts[pg] += b.text + "\n"

                        if _tb_page_texts:
                            _tb_client = _TBClient(db_session=db, timeout_s=180)
                            _tb_seg = _doc_meta_pre.get("segregation", {})
                            _tb_doc_type = _tb_seg.get("document_type", "unknown") if isinstance(_tb_seg, dict) else "unknown"
                            _tb_fields = _tb_seg.get("field_inventory", []) if isinstance(_tb_seg, dict) else []
                            _onset = doc.sample_onset_page or 0

                            # --- Marker detection (one LLM call) ---
                            _markers = {}
                            try:
                                _markers = detect_markers(
                                    doc.source_path, _tb_client, onset_page=_onset,
                                )
                            except Exception:
                                logger.warning("Marker detection failed for %s", doc.file_name, exc_info=True)

                            _strategy = _markers.get("strategy", "B")
                            _record_unit = _markers.get("record_unit", "page")
                            _records_per_page = _markers.get("records_per_page", 1)

                            # Vision fallback: if text says "page" but page is dense
                            if _record_unit == "page" and _records_per_page <= 1:
                                # Check if pages are suspiciously dense for 1 record
                                _sample_pg = list(_tb_page_texts.keys())[len(_tb_page_texts) // 2]
                                if len(_tb_page_texts.get(_sample_pg, "")) > 4000:
                                    try:
                                        _vis_result = detect_visual_separators(
                                            doc.source_path, _tb_client,
                                            page_num=_sample_pg,
                                            vision_model=settings.ollama_vision_model,
                                        )
                                        if _vis_result:
                                            _record_unit = _vis_result.get("record_unit", _record_unit)
                                            _records_per_page = _vis_result.get("records_per_page", _records_per_page)
                                            _markers["record_unit"] = _record_unit
                                            _markers["records_per_page"] = _records_per_page
                                            logger.info(
                                                "[%d/%d] Vision detected separators: unit=%s, rpp=%d for %s",
                                                i, len(approved_docs), _record_unit, _records_per_page, doc.file_name,
                                            )
                                    except Exception:
                                        pass  # vision fallback is optional

                            # --- Strategy A: Marker-filter extraction ---
                            if _strategy == "A":
                                logger.info(
                                    "[%d/%d] STRATEGY A (marker-filter): %s | %d pages | marker='%s'",
                                    i, len(approved_docs), doc.file_name,
                                    len(_tb_page_texts),
                                    (_markers.get("name_after_label") or _markers.get("name_before_label", ""))[:30],
                                )

                                _seg = (doc.metadata_json or {}).get("segregation", {})
                                _country_hint = _seg.get("country_hint")
                                _field_labels = [
                                    f.get("name") for f in _seg.get("fields", [])
                                    if isinstance(f, dict) and f.get("name")
                                ]
                                records = extract_with_markers(
                                    page_texts=_tb_page_texts,
                                    ollama_client=_tb_client,
                                    doc_id=str(doc.id),
                                    markers=_markers,
                                    records_per_page=_records_per_page,
                                    field_inventory=_tb_fields,
                                    country_hint=_country_hint,
                                    field_labels=_field_labels,
                                )

                                if records:
                                    extraction_path = "A-marker-filter"
                                    logger.info(
                                        "[%d/%d] STRATEGY A DONE: %s | %d records",
                                        i, len(approved_docs), doc.file_name, len(records),
                                    )

                            # --- Strategy B: Full text batch ---
                            if not records:
                                logger.info(
                                    "[%d/%d] STRATEGY B (text batch): %s | %d pages | type=%s",
                                    i, len(approved_docs), doc.file_name,
                                    len(_tb_page_texts), _tb_doc_type,
                                )

                                _country_hint = (doc.metadata_json or {}).get("segregation", {}).get("country_hint")
                                records = extract_text_batch(
                                    page_texts=_tb_page_texts,
                                    ollama_client=_tb_client,
                                    doc_id=str(doc.id),
                                    document_type=_tb_doc_type,
                                    field_inventory=_tb_fields,
                                    record_unit=_record_unit,
                                    records_per_page=_records_per_page,
                                    country_hint=_country_hint,
                                )

                                if records:
                                    extraction_path = "B-text-batch"
                                    logger.info(
                                        "[%d/%d] STRATEGY B DONE: %s | %d records from %d pages",
                                        i, len(approved_docs), doc.file_name,
                                        len(records), len(_tb_page_texts),
                                    )

                    except Exception:
                        logger.warning(
                            "Text extraction failed for %s — falling through to legacy",
                            doc.file_name, exc_info=True,
                        )
                    finally:
                        # Release page text dict — can be 7+ MB for large docs
                        _tb_page_texts.clear()

                # ── Legacy path: Selector → Coordinate → Table → Presidio ──
                # Only runs when text batch is OFF or failed to produce records
                _text_batch_produced_records = len(records) > 0
                _selector_produced_records = _text_batch_produced_records  # skip all legacy if text batch worked
                if not records and settings.use_extraction_selector:
                    _use_selector = True
                    _vr_pre = _doc_meta_pre.get("vision_routing", {})
                    _recommended = _vr_pre.get("recommended_path", "")
                    _has_field_map = bool(
                        _doc_meta_pre.get("auditor_layout_field_map")
                        or _doc_meta_pre.get("vision_field_map")
                        or (_doc_meta_pre.get("document_schema", {}) or {}).get("layout_field_map")
                    )
                    if _recommended == "coordinate" and _has_field_map:
                        logger.info(
                            "[%d/%d] SELECTOR SKIPPED for %s — coordinate with field map",
                            i, len(approved_docs), doc.file_name,
                        )
                        _use_selector = False

                    if _use_selector:
                        try:
                            from app.pipeline.extraction_selector import (
                                build_document_profile, pick_sample_pages,
                            )
                            from app.pipeline.extraction_methods import compete_methods
                            from app.pipeline.content_onset import find_verified_onset

                            _onset = doc.metadata_json.get("verified_onset", 0) if doc.metadata_json else 0
                            _profile = build_document_profile(
                                doc_path=doc.source_path, blocks=blocks,
                                file_type=doc.file_type or "pdf", file_name=doc.file_name,
                                onset_page=_onset, engine=engine,
                            )
                            _sample_pages = pick_sample_pages(_profile, blocks, n=5)
                            _schema = None
                            _schema_dict = _doc_meta_pre.get("document_schema")
                            if _schema_dict:
                                try:
                                    from app.structure.document_schema import DocumentSchema as _SelDS
                                    _schema = _SelDS.from_dict(_schema_dict)
                                except Exception:
                                    pass

                            _llm_client = None
                            if settings.llm_assist_enabled:
                                try:
                                    from app.llm.client import OllamaClient
                                    _llm_client = OllamaClient()
                                except Exception:
                                    pass

                            _winner, _results = compete_methods(
                                profile=_profile, blocks=blocks, sample_pages=_sample_pages,
                                schema=_schema, field_map=None, engine=engine,
                                llm_client=_llm_client, target_entities=doc_targets,
                            )

                            if _winner is not None:
                                _all_data_pages = sorted(doc_pages)
                                if isinstance(_profile.onset_page, int):
                                    _all_data_pages = [p for p in _all_data_pages if p >= _profile.onset_page]
                                extraction_path = f"selector:{_winner.name}"
                                records = _winner.extract(
                                    pages=_all_data_pages, blocks=blocks,
                                    profile=_profile, schema=_schema,
                                )
                                logger.info(
                                    "[%d/%d] SELECTOR: %s | %d records via %s",
                                    i, len(approved_docs), doc.file_name,
                                    len(records), _winner.name,
                                )
                        except Exception:
                            logger.warning("Selector failed for %s", doc.file_name, exc_info=True)

                    _selector_produced_records = len(records) > 0

                # ============================================================
                # LARGE DOC FAST-PATH (>500 pages)
                # Skip LLM-heavy paths (1, 2a, 2b). Instead:
                # 1. LLM reads pages 0-20 to learn the pattern (~1 min)
                # 2. Try coordinate extraction on ALL pages (30ms/page)
                # 3. Fall back to Presidio smart-group (30s for 3000 pages)
                # ============================================================
                _is_large_doc = total_pg > 500
                if _is_large_doc and not _selector_produced_records:
                    logger.info(
                        "[%d/%d] LARGE DOC: %s (%d pages) — using fast-path (LLM sample + coordinate/Presidio)",
                        i, len(approved_docs), doc.file_name, total_pg,
                    )

                    # Step 1: LLM learns from first 20 pages (4 batches × 5 pages)
                    llm_sample_records: list[PIIRecord] = []
                    if settings.llm_assist_enabled and blocks:
                        try:
                            from app.llm.client import OllamaClient
                            from app.structure.llm_template_extractor import LLMTemplateExtractor

                            sample_page_texts: dict[int, str] = {}
                            sample_pages = sorted(doc_pages)[:20]
                            for b in blocks:
                                if b.page_or_sheet in sample_pages:
                                    if b.page_or_sheet not in sample_page_texts:
                                        sample_page_texts[b.page_or_sheet] = ""
                                    sample_page_texts[b.page_or_sheet] += b.text + "\n"

                            if sample_page_texts:
                                client = OllamaClient(db_session=db, timeout_s=120)
                                extractor = LLMTemplateExtractor(
                                    client, batch_size=DEFAULT_EXTRACTION_BATCH_SIZE,
                                )
                                llm_sample_records = extractor.extract_all_instances(
                                    schema, sample_page_texts, doc.source_path, len(sample_pages),
                                ) if schema else []
                                if llm_sample_records:
                                    logger.info(
                                        "Large doc LLM sample: %d records from %d pages of %s",
                                        len(llm_sample_records), len(sample_pages), doc.file_name,
                                    )
                        except Exception:
                            logger.warning("Large doc LLM sample failed for %s", doc.file_name, exc_info=True)

                    # Step 2: Try coordinate extraction on ALL pages
                    coord_records: list[PIIRecord] = []
                    effective_field_map = None
                    if schema and getattr(schema, "layout_field_map", None):
                        effective_field_map = schema.layout_field_map
                    if not effective_field_map:
                        vision_fm = doc_meta.get("vision_field_map")
                        if vision_fm:
                            try:
                                from app.structure.document_schema import FieldMapping as _FM
                                effective_field_map = [_FM(**f) for f in vision_fm]
                            except Exception:
                                pass

                    if effective_field_map:
                        try:
                            from app.pipeline.coordinate_extractor import CoordinateExtractor
                            _person_samples = doc_meta.get("person_samples")
                            coord_ext = CoordinateExtractor(
                                effective_field_map, doc.source_path, str(doc.id),
                                name_samples=_person_samples or None,
                            )
                            coord_records, _failed = coord_ext.extract_all_pages()
                            if coord_records:
                                logger.info(
                                    "Large doc coordinate: %d records from %s (%d failed pages)",
                                    len(coord_records), doc.file_name, len(_failed),
                                )
                        except Exception:
                            logger.warning("Large doc coordinate failed for %s", doc.file_name, exc_info=True)

                    # Step 3: Presidio smart-group ALL pages (always runs as backup)
                    presidio_records: list[PIIRecord] = []
                    if blocks:
                        try:
                            detections = engine.analyze(blocks, target_entity_types=doc_targets)
                            if schema is not None and schema_filter_cls is not None:
                                try:
                                    sf = schema_filter_cls(schema)
                                    result = sf.filter_detections(detections)
                                    detections = result.kept
                                except Exception:
                                    pass
                            from app.pipeline.smart_grouping import group_detections_to_records as _sg
                            presidio_records = _sg(
                                detections, str(doc.id),
                                schema=schema, doc_path=doc.source_path,
                            )
                            logger.info(
                                "Large doc Presidio: %d records from %s (%d detections)",
                                len(presidio_records), doc.file_name, len(detections),
                            )
                        except Exception:
                            logger.warning("Large doc Presidio failed for %s", doc.file_name, exc_info=True)

                    # Merge: coordinate > LLM sample > Presidio gap-fill
                    if coord_records:
                        records = coord_records
                        extraction_path = "0-coord-large"
                    elif llm_sample_records:
                        records = llm_sample_records
                        extraction_path = "2-llm-sample"

                    # Add Presidio gap-fill (names not already found)
                    if presidio_records:
                        existing_names = {r.raw_name.lower().strip() for r in records if r.raw_name}
                        existing_ids = {r.raw_government_id for r in records if r.raw_government_id}
                        existing_emails = {r.raw_email for r in records if r.raw_email}
                        for pr in presidio_records:
                            if pr.raw_name and pr.raw_name.lower().strip() not in existing_names:
                                records.append(pr)
                                existing_names.add(pr.raw_name.lower().strip())
                            elif not pr.raw_name:
                                # Only add orphans if their key PII isn't already in a named record
                                is_dup = False
                                if pr.raw_government_id and pr.raw_government_id in existing_ids:
                                    is_dup = True
                                if pr.raw_email and pr.raw_email in existing_emails:
                                    is_dup = True
                                if not is_dup and (pr.raw_government_id or pr.raw_phone or pr.raw_email):
                                    records.append(pr)
                        if not extraction_path.startswith("0"):
                            extraction_path = "3-presidio-large"

                    logger.info(
                        "[%d/%d] LARGE DOC DONE: %s | path=%s | records=%d | time=%.1fs",
                        i, len(approved_docs), doc.file_name, extraction_path,
                        len(records), time.time() - _doc_start,
                    )

                    # Skip normal Path 0/1/2/3 logic — handled above

                # ============================================================
                # NORMAL EXTRACTION PATHS (≤500 pages)
                # Only runs if large-doc fast-path was NOT triggered
                # ============================================================

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

                # Heartbeat callback — keeps SSE relay alive + stall detection + hard timeout
                class _StallError(Exception):
                    """Raised when per-doc extraction stalls (no progress for 5 min)."""

                def _heartbeat_cb(batch_idx: int, total_batches: int, records_so_far: int) -> None:
                    # Hard timeout check (A7) — absolute wall-clock limit per doc
                    _elapsed = time.time() - _doc_start
                    if _elapsed > _doc_timeout:
                        raise _DocTimeoutError(
                            f"Hard timeout after {_elapsed:.0f}s "
                            f"at batch {batch_idx}/{total_batches} ({records_so_far} records)"
                        )
                    _stall_detector.update(records_so_far, batch_idx, total_batches)
                    if _stall_detector.is_stalled():
                        raise _StallError(
                            f"Stalled for {_stall_detector.stall_seconds:.0f}s "
                            f"at batch {batch_idx}/{total_batches} ({records_so_far} records)"
                        )
                    # Write progress every 10 batches (reduces DB commits for large docs)
                    # Still checks timeout/stall every batch above.
                    if batch_idx % 10 == 0 or batch_idx == total_batches:
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
                # - Must NOT be tabular (multiple records per page)
                # - Either vision recommended "coordinate", OR auditor explicitly set field map,
                #   OR legacy LLM schema says "fixed"/"template_with_drift"
                is_coordinate_path = (
                    effective_field_map is not None
                    and use_coordinate
                    and not is_tabular  # tabular docs MUST use llm_table, not coordinate
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
                    _onset = doc.sample_onset_page or 0
                    if not _validate_field_map(effective_field_map, doc.source_path, onset=_onset):
                        logger.warning(
                            "Field map validation failed for %s, skipping coordinate path",
                            doc.file_name,
                        )
                        is_coordinate_path = False
                        # Re-classify as tabular if it has multiple records per page
                        # (the LLM said "fixed" but coordinate extraction disagrees)
                        if not is_tabular and total_pg > 5:
                            is_tabular = True
                            logger.info(
                                "Re-routing %s to table extraction (coord validation failed, %d pages)",
                                doc.file_name, total_pg,
                            )

                if is_coordinate_path and not _selector_produced_records:
                    try:
                        from app.pipeline.coordinate_extractor import CoordinateExtractor
                        from app.pipeline.reconciliation import ExtractionReconciler

                        # Load person name samples for mixed-case fallback (Gap 1)
                        _person_samples = doc_meta.get("person_samples") if doc_meta else None

                        coord_ext = CoordinateExtractor(
                            effective_field_map, doc.source_path, str(doc.id),
                            name_samples=_person_samples or None,
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

                # Quality gate: reject Path 0 if names are mostly garbage
                if records and extraction_path == "0-coord" and not _check_extraction_quality(records, "0-coord"):
                    records = []
                    extraction_path = ""

                # --- Path 1: Vision direct (small docs or scanned) ---
                # Respects vision routing: if recommended_path is "vision_direct",
                # or if no records yet and vision is available.
                # Skip vision for large docs with text blocks — Path 2 (text+LLM) is
                # much faster (~5s vs ~60s per batch) for text-extractable content.
                _has_text = len(blocks) > 0
                # Skip vision for docs with text blocks that are large OR where
                # coordinate extraction already failed (vision likely won't help either)
                _coord_failed = not is_coordinate_path and effective_field_map
                _too_large_for_vision = (_has_text and total_pg > 20) or _coord_failed
                if _too_large_for_vision and (is_template or is_tabular):
                    logger.info(
                        "Skipping Path 1 (Vision) for %s: %d pages with text blocks, "
                        "using text+LLM path instead",
                        doc.file_name, total_pg,
                    )
                if (
                    not records
                    and settings.use_vision_extraction
                    and settings.llm_assist_enabled
                    and doc.source_path
                    and (doc.file_type or "").lower() in ("pdf", ".pdf", "application/pdf")
                    and not _too_large_for_vision
                ):
                    try:
                        from app.llm.client import OllamaClient
                        from app.structure.vision_extractor import VisionDocumentExtractor
                        client = OllamaClient(db_session=db, timeout_s=300)
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

                # Quality gate: reject Path 1 if names are mostly garbage
                if records and extraction_path == "1" and not _check_extraction_quality(records, "1-vision"):
                    records = []
                    extraction_path = ""

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

                # Quality gate: reject Path 2a if names are mostly garbage
                if records and extraction_path == "2-table" and not _check_extraction_quality(records, "2-table"):
                    records = []
                    extraction_path = ""

                # --- Path 2b: Text + LLM template ---
                # Scale budget with doc size: min 100, max 500.
                # A 158-page doc with 6 records/page needs ~158 batches.
                _MAX_LLM_BATCHES = min(500, max(100, total_pg * 2))

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

                        total_batches_est = (len(instances) + batch_size - 1) // batch_size

                        if total_batches_est > _MAX_LLM_BATCHES:
                            # Learn-then-extract: LLM for first N instances, code for rest
                            logger.info(
                                "Path 2b: %d batches exceeds budget (%d). Using learn-then-extract: "
                                "LLM for first %d instances, code for remaining %d",
                                total_batches_est, _MAX_LLM_BATCHES,
                                _MAX_LLM_BATCHES * batch_size,
                                len(instances) - _MAX_LLM_BATCHES * batch_size,
                            )
                            learn_instances = instances[:_MAX_LLM_BATCHES * batch_size]
                            llm_records = text_extractor.extract_all_instances(
                                schema, page_texts, str(doc.id), total_pg,
                                active_anchors=dedup_anchors,
                                progress_callback=_heartbeat_cb,
                                instances=learn_instances,
                            )

                            # Phase 2: Learn name patterns from LLM results, extract remaining via regex
                            code_records: list[PIIRecord] = []
                            name_samples = [r.raw_name for r in llm_records if r.raw_name]
                            if name_samples:
                                try:
                                    from app.pipeline.coordinate_extractor import _learn_name_regex
                                    name_regex, _name_fmt = _learn_name_regex(name_samples)
                                    if name_regex:
                                        remaining_pages: set[int] = set()
                                        for inst in instances[_MAX_LLM_BATCHES * batch_size:]:
                                            remaining_pages.update(inst)
                                        for pg in sorted(remaining_pages):
                                            text = page_texts.get(pg, "")
                                            for line in text.split("\n"):
                                                line_s = line.strip()
                                                if len(line_s) < 5 or len(line_s) > 60:
                                                    continue
                                                m = name_regex.search(line_s)
                                                if m and _is_likely_name(m.group()):
                                                    from uuid import uuid4
                                                    rec = PIIRecord(
                                                        record_id=str(uuid4()),
                                                        document_id=str(doc.id),
                                                        page_range=str(pg + 1),
                                                        raw_name=m.group().strip(),
                                                        entity_types_found=["PERSON"],
                                                    )
                                                    code_records.append(rec)
                                                    break  # one name per page
                                        logger.info(
                                            "Learn-then-extract: %d LLM records + %d code records",
                                            len(llm_records), len(code_records),
                                        )
                                except Exception:
                                    logger.warning("Learn-then-extract code phase failed, using LLM records only", exc_info=True)

                            llm_records.extend(code_records)
                            if llm_records:
                                records = llm_records
                                extraction_path = "2-hybrid"
                        else:
                            # Normal Path 2b — LLM all instances (fits within budget)
                            llm_records = text_extractor.extract_all_instances(
                                schema, page_texts, str(doc.id), total_pg,
                                active_anchors=dedup_anchors,
                                progress_callback=_heartbeat_cb,
                            )
                            if llm_records:
                                records = llm_records
                                extraction_path = "2"

                        if records:
                            logger.info("Path 2b (%s) for %s: %d records", extraction_path, doc.file_name, len(records))
                    except Exception:
                        logger.warning("Path 2 (Text+LLM) failed for %s, falling back to Path 3", doc.file_name, exc_info=True)

                # Quality gate: reject Path 2b if names are mostly garbage
                if records and extraction_path in ("2", "2-hybrid") and not _check_extraction_quality(records, "2-template"):
                    records = []
                    extraction_path = ""

                # Density guard: if template extraction yields very few records
                # relative to the page count, the schema likely misclassified a
                # tabular doc as a template.  Re-try as table extraction.
                if (
                    records
                    and extraction_path in ("2", "2-hybrid")
                    and total_pg >= 20
                    and len(records) < total_pg * 0.3
                    and settings.llm_assist_enabled
                    and schema
                ):
                    logger.info(
                        "Density guard: Path 2b yielded %d records for %d pages (%.1f rec/page) "
                        "in %s. Re-trying as table extraction.",
                        len(records), total_pg, len(records) / total_pg, doc.file_name,
                    )
                    try:
                        from app.llm.client import OllamaClient
                        from app.structure.llm_template_extractor import LLMTemplateExtractor
                        page_texts_retry: dict[int, str] = {}
                        for b in blocks:
                            pg = b.page_or_sheet
                            if pg not in page_texts_retry:
                                page_texts_retry[pg] = ""
                            page_texts_retry[pg] += b.text + "\n"
                        client = OllamaClient(db_session=db, timeout_s=120)
                        retry_extractor = LLMTemplateExtractor(client, batch_size=batch_size)
                        table_retry_records = retry_extractor.extract_table_pages(
                            schema, page_texts_retry, str(doc.id),
                            progress_callback=_heartbeat_cb,
                        )
                        if table_retry_records and len(table_retry_records) > len(records) * 1.5:
                            logger.info(
                                "Table re-extraction improved %s: %d→%d records",
                                doc.file_name, len(records), len(table_retry_records),
                            )
                            records = table_retry_records
                            extraction_path = "2-table-retry"
                    except Exception:
                        logger.warning(
                            "Table re-extraction failed for %s, keeping template results",
                            doc.file_name, exc_info=True,
                        )

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
                        from app.pipeline.smart_grouping import group_detections_to_records as _smart_group
                        records = _smart_group(
                            detections, str(doc.id),
                            schema=schema, doc_path=doc.source_path,
                        )
                    extraction_path = "3"
                    logger.info("Path 3 (Presidio) for %s: %d records (from %d detections)", doc.file_name, len(records), len(detections))

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
                            # Null out the bad name but keep record if it has
                            # any other identifying info (gov ID, email, phone)
                            if rec.raw_government_id or rec.raw_email or rec.raw_phone:
                                object.__setattr__(rec, "raw_name", None)
                                object.__setattr__(rec, "normalized_value",
                                    rec.raw_government_id or rec.raw_email or rec.raw_phone)
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

                # --- B2/B3: Email sender + label deny list filters ---
                if records:
                    _before_fp = len(records)
                    _fp_filtered: list[PIIRecord] = []
                    for rec in records:
                        _drop = False

                        # B3: Label deny list — reject "PERSON" names that are labels
                        if rec.raw_name:
                            try:
                                from app.pii.context_deny_list import is_label_as_person
                                _is_label, _label_reason = is_label_as_person(rec.raw_name)
                                if _is_label:
                                    if rec.raw_government_id:
                                        # Keep record but remove the fake name
                                        object.__setattr__(rec, "raw_name", None)
                                        object.__setattr__(rec, "normalized_value", rec.raw_government_id)
                                    else:
                                        _drop = True
                                        continue
                            except ImportError:
                                pass

                        # B2: Email sender context — skip records near "From:" etc.
                        # Only applies to MSG/EML-sourced docs or docs with email-like text.
                        # IMPORTANT: Do NOT drop records that have corroborating PII
                        # (email, phone, DOB, address) — those are real subjects, not noise.
                        if rec.raw_name and rec.entity_role in (None, "unknown"):
                            _has_corroborating = bool(
                                rec.raw_email or rec.raw_phone or rec.raw_dob
                                or rec.raw_address or rec.raw_government_id
                            )
                            if not _has_corroborating:
                                try:
                                    from app.pii.context_deny_list import is_email_sender_context
                                    # Build a context window from blocks on the same page
                                    _page_key = rec.page_or_sheet
                                    _ctx_blocks = [b for b in blocks if b.page_or_sheet == _page_key]
                                    _ctx_text = " ".join(b.text for b in _ctx_blocks[:5])  # first 5 blocks
                                    _is_sender, _sender_reason = is_email_sender_context(
                                        rec.raw_name, "PERSON", _ctx_text,
                                    )
                                    if _is_sender:
                                        if rec.raw_government_id:
                                            object.__setattr__(rec, "raw_name", None)
                                            object.__setattr__(rec, "normalized_value", rec.raw_government_id)
                                        else:
                                            _drop = True
                                            continue
                                except ImportError:
                                    pass

                        if not _drop:
                            _fp_filtered.append(rec)

                    if len(_fp_filtered) < _before_fp:
                        logger.info(
                            "FP filters (B2+B3): %d → %d records for %s",
                            _before_fp, len(_fp_filtered), doc.file_name,
                        )
                    records = _fp_filtered

                # --- B1: ValueFrequencyFilter — suppress org metadata ---
                if records and total_pg >= 5:
                    try:
                        from app.pii.schema_filter import ValueFrequencyFilter

                        # Adapt PIIRecords to the interface VFF expects
                        # (pii_type, evidence_page, detected_text)
                        class _VFFAdapter:
                            __slots__ = ("pii_type", "evidence_page", "detected_text", "hashed_value")
                            def __init__(self, pt: str, pg: int | str, txt: str):
                                self.pii_type = pt
                                self.evidence_page = pg
                                self.detected_text = txt
                                self.hashed_value = None

                        _vff_items: list = []
                        for _r in records:
                            _pg = _r.page_or_sheet
                            if _r.raw_name:
                                _vff_items.append(_VFFAdapter("PERSON", _pg, _r.raw_name))
                            if _r.raw_phone:
                                _vff_items.append(_VFFAdapter("PHONE_NUMBER", _pg, _r.raw_phone))
                            if _r.raw_email:
                                _vff_items.append(_VFFAdapter("EMAIL_ADDRESS", _pg, _r.raw_email))

                        vff = ValueFrequencyFilter.from_extractions(_vff_items, total_pg)
                        if vff.flagged_values:
                            _before_vff = len(records)
                            _vff_kept: list[PIIRecord] = []
                            for rec in records:
                                _suppressed = False
                                for field_name, pii_type in [
                                    ("raw_name", "PERSON"),
                                    ("raw_phone", "PHONE_NUMBER"),
                                    ("raw_email", "EMAIL_ADDRESS"),
                                ]:
                                    val = getattr(rec, field_name, None)
                                    if val:
                                        _is_org, _ = vff.is_org_metadata(val, pii_type)
                                        if _is_org:
                                            object.__setattr__(rec, field_name, None)
                                            _suppressed = True
                                # Drop record if it lost all identifying info
                                if not (rec.raw_name or rec.raw_government_id or rec.raw_email):
                                    continue
                                _vff_kept.append(rec)

                            if len(_vff_kept) < _before_vff:
                                logger.info(
                                    "ValueFrequencyFilter (B1): %d → %d records for %s",
                                    _before_vff, len(_vff_kept), doc.file_name,
                                )
                            records = _vff_kept
                    except ImportError:
                        pass
                    except Exception:
                        logger.debug("ValueFrequencyFilter failed for %s", doc.file_name, exc_info=True)

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

                # --- Post-extraction audit (all paths) ---
                # Coordinate text audit: verify values exist in source
                audit_result = None
                if (
                    records
                    and doc.source_path
                    and (doc.file_type or "").lower() in ("pdf", ".pdf", "application/pdf")
                ):
                    try:
                        from app.pipeline.extraction_verifier import (
                            ExtractionVerifier,
                            records_to_page_dict,
                        )
                        verifier = ExtractionVerifier()
                        page_recs_audit = records_to_page_dict(records)
                        if page_recs_audit:
                            audit_result = verifier.verify_by_coordinates(
                                doc.source_path, page_recs_audit, sample_size=15,
                            )
                            logger.info(
                                "Post-extraction audit (%s, Path %s): %s (%d%% confidence, %d%% consistency)",
                                doc.file_name, extraction_path,
                                audit_result.audit_status, audit_result.audit_confidence,
                                audit_result.audit_consistency,
                            )
                    except Exception:
                        logger.debug("Post-extraction audit failed for %s", doc.file_name, exc_info=True)

                # NOTE: Vision gap-fill moved to post-extraction stage (after
                # all docs complete) to avoid blocking the extraction loop.
                # See "Post-extraction gap analysis" below the for-loop.

                # Store audit metrics
                audit_detail: dict = {"extraction_path": extraction_path, "records": len(records), "total": len(approved_docs), "current": i, "status": "running"}
                if audit_result:
                    audit_detail["audit_status"] = audit_result.audit_status
                    audit_detail["audit_confidence"] = audit_result.audit_confidence
                    audit_detail["audit_consistency"] = audit_result.audit_consistency
                    audit_detail["pages_audited"] = audit_result.pages_audited

                completed_doc_ids.append(str(doc.id))
                _doc_elapsed = time.time() - _doc_start
                logger.info(
                    "[%d/%d] DONE: %s | path=%s | records=%d | time=%.1fs | audit=%s",
                    i, len(approved_docs), doc.file_name,
                    extraction_path, len(records), _doc_elapsed,
                    audit_result.audit_status if audit_result else "n/a",
                )
                audit_detail["elapsed_s"] = round(_doc_elapsed, 1)
                _update_extraction_progress(
                    db, run,
                    stage="detection",
                    message=f"Extracted {len(records)} record(s) from {doc.file_name} (Path {extraction_path}, {_doc_elapsed:.0f}s)",
                    completed_doc_ids=completed_doc_ids,
                    total_docs=len(approved_docs), current_doc=i,
                    records_found=len(all_records),
                    detail=audit_detail,
                )

            except (Exception, MemoryError) as e:
                # A7: Robust per-doc error handling — NEVER let one doc kill the queue
                _doc_elapsed = time.time() - _doc_start
                _is_timeout = isinstance(e, _DocTimeoutError)
                _is_oom = isinstance(e, MemoryError)
                _severity = "ERROR" if (_is_timeout or _is_oom) else "WARNING"
                logger.log(
                    logging.ERROR if _severity == "ERROR" else logging.WARNING,
                    "[%d/%d] FAILED: %s | error=%s | time=%.1fs | records_so_far=%d | timeout=%s | oom=%s",
                    i, len(approved_docs), doc.file_name,
                    type(e).__name__, _doc_elapsed, len(records),
                    _is_timeout, _is_oom,
                    exc_info=True,
                )
                # Track failed docs for end-of-run summary
                _failed_docs.append({
                    "file_name": doc.file_name,
                    "doc_id": str(doc.id),
                    "error": type(e).__name__,
                    "elapsed_s": round(_doc_elapsed, 1),
                    "records_before_fail": len(records),
                    "is_timeout": _is_timeout,
                    "is_oom": _is_oom,
                })
                # Save partial results + mark status appropriately
                try:
                    _partial_meta = dict(doc.metadata_json or {})
                    if records:
                        _partial_meta["extracted_records"] = [_serialize_pii_record(r) for r in records]
                        _partial_meta["extraction_status"] = "partial"
                        _partial_meta["last_record_count"] = len(records)
                        all_records.extend(records)
                        completed_doc_ids.append(str(doc.id))
                    else:
                        _partial_meta["extraction_status"] = "failed"
                    _partial_meta["extraction_path_used"] = extraction_path
                    _partial_meta["extraction_error"] = type(e).__name__
                    _partial_meta["extraction_error_time"] = _doc_elapsed
                    doc.metadata_json = _partial_meta
                    flag_modified(doc, "metadata_json")
                    db.commit()
                    logger.info(
                        "[%d/%d] %s: %s | %d records saved | continuing to next doc",
                        i, len(approved_docs),
                        "PARTIAL" if records else "FAILED",
                        doc.file_name, len(records),
                    )
                except Exception:
                    # DB save failed too — rollback and move on
                    try:
                        db.rollback()
                    except Exception:
                        pass
                # Update SSE so UI shows the failure
                try:
                    _update_extraction_progress(
                        db, run,
                        stage="detection",
                        message=f"{'Timed out' if _is_timeout else 'Failed'}: {doc.file_name} ({type(e).__name__}) — continuing...",
                        completed_doc_ids=completed_doc_ids,
                        total_docs=len(approved_docs), current_doc=i,
                        records_found=len(all_records),
                        detail={"total": len(approved_docs), "current": i,
                                "status": "doc_failed", "doc_name": doc.file_name,
                                "error": type(e).__name__},
                    )
                except Exception:
                    pass
                continue  # A7: ALWAYS continue to next document

        # --- End-of-extraction summary (A7) ---
        if _failed_docs:
            logger.warning(
                "EXTRACTION SUMMARY: %d/%d docs failed | failures: %s",
                len(_failed_docs), len(approved_docs),
                ", ".join(f"{d['file_name']}({d['error']}, {d['elapsed_s']}s)" for d in _failed_docs),
            )
            # Persist failure summary in run metrics for API access
            try:
                _metrics = dict(run.metrics or {})
                _metrics["failed_docs"] = _failed_docs
                _metrics["docs_succeeded"] = len(approved_docs) - len(_failed_docs)
                _metrics["docs_failed"] = len(_failed_docs)
                run.metrics = _metrics
                flag_modified(run, "metrics")
                db.commit()
            except Exception:
                pass
        else:
            logger.info(
                "EXTRACTION SUMMARY: %d/%d docs succeeded | %d total records",
                len(approved_docs), len(approved_docs), len(all_records),
            )

        if _is_cancelled():
            return

        # --- Retry partial docs (DLQ) ---
        # Docs that stalled or failed with partial results get one more try
        # using the same extraction strategy, resuming from existing records.
        partial_docs = [
            d for d in approved_docs
            if (d.metadata_json or {}).get("extraction_status") == "partial"
        ]
        if partial_docs:
            logger.info("Retrying %d partial doc(s)...", len(partial_docs))
            _update_extraction_progress(
                db, run, stage="detection",
                message=f"Retrying {len(partial_docs)} partial document(s)...",
                completed_doc_ids=completed_doc_ids,
                total_docs=len(approved_docs), current_doc=len(approved_docs),
                records_found=len(all_records),
            )

            for pdoc in partial_docs:
                if _is_cancelled():
                    break
                _retry_start = time.time()
                _pmeta = pdoc.metadata_json or {}
                _orig_path = _pmeta.get("extraction_path_used", "3")

                try:
                    # Load existing partial records
                    existing_records = []
                    for rd in _pmeta.get("extracted_records", []):
                        try:
                            existing_records.append(_deserialize_pii_record(rd))
                        except Exception:
                            pass

                    logger.info(
                        "RETRY: %s | original_path=%s | existing=%d records",
                        pdoc.file_name, _orig_path, len(existing_records),
                    )

                    # Load schema + blocks
                    retry_blocks = get_reader(pdoc.source_path).read()
                    if not retry_blocks:
                        continue

                    retry_schema = None
                    retry_schema_dict = _pmeta.get("document_schema")
                    if retry_schema_dict:
                        try:
                            from app.structure.document_schema import DocumentSchema as _RetryDS
                            retry_schema = _RetryDS.from_dict(retry_schema_dict)
                        except Exception:
                            pass

                    retry_records: list[PIIRecord] = []

                    # --- Retry using ORIGINAL extraction method ---
                    if _orig_path.startswith("0") and retry_schema:
                        # Coordinate extraction
                        try:
                            from app.pipeline.coordinate_extractor import CoordinateExtractor as _RetryCoord
                            fm = getattr(retry_schema, "layout_field_map", None)
                            vision_fm = _pmeta.get("vision_field_map")
                            if not fm and vision_fm:
                                from app.structure.document_schema import FieldMapping as _RetryFM
                                fm = [_RetryFM(**f) for f in vision_fm]
                            if fm:
                                ext = _RetryCoord(fm, pdoc.source_path, str(pdoc.id),
                                                  name_samples=_pmeta.get("person_samples"))
                                retry_records, _ = ext.extract_all_pages()
                                logger.info("RETRY coordinate: %d records", len(retry_records))
                        except Exception:
                            logger.warning("RETRY coordinate failed for %s", pdoc.file_name, exc_info=True)

                    elif _orig_path.startswith("2") and settings.llm_assist_enabled:
                        # LLM template/table extraction
                        try:
                            from app.llm.client import OllamaClient as _RetryOllama
                            from app.structure.llm_template_extractor import LLMTemplateExtractor as _RetryLLM

                            page_texts: dict[int, str] = {}
                            for b in retry_blocks:
                                pg = b.page_or_sheet
                                if pg not in page_texts:
                                    page_texts[pg] = ""
                                page_texts[pg] += b.text + "\n"

                            client = _RetryOllama(db_session=db, timeout_s=120)
                            extractor = _RetryLLM(client, batch_size=DEFAULT_EXTRACTION_BATCH_SIZE)
                            if retry_schema:
                                retry_records = extractor.extract_all_instances(
                                    retry_schema, page_texts, pdoc.source_path, len(page_texts),
                                )
                                logger.info("RETRY LLM: %d records", len(retry_records))
                        except Exception:
                            logger.warning("RETRY LLM failed for %s", pdoc.file_name, exc_info=True)

                    # --- Fallback: Presidio smart-group (if original method produced nothing) ---
                    if not retry_records:
                        try:
                            retry_dets = engine.analyze(retry_blocks, target_entity_types=target_entities)
                            if retry_schema and schema_filter_cls:
                                try:
                                    sf = schema_filter_cls(retry_schema)
                                    r = sf.filter_detections(retry_dets)
                                    retry_dets = r.kept
                                except Exception:
                                    pass
                            from app.pipeline.smart_grouping import group_detections_to_records as _retry_sg
                            retry_records = _retry_sg(
                                retry_dets, str(pdoc.id),
                                schema=retry_schema, doc_path=pdoc.source_path,
                            )
                            logger.info("RETRY Presidio fallback: %d records", len(retry_records))
                        except Exception:
                            logger.warning("RETRY Presidio failed for %s", pdoc.file_name, exc_info=True)

                    # --- Merge: existing + new (name dedup) ---
                    existing_names = {r.raw_name.lower().strip() for r in existing_records if r.raw_name}
                    merged = list(existing_records)
                    new_count = 0
                    for rr in retry_records:
                        if rr.raw_name and rr.raw_name.lower().strip() not in existing_names:
                            merged.append(rr)
                            existing_names.add(rr.raw_name.lower().strip())
                            new_count += 1
                        elif not rr.raw_name and (rr.raw_government_id or rr.raw_phone or rr.raw_email):
                            merged.append(rr)
                            new_count += 1

                    # Save merged results
                    _pmeta["extracted_records"] = [_serialize_pii_record(r) for r in merged]
                    _pmeta["extraction_status"] = "complete"
                    _pmeta["retry_added"] = new_count
                    _pmeta["retry_method"] = _orig_path if retry_records else "presidio_fallback"
                    pdoc.metadata_json = _pmeta
                    flag_modified(pdoc, "metadata_json")
                    all_records.extend(merged[len(existing_records):])
                    db.commit()

                    logger.info(
                        "RETRY OK: %s | method=%s | existing=%d + new=%d = %d | %.1fs",
                        pdoc.file_name, _pmeta.get("retry_method", "?"),
                        len(existing_records), new_count, len(merged),
                        time.time() - _retry_start,
                    )
                except Exception as retry_err:
                    logger.warning("RETRY FAILED: %s | %s", pdoc.file_name, type(retry_err).__name__)

        _update_extraction_progress(
            db, run,
            stage="detection",
            message=f"Detected {len(all_records)} PII record(s) across {len(approved_docs)} document(s)",
            completed_doc_ids=completed_doc_ids,
            total_docs=len(approved_docs), current_doc=len(approved_docs),
            records_found=len(all_records),
            detail={"records_found": len(all_records), "status": "complete"},
        )

        # --- Post-extraction gap analysis (deferred from per-doc loop) ---
        # Runs AFTER all docs are extracted, with a hard budget cap.
        _MAX_GAP_FILL_CALLS = 10  # 10 vision calls total — vision is slow (30s+/call)

        if settings.llm_assist_enabled and getattr(settings, "use_vision_extraction", False):
            try:
                from app.llm.client import OllamaClient
                from app.pipeline.extraction_verifier import ExtractionVerifier

                verifier = ExtractionVerifier()
                gap_budget_remaining = _MAX_GAP_FILL_CALLS

                # Collect gap analysis across all docs
                gap_plan: list[tuple] = []  # (doc, gap_pages, critical_count)
                for doc in approved_docs:
                    if not doc.source_path:
                        continue
                    if (doc.file_type or "").lower() not in ("pdf", ".pdf", "application/pdf"):
                        continue
                    doc_records = [r for r in all_records if r.source_document_id == str(doc.id)]
                    if not doc_records:
                        continue
                    try:
                        gap_pages = verifier.find_gap_pages(doc_records)
                    except Exception:
                        continue
                    if gap_pages:
                        critical_count = sum(
                            1
                            for _page_num, items in gap_pages.items()
                            for _rec_idx, missing in items
                            if "raw_government_id" in missing
                        )
                        gap_plan.append((doc, gap_pages, critical_count))

                # Sort by priority (most critical gaps first)
                gap_plan.sort(key=lambda x: -x[2])

                for doc, gap_pages, _priority in gap_plan:
                    if gap_budget_remaining <= 0:
                        break
                    pages_to_fill = min(len(gap_pages), gap_budget_remaining, 10)
                    gap_budget_remaining -= pages_to_fill

                    gap_client = OllamaClient(db_session=db, timeout_s=300)
                    _ev_vision_model = settings.ollama_vision_model
                    if gap_client.is_vision_available(model_override=_ev_vision_model):
                        try:
                            doc_records = [r for r in all_records if r.source_document_id == str(doc.id)]
                            updated_records, gf_result = verifier.vision_gap_fill(
                                records=doc_records,
                                doc_path=doc.source_path,
                                doc_id=str(doc.id),
                                ollama_client=gap_client,
                                vision_model=_ev_vision_model,
                                max_pages=pages_to_fill,
                            )
                            # Replace records in all_records
                            for j, r in enumerate(all_records):
                                if r.source_document_id == str(doc.id):
                                    for ur in updated_records:
                                        if getattr(ur, "page_range", None) == getattr(r, "page_range", None) and ur.raw_name == r.raw_name:
                                            all_records[j] = ur
                                            break
                            logger.info(
                                "Post-extraction gap-fill for %s: %d/%d pages, budget remaining: %d",
                                doc.file_name, gf_result.gap_fill_succeeded,
                                pages_to_fill, gap_budget_remaining,
                            )
                        except Exception:
                            logger.warning("Post-extraction gap-fill failed for %s", doc.file_name, exc_info=True)

                gap_calls_used = _MAX_GAP_FILL_CALLS - gap_budget_remaining
                _update_extraction_progress(
                    db, run, stage="gap_fill",
                    message=f"Gap-fill complete: {gap_calls_used} vision calls used",
                    completed_doc_ids=completed_doc_ids,
                    total_docs=len(approved_docs), current_doc=len(approved_docs),
                    records_found=len(all_records),
                    detail={"gap_fill_calls": gap_calls_used, "status": "complete"},
                )
            except Exception:
                logger.warning("Post-extraction gap analysis failed", exc_info=True)

        # --- Stage 1.4: LLM Record Validation ---
        # Validates extracted records using LLM context awareness.  Purges
        # garbage (form codes, legal entities, empty names) so those pages
        # appear as gaps and trigger vision fallback naturally.
        if all_records and settings.llm_assist_enabled:
            try:
                from app.pipeline.record_validator import validate_records as _validate_records

                _update_extraction_progress(
                    db, run, stage="record_validation",
                    message="Validating extracted records...",
                    completed_doc_ids=completed_doc_ids,
                    total_docs=len(approved_docs), current_doc=len(approved_docs),
                    records_found=len(all_records),
                    detail={"status": "running"},
                )

                # Group records by document for per-doc validation
                from collections import defaultdict as _rv_dd
                _rv_by_doc: dict[str, list] = _rv_dd(list)
                for _rv_r in all_records:
                    _rv_by_doc[_rv_r.source_document_id].append(_rv_r)

                _total_purged = 0
                _validated_records: list = []
                for _rv_doc_key, _rv_doc_records in _rv_by_doc.items():
                    # Find the document for type info
                    _rv_doc = next(
                        (d for d in approved_docs
                         if str(d.id) == _rv_doc_key or d.source_path == _rv_doc_key),
                        None,
                    )
                    _rv_doc_type = "unknown"
                    _rv_doc_name = _rv_doc_key
                    if _rv_doc:
                        _rv_seg = (dict(_rv_doc.metadata_json or {})).get("segregation", {})
                        _rv_doc_type = _rv_seg.get("document_type", "unknown") if isinstance(_rv_seg, dict) else "unknown"
                        _rv_doc_name = _rv_doc.file_name or _rv_doc_key

                    try:
                        from app.llm.client import OllamaClient as _RVOllama
                        _rv_client = _RVOllama(db_session=db, timeout_s=300)
                        _rv_valid, _rv_purged, _rv_stats = _validate_records(
                            records=_rv_doc_records,
                            document_type=_rv_doc_type,
                            document_name=_rv_doc_name,
                            ollama_client=_rv_client,
                            doc_id=str(_rv_doc.id) if _rv_doc else None,
                        )
                        _validated_records.extend(_rv_valid)
                        _total_purged += len(_rv_purged)
                    except Exception:
                        logger.debug("Record validation failed for %s", _rv_doc_name, exc_info=True)
                        _validated_records.extend(_rv_doc_records)

                if _total_purged > 0:
                    logger.info(
                        "Record validation: purged %d/%d garbage records across %d docs",
                        _total_purged, len(all_records), len(_rv_by_doc),
                    )
                    all_records = _validated_records

                _update_extraction_progress(
                    db, run, stage="record_validation",
                    message=f"Validated {len(all_records)} records ({_total_purged} purged)",
                    completed_doc_ids=completed_doc_ids,
                    total_docs=len(approved_docs), current_doc=len(approved_docs),
                    records_found=len(all_records),
                    detail={"purged": _total_purged, "status": "complete"},
                )
            except ImportError:
                logger.info("Record validator not available — skipping")
            except Exception:
                logger.warning("Record validation failed — keeping all records", exc_info=True)

        # --- Stage 1.45: Completeness-driven vision recovery ---
        # If unique subjects found << expected, get a name roster from summary
        # pages and vision-extract specific pages to find missing people.
        if all_records and settings.llm_assist_enabled:
            try:
                from app.pipeline.completeness_checker import check_completeness_and_recover

                _update_extraction_progress(
                    db, run, stage="completeness_check",
                    message="Checking extraction completeness...",
                    completed_doc_ids=completed_doc_ids,
                    total_docs=len(approved_docs), current_doc=len(approved_docs),
                    records_found=len(all_records),
                    detail={"status": "running"},
                )

                _pre_completeness_count = len(all_records)
                for _cc_doc in approved_docs:
                    # Get records for this doc
                    _cc_doc_id = str(_cc_doc.id)
                    _cc_doc_path = _cc_doc.source_path or ""
                    _cc_doc_records = [
                        r for r in all_records
                        if r.source_document_id == _cc_doc_id
                        or r.source_document_id == _cc_doc_path
                    ]
                    if not _cc_doc_records:
                        continue

                    try:
                        from app.llm.client import OllamaClient as _CCOllama
                        _cc_client = _CCOllama(db_session=db, timeout_s=300)
                        _cc_result = check_completeness_and_recover(
                            records=_cc_doc_records,
                            doc=_cc_doc,
                            ollama_client=_cc_client,
                            settings=settings,
                            db_session=db,
                        )
                        # Replace this doc's records with recovered set
                        if len(_cc_result) > len(_cc_doc_records):
                            _new_records = [
                                r for r in all_records
                                if r.source_document_id != _cc_doc_id
                                and r.source_document_id != _cc_doc_path
                            ]
                            _new_records.extend(_cc_result)
                            all_records = _new_records
                    except Exception:
                        logger.debug(
                            "Completeness check failed for %s", _cc_doc.file_name,
                            exc_info=True,
                        )

                _recovered = len(all_records) - _pre_completeness_count
                if _recovered > 0:
                    logger.info(
                        "Completeness recovery: %d additional records across %d docs",
                        _recovered, len(approved_docs),
                    )

                _update_extraction_progress(
                    db, run, stage="completeness_check",
                    message=f"Completeness check done ({_recovered} recovered)" if _recovered else "Completeness check done",
                    completed_doc_ids=completed_doc_ids,
                    total_docs=len(approved_docs), current_doc=len(approved_docs),
                    records_found=len(all_records),
                    detail={"recovered": _recovered, "status": "complete"},
                )
            except ImportError:
                logger.info("Completeness checker not available — skipping")
            except Exception:
                logger.warning("Completeness check failed", exc_info=True)

        # --- Stage 1.5: GapDetector + GapFiller (4-path cascade) ---
        # Runs after ExtractionVerifier vision gap-fill.  Detects page/field/
        # truncation gaps and attempts auto-fill through coordinate → LLM →
        # vision → Presidio cascade.  Results persisted to JSON for QA screen.
        try:
            from app.pipeline.gap_detector import GapDetector
            from app.pipeline.gap_filler import GapFiller, persist_gaps

            _gap_detector = GapDetector()
            all_detected_gaps: list = []

            _update_extraction_progress(
                db, run, stage="gap_detection",
                message="Running automated gap detection...",
                completed_doc_ids=completed_doc_ids,
                total_docs=len(approved_docs), current_doc=len(approved_docs),
                records_found=len(all_records),
                detail={"status": "running"},
            )

            for gdoc in approved_docs:
                if not gdoc.source_path:
                    continue
                gdoc_meta = dict(gdoc.metadata_json or {})

                # Get field_inventory from segregation or document schema
                field_inv: list[str] = []
                seg_data = gdoc_meta.get("segregation", {})
                if isinstance(seg_data, dict):
                    field_inv = seg_data.get("field_inventory", [])
                if not field_inv:
                    # Fallback: from document_schema
                    schema_data = gdoc_meta.get("document_schema", {})
                    if isinstance(schema_data, dict):
                        field_inv = [
                            f.get("field_type", "")
                            for f in schema_data.get("field_mappings", [])
                            if f.get("field_type")
                        ]

                if not field_inv:
                    continue  # No expectations → no gaps to detect

                # Serialize records for this document — match by UUID or file path
                # (selector/Presidio path uses source_path, Strategy A/B uses doc UUID)
                _gdoc_id_str = str(gdoc.id)
                _gdoc_path = gdoc.source_path or ""
                doc_records_for_gap = [
                    _serialize_pii_record(r)
                    for r in all_records
                    if r.source_document_id == _gdoc_id_str
                    or r.source_document_id == _gdoc_path
                ]
                if not doc_records_for_gap:
                    continue

                # Determine onset + total pages
                onset = gdoc.sample_onset_page or 0
                total_pg = gdoc_meta.get("page_count", 0)
                if not total_pg and (gdoc.file_type or "").lower() in ("pdf", ".pdf", "application/pdf"):
                    try:
                        import fitz as _gfitz
                        _gdoc = _gfitz.open(gdoc.source_path)
                        total_pg = _gdoc.page_count
                        _gdoc.close()
                    except Exception:
                        total_pg = max(
                            (int(r.get("page_range", "0").split("-")[0]) for r in doc_records_for_gap),
                            default=0,
                        )

                if total_pg <= 0:
                    continue

                # Determine which pages have content (skip blank pages)
                _content_pages: set[int] | None = None
                if gdoc.source_path and (gdoc.file_type or "").lower() in ("pdf", ".pdf", "application/pdf"):
                    try:
                        import fitz as _cpfitz
                        _cpdoc = _cpfitz.open(gdoc.source_path)
                        _content_pages = set()
                        for _cpg in range(_cpdoc.page_count):
                            if len(_cpdoc[_cpg].get_text().strip()) > 5:
                                _content_pages.add(_cpg)
                            _cpdoc._forget_page(_cpg)
                        _cpdoc.close()
                    except Exception:
                        pass

                doc_gaps = _gap_detector.detect(
                    records=doc_records_for_gap,
                    field_inventory=field_inv,
                    total_pages=total_pg,
                    onset_page=onset,
                    document_id=str(gdoc.id),
                    document_name=gdoc.file_name or gdoc.source_path,
                    content_pages=_content_pages,
                )
                all_detected_gaps.extend(doc_gaps)

            # Attempt auto-fill on detected gaps (per-document)
            if all_detected_gaps and settings.llm_assist_enabled:
                # Scale LLM budget with gap count and document size
                # For 3000-page docs with 500+ gaps, need proportional budget.
                # Budget ≈ gaps × 0.6 (batched fills handle ~5 gaps per call),
                # floored at 50, capped at 500.
                _gap_budget = min(500, max(50, int(len(all_detected_gaps) * 0.6)))

                # Group gaps by document
                from collections import defaultdict as _dd
                gaps_by_doc: dict[str, list] = _dd(list)
                for gap in all_detected_gaps:
                    gaps_by_doc[gap.document_id].append(gap)

                filled_gaps: list = []
                for doc_id, doc_gaps in gaps_by_doc.items():
                    # Find the document
                    _fill_doc = next((d for d in approved_docs if str(d.id) == doc_id), None)
                    if not _fill_doc or not _fill_doc.source_path:
                        filled_gaps.extend(doc_gaps)
                        continue

                    _fill_meta = dict(_fill_doc.metadata_json or {})
                    _fill_fm = _fill_meta.get("vision_field_map") or _fill_meta.get("coordinate_field_map")
                    field_map_objs = None
                    if _fill_fm:
                        try:
                            from app.structure.document_schema import FieldMapping as _GapFM
                            field_map_objs = [_GapFM(**f) if isinstance(f, dict) else f for f in _fill_fm]
                        except Exception:
                            pass

                    ollama_client = None
                    try:
                        from app.llm.client import OllamaClient as _GapOllama
                        ollama_client = _GapOllama(db_session=db, timeout_s=120)
                        logger.info("Gap fill: OllamaClient created for doc %s", doc_id)
                    except Exception:
                        logger.warning("Gap fill: OllamaClient creation FAILED for doc %s", doc_id, exc_info=True)

                    # Resolve vision model for gap filling
                    _gap_vision_model = None
                    try:
                        _gap_vision_model = settings.ollama_vision_model
                        if protocol_config and isinstance(protocol_config, dict):
                            _gap_vision_model = protocol_config.get("vision_model", _gap_vision_model)
                    except Exception:
                        pass

                    filler = GapFiller(
                        doc_path=_fill_doc.source_path,
                        document_id=doc_id,
                        field_map=field_map_objs,
                        ollama_client=ollama_client,
                        vision_model=_gap_vision_model,
                        max_llm_total=_gap_budget,
                    )
                    try:
                        doc_filled = filler.fill(doc_gaps)
                        filled_gaps.extend(doc_filled)
                    except Exception:
                        logger.warning("Gap fill failed for doc %s", doc_id, exc_info=True)
                        filled_gaps.extend(doc_gaps)  # keep as unfilled

                all_detected_gaps = filled_gaps

            # Persist to disk for QA screen
            _gap_project_id = str(run.project_id) if run.project_id else "default"
            _gap_job_id = str(run.id) if run.id else "unknown"
            persist_gaps(all_detected_gaps, _gap_project_id, _gap_job_id)

            _gap_filled = sum(1 for g in all_detected_gaps if g.fill_result == "filled")
            _gap_unfilled = sum(1 for g in all_detected_gaps if g.fill_result == "unfilled")
            _update_extraction_progress(
                db, run, stage="gap_detection",
                message=f"Gap detection complete: {len(all_detected_gaps)} gaps ({_gap_filled} filled, {_gap_unfilled} unfilled)",
                completed_doc_ids=completed_doc_ids,
                total_docs=len(approved_docs), current_doc=len(approved_docs),
                records_found=len(all_records),
                detail={
                    "total_gaps": len(all_detected_gaps),
                    "filled": _gap_filled,
                    "unfilled": _gap_unfilled,
                    "status": "complete",
                },
            )
            logger.info(
                "Gap detection + fill: %d total, %d filled, %d unfilled",
                len(all_detected_gaps), _gap_filled, _gap_unfilled,
            )
        except ImportError:
            logger.info("Gap detector/filler not available — skipping")
        except Exception:
            logger.warning("Gap detection + fill failed", exc_info=True)

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
            if subj.ingestion_run_id is None:
                subj.ingestion_run_id = run.id

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
                # Dedup summary (#5)
                "total_records": len(all_records),
                "total_documents": len(approved_docs),
                "duplicates_removed": max(0, len(all_records) - len(subjects)),
                "flagged_for_review": review_count,
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


# ---------------------------------------------------------------------------
# Analysis background thread + SSE relay (same pattern as extraction)
# ---------------------------------------------------------------------------

_analysis_threads: dict[str, threading.Thread] = {}


def run_analysis_background(body, registry: ProtocolRegistry) -> None:
    """Run analyze_generator in a background thread.

    Captures each SSE event and writes it to IngestionRun.metrics["analysis_progress"].
    The SSE relay polls this and forwards to the browser.

    IMPORTANT: The generator's internal session may be closed/replaced by
    _refresh_session().  This wrapper must use its OWN short-lived sessions
    for heartbeat writes so it never touches the generator's session.
    """
    from app.api.deps import _get_session_factory

    SessionFactory = _get_session_factory()
    # The generator gets its own session that it manages internally
    gen_db = SessionFactory()
    job_uuid = None

    def _write_heartbeat(event: dict) -> None:
        """Write progress to IngestionRun.metrics using a fresh session."""
        if job_uuid is None:
            return
        hb_db = SessionFactory()
        try:
            run = hb_db.get(IngestionRun, job_uuid)
            if run is None:
                return
            metrics = dict(run.metrics or {})
            metrics["analysis_progress"] = {
                "stage": event.get("stage", ""),
                "message": event.get("message", ""),
                "status": event.get("status", "running"),
                "detail": event.get("detail", {}),
                "result": event.get("result"),
                "heartbeat": datetime.now(timezone.utc).isoformat(),
            }
            run.metrics = metrics
            flag_modified(run, "metrics")
            hb_db.commit()
        except Exception:
            try:
                hb_db.rollback()
            except Exception:
                pass
        finally:
            hb_db.close()

    def _update_run_status(status: str, error: str | None = None) -> None:
        """Update IngestionRun status using a fresh session."""
        if job_uuid is None:
            return
        st_db = SessionFactory()
        try:
            run = st_db.get(IngestionRun, job_uuid)
            if run is None:
                return
            if status == "analyzed":
                if run.status != "running":
                    return
                run.status = "analyzed"
                run.analysis_completed_at = datetime.now(timezone.utc)
            elif status == "failed":
                run.status = "failed"
                run.error_summary = error
            st_db.commit()
            logger.info("Analysis background: set job %s → %s", str(job_uuid)[:8], status)
        except Exception:
            try:
                st_db.rollback()
            except Exception:
                pass
        finally:
            st_db.close()

    try:
        gen = analyze_generator(body, gen_db, registry)

        for sse_line in gen:
            # Parse SSE event
            if not sse_line.startswith("data: "):
                continue
            try:
                event = json.loads(sse_line[6:].strip())
            except (json.JSONDecodeError, ValueError):
                continue

            # Find the run UUID from the first events
            if job_uuid is None:
                find_db = SessionFactory()
                try:
                    run = find_db.execute(
                        select(IngestionRun)
                        .where(IngestionRun.pipeline_mode == "two_phase")
                        .order_by(IngestionRun.created_at.desc())
                    ).scalars().first()
                    if run:
                        job_uuid = run.id
                finally:
                    find_db.close()

            _write_heartbeat(event)

        # Generator finished successfully
        _update_run_status("analyzed")

    except Exception as exc:
        logger.error("Analysis background thread failed: %s", type(exc).__name__, exc_info=True)
        _update_run_status("failed", error=type(exc).__name__)
    finally:
        try:
            gen_db.close()
        except Exception:
            pass


def analysis_relay_generator(
    job_id: str,
    body,
    registry: ProtocolRegistry,
) -> Generator[str, None, None]:
    """SSE relay for analysis — starts background thread, polls progress.

    Survives browser disconnects because the analysis runs in a background
    thread. The relay just polls IngestionRun.metrics["analysis_progress"].
    """
    from app.api.deps import _get_session_factory

    db = _get_session_factory()()

    try:
        job_uuid = UUID(job_id)
    except (ValueError, AttributeError):
        yield _sse({"stage": "error", "message": f"Invalid job_id: {job_id!r}"})
        db.close()
        return

    run = db.execute(
        select(IngestionRun).where(IngestionRun.id == job_uuid)
    ).scalar_one_or_none()

    if run is None:
        yield _sse({"stage": "error", "message": f"Job {job_id!r} not found"})
        db.close()
        return

    # Start background thread if not already running
    existing = _analysis_threads.get(job_id)
    if existing is None or not existing.is_alive():
        if run.status in ("pending", "running"):
            logger.info("Launching analysis background thread for %s", job_id[:8])
            t = threading.Thread(
                target=run_analysis_background,
                args=(body, registry),
                daemon=True,
                name=f"analyze-{job_id[:8]}",
            )
            _analysis_threads[job_id] = t
            t.start()

    # Poll loop
    last_message = ""
    POLL_INTERVAL = 2

    for _ in range(1800):  # Max 1 hour (1800 × 2s)
        time.sleep(POLL_INTERVAL)

        db.expire_all()
        run = db.execute(
            select(IngestionRun).where(IngestionRun.id == job_uuid)
        ).scalar_one_or_none()

        if run is None:
            yield _sse({"stage": "error", "message": "Job disappeared"})
            break

        progress = (run.metrics or {}).get("analysis_progress", {})
        message = progress.get("message", "")

        if message and message != last_message:
            event = {
                "stage": progress.get("stage", ""),
                "message": message,
                "status": progress.get("status", "running"),
            }
            detail = progress.get("detail")
            if detail:
                event["detail"] = detail
            result = progress.get("result")
            if result:
                event["result"] = result
            yield _sse(event)
            last_message = message

        # Terminal states
        if run.status == "analyzed":
            final = progress.get("result") or {"job_id": job_id, "status": "analyzed"}
            yield _sse({"stage": "complete", "result": final})
            break
        if run.status == "failed":
            yield _sse({"stage": "error", "message": progress.get("message", "Analysis failed")})
            break
        if run.status == "cancelled":
            yield _sse({"stage": "error", "message": "Analysis cancelled"})
            break

    db.close()