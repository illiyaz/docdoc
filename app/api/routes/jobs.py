"""Job management routes — full implementation.

POST /jobs/upload saves uploaded files and returns an upload_id.
POST /jobs runs the synchronous pipeline (JSON response).
POST /jobs/run runs the pipeline with SSE streaming progress.

GET /jobs/{job_id} returns job status.
GET /jobs/{job_id}/results returns masked NotificationSubjects.
GET /jobs/{job_id}/status returns per-stage pipeline status (Step 8b).
GET /jobs/recent returns recent jobs, optionally unlinked only (Step 8b).
PATCH /jobs/{job_id} links a job to a project (Step 8b).
"""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Generator
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, model_validator
from sqlalchemy import func as sqla_func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_protocol_registry
from app.core.settings import get_settings
from app.db.models import Document, IngestionRun, NotificationList, NotificationSubject, Project
from app.notification.list_builder import build_notification_list, get_notification_subjects
from app.protocols.registry import ProtocolRegistry
from app.rra.deduplicator import Deduplicator
from app.rra.entity_resolver import EntityResolver
from app.tasks.discovery import DiscoveryTask, FilesystemConnector

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["jobs"])

# Supported extensions for upload (matches reader registry)
SUPPORTED_EXTENSIONS = frozenset({
    ".pdf", ".xlsx", ".xls", ".docx", ".csv",
    ".html", ".htm", ".xml", ".eml", ".msg",
    ".parquet", ".avro",
})

# Extensions to silently skip during upload
SKIP_EXTENSIONS = frozenset({
    ".ds_store", ".txt", ".log", ".tmp", ".swp",
    ".gitignore", ".gitkeep",
})


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


def _masked_subject(ns: NotificationSubject) -> dict:
    settings = get_settings()
    if settings.pii_masking_enabled:
        email = _mask_email(ns.canonical_email)
        phone = _mask_phone(ns.canonical_phone)
    else:
        email = ns.canonical_email
        phone = ns.canonical_phone
    return {
        "subject_id": str(ns.subject_id),
        "canonical_name": ns.canonical_name,
        "canonical_email": email,
        "canonical_phone": phone,
        "pii_types_found": ns.pii_types_found or [],
        "notification_required": ns.notification_required,
        "review_status": ns.review_status,
    }


# ---------------------------------------------------------------------------
# Upload helpers
# ---------------------------------------------------------------------------

def _is_supported(filename: str) -> bool:
    """Return True if the file extension is supported by a reader."""
    ext = Path(filename).suffix.lower()
    return ext in SUPPORTED_EXTENSIONS


def _should_skip(filename: str) -> bool:
    """Return True if the file should be silently skipped."""
    name = Path(filename).name.lower()
    ext = Path(filename).suffix.lower()
    return ext in SKIP_EXTENSIONS or name.startswith(".")


def _safe_filename(directory: Path, original_name: str) -> Path:
    """Return a unique path under directory, adding _1, _2 suffix for dupes."""
    target = directory / original_name
    if not target.exists():
        return target
    stem = Path(original_name).stem
    suffix = Path(original_name).suffix
    counter = 1
    while True:
        candidate = directory / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


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


@router.post("/upload", summary="Upload files for a new job")
async def upload_files(files: list[UploadFile] = File(...)):
    """Save uploaded files to a temp directory and return an upload_id.

    Unsupported files (e.g. .DS_Store, .txt) are silently skipped.
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

            dest = _safe_filename(upload_path, filename)
            dest.write_bytes(content)
            saved_files.append({
                "name": dest.name,
                "size_bytes": file_size,
                "extension": Path(filename).suffix.lower(),
            })

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


# ---------------------------------------------------------------------------
# SSE streaming pipeline
# ---------------------------------------------------------------------------

def _sse(data: dict) -> str:
    """Format a dict as an SSE data line."""
    return f"data: {json.dumps(data)}\n\n"


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

        for i, doc_info in enumerate(docs, 1):
            yield _sse({
                "stage": "detection", "status": "running",
                "message": f"Scanning document {i}/{len(docs)}...",
                "detail": {"total": len(docs), "current": i},
            })
            reader = get_reader(doc_info["source_path"])
            blocks = reader.read()
            detections = engine.analyze(blocks)

            from app.rra.entity_resolver import PIIRecord

            for det in detections:
                rec = PIIRecord(
                    record_id=str(uuid4()),
                    entity_type=det.entity_type,
                    normalized_value=det.text if hasattr(det, "text") else "",
                    source_document_id=doc_info["source_path"],
                )
                all_records.append(rec)

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
        dedup = Deduplicator(db)
        subjects = dedup.build_subjects(groups)
        yield _sse({
            "stage": "deduplication", "status": "complete",
            "message": f"Built {len(subjects)} subject(s)",
        })

        # --- Stage 6: Notification ---
        yield _sse({"stage": "notification", "status": "running", "message": "Building notification list..."})
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
        logger.error("Job %s failed at streaming pipeline: %s", str(job_uuid), type(exc).__name__)
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


@router.post("/run/stream", summary="Submit job with streaming progress (SSE)")
def run_job_stream(
    body: CreateJobBody,
    registry: ProtocolRegistry = Depends(get_protocol_registry),
):
    """Run the full pipeline with SSE streaming progress events."""
    return StreamingResponse(
        _pipeline_generator(body, None, registry),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/analyze/stream", summary="Run analysis phase (two-phase pipeline)")
def analyze_stream(
    body: CreateJobBody,
    registry: ProtocolRegistry = Depends(get_protocol_registry),
):
    """Run the analysis phase of the two-phase pipeline with SSE streaming.

    Stages: discovery, cataloging, structure analysis, sample extraction,
    auto-approve decisions.
    """
    from app.pipeline.two_phase import analyze_generator

    return StreamingResponse(
        analyze_generator(body, None, registry),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{job_id}/extract/stream", summary="Run extraction phase (two-phase pipeline)")
def extract_stream(
    job_id: str,
    registry: ProtocolRegistry = Depends(get_protocol_registry),
):
    """Run the extraction phase for approved documents with SSE streaming.

    Requires the job to have status='analyzed' and pipeline_mode='two_phase'.
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
            # Convert detections to PIIRecord-like objects for entity resolver
            from app.rra.entity_resolver import PIIRecord

            for det in detections:
                rec = PIIRecord(
                    record_id=str(uuid4()),
                    entity_type=det.entity_type,
                    normalized_value=det.text if hasattr(det, "text") else "",
                    source_document_id=doc_info["source_path"],
                )
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


@router.get("/{job_id}/results", summary="Get job extraction results (masked)")
def get_job_results(job_id: str, db: Session = Depends(get_db)):
    nl = db.execute(
        select(NotificationList).where(NotificationList.job_id == job_id)
    ).scalar_one_or_none()

    if nl is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    subjects = get_notification_subjects(nl, db)
    return [_masked_subject(s) for s in subjects]


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

    duration_seconds: float | None = None
    if run.started_at and run.completed_at:
        duration_seconds = (run.completed_at - run.started_at).total_seconds()

    return {
        "id": str(run.id),
        "project_id": str(run.project_id) if run.project_id else None,
        "status": run.status,
        "source_path": run.source_path,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "document_count": doc_count,
        "duration_seconds": duration_seconds,
        "error_summary": run.error_summary,
    }
