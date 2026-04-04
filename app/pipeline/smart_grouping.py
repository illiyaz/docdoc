"""Schema-aware grouping for Presidio detections → composite PIIRecords.

Uses the DocumentSchema from LLM document understanding to choose the
right grouping strategy:

1. **Single person per page** (records_per_page_estimate=1):
   One composite per page. Simple, accurate.

2. **Table / multiple persons per page** (is_tabular or records_per_page > 1):
   Use row attributes for Excel/CSV. For PDFs, create one composite per
   PERSON detection with nearest-neighbour assignment of non-PERSON fields.

3. **Multi-page template** (template with pages_per_instance > 1):
   Group detections across consecutive pages into one composite per instance.

4. **Unknown / no schema**: Fall back to per-person proximity grouping.
"""
from __future__ import annotations

import logging
from collections import defaultdict

from app.pipeline.record_mapper import build_composite_record, detection_to_pii_record
from app.rra.entity_resolver import PIIRecord

logger = logging.getLogger(__name__)

_PERSON_TYPES = frozenset({"PERSON", "PERSON_NAME"})


def group_detections_to_records(
    detections: list,
    doc_id: str,
    schema: object | None = None,
) -> list[PIIRecord]:
    """Group Presidio detections into composite PIIRecords.

    If *schema* is a DocumentSchema, uses its structure metadata to choose
    the best grouping strategy. Otherwise falls back to proximity grouping.
    """
    if not detections:
        return []

    # Extract schema hints (if available)
    records_per_page = 1
    is_tabular = False
    pages_per_instance = 1

    if schema is not None:
        records_per_page = getattr(schema, "records_per_page_estimate", 1) or 1
        is_tabular = getattr(schema, "is_tabular", False)
        template = getattr(schema, "template", None)
        if template:
            pages_per_instance = getattr(template, "pages_per_instance", 1) or 1

    # Strategy 1: Multi-page template — group across consecutive pages
    if pages_per_instance > 1:
        return _group_multi_page_template(detections, doc_id, pages_per_instance)

    # Strategy 2: Single person per page — one composite per page
    if records_per_page <= 1 and not is_tabular:
        return _group_one_per_page(detections, doc_id)

    # Strategy 3: Table / multiple persons — per-row or per-person proximity
    return _group_per_person(detections, doc_id)


def _group_one_per_page(detections: list, doc_id: str) -> list[PIIRecord]:
    """One composite record per page. Best for 1-person-per-page docs."""
    page_dets: dict = defaultdict(list)
    for d in detections:
        pg = d.block.page_or_sheet if hasattr(d, "block") and d.block else 0
        page_dets[pg].append(d)

    records: list[PIIRecord] = []
    for group in page_dets.values():
        has_person = any(d.entity_type in _PERSON_TYPES for d in group)
        if has_person:
            records.append(build_composite_record(group, doc_id))
        else:
            records.extend(detection_to_pii_record(d, doc_id) for d in group)
    return records


def _group_multi_page_template(
    detections: list,
    doc_id: str,
    pages_per_instance: int,
) -> list[PIIRecord]:
    """Group detections across consecutive pages into one composite per person.

    For documents like pension statements where one person spans 3 pages.
    """
    page_dets: dict[int | str, list] = defaultdict(list)
    for d in detections:
        pg = d.block.page_or_sheet if hasattr(d, "block") and d.block else 0
        page_dets[pg].append(d)

    # Sort pages and group into instances
    int_pages = sorted(p for p in page_dets if isinstance(p, int))

    records: list[PIIRecord] = []
    for i in range(0, len(int_pages), pages_per_instance):
        instance_pages = int_pages[i : i + pages_per_instance]
        instance_dets = []
        for pg in instance_pages:
            instance_dets.extend(page_dets[pg])

        if not instance_dets:
            continue

        has_person = any(d.entity_type in _PERSON_TYPES for d in instance_dets)
        if has_person:
            records.append(build_composite_record(instance_dets, doc_id))
        else:
            records.extend(detection_to_pii_record(d, doc_id) for d in instance_dets)

    # Handle non-int pages (e.g. Excel sheet names)
    for pg, dets in page_dets.items():
        if not isinstance(pg, int):
            has_person = any(d.entity_type in _PERSON_TYPES for d in dets)
            if has_person:
                records.append(build_composite_record(dets, doc_id))

    return records


def _group_per_person(detections: list, doc_id: str) -> list[PIIRecord]:
    """One composite per PERSON detection, with nearest-neighbour assignment.

    For tables and multi-person pages. Uses row attribute when available
    (Excel/CSV), falls back to text position proximity for PDFs.
    """
    # First: group by (page, row) for table data
    row_groups: dict = defaultdict(list)
    for d in detections:
        pg = d.block.page_or_sheet if hasattr(d, "block") and d.block else 0
        row = getattr(d.block, "row", None) if hasattr(d, "block") and d.block else None
        key = (pg, row) if row is not None else pg
        row_groups[key].append(d)

    records: list[PIIRecord] = []
    for group in row_groups.values():
        persons = [d for d in group if d.entity_type in _PERSON_TYPES]
        non_persons = [d for d in group if d.entity_type not in _PERSON_TYPES]

        if len(persons) <= 1:
            # Single person or none — standard composite
            if persons:
                records.append(build_composite_record(group, doc_id))
            else:
                records.extend(detection_to_pii_record(d, doc_id) for d in group)
        else:
            # Multiple persons — nearest-neighbour assignment
            person_groups: dict[int, list] = {i: [p] for i, p in enumerate(persons)}
            for np_det in non_persons:
                np_pos = np_det.start if hasattr(np_det, "start") else 0
                best_idx = min(
                    range(len(persons)),
                    key=lambda i: abs((persons[i].start if hasattr(persons[i], "start") else 0) - np_pos),
                )
                person_groups[best_idx].append(np_det)

            for dets in person_groups.values():
                records.append(build_composite_record(dets, doc_id))

    return records
