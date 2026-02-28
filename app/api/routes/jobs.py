"""Job management routes — full implementation.

POST /jobs/upload saves uploaded files and returns an upload_id.
POST /jobs runs the synchronous pipeline (JSON response).
POST /jobs/run runs the pipeline with SSE streaming progress.

GET /jobs/{job_id} returns job status.
GET /jobs/{job_id}/results returns masked NotificationSubjects.
"""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Generator
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_protocol_registry
from app.core.settings import get_settings
from app.db.models import NotificationList, NotificationSubject
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

    @model_validator(mode="after")
    def exactly_one_source(self):
        has_dir = self.source_directory is not None and self.source_directory.strip() != ""
        has_upload = self.upload_id is not None and self.upload_id.strip() != ""
        if has_dir and has_upload:
            raise ValueError("Provide either source_directory or upload_id, not both")
        if not has_dir and not has_upload:
            raise ValueError("Provide either source_directory or upload_id")
        return self


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
    job_id = body.job_id or str(uuid4())
    settings = get_settings()
    owns_db = False

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

        # --- Stage 2: PII Detection ---
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

        # --- Stage 3: Entity Resolution ---
        yield _sse({"stage": "resolution", "status": "running", "message": "Resolving entities..."})
        resolver = EntityResolver()
        groups = resolver.resolve(all_records)
        yield _sse({
            "stage": "resolution", "status": "complete",
            "message": f"Resolved into {len(groups)} group(s)",
        })

        # --- Stage 4: Deduplication ---
        yield _sse({"stage": "deduplication", "status": "running", "message": "Building notification subjects..."})
        dedup = Deduplicator(db)
        subjects = dedup.build_subjects(groups)
        yield _sse({
            "stage": "deduplication", "status": "complete",
            "message": f"Built {len(subjects)} subject(s)",
        })

        # --- Stage 5: Notification ---
        yield _sse({"stage": "notification", "status": "running", "message": "Building notification list..."})
        nl = build_notification_list(job_id, protocol, subjects, db)
        notif_count = sum(1 for s in subjects if s.notification_required)
        yield _sse({
            "stage": "notification", "status": "complete",
            "message": f"{notif_count} notification(s) required",
        })

        # --- Complete ---
        yield _sse({
            "stage": "complete",
            "result": {
                "job_id": job_id,
                "status": "COMPLETE",
                "subjects_found": len(subjects),
                "notification_required": notif_count,
            },
        })

    except Exception as exc:
        logger.error("Job %s failed at streaming pipeline: %s", job_id, type(exc).__name__)
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


@router.post("/run", summary="Submit job with streaming progress (SSE)")
def run_job(
    body: CreateJobBody,
    registry: ProtocolRegistry = Depends(get_protocol_registry),
):
    return StreamingResponse(
        _pipeline_generator(body, None, registry),
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
