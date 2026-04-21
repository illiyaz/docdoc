"""Segregation Review endpoints (Step 30e-3).

Provides CRUD-like operations on document groups produced by the
segregation engine + grouping pipeline.  The auditor reviews these
groups before extraction begins.

GET  /projects/{project_id}/segregation/groups      — list all groups for a job
POST /projects/{project_id}/segregation/groups/{id}/approve  — approve a group
POST /projects/{project_id}/segregation/groups/{id}/reject   — reject a group
POST /projects/{project_id}/segregation/groups/{id}/reclassify — reclassify a group
POST /projects/{project_id}/segregation/approve-all          — bulk approve all pending groups
POST /projects/{project_id}/segregation/run                  — run segregation on a job's documents
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.settings import get_settings
from app.db.models import IngestionRun, Document

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/projects/{project_id}/segregation", tags=["segregation"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class RunSegregationBody(BaseModel):
    """Request to run segregation on a job's documents."""
    job_id: str
    sample_size: int = 5


class ApproveGroupBody(BaseModel):
    """Approve a segregation group."""
    reviewer_id: str = "auditor"
    rationale: str | None = None


class RejectGroupBody(BaseModel):
    """Reject a segregation group."""
    reviewer_id: str = "auditor"
    rationale: str | None = None


class ReclassifyGroupBody(BaseModel):
    """Reclassify a group (change document_type or PII status)."""
    reviewer_id: str = "auditor"
    new_document_type: str | None = None
    new_is_pii: bool | None = None
    rationale: str | None = None


class BulkApproveBody(BaseModel):
    """Bulk approve all pending groups."""
    reviewer_id: str = "auditor"
    rationale: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_segregation_path(project_id: str, job_id: str) -> Path:
    """Return path to the segregation results JSON for a job."""
    settings = get_settings()
    seg_dir = Path(settings.upload_dir).parent / "segregation"
    seg_dir.mkdir(parents=True, exist_ok=True)
    return seg_dir / f"{project_id}_{job_id}_groups.json"


def _load_groups(project_id: str, job_id: str) -> list[dict]:
    """Load segregation groups from disk."""
    path = _get_segregation_path(project_id, job_id)
    if not path.exists():
        return []
    try:
        with open(path) as f:
            data = json.load(f)
        return data.get("groups", [])
    except Exception as e:
        logger.warning("Failed to load segregation groups: %s", e)
        return []


def _save_groups(project_id: str, job_id: str, groups: list[dict]) -> None:
    """Save segregation groups to disk."""
    path = _get_segregation_path(project_id, job_id)
    data = {
        "project_id": project_id,
        "job_id": job_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "groups": groups,
    }
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def _find_job_for_project(
    project_id: str, job_id: str | None, db: Session
) -> IngestionRun:
    """Find the latest job for a project, or a specific job."""
    try:
        proj_uuid = UUID(project_id)
    except ValueError:
        raise HTTPException(400, "Invalid project_id")

    if job_id:
        try:
            job_uuid = UUID(job_id)
        except ValueError:
            raise HTTPException(400, "Invalid job_id")
        run = db.get(IngestionRun, job_uuid)
        if not run:
            raise HTTPException(404, "Job not found")
        return run

    # Find latest job for project
    run = (
        db.query(IngestionRun)
        .filter(IngestionRun.project_id == proj_uuid)
        .order_by(IngestionRun.created_at.desc())
        .first()
    )
    if not run:
        raise HTTPException(404, "No jobs found for this project")
    return run


