"""Job management routes — full implementation.

POST /jobs/upload saves uploaded files and returns an upload_id.
POST /jobs/{job_id}/upload adds files to an existing job (Step 24b).
POST /jobs runs the synchronous pipeline (JSON response).
POST /jobs/run runs the pipeline with SSE streaming progress.

GET /jobs/{job_id} returns job status.
GET /jobs/{job_id}/results returns masked NotificationSubjects.
GET /jobs/{job_id}/status returns per-stage pipeline status (Step 8b).
GET /jobs/recent returns recent jobs, optionally unlinked only (Step 8b).
PATCH /jobs/{job_id} links a job to a project (Step 8b).

POST /jobs/{job_id}/cancel cancels a running/pending job (Step 16b).
DELETE /jobs/{job_id} soft-deletes (archives) a job (Step 16b).
"""
from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, model_validator
from sqlalchemy import func as sqla_func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_protocol_registry

def get_db_session():
    """Create a standalone DB session for background threads."""
    from app.api.deps import _get_session_factory
    return _get_session_factory()()
from app.core.settings import get_settings
from app.db.models import Document, Extraction, IngestionRun, NotificationList, NotificationSubject, Project
from app.notification.list_builder import build_notification_list, get_notification_subjects
from app.protocols.registry import ProtocolRegistry
from app.rra.deduplicator import Deduplicator
from app.pipeline.record_mapper import (
    build_composite_record,
    detection_to_pii_record,
    extract_with_template,
)
from app.pipeline.smart_grouping import group_detections_to_records
from app.rra.entity_resolver import EntityResolver
from app.tasks.discovery import DiscoveryTask, FilesystemConnector

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["jobs"])

# Supported extensions for upload (matches reader registry + archives)
from app.api.upload_helpers import (
    ARCHIVE_EXTENSIONS,
    EMAIL_EXTENSIONS,
    SKIP_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    extract_archive as _extract_archive,
    extract_email_attachments as _extract_email_attachments,
    is_supported as _is_supported,
    process_uploaded_file as _process_uploaded_file,
    safe_filename as _safe_filename,
    should_skip as _should_skip,
)



# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class CreateJobBody(BaseModel):
    job_id: str | None = None
    protocol_id: str
    source_directory: str | None = None
    upload_id: str | None = None
    project_id: str | None = None
    protocol_config_id: str | None = None
    pipeline_mode: str = "full"

    @model_validator(mode="after")
    def exactly_one_source(self):
        has_dir = self.source_directory is not None and self.source_directory.strip() != ""
        has_upload = self.upload_id is not None and self.upload_id.strip() != ""
        if has_dir and has_upload:
            raise ValueError("Provide either source_directory or upload_id, not both")
        if not has_dir and not has_upload:
            raise ValueError("Provide either source_directory or upload_id")
        return self


class PatchJobBody(BaseModel):
    project_id: str


# ---------------------------------------------------------------------------
# PII masking helpers
# ---------------------------------------------------------------------------

def _mask_email(email: str | None) -> str | None:
    return "***@***.***" if email else None


def _mask_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    digits = "".join(c for c in phone if c.isdigit())
    return f"***-***-{digits[-4:]}" if len(digits) >= 4 else "***-***-****"


def _masked_subject(
    ns: NotificationSubject,
    db: Session | None = None,
) -> dict:
    settings = get_settings()
    if settings.pii_masking_enabled:
        email = _mask_email(ns.canonical_email)
        phone = _mask_phone(ns.canonical_phone)
    else:
        email = ns.canonical_email
        phone = ns.canonical_phone

    result = {
        "subject_id": str(ns.subject_id),
        "canonical_name": ns.canonical_name,
        "canonical_email": email,
        "canonical_phone": phone,
        "pii_types_found": ns.pii_types_found or [],
        "notification_required": ns.notification_required,
        "review_status": ns.review_status,
    }

    # Enrich with field frequency and person context when db is available
    if db is not None:
        try:
            result.update(_compute_field_enrichment(ns, db))
        except Exception:
            # Never break the results endpoint for enrichment failures
            logger.debug(
                "Field enrichment failed for subject %s", ns.subject_id,
                exc_info=True,
            )

    return result


def _compute_field_enrichment(
    ns: NotificationSubject,
    db: Session,
) -> dict:
    """Compute field_frequency and person_context for a subject.

    Uses the source_records JSON on the NotificationSubject to look up
    Extraction records and compute page-level frequency and person context.
    """
    from app.pii.schema_filter import compute_field_frequency, build_person_context

    source_records = ns.source_records or []
    if not source_records:
        return {}

    # Gather document IDs from source records to query extractions
    doc_ids: set[str] = set()
    for rec in source_records:
        if isinstance(rec, dict):
            doc_id = rec.get("document_id") or rec.get("doc_id")
            if doc_id:
                doc_ids.add(str(doc_id))

    if not doc_ids:
        return {}

    # Query extractions for these documents
    try:
        extractions = db.execute(
            select(Extraction).where(
                Extraction.document_id.in_(doc_ids)
            )
        ).scalars().all()
    except Exception:
        return {}

    if not extractions:
        return {}

    # Compute total pages from source_page_range or extraction evidence_pages
    total_pages = 1
    if ns.source_page_range:
        try:
            parts = ns.source_page_range.split("-")
            if len(parts) == 2:
                total_pages = max(1, int(parts[1]) - int(parts[0]) + 1)
            else:
                total_pages = max(1, int(parts[0]))
        except (ValueError, IndexError):
            pass
    if total_pages <= 1:
        # Estimate from extraction evidence pages
        all_pages = {
            ext.evidence_page for ext in extractions
            if ext.evidence_page is not None
        }
        if all_pages:
            total_pages = max(all_pages) - min(all_pages) + 1

    field_freq = compute_field_frequency(extractions, total_pages)
    person_ctx = build_person_context(extractions)

    enrichment: dict = {}
    if field_freq:
        enrichment["field_frequency"] = [f.to_dict() for f in field_freq]
    if person_ctx:
        enrichment["person_context"] = [p.to_dict() for p in person_ctx]

    return enrichment


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/protocols", summary="List available protocols")
def list_protocols(registry: ProtocolRegistry = Depends(get_protocol_registry)):
    return [
        {
            "protocol_id": p.protocol_id,
            "name": p.name,
            "jurisdiction": p.jurisdiction,
            "regulatory_framework": p.regulatory_framework,
            "notification_deadline_days": p.notification_deadline_days,
        }
        for p in registry.list_all()
    ]


