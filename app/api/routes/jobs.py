"""Job management routes — stubs (Phase 1 skeleton)."""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/jobs", tags=["jobs"])

_NOT_IMPLEMENTED = {"detail": "not yet implemented"}


@router.post("", status_code=501, summary="Submit a new extraction job")
async def create_job() -> JSONResponse:
    return JSONResponse(status_code=501, content=_NOT_IMPLEMENTED)


@router.get("/{job_id}", status_code=501, summary="Get job status")
async def get_job(job_id: str) -> JSONResponse:
    return JSONResponse(status_code=501, content=_NOT_IMPLEMENTED)


@router.get("/{job_id}/results", status_code=501, summary="Get job extraction results")
async def get_job_results(job_id: str) -> JSONResponse:
    return JSONResponse(status_code=501, content=_NOT_IMPLEMENTED)
