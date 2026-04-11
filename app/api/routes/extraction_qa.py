"""Extraction QA endpoints (Step 30e-7).

Post-extraction auditor confidence screen: summary dashboard, smart
sample panel, unresolved gaps, approval gating.

GET  /projects/{project_id}/qa/summary   — completeness stats
GET  /projects/{project_id}/qa/samples   — curated sample records
GET  /projects/{project_id}/qa/gaps      — unresolved gaps
POST /projects/{project_id}/qa/gaps/{index}/resolve  — manual gap resolution
POST /projects/{project_id}/qa/approve   — final approval (gated on gaps)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.pipeline.gap_filler import load_gaps, persist_gaps, _mask_value
from app.pipeline.qa_sampler import QASampler

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/projects/{project_id}/qa", tags=["extraction-qa"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class ApproveBody(BaseModel):
    """Approve extraction for notification."""
    reviewer_id: str = "auditor"
    rationale: str | None = None


class ResolveGapBody(BaseModel):
    """Resolve a gap manually."""
    value: str | None = None
    action: str = "resolve"   # "resolve" | "mark_na" | "mark_unrecoverable"
    reviewer_id: str = "auditor"
    notes: str | None = None


# ---------------------------------------------------------------------------
# Helpers — QA state persistence (JSON on disk, like gaps & segregation)
# ---------------------------------------------------------------------------

def _get_qa_state_path(project_id: str, job_id: str) -> Path:
    return Path("data") / "projects" / project_id / "qa" / f"{job_id}.json"


def _load_qa_state(project_id: str, job_id: str) -> dict:
    path = _get_qa_state_path(project_id, job_id)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {
        "project_id": project_id,
        "job_id": job_id,
        "status": "pending_review",  # pending_review | approved
        "approved_at": None,
        "approved_by": None,
        "approval_rationale": None,
        "gap_summary_at_approval": None,
    }


def _save_qa_state(project_id: str, job_id: str, state: dict) -> None:
    path = _get_qa_state_path(project_id, job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, default=str))


def _load_extraction_records(project_id: str, job_id: str) -> list[dict]:
    """Load extraction records from JSON persistence.

    Records are stored per-job during extraction. Returns list of record dicts.
    """
    path = Path("data") / "projects" / project_id / "records" / f"{job_id}.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return data.get("records", []) if isinstance(data, dict) else data
    except Exception:
        return []


def _load_merge_groups(project_id: str, job_id: str) -> list[dict]:
    """Load merge group data if available."""
    path = Path("data") / "projects" / project_id / "merge_groups" / f"{job_id}.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return data.get("groups", []) if isinstance(data, dict) else data
    except Exception:
        return []


def _load_document_groups(project_id: str, job_id: str) -> list[dict]:
    """Load document groups from segregation."""
    path = Path("data") / "projects" / project_id / "segregation" / f"{job_id}.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return data.get("groups", []) if isinstance(data, dict) else data
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/summary")
def qa_summary(
    project_id: str,
    job_id: str = Query(..., description="Job/run ID"),
    db: Session = Depends(get_db),
):
    """Return extraction QA summary dashboard data."""
    records = _load_extraction_records(project_id, job_id)
    gaps = load_gaps(project_id, job_id)
    qa_state = _load_qa_state(project_id, job_id)

    # Compute stats
    total_records = len(records)
    total_documents = len(set(r.get("source_document_id", "") for r in records))
    total_pages = len(set(
        (r.get("source_document_id", ""), str(r.get("page_range", "")))
        for r in records
    ))

    # Gap stats
    total_gaps = len(gaps)
    filled_gaps = sum(1 for g in gaps if g.fill_result == "filled")
    unfilled_gaps = sum(1 for g in gaps if g.fill_result == "unfilled")
    pending_gaps = sum(1 for g in gaps if g.fill_result == "pending")
    na_gaps = sum(1 for g in gaps if g.fill_result == "not_applicable")
    manual_gaps = sum(1 for g in gaps if g.filled_by == "manual")
    high_unfilled = sum(
        1 for g in gaps
        if g.severity == "high" and g.fill_result == "unfilled"
    )

    # Completeness percentage
    if total_gaps > 0:
        completeness = round(100 * (total_gaps - unfilled_gaps - pending_gaps) / total_gaps, 1)
    else:
        completeness = 100.0

    # Per-document breakdown
    doc_breakdown: dict[str, dict] = {}
    for r in records:
        doc_name = r.get("source_document_name", r.get("document_name", "Unknown"))
        if doc_name not in doc_breakdown:
            doc_breakdown[doc_name] = {"records": 0, "pages": set()}
        doc_breakdown[doc_name]["records"] += 1
        doc_breakdown[doc_name]["pages"].add(str(r.get("page_range", "")))

    per_document = [
        {
            "document_name": name,
            "record_count": info["records"],
            "page_count": len(info["pages"]),
        }
        for name, info in sorted(doc_breakdown.items(), key=lambda x: -x[1]["records"])
    ]

    return {
        "project_id": project_id,
        "job_id": job_id,
        "status": qa_state.get("status", "pending_review"),
        "approved_at": qa_state.get("approved_at"),
        "approved_by": qa_state.get("approved_by"),
        "stats": {
            "total_notification_subjects": total_records,
            "total_documents": total_documents,
            "total_pages": total_pages,
            "completeness_pct": completeness,
        },
        "gaps": {
            "total": total_gaps,
            "filled": filled_gaps,
            "unfilled": unfilled_gaps,
            "pending": pending_gaps,
            "not_applicable": na_gaps,
            "manually_resolved": manual_gaps,
            "high_severity_unfilled": high_unfilled,
        },
        "per_document": per_document,
    }


@router.get("/samples")
def qa_samples(
    project_id: str,
    job_id: str = Query(..., description="Job/run ID"),
    max_samples: int = Query(20, description="Maximum samples to return"),
    db: Session = Depends(get_db),
):
    """Return curated sample records for auditor review."""
    records = _load_extraction_records(project_id, job_id)
    gaps_raw = load_gaps(project_id, job_id)
    gaps_dicts = [g.to_dict() for g in gaps_raw]
    merge_groups = _load_merge_groups(project_id, job_id)
    doc_groups = _load_document_groups(project_id, job_id)

    sampler = QASampler(max_samples=max_samples)
    samples = sampler.select(
        records=records,
        gaps=gaps_dicts,
        merge_groups=merge_groups,
        document_groups=doc_groups,
    )

    return {
        "project_id": project_id,
        "job_id": job_id,
        "total_samples": len(samples),
        "samples": [s.to_dict() for s in samples],
    }


@router.get("/gaps")
def qa_gaps(
    project_id: str,
    job_id: str = Query(..., description="Job/run ID"),
    status: str | None = Query(None, description="Filter: unfilled, filled, pending"),
    severity: str | None = Query(None, description="Filter: high, medium, low"),
    db: Session = Depends(get_db),
):
    """Return extraction gaps for manual review."""
    gaps = load_gaps(project_id, job_id)

    # Apply filters
    if status:
        gaps = [g for g in gaps if g.fill_result == status]
    if severity:
        gaps = [g for g in gaps if g.severity == severity]

    return {
        "project_id": project_id,
        "job_id": job_id,
        "total": len(gaps),
        "gaps": [g.to_dict() for g in gaps],
    }


@router.post("/gaps/{gap_index}/resolve")
def qa_resolve_gap(
    project_id: str,
    gap_index: int,
    body: ResolveGapBody,
    job_id: str = Query(..., description="Job/run ID"),
    db: Session = Depends(get_db),
):
    """Manually resolve a gap.

    Actions:
    - resolve: auditor provides the value visible on the source image
    - mark_na: field not applicable for this page
    - mark_unrecoverable: field present but unreadable
    """
    gaps = load_gaps(project_id, job_id)
    if gap_index < 0 or gap_index >= len(gaps):
        raise HTTPException(status_code=404, detail=f"Gap index {gap_index} not found")

    gap = gaps[gap_index]

    if body.action == "resolve":
        if not body.value:
            raise HTTPException(status_code=400, detail="Value required for resolve action")
        masked = _mask_value(body.value, gap.expected_field)
        gap.fill_result = "filled"
        gap.filled_by = "manual"
        gap.fill_method = "manual"
        gap.filled_value_masked = masked
        gap.fill_attempted = True

    elif body.action == "mark_na":
        gap.fill_result = "not_applicable"
        gap.filled_by = "manual"
        gap.fill_attempted = True
        if body.notes:
            gap.context = (gap.context or "") + f" [N/A: {body.notes}]"

    elif body.action == "mark_unrecoverable":
        gap.fill_result = "unfilled"
        gap.filled_by = "manual"
        gap.fill_attempted = True
        if body.notes:
            gap.context = (gap.context or "") + f" [Unrecoverable: {body.notes}]"

    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {body.action}")

    persist_gaps(gaps, project_id, job_id)

    return {
        "status": "ok",
        "gap_index": gap_index,
        "action": body.action,
        "gap": gap.to_dict(),
    }


@router.post("/approve")
def qa_approve(
    project_id: str,
    body: ApproveBody,
    job_id: str = Query(..., description="Job/run ID"),
    db: Session = Depends(get_db),
):
    """Approve extraction for notification.

    Gated: cannot approve if unresolved high-severity gaps exist.
    All high-severity gaps must be resolved, marked N/A, or marked unrecoverable.
    """
    gaps = load_gaps(project_id, job_id)

    # Check gating: no unresolved high-severity gaps
    high_unresolved = [
        g for g in gaps
        if g.severity == "high"
        and g.fill_result in ("pending", "unfilled")
        and g.filled_by != "manual"
    ]

    if high_unresolved:
        return {
            "status": "blocked",
            "reason": f"{len(high_unresolved)} unresolved high-severity gaps must be addressed before approval",
            "unresolved_gaps": [g.to_dict() for g in high_unresolved[:5]],
        }

    # Build gap summary at time of approval
    gap_summary = {
        "total": len(gaps),
        "filled": sum(1 for g in gaps if g.fill_result == "filled"),
        "unfilled": sum(1 for g in gaps if g.fill_result == "unfilled"),
        "not_applicable": sum(1 for g in gaps if g.fill_result == "not_applicable"),
        "manually_resolved": sum(1 for g in gaps if g.filled_by == "manual"),
    }

    # Save approval state
    now = datetime.now(timezone.utc).isoformat()
    qa_state = _load_qa_state(project_id, job_id)
    qa_state["status"] = "approved"
    qa_state["approved_at"] = now
    qa_state["approved_by"] = body.reviewer_id
    qa_state["approval_rationale"] = body.rationale
    qa_state["gap_summary_at_approval"] = gap_summary
    _save_qa_state(project_id, job_id, qa_state)

    return {
        "status": "approved",
        "approved_at": now,
        "approved_by": body.reviewer_id,
        "gap_summary": gap_summary,
    }
