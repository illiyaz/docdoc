"""Dashboard summary endpoint.

GET /dashboard/summary — aggregated stats, attention items, running jobs,
                         active projects, and recent activity.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.db.models import (
    Document,
    DocumentAnalysisReview,
    ExportJob,
    IngestionRun,
    Project,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iso(dt: datetime | None) -> str | None:
    """Return ISO-8601 string or None."""
    return dt.isoformat() if dt else None


# ---------------------------------------------------------------------------
# GET /dashboard/summary
# ---------------------------------------------------------------------------

@router.get("/summary", summary="Dashboard summary")
def dashboard_summary(db: Session = Depends(get_db)):
    now = datetime.now(timezone.utc)
    one_week_ago = now - timedelta(days=7)

    # ------------------------------------------------------------------
    # 1. stats
    # ------------------------------------------------------------------
    active_projects = db.execute(
        select(func.count(Project.id)).where(Project.status == "active")
    ).scalar() or 0

    pending_reviews = db.execute(
        select(func.count(DocumentAnalysisReview.id)).where(
            DocumentAnalysisReview.status == "pending_review"
        )
    ).scalar() or 0

    jobs_this_week = db.execute(
        select(func.count(IngestionRun.id)).where(
            IngestionRun.created_at >= one_week_ago
        )
    ).scalar() or 0

    documents_processed = db.execute(
        select(func.count(Document.id)).where(Document.status != "discovered")
    ).scalar() or 0

    stats = {
        "active_projects": active_projects,
        "pending_reviews": pending_reviews,
        "jobs_this_week": jobs_this_week,
        "documents_processed": documents_processed,
    }

    # ------------------------------------------------------------------
    # 2. needs_attention — projects with pending document reviews
    # ------------------------------------------------------------------
    needs_attention_q = (
        select(
            Project.id.label("project_id"),
            Project.name.label("project_name"),
            func.count(DocumentAnalysisReview.id).label("pending_count"),
            func.min(DocumentAnalysisReview.created_at).label("oldest_pending_at"),
        )
        .join(IngestionRun, IngestionRun.project_id == Project.id)
        .join(
            DocumentAnalysisReview,
            DocumentAnalysisReview.ingestion_run_id == IngestionRun.id,
        )
        .where(DocumentAnalysisReview.status == "pending_review")
        .group_by(Project.id, Project.name)
        .order_by(func.min(DocumentAnalysisReview.created_at).asc())
    )
    needs_attention_rows = db.execute(needs_attention_q).all()
    needs_attention = [
        {
            "project_id": str(row.project_id),
            "project_name": row.project_name,
            "pending_count": row.pending_count,
            "oldest_pending_at": _iso(row.oldest_pending_at),
        }
        for row in needs_attention_rows
    ]

    # ------------------------------------------------------------------
    # 3. running_jobs — in-progress ingestion runs
    # ------------------------------------------------------------------
    running_runs = db.execute(
        select(IngestionRun)
        .where(IngestionRun.status.in_(["running", "analyzing", "extracting"]))
        .order_by(IngestionRun.started_at.desc())
    ).scalars().all()

    running_jobs = []
    for run in running_runs:
        total_docs = db.execute(
            select(func.count(Document.id)).where(
                Document.ingestion_run_id == run.id
            )
        ).scalar() or 0

        processed_docs = db.execute(
            select(func.count(Document.id)).where(
                Document.ingestion_run_id == run.id,
                Document.status != "discovered",
            )
        ).scalar() or 0

        progress_pct = round((processed_docs / total_docs * 100), 1) if total_docs > 0 else 0.0

        # Resolve project name
        project_name: str | None = None
        if run.project_id:
            proj = db.get(Project, run.project_id)
            if proj:
                project_name = proj.name

        running_jobs.append({
            "job_id": str(run.id),
            "project_id": str(run.project_id) if run.project_id else None,
            "project_name": project_name,
            "status": run.status,
            "progress_pct": progress_pct,
            "document_count": total_docs,
            "started_at": _iso(run.started_at),
        })

    # ------------------------------------------------------------------
    # 4. active_projects — with summary stats, ordered by last activity
    # ------------------------------------------------------------------
    # Subquery: document count per project
    doc_count_sq = (
        select(
            IngestionRun.project_id.label("project_id"),
            func.count(Document.id).label("document_count"),
        )
        .join(Document, Document.ingestion_run_id == IngestionRun.id)
        .where(IngestionRun.project_id.isnot(None))
        .group_by(IngestionRun.project_id)
        .subquery()
    )

    # Subquery: pending reviews per project
    pending_sq = (
        select(
            IngestionRun.project_id.label("project_id"),
            func.count(DocumentAnalysisReview.id).label("pending_reviews"),
        )
        .join(
            DocumentAnalysisReview,
            DocumentAnalysisReview.ingestion_run_id == IngestionRun.id,
        )
        .where(
            IngestionRun.project_id.isnot(None),
            DocumentAnalysisReview.status == "pending_review",
        )
        .group_by(IngestionRun.project_id)
        .subquery()
    )

    # Subquery: completed jobs per project
    completed_sq = (
        select(
            IngestionRun.project_id.label("project_id"),
            func.count(IngestionRun.id).label("completed_jobs"),
        )
        .where(
            IngestionRun.project_id.isnot(None),
            IngestionRun.status == "completed",
        )
        .group_by(IngestionRun.project_id)
        .subquery()
    )

    # Subquery: last activity (most recent ingestion_run updated_at) per project
    last_activity_sq = (
        select(
            IngestionRun.project_id.label("project_id"),
            func.max(IngestionRun.updated_at).label("last_activity_at"),
        )
        .where(IngestionRun.project_id.isnot(None))
        .group_by(IngestionRun.project_id)
        .subquery()
    )

    active_projects_q = (
        select(
            Project.id,
            Project.name,
            Project.status,
            func.coalesce(doc_count_sq.c.document_count, 0).label("document_count"),
            last_activity_sq.c.last_activity_at,
            func.coalesce(pending_sq.c.pending_reviews, 0).label("pending_reviews"),
            func.coalesce(completed_sq.c.completed_jobs, 0).label("completed_jobs"),
        )
        .outerjoin(doc_count_sq, doc_count_sq.c.project_id == Project.id)
        .outerjoin(pending_sq, pending_sq.c.project_id == Project.id)
        .outerjoin(completed_sq, completed_sq.c.project_id == Project.id)
        .outerjoin(last_activity_sq, last_activity_sq.c.project_id == Project.id)
        .where(Project.status == "active")
        .order_by(
            func.coalesce(last_activity_sq.c.last_activity_at, Project.created_at).desc()
        )
    )

    active_projects_rows = db.execute(active_projects_q).all()
    active_projects_list = [
        {
            "id": str(row.id),
            "name": row.name,
            "status": row.status,
            "document_count": row.document_count,
            "last_activity_at": _iso(row.last_activity_at),
            "pending_reviews": row.pending_reviews,
            "completed_jobs": row.completed_jobs,
        }
        for row in active_projects_rows
    ]

    # ------------------------------------------------------------------
    # 5. recent_activity — last 20 events from multiple sources
    # ------------------------------------------------------------------
    activity_items: list[dict] = []

    # 5a. Completed jobs
    completed_runs = db.execute(
        select(IngestionRun, Project.name.label("project_name"))
        .outerjoin(Project, Project.id == IngestionRun.project_id)
        .where(IngestionRun.status == "completed")
        .order_by(IngestionRun.completed_at.desc())
        .limit(20)
    ).all()
    for row in completed_runs:
        run = row[0]
        project_name = row.project_name
        activity_items.append({
            "type": "job_completed",
            "project_name": project_name,
            "detail": f"Job completed: {run.source_path}",
            "timestamp": _iso(run.completed_at or run.updated_at),
        })

    # 5b. Document approvals / rejections
    reviewed_docs = db.execute(
        select(
            DocumentAnalysisReview,
            IngestionRun.project_id.label("project_id"),
        )
        .join(IngestionRun, IngestionRun.id == DocumentAnalysisReview.ingestion_run_id)
        .where(DocumentAnalysisReview.status.in_(["approved", "rejected"]))
        .order_by(DocumentAnalysisReview.reviewed_at.desc())
        .limit(20)
    ).all()
    # Build a set of project_ids we need names for
    project_ids_needed = {row.project_id for row in reviewed_docs if row.project_id}
    project_name_map: dict[str, str] = {}
    if project_ids_needed:
        name_rows = db.execute(
            select(Project.id, Project.name).where(Project.id.in_(project_ids_needed))
        ).all()
        project_name_map = {str(r.id): r.name for r in name_rows}

    for row in reviewed_docs:
        dar = row[0]
        proj_name = project_name_map.get(str(row.project_id)) if row.project_id else None
        activity_items.append({
            "type": "document_reviewed",
            "project_name": proj_name,
            "detail": f"Document {dar.status} by {dar.reviewer_id or 'auto'}",
            "timestamp": _iso(dar.reviewed_at or dar.created_at),
        })

    # 5c. Export generations
    completed_exports = db.execute(
        select(ExportJob, Project.name.label("project_name"))
        .outerjoin(Project, Project.id == ExportJob.project_id)
        .where(ExportJob.status == "completed")
        .order_by(ExportJob.completed_at.desc())
        .limit(20)
    ).all()
    for row in completed_exports:
        ej = row[0]
        activity_items.append({
            "type": "export_completed",
            "project_name": row.project_name,
            "detail": f"Export completed ({ej.export_type}, {ej.row_count or 0} rows)",
            "timestamp": _iso(ej.completed_at or ej.created_at),
        })

    # 5d. Project creations
    recent_projects = db.execute(
        select(Project)
        .order_by(Project.created_at.desc())
        .limit(20)
    ).scalars().all()
    for proj in recent_projects:
        activity_items.append({
            "type": "project_created",
            "project_name": proj.name,
            "detail": f"Project created: {proj.name}",
            "timestamp": _iso(proj.created_at),
        })

    # Sort all activity items by timestamp descending, take top 20
    activity_items.sort(
        key=lambda x: x["timestamp"] or "",
        reverse=True,
    )
    recent_activity = activity_items[:20]

    # ------------------------------------------------------------------
    # Assemble response
    # ------------------------------------------------------------------
    return {
        "stats": stats,
        "needs_attention": needs_attention,
        "running_jobs": running_jobs,
        "active_projects": active_projects_list,
        "recent_activity": recent_activity,
    }
