"""Project management routes.

POST   /projects            — create project
GET    /projects            — list projects
GET    /projects/{id}       — project detail with attached protocols
PATCH  /projects/{id}       — update project
GET    /projects/{id}/catalog-summary — catalog breakdown (Step 3)
GET    /projects/{id}/density         — density summary (Step 4)
"""
from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.db.models import DensitySummary, Document, IngestionRun, Project, ProtocolConfig

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects", tags=["projects"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class CreateProjectBody(BaseModel):
    name: str
    description: str | None = None
    created_by: str | None = None


class UpdateProjectBody(BaseModel):
    name: str | None = None
    description: str | None = None
    status: str | None = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("", summary="Create a new project")
def create_project(body: CreateProjectBody, db: Session = Depends(get_db)):
    project = Project(
        name=body.name,
        description=body.description,
        created_by=body.created_by,
    )
    db.add(project)
    db.flush()
    return _project_summary(project)


@router.get("", summary="List all projects")
def list_projects(db: Session = Depends(get_db)):
    projects = db.execute(
        select(Project).order_by(Project.created_at.desc())
    ).scalars().all()
    return [_project_summary(p) for p in projects]


@router.get("/{project_id}", summary="Get project detail")
def get_project(project_id: UUID, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    protocols = db.execute(
        select(ProtocolConfig).where(ProtocolConfig.project_id == project_id)
    ).scalars().all()

    return {
        **_project_summary(project),
        "protocols": [_protocol_config_summary(pc) for pc in protocols],
    }


@router.patch("/{project_id}", summary="Update a project")
def update_project(project_id: UUID, body: UpdateProjectBody, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    if body.name is not None:
        project.name = body.name
    if body.description is not None:
        project.description = body.description
    if body.status is not None:
        if body.status not in ("active", "archived", "completed"):
            raise HTTPException(status_code=400, detail="Invalid status")
        project.status = body.status

    db.flush()
    return _project_summary(project)


@router.get("/{project_id}/catalog-summary", summary="Catalog breakdown for project")
def get_catalog_summary(project_id: UUID, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    # Get all documents through ingestion runs linked to this project
    docs = db.execute(
        select(Document).join(IngestionRun).where(IngestionRun.project_id == project_id)
    ).scalars().all()

    by_type: dict[str, int] = {}
    by_structure: dict[str, int] = {}
    auto_count = 0
    manual_count = 0
    total = len(docs)

    for doc in docs:
        ft = doc.file_type or "unknown"
        by_type[ft] = by_type.get(ft, 0) + 1

        sc = doc.structure_class or "unclassified"
        by_structure[sc] = by_structure.get(sc, 0) + 1

        if doc.can_auto_process:
            auto_count += 1
        else:
            manual_count += 1

    return {
        "project_id": str(project_id),
        "total_documents": total,
        "auto_processable": auto_count,
        "manual_review": manual_count,
        "by_file_type": by_type,
        "by_structure_class": by_structure,
    }


@router.get("/{project_id}/density", summary="Density summary for project")
def get_density(project_id: UUID, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    # Project-level summary (document_id is NULL)
    project_summary = db.execute(
        select(DensitySummary).where(
            DensitySummary.project_id == project_id,
            DensitySummary.document_id.is_(None),
        ).order_by(DensitySummary.created_at.desc())
    ).scalars().first()

    # Document-level summaries
    doc_summaries = db.execute(
        select(DensitySummary).where(
            DensitySummary.project_id == project_id,
            DensitySummary.document_id.isnot(None),
        ).order_by(DensitySummary.created_at.desc())
    ).scalars().all()

    return {
        "project_id": str(project_id),
        "project_summary": _density_dict(project_summary) if project_summary else None,
        "document_summaries": [_density_dict(ds) for ds in doc_summaries],
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _project_summary(p: Project) -> dict:
    return {
        "id": str(p.id),
        "name": p.name,
        "description": p.description,
        "status": p.status,
        "created_by": p.created_by,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


def _protocol_config_summary(pc: ProtocolConfig) -> dict:
    return {
        "id": str(pc.id),
        "project_id": str(pc.project_id),
        "base_protocol_id": pc.base_protocol_id,
        "name": pc.name,
        "config_json": pc.config_json,
        "status": pc.status,
        "created_at": pc.created_at.isoformat() if pc.created_at else None,
        "updated_at": pc.updated_at.isoformat() if pc.updated_at else None,
    }


def _density_dict(ds: DensitySummary) -> dict:
    return {
        "id": str(ds.id),
        "document_id": str(ds.document_id) if ds.document_id else None,
        "total_entities": ds.total_entities,
        "by_category": ds.by_category,
        "by_type": ds.by_type,
        "confidence": ds.confidence,
        "confidence_notes": ds.confidence_notes,
        "created_at": ds.created_at.isoformat() if ds.created_at else None,
    }
