"""Smart grouping for Presidio detections — per-person composite records.

When multiple PERSON detections appear on the same page (common in tables,
lists, and multi-record PDFs), this module creates one composite PIIRecord
per individual instead of one per page.

Non-PERSON detections (SSN, phone, email, address) are assigned to the
nearest PERSON by text position proximity.
"""
from __future__ import annotations

from collections import defaultdict

from app.pipeline.record_mapper import build_composite_record, detection_to_pii_record
from app.rra.entity_resolver import PIIRecord


def group_detections_to_records(
    detections: list,
    doc_id: str,
) -> list[PIIRecord]:
    """Group Presidio detections into composite PIIRecords.

    Uses (page, row) for table-format data and per-person proximity
    grouping for prose/PDF data with multiple people per page.

    Returns one PIIRecord per individual person found.
    """
    if not detections:
        return []

    # Group by (page, row) for table data, page-only for prose
    groups: dict = defaultdict(list)
    for det in detections:
        pg = det.block.page_or_sheet if hasattr(det, "block") and det.block else 0
        row = getattr(det.block, "row", None) if hasattr(det, "block") and det.block else None
        key = (pg, row) if row is not None else pg
        groups[key].append(det)

    records: list[PIIRecord] = []
    for group_dets in groups.values():
        persons = [d for d in group_dets if d.entity_type in ("PERSON", "PERSON_NAME")]
        non_persons = [d for d in group_dets if d.entity_type not in ("PERSON", "PERSON_NAME")]

        if len(persons) <= 1:
            # Single person or none — standard composite
            if persons:
                records.append(build_composite_record(group_dets, doc_id))
            else:
                records.extend(detection_to_pii_record(d, doc_id) for d in group_dets)
        else:
            # Multiple persons — one composite per person
            # Assign each non-person detection to the nearest person by text position
            person_groups: dict[int, list] = {i: [p] for i, p in enumerate(persons)}

            for np_det in non_persons:
                np_pos = np_det.start if hasattr(np_det, "start") else 0
                best_idx = 0
                best_dist = float("inf")
                for i, p in enumerate(persons):
                    p_pos = p.start if hasattr(p, "start") else 0
                    dist = abs(np_pos - p_pos)
                    if dist < best_dist:
                        best_dist = dist
                        best_idx = i
                person_groups[best_idx].append(np_det)

            for dets in person_groups.values():
                records.append(build_composite_record(dets, doc_id))

    return records
