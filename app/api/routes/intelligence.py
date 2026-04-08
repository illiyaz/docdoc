"""Document Intelligence endpoints.

Provides a clean, read-only view of what the LLM understood about each
document after analysis — document type, field maps, routing decisions,
and sample extractions.  Also supports correction memory: users can submit
corrections to the LLM's understanding, which are stored as few-shot
examples for future runs.

GET  /projects/{project_id}/intelligence          — intelligence summary for all analyzed docs
POST /projects/{project_id}/intelligence/correct   — submit a correction to LLM understanding
POST /intelligence/test-extract                    — test-extract N pages from onset for a doc
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
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.settings import get_settings
from app.db.models import Document, Extraction, IngestionRun

logger = logging.getLogger(__name__)
router = APIRouter(tags=["intelligence"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class CorrectionBody(BaseModel):
    """User correction to LLM document understanding."""
    document_id: str
    field: str  # which field was wrong: "document_type", "layout_type", "field_map", etc.
    original_value: Any = None
    corrected_value: Any
    reason: str | None = None


class TestExtractBody(BaseModel):
    """Request to test-extract a few pages from a document."""
    document_id: str
    job_id: str
    pages: int = 3  # how many pages to extract from onset


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_doc_intelligence(doc: Document, db: Session) -> dict:
    """Build intelligence summary for a single document."""
    meta = doc.metadata_json or {}
    dsa = doc.structure_analysis or {}
    ea = doc.entity_analysis or {}
    doc_schema = meta.get("document_schema", {})
    vision_routing = meta.get("vision_routing", {})
    vision_field_map = meta.get("vision_field_map")

    # Template info from document schema
    template = doc_schema.get("template") if doc_schema else None

    # Correction history
    corrections = meta.get("intelligence_corrections", [])

    # Sample extractions
    settings = get_settings()
    sample_extractions = db.query(Extraction).filter(
        Extraction.document_id == doc.id,
        Extraction.is_sample == True,  # noqa: E712
    ).limit(20).all()

    samples = []
    for ext in sample_extractions:
        display = ext.normalized_value if not settings.pii_masking_enabled else (ext.masked_value or "***")
        samples.append({
            "pii_type": ext.pii_type,
            "masked_value": display,
            "confidence": ext.confidence_score,
            "entity_role": ext.entity_role,
            "page": ext.evidence_page,
        })

    # Determine effective field map (auditor override > vision > schema)
    effective_field_map = vision_field_map or doc_schema.get("layout_field_map")

    # Build the response
    return {
        "document_id": str(doc.id),
        "file_name": doc.file_name,
        "file_type": doc.file_type,
        "page_count": doc.page_count,
        "status": doc.analysis_phase_status or doc.status,

        # Structure analysis (heuristic)
        "structure": {
            "document_type": dsa.get("document_type", "unknown"),
            "document_type_confidence": dsa.get("document_type_confidence", 0),
            "detected_by": dsa.get("detected_by", "unknown"),
            "sections": dsa.get("sections", []),
        },

        # LLM Document Understanding
        "understanding": {
            "document_type": doc_schema.get("document_type"),
            "document_subtype": doc_schema.get("document_subtype"),
            "issuing_entity": doc_schema.get("issuing_entity"),
            "schema_confidence": doc_schema.get("schema_confidence", 0),
            "is_tabular": doc_schema.get("is_tabular", False),
            "records_per_page": doc_schema.get("records_per_page_estimate", 1),
            "layout_type": doc_schema.get("layout_type", "variable"),
            "layout_confidence": doc_schema.get("layout_confidence", 0),
            "extraction_notes": doc_schema.get("extraction_notes"),
            "suppression_hints": doc_schema.get("suppression_hints", []),
            "field_map": doc_schema.get("field_map", []),
            "people": doc_schema.get("people", []),
            "tables": doc_schema.get("tables", []),
            "template": template,
        },

        # Routing decision
        "routing": {
            "recommended_path": vision_routing.get("recommended_path", "unknown"),
            "structure_type": vision_routing.get("structure_type", "unknown"),
            "pii_field_count": vision_routing.get("pii_field_count", 0),
            "records_per_page": vision_routing.get("records_per_page", 1),
            "schema_skip": vision_routing.get("schema_skip", False),
        },

        # Effective field map for extraction
        "field_map": [
            {
                "field_type": fm.get("field_type", ""),
                "anchor_text": fm.get("anchor_text", ""),
                "spatial_relationship": fm.get("spatial_relationship", ""),
                "line_count": fm.get("line_count", 1),
                "value_pattern": fm.get("value_pattern"),
            }
            for fm in (effective_field_map or [])
        ],

        # Entity analysis
        "entities": {
            "summary": ea.get("document_summary"),
            "estimated_individuals": ea.get("estimated_unique_individuals"),
            "guidance": ea.get("extraction_guidance"),
            "groups": ea.get("entity_groups", []),
        },

        # Onset
        "onset_page": doc.sample_onset_page,

        # Sample extractions
        "sample_extractions": samples,

        # Correction history
        "corrections": corrections,
    }


# ---------------------------------------------------------------------------
# GET /projects/{project_id}/intelligence
# ---------------------------------------------------------------------------

@router.get("/projects/{project_id}/intelligence")
def get_project_intelligence(project_id: str, db: Session = Depends(get_db)):
    """Get intelligence summary for all analyzed documents in a project."""
    try:
        proj_uuid = UUID(project_id)
    except ValueError:
        raise HTTPException(400, "Invalid project_id")

    # Find all jobs for this project
    runs = db.query(IngestionRun).filter(
        IngestionRun.project_id == proj_uuid
    ).all()

    if not runs:
        return {"documents": [], "job_count": 0}

    # Gather all documents across all jobs, newest first
    run_ids = [r.id for r in runs]
    docs = db.query(Document).filter(
        Document.ingestion_run_id.in_(run_ids)
    ).order_by(Document.updated_at.desc()).all()

    # Build run lookup for job-level info
    run_map = {r.id: r for r in runs}

    documents = []
    for doc in docs:
        try:
            intel = _build_doc_intelligence(doc, db)
            # Add job info + timestamps
            run = run_map.get(doc.ingestion_run_id)
            intel["job_id"] = str(doc.ingestion_run_id)
            intel["job_status"] = run.status if run else "unknown"
            intel["job_started_at"] = (
                run.started_at.isoformat() if run and run.started_at else None
            )
            intel["analyzed_at"] = (
                doc.updated_at.isoformat() if doc.updated_at else
                doc.created_at.isoformat() if doc.created_at else None
            )
            intel["created_at"] = (
                doc.created_at.isoformat() if doc.created_at else None
            )
            # Job doc count for grouping display
            intel["job_doc_count"] = sum(
                1 for d in docs if d.ingestion_run_id == doc.ingestion_run_id
            )
            documents.append(intel)
        except Exception as e:
            logger.warning("Failed to build intelligence for %s: %s", doc.file_name, e)
            documents.append({
                "document_id": str(doc.id),
                "file_name": doc.file_name,
                "file_type": doc.file_type,
                "error": str(e),
                "job_id": str(doc.ingestion_run_id),
                "analyzed_at": (
                    doc.updated_at.isoformat() if doc.updated_at else None
                ),
            })

    # Summary stats
    total_pages = sum(d.get("page_count") or 0 for d in documents)
    routed = [d for d in documents if d.get("routing", {}).get("recommended_path", "unknown") != "unknown"]
    path_counts: dict[str, int] = {}
    for d in routed:
        p = d.get("routing", {}).get("recommended_path", "unknown")
        path_counts[p] = path_counts.get(p, 0) + 1

    return {
        "documents": documents,
        "job_count": len(runs),
        "summary": {
            "total_documents": len(documents),
            "total_pages": total_pages,
            "routed_documents": len(routed),
            "path_distribution": path_counts,
        },
    }


# ---------------------------------------------------------------------------
# POST /projects/{project_id}/intelligence/correct
# ---------------------------------------------------------------------------

@router.post("/projects/{project_id}/intelligence/correct")
def submit_correction(
    project_id: str,
    body: CorrectionBody,
    db: Session = Depends(get_db),
):
    """Submit a user correction to LLM document understanding.

    Stores the correction in the document's metadata_json for future
    few-shot prompt injection.  Also saves to a project-level correction
    log for aggregation.
    """
    try:
        doc_uuid = UUID(body.document_id)
    except ValueError:
        raise HTTPException(400, "Invalid document_id")

    doc = db.get(Document, doc_uuid)
    if not doc:
        raise HTTPException(404, "Document not found")

    # Build correction record
    correction = {
        "field": body.field,
        "original_value": body.original_value,
        "corrected_value": body.corrected_value,
        "reason": body.reason,
        "corrected_at": datetime.now(timezone.utc).isoformat(),
    }

    # Store in document metadata
    meta = doc.metadata_json or {}
    corrections = meta.get("intelligence_corrections", [])
    corrections.append(correction)
    meta["intelligence_corrections"] = corrections
    doc.metadata_json = meta

    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(doc, "metadata_json")
    db.commit()

    # Also persist to correction memory file (project-level, for few-shot retrieval)
    _persist_correction_to_file(project_id, doc, correction)

    return {
        "status": "saved",
        "document_id": body.document_id,
        "correction_count": len(corrections),
    }


def _persist_correction_to_file(project_id: str, doc: Document, correction: dict):
    """Append correction to a JSONL file for few-shot retrieval."""
    settings = get_settings()
    corrections_dir = Path(settings.upload_dir).parent / "corrections"
    corrections_dir.mkdir(parents=True, exist_ok=True)

    record = {
        "project_id": project_id,
        "document_id": str(doc.id),
        "file_name": doc.file_name,
        "file_type": doc.file_type,
        **correction,
    }

    filepath = corrections_dir / f"{project_id}_corrections.jsonl"
    try:
        with open(filepath, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        logger.warning("Failed to persist correction to file: %s", e)


# ---------------------------------------------------------------------------
# POST /intelligence/test-extract
# ---------------------------------------------------------------------------

@router.post("/intelligence/test-extract")
def test_extract(body: TestExtractBody, db: Session = Depends(get_db)):
    """Test-extract a few pages from onset for a document.

    Runs the extraction pipeline on N pages starting from the document's
    onset page and returns the resulting records without persisting them.
    This is the Tier 1 testing loop.
    """
    try:
        doc_uuid = UUID(body.document_id)
    except ValueError:
        raise HTTPException(400, "Invalid document_id")

    doc = db.get(Document, doc_uuid)
    if not doc:
        raise HTTPException(404, "Document not found")

    if not doc.source_path or not Path(doc.source_path).exists():
        raise HTTPException(400, "Document source file not found on disk")

    meta = doc.metadata_json or {}
    doc_schema_dict = meta.get("document_schema", {})
    vision_routing = meta.get("vision_routing", {})
    recommended_path = vision_routing.get("recommended_path", "")

    onset = doc.sample_onset_page or 0
    pages_to_extract = body.pages

    results: list[dict] = []

    try:
        if recommended_path == "coordinate":
            results = _test_coordinate_extract(doc, onset, pages_to_extract, meta)
        elif recommended_path in ("llm_table", "llm_template"):
            results = _test_llm_extract(doc, onset, pages_to_extract, recommended_path)
        else:
            # Fallback: use Presidio detection on sample pages
            results = _test_presidio_extract(doc, onset, pages_to_extract)
    except Exception as e:
        logger.warning("Test extract failed for %s: %s", doc.file_name, e, exc_info=True)
        return {
            "document_id": body.document_id,
            "extraction_path": recommended_path or "unknown",
            "pages_tested": 0,
            "records": [],
            "error": str(e),
        }

    return {
        "document_id": body.document_id,
        "extraction_path": recommended_path or "presidio",
        "onset_page": onset,
        "pages_tested": pages_to_extract,
        "records": results[:50],  # Cap at 50 for response size
        "total_records": len(results),
    }


# ---------------------------------------------------------------------------
# Test extraction helpers
# ---------------------------------------------------------------------------

def _test_coordinate_extract(
    doc: Document, onset: int, n_pages: int, meta: dict
) -> list[dict]:
    """Run coordinate extraction on N pages from onset."""
    from app.pipeline.coordinate_extractor import CoordinateExtractor

    field_map = meta.get("vision_field_map") or (
        meta.get("document_schema", {}).get("layout_field_map")
    )
    if not field_map:
        return [{"error": "No field map available for coordinate extraction"}]

    extractor = CoordinateExtractor(doc.source_path, field_map)
    records = []
    for page_num in range(onset, min(onset + n_pages, (doc.page_count or onset + n_pages))):
        try:
            page_records = extractor.extract_page(page_num)
            for rec in page_records:
                rec_dict = _record_to_dict(rec, page_num)
                if rec_dict:
                    records.append(rec_dict)
        except Exception as e:
            records.append({"page": page_num, "error": str(e)})

    return records


def _test_llm_extract(
    doc: Document, onset: int, n_pages: int, path: str
) -> list[dict]:
    """Run LLM-based extraction on N pages from onset."""
    try:
        import fitz
    except ImportError:
        return [{"error": "PyMuPDF not available"}]

    pdf = fitz.open(doc.source_path)
    records = []

    for page_num in range(onset, min(onset + n_pages, pdf.page_count)):
        page = pdf[page_num]
        text = page.get_text("text")
        if not text.strip():
            continue

        # Use LLM template extractor
        try:
            from app.structure.llm_template_extractor import LLMTemplateExtractor

            # Create a minimal extractor and extract this page
            from app.llm.client import OllamaClient
            client = OllamaClient()
            extractor = LLMTemplateExtractor(client)

            page_records = extractor.extract_page_text(text, page_num, doc.file_name or "")
            for rec in page_records:
                rec_dict = _record_to_dict(rec, page_num)
                if rec_dict:
                    records.append(rec_dict)
        except Exception as e:
            records.append({"page": page_num, "error": str(e)})

    pdf.close()
    return records


def _test_presidio_extract(
    doc: Document, onset: int, n_pages: int
) -> list[dict]:
    """Run Presidio detection on N pages from onset."""
    try:
        import fitz
        from app.pii.presidio_engine import PresidioEngine
    except ImportError:
        return [{"error": "Required libraries not available"}]

    pdf = fitz.open(doc.source_path)
    engine = PresidioEngine()
    records = []

    for page_num in range(onset, min(onset + n_pages, pdf.page_count)):
        page = pdf[page_num]
        text = page.get_text("text")
        if not text.strip():
            continue

        detections = engine.analyze(text)
        for det in detections:
            records.append({
                "page": page_num,
                "pii_type": det.entity_type,
                "value": "***",  # Never expose raw PII
                "confidence": round(det.score, 3),
                "start": det.start,
                "end": det.end,
            })

    pdf.close()
    return records


def _record_to_dict(rec: Any, page_num: int) -> dict | None:
    """Convert a PIIRecord or similar to a safe dict for API response."""
    if isinstance(rec, dict):
        return rec

    try:
        d: dict[str, Any] = {"page": page_num}
        if hasattr(rec, "raw_name") and rec.raw_name:
            d["name"] = rec.raw_name[:3] + "***" if len(rec.raw_name) > 3 else "***"
        if hasattr(rec, "raw_email") and rec.raw_email:
            d["email"] = "***@***"
        if hasattr(rec, "raw_phone") and rec.raw_phone:
            d["phone"] = "***-***-" + rec.raw_phone[-4:] if len(rec.raw_phone) >= 4 else "***"
        if hasattr(rec, "raw_government_id") and rec.raw_government_id:
            d["gov_id"] = "***-" + rec.raw_government_id[-4:] if len(rec.raw_government_id) >= 4 else "***"
        if hasattr(rec, "raw_address") and rec.raw_address:
            d["address"] = "***"
        if hasattr(rec, "raw_dob") and rec.raw_dob:
            d["dob"] = "***"

        # Only return if there's at least one PII field
        if len(d) > 1:
            return d
    except Exception:
        pass
    return None