@router.get("/recent", summary="List recent jobs, optionally only unlinked")
def list_recent_jobs(
    unlinked: bool = Query(False, description="If true, only return jobs not linked to any project"),
    limit: int = Query(50, ge=1, le=200, description="Max number of jobs to return"),
    db: Session = Depends(get_db),
):
    """Return recent ingestion runs ordered by created_at desc.

    When ``unlinked=true``, only runs with ``project_id IS NULL`` are returned.
    """
    stmt = select(IngestionRun).order_by(IngestionRun.created_at.desc())
    if unlinked:
        stmt = stmt.where(IngestionRun.project_id.is_(None))
    stmt = stmt.limit(limit)

    runs = db.execute(stmt).scalars().all()
    return [_ingestion_run_summary(run, db) for run in runs]


def _job_stats(run: IngestionRun, db: Session) -> dict:
    """Return a normalized stats bundle for one job (used by compare)."""
    subjects = db.execute(
        select(NotificationSubject).where(NotificationSubject.ingestion_run_id == run.id)
    ).scalars().all()
    docs = db.execute(
        select(Document).where(Document.ingestion_run_id == run.id)
    ).scalars().all()

    gov_id = sum(1 for s in subjects if s.canonical_government_id)
    dob = sum(1 for s in subjects if s.canonical_dob)
    addr = sum(1 for s in subjects if s.canonical_address)
    email = sum(1 for s in subjects if s.canonical_email)
    phone = sum(1 for s in subjects if s.canonical_phone)
    total = len(subjects) or 1  # avoid div-by-zero in pct calc

    doc_stats: list[dict] = []
    for d in docs:
        doc_subj = [s for s in subjects if s.source_document_name == d.file_name]
        doc_stats.append({
            "id": str(d.id),
            "file_name": d.file_name,
            "subject_count": len(doc_subj),
            "status": (d.metadata_json or {}).get("extraction_status", "unknown"),
        })

    return {
        "job_id": str(run.id),
        "status": run.status,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "subject_count": len(subjects),
        "doc_count": len(docs),
        "coverage": {
            "gov_id_pct": round(gov_id * 100 / total, 1),
            "dob_pct": round(dob * 100 / total, 1),
            "address_pct": round(addr * 100 / total, 1),
            "email_pct": round(email * 100 / total, 1),
            "phone_pct": round(phone * 100 / total, 1),
        },
        "counts": {"gov_id": gov_id, "dob": dob, "address": addr, "email": email, "phone": phone},
        "docs": doc_stats,
    }


@router.get("/compare", summary="Compare two jobs side-by-side (stats + per-doc delta)")
def compare_jobs(
    a: UUID = Query(..., description="Baseline job id"),
    b: UUID = Query(..., description="Candidate job id"),
    db: Session = Depends(get_db),
):
    """Return side-by-side stats for two ingestion runs.

    Response shape:
      { "a": {...stats...}, "b": {...stats...}, "delta": {
          "subject_count": +N, "gov_id_pct": +X.X, ...
      } }
    """
    run_a = db.get(IngestionRun, a)
    run_b = db.get(IngestionRun, b)
    if run_a is None:
        raise HTTPException(status_code=404, detail=f"Job {a} not found")
    if run_b is None:
        raise HTTPException(status_code=404, detail=f"Job {b} not found")

    stats_a = _job_stats(run_a, db)
    stats_b = _job_stats(run_b, db)

    delta = {
        "subject_count": stats_b["subject_count"] - stats_a["subject_count"],
        "doc_count": stats_b["doc_count"] - stats_a["doc_count"],
    }
    for key in ("gov_id_pct", "dob_pct", "address_pct", "email_pct", "phone_pct"):
        delta[key] = round(stats_b["coverage"][key] - stats_a["coverage"][key], 1)

    return {"a": stats_a, "b": stats_b, "delta": delta}


@router.post("/upload", summary="Upload files for a new job")
async def upload_files(files: list[UploadFile] = File(...)):
    """Save uploaded files to a temp directory and return an upload_id.

    Handles all 47 supported formats. Archives (ZIP/7z) are extracted
    recursively. Email attachments (EML/MSG) are saved alongside the
    email body. Unsupported files are silently skipped.
    Returns 400 if no supported files remain after filtering.
    """
    settings = get_settings()
    max_file_bytes = settings.upload_max_file_size_mb * 1024 * 1024
    max_total_bytes = settings.upload_max_total_size_mb * 1024 * 1024

    upload_id = str(uuid4())
    upload_path = Path(settings.upload_dir) / upload_id
    upload_path.mkdir(parents=True, exist_ok=True)

    saved_files: list[dict] = []
    total_bytes = 0

    try:
        for f in files:
            filename = f.filename or "unknown"

            # Skip unsupported/hidden files
            if _should_skip(filename) or not _is_supported(filename):
                continue

            # Read file content
            content = await f.read()
            file_size = len(content)

            # Per-file size check
            if file_size > max_file_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"File {filename!r} exceeds {settings.upload_max_file_size_mb}MB limit",
                )

            total_bytes += file_size
            if total_bytes > max_total_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"Total upload exceeds {settings.upload_max_total_size_mb}MB limit",
                )

            produced = _process_uploaded_file(content, filename, upload_path)
            saved_files.extend(produced)

        if not saved_files:
            raise HTTPException(
                status_code=400,
                detail="No supported files in upload. Supported: "
                       + ", ".join(sorted(SUPPORTED_EXTENSIONS)),
            )

        return {
            "upload_id": upload_id,
            "directory": str(upload_path),
            "file_count": len(saved_files),
            "total_size_bytes": total_bytes,
            "files": saved_files,
        }

    except HTTPException:
        # Clean up on validation error
        shutil.rmtree(upload_path, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(upload_path, ignore_errors=True)
        raise


@router.post("/{job_id}/upload", summary="Upload additional files to an existing job")
async def upload_to_job(
    job_id: UUID,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    """Add files to an existing job's source directory.

    The job must exist and have status 'pending' or 'analyzed' (not yet
    extracting or complete). Handles all 47 supported formats: archives
    are extracted, email attachments are saved alongside their parent.

    Returns the list of files added and updated totals.
    """
    settings = get_settings()
    max_file_bytes = settings.upload_max_file_size_mb * 1024 * 1024
    max_total_bytes = settings.upload_max_total_size_mb * 1024 * 1024

    # Look up the job
    run = db.execute(
        select(IngestionRun).where(IngestionRun.id == job_id)
    ).scalar_one_or_none()

    if run is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    # Only allow uploads to jobs that haven't started extraction
    if run.status not in ("pending", "analyzed", "analysis_complete"):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot upload to job with status '{run.status}'. "
                   "Job must be pending or analyzed.",
        )

    # Resolve the job's source directory
    source_dir = Path(run.source_path)
    if not source_dir.is_dir():
        raise HTTPException(
            status_code=404,
            detail=f"Job source directory not found: {run.source_path}",
        )

    saved_files: list[dict] = []
    total_bytes = 0

    for f in files:
        filename = f.filename or "unknown"

        if _should_skip(filename) or not _is_supported(filename):
            continue

        content = await f.read()
        file_size = len(content)

        if file_size > max_file_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"File {filename!r} exceeds {settings.upload_max_file_size_mb}MB limit",
            )

        total_bytes += file_size
        if total_bytes > max_total_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"Total upload exceeds {settings.upload_max_total_size_mb}MB limit",
            )

        produced = _process_uploaded_file(content, filename, source_dir)
        saved_files.extend(produced)

    if not saved_files:
        raise HTTPException(
            status_code=400,
            detail="No supported files in upload. Supported: "
                   + ", ".join(sorted(SUPPORTED_EXTENSIONS)),
        )

    # Count total files now in the directory
    existing_count = sum(1 for p in source_dir.iterdir() if p.is_file())

    return {
        "job_id": str(job_id),
        "files_added": len(saved_files),
        "total_bytes_added": total_bytes,
        "total_files_in_job": existing_count,
        "files": saved_files,
    }


