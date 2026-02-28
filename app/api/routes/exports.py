"""Export management routes.

POST   /projects/{id}/exports               -- trigger CSV export
GET    /projects/{id}/exports               -- list export jobs
GET    /projects/{id}/exports/{eid}         -- get export job detail
GET    /projects/{id}/exports/{eid}/download -- download the CSV file
"""
from __future__ import annotations

import logging
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.db.models import ExportJob, Project
from app.export.csv_exporter import CSVExporter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects/{project_id}/exports", tags=["exports"])

# Default output directory for export files.
_DEFAULT_EXPORT_DIR = Path("/tmp/docdoc_exports")


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class CreateExportBody(BaseModel):
    protocol_config_id: str | None = None
    filters: dict | None = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("", summary="Trigger a CSV export for a project")
def create_export(
    project_id: UUID,
    body: CreateExportBody,
    db: Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    pc_id: UUID | None = None
    if body.protocol_config_id is not None:
        pc_id = UUID(body.protocol_config_id)

    output_dir = _DEFAULT_EXPORT_DIR / str(project_id)

    exporter = CSVExporter(db)
    export_job = exporter.run(
        project_id=project_id,
        output_dir=output_dir,
        protocol_config_id=pc_id,
        filters=body.filters,
    )
    return _export_job_dict(export_job)


@router.get("", summary="List export jobs for a project")
def list_exports(project_id: UUID, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    jobs = db.execute(
        select(ExportJob)
        .where(ExportJob.project_id == project_id)
        .order_by(ExportJob.created_at.desc())
    ).scalars().all()

    return [_export_job_dict(j) for j in jobs]


@router.get("/{export_id}", summary="Get export job detail")
def get_export(
    project_id: UUID,
    export_id: UUID,
    db: Session = Depends(get_db),
):
    job = _get_or_404(db, project_id, export_id)
    return _export_job_dict(job)


@router.get("/{export_id}/download", summary="Download the exported CSV file")
def download_export(
    project_id: UUID,
    export_id: UUID,
    db: Session = Depends(get_db),
):
    job = _get_or_404(db, project_id, export_id)

    if job.status != "completed":
        raise HTTPException(status_code=400, detail="Export is not yet completed")

    if not job.file_path:
        raise HTTPException(status_code=404, detail="Export file path not set")

    path = Path(job.file_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Export file not found on disk")

    return FileResponse(
        path=str(path),
        media_type="text/csv",
        filename=path.name,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_or_404(db: Session, project_id: UUID, export_id: UUID) -> ExportJob:
    job = db.execute(
        select(ExportJob).where(
            ExportJob.id == export_id,
            ExportJob.project_id == project_id,
        )
    ).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Export job not found")
    return job


def _export_job_dict(j: ExportJob) -> dict:
    return {
        "id": str(j.id),
        "project_id": str(j.project_id),
        "protocol_config_id": str(j.protocol_config_id) if j.protocol_config_id else None,
        "export_type": j.export_type,
        "status": j.status,
        "file_path": j.file_path,
        "row_count": j.row_count,
        "filters_json": j.filters_json,
        "created_at": j.created_at.isoformat() if j.created_at else None,
        "completed_at": j.completed_at.isoformat() if j.completed_at else None,
    }