def _persist_correction(project_id: str, group: dict, action: str, body: dict) -> None:
    """Append segregation correction to JSONL for future few-shot learning."""
    settings = get_settings()
    corrections_dir = Path(settings.upload_dir).parent / "corrections"
    corrections_dir.mkdir(parents=True, exist_ok=True)

    record = {
        "project_id": project_id,
        "group_id": group.get("group_id"),
        "group_name": group.get("group_name"),
        "document_type": group.get("document_type"),
        "action": action,
        "corrected_at": datetime.now(timezone.utc).isoformat(),
        **body,
    }

    filepath = corrections_dir / f"{project_id}_segregation_corrections.jsonl"
    try:
        with open(filepath, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception as e:
        logger.warning("Failed to persist segregation correction: %s", e)


# ---------------------------------------------------------------------------
# POST /projects/{project_id}/segregation/run
# ---------------------------------------------------------------------------

@router.post("/run")
def run_segregation(
    project_id: str,
    body: RunSegregationBody,
    db: Session = Depends(get_db),
):
    """Run segregation on all documents in a job.

    Classifies each document via LLM vision/text, groups them,
    and persists groups for auditor review.
    """
    run = _find_job_for_project(project_id, body.job_id, db)

    # Get all documents for this job
    docs = (
        db.query(Document)
        .filter(Document.ingestion_run_id == run.id)
        .all()
    )
    if not docs:
        raise HTTPException(404, "No documents found for this job")

    # Run segregation engine
    try:
        from app.pipeline.segregation import SegregationEngine
        from app.pipeline.grouping import group_documents

        engine = SegregationEngine(db_session=db, project_id=project_id)
        file_paths = [d.source_path for d in docs if d.source_path]

        if not file_paths:
            raise HTTPException(400, "No document files found on disk")

        # Classify all files
        results = engine.classify_batch(file_paths)

        # Group results
        groups = group_documents(results, sample_size=body.sample_size)

        # Convert to dicts and persist
        group_dicts = [g.to_dict() for g in groups]
        _save_groups(project_id, str(run.id), group_dicts)

        return {
            "status": "completed",
            "job_id": str(run.id),
            "total_files": len(file_paths),
            "total_groups": len(group_dicts),
            "pii_groups": sum(1 for g in group_dicts if g.get("is_pii")),
            "non_pii_groups": sum(1 for g in group_dicts if not g.get("is_pii")),
            "groups": group_dicts,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Segregation failed: %s", e, exc_info=True)
        raise HTTPException(500, f"Segregation failed: {e}")


# ---------------------------------------------------------------------------
# GET /projects/{project_id}/segregation/groups
# ---------------------------------------------------------------------------

@router.get("/flat", summary="Project-wide flat segregation view across all jobs")
def list_flat(
    project_id: str,
    db: Session = Depends(get_db),
):
    """Return one row per file across every job in the project.

    Used by the Segregation tab to show a project-wide table without
    the auditor needing to drill into each job. Pulls directly from
    Document.metadata_json.segregation so it reflects live data.
    """
    try:
        proj_uuid = UUID(project_id)
    except ValueError:
        raise HTTPException(400, "Invalid project_id")

    from sqlalchemy import select as _select
    runs = db.execute(
        _select(IngestionRun)
        .where(IngestionRun.project_id == proj_uuid)
        .order_by(IngestionRun.created_at.desc())
    ).scalars().all()

    run_by_id = {r.id: r for r in runs}
    run_ids = list(run_by_id.keys())
    if not run_ids:
        return {"project_id": project_id, "rows": [], "summary": {
            "total_files": 0, "pii_files": 0, "non_pii_files": 0,
            "jobs": 0, "unique_doc_types": 0,
        }}

    docs = db.execute(
        _select(Document).where(Document.ingestion_run_id.in_(run_ids))
    ).scalars().all()

    rows: list[dict] = []
    pii_count = 0
    doc_types: set[str] = set()
    for d in docs:
        meta = d.metadata_json or {}
        seg = meta.get("segregation", {}) if isinstance(meta.get("segregation"), dict) else {}
        has_pii = seg.get("contains_pii") in (True, "true", "yes")
        if has_pii:
            pii_count += 1
        doc_type = seg.get("document_type") or d.doc_type or "unknown"
        doc_types.add(doc_type)
        run = run_by_id.get(d.ingestion_run_id)
        rows.append({
            "job_id": str(d.ingestion_run_id),
            "job_status": run.status if run else None,
            "job_created_at": run.created_at.isoformat() if (run and run.created_at) else None,
            "doc_id": str(d.id),
            "file_name": d.file_name,
            "file_type": d.file_type,
            "size_bytes": d.size_bytes,
            "document_type": doc_type,
            "contains_pii": has_pii,
            "field_count": len(seg.get("field_inventory", []) or []),
            "country_hint": seg.get("country_hint"),
            "confidence": seg.get("confidence"),
            "extraction_status": meta.get("extraction_status", "unknown"),
        })

    return {
        "project_id": project_id,
        "rows": rows,
        "summary": {
            "total_files": len(rows),
            "pii_files": pii_count,
            "non_pii_files": len(rows) - pii_count,
            "jobs": len(run_ids),
            "unique_doc_types": len(doc_types),
        },
    }


@router.get("/groups")
def list_groups(
    project_id: str,
    job_id: str | None = None,
    db: Session = Depends(get_db),
):
    """List segregation groups for auditor review."""
    run = _find_job_for_project(project_id, job_id, db)
    groups = _load_groups(project_id, str(run.id))

    # Summary stats
    pending = sum(1 for g in groups if g.get("status") == "pending_review")
    approved = sum(1 for g in groups if g.get("status") == "approved")
    rejected = sum(1 for g in groups if g.get("status") == "rejected")

    return {
        "job_id": str(run.id),
        "groups": groups,
        "summary": {
            "total_groups": len(groups),
            "pending_review": pending,
            "approved": approved,
            "rejected": rejected,
            "total_files": sum(g.get("file_count", 0) for g in groups),
        },
    }


# ---------------------------------------------------------------------------
# POST /projects/{project_id}/segregation/groups/{group_id}/approve
# ---------------------------------------------------------------------------

@router.post("/groups/{group_id}/approve")
def approve_group(
    project_id: str,
    group_id: str,
    body: ApproveGroupBody,
    job_id: str | None = None,
    db: Session = Depends(get_db),
):
    """Approve a segregation group for extraction."""
    run = _find_job_for_project(project_id, job_id, db)
    groups = _load_groups(project_id, str(run.id))

    found = False
    for g in groups:
        if g.get("group_id") == group_id:
            g["status"] = "approved"
            g["reviewed_by"] = body.reviewer_id
            g["reviewed_at"] = datetime.now(timezone.utc).isoformat()
            g["review_rationale"] = body.rationale
            found = True
            break

    if not found:
        raise HTTPException(404, "Group not found")

    _save_groups(project_id, str(run.id), groups)
    return {"status": "approved", "group_id": group_id}


# ---------------------------------------------------------------------------
# POST /projects/{project_id}/segregation/groups/{group_id}/reject
# ---------------------------------------------------------------------------

@router.post("/groups/{group_id}/reject")
def reject_group(
    project_id: str,
    group_id: str,
    body: RejectGroupBody,
    job_id: str | None = None,
    db: Session = Depends(get_db),
):
    """Reject a segregation group (excluded from extraction)."""
    run = _find_job_for_project(project_id, job_id, db)
    groups = _load_groups(project_id, str(run.id))

    found = False
    for g in groups:
        if g.get("group_id") == group_id:
            g["status"] = "rejected"
            g["reviewed_by"] = body.reviewer_id
            g["reviewed_at"] = datetime.now(timezone.utc).isoformat()
            g["review_rationale"] = body.rationale
            found = True
            _persist_correction(project_id, g, "reject", body.model_dump())
            break

    if not found:
        raise HTTPException(404, "Group not found")

    _save_groups(project_id, str(run.id), groups)
    return {"status": "rejected", "group_id": group_id}


# ---------------------------------------------------------------------------
# POST /projects/{project_id}/segregation/groups/{group_id}/reclassify
# ---------------------------------------------------------------------------

@router.post("/groups/{group_id}/reclassify")
def reclassify_group(
    project_id: str,
    group_id: str,
    body: ReclassifyGroupBody,
    job_id: str | None = None,
    db: Session = Depends(get_db),
):
    """Reclassify a group — change document type or PII status."""
    run = _find_job_for_project(project_id, job_id, db)
    groups = _load_groups(project_id, str(run.id))

    found = False
    for g in groups:
        if g.get("group_id") == group_id:
            if body.new_document_type is not None:
                g["document_type"] = body.new_document_type
            if body.new_is_pii is not None:
                g["is_pii"] = body.new_is_pii
            g["reclassified_by"] = body.reviewer_id
            g["reclassified_at"] = datetime.now(timezone.utc).isoformat()
            g["reclassify_rationale"] = body.rationale
            found = True
            _persist_correction(project_id, g, "reclassify", body.model_dump())
            break

    if not found:
        raise HTTPException(404, "Group not found")

    _save_groups(project_id, str(run.id), groups)
    return {"status": "reclassified", "group_id": group_id}


# ---------------------------------------------------------------------------
# POST /projects/{project_id}/segregation/approve-all
# ---------------------------------------------------------------------------

@router.post("/approve-all")
def approve_all_groups(
    project_id: str,
    body: BulkApproveBody,
    job_id: str | None = None,
    db: Session = Depends(get_db),
):
    """Bulk approve all pending groups."""
    run = _find_job_for_project(project_id, job_id, db)
    groups = _load_groups(project_id, str(run.id))

    now = datetime.now(timezone.utc).isoformat()
    approved_count = 0
    for g in groups:
        if g.get("status") == "pending_review":
            g["status"] = "approved"
            g["reviewed_by"] = body.reviewer_id
            g["reviewed_at"] = now
            g["review_rationale"] = body.rationale
            approved_count += 1

    _save_groups(project_id, str(run.id), groups)

    return {
        "approved": approved_count,
        "total": len(groups),
        "already_approved": sum(1 for g in groups if g.get("status") == "approved") - approved_count,
    }