# ---------------------------------------------------------------------------
# SSE streaming pipeline
# ---------------------------------------------------------------------------

def _sse(data: dict) -> str:
    """Format a dict as an SSE data line."""
    return f"data: {json.dumps(data)}\n\n"


def build_composite_record_from_dict(data: dict, doc_id: str):
    """Build a PIIRecord from an LLM JSON dict (spatial text extraction)."""
    from uuid import uuid4
    from app.rra.entity_resolver import PIIRecord

    raw_name = data.get("PERSON") or data.get("NAME") or data.get("name") or data.get("person")
    raw_gov_id = data.get("US_SSN") or data.get("SSN") or data.get("NI_NUMBER") or data.get("GOVERNMENT_ID")
    raw_dob = data.get("DATE_OF_BIRTH") or data.get("DOB") or data.get("dob")
    raw_email = data.get("EMAIL_ADDRESS") or data.get("EMAIL") or data.get("email")
    raw_phone = data.get("PHONE_NUMBER") or data.get("PHONE") or data.get("phone")
    raw_addr = data.get("LOCATION") or data.get("ADDRESS") or data.get("address")

    if not raw_name and not raw_gov_id:
        return None

    # Build entity_types_found from populated fields
    types_found: list[str] = []
    if raw_name:
        types_found.append("PERSON")
    if raw_gov_id:
        types_found.append("US_SSN")
    if raw_dob:
        types_found.append("DATE_OF_BIRTH")
    if raw_email:
        types_found.append("EMAIL_ADDRESS")
    if raw_phone:
        types_found.append("PHONE_NUMBER")
    if raw_addr:
        types_found.append("LOCATION")

    return PIIRecord(
        record_id=str(uuid4()),
        entity_type="PERSON" if raw_name else "GOVERNMENT_ID",
        normalized_value=raw_name or raw_gov_id or "",
        raw_name=raw_name,
        raw_government_id=raw_gov_id,
        raw_dob=raw_dob,
        raw_email=raw_email,
        raw_phone=raw_phone,
        raw_address={"raw": raw_addr} if raw_addr else None,
        source_document_id=doc_id,
        entity_types_found=tuple(types_found),
    )


