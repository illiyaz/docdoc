"""Instance boundary detection for repeating template documents (Step 20).

Scans all pages of a PDF for repeating section headers to find where each
template instance (individual) starts.  Much more reliable than fixed
``pages_per_instance`` because it handles variable-length instances.

Falls back to ``None`` if no markers are found — caller should use
``DocumentTemplate.get_instance_pages()`` instead.
"""
from __future__ import annotations

import logging

import fitz  # PyMuPDF

logger = logging.getLogger(__name__)

# Generic markers that commonly indicate the start of a new individual's
# record in breach notification datasets.
DEFAULT_FALLBACK_MARKERS: list[str] = [
    "SUMMARY OF DETAILS IN RESPECT OF",
    "IN RESPECT OF:",
    "MEMBER RECORD",
    "EMPLOYEE RECORD",
    "PATIENT RECORD",
    "CLAIMANT DETAILS",
    "INDIVIDUAL DETAILS",
    "PERSONAL DETAILS",
    "NOTIFICATION SUBJECT",
]


def find_instance_boundaries(
    doc_path: str,
    instance_marker: str | None = None,
    fallback_markers: list[str] | None = None,
) -> list[list[int]] | None:
    """Find where each template instance starts by scanning for repeating headers.

    Args:
        doc_path: path to the PDF file.
        instance_marker: specific text to look for (from DocumentSchema.template).
        fallback_markers: generic markers to try if instance_marker not set.

    Returns:
        List of page groupings per instance: ``[[0,1,2], [3,4,5], ...]``,
        or ``None`` if no markers are found (caller should fall back to
        fixed ``pages_per_instance``).
    """
    if fallback_markers is None:
        fallback_markers = DEFAULT_FALLBACK_MARKERS

    markers = [instance_marker] if instance_marker else fallback_markers

    doc = fitz.open(doc_path)
    boundary_pages: list[int] = []

    try:
        total_pages = doc.page_count
        for page_num in range(total_pages):
            page = doc[page_num]
            # Only read the first 500 chars of each page for speed
            text = page.get_text()[:500].upper()

            for marker in markers:
                if marker.upper() in text:
                    boundary_pages.append(page_num)
                    break
    finally:
        doc.close()

    if not boundary_pages:
        return None

    # Need at least 2 boundaries for a repeating template
    if len(boundary_pages) < 2:
        return None

    # Convert boundary pages to instance page ranges
    instances: list[list[int]] = []
    for i, start in enumerate(boundary_pages):
        end = boundary_pages[i + 1] if i + 1 < len(boundary_pages) else total_pages
        instances.append(list(range(start, end)))

    logger.info(
        "Found %d instance boundaries in %s using marker scan (%d pages)",
        len(instances), doc_path, total_pages,
    )

    return instances
