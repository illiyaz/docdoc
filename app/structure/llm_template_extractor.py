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

Reliability: failed batches are retried up to MAX_RETRIES times with
exponential backoff (2s, 4s, 8s).  If a batch still fails, it is split
into individual instances and each is retried separately.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import replace
from uuid import uuid4

from app.llm.client import OllamaClient
from app.llm.extraction_prompts import (
    build_batch_extraction_prompt,
    build_extraction_prompt,
)
from app.pii.pattern_validator import validate_dob, validate_email
from app.rra.entity_resolver import PIIRecord
from app.structure.document_schema import DocumentSchema

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Retry / reliability constants
# ---------------------------------------------------------------------------

MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = [2, 4, 8]
EXTRACTION_TIMEOUT_S = 120

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
        *,
        active_anchors: list[str] | None = None,
        progress_callback: "Callable[[int, int, int], None] | None" = None,
    ) -> list[PIIRecord]:
        """Extract PII from all template instances in the document.

        Returns one PIIRecord per unique individual with all fields populated.
        Empty list if template is missing or all extractions fail.
        Deduplicates across all batches using configurable anchors.

        Parameters
        ----------
        active_anchors:
            Dedup anchor names from protocol config (e.g. ``["ssn", "email"]``).
            Passed through to ``_deduplicate_records``.
        progress_callback:
            Called after each batch with ``(batch_index, total_batches, records_so_far)``.
            Used by the background extraction thread to update heartbeat.
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

        # Unload unused models to free VRAM before heavy extraction
        _unload_unused_models(self.client)

        if self.batch_size > 1 and len(instances) > 1:
            records, retries, failures = self._extract_batched(
                schema, template, instances, page_texts, doc_id,
                progress_callback=progress_callback,
            )
        else:
            records, retries, failures = self._extract_sequential(
                schema, template, instances, page_texts, doc_id,
                progress_callback=progress_callback,
            )

        logger.info(
            "Extracted %d/%d instances for %s, %d retries, %d permanent failures",
            len(records), len(instances), doc_id, retries, failures,
        )

        # Deduplicate across all batches
        return _deduplicate_records(records, active_anchors=active_anchors)

    def _extract_sequential(
        self,
        schema: DocumentSchema,
        template,
        instances: list[list[int]],
        page_texts: dict[int, str],
        doc_id: str,
        *,
        progress_callback: "Callable[[int, int, int], None] | None" = None,
    ) -> tuple[list[PIIRecord], int, int]:
        """Extract one instance at a time.

        Returns (records, total_retries, permanent_failures).
        """
        records: list[PIIRecord] = []
        total_retries = 0
        permanent_failures = 0

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

            record = None
            for attempt in range(MAX_RETRIES):
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
                        break
                    # Parsed OK but no person found — not a retry-able error
                    break
                except Exception:
                    if attempt < MAX_RETRIES - 1:
                        backoff = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
                        logger.warning(
                            "LLM extraction attempt %d/%d failed for instance %d of %s, "
                            "retrying in %ds",
                            attempt + 1, MAX_RETRIES, idx, doc_id, backoff,
                        )
                        total_retries += 1
                        time.sleep(backoff)
                    else:
                        logger.warning(
                            "LLM extraction permanently failed for instance %d of %s",
                            idx, doc_id, exc_info=True,
                        )
                        permanent_failures += 1

            if record is not None:
                records.append(record)

            if progress_callback is not None:
                try:
                    progress_callback(idx + 1, len(instances), len(records))
                except Exception:
                    pass

        return records, total_retries, permanent_failures

    def _extract_batched(
        self,
        schema: DocumentSchema,
        template,
        instances: list[list[int]],
        page_texts: dict[int, str],
        doc_id: str,
        *,
        progress_callback: "Callable[[int, int, int], None] | None" = None,
    ) -> tuple[list[PIIRecord], int, int]:
        """Extract multiple instances per LLM call with retry and split-to-individual.

        Returns (records, total_retries, permanent_failures).

        Retry strategy:
        1. Try the batch up to MAX_RETRIES times with exponential backoff.
        2. If all batch retries fail, split into individual instances and
           retry each separately (also with retries).
        """
        records: list[PIIRecord] = []
        total_retries = 0
        permanent_failures = 0
        total_batches = (len(instances) + self.batch_size - 1) // self.batch_size

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

            # --- Retry the batch ---
            batch_succeeded = False
            for attempt in range(MAX_RETRIES):
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
                    if batch_records:
                        records.extend(batch_records)
                        batch_succeeded = True
                        break
                    # Parsed OK but empty — not retry-able
                    batch_succeeded = True
                    break
                except Exception:
                    if attempt < MAX_RETRIES - 1:
                        backoff = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
                        logger.warning(
                            "Batch extraction attempt %d/%d failed at offset %d of %s, "
                            "retrying in %ds",
                            attempt + 1, MAX_RETRIES, batch_start, doc_id, backoff,
                        )
                        total_retries += 1
                        time.sleep(backoff)
                    else:
                        logger.warning(
                            "Batch extraction failed %d times at offset %d of %s, "
                            "splitting to individual instances",
                            MAX_RETRIES, batch_start, doc_id,
                        )

            if batch_succeeded:
                if progress_callback is not None:
                    batch_idx = batch_start // self.batch_size + 1
                    try:
                        progress_callback(batch_idx, total_batches, len(records))
                    except Exception:
                        pass
                continue

            # --- Split to individual instances ---
            for i, instance_pages in enumerate(batch_instances):
                texts_single = batch_texts[i]
                if not any(t.strip() for t in texts_single):
                    continue

                single_prompt = build_extraction_prompt(
                    page_texts=texts_single,
                    page_roles=template.page_roles,
                    instance_index=batch_start + i,
                    document_type=schema.document_type,
                )

                rec = None
                for attempt in range(MAX_RETRIES):
                    try:
                        resp = self.client.generate(
                            single_prompt,
                            system="You extract personal information from documents. "
                            "Respond ONLY with valid JSON.",
                            use_case="template_extraction",
                            document_id=doc_id,
                        )
                        rec = self._parse_extraction(resp, doc_id, instance_pages)
                        if rec is not None:
                            break
                        break  # parsed OK, no person
                    except Exception:
                        if attempt < MAX_RETRIES - 1:
                            backoff = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
                            total_retries += 1
                            time.sleep(backoff)
                        else:
                            permanent_failures += 1

                if rec is not None:
                    records.append(rec)

            if progress_callback is not None:
                batch_idx = batch_start // self.batch_size + 1
                try:
                    progress_callback(batch_idx, total_batches, len(records))
                except Exception:
                    pass

        return records, total_retries, permanent_failures

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
        Validates DOB (rejects transaction dates) and email (rejects URLs).
        Only includes entity types in entity_types_found for fields that
        are actually populated.
        """
        raw_name: str | None = None
        raw_email: str | None = None
        raw_phone: str | None = None
        raw_dob: str | None = None
        raw_address: dict | None = None
        raw_government_id: str | None = None
        government_id_type: str | None = None

        for entity_type, value in data.items():
            if value is None or value == "" or value == "null":
                continue
            value_str = str(value).strip()
            if not value_str:
                continue

            raw_field = _FIELD_TO_RAW.get(entity_type)
            if raw_field is None:
                continue

            if raw_field == "raw_name":
                raw_name = value_str
            elif raw_field == "raw_email":
                if validate_email(value_str):
                    raw_email = value_str
            elif raw_field == "raw_phone":
                raw_phone = value_str
            elif raw_field == "raw_dob":
                if validate_dob(value_str):
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

        # Build entity_types_found from actually-populated fields only
        entity_types_found: list[str] = ["PERSON"]
        if raw_address:
            entity_types_found.append("LOCATION")
        if raw_dob:
            entity_types_found.append("DATE_OF_BIRTH")
        if raw_government_id:
            entity_types_found.append(government_id_type or "GOVERNMENT_ID")
        if raw_email:
            entity_types_found.append("EMAIL_ADDRESS")
        if raw_phone:
            entity_types_found.append("PHONE_NUMBER")

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
    # Table extraction (multiple individuals per page)
    # ------------------------------------------------------------------

    def extract_table_pages(
        self,
        schema: DocumentSchema,
        page_texts: dict[int, str],
        doc_id: str,
        *,
        progress_callback: "Callable[[int, int, int], None] | None" = None,
    ) -> list[PIIRecord]:
        """Extract multiple individuals per page from tabular documents.

        Text-based fallback for when vision is unavailable.
        Each page may have many rows of data.
        """
        if not page_texts:
            return []

        all_records: list[PIIRecord] = []
        sorted_pages = sorted(page_texts.keys())
        total_batches = (len(sorted_pages) + self.batch_size - 1) // self.batch_size

        for batch_start in range(0, len(sorted_pages), self.batch_size):
            batch_pages = sorted_pages[batch_start:batch_start + self.batch_size]
            texts = [
                page_texts.get(p, "")[:_MAX_PAGE_CHARS]
                for p in batch_pages
            ]
            if not any(t.strip() for t in texts):
                continue

            prompt = self._build_table_text_prompt(texts, schema)

            try:
                response = self.client.generate(
                    prompt,
                    system="You extract personal information from documents. "
                    "Respond ONLY with valid JSON.",
                    use_case="table_text_extraction",
                    document_id=doc_id,
                )
                records = self._parse_table_text_response(response, doc_id, batch_pages)
                all_records.extend(records)
            except Exception:
                logger.warning(
                    "Text table extraction failed for pages %s of %s",
                    batch_pages, doc_id, exc_info=True,
                )

            if progress_callback is not None:
                batch_idx = batch_start // self.batch_size + 1
                try:
                    progress_callback(batch_idx, total_batches, len(all_records))
                except Exception:
                    pass

        return _deduplicate_records(all_records, instance_aware=False)

    def _build_table_text_prompt(
        self,
        page_texts: list[str],
        schema: DocumentSchema,
    ) -> str:
        """Build prompt for text-based table extraction."""
        from app.llm.extraction_prompts import (
            ALWAYS_EXTRACT_IF_PRESENT,
            ENTITY_EXTRACTION_GUIDE,
        )

        fields = set(ALWAYS_EXTRACT_IF_PRESENT)
        if schema.tables:
            for table in schema.tables:
                for col in table.columns:
                    if col.contains_pii and col.pii_type:
                        fields.add(col.pii_type)

        field_guide = "\n".join(
            f"- {f}: {ENTITY_EXTRACTION_GUIDE.get(f, f'Extract {f}')}"
            for f in sorted(fields)
        )

        page_sections = []
        for i, text in enumerate(page_texts):
            if text.strip():
                page_sections.append(f"--- PAGE {i + 1} ---\n{text}")

        return (
            f"You are extracting personal information from a {schema.document_type}.\n"
            "This is a tabular document with MULTIPLE individuals per page.\n\n"
            + "\n\n".join(page_sections)
            + "\n\n"
            f"For EACH individual/row found, extract:\n{field_guide}\n\n"
            "Return a JSON ARRAY with one object per individual:\n"
            '[\n  {"PERSON": "...", "LOCATION": "...", ...},\n  ...\n]\n\n'
            "RULES:\n"
            "- Extract EVERY individual/row visible on each page\n"
            "- Column headers are NOT individuals — skip them\n"
            "- If a value is empty, set to null\n"
            "- For addresses, include the COMPLETE address\n"
            "- Organization names are NOT individuals\n"
            "- Return ONLY valid JSON array"
        )

    def _parse_table_text_response(
        self,
        response_text: str,
        doc_id: str,
        page_numbers: list[int],
    ) -> list[PIIRecord]:
        """Parse text table extraction response."""
        data = _parse_json(response_text)
        if data is None:
            return []

        if isinstance(data, dict):
            data = [data]

        if not isinstance(data, list):
            return []

        records: list[PIIRecord] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            rec = self._data_to_record(item, doc_id, page_numbers)
            if rec is not None:
                records.append(rec)

        return records

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