def _pipeline_generator(
    body: CreateJobBody,
    db: Session | None,
    registry: ProtocolRegistry,
) -> Generator[str, None, None]:
    """Run the full pipeline, yielding SSE events at each stage.

    If *db* is None (e.g. when called from the streaming endpoint which
    manages its own session), a session is created lazily from the
    default session factory.
    """
    import hashlib
    from datetime import datetime, timezone

    job_id = body.job_id or str(uuid4())
    job_uuid = UUID(job_id) if body.job_id else uuid4()
    settings = get_settings()
    owns_db = False
    run: IngestionRun | None = None

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

    # --- Create IngestionRun record ---
    project_uuid = None
    if body.project_id:
        try:
            project_uuid = UUID(body.project_id)
        except (ValueError, AttributeError):
            pass

    existing = db.get(IngestionRun, job_uuid)
    if existing is not None:
        run = existing
        run.status = "running"
        if run.started_at is None:
            run.started_at = datetime.now(timezone.utc)
        db.flush()
    else:
        run = IngestionRun(
            id=job_uuid,
            project_id=project_uuid,
            source_path=source_directory,
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

        # --- Stage 3: PII Detection ---
        yield _sse({
            "stage": "detection", "status": "running",
            "message": "Starting PII detection...",
            "detail": {"total": len(docs), "current": 0},
        })

        from app.pii.presidio_engine import PresidioEngine
        from app.readers.registry import get_reader

        engine = PresidioEngine()
        all_records = []

        # Optionally set up LLM document understanding + schema filter
        schema_filter_cls = None
        doc_understanding_cls = None
        try:
            from app.core.settings import get_settings as _gs
            if _gs().llm_assist_enabled:
                from app.pii.schema_filter import SchemaFilter as _SF
                from app.structure.llm_document_understanding import LLMDocumentUnderstanding as _LDU
                schema_filter_cls = _SF
                doc_understanding_cls = _LDU
        except Exception:
            pass

        for i, doc_info in enumerate(docs, 1):
            doc_name = doc_info.get("file_name", "?")
            yield _sse({
                "stage": "detection", "status": "running",
                "message": f"Processing {doc_name} ({i}/{len(docs)})...",
                "detail": {"total": len(docs), "current": i, "doc_name": doc_name},
            })

            try:
                # --- Read document ---
                try:
                    reader = get_reader(doc_info["source_path"])
                    blocks = reader.read()
                except Exception as read_err:
                    logger.warning("Reader failed for %s: %s — skipping", doc_name, type(read_err).__name__)
                    continue

                if not blocks:
                    logger.info("No blocks from %s — skipping", doc_name)
                    continue

                # --- LiteParse spatial text (better than flat PyMuPDF for LLM) ---
                spatial_pages: dict[int, str] = {}
                is_pdf = doc_info.get("file_type", "").lower() in ("pdf", "application/pdf")
                if is_pdf and doc_info.get("source_path"):
                    try:
                        from app.readers.liteparse_adapter import get_spatial_text_pages
                        doc_page_nums = sorted(set(b.page_or_sheet for b in blocks if isinstance(b.page_or_sheet, int)))
                        spatial_pages = get_spatial_text_pages(
                            doc_info["source_path"],
                            page_numbers=doc_page_nums[:50] if doc_page_nums else None,
                            max_pages=50,
                        )
                    except Exception:
                        pass

                # --- Document understanding ---
                schema = None
                if doc_understanding_cls is not None and schema_filter_cls is not None:
                    try:
                        doc_pages = set(b.page_or_sheet for b in blocks)
                        du = doc_understanding_cls(db_session=db)
                        schema = du.understand(
                            blocks,
                            file_name=doc_name,
                            file_type=doc_info.get("file_type", ""),
                            total_pages=len(doc_pages),
                        )
                    except Exception:
                        pass

                # --- 3-path extraction (Step 19 — exclusive) ---
                is_template = (
                    schema is not None
                    and schema.template
                    and schema.template.pages_per_instance >= 2
                )
                doc_records: list = []

                if is_template:
                    doc_pages = set(b.page_or_sheet for b in blocks)
                    total_pg = len(doc_pages)

                    # Path A: LLM extraction
                    if schema_filter_cls is not None:
                        try:
                            from app.llm.client import OllamaClient
                            from app.structure.llm_template_extractor import LLMTemplateExtractor
                            from app.core.constants import DEFAULT_EXTRACTION_BATCH_SIZE

                            # Use LiteParse spatial text if available (better layout)
                            page_texts: dict[int, str] = {}
                            for b in blocks:
                                pg = b.page_or_sheet
                                if pg not in page_texts:
                                    page_texts[pg] = ""
                                page_texts[pg] += b.text + "\n"

                            client = OllamaClient(db_session=db)
                            extractor = LLMTemplateExtractor(client, batch_size=DEFAULT_EXTRACTION_BATCH_SIZE)
                            llm_records = extractor.extract_all_instances(
                                schema, page_texts, doc_info["source_path"], total_pg,
                            )
                            if llm_records:
                                doc_records = llm_records
                        except Exception:
                            logger.warning("Path A (LLM template) failed for %s", doc_name, exc_info=True)

                    # Path B fallback: Presidio composite
                    if not doc_records:
                        try:
                            detections = engine.analyze(blocks)
                            if schema is not None and schema_filter_cls is not None:
                                try:
                                    sf = schema_filter_cls(schema)
                                    result = sf.filter_detections(detections)
                                    detections = result.kept
                                except Exception:
                                    pass
                            doc_records = extract_with_template(detections, schema, doc_info["source_path"], total_pg)
                        except Exception:
                            logger.warning("Path B (Presidio template) failed for %s", doc_name, exc_info=True)

                else:
                    # Path C: non-template
                    # Try LLM with spatial text first — batch 3 pages per LLM call
                    if spatial_pages and len(spatial_pages) > 0:
                        try:
                            import json as _json
                            from app.llm.client import OllamaClient
                            client = OllamaClient(db_session=db)

                            sorted_pages = sorted(spatial_pages.keys())
                            BATCH_SIZE = 3
                            MAX_BATCHES = 30  # Cap at ~90 pages to avoid runaway LLM costs

                            for batch_idx in range(0, min(len(sorted_pages), MAX_BATCHES * BATCH_SIZE), BATCH_SIZE):
                                batch_page_nums = sorted_pages[batch_idx:batch_idx + BATCH_SIZE]
                                batch_text = ""
                                for pn in batch_page_nums:
                                    pt = spatial_pages.get(pn, "")
                                    if pt.strip():
                                        batch_text += f"\n--- PAGE {pn + 1} ---\n{pt}"

                                if len(batch_text.strip()) < 100:
                                    continue

                                prompt = (
                                    "Extract ALL personally identifiable information from these document pages.\n"
                                    "IMPORTANT: There may be MULTIPLE people per page (e.g. tables, lists).\n"
                                    "Return one JSON object per INDIVIDUAL person with fields: "
                                    "PERSON, US_SSN, DATE_OF_BIRTH, EMAIL_ADDRESS, PHONE_NUMBER, LOCATION.\n"
                                    "Return a JSON array. If a page has 8 people, return 8 objects.\n\n"
                                    f"Document content:\n{batch_text[:6000]}"
                                )
                                try:
                                    response = client.generate(prompt, use_case="spatial_text_extraction")
                                    if response:
                                        data = _json.loads(response)
                                        if isinstance(data, dict):
                                            data = [data]
                                        if isinstance(data, list):
                                            for item in data:
                                                if isinstance(item, dict):
                                                    rec = build_composite_record_from_dict(item, doc_info["source_path"])
                                                    if rec and (rec.raw_name or rec.raw_government_id):
                                                        doc_records.append(rec)
                                except (_json.JSONDecodeError, ValueError):
                                    pass
                                except Exception:
                                    break  # LLM error — stop batching, fall to Presidio

                            if doc_records:
                                logger.info("LiteParse+LLM extraction for %s: %d records from %d pages",
                                            doc_name, len(doc_records), len(sorted_pages))
                        except Exception:
                            pass  # Fall through to Presidio

                    # ALWAYS run Presidio with smart_grouping for complete coverage
                    # LLM may return partial results (3 out of 8 people on a page)
                    # Presidio fills the gaps
                    try:
                        detections = engine.analyze(blocks)
                        if schema is not None and schema_filter_cls is not None:
                            try:
                                sf = schema_filter_cls(schema)
                                result = sf.filter_detections(detections)
                                detections = result.kept
                            except Exception:
                                pass

                        presidio_records = group_detections_to_records(
                            detections, doc_info["source_path"],
                            schema=schema, doc_path=doc_info.get("source_path"),
                        )

                        if doc_records and presidio_records:
                            # Merge: LLM records are primary, Presidio fills gaps
                            # Deduplicate by name — LLM records win on conflict
                            llm_names = {r.raw_name.lower().strip() for r in doc_records if r.raw_name}
                            for pr in presidio_records:
                                if pr.raw_name and pr.raw_name.lower().strip() not in llm_names:
                                    doc_records.append(pr)
                                elif not pr.raw_name and (pr.raw_government_id or pr.raw_phone or pr.raw_email):
                                    doc_records.append(pr)  # Nameless PII records from Presidio
                            logger.info("Merged LLM+Presidio for %s: %d total records", doc_name, len(doc_records))
                        elif not doc_records:
                            doc_records = presidio_records
                    except Exception:
                        logger.warning("Presidio failed for %s", doc_name, exc_info=True)

                if doc_records:
                    logger.info("Extracted %d records from %s", len(doc_records), doc_name)
                all_records.extend(doc_records)

            except Exception:
                logger.error("Document %s failed completely — skipping", doc_name, exc_info=True)

        # --- Pattern validation (Step 20) ---
        if all_records:
            from app.pii.pattern_validator import validate_extracted_records
            all_records = validate_extracted_records(all_records)

        yield _sse({
            "stage": "detection", "status": "complete",
            "message": f"Detected {len(all_records)} PII record(s) across {len(docs)} document(s)",
            "detail": {"records_found": len(all_records)},
        })

        # --- Stage 4: Entity Resolution ---
        yield _sse({"stage": "resolution", "status": "running", "message": "Resolving entities..."})
        resolver = EntityResolver()
        groups = resolver.resolve(all_records)
        yield _sse({
            "stage": "resolution", "status": "complete",
            "message": f"Resolved into {len(groups)} group(s)",
        })

        # --- Stage 5: Deduplication ---
        yield _sse({"stage": "deduplication", "status": "running", "message": "Building notification subjects..."})

        # Clean previous subjects for this project (each run is a clean slate)
        if run.project_id is not None:
            old_count = db.query(NotificationSubject).filter(
                NotificationSubject.project_id == run.project_id,
            ).delete()
            db.flush()
            if old_count:
                logger.info("Cleared %d old notification subjects for project %s", old_count, run.project_id)

        dedup = Deduplicator(db)
        subjects = dedup.build_subjects(groups)

        # Set project_id on each NotificationSubject
        for subj in subjects:
            if subj.project_id is None and run.project_id is not None:
                subj.project_id = run.project_id

        # Create ReviewTasks based on merge confidence
        review_count = 0
        try:
            from app.review.queue_manager import QueueManager
            qm = QueueManager(db)
            for i, group in enumerate(groups):
                if i >= len(subjects):
                    break
                subj = subjects[i]
                sid = str(subj.subject_id)
                if group.merge_confidence < 0.60:
                    qm.create_task("escalation", sid)
                    review_count += 1
                elif group.merge_confidence < 0.80 or group.needs_human_review:
                    qm.create_task("low_confidence", sid)
                    review_count += 1
        except Exception:
            pass  # best-effort; don't fail pipeline if review queue fails

        db.flush()

        yield _sse({
            "stage": "deduplication", "status": "complete",
            "message": f"Built {len(subjects)} subject(s), {review_count} for review",
        })

        # --- Stage 6: Notification ---
        yield _sse({"stage": "notification", "status": "running", "message": "Building notification list..."})
        nl = build_notification_list(str(job_uuid), protocol, subjects, db)
        notif_count = sum(1 for s in subjects if s.notification_required)
        yield _sse({
            "stage": "notification", "status": "complete",
            "message": f"{notif_count} notification(s) required",
        })

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
                yield _sse({
                    "stage": "export", "status": "complete",
                    "message": f"CSV export ready: {export_count} subject(s)",
                })
            except Exception:
                pass  # best-effort

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
                "export_count": export_count,
            },
        })

    except Exception as exc:
        logger.error("Job %s failed at streaming pipeline: %s", str(job_uuid), type(exc).__name__, exc_info=True)
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
        if body.upload_id:
            upload_dir = Path(settings.upload_dir) / body.upload_id
            shutil.rmtree(upload_dir, ignore_errors=True)
        if owns_db and db is not None:
            try:
                db.commit()
            except Exception:
                db.rollback()
            finally:
                db.close()


