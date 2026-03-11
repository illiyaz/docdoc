"""Map DetectionResult objects to PIIRecord objects with raw_* fields populated.

The EntityResolver requires raw_name, raw_email, raw_phone, raw_dob, and
raw_address fields to compute merge confidence between records.  Without
these, all pairwise confidence scores are 0.0 and no records are ever merged,
producing empty NotificationSubjects.

This module bridges the gap between the Presidio detection layer (which
produces DetectionResult) and the RRA layer (which consumes PIIRecord).

Step 17 additions:
- ``build_composite_record()`` — merges detections from multiple pages into
  a single PIIRecord with all raw_* fields populated.
- ``extract_with_template()`` — groups detections by template instance pages
  and builds one composite PIIRecord per individual.
"""
from __future__ import annotations

from collections import defaultdict
from uuid import uuid4

from app.pii.presidio_engine import DetectionResult
from app.rra.entity_resolver import PIIRecord, _GOV_ID_TYPES
from app.structure.document_schema import DocumentSchema

# ---------------------------------------------------------------------------
# Entity type → raw_* field mapping
# ---------------------------------------------------------------------------

_PERSON_TYPES = frozenset({
    "PERSON", "PERSON_NAME",
})

_EMAIL_TYPES = frozenset({
    "EMAIL_ADDRESS", "EMAIL",
})

_PHONE_TYPES = frozenset({
    "PHONE_NUMBER", "PHONE_US", "PHONE_INTL",
})

_DOB_TYPES = frozenset({
    "DATE_OF_BIRTH", "DATE_OF_BIRTH_MDY", "DATE_OF_BIRTH_DMY",
})

_ADDRESS_TYPES = frozenset({
    "LOCATION", "ADDRESS",
})

# National insurance / government ID types for raw_government_id
_GOV_ID_FIELD_TYPES = _GOV_ID_TYPES | frozenset({
    "NATIONAL_INSURANCE_UK", "UK_NINO", "NI_NUMBER",
    "NATIONAL_ID", "TAX_ID",
})


def detection_to_pii_record(
    det: DetectionResult,
    doc_id: str,
) -> PIIRecord:
    """Map a DetectionResult to a PIIRecord with raw_* fields populated.

    The detected text is mapped to the appropriate raw_* field based on the
    entity type.  PIIRecord is frozen, so all fields must be set at init time.
    """
    detected_text = det.block.text[det.start:det.end] if hasattr(det, "block") else ""
    page = det.block.page_or_sheet if hasattr(det, "block") else 0

    raw_name: str | None = None
    raw_email: str | None = None
    raw_phone: str | None = None
    raw_dob: str | None = None
    raw_address: dict | None = None
    raw_government_id: str | None = None

    et = det.entity_type

    if et in _PERSON_TYPES:
        raw_name = detected_text
    elif et in _EMAIL_TYPES:
        raw_email = detected_text
    elif et in _PHONE_TYPES:
        raw_phone = detected_text
    elif et in _DOB_TYPES:
        raw_dob = detected_text
    elif et in _ADDRESS_TYPES:
        # raw_address is typed as dict | None in PIIRecord
        raw_address = {"raw": detected_text}
    elif et.upper() in _GOV_ID_FIELD_TYPES:
        raw_government_id = detected_text

    return PIIRecord(
        record_id=str(uuid4()),
        entity_type=et,
        normalized_value=detected_text,
        raw_name=raw_name,
        raw_email=raw_email,
        raw_phone=raw_phone,
        raw_dob=raw_dob,
        raw_address=raw_address,
        raw_government_id=raw_government_id,
        source_document_id=doc_id,
        page_or_sheet=page,
        page_range=str(int(page) + 1) if isinstance(page, (int, float)) else str(page),
        entity_types_found=(et,),
    )


# ---------------------------------------------------------------------------
# Composite record building (Step 17)
# ---------------------------------------------------------------------------

def _extract_text(det: DetectionResult) -> str:
    """Extract detected text from a DetectionResult."""
    if hasattr(det, "block") and det.block:
        return det.block.text[det.start:det.end]
    return ""


