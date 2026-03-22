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

    # Which model actually produced the response (primary or fallback)
    model_used: str = ""


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


# Fallback prompt — compliance-framed for models (like llama) that may refuse
# to process PII without understanding the legal context.
_ROUTING_PROMPT_FALLBACK = """\
You are assisting a data breach response team with regulatory compliance. \
This document has been legally obtained as part of breach notification requirements \
under state and federal law. Your role is to identify what types of personal \
information appear on this page so affected individuals can be properly notified.

Analyze this document page and report what you see in JSON format:
1. "pii_fields": [{"type":"PERSON|LOCATION|US_SSN|DATE_OF_BIRTH|PHONE_NUMBER|EMAIL_ADDRESS|GOVERNMENT_ID|ACCOUNT_NUMBER","value":"exact text as shown","label":"field label nearby","position":"top_left|top_right|middle_left|etc"}]
2. "structure_type": "fixed_single_page"|"multi_page_template"|"table"|"variable"
3. "records_per_page": number of individuals on this page
4. "cross_page_data": true/false
5. "pages_per_instance": pages per person (1 if single page)
Report only what is directly visible. Use exact text values. JSON only, no explanation."""


class VisionRouter:
    """Routes documents to the best extraction path using vision analysis."""

    def __init__(
        self,
        ollama_client: OllamaClient,
        vision_model: str | None = None,
        fallback_model: str | None = None,
    ) -> None:
        self.client = ollama_client
        self.vision_model = vision_model
        self.fallback_model = fallback_model

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
        
        If the primary model fails (500 error, timeout), automatically
        retries with the fallback model before giving up.
        If all models fail at 200 DPI, retries at 150 DPI (helps with
        landscape/wide pages that cause OOM).
        """
        # Try at 200 DPI first, then 150 DPI on failure
        for dpi in [200, 150]:
            try:
                image = render_page_to_image(doc_path, onset_page, dpi=dpi)
            except Exception:
                logger.warning(
                    "Failed to render page %d of %s at %d DPI",
                    onset_page, doc_path, dpi, exc_info=True,
                )
                continue

            # Try primary model, then fallback on failure
            response = None
            model_used = self.vision_model
            
            models_to_try = [(self.vision_model, False), (self.fallback_model, True)]
            for attempt_model, is_fallback in models_to_try:
                if attempt_model is None:
                    continue
                prompt = self._build_routing_prompt(total_pages, is_fallback=is_fallback)
                try:
                    response = self.client.generate_with_images(
                        prompt=prompt,
                        images=[image],
                        use_case="vision_routing",
                        model_override=attempt_model,
                    )
                    model_used = attempt_model
                    break  # Success
                except Exception:
                    logger.warning(
                        "Vision routing failed with model %s at %d DPI for %s%s",
                        attempt_model, dpi, doc_path,
                        " — trying fallback" if not is_fallback and self.fallback_model else "",
                        exc_info=True,
                    )
                    continue
            
            if response is not None:
                # Parse the response and determine routing
                result = self._parse_routing_response(response, total_pages)
                result.recommended_path = self._determine_path(
                    result, total_pages, is_scanned,
                )
                result.model_used = model_used  # type: ignore[attr-defined]
                return result
            
            if dpi == 200:
                logger.info("All models failed at 200 DPI for %s, retrying at 150 DPI", doc_path)

        # All DPI + model combinations failed
        return VisionRoutingResult(
            structure_type="variable",
            structure_confidence=0.0,
            recommended_path="presidio",
        )

    def _build_routing_prompt(self, total_pages: int, is_fallback: bool = False) -> str:
        """Build the vision analysis prompt.
        
        Uses compliance-framed prompt for fallback models (e.g., llama)
        that may refuse to process PII without legal context.
        """
        base = _ROUTING_PROMPT_FALLBACK if is_fallback else _ROUTING_PROMPT
        suffix = ""
        if total_pages > 1:
            suffix = (
                f"\nDocument has {total_pages} total pages."
                if is_fallback
                else f"\n\nThis document has {total_pages} total pages. "
                     "Consider whether the layout likely repeats across pages."
            )
        return base + suffix

    def _parse_routing_response(
        self,
        response: str,
        total_pages: int,
    ) -> VisionRoutingResult:
        """Parse vision model JSON response into VisionRoutingResult.

        Handles malformed JSON gracefully — returns variable/presidio default.
        Also normalizes edge cases:
        - Bare list: [{"type":"PERSON",...}] → wrap in pii_fields
        - Single field dict: {"type":"PERSON","value":"..."} → wrap in pii_fields
        """
        # Try to extract JSON from the response
        data = _parse_json(response)
        if data is None:
            return VisionRoutingResult(
                structure_type="variable",
                structure_confidence=0.0,
                raw_response=response,
            )

        # Normalize: bare list → wrap in dict
        if isinstance(data, list):
            data = {"pii_fields": data, "structure_type": "unknown", "records_per_page": 1}

        # Normalize: single field dict → wrap in pii_fields
        if isinstance(data, dict) and "pii_fields" not in data and "type" in data and "value" in data:
            data = {"pii_fields": [data], "structure_type": "unknown", "records_per_page": 1}

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


def _parse_json(text: str) -> dict | list | None:
    """Parse JSON from a vision model response.

    Handles code fences, leading text, array responses, and other
    common LLM quirks.
    """
    if not text or not text.strip():
        return None

    text = text.strip()

    # Strip markdown code fences
    if "```" in text:
        for part in text.split("```"):
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{") or part.startswith("["):
                text = part
                break

    # Try direct parse (handles both dict and list)
    try:
        data = json.loads(text)
        if isinstance(data, (dict, list)):
            return data
        return None
    except json.JSONDecodeError:
        pass

    # Try to find JSON object or array in the text
    for start_char, end_char in [("{", "}"), ("[", "]")]:
        start = text.find(start_char)
        if start < 0:
            continue
        depth = 0
        end = start
        for i, ch in enumerate(text[start:], start):
            if ch == start_char:
                depth += 1
            elif ch == end_char:
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        try:
            data = json.loads(text[start:end])
            if isinstance(data, (dict, list)):
                return data
        except json.JSONDecodeError:
            continue

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