"""LLM reconciliation for failed coordinate extraction pages (Step 21c).

When ``CoordinateExtractor`` fails to extract PII from a page (anchor text
not found, value pattern didn't match, PERSON field missing), those pages
are sent to the LLM for direct extraction.

This is a lightweight fallback — typically <5% of pages need reconciliation
for fixed-layout documents.  Each failed page gets one LLM call.
"""
from __future__ import annotations

import json
import logging
from uuid import uuid4

import fitz  # PyMuPDF

from app.llm.client import OllamaClient
from app.rra.entity_resolver import PIIRecord
from app.structure.document_schema import FieldMapping

logger = logging.getLogger(__name__)

# Maximum characters per page sent to LLM
_MAX_PAGE_CHARS = 3000


def _build_reconciliation_prompt(
    page_text: str,
    field_map: list[FieldMapping],
) -> str:
    """Build a reconciliation prompt for a single failed page.

    Tells the LLM what fields are expected and asks it to extract them
    from the raw page text.
    """
    field_descriptions = []
    for fm in field_map:
        desc = f"- {fm.field_type}: look for label \"{fm.anchor_text}\""
        if fm.value_pattern:
            desc += f" (pattern: {fm.value_pattern})"
        field_descriptions.append(desc)

    fields_text = "\n".join(field_descriptions)

    return f"""Extract PII fields from this document page.
The document has a fixed layout with these expected fields:
{fields_text}

Page text:
---
{page_text[:_MAX_PAGE_CHARS]}
---

Return a JSON object with the field types as keys and extracted values as strings.
Example: {{"PERSON": "John Smith", "DATE_OF_BIRTH": "15/03/1980", "LOCATION": "123 Main St, London"}}

Return ONLY valid JSON. If a field is not found, omit it from the output."""


# ---------------------------------------------------------------------------
# Field mapping (same as coordinate_extractor)
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


# ---------------------------------------------------------------------------
# ExtractionReconciler
# ---------------------------------------------------------------------------


class ExtractionReconciler:
    """Send failed coordinate-extraction pages to LLM for recovery.

    Typically handles <5% of pages in a fixed-layout document.
    """

    def reconcile(
        self,
        failed_pages: list[int],
        doc_path: str,
        doc_id: str,
        field_map: list[FieldMapping],
        ollama_client: OllamaClient,
    ) -> list[PIIRecord]:
        """Attempt LLM extraction for each failed page.

        Returns recovered ``PIIRecord`` objects.  Pages where the LLM also
        fails are silently dropped (logged as warnings).
        """
        if not failed_pages:
            return []

        records: list[PIIRecord] = []
        still_failed: list[int] = []

        doc = fitz.open(doc_path)

        for page_num in failed_pages:
            if page_num < 0 or page_num >= doc.page_count:
                still_failed.append(page_num)
                continue

            page_text = doc[page_num].get_text("text")
            prompt = _build_reconciliation_prompt(page_text, field_map)

            try:
                response = ollama_client.generate(
                    prompt=prompt,
                    system="You are a document data transcription assistant. Extract PII fields accurately.",
                    use_case="reconciliation_extraction",
                    document_id=doc_id,
                )
                rec = self._parse_response(response, doc_id, page_num, field_map)
                if rec:
                    records.append(rec)
                else:
                    still_failed.append(page_num)
            except Exception:
                logger.warning(
                    "Reconciliation LLM call failed for page %d (doc=%s)",
                    page_num, doc_id, exc_info=True,
                )
                still_failed.append(page_num)

        doc.close()

        logger.info(
            "Reconciliation: %d recovered, %d still failed (doc=%s)",
            len(records), len(still_failed), doc_id,
        )
        return records

    @staticmethod
    def _parse_response(
        response: str,
        doc_id: str,
        page_num: int,
        field_map: list[FieldMapping],
    ) -> PIIRecord | None:
        """Parse LLM JSON response into a PIIRecord.

        Returns ``None`` if the response doesn't contain a valid PERSON field.
        """
        # Strip markdown code fences if present
        text = response.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            # Remove first line (```json) and last line (```)
            lines = [ln for ln in lines if not ln.strip().startswith("```")]
            text = "\n".join(lines).strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Try to extract JSON from the response
            import re
            match = re.search(r"\{[^{}]+\}", text, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group())
                except json.JSONDecodeError:
                    return None
            else:
                return None

        if not isinstance(data, dict):
            return None

        # Must have a PERSON field
        person_name = data.get("PERSON")
        if not person_name or not isinstance(person_name, str):
            return None

        # Collect fields into a dict first (PIIRecord is frozen)
        fields: dict[str, str | dict | None] = {
            "raw_name": person_name.strip(),
            "raw_address": None,
            "raw_phone": None,
            "raw_email": None,
            "raw_dob": None,
            "raw_government_id": None,
        }
        entity_types_found = ["PERSON"]

        for key, value in data.items():
            if key == "PERSON" or not isinstance(value, str) or not value.strip():
                continue
            raw_field = _FIELD_TO_RAW.get(key)
            if not raw_field:
                continue
            val = value.strip()
            entity_types_found.append(key)
            if raw_field == "raw_address":
                fields["raw_address"] = {"full": val}
            else:
                fields[raw_field] = val

        return PIIRecord(
            record_id=str(uuid4()),
            entity_type="PERSON",
            normalized_value=person_name.strip(),
            raw_name=fields["raw_name"],
            raw_address=fields["raw_address"],
            raw_phone=fields["raw_phone"] if isinstance(fields["raw_phone"], str) else None,
            raw_email=fields["raw_email"] if isinstance(fields["raw_email"], str) else None,
            raw_dob=fields["raw_dob"] if isinstance(fields["raw_dob"], str) else None,
            raw_government_id=fields["raw_government_id"] if isinstance(fields["raw_government_id"], str) else None,
            source_document_id=doc_id,
            page_range=str(page_num + 1),
            entity_types_found=tuple(entity_types_found),
        )
