"""ProtocolConfig management routes.

POST   /projects/{id}/protocols       — create protocol config
GET    /projects/{id}/protocols       — list protocol configs for project
GET    /projects/{id}/protocols/{pid} — get protocol config detail
PATCH  /projects/{id}/protocols/{pid} — update protocol config

GET    /protocols/base                — list available base protocol IDs from YAML files
"""
from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_protocol_registry
from app.db.models import Project, ProtocolConfig
from app.protocols.registry import ProtocolRegistry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects/{project_id}/protocols", tags=["protocols"])
base_router = APIRouter(prefix="/protocols", tags=["protocols"])


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class CreateProtocolConfigBody(BaseModel):
    base_protocol_id: str | None = None
    name: str
    config_json: dict


class UpdateProtocolConfigBody(BaseModel):
    name: str | None = None
    config_json: dict | None = None
    status: str | None = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("", summary="Create a protocol config for a project")
def create_protocol_config(
    project_id: UUID,
    body: CreateProtocolConfigBody,
    db: Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    pc = ProtocolConfig(
        project_id=project_id,
        base_protocol_id=body.base_protocol_id,
        name=body.name,
        config_json=body.config_json,
    )
    db.add(pc)
    db.flush()
    return _pc_dict(pc)


@router.get("", summary="List protocol configs for a project")
def list_protocol_configs(project_id: UUID, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    configs = db.execute(
        select(ProtocolConfig)
        .where(ProtocolConfig.project_id == project_id)
        .order_by(ProtocolConfig.created_at.desc())
    ).scalars().all()

    return [_pc_dict(pc) for pc in configs]


@router.get("/{protocol_config_id}", summary="Get protocol config detail")
def get_protocol_config(
    project_id: UUID,
    protocol_config_id: UUID,
    db: Session = Depends(get_db),
):
    pc = _get_or_404(db, project_id, protocol_config_id)
    return _pc_dict(pc)


@router.patch("/{protocol_config_id}", summary="Update protocol config")
def update_protocol_config(
    project_id: UUID,
    protocol_config_id: UUID,
    body: UpdateProtocolConfigBody,
    db: Session = Depends(get_db),
):
    pc = _get_or_404(db, project_id, protocol_config_id)

    if pc.status == "locked":
        raise HTTPException(status_code=409, detail="Protocol config is locked and cannot be edited")

    if body.name is not None:
        pc.name = body.name
    if body.config_json is not None:
        pc.config_json = body.config_json
    if body.status is not None:
        if body.status not in ("draft", "active", "locked"):
            raise HTTPException(status_code=400, detail="Invalid status")
        pc.status = body.status

    db.flush()
    return _pc_dict(pc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_or_404(db: Session, project_id: UUID, config_id: UUID) -> ProtocolConfig:
    pc = db.execute(
        select(ProtocolConfig).where(
            ProtocolConfig.id == config_id,
            ProtocolConfig.project_id == project_id,
        )
    ).scalar_one_or_none()
    if pc is None:
        raise HTTPException(status_code=404, detail="Protocol config not found")
    return pc


def _pc_dict(pc: ProtocolConfig) -> dict:
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


# ---------------------------------------------------------------------------
# Base protocol IDs (standalone, not project-scoped)
# ---------------------------------------------------------------------------

@base_router.get("/base", summary="List available base protocol IDs from YAML files")
def list_base_protocols(
    registry: ProtocolRegistry = Depends(get_protocol_registry),
):
    """Return all base protocol IDs loaded from config/protocols/*.yaml.

    Each entry contains the protocol_id, human-readable name, jurisdiction,
    and regulatory framework so the frontend can populate a dropdown.
    """
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
