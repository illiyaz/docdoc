"""Map DetectionResult objects to PIIRecord objects with raw_* fields populated.

The EntityResolver requires raw_name, raw_email, raw_phone, raw_dob, and
raw_address fields to compute merge confidence between records.  Without
these, all pairwise confidence scores are 0.0 and no records are ever merged,
producing empty NotificationSubjects.

This module bridges the gap between the Presidio detection layer (which
produces DetectionResult) and the RRA layer (which consumes PIIRecord).
"""
from __future__ import annotations

from uuid import uuid4

from app.pii.presidio_engine import DetectionResult
from app.rra.entity_resolver import PIIRecord

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

    return PIIRecord(
        record_id=str(uuid4()),
        entity_type=et,
        normalized_value=detected_text,
        raw_name=raw_name,
        raw_email=raw_email,
        raw_phone=raw_phone,
        raw_dob=raw_dob,
        raw_address=raw_address,
        source_document_id=doc_id,
        page_or_sheet=page,
    )
