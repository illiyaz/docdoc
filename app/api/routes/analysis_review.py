"""Analysis review endpoints for the two-phase pipeline.

Provides endpoints to inspect analysis results and approve/reject documents
before full extraction begins.

GET  /jobs/{job_id}/analysis                    — analysis results for all docs
POST /jobs/{job_id}/documents/{doc_id}/approve  — approve a document
POST /jobs/{job_id}/documents/{doc_id}/reject   — reject a document
POST /jobs/{job_id}/approve-all                 — batch approve all pending
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.settings import get_settings
from app.db.models import Document, DocumentAnalysisReview, Extraction, IngestionRun

router = APIRouter(prefix="/jobs", tags=["analysis-review"])


class ReviewBody(BaseModel):
    reviewer_id: str
    rationale: str | None = None


@router.get("/{job_id}/analysis")
def get_analysis_results(job_id: str, db: Session = Depends(get_db)):
    """Get analysis results for all documents in a two-phase job."""
    try:
        run_uuid = UUID(job_id)
    except ValueError:
        raise HTTPException(400, "Invalid job_id")

    run = db.get(IngestionRun, run_uuid)
    if not run:
        raise HTTPException(404, "Job not found")

    docs = db.query(Document).filter(Document.ingestion_run_id == run_uuid).all()

    results = []
    for doc in docs:
        # Get review record if exists
        review = db.query(DocumentAnalysisReview).filter(
            DocumentAnalysisReview.document_id == doc.id
        ).first()

        # Get sample extractions (is_sample=True)
        sample_extractions = db.query(Extraction).filter(
            Extraction.document_id == doc.id,
            Extraction.is_sample == True,  # noqa: E712
        ).all()

        # Build sample extraction list
        masking_on = get_settings().pii_masking_enabled
        samples = []
        for ext in sample_extractions:
            display_value = ext.masked_value or "***"
            if not masking_on:
                # When masking disabled (testing), show normalized_value if available
                display_value = ext.normalized_value or ext.masked_value or "***"
            samples.append({
                "pii_type": ext.pii_type,
                "masked_value": display_value,
                "confidence": ext.confidence_score,
                "entity_role": ext.entity_role,
                "evidence_page": ext.evidence_page,
            })

        # Parse structure analysis for document_type
        dsa = doc.structure_analysis or {}

        # Parse entity analysis (from LLM entity relationship analysis)
        ea = doc.entity_analysis or {}

        results.append({
            "document_id": str(doc.id),
            "file_name": doc.file_name,
            "file_type": doc.file_type,
            "structure_class": doc.structure_class,
            "document_type": dsa.get("document_type"),
            "document_type_confidence": dsa.get("document_type_confidence"),
            "onset_page": doc.sample_onset_page,
            "sample_extraction_count": doc.sample_extraction_count or 0,
            "sample_extractions": samples,
            "analysis_phase_status": doc.analysis_phase_status,
            "review_status": review.status if review else None,
            "auto_approve_reason": review.auto_approve_reason if review else None,
            "reviewed_by": review.reviewer_id if review else None,
            "reviewed_at": review.reviewed_at.isoformat() if review and review.reviewed_at else None,
            # Entity analysis from LLM
            "document_summary": ea.get("document_summary"),
            "entity_groups": ea.get("entity_groups", []),
            "relationships": ea.get("relationships", []),
            "estimated_unique_individuals": ea.get("estimated_unique_individuals"),
            "extraction_guidance": ea.get("extraction_guidance"),
        })

    return results


@router.post("/{job_id}/documents/{doc_id}/approve")
def approve_document(job_id: str, doc_id: str, body: ReviewBody, db: Session = Depends(get_db)):
    """Approve a document for full extraction."""
    try:
        run_uuid = UUID(job_id)
        doc_uuid = UUID(doc_id)
    except ValueError:
        raise HTTPException(400, "Invalid UUID")

    run = db.get(IngestionRun, run_uuid)
    if not run:
        raise HTTPException(404, "Job not found")

    doc = db.get(Document, doc_uuid)
    if not doc or doc.ingestion_run_id != run_uuid:
        raise HTTPException(404, "Document not found in this job")

    review = db.query(DocumentAnalysisReview).filter(
        DocumentAnalysisReview.document_id == doc_uuid,
        DocumentAnalysisReview.ingestion_run_id == run_uuid,
    ).first()

    if not review:
        raise HTTPException(404, "No analysis review found for this document")

    if review.status not in ("pending_review", "rejected"):
        raise HTTPException(409, f"Cannot approve: current status is '{review.status}'")

    review.status = "approved"
    review.reviewer_id = body.reviewer_id
    review.rationale = body.rationale
    review.reviewed_at = datetime.now(timezone.utc)

    doc.analysis_phase_status = "approved"

    db.flush()
    return {"status": "approved", "document_id": doc_id}


@router.post("/{job_id}/documents/{doc_id}/reject")
def reject_document(job_id: str, doc_id: str, body: ReviewBody, db: Session = Depends(get_db)):
    """Reject a document from full extraction."""
    try:
        run_uuid = UUID(job_id)
        doc_uuid = UUID(doc_id)
    except ValueError:
        raise HTTPException(400, "Invalid UUID")

    run = db.get(IngestionRun, run_uuid)
    if not run:
        raise HTTPException(404, "Job not found")

    doc = db.get(Document, doc_uuid)
    if not doc or doc.ingestion_run_id != run_uuid:
        raise HTTPException(404, "Document not found in this job")

    review = db.query(DocumentAnalysisReview).filter(
        DocumentAnalysisReview.document_id == doc_uuid,
        DocumentAnalysisReview.ingestion_run_id == run_uuid,
    ).first()

    if not review:
        raise HTTPException(404, "No analysis review found for this document")

    review.status = "rejected"
    review.reviewer_id = body.reviewer_id
    review.rationale = body.rationale
    review.reviewed_at = datetime.now(timezone.utc)

    doc.analysis_phase_status = "rejected"

    db.flush()
    return {"status": "rejected", "document_id": doc_id}


@router.post("/{job_id}/approve-all")
def approve_all_documents(job_id: str, body: ReviewBody, db: Session = Depends(get_db)):
    """Batch approve all pending documents for full extraction."""
    try:
        run_uuid = UUID(job_id)
    except ValueError:
        raise HTTPException(400, "Invalid job_id")

    run = db.get(IngestionRun, run_uuid)
    if not run:
        raise HTTPException(404, "Job not found")

    reviews = db.query(DocumentAnalysisReview).filter(
        DocumentAnalysisReview.ingestion_run_id == run_uuid,
        DocumentAnalysisReview.status == "pending_review",
    ).all()

    now = datetime.now(timezone.utc)
    approved_count = 0
    for review in reviews:
        review.status = "approved"
        review.reviewer_id = body.reviewer_id
        review.rationale = body.rationale
        review.reviewed_at = now

        doc = db.get(Document, review.document_id)
        if doc:
            doc.analysis_phase_status = "approved"
        approved_count += 1

    db.flush()
    return {"approved": approved_count, "total": len(reviews)}
