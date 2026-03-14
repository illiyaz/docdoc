"""PDF page-to-image renderer for vision-language model extraction (Step 20).

Renders individual PDF pages as base64-encoded PNG images suitable for
sending to Ollama vision models (e.g. qwen2.5vl:32b).

Memory-safe: opens doc, renders page(s), closes immediately.  Uses
``fitz._forget_page()`` to release each page after rendering.
"""
from __future__ import annotations

import base64

import fitz  # PyMuPDF


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
