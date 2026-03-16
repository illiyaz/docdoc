"""Vision-based document routing (Step 22a).

Reads ONE page with the vision model to determine:
- What PII fields exist (names, SSN, address, DOB, etc.)
- Document structure type (fixed, template, table, variable)
- Whether data spans multiple pages

This is the FIRST step in extraction — it decides which path to use.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from app.llm.client import OllamaClient
from app.pdf.renderer import render_page_to_image

logger = logging.getLogger(__name__)

# Valid structure types returned by the vision model
_VALID_STRUCTURE_TYPES = frozenset({
    "fixed_single_page",
    "multi_page_template",
    "table",
    "variable",
})


@dataclass
class VisionRoutingResult:
    """Result from vision-based document analysis."""

    # Document structure classification
    structure_type: str  # "fixed_single_page" | "multi_page_template" | "table" | "variable"
    structure_confidence: float  # 0.0-1.0

    # PII fields identified on the sample page
    pii_fields: list[dict] = field(default_factory=list)

    # Estimated records per page
    records_per_page: int = 1

    # Whether data flows across page boundaries
    cross_page_data: bool = False

    # Estimated pages per instance (for multi_page_template)
    pages_per_instance: int = 1

    # Recommended extraction path
    recommended_path: str = "presidio"

    # Raw vision model response (for debugging)
    raw_response: str = ""


# ------------------------------------------------------------------
# Routing prompt
# ------------------------------------------------------------------

_ROUTING_PROMPT = """\
Analyze this document page. I need to understand the structure for bulk PII extraction.

Answer these questions in JSON format:

1. "pii_fields": List every PII field you can see. For EACH field, report:
   - "type": The PII type (PERSON, LOCATION, US_SSN, DATE_OF_BIRTH, PHONE_NUMBER, EMAIL_ADDRESS, GOVERNMENT_ID, ACCOUNT_NUMBER)
   - "value": The actual value you see (e.g., "ADELINE CHANDLER")
   - "label": The text label next to it (e.g., "Client:", "Tax No.", "Name:", "Address:")
   - "position": Where on the page roughly ("top_left", "top_right", "middle_left", etc.)

2. "structure_type": One of:
   - "fixed_single_page" — Every page has the SAME layout with labeled fields. One person per page. Example: account statements, payslips, individual forms.
   - "multi_page_template" — Each person spans MULTIPLE pages (e.g., 3 pages per person). The layout repeats every N pages.
   - "table" — Multiple people per page in a table/grid/list format. Rows = people, columns = fields.
   - "variable" — No repeating structure. Free-form text, letters, mixed content.

3. "records_per_page": How many individual people/records appear on this single page? (1 for statements, N for tables)

4. "cross_page_data": true/false — Does it look like a person's data might continue on the next page? (e.g., address starts on this page but might continue, or you can see "Page 1 of 3" per person)

5. "pages_per_instance": If multi_page_template, how many pages per person? (e.g., 3). Otherwise 1.

IMPORTANT: Only report PII you can ACTUALLY SEE on this page. Do not guess or infer values.
Report the EXACT text values as they appear.

