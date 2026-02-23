"""Job management routes — full implementation.

POST /jobs runs the synchronous pipeline: discovery → PII detection →
entity resolution → deduplication → notification list building.

GET /jobs/{job_id} returns job status.
GET /jobs/{job_id}/results returns masked NotificationSubjects.
"""
from __future__ import annotations

import logging
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_protocol_registry
from app.db.models import NotificationList, NotificationSubject
from app.notification.list_builder import build_notification_list, get_notification_subjects
from app.protocols.registry import ProtocolRegistry
from app.rra.deduplicator import Deduplicator
from app.rra.entity_resolver import EntityResolver
from app.tasks.discovery import DiscoveryTask, FilesystemConnector

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["jobs"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class CreateJobBody(BaseModel):
    job_id: str | None = None
    protocol_id: str
    source_directory: str


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
    return {
        "subject_id": str(ns.subject_id),
        "canonical_name": ns.canonical_name,
        "canonical_email": _mask_email(ns.canonical_email),
        "canonical_phone": _mask_phone(ns.canonical_phone),
        "pii_types_found": ns.pii_types_found or [],
        "notification_required": ns.notification_required,
        "review_status": ns.review_status,
    }


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


@router.post("", summary="Submit a new extraction job")
def create_job(
    body: CreateJobBody,
    db: Session = Depends(get_db),
    registry: ProtocolRegistry = Depends(get_protocol_registry),
):
    job_id = body.job_id or str(uuid4())

    # 1. Load protocol
    try:
        protocol = registry.get(body.protocol_id)
    except KeyError:
        raise HTTPException(status_code=400, detail=f"Protocol not found: {body.protocol_id!r}")

    try:
        # 2. Discover documents
        connector = FilesystemConnector(body.source_directory)
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
