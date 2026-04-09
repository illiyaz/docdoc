"""Heuristic field-map builder for repeating fixed-layout documents.

Compares N pages line-by-line to find:
  - FIXED lines (identical across ≥80% of pages) → structure / anchors
  - VARYING lines (differ across pages) → candidate PII values

Then classifies the varying lines using simple heuristics + Presidio
entity types to build a layout_field_map without any LLM call.

Trigger: LLM document understanding returns valid semantic info
(document_type, template) but layout_type="variable" and no field map.

Gated behind ``HEURISTIC_FIELD_MAP_ENABLED`` feature flag (default True).
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass

from app.readers.base import ExtractedBlock
from app.structure.document_schema import DocumentSchema, FieldMapping

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# A line is "fixed" if it appears identically on ≥ this fraction of pages
_FIXED_THRESHOLD = 0.75

# Minimum pages needed to reliably detect repeating structure
_MIN_PAGES = 3

# Maximum lines from the top of each page to analyze
_MAX_LINES = 25


# ---------------------------------------------------------------------------
# Line classification helpers
# ---------------------------------------------------------------------------

# Patterns that suggest a line contains a person name
_PERSON_PATTERNS = [
    # "First Last" or "First M. Last" or "First & Second Last"
    re.compile(r"^[A-Z][a-z]+ (?:& [A-Z][a-z]+ )?(?:[A-Z]\.\s?)?[A-Z][a-z]+$"),
    # "FIRST M. LAST" (all caps)
    re.compile(r"^[A-Z]{2,} (?:[A-Z]\.\s?)?[A-Z]{2,}$"),
    # "Last, First" or "Last, First M." (mixed or upper case)
    re.compile(r"^[A-Z][A-Za-z]+,\s+[A-Z][A-Za-z]+"),
    # "LASTNAME, FIRSTNAME" (all uppercase)
    re.compile(r"^[A-Z]{2,},\s+[A-Z]{2,}"),
    # "First & Second Lastname" (parents)
    re.compile(r"^[A-Z][a-z]+ & [A-Z][a-z]+ [A-Z][a-z]+"),
    # "Mr K P Lastname" (titled)
    re.compile(r"^(?:Mr|Mrs|Ms|Dr|Miss)\s+[A-Z]"),
]

# Patterns that suggest a line is an address
_ADDRESS_PATTERNS = [
    # Street number + street name: "18415 BLUE RIDGE DR"
    re.compile(r"^\d+\s+[A-Z0-9 ]+(?:ST|AVE|DR|RD|LN|BLVD|CT|PL|WAY|CIR|HWY)\b", re.IGNORECASE),
    # "123 Main St" style
    re.compile(r"^\d+\s+\w+\s+(?:Street|Avenue|Drive|Road|Lane|Boulevard|Court|Place|Way)", re.IGNORECASE),
    # PO BOX
    re.compile(r"^P\.?O\.?\s*BOX\s+\d+", re.IGNORECASE),
]

# City, State ZIP pattern: "LYNNWOOD WA 98037" or "Lynnwood, WA 98037"
_CITY_STATE_ZIP = re.compile(
    r"^[A-Za-z ]+,?\s+[A-Z]{2}\s+\d{5}(?:-\d{4})?$"
)


@dataclass
class _LineProfile:
    """Profile for a single line position across pages."""
    index: int
    is_fixed: bool
    fixed_text: str | None  # the common text if fixed, else None
    sample_values: list[str]  # representative values if varying


def _classify_pii_type(values: list[str]) -> str | None:
    """Classify a set of varying line values as a PII type.

    Returns 'PERSON', 'LOCATION', or None.
    """
    person_score = 0
    address_score = 0

    for val in values[:10]:  # sample up to 10
        for pat in _PERSON_PATTERNS:
            if pat.search(val):
                person_score += 1
                break

        for pat in _ADDRESS_PATTERNS:
            if pat.search(val):
                address_score += 1
                break

        if _CITY_STATE_ZIP.match(val):
            address_score += 1

    n = min(len(values), 10)
    if n == 0:
        return None

    if person_score / n >= 0.5:
        return "PERSON"
    if address_score / n >= 0.4:
        return "LOCATION"

    return None


# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------

def build_heuristic_field_map(
    blocks: list[ExtractedBlock],
    onset_page: int | str,
    total_pages: int,
    *,
    min_pages: int = _MIN_PAGES,
    max_sample_pages: int = 9,
) -> list[FieldMapping] | None:
    """Build a field map by comparing pages line-by-line.

    Parameters
    ----------
    blocks:
        All extracted blocks for the document.
    onset_page:
        Page number where PII content begins.
    total_pages:
        Total pages in the document.
    min_pages:
        Minimum number of non-empty pages required.
    max_sample_pages:
        Maximum pages to sample for comparison.

    Returns
    -------
    list[FieldMapping] | None
        A list of field mappings if a repeating structure is detected,
        or None if the document doesn't have a clear repeating layout.
    """
    # Group blocks by page
    page_texts: dict[int | str, str] = {}
    page_order: list[int | str] = []
    seen: set[int | str] = set()

    for b in blocks:
        if b.page_or_sheet not in seen:
            seen.add(b.page_or_sheet)
            page_order.append(b.page_or_sheet)
        if b.page_or_sheet not in page_texts:
            page_texts[b.page_or_sheet] = b.text
        else:
            page_texts[b.page_or_sheet] += "\n" + b.text

    # Find onset index and sample pages from onset onward
    try:
        start_idx = page_order.index(onset_page)
    except ValueError:
        start_idx = 0

    # Sample pages: take every other page if there are many
    candidate_pages = page_order[start_idx:]
    # Filter to non-empty pages with reasonable content
    sample_pages = []
    for pg in candidate_pages:
        text = page_texts.get(pg, "").strip()
        if text and len(text) > 20:
            sample_pages.append(pg)
            if len(sample_pages) >= max_sample_pages:
                break

    if len(sample_pages) < min_pages:
        logger.debug(
            "Heuristic field map: only %d non-empty pages (need %d), skipping",
            len(sample_pages), min_pages,
        )
        return None

    # Split each page into lines
    pages_lines: list[list[str]] = []
    for pg in sample_pages:
        raw = page_texts[pg]
        lines = [ln.strip() for ln in raw.split("\n") if ln.strip()]
        pages_lines.append(lines[:_MAX_LINES])

    # Build line profiles by comparing across pages
    max_lines = min(max(len(pl) for pl in pages_lines), _MAX_LINES)
    profiles: list[_LineProfile] = []

    for i in range(max_lines):
        values: list[str] = []
        for pl in pages_lines:
            if i < len(pl):
                values.append(pl[i])

        if not values:
            continue

        # Count most common value
        counter = Counter(values)
        most_common_text, most_common_count = counter.most_common(1)[0]
        fraction = most_common_count / len(values)

        is_fixed = fraction >= _FIXED_THRESHOLD
        profiles.append(_LineProfile(
            index=i,
            is_fixed=is_fixed,
            fixed_text=most_common_text if is_fixed else None,
            sample_values=values if not is_fixed else [],
        ))

    # Find the best anchor: we need a block of ≥2 consecutive fixed lines
    # followed by a block of varying lines (the PII zone).
    # Skip any leading varying lines (e.g., student IDs before the header).
    best_anchor: _LineProfile | None = None
    best_pii_start: int | None = None

    i = 0
    while i < len(profiles):
        # Skip varying lines
        if not profiles[i].is_fixed:
            i += 1
            continue

        # Found a fixed line — count consecutive fixed lines
        fixed_start = i
        while i < len(profiles) and profiles[i].is_fixed:
            i += 1
        fixed_end = i  # exclusive

        # Need at least 2 consecutive fixed lines for a reliable header
        if fixed_end - fixed_start < 2:
            continue

        # Check if varying lines follow this fixed block
        if fixed_end < len(profiles) and not profiles[fixed_end].is_fixed:
            # Use the LAST fixed line in this block as anchor
            best_anchor = profiles[fixed_end - 1]
            best_pii_start = fixed_end
            break  # Use the first qualifying block

    if best_anchor is None or best_pii_start is None:
        logger.debug("Heuristic field map: no fixed header block followed by varying lines")
        return None

    anchor_text = best_anchor.fixed_text
    anchor_idx = best_anchor.index

    logger.info(
        "Heuristic field map: anchor=%r at line %d, analyzing varying lines",
        anchor_text, anchor_idx,
    )

    # Classify each varying line after the anchor
    field_maps: list[FieldMapping] = []
    lines_from_anchor = 0

    for p in profiles[best_pii_start:]:
        if p.is_fixed:
            # Hit another fixed block — stop the PII zone
            break

        lines_from_anchor = p.index - anchor_idx
        pii_type = _classify_pii_type(p.sample_values)

        if pii_type is None:
            continue

        # Determine spatial relationship
        if lines_from_anchor == 1:
            spatial = "line_below"
        else:
            spatial = f"lines_below_{lines_from_anchor}"

        fm = FieldMapping(
            field_type=pii_type,
            anchor_text=anchor_text,
            spatial_relationship=spatial,
            line_count=1,
        )

        # For LOCATION, check if next varying line is also LOCATION (multi-line address)
        # We'll handle this by setting line_count appropriately
        field_maps.append(fm)

        logger.info(
            "  → %s at %s (line +%d from anchor)",
            pii_type, spatial, lines_from_anchor,
        )

    # Post-process: merge consecutive LOCATION fields into multi-line
    merged: list[FieldMapping] = []
    i = 0
    while i < len(field_maps):
        fm = field_maps[i]
        if fm.field_type == "LOCATION" and i + 1 < len(field_maps):
            next_fm = field_maps[i + 1]
            if next_fm.field_type == "LOCATION":
                # Merge: keep first LOCATION's position, extend line_count
                fm.line_count = 2
                merged.append(fm)
                i += 2
                continue
        merged.append(fm)
        i += 1

    if len(merged) < 2:
        logger.debug(
            "Heuristic field map: only %d field(s) — need at least 2 for reliable map",
            len(merged),
        )
        return None

    logger.info(
        "Heuristic field map: built %d fields with anchor %r",
        len(merged), anchor_text,
    )
    return merged
