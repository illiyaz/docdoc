"""Vision-language model PII extraction for all document types (Step 20).

Primary extraction path: render PDF pages as images → send to vision model →
get structured JSON directly.  Eliminates text extraction bugs (concatenated
fields, lost table structure, partial addresses).

For template documents: renders the key page per instance (member details)
and extracts in batches.  For non-template: extracts per-page.

Falls back gracefully: if vision model fails, returns empty list and
the caller falls back to text-based extraction.
"""
from __future__ import annotations

import json
import logging
from dataclasses import replace
from uuid import uuid4

from app.llm.client import OllamaClient
from app.llm.extraction_prompts import (
    ALWAYS_EXTRACT_IF_PRESENT,
    ENTITY_EXTRACTION_GUIDE,
)
from app.pdf.renderer import render_page_to_image, render_pages_to_images
from app.rra.entity_resolver import PIIRecord
from app.structure.document_schema import DocumentSchema
from app.structure.llm_template_extractor import (
    _FIELD_TO_RAW,
    _GOV_ID_TYPES,
    _deduplicate_records,
    _parse_json,
)

logger = logging.getLogger(__name__)


class VisionDocumentExtractor:
    """Extract PII from documents using vision-language models.

    Primary extraction path for ALL document types:
    - Template documents: extract from key pages of each instance
    - Non-template documents: extract from each page sequentially

    Falls back to text-based extraction if vision model unavailable.
    """

    def __init__(
        self,
        client: OllamaClient,
        *,
        batch_size: int = 5,
        dpi: int = 150,
        vision_model: str | None = None,
    ) -> None:
        self.client = client
        self.batch_size = max(1, batch_size)
        self.dpi = dpi
        self.vision_model = vision_model

    # ------------------------------------------------------------------
    # Template documents
    # ------------------------------------------------------------------

    def extract_template_instances(
        self,
        doc_path: str,
        schema: DocumentSchema,
        instance_boundaries: list[list[int]],
        doc_id: str,
    ) -> list[PIIRecord]:
        """Extract PII from all template instances using vision.

        For each instance, renders the KEY PAGE (page with most PII fields)
        as an image and sends to the vision model.  Supports batching.

        Returns one PIIRecord per unique individual.
        """
        if not instance_boundaries:
            return []

        key_page_offset = self._find_key_page_offset(schema)
        records: list[PIIRecord] = []

        for batch_start in range(0, len(instance_boundaries), self.batch_size):
            batch = instance_boundaries[batch_start:batch_start + self.batch_size]

            # Render key page for each instance in batch
            batch_images: list[str] = []
            batch_pages: list[list[int]] = []
            for instance_pages in batch:
                key_page = instance_pages[min(key_page_offset, len(instance_pages) - 1)]
                try:
                    image = render_page_to_image(doc_path, key_page, dpi=self.dpi)
                    batch_images.append(image)
                    batch_pages.append(instance_pages)
                except Exception:
                    logger.warning(
                        "Failed to render page %d of %s", key_page, doc_id,
                        exc_info=True,
                    )
                    continue

            if not batch_images:
                continue

            prompt = self._build_batch_prompt(schema, len(batch_images))

            try:
                response = self.client.generate_with_images(
                    prompt=prompt,
                    images=batch_images,
                    use_case="vision_template_extraction",
                    document_id=doc_id,
                    model_override=self.vision_model,
                )
                batch_records = self._parse_batch_response(
                    response, doc_id, batch_pages,
                )
                records.extend(batch_records)
            except Exception:
                logger.warning(
                    "Vision batch extraction failed at offset %d of %s",
                    batch_start, doc_id, exc_info=True,
                )
                continue

        return _deduplicate_records(records)

    # ------------------------------------------------------------------
    # Non-template documents
    # ------------------------------------------------------------------

    def extract_pages(
        self,
        doc_path: str,
        page_numbers: list[int],
        doc_id: str,
        schema: DocumentSchema | None = None,
    ) -> list[PIIRecord]:
        """Extract PII from individual pages using vision.

        For non-template documents (letters, mixed content).
        Each page is rendered and sent to the vision model.
        """
        if not page_numbers:
            return []

        all_records: list[PIIRecord] = []
        batch_size = max(1, min(self.batch_size, 5))

        for batch_start in range(0, len(page_numbers), batch_size):
            batch_page_nums = page_numbers[batch_start:batch_start + batch_size]
            try:
                images = render_pages_to_images(doc_path, batch_page_nums, dpi=self.dpi)
            except Exception:
                logger.warning(
                    "Failed to render pages %s of %s", batch_page_nums, doc_id,
                    exc_info=True,
                )
                continue

            if not images:
                continue

            prompt = self._build_page_extraction_prompt(schema, len(images))

            try:
                response = self.client.generate_with_images(
                    prompt=prompt,
                    images=images,
                    use_case="vision_page_extraction",
                    document_id=doc_id,
                    model_override=self.vision_model,
                )
                page_groups = [[p] for p in batch_page_nums[:len(images)]]
                records = self._parse_batch_response(response, doc_id, page_groups)
                all_records.extend(records)
            except Exception:
                logger.warning(
                    "Vision page extraction failed for pages %s of %s",
                    batch_page_nums, doc_id, exc_info=True,
                )
                continue

        return _deduplicate_records(all_records)

    # ------------------------------------------------------------------
    # Tabular documents (multiple individuals per page)
    # ------------------------------------------------------------------

    def extract_table_pages(
        self,
        doc_path: str,
        page_numbers: list[int],
        doc_id: str,
        schema: DocumentSchema | None = None,
    ) -> list[PIIRecord]:
        """Extract multiple individuals from tabular pages using vision.

        Each page may contain many rows of data (10-50 individuals).
        Returns multiple PIIRecords per page.
        """
        if not page_numbers:
            return []

        all_records: list[PIIRecord] = []

        for batch_start in range(0, len(page_numbers), self.batch_size):
            batch_page_nums = page_numbers[batch_start:batch_start + self.batch_size]
            try:
                images = render_pages_to_images(doc_path, batch_page_nums, dpi=self.dpi)
            except Exception:
                logger.warning(
                    "Failed to render table pages %s of %s",
                    batch_page_nums, doc_id, exc_info=True,
                )
                continue

            if not images:
                continue

            prompt = self._build_table_prompt(schema, len(images))

            try:
                response = self.client.generate_with_images(
                    prompt=prompt,
                    images=images,
                    use_case="vision_table_extraction",
                    document_id=doc_id,
                    model_override=self.vision_model,
                )
                records = self._parse_table_response(response, doc_id, batch_page_nums)
                all_records.extend(records)
            except Exception:
                logger.warning(
                    "Vision table extraction failed for pages %s of %s",
                    batch_page_nums, doc_id, exc_info=True,
                )
                continue

        return _deduplicate_records(all_records)

    def _parse_table_response(
        self,
        response_text: str,
        doc_id: str,
        page_numbers: list[int],
    ) -> list[PIIRecord]:
        """Parse table extraction response — expects JSON array of many records."""
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
            # All records from this batch share the batch's page numbers
            rec = self._data_to_record(item, doc_id, page_numbers)
            if rec is not None:
                records.append(rec)

        return records

    # ------------------------------------------------------------------
    # Prompt builders
    # ------------------------------------------------------------------

    def _build_batch_prompt(self, schema: DocumentSchema, num_images: int) -> str:
        """Build prompt for batch template extraction from schema."""
        fields: set[str] = set()
        if schema.template:
            for role in schema.template.page_roles:
                fields.update(role.pii_fields_expected)
        fields.update(ALWAYS_EXTRACT_IF_PRESENT)

        field_guide = "\n".join(
            f"- {f}: {ENTITY_EXTRACTION_GUIDE.get(f, f'Extract {f}')}"
            for f in sorted(fields)
        )

        field_keys = ", ".join(f'"{f}": "value or null"' for f in sorted(fields))

        return (
            f"You are extracting personal information from "
            f"{schema.document_type or 'a document'}.\n"
            f"Each image is one person's record page. "
            f"There are {num_images} images = {num_images} individuals.\n\n"
            f"For EACH image, extract these fields:\n{field_guide}\n\n"
            f"Return a JSON ARRAY with one object per image/individual:\n"
            f"[\n  {{{field_keys}}},\n  ...\n]\n\n"
            "RULES:\n"
            "- Extract the EXACT value as it appears in the document\n"
            "- For addresses, include the COMPLETE address "
            "(every line: street, area, city, county, postcode, country)\n"
            "- For dates, preserve the original format (e.g., 10-Aug-1959)\n"
            "- For names, include title if present (Mr, Mrs, Dr, Miss)\n"
            "- For National Insurance Numbers, extract the full code "
            "(2 letters + 6 digits + 1 letter)\n"
            "- If a field is not visible on the page, set it to null\n"
            "- Do NOT guess or infer values not shown in the image\n"
            "- Return ONLY valid JSON, no other text"
        )

    def _build_table_prompt(
        self,
        schema: DocumentSchema | None,
        num_images: int,
    ) -> str:
        """Build prompt for tabular documents with multiple records per page."""
        # Collect fields from schema tables if available
        extra_fields: list[str] = []
        if schema and schema.tables:
            for table in schema.tables:
                for col in table.columns:
                    if col.contains_pii and col.pii_type:
                        extra_fields.append(col.pii_type)

        field_set = set(ALWAYS_EXTRACT_IF_PRESENT)
        field_set.update(extra_fields)

        field_guide = "\n".join(
            f"- {f}: {ENTITY_EXTRACTION_GUIDE.get(f, f'Extract {f}')}"
            for f in sorted(field_set)
        )

        return (
            "You are extracting personal information from a tabular document.\n"
            f"Each image is one page that may contain MULTIPLE individuals in rows.\n"
            f"There are {num_images} page image(s).\n\n"
            f"For EACH individual/row found, extract:\n{field_guide}\n\n"
            "Return a JSON ARRAY with one object per individual found across ALL pages:\n"
            '[\n'
            '  {"PERSON": "Alice Smith", "LOCATION": "123 Oak St", '
            '"DATE_OF_BIRTH": "03/15/2001", ...},\n'
            '  {"PERSON": "Bob Johnson", "LOCATION": "456 Elm Ave", '
            '"DATE_OF_BIRTH": "07/22/2000", ...},\n'
            '  ...\n'
            ']\n\n'
            "RULES:\n"
            "- Extract EVERY row/individual visible on each page\n"
            "- Column headers are NOT individuals — skip them\n"
            "- If a row spans multiple lines, combine into one record\n"
            "- If a value is empty or illegible, set it to null\n"
            "- For addresses, include the COMPLETE address\n"
            "- For names, include title if present (Mr, Mrs, Dr)\n"
            "- Organization names are NOT individuals\n"
            "- Return ONLY valid JSON array, no other text"
        )

    def _build_page_extraction_prompt(
        self,
        schema: DocumentSchema | None,
        num_images: int,
    ) -> str:
        """Build prompt for non-template page extraction."""
        return (
            "You are extracting personal information from document pages.\n"
            f"There are {num_images} page images. "
            "Multiple individuals may appear on a single page.\n\n"
            "For each individual found, extract:\n"
            "- PERSON: full name with title\n"
            "- LOCATION: complete address\n"
            "- DATE_OF_BIRTH: date of birth\n"
            "- NI_NUMBER: National Insurance Number (UK) or "
            "US_SSN (US) or equivalent\n"
            "- EMAIL_ADDRESS: email\n"
            "- PHONE_NUMBER: phone number\n\n"
            "Return a JSON ARRAY with one object per individual "
            "found across all pages:\n"
            '[  {"PERSON": "...", "LOCATION": "...", '
            '"DATE_OF_BIRTH": "...", ...},\n  ...\n]\n\n'
            "RULES:\n"
            "- Same individual appearing on multiple pages = ONE entry "
            "(merge their data)\n"
            "- Organization names are NOT individuals "
            "(skip companies, schemes, institutions)\n"
            "- Financial terms (Lump Sum, Transfer Value, Pension) "
            "are NOT person names\n"
            "- Return ONLY valid JSON"
        )

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_batch_response(
        self,
        response_text: str,
        doc_id: str,
        batch_pages: list[list[int]],
    ) -> list[PIIRecord]:
        """Parse vision model JSON response into PIIRecords.

        Uses the same defensive parsing as LLMTemplateExtractor (Step 19c).
        """
        data = _parse_json(response_text)
        if data is None:
            return []

        if isinstance(data, dict):
            data = [data]

        if not isinstance(data, list):
            return []

        records: list[PIIRecord] = []
        for i, item in enumerate(data):
            if i >= len(batch_pages):
                break
            if not isinstance(item, dict):
                continue
            rec = self._data_to_record(item, doc_id, batch_pages[i])
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

        Reuses the field mapping from LLMTemplateExtractor.
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

        if not raw_name:
            return None

        # Build page range (1-indexed for humans)
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
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _find_key_page_offset(schema: DocumentSchema) -> int:
        """Find which page in the template has the most PII fields."""
        if not schema.template or not schema.template.page_roles:
            return 1  # default: second page (member details)

        best_offset = 0
        best_count = 0
        for role in schema.template.page_roles:
            count = len(role.pii_fields_expected)
            if count > best_count:
                best_count = count
                best_offset = role.page_offset
        return best_offset
