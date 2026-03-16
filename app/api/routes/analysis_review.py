"""Analysis review endpoints for the two-phase pipeline.

Provides endpoints to inspect analysis results and approve/reject documents
before full extraction begins.

GET  /jobs/{job_id}/analysis                              — analysis results for all docs
GET  /jobs/{job_id}/documents/{doc_id}/protocol-mapping   — protocol field mapping
PUT  /jobs/{job_id}/field-map                              — auditor edits field mappings
POST /jobs/{job_id}/documents/{doc_id}/approve             — approve a document (with detection decisions)
POST /jobs/{job_id}/documents/{doc_id}/reject              — reject a document
POST /jobs/{job_id}/approve-all                            — batch approve all pending
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.constants import PROTOCOL_LLM_CONFIG, PROTOCOL_REQUIRED_FIELDS
from app.core.settings import get_settings
from app.db.models import (
    DetectionReviewDecision,
    Document,
    DocumentAnalysisReview,
    Extraction,
    IngestionRun,
)

router = APIRouter(prefix="/jobs", tags=["analysis-review"])


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------

class FieldMappingBody(BaseModel):
    field_type: str
    anchor_text: str
    spatial_relationship: str
    value_pattern: str | None = None
    sample_bbox: list[float] = []
    line_count: int = 1
    skip_pattern: str | None = None


class UpdateFieldMapBody(BaseModel):
    document_id: str
    field_mappings: list[FieldMappingBody]
    extraction_method: str = "coordinate"  # "coordinate" or "ai"


class DetectionDecision(BaseModel):
    entity_type: str
    detected_value_masked: str | None = None
    page: int | None = None
    include: bool = True
    reason: str | None = None


class ReviewBody(BaseModel):
    reviewer_id: str
    rationale: str | None = None
    detection_decisions: list[DetectionDecision] | None = None


# ---------------------------------------------------------------------------
# GET /jobs/{job_id}/analysis
# ---------------------------------------------------------------------------

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

    # Resolve dedup anchors and protocol name from config
    config_snapshot = run.config_snapshot or {}
    protocol_name = config_snapshot.get("protocol_id", "")
    dedup_anchors: list[str] | None = None
    protocol_config_id = config_snapshot.get("protocol_config_id")
    if protocol_config_id:
        try:
            from app.db.models import ProtocolConfig
            pc = db.get(ProtocolConfig, UUID(protocol_config_id))
            if pc is not None and pc.config_json:
                dedup_anchors = pc.config_json.get("dedup_anchors")
        except Exception:
            pass

    # Fallback to PROTOCOL_LLM_CONFIG defaults if no dedup_anchors from config
    if dedup_anchors is None and protocol_name:
        llm_cfg = PROTOCOL_LLM_CONFIG.get(protocol_name, {})
        dedup_anchors = llm_cfg.get("dedup_anchors")

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

        # Document schema from LLM Document Understanding (Phase 14b)
        doc_schema = getattr(doc, "document_schema", None)
        if doc_schema is None and hasattr(doc, "structure_analysis") and isinstance(dsa, dict):
            doc_schema = dsa.get("document_schema")

        # Extract layout info from extraction_preview or document_schema
        preview = review.extraction_preview if review else None
        layout_type = None
        layout_field_map = None
        layout_confidence = None
        if preview and isinstance(preview, dict):
            layout_type = preview.get("layout_type")
            layout_confidence = preview.get("layout_confidence")
        if doc_schema and isinstance(doc_schema, dict):
            layout_type = layout_type or doc_schema.get("layout_type")
            layout_field_map = doc_schema.get("layout_field_map")
            layout_confidence = layout_confidence or doc_schema.get("layout_confidence")

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
            # Document schema from LLM Document Understanding (Phase 14b/14c)
            "document_schema": doc_schema,
            # Layout info for coordinate extraction (Step 21)
            "layout_type": layout_type,
            "layout_field_map": layout_field_map,
            "layout_confidence": layout_confidence,
            # Vision routing info (Step 22c)
            "vision_routing": (doc.metadata_json or {}).get("vision_routing"),
            "vision_field_map": (doc.metadata_json or {}).get("vision_field_map"),
            # LLM extraction preview (Step 19b)
            "extraction_preview": preview,
        })

    return {
        "documents": results,
        "dedup_anchors": dedup_anchors,
        "protocol_name": protocol_name,
    }


# ---------------------------------------------------------------------------
# PUT /jobs/{job_id}/field-map
# ---------------------------------------------------------------------------

@router.put("/{job_id}/field-map")
def update_field_map(job_id: str, body: UpdateFieldMapBody, db: Session = Depends(get_db)):
    """Auditor edits field mappings before approving coordinate extraction."""
    try:
        run_uuid = UUID(job_id)
        doc_uuid = UUID(body.document_id)
    except ValueError:
        raise HTTPException(400, "Invalid UUID")

    run = db.get(IngestionRun, run_uuid)
    if not run:
        raise HTTPException(404, "Job not found")

    doc = db.get(Document, doc_uuid)
    if not doc or doc.ingestion_run_id != run_uuid:
        raise HTTPException(404, "Document not found in this job")

    # Validate field mappings
    if not body.field_mappings:
        raise HTTPException(422, "field_mappings cannot be empty")

    valid_relationships = {
        "same_line_right", "same_line_left", "line_below", "region_right",
    }
    # Also allow "lines_below_N" pattern
    for fm in body.field_mappings:
        rel = fm.spatial_relationship
        if rel not in valid_relationships and not rel.startswith("lines_below_"):
            raise HTTPException(
                422,
                f"Invalid spatial_relationship '{rel}'. "
                f"Must be one of {sorted(valid_relationships)} or 'lines_below_N'.",
            )

    # Build serializable field map list
    field_map_dicts = [
        {
            "field_type": fm.field_type,
            "anchor_text": fm.anchor_text,
            "spatial_relationship": fm.spatial_relationship,
            "value_pattern": fm.value_pattern,
            "sample_bbox": fm.sample_bbox,
            "line_count": fm.line_count,
            "skip_pattern": fm.skip_pattern,
        }
        for fm in body.field_mappings
    ]

    # Store on document metadata_json so extraction can pick it up
    meta = doc.metadata_json or {}
    meta["auditor_layout_field_map"] = field_map_dicts
    meta["auditor_extraction_method"] = body.extraction_method
    doc.metadata_json = meta

    # Also update the extraction_preview on the review record
    review = db.query(DocumentAnalysisReview).filter(
        DocumentAnalysisReview.document_id == doc_uuid,
        DocumentAnalysisReview.ingestion_run_id == run_uuid,
    ).first()

    if review and review.extraction_preview and isinstance(review.extraction_preview, dict):
        preview = dict(review.extraction_preview)
        preview["field_map_count"] = len(field_map_dicts)
        preview["extraction_method"] = body.extraction_method
        review.extraction_preview = preview

    db.flush()
    return {
        "document_id": body.document_id,
        "field_mappings_count": len(field_map_dicts),
        "extraction_method": body.extraction_method,
    }


# ---------------------------------------------------------------------------
# GET /jobs/{job_id}/documents/{doc_id}/protocol-mapping
# ---------------------------------------------------------------------------

@router.get("/{job_id}/documents/{doc_id}/protocol-mapping")
def get_protocol_mapping(job_id: str, doc_id: str, db: Session = Depends(get_db)):
    """Get protocol field mapping for a document's sample detections."""
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

    # Determine protocol from the run's config_snapshot
    config_snapshot = run.config_snapshot or {}
    protocol_id = config_snapshot.get("protocol_id", "")

    # Get sample extractions
    sample_extractions = db.query(Extraction).filter(
        Extraction.document_id == doc_uuid,
        Extraction.is_sample == True,  # noqa: E712
    ).all()

    masking_on = get_settings().pii_masking_enabled

    # Build set of detected entity types with their details
    detected_types: dict[str, list[dict]] = {}
    for ext in sample_extractions:
        display_value = ext.masked_value or "***"
        if not masking_on:
            display_value = ext.normalized_value or ext.masked_value or "***"
        entry = {
            "entity_type": ext.pii_type,
            "value_masked": display_value,
            "confidence": ext.confidence_score,
            "page": ext.evidence_page,
            "included": True,
        }
        detected_types.setdefault(ext.pii_type, []).append(entry)

    # Look up protocol requirements — try exact match, then prefix match
    protocol_fields = None
    for pid in [protocol_id, protocol_id.split("_")[0] if "_" in protocol_id else ""]:
        if pid and pid in PROTOCOL_REQUIRED_FIELDS:
            protocol_fields = PROTOCOL_REQUIRED_FIELDS[pid]
            break

    if not protocol_fields:
        # Fallback: try to match by prefix
        for key in PROTOCOL_REQUIRED_FIELDS:
            if protocol_id.startswith(key) or key.startswith(protocol_id):
                protocol_fields = PROTOCOL_REQUIRED_FIELDS[key]
                protocol_id = key
                break

    if not protocol_fields:
        protocol_fields = {"required": []}

    # Build field mapping
    field_mapping = []
    required_count = 0
    required_detected = 0

    for field_def in protocol_fields.get("required", []):
        field_name = field_def["field"]
        entity_types = field_def["entity_types"]
        criticality = field_def["criticality"]

        # Find matching detections
        matched = []
        for et in entity_types:
            matched.extend(detected_types.get(et, []))

        # Determine status
        if matched:
            low_conf = all(d["confidence"] and d["confidence"] < 0.60 for d in matched)
            status = "needs_review" if low_conf else "detected"
        else:
            status = "missing" if criticality == "required" else "optional_missing"

        if criticality == "required":
            required_count += 1
            if matched:
                required_detected += 1

        field_mapping.append({
            "field": field_name,
            "criticality": criticality,
            "status": status,
            "matched_detections": matched,
        })

    completeness_pct = round(required_detected / required_count * 100) if required_count > 0 else 100

    return {
        "protocol": protocol_id,
        "field_mapping": field_mapping,
        "coverage": {
            "required_fields": required_count,
            "required_detected": required_detected,
            "required_missing": required_count - required_detected,
            "completeness_pct": completeness_pct,
        },
    }


