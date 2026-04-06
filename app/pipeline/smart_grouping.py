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

4. **Multi-page record blocks** (mainframe reports where one person spans
   2-4 consecutive pages): detect PERSON boundaries and merge cross-page
   PII into one composite per person.

5. **Unknown / no schema**: Fall back to per-person proximity grouping.
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
    doc_path: str | None = None,
) -> list[PIIRecord]:
    """Group Presidio detections into composite PIIRecords.

    If *schema* is a DocumentSchema, uses its structure metadata to choose
    the best grouping strategy. If *doc_path* is provided and the schema
    indicates a multi-page template, uses marker-based instance detection
    for variable-length records (e.g. person 1 = 3 pages, person 2 = 2 pages).

    Otherwise falls back to proximity grouping.
    """
    if not detections:
        return []

    # Extract schema hints (if available)
    records_per_page = 1
    is_tabular = False
    pages_per_instance = 1
    instance_marker: str | None = None

    if schema is not None:
        records_per_page = getattr(schema, "records_per_page_estimate", 1) or 1
        is_tabular = getattr(schema, "is_tabular", False)
        template = getattr(schema, "template", None)
        if template:
            pages_per_instance = getattr(template, "pages_per_instance", 1) or 1
            instance_marker = getattr(template, "instance_marker", None)

    # Strategy 1: Multi-page template — try marker-based boundaries first
    if pages_per_instance > 1:
        boundaries = None
        if doc_path and instance_marker:
            try:
                from app.pipeline.instance_detector import find_instance_boundaries
                boundaries = find_instance_boundaries(doc_path, instance_marker=instance_marker)
                if boundaries:
                    logger.info(
                        "Marker-based instance boundaries for %s: %d instances (marker=%r)",
                        doc_id, len(boundaries), instance_marker,
                    )
            except Exception:
                logger.debug("Marker-based boundary detection failed for %s", doc_id, exc_info=True)

        if boundaries:
            return _group_by_boundaries(detections, doc_id, boundaries)
        return _group_multi_page_template(detections, doc_id, pages_per_instance)

    # Strategy 2: Single person per page — one composite per page
    if records_per_page <= 1 and not is_tabular:
        records = _group_one_per_page(detections, doc_id)
        # Density guard: if most records have no PERSON name, this is likely
        # a multi-page-per-person report (e.g. mainframe caller journal) where
        # one person's PII spans consecutive pages.  Re-group using cross-page
        # person-boundary detection to merge orphan fragments.
        if len(records) >= 10:
            named = sum(1 for r in records if r.raw_name)
            orphan_ratio = 1.0 - (named / len(records)) if records else 0.0
            if orphan_ratio > 0.50:
                logger.info(
                    "Density guard triggered for %s: %d/%d records lack names (%.0f%% orphans). "
                    "Re-grouping with cross-page person boundaries.",
                    doc_id, len(records) - named, len(records), orphan_ratio * 100,
                )
                regrouped = _group_cross_page_person_blocks(detections, doc_id)
                if regrouped:
                    regrouped_named = sum(1 for r in regrouped if r.raw_name)
                    if regrouped_named > named:
                        logger.info(
                            "Cross-page regrouping improved %s: %d→%d named records (%d→%d total)",
                            doc_id, named, regrouped_named, len(records), len(regrouped),
                        )
                        return regrouped
                    else:
                        logger.info(
                            "Cross-page regrouping did not improve %s, keeping per-page grouping.",
                            doc_id,
                        )
        return records

    # Strategy 3: Table / multiple persons — per-row or per-person proximity
    return _group_per_person(detections, doc_id)


def _group_by_boundaries(
    detections: list,
    doc_id: str,
    boundaries: list[list[int]],
) -> list[PIIRecord]:
    """Group detections by marker-detected instance boundaries.

    Handles variable-length instances (person 1 = pages 0-2, person 2 = pages 3-6).
    Each boundary is a list of page numbers belonging to one instance.
    """
    # Map page → boundary index
    page_to_instance: dict[int, int] = {}
    for idx, pages in enumerate(boundaries):
        for pg in pages:
            page_to_instance[pg] = idx

    # Group detections by instance
    instance_dets: dict[int, list] = defaultdict(list)
    unassigned: list = []
    for d in detections:
        pg = d.block.page_or_sheet if hasattr(d, "block") and d.block else -1
        inst = page_to_instance.get(pg)
        if inst is not None:
            instance_dets[inst].append(d)
        else:
            unassigned.append(d)

    records: list[PIIRecord] = []
    for dets in instance_dets.values():
        if not dets:
            continue
        has_person = any(d.entity_type in _PERSON_TYPES for d in dets)
        if has_person:
            records.append(build_composite_record(dets, doc_id))
        else:
            records.extend(detection_to_pii_record(d, doc_id) for d in dets)

    # Handle detections on pages not in any boundary (e.g. cover page)
    if unassigned:
        for d in unassigned:
            records.append(detection_to_pii_record(d, doc_id))

    return records


