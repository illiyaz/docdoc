"""Extraction Gap endpoints (Step 30e-6).

Provides read access to detected and filled gaps, plus manual resolution.

GET  /projects/{project_id}/gaps            — list all gaps for a job
GET  /projects/{project_id}/gaps/summary    — summary stats (filled/unfilled/pending)
POST /projects/{project_id}/gaps/{index}/resolve — manually resolve a gap
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/projects/{project_id}/gaps", tags=["gaps"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class ResolveGapBody(BaseModel):
    """Manual resolution of a gap."""
    value: str | None = None  # the manually entered value (will be masked on save)
    action: str = "resolve"   # "resolve" | "mark_na" | "mark_unrecoverable"
    reviewer_id: str = "auditor"


class GapSummary(BaseModel):
    """Summary stats for extraction gaps."""
    total: int = 0
    filled: int = 0
    unfilled: int = 0
    pending: int = 0
    not_applicable: int = 0
    manually_resolved: int = 0
    high_severity_unfilled: int = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_gaps_path(project_id: str, job_id: str) -> Path:
    """Return path to gap JSON file."""
    return Path("data") / "projects" / project_id / "gaps" / f"{job_id}.json"


def _load_gap_data(project_id: str, job_id: str) -> dict:
    """Load gap data from JSON file."""
    path = _get_gaps_path(project_id, job_id)
    if not path.exists():
        return {"gaps": [], "total_gaps": 0, "filled": 0, "unfilled": 0, "pending": 0}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {"gaps": [], "total_gaps": 0, "filled": 0, "unfilled": 0, "pending": 0}


def _save_gap_data(project_id: str, job_id: str, data: dict) -> None:
    """Save gap data to JSON file."""
    path = _get_gaps_path(project_id, job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str))


def _recompute_counts(data: dict) -> dict:
    """Recompute summary counts from gap list."""
    gaps = data.get("gaps", [])
    data["total_gaps"] = len(gaps)
    data["filled"] = sum(1 for g in gaps if g.get("fill_result") == "filled")
    data["unfilled"] = sum(1 for g in gaps if g.get("fill_result") == "unfilled")
    data["pending"] = sum(1 for g in gaps if g.get("fill_result") == "pending")
    return data


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/")
def list_gaps(
    project_id: str,
    job_id: str = Query(..., description="Job/run ID"),
    severity: str | None = Query(None, description="Filter by severity (high/medium/low)"),
    gap_type: str | None = Query(None, description="Filter by type (empty_page/missing_field/truncated)"),
    fill_result: str | None = Query(None, description="Filter by fill result"),
    db: Session = Depends(get_db),
):
    """List all gaps for a job, with optional filters."""
    data = _load_gap_data(project_id, job_id)
    gaps = data.get("gaps", [])

    # Apply filters
    if severity:
        gaps = [g for g in gaps if g.get("severity") == severity]
    if gap_type:
        gaps = [g for g in gaps if g.get("gap_type") == gap_type]
    if fill_result:
        gaps = [g for g in gaps if g.get("fill_result") == fill_result]

    return {
        "project_id": project_id,
        "job_id": job_id,
        "total": len(gaps),
        "gaps": gaps,
    }


@router.get("/summary")
def gap_summary(
    project_id: str,
    job_id: str = Query(..., description="Job/run ID"),
    db: Session = Depends(get_db),
):
    """Return summary stats for extraction gaps."""
    data = _load_gap_data(project_id, job_id)
    gaps = data.get("gaps", [])

    summary = GapSummary(
        total=len(gaps),
        filled=sum(1 for g in gaps if g.get("fill_result") == "filled"),
        unfilled=sum(1 for g in gaps if g.get("fill_result") == "unfilled"),
        pending=sum(1 for g in gaps if g.get("fill_result") == "pending"),
        not_applicable=sum(1 for g in gaps if g.get("fill_result") == "not_applicable"),
        manually_resolved=sum(1 for g in gaps if g.get("filled_by") == "manual"),
        high_severity_unfilled=sum(
            1 for g in gaps
            if g.get("severity") == "high" and g.get("fill_result") == "unfilled"
        ),
    )

    return {
        "project_id": project_id,
        "job_id": job_id,
        "summary": summary.model_dump(),
    }


@router.post("/{gap_index}/resolve")
def resolve_gap(
    project_id: str,
    gap_index: int,
    body: ResolveGapBody,
    job_id: str = Query(..., description="Job/run ID"),
    db: Session = Depends(get_db),
):
    """Manually resolve a gap.

    Actions:
    - resolve: provide a value (will be masked for storage)
    - mark_na: mark as not applicable
    - mark_unrecoverable: mark as unrecoverable (will appear in QA report)
    """
    data = _load_gap_data(project_id, job_id)
    gaps = data.get("gaps", [])

    if gap_index < 0 or gap_index >= len(gaps):
        raise HTTPException(status_code=404, detail=f"Gap index {gap_index} not found")

    gap = gaps[gap_index]

    if body.action == "resolve":
        if not body.value:
            raise HTTPException(status_code=400, detail="Value required for resolve action")
        # Mask the value before storing (no raw PII on disk)
        from app.pipeline.gap_filler import _mask_value
        masked = _mask_value(body.value, gap.get("expected_field"))
        gap["fill_result"] = "filled"
        gap["filled_by"] = "manual"
        gap["fill_method"] = "manual"
        gap["filled_value_masked"] = masked
        gap["fill_attempted"] = True

    elif body.action == "mark_na":
        gap["fill_result"] = "not_applicable"
        gap["filled_by"] = "manual"
        gap["fill_attempted"] = True

    elif body.action == "mark_unrecoverable":
        gap["fill_result"] = "unfilled"
        gap["filled_by"] = "manual"
        gap["fill_attempted"] = True
        gap["context"] = (gap.get("context", "") or "") + f" [Marked unrecoverable by {body.reviewer_id}]"

    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {body.action}")

    data = _recompute_counts(data)
    _save_gap_data(project_id, job_id, data)

    return {
        "status": "ok",
        "gap_index": gap_index,
        "action": body.action,
        "gap": gap,
    }