def _build_anchor_key(
    rec: PIIRecord,
    active_anchors: list[str] | None,
) -> str | tuple:
    """Build a merge key from the record based on active dedup anchors.

    Priority order (first match wins):
    1. ``"ssn"`` + record has government_id → key includes government_id
    2. ``"name_dob"`` + record has name + dob → key = (name, dob)
    3. ``"email"`` + record has email → key includes email
    4. ``"phone"`` + record has phone → key includes phone
    5. ``"name_address"`` + record has name + address → key = (name, address)
    6. Fallback: (name, page_range) — instance-aware, never cross-merges
    """
    name_norm = " ".join(rec.raw_name.lower().split()) if rec.raw_name else ""
    if not name_norm:
        return ("", rec.page_range or "")

    if active_anchors is None:
        # Default: instance-aware (backward compatible)
        return (name_norm, rec.page_range or "")

    anchors = set(a.lower().strip() for a in active_anchors)

    if "ssn" in anchors and rec.raw_government_id:
        gov_norm = rec.raw_government_id.strip().upper().replace(" ", "")
        return ("gov", gov_norm)

    if "name_dob" in anchors and rec.raw_dob:
        return ("name_dob", name_norm, rec.raw_dob.strip().lower())

    if "email" in anchors and rec.raw_email:
        return ("email", rec.raw_email.strip().lower())

    if "phone" in anchors and rec.raw_phone:
        return ("phone", rec.raw_phone.strip())

    if "name_address" in anchors and rec.raw_address:
        addr_str = str(rec.raw_address).lower()
        return ("name_addr", name_norm, addr_str)

    # Fallback: instance-aware
    return (name_norm, rec.page_range or "")


