"""Repeating Unit Detection (Step 37b).

Detects document structure by sampling 9 pages (3 onset + 3 middle + 3 end)
and asking the LLM to describe the repeating pattern. Returns:

1. record_unit — page | block | row | multi_page
2. separator — what separates records (page_break, dashed_line, table_row, etc.)
3. records_per_page — expected count of persons per page
4. context_markers — fixed text labels that bracket person names
5. strategy — A (marker-filter), B (full text batch), or C (vision)

One LLM call with 9 sample pages. Memory-safe page streaming.
"""
from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 9-page sampling helper
# ---------------------------------------------------------------------------


def _compute_sample_pages(
    total_pages: int,
    onset_page: int,
) -> list[int]:
    """Select 9 pages: 3 from onset, 3 from middle, 3 from end.

    Falls back gracefully for short documents (returns all pages if < 9).
    """
    content_start = onset_page
    content_end = total_pages - 1

    if content_end - content_start < 8:
        # Short doc — sample all content pages
        return list(range(content_start, min(total_pages, content_start + 9)))

    # 3 onset pages
    onset_pages = [content_start, content_start + 1, content_start + 2]

    # 3 middle pages
    mid = (content_start + content_end) // 2
    mid_pages = [mid - 1, mid, mid + 1]

    # 3 end pages
    end_pages = [content_end - 2, content_end - 1, content_end]

    # Deduplicate and clamp
    seen = set()
    result = []
    for pg in onset_pages + mid_pages + end_pages:
        pg = max(0, min(pg, total_pages - 1))
        if pg not in seen:
            seen.add(pg)
            result.append(pg)

    return sorted(result)


# ---------------------------------------------------------------------------
# Main detection function
# ---------------------------------------------------------------------------