@router.post("/run", summary="Submit job and return job_id for polling")
def run_job(
    body: CreateJobBody,
    db: Session = Depends(get_db),
    registry: ProtocolRegistry = Depends(get_protocol_registry),
):
    """Create an IngestionRun record and return the job_id immediately.

    The caller can then poll ``GET /jobs/{id}/status`` to track progress.
    The pipeline SSE stream is also returned for clients that support it.
    """
    job_uuid = UUID(body.job_id) if body.job_id else uuid4()

    # Validate protocol
    try:
        registry.get(body.protocol_id)
    except KeyError:
        raise HTTPException(status_code=400, detail=f"Protocol not found: {body.protocol_id!r}")

    # Resolve project_id to UUID if provided
    project_uuid = None
    if body.project_id:
        try:
            project_uuid = UUID(body.project_id)
        except (ValueError, AttributeError):
            raise HTTPException(status_code=400, detail="Invalid project_id format")

    # Create IngestionRun record so it is immediately queryable
    run = IngestionRun(
        id=job_uuid,
        project_id=project_uuid,
        source_path=body.source_directory or body.upload_id or "",
        config_hash="",
        code_version="0.1.0",
        initiated_by="api",
        status="pending",
        config_snapshot={
            "protocol_id": body.protocol_id,
            "protocol_config_id": body.protocol_config_id,
        },
    )
    db.add(run)
    db.flush()

    return {
        "job_id": str(job_uuid),
        "status": "pending",
        "project_id": str(project_uuid) if project_uuid else None,
        "protocol_config_id": body.protocol_config_id,
    }


_pipeline_threads: dict[str, threading.Thread] = {}


def _run_pipeline_background(body: CreateJobBody, registry: ProtocolRegistry) -> None:
    """Run _pipeline_generator in a background thread.

    Captures SSE events and writes progress to IngestionRun.metrics["pipeline_progress"].
    Survives browser disconnects.

    IMPORTANT: Uses fresh sessions for heartbeat writes — the generator's
    internal session may be closed/replaced by _refresh_session().
    """
    from app.api.deps import _get_session_factory
    from sqlalchemy.orm.attributes import flag_modified

    SessionFactory = _get_session_factory()
    gen_db = SessionFactory()
    job_uuid = None

    def _write_heartbeat(event: dict) -> None:
        if job_uuid is None:
            return
        hb_db = SessionFactory()
        try:
            run = hb_db.get(IngestionRun, job_uuid)
            if run is None:
                return
            metrics = dict(run.metrics or {})
            metrics["pipeline_progress"] = {
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
        if job_uuid is None:
            return
        st_db = SessionFactory()
        try:
            run = st_db.get(IngestionRun, job_uuid)
            if run is None:
                return
            run.status = status
            if error:
                run.error_summary = error
            if status == "completed":
                run.completed_at = datetime.now(timezone.utc)
            st_db.commit()
            logger.info("Pipeline background: set job %s → %s", str(job_uuid)[:8], status)
        except Exception:
            try:
                st_db.rollback()
            except Exception:
                pass
        finally:
            st_db.close()

    try:
        gen = _pipeline_generator(body, gen_db, registry)

        for sse_line in gen:
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
                        .where(IngestionRun.pipeline_mode == "full")
                        .order_by(IngestionRun.created_at.desc())
                    ).scalars().first()
                    if run:
                        job_uuid = run.id
                finally:
                    find_db.close()

            _write_heartbeat(event)

    except Exception as exc:
        logger.error("Pipeline background thread failed: %s", type(exc).__name__, exc_info=True)
        _update_run_status("failed", error=type(exc).__name__)
    finally:
        try:
            gen_db.close()
        except Exception:
            pass