def _deduplicate_records(
    records: list[PIIRecord],
    *,
    instance_aware: bool = True,
    active_anchors: list[str] | None = None,
) -> list[PIIRecord]:
    """Deduplicate PIIRecords using configurable anchor strategy.

    When ``active_anchors`` is provided (from protocol config), records are
    merged when they share the anchor value (e.g., same government ID).
    Page ranges are concatenated for lineage.

    When ``active_anchors`` is ``None`` (default):
        - ``instance_aware=True``: Key = (name, page_range) — template docs
        - ``instance_aware=False``: Key = name only — tabular docs

    Within the same key, merges fields: keeps the most-populated record,
    fills in any gaps from duplicates.  Combines entity_types_found.
    """
    if not records:
        return records

    seen: dict[str | tuple, PIIRecord] = {}

    for rec in records:
        if not rec.raw_name:
            continue

        if active_anchors is not None:
            key = _build_anchor_key(rec, active_anchors)
        else:
            name_norm = " ".join(rec.raw_name.lower().split())
            if instance_aware:
                key = (name_norm, rec.page_range or "")
            else:
                key = name_norm

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

        # Combine page ranges (concatenate for lineage)
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
    """Parse JSON from LLM response, handling markdown fences and truncation.

    Handles common LLM response issues:
    - Markdown ````` ``` ````` fences around JSON
    - "Extra data" errors (multiple JSON objects concatenated)
    - Truncated JSON (attempts to close open brackets)
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = [ln for ln in lines if not ln.strip().startswith("```")]
        cleaned = "\n".join(lines).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Try to find JSON within the response
    for start_char in ("[", "{"):
        idx = cleaned.find(start_char)
        if idx < 0:
            continue
        fragment = cleaned[idx:]
        try:
            return json.loads(fragment)
        except json.JSONDecodeError as e:
            # "Extra data" — multiple JSON values concatenated.
            # Use json.JSONDecoder to parse just the first valid value.
            if "Extra data" in str(e):
                try:
                    decoder = json.JSONDecoder()
                    result, _ = decoder.raw_decode(fragment)
                    return result
                except json.JSONDecodeError:
                    pass

            # Truncated JSON — try to close open brackets
            result = _try_close_truncated(fragment)
            if result is not None:
                return result

    return None


def _try_close_truncated(text: str) -> dict | list | None:
    """Attempt to close truncated JSON by adding missing brackets/braces."""
    # Count open vs close brackets
    open_brackets = text.count("[") - text.count("]")
    open_braces = text.count("{") - text.count("}")

    if open_brackets <= 0 and open_braces <= 0:
        return None

    # Remove trailing comma if present
    attempt = text.rstrip().rstrip(",")

    # Close in reverse order (braces first, then brackets)
    attempt += "}" * max(0, open_braces) + "]" * max(0, open_brackets)

    try:
        return json.loads(attempt)
    except json.JSONDecodeError:
        # Try more aggressively: truncate to last complete object
        if text.startswith("["):
            # Find the last complete "}" before the truncation
            last_brace = text.rfind("}")
            if last_brace > 0:
                candidate = text[:last_brace + 1] + "]"
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    pass
        return None


def _unload_unused_models(client: OllamaClient) -> None:
    """Unload non-active models from Ollama to free VRAM.

    Posts keep_alive=0 to tell Ollama to release the model from memory
    for any model that is NOT the client's active model.
    """
    try:
        import httpx
        resp = httpx.get(f"{client.base_url}/api/tags", timeout=10)
        if resp.status_code != 200:
            return
        models = resp.json().get("models", [])
        for m in models:
            model_name = m.get("name", "")
            if model_name and model_name != client.model:
                try:
                    httpx.post(
                        f"{client.base_url}/api/generate",
                        json={"model": model_name, "keep_alive": 0},
                        timeout=10,
                    )
                    logger.debug("Unloaded model %s to free VRAM", model_name)
                except Exception:
                    pass
    except Exception:
        pass  # best-effort
