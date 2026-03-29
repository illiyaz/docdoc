"""Source document viewer API (Step 27 — Critical #1).

Endpoints for on-demand PDF page rendering with optional bounding-box
overlays.  Page images are rendered in-memory via PyMuPDF and returned
as base64 PNG — never cached to disk (breach data).

Phase 6: add ``Depends(get_current_user)`` for auth gating.
"""
from __future__ import annotations

import logging
from pathlib import Path
from uuid import UUID

import fitz  # PyMuPDF
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.db.models import Document, Extraction
from app.pdf.renderer import render_page_with_overlays, render_page_to_image

logger = logging.getLogger(__name__)
router = APIRouter(tags=["documents"])


# ------------------------------------------------------------------
# GET /documents/{document_id}/info
# ------------------------------------------------------------------
@router.get("/documents/{document_id}/info")
def get_document_info(
    document_id: UUID,
    db: Session = Depends(get_db),
):
    """Return document metadata needed for the viewer (page count, file type, onset page)."""
    doc = db.query(Document).filter(Document.id == document_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    is_pdf = (doc.file_type or "").lower() in ("pdf", "application/pdf")
    page_count = doc.page_count

    # If page_count is missing and it's a PDF, get it from PyMuPDF
    if page_count is None and is_pdf and Path(doc.source_path).exists():
        try:
            pdf = fitz.open(doc.source_path)
            page_count = pdf.page_count
            pdf.close()
        except Exception:
            page_count = None

    return {
        "document_id": str(doc.id),
        "file_name": doc.file_name,
        "file_type": doc.file_type,
        "page_count": page_count,
        "onset_page": doc.content_onset_page,
        "is_pdf": is_pdf,
    }


# ------------------------------------------------------------------
# GET /documents/{document_id}/pages/{page_number}
# ------------------------------------------------------------------
@router.get("/documents/{document_id}/pages/{page_number}")
def get_document_page(
    document_id: UUID,
    page_number: int,
    db: Session = Depends(get_db),
    dpi: int = 150,
    highlight_extractions: bool = False,
):
    """Render a single PDF page as base64 PNG with optional bbox overlays."""
    if page_number < 0:
        raise HTTPException(status_code=400, detail="page_number must be >= 0")

    doc = db.query(Document).filter(Document.id == document_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    is_pdf = (doc.file_type or "").lower() in ("pdf", "application/pdf")
    if not is_pdf:
        raise HTTPException(status_code=422, detail="Only PDF documents can be rendered")

    source = Path(doc.source_path)
    if not source.exists():
        raise HTTPException(status_code=404, detail="Source file not found on disk")

    # Validate page range
    try:
        pdf = fitz.open(str(source))
        total_pages = pdf.page_count
        pdf.close()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Cannot open PDF: {exc}")

    if page_number >= total_pages:
        raise HTTPException(
            status_code=400,
            detail=f"Page {page_number} out of range (document has {total_pages} pages)",
        )

    # Gather bounding boxes from extractions if requested
    highlighted = []
    bboxes = None
    if highlight_extractions:
        extractions = (
            db.query(Extraction)
            .filter(
                Extraction.document_id == document_id,
                Extraction.evidence_page == page_number,
            )
            .all()
        )
        bboxes = []
        for ext in extractions:
            bbox_data = ext.evidence_bbox
            entry = {
                "extraction_id": str(ext.id),
                "pii_type": ext.pii_type,
                "bbox": None,
                "masked_value": ext.masked_value,
            }
            if bbox_data and all(k in bbox_data for k in ("x0", "y0", "x1", "y1")):
                bboxes.append({**bbox_data, "pii_type": ext.pii_type})
                entry["bbox"] = [bbox_data["x0"], bbox_data["y0"], bbox_data["x1"], bbox_data["y1"]]
            highlighted.append(entry)

    # Render
    if bboxes:
        image_base64 = render_page_with_overlays(str(source), page_number, bboxes, dpi=dpi)
    else:
        image_base64 = render_page_to_image(str(source), page_number, dpi=dpi)

    return {
        "document_id": str(doc.id),
        "page_number": page_number,
        "page_count": total_pages,
        "image_base64": image_base64,
        "highlighted_extractions": highlighted,
    }


# ------------------------------------------------------------------
# GET /subjects/{subject_id}/source-pages
# ------------------------------------------------------------------
@router.get("/subjects/{subject_id}/source-pages")
def get_subject_source_pages(
    subject_id: UUID,
    db: Session = Depends(get_db),
):
    """Return source documents and pages relevant to a notification subject.

    Groups extractions by document and page so the frontend knows which
    pages to request from the document viewer.
    """
    from app.db.models import NotificationSubject, PersonLink, PersonEntity

    subject = db.query(NotificationSubject).filter(NotificationSubject.subject_id == subject_id).first()
    if not subject:
        raise HTTPException(status_code=404, detail="Subject not found")

    # Find extractions linked to this subject via PersonEntity → PersonLink → Extraction
    entities = (
        db.query(PersonEntity)
        .filter(PersonEntity.canonical_name == subject.canonical_name)
        .all()
    )
    entity_ids = [e.id for e in entities]

    if not entity_ids:
        return {"subject_id": str(subject_id), "source_documents": []}

    links = (
        db.query(PersonLink)
        .filter(PersonLink.entity_id.in_(entity_ids))
        .all()
    )
    extraction_ids = [lnk.extraction_id for lnk in links]

    if not extraction_ids:
        return {"subject_id": str(subject_id), "source_documents": []}

    extractions = (
        db.query(Extraction)
        .filter(Extraction.id.in_(extraction_ids))
        .all()
    )

    # Group by document → page
    doc_map: dict[UUID, dict] = {}
    for ext in extractions:
        did = ext.document_id
        if did not in doc_map:
            doc_row = db.query(Document).filter(Document.id == did).first()
            is_pdf = (doc_row.file_type or "").lower() in ("pdf", "application/pdf") if doc_row else False
            doc_map[did] = {
                "document_id": str(did),
                "file_name": doc_row.file_name if doc_row else "unknown",
                "is_pdf": is_pdf,
                "pages": {},
            }

        page_num = ext.evidence_page
        if page_num is None:
            continue

        pages = doc_map[did]["pages"]
        if page_num not in pages:
            pages[page_num] = []
        pages[page_num].append({
            "extraction_id": str(ext.id),
            "pii_type": ext.pii_type,
            "masked_value": ext.masked_value,
            "evidence_bbox": ext.evidence_bbox,
        })

    # Convert to list format
    source_documents = []
    for doc_info in doc_map.values():
        pages_list = [
            {"page_number": pn, "extractions": exts}
            for pn, exts in sorted(doc_info["pages"].items())
        ]
        source_documents.append({
            "document_id": doc_info["document_id"],
            "file_name": doc_info["file_name"],
            "is_pdf": doc_info["is_pdf"],
            "pages": pages_list,
        })

    return {"subject_id": str(subject_id), "source_documents": source_documents}
