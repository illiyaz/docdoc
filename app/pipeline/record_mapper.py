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

import logging
import re
from collections import defaultdict
from uuid import uuid4

from app.pii.presidio_engine import DetectionResult
from app.rra.entity_resolver import PIIRecord, _GOV_ID_TYPES
from app.structure.document_schema import DocumentSchema

logger = logging.getLogger(__name__)

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

# UK NI number pattern: 2 letters, 6 digits, 1 letter (e.g. NH828286D)
_NI_NUMBER_RE = re.compile(r"\b([A-Z]{2}\d{6}[A-Z])\b")


# ---------------------------------------------------------------------------
# Name validation (imported from coordinate_extractor for Presidio path)
# ---------------------------------------------------------------------------

def _is_likely_name(name: str) -> bool:
    """Check if a string looks like a real person name (not header/boilerplate).

    Rejects:
    - Too short (<3 chars) or too long (>80 chars)
    - Contains digits
    - Single-word "names"
    - All significant words are in blocklist
    - First word (likely surname) is a blocklisted word
    """
    from app.pipeline.coordinate_extractor import _NAME_BLOCKLIST
    if not name:
        return False
    t = name.strip()
    if len(t) < 3 or len(t) > 80:
        return False
    if any(c.isdigit() for c in t):
        return False
    words = t.split()
    if len(words) < 2:
        return False
    if not any(len(w) >= 3 for w in words):
        return False
    upper_words = [w.upper().rstrip(",.;:") for w in words]
    if all(w in _NAME_BLOCKLIST for w in upper_words if len(w) >= 2):
        return False
    if upper_words and upper_words[0] in _NAME_BLOCKLIST:
        return False
    return True


# ---------------------------------------------------------------------------
# Address validation
# ---------------------------------------------------------------------------

# Bare state abbreviations, account numbers, city lists are not addresses
_US_STATE_ABBREVS = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
})

_ADDRESS_INDICATOR_RE = re.compile(
    r"\b(\d+\s+\w+|P\.?\s*O\.?\s*BOX|SUITE\s+\d|APT\s+\d|UNIT\s+\d)",
    re.IGNORECASE,
)


def _is_likely_address(text: str) -> bool:
    """Check if a text looks like a real street address.

    Requires either a street number, PO Box, suite/apt/unit number, or
    a zip code pattern. Rejects bare state abbreviations, single words,
    and text that looks like account numbers or form labels.
    """
    if not text:
        return False
    t = text.strip()
    if len(t) < 5:
        return False
    # Single word → not an address
    if len(t.split()) < 2:
        return False
    # Bare state abbreviation
    if t.upper().strip() in _US_STATE_ABBREVS:
        return False
    # Has a street number, PO Box, or suite/apt/unit?
    if _ADDRESS_INDICATOR_RE.search(t):
        return True
    # Has a zip code?
    if re.search(r"\b\d{5}(?:-\d{4})?\b", t):
        return True
    # Has a common street suffix?
    if re.search(
        r"\b(?:ST|AVE|RD|DR|LN|BLVD|WAY|PL|CT|STREET|AVENUE|ROAD|DRIVE|LANE)\b",
        t.upper(),
    ):
        return True
    return False


# ---------------------------------------------------------------------------
# Synthetic data detection
# ---------------------------------------------------------------------------

_SYNTHETIC_SSNS = frozenset({
    "123-45-6789", "12345-6789", "123456789",
    "000-00-0000", "999-99-9999",
    "111-11-1111", "222-22-2222", "333-33-3333",
    "444-44-4444", "555-55-5555", "666-66-6666",
    "777-77-7777", "888-88-8888",
})

_SYNTHETIC_NAME_RE = re.compile(
    r"\b(JOHN\s+DOE|JANE\s+DOE|JANE\s+SMITH|JOHN\s+SMITH|"
    r"TEST\s+USER|SAMPLE\s+NAME|DUMMY\s+NAME|"
    r"XXX\s+XXX|FIRST\s+LAST)\b",
    re.IGNORECASE,
)