def detect_markers(
    doc_path: str,
    ollama_client,
    onset_page: int = 0,
) -> dict:
    """Detect repeating structure and context markers in a PDF document.

    Sends 9 sample pages (3 onset + 3 middle + 3 end) to the LLM and asks
    for the repeating pattern and fixed text labels.

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
        strategy: "A" (markers found) or "B" (no markers)
        record_unit: "page" | "block" | "row" | "multi_page"
        separator: "page_break" | "dashed_line" | "blank_lines" | ...
        records_per_page: int (expected persons per page)
        name_after_label: str — fixed label BEFORE person name
        name_before_label: str — fixed label AFTER person name/address
        sample_name: str — a person name found during detection
        sample_address: str — their address
        has_continuation: bool — whether records span page boundaries
    """
    import fitz

    doc = fitz.open(doc_path)
    total = doc.page_count

    # 9-page sampling
    sample_pages = _compute_sample_pages(total, onset_page)

    # Build sample text grouped by region
    onset_text = ""
    mid_text = ""
    end_text = ""
    mid_start = total // 3
    end_start = (2 * total) // 3

    for pg in sample_pages:
        if 0 <= pg < total:
            text = doc[pg].get_text()[:2500]
            entry = f"\n--- PAGE {pg + 1} ---\n{text}\n"
            if pg < mid_start:
                onset_text += entry
            elif pg < end_start:
                mid_text += entry
            else:
                end_text += entry
            doc._forget_page(pg)
    doc.close()

    all_text = onset_text + mid_text + end_text
    if not all_text.strip():
        return _default_result("B")

    # Build the combined prompt — asks for both structure AND markers
    prompt = (
        f"Here are {len(sample_pages)} pages from a document — "
        f"from the beginning, middle, and end.\n\n"
        "Analyze the REPEATING STRUCTURE and answer these questions:\n\n"
        "1. What represents ONE person's complete record?\n"
        "   (a full page, a block between separators, a table row, "
        "multiple pages)\n"
        "2. How do records separate?\n"
        "   (page break, dashed line, blank lines, header repeat, "
        "table row boundary)\n"
        "3. How many distinct person records appear per page?\n"
        "4. Do any records CONTINUE across page breaks?\n"
        "5. Are there FIXED TEXT LABELS that appear before and after "
        "person names on every page?\n"
        "   (like 'Employee ID:', 'Client:', 'Name:' — labels, NOT "
        "person names)\n\n"
        "Return JSON:\n"
        "{\n"
        '  "record_unit": "page | block | row | multi_page",\n'
        '  "separator": "page_break | dashed_line | blank_lines | '
        'header_repeat | table_row | none",\n'
        '  "separator_pattern": "exact text of separator if applicable",\n'
        '  "records_per_page": 1,\n'
        '  "has_continuation": false,\n'
        '  "name_after_label": "the fixed label that appears on the '
        'line BEFORE a person name — must repeat on every page, '
        'NOT a person name",\n'
        '  "name_before_label": "the fixed label that appears AFTER '
        'the person name and address block",\n'
        '  "sample_name": "an actual person name you found",\n'
        '  "sample_address": "their address if visible"\n'
        "}\n\n"
        "IMPORTANT:\n"
        "- name_after_label / name_before_label must be FIXED TEXT "
        "labels that appear on EVERY page, not person names\n"
        "- If no fixed labels exist, set them to empty string\n"
        "- record_unit is the MOST IMPORTANT field — get it right\n"
        "- Return ONLY valid JSON\n\n"
        f"{all_text}"
    )

    try:
        response = ollama_client.generate(
            prompt=prompt,
            system="You are a document structure analyst. Return only JSON.",
            use_case="repeating_unit_detection",
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
        logger.warning("Repeating unit detection failed for %s", doc_path, exc_info=True)
        result = {}

    # Normalize fields
    name_after = (result.get("name_after_label") or "").strip()
    name_before = (result.get("name_before_label") or "").strip()
    record_unit = (result.get("record_unit") or "page").strip().lower()
    separator = (result.get("separator") or "page_break").strip().lower()
    records_per_page = result.get("records_per_page", 1)
    has_continuation = result.get("has_continuation", False)

    # Validate record_unit
    valid_units = {"page", "block", "row", "multi_page"}
    if record_unit not in valid_units:
        record_unit = "page"

    # Coerce records_per_page to int
    try:
        records_per_page = int(records_per_page)
    except (TypeError, ValueError):
        records_per_page = 1

    # Validate markers are actual labels (>3 chars, not empty)
    has_markers = len(name_after) > 3 or len(name_before) > 3

    # Determine strategy
    strategy = "A" if has_markers else "B"

    final = {
        "strategy": strategy,
        "record_unit": record_unit,
        "separator": separator,
        "separator_pattern": (result.get("separator_pattern") or "").strip(),
        "records_per_page": records_per_page,
        "has_continuation": bool(has_continuation),
        "name_after_label": name_after,
        "name_before_label": name_before,
        "sample_name": (result.get("sample_name") or "").strip(),
        "sample_address": (result.get("sample_address") or "").strip(),
    }

    logger.info(
        "Repeating unit detection: strategy=%s, unit=%s, rpp=%d, "
        "separator=%s, markers=%s/%s for %s",
        strategy, record_unit, records_per_page,
        separator, name_after[:30], name_before[:30], doc_path,
    )

    return final


def _default_result(strategy: str = "B") -> dict:
    """Return a default result when detection can't run."""
    return {
        "strategy": strategy,
        "record_unit": "page",
        "separator": "page_break",
        "separator_pattern": "",
        "records_per_page": 1,
        "has_continuation": False,
        "name_after_label": "",
        "name_before_label": "",
        "sample_name": "",
        "sample_address": "",
    }


# ---------------------------------------------------------------------------
# Vision fallback for invisible separators
# ---------------------------------------------------------------------------


def detect_visual_separators(
    doc_path: str,
    ollama_client,
    page_num: int,
    vision_model: str | None = None,
) -> dict | None:
    """Check a single page for visual-only separators (horizontal rules,
    shaded rows, box borders) that are invisible to get_text().

    Only called when text analysis says record_unit="page" but the page
    has >4000 chars (suspiciously dense for a single record).

    Returns updated record_unit/separator or None if no visual separators.
    """
    try:
        from app.pdf.renderer import render_page_to_image
    except ImportError:
        return None

    try:
        image = render_page_to_image(doc_path, page_num, dpi=150)
    except Exception:
        return None

    prompt = (
        "Look at this document page. Does it contain VISUAL SEPARATORS "
        "between different person records? Look for:\n"
        "- Horizontal lines/rules\n"
        "- Shaded/alternating row backgrounds\n"
        "- Box borders around individual records\n"
        "- Dashed lines between blocks\n\n"
        "If YES, return:\n"
        '{"has_visual_separators": true, "separator_type": "horizontal_rule", '
        '"records_per_page": 3}\n\n'
        "If this page is ONE person's record (no separators), return:\n"
        '{"has_visual_separators": false}\n\n'
        "Return ONLY JSON."
    )

    try:
        response = ollama_client.generate_with_images(
            prompt=prompt,
            images=[image],
            use_case="visual_separator_detection",
            model_override=vision_model,
        )

        cleaned = response.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            cleaned = "\n".join(lines)

        match = re.search(r'\{.*\}', cleaned, re.DOTALL)
        if match:
            data = json.loads(match.group())
            if data.get("has_visual_separators"):
                return {
                    "record_unit": "block",
                    "separator": data.get("separator_type", "visual"),
                    "records_per_page": data.get("records_per_page", 2),
                }
        return None

    except Exception:
        logger.debug("Visual separator detection failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Page text filtering (Strategy A helper)
# ---------------------------------------------------------------------------


def filter_page_by_markers(
    page_text: str,
    name_after_label: str,
    name_before_label: str,
    lines_before: int = 3,
    lines_after: int = 5,
    additional_labels: list[str] | None = None,
) -> str | None:
    """Filter a page's text to lines around the name marker plus any
    supplementary labels (DOB, SSN, address, etc.).

    Without ``additional_labels`` this captures ~8 lines around the name
    marker only — fine for label-dense docs where DOB sits next to the
    name, but misses fields that live elsewhere on the page. Pass
    ``additional_labels`` (typically segregation's ``fields[].name``) to
    widen coverage while keeping the snippet short.

    Returns the merged snippet (roughly 5-30 lines, depending on how many
    labels matched) or ``None`` if the name marker isn't on this page.
    """
    lines = page_text.split("\n")

    if not (name_after_label or name_before_label):
        return None

    lower_lines = [ln.lower() for ln in lines]

    def _normalize_marker(m: str) -> str:
        """Strip leading section numbers / punctuation so "2. MEMBER DETAILS"
        matches a line that's just "MEMBER DETAILS" (the PDF may split
        "2." and the label onto separate lines)."""
        # Remove leading "1.", "2.1", "A.", "§", bullets, etc.
        s = re.sub(r"^[\s\d.\-§•]+", "", m).strip()
        return s.lower()[:20]

    # Try both markers. Multi-page member docs (pension/payroll) often
    # have the "before" marker ("2. MEMBER DETAILS") on a different page
    # than the "after" marker ("SUMMARY OF DETAILS IN RESPECT "); earlier
    # logic bailed on any page missing the primary, which dropped NI
    # labels. Now we accept either — widening is still performed below.
    candidates: list[tuple[str, int]] = []  # (which_label, marker_idx)
    if name_after_label and len(name_after_label) >= 3:
        nl = _normalize_marker(name_after_label)
        if nl:
            idx = next((i for i, ln in enumerate(lower_lines) if nl in ln), None)
            if idx is not None:
                candidates.append(("after", idx))
    if name_before_label and len(name_before_label) >= 3:
        nl = _normalize_marker(name_before_label)
        if nl:
            idx = next((i for i, ln in enumerate(lower_lines) if nl in ln), None)
            if idx is not None:
                candidates.append(("before", idx))

    if not candidates:
        return None

    # Build a set of line indices to include. Start with the marker window(s).
    keep: set[int] = set()
    for which, marker_idx in candidates:
        if which == "after":
            keep.update(range(max(0, marker_idx - lines_before),
                              min(len(lines), marker_idx + lines_after + 1)))
        else:
            keep.update(range(max(0, marker_idx - lines_after),
                              min(len(lines), marker_idx + lines_before + 1)))

    # Widen around each additional label that's actually present.
    for label in additional_labels or []:
        if not label or len(label) < 3:
            continue
        lbl_lower = label.lower()[:30]
        for i, ln in enumerate(lower_lines):
            if lbl_lower in ln:
                # Grab a few lines after the label (values usually follow)
                # and one line before (in case label+value are swapped).
                keep.update(range(max(0, i - 1),
                                  min(len(lines), i + lines_after + 1)))

    if not keep:
        return None

    snippet = "\n".join(
        lines[i].strip() for i in sorted(keep) if lines[i].strip()
    )
    return snippet if len(snippet) > 10 else None