Respond ONLY with valid JSON, no explanation."""


class VisionRouter:
    """Routes documents to the best extraction path using vision analysis."""

    def __init__(
        self,
        ollama_client: OllamaClient,
        vision_model: str | None = None,
    ) -> None:
        self.client = ollama_client
        self.vision_model = vision_model

    def analyze_document(
        self,
        doc_path: str,
        onset_page: int = 0,
        total_pages: int = 1,
        is_scanned: bool = False,
    ) -> VisionRoutingResult:
        """Analyze one page with vision model to determine routing.

        Renders the onset page as an image, sends to the vision model,
        and parses the response to determine the best extraction path.
        """
        # Render the onset page
        try:
            image = render_page_to_image(doc_path, onset_page, dpi=200)
        except Exception:
            logger.warning(
                "Failed to render page %d of %s for routing",
                onset_page,
                doc_path,
                exc_info=True,
            )
            return VisionRoutingResult(
                structure_type="variable",
                structure_confidence=0.0,
                recommended_path="presidio",
            )

        # Send to vision model
        prompt = self._build_routing_prompt(total_pages)
        try:
            response = self.client.generate_with_images(
                prompt=prompt,
                images=[image],
                use_case="vision_routing",
                model_override=self.vision_model,
            )
        except Exception:
            logger.warning(
                "Vision routing failed for %s",
                doc_path,
                exc_info=True,
            )
            return VisionRoutingResult(
                structure_type="variable",
                structure_confidence=0.0,
                recommended_path="presidio",
            )

        # Parse the response and determine routing
        result = self._parse_routing_response(response, total_pages)
        result.recommended_path = self._determine_path(
            result, total_pages, is_scanned,
        )
        return result

    def _build_routing_prompt(self, total_pages: int) -> str:
        """Build the vision analysis prompt."""
        suffix = ""
        if total_pages > 1:
            suffix = (
                f"\n\nThis document has {total_pages} total pages. "
                "Consider whether the layout likely repeats across pages."
            )
        return _ROUTING_PROMPT + suffix

    def _parse_routing_response(
        self,
        response: str,
        total_pages: int,
    ) -> VisionRoutingResult:
        """Parse vision model JSON response into VisionRoutingResult.

        Handles malformed JSON gracefully — returns variable/presidio default.
        """
        # Try to extract JSON from the response
        data = _parse_json(response)
        if data is None:
            return VisionRoutingResult(
                structure_type="variable",
                structure_confidence=0.0,
                raw_response=response,
            )

        # Extract structure_type
        structure_type = str(data.get("structure_type", "variable")).strip()
        if structure_type not in _VALID_STRUCTURE_TYPES:
            structure_type = "variable"

        # Extract pii_fields
        pii_fields = data.get("pii_fields", [])
        if not isinstance(pii_fields, list):
            pii_fields = []
        # Ensure each entry is a dict
        pii_fields = [f for f in pii_fields if isinstance(f, dict)]

        # Extract records_per_page
        records_per_page = _safe_int(data.get("records_per_page"), default=1)

        # Extract cross_page_data
        cross_page_data = bool(data.get("cross_page_data", False))

        # Extract pages_per_instance
        pages_per_instance = _safe_int(data.get("pages_per_instance"), default=1)

        # Estimate confidence based on PII field count
        confidence = 0.8 if pii_fields else 0.3

        return VisionRoutingResult(
            structure_type=structure_type,
            structure_confidence=confidence,
            pii_fields=pii_fields,
            records_per_page=records_per_page,
            cross_page_data=cross_page_data,
            pages_per_instance=pages_per_instance,
            raw_response=response,
        )

    def _determine_path(
        self,
        result: VisionRoutingResult,
        total_pages: int,
        is_scanned: bool,
    ) -> str:
        """Determine the recommended extraction path.

        Rules:
        - Small docs (<=5 pages) → "vision_direct" (not worth coordinate overhead)
        - Scanned docs → "vision_direct"
        - fixed_single_page with PII fields → "coordinate"
        - multi_page_template → "llm_template"
        - table → "llm_table"
        - variable/unknown → "presidio"
        """
        # Small docs: just use vision directly
        if total_pages <= 5:
            return "vision_direct"

        # Scanned docs without OCR text: vision is the only option
        if is_scanned:
            return "vision_direct"

        # Fixed layout with labeled fields: coordinate extraction
        if result.structure_type == "fixed_single_page" and result.pii_fields:
            return "coordinate"

        # Multi-page template: LLM template path
        if result.structure_type == "multi_page_template":
            return "llm_template"

        # Table: LLM table path
        if result.structure_type == "table":
            return "llm_table"

        # Variable/unknown: Presidio fallback
        return "presidio"


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _parse_json(text: str) -> dict | None:
    """Parse JSON from a vision model response.

    Handles code fences, leading text, and other common LLM quirks.
    """
    if not text or not text.strip():
        return None

    text = text.strip()

    # Strip markdown code fences
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first line (```json or ```)
        lines = lines[1:]
        # Remove last line if it's ```
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    # Try direct parse
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
        return None
    except json.JSONDecodeError:
        pass

    # Try to find JSON object in the text
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

    return None


def _safe_int(value, default: int = 1) -> int:
    """Safely convert a value to int."""
    if value is None:
        return default
    try:
        result = int(value)
        return max(1, result)
    except (ValueError, TypeError):
        return default