def _pipeline_relay_generator(job_id: str, body: CreateJobBody, registry: ProtocolRegistry):
    """SSE relay for full pipeline — polls background thread progress."""
    db = get_db_session()

    try:
        job_uuid = UUID(job_id)
    except (ValueError, AttributeError):
        yield _sse({"stage": "error", "message": f"Invalid job_id: {job_id!r}"})
        db.close()
        return

    last_message = ""
    for _ in range(5400):  # Max 3 hours (5400 × 2s)
        time.sleep(2)

        try:
            db.expire_all()
            run = db.execute(
                select(IngestionRun).where(IngestionRun.id == job_uuid)
            ).scalar_one_or_none()
        except Exception:
            continue

        if run is None:
            # Job not created yet — background thread still starting
            # Also check latest full pipeline run
            run = db.execute(
                select(IngestionRun)
                .where(IngestionRun.pipeline_mode == "full")
                .order_by(IngestionRun.created_at.desc())
            ).scalars().first()
            if run is None:
                continue

        progress = (run.metrics or {}).get("pipeline_progress", {})
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

        if run.status == "completed":
            final = progress.get("result") or {"job_id": str(run.id), "status": "COMPLETE"}
            yield _sse({"stage": "complete", "result": final})
            break
        if run.status == "failed":
            yield _sse({"stage": "error", "message": progress.get("message", "Pipeline failed")})
            break

    db.close()


