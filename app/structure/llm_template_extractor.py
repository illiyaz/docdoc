"""LLM-driven PII extraction for template documents (Step 19).

For repeating template documents (e.g., pension statements with N individuals
at 3 pages each), the LLM reads each template instance's pages and extracts
structured PII directly, bypassing Presidio's per-detection approach.

Three extraction paths:
  A. template + LLM available → ``LLMTemplateExtractor`` (this module)
  B. template + no LLM → ``extract_with_template()`` (Presidio composite)
  C. non-template → per-detection ``detection_to_pii_record()``

The extraction prompt is GENERATED from the ``DocumentSchema`` — different
document types produce different prompts.  Zero hardcoding.

Batching: multiple instances per LLM call (default 3) to reduce call count.
A 450-page doc with 3-page template = 150 instances → ~50 LLM calls.
"""
from __future__ import annotations

import json
import logging
from dataclasses import replace
from uuid import uuid4

from app.llm.client import OllamaClient
from app.llm.extraction_prompts import (
    build_batch_extraction_prompt,
    build_extraction_prompt,
)
from app.rra.entity_resolver import PIIRecord
from app.structure.document_schema import DocumentSchema

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Entity type → PIIRecord field mapping
# ---------------------------------------------------------------------------

_FIELD_TO_RAW: dict[str, str] = {
    "PERSON": "raw_name",
    "LOCATION": "raw_address",
    "DATE_OF_BIRTH": "raw_dob",
    "DATE_OF_BIRTH_DMY": "raw_dob",
    "DATE_OF_BIRTH_MDY": "raw_dob",
    "DATE_OF_BIRTH_ISO": "raw_dob",
    "EMAIL_ADDRESS": "raw_email",
    "EMAIL": "raw_email",
    "PHONE_NUMBER": "raw_phone",
    "PHONE_US": "raw_phone",
    "PHONE_INTL": "raw_phone",
    "US_SSN": "raw_government_id",
    "NI_NUMBER": "raw_government_id",
    "AADHAAR": "raw_government_id",
    "US_DRIVER_LICENSE": "raw_government_id",
    "US_PASSPORT": "raw_government_id",
    "PAN_CARD": "raw_government_id",
    "NHS_NUMBER": "raw_government_id",
    "GOVERNMENT_ID": "raw_government_id",
    "IDENTIFICATION_NUMBER": "raw_government_id",
    "NATIONAL_INSURANCE_UK": "raw_government_id",
}

_GOV_ID_TYPES: frozenset[str] = frozenset({
    "US_SSN", "NI_NUMBER", "AADHAAR", "US_DRIVER_LICENSE",
    "US_PASSPORT", "PAN_CARD", "NHS_NUMBER", "GOVERNMENT_ID",
    "IDENTIFICATION_NUMBER", "NATIONAL_INSURANCE_UK",
})

# Maximum chars per page sent to LLM
_MAX_PAGE_CHARS = 3000


# ---------------------------------------------------------------------------
# LLMTemplateExtractor
# ---------------------------------------------------------------------------


