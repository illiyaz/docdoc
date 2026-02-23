"""Human review queue routes — Phase 4.

All values returned to the frontend must be masked.  Raw PII must never
appear in any response from these routes (enforced by PIIFilterMiddleware).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_queue_manager, get_workflow_engine
from app.db.models import NotificationSubject
from app.review.queue_manager import QueueManager
from app.review.roles import VALID_QUEUE_TYPES
from app.review.workflow import WorkflowEngine

router = APIRouter(prefix="/review", tags=["review"])


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------

class AssignBody(BaseModel):
    reviewer_id: str
    role: str


class CompleteBody(BaseModel):
    reviewer_id: str
    role: str
    decision: str
    rationale: str
    regulatory_basis: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _serialize_task(task):
    return {
        "review_task_id": str(task.review_task_id),
        "queue_type": task.queue_type,
        "subject_id": str(task.subject_id) if task.subject_id else None,
        "assigned_to": task.assigned_to,
        "status": task.status,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "required_role": task.required_role,
    }


# Mapping: decision → target workflow status
_DECISION_TARGET: dict[str, str] = {
    "approved": "APPROVED",
    "rejected": "REJECTED",
    "escalated": "LEGAL_REVIEW",
}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/queues", summary="Counts per queue type")
def get_queue_counts(qm: QueueManager = Depends(get_queue_manager)):
    return {qt: len(qm.get_queue(qt)) for qt in sorted(VALID_QUEUE_TYPES)}


@router.get("/queues/{queue_type}", summary="List PENDING tasks for a queue")
def get_queue(queue_type: str, qm: QueueManager = Depends(get_queue_manager)):
    if queue_type not in VALID_QUEUE_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid queue_type: {queue_type!r}")
    tasks = qm.get_queue(queue_type)
    return [_serialize_task(t) for t in tasks]


@router.post("/tasks/{task_id}/assign", summary="Assign a review task")
def assign_task(
    task_id: str,
    body: AssignBody,
    qm: QueueManager = Depends(get_queue_manager),
):
    try:
        task = qm.assign_task(task_id, body.reviewer_id, body.role)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"ReviewTask {task_id} not found")
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _serialize_task(task)


@router.post("/tasks/{task_id}/complete", summary="Complete a review task")
def complete_task(
    task_id: str,
    body: CompleteBody,
    db: Session = Depends(get_db),
    qm: QueueManager = Depends(get_queue_manager),
    wf: WorkflowEngine = Depends(get_workflow_engine),
):
    # 1. Complete the review task
    try:
        task = qm.complete_task(
            task_id,
            body.reviewer_id,
            body.role,
            decision=body.decision,
            rationale=body.rationale,
            db_session_audit=db,
            regulatory_basis=body.regulatory_basis,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"ReviewTask {task_id} not found")
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # 2. Transition the subject workflow if task has a subject
    subject_review_status = None
    if task.subject_id:
        target = _DECISION_TARGET.get(body.decision)
        if target:
            subject = db.get(NotificationSubject, task.subject_id)
            if subject and wf.can_transition(subject.review_status, target):
                try:
                    wf.transition(
                        str(task.subject_id),
                        target,
                        actor=body.reviewer_id,
                        rationale=body.rationale,
                        regulatory_basis=body.regulatory_basis,
                    )
                    subject_review_status = target
                except (ValueError, KeyError):
                    pass  # transition not valid from current state — skip
            elif subject:
                subject_review_status = subject.review_status

    result = _serialize_task(task)
    result["subject_review_status"] = subject_review_status
    return result