@router.post("/run/stream", summary="Submit job with streaming progress (SSE)")
def run_job_stream(
    body: CreateJobBody,
    registry: ProtocolRegistry = Depends(get_protocol_registry),
    db: Session = Depends(get_db),
):
    """Run the full pipeline with SSE streaming progress events.

    Pipeline runs in a background thread that survives browser disconnects.
    The SSE response is a thin relay that polls progress from the DB.
    """
    from app.db.models import IngestionRun
    from datetime import datetime, timezone

    job_id = str(uuid4())
    body.job_id = job_id

    # Create IngestionRun HERE to avoid race condition with relay
    settings = get_settings()
    source_directory = ""
    if body.upload_id:
        source_directory = str(Path(settings.upload_dir) / body.upload_id)
    elif body.source_directory:
        source_directory = body.source_directory

    project_uuid = None
    if body.project_id:
        try:
            project_uuid = UUID(body.project_id)
        except (ValueError, AttributeError):
            pass

    job_uuid = UUID(job_id)
    run = IngestionRun(
        id=job_uuid,
        project_id=project_uuid,
        source_path=source_directory,
        pipeline_mode=body.pipeline_mode or "full",
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

    t = threading.Thread(
        target=_run_pipeline_background,
        args=(body, registry),
        daemon=True,
        name=f"pipeline-{job_id[:8]}",
    )
    _pipeline_threads[job_id] = t
    t.start()

    return StreamingResponse(
        _pipeline_relay_generator(job_id, body, registry),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/analyze/stream", summary="Run analysis phase (two-phase pipeline)")
def analyze_stream(
    body: CreateJobBody,
    registry: ProtocolRegistry = Depends(get_protocol_registry),
    db: Session = Depends(get_db),
):
    """Run the analysis phase of the two-phase pipeline with SSE streaming.

    Analysis runs in a background thread that survives browser disconnects.
    The SSE response is a thin relay that polls progress from the DB.

    The IngestionRun record is created HERE (in the HTTP handler) so the
    relay can always find it — eliminates the race condition where the
    background thread hadn't committed yet.
    """
    from app.pipeline.two_phase import run_analysis_background, analysis_relay_generator, _analysis_threads
    from app.db.models import IngestionRun
    from datetime import datetime, timezone
    import threading

    # Generate a job_id and pass it in the body so analyze_generator uses it
    job_id = str(uuid4())
    body.job_id = job_id

    # Resolve source directory for the IngestionRun record
    settings = get_settings()
    source_directory = ""
    if body.upload_id:
        upload_path = Path(settings.upload_dir) / body.upload_id
        source_directory = str(upload_path)
    elif body.source_directory:
        source_directory = body.source_directory

    # Resolve project_id
    project_uuid = None
    if body.project_id:
        try:
            project_uuid = UUID(body.project_id)
        except (ValueError, AttributeError):
            pass

    # Create IngestionRun HERE so the relay can always find it
    job_uuid = UUID(job_id)
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

    # Start background analysis thread
    t = threading.Thread(
        target=run_analysis_background,
        args=(body, registry),
        daemon=True,
        name=f"analyze-{job_id[:8]}",
    )
    _analysis_threads[job_id] = t
    t.start()

    return StreamingResponse(
        analysis_relay_generator(job_id, body, registry),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{job_id}/extract/stream", summary="Run extraction phase (two-phase pipeline)")
def extract_stream(
    job_id: str,
    registry: ProtocolRegistry = Depends(get_protocol_registry),
):
    """Run or reconnect to the extraction phase with SSE streaming.

    Accepts status='analyzed' (start new) or 'extracting' (reconnect).
    Extraction runs in a background thread; this endpoint polls progress.
    """
    from app.pipeline.two_phase import extract_generator

    return StreamingResponse(
        extract_generator(job_id, None, registry),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("", summary="Submit a new extraction job")
def create_job(
    body: CreateJobBody,
    db: Session = Depends(get_db),
    registry: ProtocolRegistry = Depends(get_protocol_registry),
):
    job_id = body.job_id or str(uuid4())
    settings = get_settings()

    # Resolve source directory
    if body.upload_id:
        upload_path = Path(settings.upload_dir) / body.upload_id
        if not upload_path.is_dir():
            raise HTTPException(
                status_code=404,
                detail=f"Upload {body.upload_id!r} not found or expired",
            )
        source_directory = str(upload_path)
    else:
        source_directory = body.source_directory  # type: ignore[assignment]

    # 1. Load protocol
    try:
        protocol = registry.get(body.protocol_id)
    except KeyError:
        raise HTTPException(status_code=400, detail=f"Protocol not found: {body.protocol_id!r}")

    try:
        # 2. Discover documents
        connector = FilesystemConnector(source_directory)
        discovery = DiscoveryTask()
        docs = discovery.run([connector])

        # 3. Read + PII detect each document
        from app.pii.presidio_engine import PresidioEngine
        from app.readers.registry import get_reader

        engine = PresidioEngine()
        all_records = []

        for doc_info in docs:
            reader = get_reader(doc_info["source_path"])
            blocks = reader.read()
            detections = engine.analyze(blocks)

            for det in detections:
                rec = detection_to_pii_record(det, doc_info["source_path"])
                all_records.append(rec)

        # 4. Entity resolution
        resolver = EntityResolver()
        groups = resolver.resolve(all_records)

        # 5. Deduplication → NotificationSubjects
        dedup = Deduplicator(db)
        subjects = dedup.build_subjects(groups)

        # 6. Build notification list
        nl = build_notification_list(job_id, protocol, subjects, db)

        notif_count = sum(1 for s in subjects if s.notification_required)

        return {
            "job_id": job_id,
            "status": "COMPLETE",
            "subjects_found": len(subjects),
            "notification_required": notif_count,
        }

    except Exception as exc:
        logger.error("Job %s failed: %s", job_id, type(exc).__name__)
        raise HTTPException(status_code=500, detail="Job processing failed")

    finally:
        # Clean up upload directory after job completes or fails
        if body.upload_id:
            upload_dir = Path(settings.upload_dir) / body.upload_id
            shutil.rmtree(upload_dir, ignore_errors=True)


@router.get("/{job_id}", summary="Get job status")
def get_job(job_id: str, db: Session = Depends(get_db)):
    nl = db.execute(
        select(NotificationList).where(NotificationList.job_id == job_id)
    ).scalar_one_or_none()

    if nl is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    return {
        "job_id": nl.job_id,
        "protocol_id": nl.protocol_id,
        "status": nl.status,
        "subject_count": len(nl.subject_ids) if nl.subject_ids else 0,
        "created_at": nl.created_at.isoformat() if nl.created_at else None,
    }


@router.get("/{job_id}/failed-docs", summary="List docs from a job that produced no subjects or failed")
def list_failed_docs(job_id: UUID, db: Session = Depends(get_db)):
    """Return docs from *job_id* that need a rerun.

    A doc is "failed" if:
      - Its `metadata_json.extraction_status` is "failed" or missing after a
        completed run, OR
      - It produced zero NotificationSubjects despite segregation flagging PII

    Used by the "Re-run failed" UI action (Step 39 #5).
    """
    run = db.get(IngestionRun, job_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    docs = db.execute(
        select(Document).where(Document.ingestion_run_id == job_id)
    ).scalars().all()

    subjects = db.execute(
        select(NotificationSubject).where(NotificationSubject.ingestion_run_id == job_id)
    ).scalars().all()
    subj_by_doc: dict[str, int] = {}
    for s in subjects:
        key = s.source_document_name or ""
        subj_by_doc[key] = subj_by_doc.get(key, 0) + 1

    failed: list[dict] = []
    for d in docs:
        meta = d.metadata_json or {}
        ext_status = meta.get("extraction_status", "unknown")
        seg = meta.get("segregation", {}) if isinstance(meta.get("segregation"), dict) else {}
        has_pii = bool(seg.get("pii_detected", seg.get("contains_pii")))
        subj_count = subj_by_doc.get(d.file_name, 0)

        is_failed = (
            ext_status == "failed"
            or (run.status == "completed" and has_pii and subj_count == 0)
        )
        if not is_failed:
            continue
        failed.append({
            "id": str(d.id),
            "file_name": d.file_name,
            "source_path": d.source_path,
            "file_type": d.file_type,
            "subject_count": subj_count,
            "extraction_status": ext_status,
            "segregation_has_pii": has_pii,
        })
    return {"job_id": str(job_id), "failed_count": len(failed), "docs": failed}


@router.get("/{job_id}/results", summary="Get job extraction results (masked)")
def get_job_results(job_id: str, db: Session = Depends(get_db)):
    nl = db.execute(
        select(NotificationList).where(NotificationList.job_id == job_id)
    ).scalar_one_or_none()

    if nl is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    subjects = get_notification_subjects(nl, db)
    return [_masked_subject(s, db=db) for s in subjects]


@router.get("/{job_id}/status", summary="Get job pipeline status with per-stage breakdown")
def get_job_status(job_id: UUID, db: Session = Depends(get_db)):
    """Return current pipeline status including per-stage progress.

    The 8-stage pipeline: Discovery, Cataloging, PII Detection, PII Extraction,
    Normalization, Entity Resolution, Quality Assurance, Notification.
    """
    run = db.execute(
        select(IngestionRun).where(IngestionRun.id == job_id)
    ).scalar_one_or_none()

    if run is None:
        raise HTTPException(status_code=404, detail=f"Job {str(job_id)!r} not found")

    # Build per-stage status from metrics JSON
    stages = _build_stage_status(run)

    # Calculate overall progress percentage
    completed_stages = sum(1 for s in stages if s["status"] == "completed")
    total_stages = len(stages)
    progress_pct = round((completed_stages / total_stages) * 100, 1) if total_stages else 0.0

    # Determine current stage
    current_stage: str | None = None
    for s in stages:
        if s["status"] == "running":
            current_stage = s["name"]
            break
    if current_stage is None and run.status == "pending":
        current_stage = stages[0]["name"] if stages else None

    return {
        "id": str(run.id),
        "status": run.status,
        "project_id": str(run.project_id) if run.project_id else None,
        "current_stage": current_stage,
        "progress_pct": progress_pct,
        "stages": stages,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "error_summary": run.error_summary,
        "metrics": run.metrics or {},
    }


@router.post("/{job_id}/cancel", summary="Cancel a running or pending job")
def cancel_job(job_id: UUID, db: Session = Depends(get_db)):
    """Set a running or pending job to cancelled status."""
    run = db.execute(
        select(IngestionRun).where(IngestionRun.id == job_id)
    ).scalar_one_or_none()

    if run is None:
        raise HTTPException(status_code=404, detail=f"Job {str(job_id)!r} not found")

    cancellable = {"pending", "running", "analyzing", "extracting"}
    if run.status not in cancellable:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot cancel job with status {run.status!r}",
        )

    from datetime import datetime, timezone
    run.status = "cancelled"
    run.completed_at = datetime.now(timezone.utc)
    db.flush()

    return _ingestion_run_summary(run, db)


@router.delete("/{job_id}", summary="Soft-delete (archive) a job")
def archive_job(job_id: UUID, db: Session = Depends(get_db)):
    """Set a completed/failed/cancelled job to archived status (soft delete)."""
    run = db.execute(
        select(IngestionRun).where(IngestionRun.id == job_id)
    ).scalar_one_or_none()

    if run is None:
        raise HTTPException(status_code=404, detail=f"Job {str(job_id)!r} not found")

    archivable = {"completed", "failed", "cancelled", "analyzed"}
    if run.status not in archivable:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot archive job with status {run.status!r}",
        )

    run.status = "archived"
    db.flush()

    return _ingestion_run_summary(run, db)


@router.delete("/{job_id}/purge", summary="Purge job data (extractions, subjects, gaps)")
def purge_job_data(job_id: UUID, db: Session = Depends(get_db)):
    """Delete heavy data tables for a job to free DB space.

    Removes: extractions, notification_subjects, gap files on disk.
    Keeps: ingestion_run metadata, documents (with metadata_json cleared).
    Only works on archived/completed/failed/cancelled jobs.
    """
    run = db.execute(
        select(IngestionRun).where(IngestionRun.id == job_id)
    ).scalar_one_or_none()

    if run is None:
        raise HTTPException(status_code=404, detail=f"Job {str(job_id)!r} not found")

    purgeable = {"completed", "failed", "cancelled", "archived"}
    if run.status not in purgeable:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot purge job with status {run.status!r} — must be completed/failed/cancelled/archived",
        )

    # Count before purge
    doc_ids = [
        d.id for d in db.execute(
            select(Document).where(Document.ingestion_run_id == job_id)
        ).scalars().all()
    ]

    # 1. Delete extractions
    ext_deleted = 0
    if doc_ids:
        from sqlalchemy import delete
        result = db.execute(
            delete(Extraction).where(Extraction.document_id.in_(doc_ids))
        )
        ext_deleted = result.rowcount

    # 2. Delete notification subjects for this project
    ns_deleted = 0
    if run.project_id:
        result = db.execute(
            delete(NotificationSubject).where(
                NotificationSubject.project_id == run.project_id
            )
        )
        ns_deleted = result.rowcount

    # 3. Clear extracted_records from document metadata (heavy JSON)
    docs_cleared = 0
    for doc in db.execute(
        select(Document).where(Document.ingestion_run_id == job_id)
    ).scalars().all():
        if doc.metadata_json and "extracted_records" in (doc.metadata_json or {}):
            meta = dict(doc.metadata_json)
            meta.pop("extracted_records", None)
            doc.metadata_json = meta
            docs_cleared += 1

    # 4. Delete gap files on disk
    gap_file_deleted = False
    if run.project_id:
        import os
        gap_path = f"data/projects/{run.project_id}/gaps/{job_id}.json"
        if os.path.exists(gap_path):
            os.remove(gap_path)
            gap_file_deleted = True

    db.commit()

    return {
        "status": "purged",
        "job_id": str(job_id),
        "extractions_deleted": ext_deleted,
        "subjects_deleted": ns_deleted,
        "docs_metadata_cleared": docs_cleared,
        "gap_file_deleted": gap_file_deleted,
    }


@router.patch("/{job_id}", summary="Update job (e.g. link to a project)")
def patch_job(job_id: UUID, body: PatchJobBody, db: Session = Depends(get_db)):
    """Associate an existing job with a project.

    Returns 404 if job or project not found.
    Returns 409 if the job is already linked to a different project.
    """
    run = db.execute(
        select(IngestionRun).where(IngestionRun.id == job_id)
    ).scalar_one_or_none()

    if run is None:
        raise HTTPException(status_code=404, detail=f"Job {str(job_id)!r} not found")

    # Validate project exists
    try:
        project_uuid = UUID(body.project_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="Invalid project_id format")

    project = db.get(Project, project_uuid)
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project {body.project_id!r} not found")

    # Check if already linked to a different project
    if run.project_id is not None and run.project_id != project_uuid:
        raise HTTPException(
            status_code=409,
            detail=f"Job is already linked to project {run.project_id!s}",
        )

    run.project_id = project.id
    db.flush()

    return _ingestion_run_summary(run, db)


# ---------------------------------------------------------------------------
# Pipeline stage definitions and helpers
# ---------------------------------------------------------------------------

PIPELINE_STAGES = [
    "Discovery",
    "Cataloging",
    "PII Detection",
    "PII Extraction",
    "Normalization",
    "Entity Resolution",
    "Quality Assurance",
    "Notification",
]


def _build_stage_status(run: IngestionRun) -> list[dict]:
    """Build per-stage status list from an IngestionRun's metrics JSON.

    The ``metrics`` JSON field can contain a ``stages`` dict with keys
    matching the stage names and values being dicts with ``status``,
    ``started_at``, ``completed_at``, and optional ``error_count``.

    If no metrics are stored yet, stage status is inferred from the
    overall run status.
    """
    metrics = run.metrics or {}
    stage_data = metrics.get("stages", {})

    result = []
    run_completed = run.status in ("completed", "failed")

    for stage_name in PIPELINE_STAGES:
        info = stage_data.get(stage_name, {})
        stage_status = info.get("status", "pending")

        # If the overall run is completed/failed and we have no per-stage data,
        # mark all stages as completed (for backward compat with jobs that
        # don't store per-stage metrics).
        if not stage_data and run_completed:
            if run.status == "completed":
                stage_status = "completed"
            else:
                stage_status = "failed"

        result.append({
            "name": stage_name,
            "status": stage_status,
            "started_at": info.get("started_at"),
            "completed_at": info.get("completed_at"),
            "error_count": info.get("error_count", 0),
        })

    return result


def _ingestion_run_summary(run: IngestionRun, db: Session) -> dict:
    """Build a summary dict for an ingestion run."""
    doc_count = db.execute(
        select(sqla_func.count(Document.id)).where(Document.ingestion_run_id == run.id)
    ).scalar() or 0

    # First document filename for display
    first_doc = db.execute(
        select(Document.file_name)
        .where(Document.ingestion_run_id == run.id)
        .order_by(Document.created_at)
        .limit(1)
    ).scalar_one_or_none()

    # Duration: use analysis_completed_at for analyzed jobs, completed_at otherwise
    duration_seconds: float | None = None
    if run.started_at:
        end = run.completed_at or run.analysis_completed_at
        if end:
            duration_seconds = (end - run.started_at).total_seconds()

    return {
        "id": str(run.id),
        "project_id": str(run.project_id) if run.project_id else None,
        "status": run.status,
        "source_path": run.source_path,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "analysis_completed_at": run.analysis_completed_at.isoformat() if run.analysis_completed_at else None,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "document_count": doc_count,
        "first_file_name": first_doc,
        "duration_seconds": duration_seconds,
        "pipeline_mode": run.pipeline_mode,
        "error_summary": run.error_summary,
    }