def _group_one_per_page(detections: list, doc_id: str) -> list[PIIRecord]:
    """One composite record per page. Best for 1-person-per-page docs.

    Even if no PERSON detection is present on a page, we still build a single
    composite record when there are 2+ detections — this keeps SSN, email, and
    phone on the same page linked into one subject instead of creating orphan
    records.  The subject may lack a name but will carry the other PII fields.
    """
    page_dets: dict = defaultdict(list)
    for d in detections:
        pg = d.block.page_or_sheet if hasattr(d, "block") and d.block else 0
        page_dets[pg].append(d)

    records: list[PIIRecord] = []
    for group in page_dets.values():
        if len(group) >= 2:
            # Always build a composite when multiple detections on the same page,
            # even without a PERSON detection — keeps related PII together.
            records.append(build_composite_record(group, doc_id))
        elif len(group) == 1:
            records.append(detection_to_pii_record(group[0], doc_id))
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


def _group_cross_page_person_blocks(detections: list, doc_id: str) -> list[PIIRecord]:
    """Merge detections across consecutive pages into one composite per person.

    For mainframe-style reports (e.g. Middlefield Banking Caller Reference
    Journal) where one person's block spans 2-4 consecutive pages:
      page N   → NAME + SSN + address
      page N+1 → phone + email + account details
      page N+2 → more account details (same person)

    Algorithm: walk pages in order.  When a PERSON detection appears, start a
    new person block.  All subsequent detections on the same or consecutive
    pages (until the next PERSON) belong to the same person.
    """
    # Gather detections by page
    page_dets: dict[int, list] = defaultdict(list)
    for d in detections:
        pg = d.block.page_or_sheet if hasattr(d, "block") and d.block else 0
        page_dets[pg].append(d)

    sorted_pages = sorted(page_dets.keys())
    if not sorted_pages:
        return []

    # Walk pages in order; PERSON detections mark the start of a new block
    blocks: list[list] = []  # each block = list of detections for one person
    current_block: list = []

    for pg in sorted_pages:
        dets_on_page = page_dets[pg]
        has_person = any(d.entity_type in _PERSON_TYPES for d in dets_on_page)

        if has_person:
            # New person boundary — flush previous block
            if current_block:
                blocks.append(current_block)
            current_block = list(dets_on_page)
        else:
            # No PERSON on this page — belongs to current person's block
            if current_block:
                current_block.extend(dets_on_page)
            else:
                # Orphan page before first person — start a block anyway
                current_block = list(dets_on_page)

    # Flush last block
    if current_block:
        blocks.append(current_block)

    # Build composites from each person block
    records: list[PIIRecord] = []
    for block_dets in blocks:
        has_person = any(d.entity_type in _PERSON_TYPES for d in block_dets)
        if has_person:
            # May have multiple PERSON detections (e.g. repeated header name
            # + actual name).  Use per-person grouping within the block.
            persons = [d for d in block_dets if d.entity_type in _PERSON_TYPES]
            non_persons = [d for d in block_dets if d.entity_type not in _PERSON_TYPES]
            if len(persons) == 1:
                records.append(build_composite_record(block_dets, doc_id))
            else:
                # Multiple persons in one block — nearest-neighbour
                person_groups: dict[int, list] = {i: [p] for i, p in enumerate(persons)}
                for np_det in non_persons:
                    np_pos = np_det.start if hasattr(np_det, "start") else 0
                    best_idx = min(
                        range(len(persons)),
                        key=lambda j: abs(
                            (persons[j].start if hasattr(persons[j], "start") else 0) - np_pos
                        ),
                    )
                    person_groups[best_idx].append(np_det)
                for dets in person_groups.values():
                    records.append(build_composite_record(dets, doc_id))
        else:
            # No person at all — emit individual orphans
            records.extend(detection_to_pii_record(d, doc_id) for d in block_dets)

    logger.info(
        "Cross-page person-block grouping for %s: %d person blocks from %d pages, %d records",
        doc_id, len(blocks), len(sorted_pages), len(records),
    )
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
            # Single person or none — standard composite.
            # Even without a PERSON detection, build a composite when there are
            # 2+ detections on the same row/page so related PII stays linked.
            if persons or len(group) >= 2:
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
