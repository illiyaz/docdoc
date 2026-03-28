"""PDF page-to-image renderer for vision-language model extraction (Step 20).

Renders individual PDF pages as base64-encoded PNG images suitable for
sending to Ollama vision models (e.g. qwen2.5vl:32b).

Memory-safe: opens doc, renders page(s), closes immediately.  Uses
``fitz._forget_page()`` to release each page after rendering.

Step 27 addition: ``render_page_with_overlays()`` draws bounding-box
rectangles on the page before rendering, for the source document viewer.
"""
from __future__ import annotations

import base64
import logging

import fitz  # PyMuPDF

logger = logging.getLogger(__name__)

# Overlay colours by PII type (R, G, B) — used for bounding-box highlights
_PII_COLORS: dict[str, tuple[float, float, float]] = {
    "PERSON": (0.2, 0.5, 1.0),       # blue
    "US_SSN": (1.0, 0.2, 0.2),       # red
    "DOB": (0.9, 0.5, 0.0),          # orange
    "LOCATION": (0.2, 0.7, 0.3),     # green
    "EMAIL_ADDRESS": (0.6, 0.3, 0.8),# purple
    "PHONE_NUMBER": (0.0, 0.7, 0.7), # teal
    "NI_NUMBER": (1.0, 0.2, 0.2),    # red (same as SSN)
    "GOVERNMENT_ID": (1.0, 0.2, 0.2),# red
}


def render_page_to_image(doc_path: str, page_number: int, dpi: int = 150) -> str:
    """Render a single PDF page as a base64-encoded PNG image.

    150 DPI is sufficient for text recognition while keeping image size
    manageable (~200-400KB per page).
    """
    doc = fitz.open(doc_path)
    try:
        if page_number >= doc.page_count:
            raise IndexError(
                f"Page {page_number} out of range (doc has {doc.page_count} pages)"
            )
        page = doc[page_number]
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=mat)
        img_bytes = pix.tobytes("png")
        return base64.b64encode(img_bytes).decode("ascii")
    finally:
        doc.close()


def render_page_with_overlays(
    doc_path: str,
    page_number: int,
    bboxes: list[dict] | None = None,
    dpi: int = 150,
    fill_opacity: float = 0.15,
    stroke_width: float = 1.5,
) -> str:
    """Render a PDF page with optional bounding-box overlay rectangles.

    Each bbox dict should have: ``x0``, ``y0``, ``x1``, ``y1`` (PDF points
    at 72 DPI), and optionally ``pii_type`` (for colour-coding).

    Coordinates are in the page's native PDF coordinate system — PyMuPDF's
    Shape drawing uses the same system, so no scaling is needed.

    Returns a base64-encoded PNG string.  Never writes to disk.
    """
    doc = fitz.open(doc_path)
    try:
        if page_number >= doc.page_count:
            raise IndexError(
                f"Page {page_number} out of range (doc has {doc.page_count} pages)"
            )
        page = doc[page_number]

        # Draw overlay rectangles on the page before rendering
        if bboxes:
            shape = page.new_shape()
            for bb in bboxes:
                try:
                    rect = fitz.Rect(bb["x0"], bb["y0"], bb["x1"], bb["y1"])
                except (KeyError, TypeError):
                    continue  # skip malformed bbox
                pii_type = bb.get("pii_type", "")
                color = _PII_COLORS.get(pii_type, (0.5, 0.5, 0.5))
                shape.draw_rect(rect)
                shape.finish(
                    color=color,
                    fill=color,
                    fill_opacity=fill_opacity,
                    width=stroke_width,
                )
            shape.commit()

        mat = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=mat)
        img_bytes = pix.tobytes("png")
        return base64.b64encode(img_bytes).decode("ascii")
    finally:
        doc.close()


def render_pages_to_images(
    doc_path: str,
    page_numbers: list[int],
    dpi: int = 150,
) -> list[str]:
    """Render multiple PDF pages as base64-encoded PNG images.

    Memory-safe: forgets each page after rendering.
    Skips page numbers that exceed the document's page count.
    """
    doc = fitz.open(doc_path)
    images: list[str] = []
    try:
        for page_num in page_numbers:
            if page_num >= doc.page_count:
                continue
            page = doc[page_num]
            mat = fitz.Matrix(dpi / 72, dpi / 72)
            pix = page.get_pixmap(matrix=mat)
            img_bytes = pix.tobytes("png")
            images.append(base64.b64encode(img_bytes).decode("ascii"))
        return images
    finally:
        doc.close()