def _is_synthetic_value(value: str, entity_type: str) -> bool:
    """Detect placeholder/synthetic PII values.

    Flags:
    - SSNs: 123-45-6789, 000-*, 999-*, all-same-digit, starts with 9xx-xx
    - Phones: 555-xxxx exchanges
    - Names: John Doe, Jane Smith, Test User, etc.
    """
    if not value:
        return False
    t = value.strip()

    if entity_type in ("US_SSN", "TAX_ID", "GOVERNMENT_ID"):
        digits_only = re.sub(r"[^0-9]", "", t)
        # Known synthetic SSNs
        if t in _SYNTHETIC_SSNS or digits_only in {"123456789", "000000000", "999999999"}:
            return True
        # All same digit
        if len(digits_only) == 9 and len(set(digits_only)) == 1:
            return True
        # SSNs starting with 000, 666, or 9xx are invalid per SSA rules
        if len(digits_only) == 9 and digits_only[:3] in ("000", "666"):
            return True
        if len(digits_only) == 9 and digits_only[0] == "9":
            return True

    if entity_type in ("PHONE_NUMBER", "PHONE_US", "PHONE_INTL"):
        # 555 exchange (reserved for fictional use)
        if re.search(r"\b555[-.]?\d{4}\b", t):
            return True

    if entity_type in ("PERSON", "PERSON_NAME"):
        if _SYNTHETIC_NAME_RE.search(t):
            return True

    return False


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

    # Synthetic data guard — strip placeholder values so they don't
    # contribute meaningful PII (minimum-PII threshold will filter later)
    if _is_synthetic_value(detected_text, et):
        logger.debug("Stripping synthetic %s value in doc %s", et, doc_id)
        detected_text = ""  # empty text → no raw_* field populated

    if et in _PERSON_TYPES:
        # Name validation: reject headers, form labels, company names
        if not _is_likely_name(detected_text):
            logger.debug("Rejected unlikely name %r in doc %s", detected_text[:40], doc_id)
            raw_name = None  # keep as orphan record without name
        else:
            raw_name = detected_text
        # Split embedded UK NI number: "Blunt NH828286D" → name + gov ID
        ni_match = _NI_NUMBER_RE.search(detected_text)
        if ni_match:
            raw_government_id = ni_match.group(1)
            cleaned_name = detected_text[:ni_match.start()].strip()
            if cleaned_name and _is_likely_name(cleaned_name):
                raw_name = cleaned_name
    elif et in _EMAIL_TYPES:
        raw_email = detected_text
    elif et in _PHONE_TYPES:
        raw_phone = detected_text
    elif et in _DOB_TYPES:
        raw_dob = detected_text
    elif et in _ADDRESS_TYPES:
        # Address validation: reject bare state abbreviations, labels
        if _is_likely_address(detected_text):
            raw_address = {"raw": detected_text}
        else:
            logger.debug("Rejected unlikely address %r in doc %s", detected_text[:40], doc_id)
            raw_address = None
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
        candidate_name = _extract_text(best)
        if _is_likely_name(candidate_name):
            raw_name = candidate_name
            entity_type = best.entity_type
        else:
            # Try other name candidates
            for n in sorted(names, key=lambda d: d.score, reverse=True):
                cand = _extract_text(n)
                if _is_likely_name(cand):
                    raw_name = cand
                    entity_type = n.entity_type
                    break
            if raw_name is None:
                entity_type = best.entity_type  # keep entity_type even if name rejected
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
        joined = ", ".join(unique_parts) if unique_parts else _extract_text(addresses[0])
        if _is_likely_address(joined):
            raw_address = {"raw": joined}
        else:
            logger.debug("Rejected composite address %r in doc %s", joined[:40], doc_id)
    if gov_ids:
        best_gov = max(gov_ids, key=lambda d: d.score)
        gov_text = _extract_text(best_gov)
        if _is_synthetic_value(gov_text, best_gov.entity_type):
            logger.debug("Skipping synthetic gov ID in composite for doc %s", doc_id)
        else:
            raw_government_id = gov_text

    # Use highest-confidence PERSON detection as normalized_value, or first detection
    normalized = raw_name or _extract_text(detections[0])

    # Determine page range (1-indexed for human readability)
    pages = sorted(set(
        d.block.page_or_sheet for d in detections
        if hasattr(d, "block") and d.block
    ))
    page = pages[0] if pages else 0

    # Build page range string (handles both int pages and string sheet names)
    if pages:
        int_pages = []
        str_pages = []
        for p in pages:
            try:
                int_pages.append(int(p))
            except (ValueError, TypeError):
                str_pages.append(str(p))
        if int_pages:
            pages_1 = sorted(set(p + 1 for p in int_pages))
            page_range_str = str(pages_1[0]) if len(pages_1) == 1 else f"{pages_1[0]}-{pages_1[-1]}"
        elif str_pages:
            page_range_str = str_pages[0]
        else:
            page_range_str = "1"
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
