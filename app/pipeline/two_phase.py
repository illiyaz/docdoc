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

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.policies import StorageMode, StoragePolicyConfig
from app.core.security import SecurityService
from app.core.settings import get_settings
from app.db.models import Document, DocumentAnalysisReview, IngestionRun
from app.db.repositories import ExtractionRepository
from app.pipeline.auto_approve import should_auto_approve
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
    db.flush()

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
        db.flush()

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
                doc_blocks_cache[doc.id] = blocks

                result = structure_task.run(blocks, str(doc.id), db_session=db)
                doc.structure_analysis = result.to_dict()
            except Exception as e:
                logger.warning("Structure analysis failed for doc %s: %s", doc.file_name, type(e).__name__)

        db.flush()
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

        db.flush()

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

                    schema = doc_understanding.understand(
                        sample_blocks,
                        heuristic_doc_type=heuristic_doc_type,
                        file_name=doc.file_name or "",
                        file_type=doc.file_type or "",
                        structure_class=doc.structure_class or "",
                        onset_page=onset_page,
                        document_id=str(doc.id),
                    )

                    doc_schemas[doc.id] = schema

                    if schema is not None:
                        understanding_count += 1
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

                db.flush()
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

                db.flush()
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
            )
            db.add(review)

            if approved:
                doc.analysis_phase_status = "approved"
                approved_count += 1
            else:
                doc.analysis_phase_status = "pending_review"
                review_count += 1

        db.flush()

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
        db.flush()

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
                db.flush()
            except Exception:
                pass
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

def extract_generator(
    job_id: str,
    db: Session | None,
    registry: ProtocolRegistry,
) -> Generator[str, None, None]:
    """Run the extraction phase for approved documents, yielding SSE events.

    Stages: detection -> resolution -> deduplication -> notification -> complete.

    Requires the IngestionRun to have status="analyzed" and
    pipeline_mode="two_phase".
    """
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

    # --- Load IngestionRun ---
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

    if run.status != "analyzed":
        yield _sse({
            "stage": "error",
            "message": f"Job {job_id!r} status is {run.status!r}, expected 'analyzed'",
        })
        return

    # --- Load protocol ---
    config_snapshot = run.config_snapshot or {}
    protocol_id = config_snapshot.get("protocol_id", "")
    try:
        protocol = registry.get(protocol_id)
    except KeyError:
        yield _sse({"stage": "error", "message": f"Protocol not found: {protocol_id!r}"})
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
            pass  # best-effort

    target_entities = _resolve_target_entities(protocol_config, protocol_id)

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
        yield _sse({"stage": "error", "message": "No approved documents found for extraction"})
        return

    # Update run status
    run.status = "extracting"
    db.flush()

    try:
        # --- Stage 1: Detection ---
        yield _sse({
            "stage": "detection", "status": "running",
            "message": "Starting PII detection...",
            "detail": {"total": len(approved_docs), "current": 0},
        })

        from app.pii.presidio_engine import PresidioEngine
        from app.readers.registry import get_reader
        from app.rra.entity_resolver import PIIRecord

        engine = PresidioEngine()
        all_records: list[PIIRecord] = []

        # Optionally set up SchemaFilter for extract phase
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

        for i, doc in enumerate(approved_docs, 1):
            yield _sse({
                "stage": "detection", "status": "running",
                "message": f"Scanning document {i}/{len(approved_docs)}...",
                "detail": {"total": len(approved_docs), "current": i},
            })

            try:
                reader = get_reader(doc.source_path)
                blocks = reader.read()
                detections = engine.analyze(blocks, target_entity_types=target_entities)

                # Apply SchemaFilter if LLM is available
                if doc_understanding_cls is not None and schema_filter_cls is not None:
                    try:
                        # Get onset page sample for document understanding
                        onset_page = doc.sample_onset_page or 0
                        sample_blocks = filter_sample_blocks(blocks, onset_page, doc.file_type or "unknown")
                        heuristic_doc_type = "unknown"
                        if doc.structure_analysis and isinstance(doc.structure_analysis, dict):
                            heuristic_doc_type = doc.structure_analysis.get("document_type", "unknown")

                        du = doc_understanding_cls(db_session=db)
                        schema = du.understand(
                            sample_blocks,
                            heuristic_doc_type=heuristic_doc_type,
                            file_name=doc.file_name or "",
                            file_type=doc.file_type or "",
                            structure_class=doc.structure_class or "",
                            onset_page=onset_page,
                            document_id=str(doc.id),
                        )
                        if schema is not None:
                            sf = schema_filter_cls(schema)
                            result = sf.filter_detections(detections)
                            detections = result.kept
                    except Exception:
                        pass  # best-effort; use unfiltered detections

                for det in detections:
                    rec = PIIRecord(
                        record_id=str(uuid4()),
                        entity_type=det.entity_type,
                        normalized_value=det.block.text[det.start:det.end] if hasattr(det, "block") else "",
                        source_document_id=str(doc.id),
                        page_or_sheet=det.block.page_or_sheet if hasattr(det, "block") else 0,
                    )
                    all_records.append(rec)
            except Exception as e:
                logger.warning("Detection failed for doc %s: %s", doc.file_name, type(e).__name__)

        yield _sse({
            "stage": "detection", "status": "complete",
            "message": f"Detected {len(all_records)} PII record(s) across {len(approved_docs)} document(s)",
            "detail": {"records_found": len(all_records)},
        })

        # --- Stage 2: Entity Resolution ---
        yield _sse({"stage": "resolution", "status": "running", "message": "Resolving entities..."})

        from app.rra.entity_resolver import EntityResolver

        resolver = EntityResolver()
        groups = resolver.resolve(all_records)

        yield _sse({
            "stage": "resolution", "status": "complete",
            "message": f"Resolved into {len(groups)} group(s)",
        })

        # --- Stage 3: Deduplication ---
        yield _sse({"stage": "deduplication", "status": "running", "message": "Building notification subjects..."})

        from app.rra.deduplicator import Deduplicator

        dedup = Deduplicator(db)
        subjects = dedup.build_subjects(groups)

        yield _sse({
            "stage": "deduplication", "status": "complete",
            "message": f"Built {len(subjects)} subject(s)",
        })

        # --- Stage 4: Notification ---
        yield _sse({"stage": "notification", "status": "running", "message": "Building notification list..."})

        from app.notification.list_builder import build_notification_list

        nl = build_notification_list(str(job_uuid), protocol, subjects, db)
        notif_count = sum(1 for s in subjects if s.notification_required)

        yield _sse({
            "stage": "notification", "status": "complete",
            "message": f"{notif_count} notification(s) required",
        })

        # --- Mark run as completed ---
        run.status = "completed"
        run.completed_at = datetime.now(timezone.utc)
        db.flush()

        # --- Complete ---
        yield _sse({
            "stage": "complete",
            "result": {
                "job_id": str(job_uuid),
                "status": "COMPLETE",
                "subjects_found": len(subjects),
                "notification_required": notif_count,
            },
        })

    except Exception as exc:
        logger.error("Job %s failed at extract phase: %s", str(job_uuid), type(exc).__name__)
        if run is not None:
            run.status = "failed"
            run.error_summary = str(type(exc).__name__)
            run.completed_at = datetime.now(timezone.utc)
            try:
                db.flush()
            except Exception:
                pass
        yield _sse({"stage": "error", "message": f"Pipeline failed: {type(exc).__name__}"})

    finally:
        if owns_db and db is not None:
            try:
                db.commit()
            except Exception:
                db.rollback()
            finally:
                db.close()