def build_composite_record(
    detections: list[DetectionResult],
    doc_id: str,
) -> PIIRecord:
    """Merge multiple detections into a single PIIRecord with all raw_* fields.

    Groups detections by semantic type, takes the highest-confidence detection
    for each field.  Returns ONE PIIRecord per template instance (individual).
    """
    if not detections:
        return PIIRecord(
            record_id=str(uuid4()),
            entity_type="UNKNOWN",
            normalized_value="",
            source_document_id=doc_id,
        )

    # Group by semantic type
    names: list[DetectionResult] = []
    emails: list[DetectionResult] = []
    phones: list[DetectionResult] = []
    dobs: list[DetectionResult] = []
    addresses: list[DetectionResult] = []
    gov_ids: list[DetectionResult] = []

    for d in detections:
        et = d.entity_type.upper() if d.entity_type else ""
        if d.entity_type in _PERSON_TYPES:
            names.append(d)
        elif d.entity_type in _EMAIL_TYPES:
            emails.append(d)
        elif d.entity_type in _PHONE_TYPES:
            phones.append(d)
        elif d.entity_type in _DOB_TYPES:
            dobs.append(d)
        elif d.entity_type in _ADDRESS_TYPES:
            addresses.append(d)
        elif et in _GOV_ID_FIELD_TYPES:
            gov_ids.append(d)

    raw_name: str | None = None
    raw_email: str | None = None
    raw_phone: str | None = None
    raw_dob: str | None = None
    raw_address: dict | None = None
    raw_government_id: str | None = None
    entity_type = "PERSON"  # default for composite records

    if names:
        best = max(names, key=lambda d: d.score)
        raw_name = _extract_text(best)
        entity_type = best.entity_type
    if emails:
        raw_email = _extract_text(max(emails, key=lambda d: d.score))
    if phones:
        raw_phone = _extract_text(max(phones, key=lambda d: d.score))
    if dobs:
        raw_dob = _extract_text(max(dobs, key=lambda d: d.score))
    if addresses:
        # Join all LOCATION detections in order of appearance (by start offset)
        # to build a complete address like "85 Waltings Gardens, London, NW2 3UD"
        sorted_addrs = sorted(addresses, key=lambda d: (
            d.block.page_or_sheet if hasattr(d, "block") and d.block else 0,
            d.start if hasattr(d, "start") else 0,
        ))
        # Deduplicate exact-same text (case-insensitive)
        seen_texts: set[str] = set()
        unique_parts: list[str] = []
        for d in sorted_addrs:
            txt = _extract_text(d).strip()
            if txt and txt.lower() not in seen_texts:
                seen_texts.add(txt.lower())
                unique_parts.append(txt)
        raw_address = {"raw": ", ".join(unique_parts)} if unique_parts else {"raw": _extract_text(addresses[0])}
    if gov_ids:
        raw_government_id = _extract_text(max(gov_ids, key=lambda d: d.score))

    # Use highest-confidence PERSON detection as normalized_value, or first detection
    normalized = raw_name or _extract_text(detections[0])

    # Determine page range (1-indexed for human readability)
    pages = sorted(set(
        d.block.page_or_sheet for d in detections
        if hasattr(d, "block") and d.block
    ))
    page = pages[0] if pages else 0

    # Build 1-indexed page range string
    if pages:
        pages_1 = sorted(set(int(p) + 1 for p in pages))
        if len(pages_1) == 1:
            page_range_str = str(pages_1[0])
        else:
            page_range_str = f"{pages_1[0]}-{pages_1[-1]}"
    else:
        page_range_str = "1"

    # Collect all entity types found across detections
    all_entity_types = tuple(sorted(set(
        d.entity_type for d in detections if d.entity_type
    )))

    return PIIRecord(
        record_id=str(uuid4()),
        entity_type=entity_type,
        normalized_value=normalized,
        raw_name=raw_name,
        raw_email=raw_email,
        raw_phone=raw_phone,
        raw_dob=raw_dob,
        raw_address=raw_address,
        raw_government_id=raw_government_id,
        source_document_id=doc_id,
        page_or_sheet=page,
        page_range=page_range_str,
        entity_types_found=all_entity_types,
    )


def extract_with_template(
    detections: list[DetectionResult],
    schema: DocumentSchema,
    doc_id: str,
    total_pages: int,
) -> list[PIIRecord]:
    """Group detections by template instance and build composite PIIRecords.

    For a 6-page doc with 3-page template: instances = [[0,1,2], [3,4,5]].
    For each instance, collects detections from those pages and calls
    ``build_composite_record()``.

    Falls back to per-detection records if schema has no template.
    """
    if not schema.template or schema.template.pages_per_instance < 2:
        return [detection_to_pii_record(d, doc_id) for d in detections]

    template = schema.template
    instances = template.get_instance_pages(total_pages)

    # Group detections by page
    by_page: dict[int | str, list[DetectionResult]] = defaultdict(list)
    for d in detections:
        page = d.block.page_or_sheet if hasattr(d, "block") and d.block else 0
        by_page[page].append(d)

    records: list[PIIRecord] = []
    for instance_pages in instances:
        instance_dets: list[DetectionResult] = []
        for page in instance_pages:
            instance_dets.extend(by_page.get(page, []))

        if instance_dets:
            rec = build_composite_record(instance_dets, doc_id)
            # Override page_range to match template instance pages (1-indexed)
            pages_1 = sorted(int(p) + 1 for p in instance_pages)
            if len(pages_1) == 1:
                tmpl_range = str(pages_1[0])
            else:
                tmpl_range = f"{pages_1[0]}-{pages_1[-1]}"
            # Replace frozen PIIRecord with updated page_range
            from dataclasses import replace
            rec = replace(rec, page_range=tmpl_range)
            records.append(rec)

    return records