class LLMTemplateExtractor:
    """Extract PII from template document instances using LLM.

    For each template instance (e.g., pages 1-3 for person 1):
    1. Read the page texts
    2. Build an extraction prompt from the DocumentSchema
    3. Send to LLM (Ollama)
    4. Parse JSON response into a PIIRecord
    5. Optionally validate with Presidio

    Falls back gracefully: if LLM fails for an instance, returns None
    and the caller falls back to Presidio composite for that instance.
    """

    def __init__(
        self,
        client: OllamaClient,
        *,
        batch_size: int = 3,
    ) -> None:
        self.client = client
        self.batch_size = max(1, batch_size)

    def extract_all_instances(
        self,
        schema: DocumentSchema,
        page_texts: dict[int, str],
        doc_id: str,
        total_pages: int,
    ) -> list[PIIRecord]:
        """Extract PII from all template instances in the document.

        Returns one PIIRecord per unique individual with all fields populated.
        Empty list if template is missing or all extractions fail.
        Deduplicates across all batches (same name = same person).
        """
        if not schema.template or schema.template.pages_per_instance < 2:
            return []

        template = schema.template

        # Prefer marker-based boundaries (handles variable-length instances)
        if template.instance_marker:
            instances = template.find_instance_boundaries(page_texts)
            logger.info(
                "Marker-based boundaries for %s: marker=%r, %d instances found",
                doc_id, template.instance_marker, len(instances),
            )
        else:
            instances = template.get_instance_pages(total_pages)

        if self.batch_size > 1 and len(instances) > 1:
            records = self._extract_batched(
                schema, template, instances, page_texts, doc_id,
            )
        else:
            records = self._extract_sequential(
                schema, template, instances, page_texts, doc_id,
            )

        # Deduplicate across all batches
        return _deduplicate_records(records)

    def _extract_sequential(
        self,
        schema: DocumentSchema,
        template,
        instances: list[list[int]],
        page_texts: dict[int, str],
        doc_id: str,
    ) -> list[PIIRecord]:
        """Extract one instance at a time."""
        records: list[PIIRecord] = []

        for idx, instance_pages in enumerate(instances):
            texts = [
                page_texts.get(p, "")[:_MAX_PAGE_CHARS]
                for p in instance_pages
            ]
            if not any(t.strip() for t in texts):
                continue

            prompt = build_extraction_prompt(
                page_texts=texts,
                page_roles=template.page_roles,
                instance_index=idx,
                document_type=schema.document_type,
            )

            try:
                response = self.client.generate(
                    prompt,
                    system="You extract personal information from documents. "
                    "Respond ONLY with valid JSON.",
                    use_case="template_extraction",
                    document_id=doc_id,
                )
                record = self._parse_extraction(response, doc_id, instance_pages)
                if record is not None:
                    records.append(record)
            except Exception:
                logger.warning(
                    "LLM extraction failed for instance %d of %s",
                    idx, doc_id, exc_info=True,
                )

        return records

    def _extract_batched(
        self,
        schema: DocumentSchema,
        template,
        instances: list[list[int]],
        page_texts: dict[int, str],
        doc_id: str,
    ) -> list[PIIRecord]:
        """Extract multiple instances per LLM call."""
        records: list[PIIRecord] = []

        for batch_start in range(0, len(instances), self.batch_size):
            batch_instances = instances[batch_start:batch_start + self.batch_size]

            batch_texts: list[list[str]] = []
            for instance_pages in batch_instances:
                texts = [
                    page_texts.get(p, "")[:_MAX_PAGE_CHARS]
                    for p in instance_pages
                ]
                batch_texts.append(texts)

            # Skip empty batches
            if not any(any(t.strip() for t in texts) for texts in batch_texts):
                continue

            prompt = build_batch_extraction_prompt(
                batch_page_texts=batch_texts,
                page_roles=template.page_roles,
                start_index=batch_start,
                document_type=schema.document_type,
            )

            try:
                response = self.client.generate(
                    prompt,
                    system="You extract personal information from documents. "
                    "Respond ONLY with valid JSON.",
                    use_case="template_extraction_batch",
                    document_id=doc_id,
                )
                batch_records = self._parse_batch_extraction(
                    response, doc_id, batch_instances,
                )
                records.extend(batch_records)
            except Exception:
                logger.warning(
                    "Batch LLM extraction failed at offset %d of %s, "
                    "falling back to sequential",
                    batch_start, doc_id, exc_info=True,
                )
                # Fallback: try sequential for this batch
                for i, instance_pages in enumerate(batch_instances):
                    texts = batch_texts[i]
                    if not any(t.strip() for t in texts):
                        continue
                    try:
                        single_prompt = build_extraction_prompt(
                            page_texts=texts,
                            page_roles=template.page_roles,
                            instance_index=batch_start + i,
                            document_type=schema.document_type,
                        )
                        resp = self.client.generate(
                            single_prompt,
                            system="You extract personal information from documents. "
                            "Respond ONLY with valid JSON.",
                            use_case="template_extraction",
                            document_id=doc_id,
                        )
                        rec = self._parse_extraction(resp, doc_id, instance_pages)
                        if rec is not None:
                            records.append(rec)
                    except Exception:
                        continue

        return records

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_extraction(
        self,
        response_text: str,
        doc_id: str,
        instance_pages: list[int],
    ) -> PIIRecord | None:
        """Parse LLM JSON response into a PIIRecord.

        Returns None if JSON is invalid or no PERSON name was extracted.
        """
        data = _parse_json(response_text)
        if data is None:
            return None

        # Handle case where LLM returns an array with one element
        if isinstance(data, list):
            if len(data) == 0:
                return None
            data = data[0]

        if not isinstance(data, dict):
            return None

        return self._data_to_record(data, doc_id, instance_pages)

    def _parse_batch_extraction(
        self,
        response_text: str,
        doc_id: str,
        batch_instances: list[list[int]],
    ) -> list[PIIRecord]:
        """Parse batched LLM JSON array response into PIIRecords."""
        data = _parse_json(response_text)
        if data is None:
            return []

        # Expect a JSON array
        if isinstance(data, dict):
            # LLM returned a single object instead of array — wrap it
            data = [data]

        if not isinstance(data, list):
            return []

        records: list[PIIRecord] = []
        for i, item in enumerate(data):
            if i >= len(batch_instances):
                break
            if not isinstance(item, dict):
                continue
            rec = self._data_to_record(item, doc_id, batch_instances[i])
            if rec is not None:
                records.append(rec)

        return records

    def _data_to_record(
        self,
        data: dict,
        doc_id: str,
        instance_pages: list[int],
    ) -> PIIRecord | None:
        """Convert a parsed JSON dict to a PIIRecord.

        Returns None if no PERSON name was extracted.
        """
        raw_name: str | None = None
        raw_email: str | None = None
        raw_phone: str | None = None
        raw_dob: str | None = None
        raw_address: dict | None = None
        raw_government_id: str | None = None
        government_id_type: str | None = None
        entity_types_found: list[str] = []

        for entity_type, value in data.items():
            if value is None or value == "" or value == "null":
                continue
            value_str = str(value).strip()
            if not value_str:
                continue

            raw_field = _FIELD_TO_RAW.get(entity_type)
            if raw_field is None:
                continue

            entity_types_found.append(entity_type)

            if raw_field == "raw_name":
                raw_name = value_str
            elif raw_field == "raw_email":
                raw_email = value_str
            elif raw_field == "raw_phone":
                raw_phone = value_str
            elif raw_field == "raw_dob":
                raw_dob = value_str
            elif raw_field == "raw_address":
                raw_address = {"raw": value_str}
            elif raw_field == "raw_government_id":
                raw_government_id = value_str
                if entity_type in _GOV_ID_TYPES:
                    government_id_type = entity_type

        # Must have at least a name
        if not raw_name:
            return None

        # Build page range (1-indexed)
        pages_1 = sorted(int(p) + 1 for p in instance_pages)
        if len(pages_1) == 1:
            page_range = str(pages_1[0])
        else:
            page_range = f"{pages_1[0]}-{pages_1[-1]}"

        return PIIRecord(
            record_id=str(uuid4()),
            entity_type="PERSON",
            normalized_value=raw_name,
            raw_name=raw_name,
            raw_email=raw_email,
            raw_phone=raw_phone,
            raw_dob=raw_dob,
            raw_address=raw_address,
            raw_government_id=raw_government_id,
            source_document_id=doc_id,
            page_or_sheet=instance_pages[0] if instance_pages else 0,
            page_range=page_range,
            entity_types_found=tuple(sorted(set(entity_types_found))),
        )

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @staticmethod
    def validate_with_presidio(
        record: PIIRecord,
        engine,
    ) -> tuple[bool, str]:
        """Validate LLM-extracted values against Presidio patterns.

        Returns (is_valid, detail) tuple.  If Presidio confirms the
        pattern matches (e.g., NI_NUMBER format), returns True.
        """
        if not record.raw_government_id:
            return True, "no_gov_id"

        try:
            from app.readers.base import ExtractedBlock
            block = ExtractedBlock(
                text=record.raw_government_id,
                page_or_sheet=0,
            )
            results = engine.analyze([block])
            if results:
                return True, f"presidio_confirmed:{results[0].entity_type}"
            return False, "presidio_no_match"
        except Exception:
            return True, "validation_skipped"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _deduplicate_records(records: list[PIIRecord]) -> list[PIIRecord]:
    """Deduplicate PIIRecords by normalized name.

    Same name (case-insensitive, whitespace-normalized) = same person.
    Merges fields: keeps the most-populated record, fills in any gaps
    from duplicates.  Combines page_range and entity_types_found.
    """
    if not records:
        return records

    seen: dict[str, PIIRecord] = {}  # normalized_name → best record

    for rec in records:
        if not rec.raw_name:
            continue
        key = " ".join(rec.raw_name.lower().split())
        if key not in seen:
            seen[key] = rec
            continue

        existing = seen[key]

        # Count populated optional fields
        def _field_count(r: PIIRecord) -> int:
            return sum(1 for f in (
                r.raw_address, r.raw_dob, r.raw_government_id,
                r.raw_email, r.raw_phone,
            ) if f)

        # Merge: start from record with more fields, fill gaps from other
        if _field_count(rec) > _field_count(existing):
            base, donor = rec, existing
        else:
            base, donor = existing, rec

        merged_kwargs: dict = {}
        if not base.raw_address and donor.raw_address:
            merged_kwargs["raw_address"] = donor.raw_address
        if not base.raw_dob and donor.raw_dob:
            merged_kwargs["raw_dob"] = donor.raw_dob
        if not base.raw_government_id and donor.raw_government_id:
            merged_kwargs["raw_government_id"] = donor.raw_government_id
        if not base.raw_email and donor.raw_email:
            merged_kwargs["raw_email"] = donor.raw_email
        if not base.raw_phone and donor.raw_phone:
            merged_kwargs["raw_phone"] = donor.raw_phone

        # Combine page ranges
        existing_ranges = set(base.page_range.split(", ")) if base.page_range else set()
        donor_ranges = set(donor.page_range.split(", ")) if donor.page_range else set()
        all_ranges = existing_ranges | donor_ranges
        if all_ranges:
            merged_kwargs["page_range"] = ", ".join(sorted(all_ranges))

        # Combine entity types
        all_types = set(base.entity_types_found) | set(donor.entity_types_found)
        if all_types:
            merged_kwargs["entity_types_found"] = tuple(sorted(all_types))

        if merged_kwargs:
            seen[key] = replace(base, **merged_kwargs)
        else:
            seen[key] = base

    return list(seen.values())


def _parse_json(text: str) -> dict | list | None:
    """Parse JSON from LLM response, stripping markdown fences."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = [ln for ln in lines if not ln.strip().startswith("```")]
        cleaned = "\n".join(lines).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Try to find JSON within the response
        for start_char in ("{", "["):
            idx = cleaned.find(start_char)
            if idx >= 0:
                try:
                    return json.loads(cleaned[idx:])
                except json.JSONDecodeError:
                    continue
        return None