# ---------------------------------------------------------------------------
# POST /jobs/{job_id}/documents/{doc_id}/approve
# ---------------------------------------------------------------------------

@router.post("/{job_id}/documents/{doc_id}/approve")
def approve_document(job_id: str, doc_id: str, body: ReviewBody, db: Session = Depends(get_db)):
    """Approve a document for full extraction, with optional detection decisions."""
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

    now = datetime.now(timezone.utc)
    review.status = "approved"
    review.reviewer_id = body.reviewer_id
    review.rationale = body.rationale
    review.reviewed_at = now

    # Store detection decisions if provided
    if body.detection_decisions:
        included_types: set[str] = set()
        for dd in body.detection_decisions:
            decision = DetectionReviewDecision(
                document_analysis_review_id=review.id,
                entity_type=dd.entity_type,
                detected_value_masked=dd.detected_value_masked,
                confidence=None,
                page=dd.page,
                include_in_extraction=dd.include,
                decision_reason=dd.reason,
                decided_by=body.reviewer_id,
                decided_at=now,
                decision_source="individual" if dd.reason else "bulk_type",
            )
            db.add(decision)
            if dd.include:
                included_types.add(dd.entity_type)

        review.selected_entity_types = sorted(included_types)

    doc.analysis_phase_status = "approved"

    db.flush()
    return {"status": "approved", "document_id": doc_id}


# ---------------------------------------------------------------------------
# POST /jobs/{job_id}/documents/{doc_id}/reject
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# POST /jobs/{job_id}/approve-all
# ---------------------------------------------------------------------------

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
