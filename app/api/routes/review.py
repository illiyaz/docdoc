"""Human review queue routes — stubs (Phase 1 skeleton).

All values returned to the frontend must be masked.  Raw PII must never
appear in any response from these routes (enforced by PIIFilterMiddleware).
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/review", tags=["review"])

_NOT_IMPLEMENTED = {"detail": "not yet implemented"}


@router.get("/queue", status_code=501, summary="List pending review items")
async def get_review_queue() -> JSONResponse:
    return JSONResponse(status_code=501, content=_NOT_IMPLEMENTED)


@router.post(
    "/{record_id}/approve",
    status_code=501,
    summary="Approve an extraction record",
)
async def approve_record(record_id: str) -> JSONResponse:
    return JSONResponse(status_code=501, content=_NOT_IMPLEMENTED)


@router.post(
    "/{record_id}/reject",
    status_code=501,
    summary="Reject an extraction record",
)
async def reject_record(record_id: str) -> JSONResponse:
    return JSONResponse(status_code=501, content=_NOT_IMPLEMENTED)
