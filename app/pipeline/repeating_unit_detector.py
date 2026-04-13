"""Repeating Unit Detection (Step 37b).

Detects document structure by finding context markers — fixed text labels
that appear before/after person names on every page. Used to auto-select
extraction strategy:
  - Markers found → Strategy A (marker-filter: Python filters, LLM extracts snippets)
  - No markers → Strategy B (full text batch: LLM reads entire pages)

One LLM call with 2 sample pages. Memory-safe page streaming.
"""
from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)


def detect_markers(
    doc_path: str,
    ollama_client,
    onset_page: int = 0,
) -> dict:
    """Detect context markers in a PDF document.

    Sends 2 sample pages (onset + middle) to the LLM and asks for
    fixed text labels that bracket person names.

    Parameters
    ----------
    doc_path:
        Path to the PDF file.
    ollama_client:
        OllamaClient instance (should be 32b for best accuracy).
    onset_page:
        First content page (from onset detection).

    Returns
    -------
    dict with keys:
        name_after_label: str — fixed label BEFORE person name (empty if none)
        name_before_label: str — fixed label AFTER person name/address (empty if none)
        sample_name: str — a person name found during detection
        sample_address: str — their address
        strategy: "A" (markers found) or "B" (no markers)
    """
    import fitz

    doc = fitz.open(doc_path)
    total = doc.page_count

    # Sample 2 pages: onset and middle
    mid = min(total - 1, total // 2)
    sample_pages = [onset_page, mid]

    sample_text = ""
    for pg in sample_pages:
        if 0 <= pg < total:
            text = doc[pg].get_text()[:2500]
            sample_text += f"\n--- PAGE {pg + 1} ---\n{text}\n"
            doc._forget_page(pg)
    doc.close()

    if not sample_text.strip():
        return {"strategy": "B", "name_after_label": "", "name_before_label": ""}

    prompt = (
        "Look at these document pages. Find FIXED TEXT LABELS that appear "
        "before and after person names.\n\n"
        "Return JSON:\n"
        '{\n'
        '  "name_after_label": "the fixed label/text that appears on the line '
        'BEFORE a person name — must be a LABEL that repeats on every page '
        '(like Employee ID:, Client:, Name:), NOT a person name itself",\n'
        '  "name_before_label": "the fixed label/text that appears AFTER '
        'the person name and address block",\n'
        '  "sample_name": "an actual person name you found",\n'
        '  "sample_address": "their address if visible"\n'
        '}\n\n'
        "IMPORTANT:\n"
        "- name_after_label must be FIXED TEXT that appears on EVERY page, "
        "not a person's name\n"
        "- If there are NO fixed labels before person names (names appear "
        "without any preceding label), set name_after_label to empty string\n"
        "- Return ONLY JSON\n\n"
        f"{sample_text}"
    )

    try:
        response = ollama_client.generate(
            prompt=prompt,
            system="You are a document structure analyst. Return only JSON.",
            use_case="marker_detection",
        )

        # Parse JSON
        cleaned = response.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            cleaned = "\n".join(lines)

        match = re.search(r'\{.*\}', cleaned, re.DOTALL)
        if match:
            result = json.loads(match.group())
        else:
            result = {}

    except Exception:
        logger.warning("Marker detection failed for %s", doc_path, exc_info=True)
        result = {}

    # Determine strategy
    name_after = (result.get("name_after_label") or "").strip()
    name_before = (result.get("name_before_label") or "").strip()

    # Validate markers are actual labels (>3 chars, not empty)
    has_markers = (len(name_after) > 3 or len(name_before) > 3)

    result["strategy"] = "A" if has_markers else "B"
    result["name_after_label"] = name_after
    result["name_before_label"] = name_before

    logger.info(
        "Marker detection: strategy=%s, name_after='%s', name_before='%s' for %s",
        result["strategy"], name_after[:30], name_before[:30], doc_path,
    )

    return result


def filter_page_by_markers(
    page_text: str,
    name_after_label: str,
    name_before_label: str,
    lines_before: int = 3,
    lines_after: int = 5,
) -> str | None:
    """Filter a page's text to only the lines around the context markers.

    Returns the snippet (5-10 lines) or None if marker not found on this page.
    """
    lines = page_text.split("\n")
    search_label = name_after_label or name_before_label

    if not search_label or len(search_label) < 3:
        return None

    # Find the marker line
    marker_idx = None
    search_lower = search_label.lower()[:20]
    for i, line in enumerate(lines):
        if search_lower in line.lower():
            marker_idx = i
            break

    if marker_idx is None:
        return None

    # Extract snippet around the marker
    if name_after_label:
        # Name appears AFTER this label — grab lines before and after marker
        start = max(0, marker_idx - lines_before)
        end = min(len(lines), marker_idx + lines_after)
    else:
        # Name appears BEFORE this label — grab preceding lines
        start = max(0, marker_idx - lines_after)
        end = min(len(lines), marker_idx + lines_before)

    snippet = "\n".join(l.strip() for l in lines[start:end] if l.strip())
    return snippet if len(snippet) > 10 else None
