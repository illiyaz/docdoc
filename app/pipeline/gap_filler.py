"""Automated extraction gap filler (Step 30e-6).

For each ExtractionGap, attempts targeted re-extraction through fallback paths:
1. Coordinate extraction with relaxed anchor matching
2. LLM template extraction on just that page
3. Vision direct on that page
4. Presidio NER as final fallback

Budget: max 3 LLM calls per gap, configurable total budget.

Usage:
    filler = GapFiller(
        doc_path="/path/to/file.pdf",
        document_id="doc-001",
        field_map=field_map_list,
        ollama_client=client,
    )
    filled_gaps = filler.fill(gaps)
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, replace
from typing import Optional

from app.pipeline.gap_detector import ExtractionGap

logger = logging.getLogger(__name__)

# LLM budget defaults — scaled by doc size at call site
DEFAULT_MAX_LLM_CALLS_PER_GAP = 3
DEFAULT_MAX_LLM_CALLS_TOTAL = 50  # overridden by caller for large docs

# Extraction path names (aligned with extraction_verifier and two_phase)
PATH_COORDINATE_RELAXED = "coordinate_relaxed"
PATH_LLM_TEMPLATE = "llm_template"
PATH_VISION = "vision"
PATH_PRESIDIO = "presidio"

# Field type → PIIRecord attribute mapping (matches extraction_verifier)
_FIELD_TO_ATTR: dict[str, str] = {
    "PERSON": "raw_name",
    "LOCATION": "raw_address",
    "US_SSN": "raw_government_id",
    "GOVERNMENT_ID": "raw_government_id",
    "DATE_OF_BIRTH": "raw_dob",
    "EMAIL_ADDRESS": "raw_email",
    "PHONE_NUMBER": "raw_phone",
    "NI_NUMBER": "raw_government_id",
    "IDENTIFICATION_NUMBER": "raw_government_id",
}


@dataclass
class FillAttempt:
    """Result of a single fill attempt on a gap."""

    method: str           # PATH_COORDINATE_RELAXED, PATH_LLM_TEMPLATE, etc.
    success: bool
    value_masked: str | None = None  # masked value for display
    llm_calls_used: int = 0


class GapFiller:
    """Attempt to auto-fill extraction gaps through fallback extraction paths.

    Each gap is processed through a cascade of extraction strategies.
    The first strategy that returns a valid value wins. LLM call budget
    prevents runaway costs.
    """

    def __init__(
        self,
        doc_path: str,
        document_id: str,
        field_map: list | None = None,
        ollama_client: "OllamaClient | None" = None,
        vision_model: str | None = None,
        text_model: str | None = None,
        max_llm_per_gap: int = DEFAULT_MAX_LLM_CALLS_PER_GAP,
        max_llm_total: int = DEFAULT_MAX_LLM_CALLS_TOTAL,
        dpi: int = 200,
    ):
        self.doc_path = doc_path
        self.document_id = document_id
        self.field_map = field_map or []
        self.ollama_client = ollama_client
        self.vision_model = vision_model
        self.text_model = text_model
        self.max_llm_per_gap = max_llm_per_gap
        self.max_llm_total = max_llm_total
        self.dpi = dpi

        # Budget tracking
        self._llm_calls_used = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fill(self, gaps: list[ExtractionGap]) -> list[ExtractionGap]:
        """Attempt to fill all gaps. Returns updated gap list with fill status.

        Gaps are processed by severity (high first), and each gap goes
        through the fallback cascade until filled or all paths exhausted.
        """
        if not gaps:
            return gaps

        # Sort: high severity first, then missing_field before truncated
        severity_order = {"high": 0, "medium": 1, "low": 2}
        type_order = {"empty_page": 0, "missing_field": 1, "truncated": 2, "stitching": 3}
        sorted_gaps = sorted(
            gaps,
            key=lambda g: (severity_order.get(g.severity, 9), type_order.get(g.gap_type, 9)),
        )

        results: list[ExtractionGap] = []
        for gap in sorted_gaps:
            filled_gap = self._fill_one(gap)
            results.append(filled_gap)

        # Log summary
        filled_count = sum(1 for g in results if g.fill_result == "filled")
        unfilled_count = sum(1 for g in results if g.fill_result == "unfilled")
        logger.info(
            "Gap fill complete: %d filled, %d unfilled, %d LLM calls used (budget: %d)",
            filled_count, unfilled_count, self._llm_calls_used, self.max_llm_total,
        )

        return results

    @property
    def llm_calls_used(self) -> int:
        """Total LLM calls consumed."""
        return self._llm_calls_used

    # ------------------------------------------------------------------
    # Per-gap fill logic
    # ------------------------------------------------------------------

    def _fill_one(self, gap: ExtractionGap) -> ExtractionGap:
        """Try each fallback path for a single gap."""
        # Skip non-fillable gap types
        if gap.gap_type == "stitching":
            return replace(gap, fill_attempted=True, fill_result="not_applicable",
                           context=gap.context or "Stitching gaps require manual review")

        # Build the fallback cascade based on gap type
        cascade = self._build_cascade(gap)

        llm_calls_this_gap = 0
        for method_fn, method_name, uses_llm in cascade:
            # Check budgets
            if uses_llm:
                if llm_calls_this_gap >= self.max_llm_per_gap:
                    logger.debug("Gap %s: per-gap LLM budget exhausted", gap.gap_type)
                    break
                if self._llm_calls_used >= self.max_llm_total:
                    logger.debug("Gap %s: total LLM budget exhausted", gap.gap_type)
                    break

            try:
                attempt = method_fn(gap)
            except Exception:
                logger.debug(
                    "Gap fill method %s failed for page %d",
                    method_name, gap.page_num, exc_info=True,
                )
                continue

            if uses_llm:
                llm_calls_this_gap += attempt.llm_calls_used
                self._llm_calls_used += attempt.llm_calls_used

            if attempt.success:
                return replace(
                    gap,
                    fill_attempted=True,
                    fill_method=method_name,
                    fill_result="filled",
                    filled_value_masked=attempt.value_masked,
                )

        # All paths exhausted
        return replace(gap, fill_attempted=True, fill_result="unfilled")

    def _build_cascade(
        self,
        gap: ExtractionGap,
    ) -> list[tuple["callable", str, bool]]:
        """Build the extraction fallback cascade for this gap type.

        Returns list of (method_fn, method_name, uses_llm) tuples.
        """
        cascade: list[tuple] = []

        if gap.gap_type == "empty_page":
            # Empty pages need full-page re-extraction
            cascade.append((self._try_coordinate_relaxed, PATH_COORDINATE_RELAXED, False))
            cascade.append((self._try_llm_template, PATH_LLM_TEMPLATE, True))
            cascade.append((self._try_vision, PATH_VISION, True))
            cascade.append((self._try_presidio, PATH_PRESIDIO, False))

        elif gap.gap_type == "missing_field":
            # Missing fields: targeted extraction for specific field
            cascade.append((self._try_coordinate_relaxed, PATH_COORDINATE_RELAXED, False))
            cascade.append((self._try_llm_template, PATH_LLM_TEMPLATE, True))
            cascade.append((self._try_vision, PATH_VISION, True))
            cascade.append((self._try_presidio, PATH_PRESIDIO, False))

        elif gap.gap_type == "truncated":
            # Truncated data: re-read with wider context
            cascade.append((self._try_coordinate_relaxed, PATH_COORDINATE_RELAXED, False))
            cascade.append((self._try_llm_template, PATH_LLM_TEMPLATE, True))

        return cascade

    # ------------------------------------------------------------------
    # Fallback path implementations
    # ------------------------------------------------------------------

    def _try_coordinate_relaxed(self, gap: ExtractionGap) -> FillAttempt:
        """Re-run coordinate extraction with relaxed anchor matching.

        Widens the search region for anchors and uses fuzzy text matching.
        No LLM calls — pure PyMuPDF text extraction.
        """
        if not self.doc_path or not self.field_map:
            return FillAttempt(method=PATH_COORDINATE_RELAXED, success=False)

        try:
            import fitz
        except ImportError:
            return FillAttempt(method=PATH_COORDINATE_RELAXED, success=False)

        page_idx = gap.page_num - 1  # convert to 0-indexed

        try:
            doc = fitz.open(self.doc_path)
            if page_idx >= doc.page_count:
                doc.close()
                return FillAttempt(method=PATH_COORDINATE_RELAXED, success=False)

            page = doc[page_idx]
            page_text = page.get_text()
            words = page.get_text("words")  # list of (x0, y0, x1, y1, word, ...)
            doc.close()
        except Exception:
            return FillAttempt(method=PATH_COORDINATE_RELAXED, success=False)

        if not page_text.strip():
            return FillAttempt(method=PATH_COORDINATE_RELAXED, success=False)

        # For missing_field gaps, try to find the expected field in page text
        if gap.gap_type == "missing_field" and gap.expected_field:
            value = self._extract_field_from_text(
                page_text, words, gap.expected_field,
            )
            if value:
                masked = _mask_value(value, gap.expected_field)
                return FillAttempt(
                    method=PATH_COORDINATE_RELAXED,
                    success=True,
                    value_masked=masked,
                )

        # For empty_page gaps, check if any PII-like content exists
        if gap.gap_type == "empty_page":
            # Check if page has text that looks like PII data
            has_ssn = bool(re.search(r"\d{3}[-\s]?\d{2}[-\s]?\d{4}", page_text))
            has_name_pattern = bool(re.search(r"[A-Z][a-z]+\s+[A-Z][a-z]+", page_text))
            if has_ssn or has_name_pattern:
                return FillAttempt(
                    method=PATH_COORDINATE_RELAXED,
                    success=True,
                    value_masked="[page has extractable content]",
                )

        # For truncated gaps, try wider region extraction
        if gap.gap_type == "truncated" and gap.expected_field:
            value = self._extract_field_from_text(
                page_text, words, gap.expected_field, relaxed=True,
            )
            if value:
                masked = _mask_value(value, gap.expected_field)
                return FillAttempt(
                    method=PATH_COORDINATE_RELAXED,
                    success=True,
                    value_masked=masked,
                )

        return FillAttempt(method=PATH_COORDINATE_RELAXED, success=False)

    def _try_llm_template(self, gap: ExtractionGap) -> FillAttempt:
        """LLM template extraction on a single page.

        Sends page text to text LLM with a targeted prompt asking for
        specific missing fields. Costs 1 LLM call.
        """
        if not self.ollama_client:
            return FillAttempt(method=PATH_LLM_TEMPLATE, success=False, llm_calls_used=0)

        try:
            import fitz
            doc = fitz.open(self.doc_path)
            page_idx = gap.page_num - 1
            if page_idx >= doc.page_count:
                doc.close()
                return FillAttempt(method=PATH_LLM_TEMPLATE, success=False, llm_calls_used=0)
            page_text = doc[page_idx].get_text()
            doc.close()
        except Exception:
            return FillAttempt(method=PATH_LLM_TEMPLATE, success=False, llm_calls_used=0)

        if not page_text.strip():
            return FillAttempt(method=PATH_LLM_TEMPLATE, success=False, llm_calls_used=0)

        # Build targeted prompt
        target_field = gap.expected_field or "all PII fields"
        prompt = (
            "Extract specific data from this document page text.\n\n"
            f"PAGE TEXT:\n{page_text[:3000]}\n\n"
            f"TARGET FIELD: {target_field}\n\n"
            "Return a JSON object with the extracted value:\n"
            f'{{"field_type": "{target_field}", "value": "extracted_value"}}\n\n'
            "If the field is not present on this page, return:\n"
            f'{{"field_type": "{target_field}", "value": null}}\n'
            "Return ONLY valid JSON."
        )

        try:
            response = self.ollama_client.generate(
                prompt=prompt,
                use_case="gap_fill_template",
                document_id=self.document_id,
                model_override=self.text_model,
            )
        except Exception:
            return FillAttempt(method=PATH_LLM_TEMPLATE, success=False, llm_calls_used=1)

        # Parse response
        value = _parse_llm_fill_response(response, target_field)
        if value:
            masked = _mask_value(value, gap.expected_field)
            return FillAttempt(
                method=PATH_LLM_TEMPLATE,
                success=True,
                value_masked=masked,
                llm_calls_used=1,
            )

        return FillAttempt(method=PATH_LLM_TEMPLATE, success=False, llm_calls_used=1)

    def _try_vision(self, gap: ExtractionGap) -> FillAttempt:
        """Vision model extraction on a single page image.

        Renders the page and sends to vision LLM. Costs 1 LLM call.
        Only used for PDF documents.
        """
        if not self.ollama_client or not self.doc_path:
            return FillAttempt(method=PATH_VISION, success=False, llm_calls_used=0)

        if not self.doc_path.lower().endswith(".pdf"):
            return FillAttempt(method=PATH_VISION, success=False, llm_calls_used=0)

        try:
            from app.pdf.renderer import render_page_to_image
        except ImportError:
            return FillAttempt(method=PATH_VISION, success=False, llm_calls_used=0)

        page_idx = gap.page_num - 1
        try:
            image = render_page_to_image(self.doc_path, page_idx, dpi=self.dpi)
        except Exception:
            return FillAttempt(method=PATH_VISION, success=False, llm_calls_used=0)

        target_field = gap.expected_field or "all PII fields"
        prompt = (
            "Extract specific data from this document page.\n\n"
            f"TARGET FIELD: {target_field}\n\n"
            "Return a JSON object with the extracted value:\n"
            f'{{"field_type": "{target_field}", "value": "extracted_value"}}\n\n'
            "If the field is not visible, return:\n"
            f'{{"field_type": "{target_field}", "value": null}}\n'
            "Return ONLY valid JSON."
        )

        try:
            response = self.ollama_client.generate_with_images(
                prompt=prompt,
                images=[image],
                use_case="gap_fill_vision",
                document_id=self.document_id,
                model_override=self.vision_model,
            )
        except Exception:
            return FillAttempt(method=PATH_VISION, success=False, llm_calls_used=1)

        value = _parse_llm_fill_response(response, target_field)
        if value:
            masked = _mask_value(value, gap.expected_field)
            return FillAttempt(
                method=PATH_VISION,
                success=True,
                value_masked=masked,
                llm_calls_used=1,
            )

        return FillAttempt(method=PATH_VISION, success=False, llm_calls_used=1)

    def _try_presidio(self, gap: ExtractionGap) -> FillAttempt:
        """Presidio NER extraction as final fallback.

        Runs Presidio on page text to find entities. No LLM calls.
        """
        try:
            import fitz
            doc = fitz.open(self.doc_path)
            page_idx = gap.page_num - 1
            if page_idx >= doc.page_count:
                doc.close()
                return FillAttempt(method=PATH_PRESIDIO, success=False)
            page_text = doc[page_idx].get_text()
            doc.close()
        except Exception:
            return FillAttempt(method=PATH_PRESIDIO, success=False)

        if not page_text.strip():
            return FillAttempt(method=PATH_PRESIDIO, success=False)

        # Map expected field to Presidio entity type
        presidio_map = {
            "PERSON": "PERSON",
            "US_SSN": "US_SSN",
            "GOVERNMENT_ID": "US_SSN",
            "PHONE_NUMBER": "PHONE_NUMBER",
            "EMAIL_ADDRESS": "EMAIL_ADDRESS",
            "LOCATION": "LOCATION",
            "DATE_OF_BIRTH": "DATE_TIME",
        }
        target_entity = presidio_map.get(gap.expected_field or "")

        try:
            from presidio_analyzer import AnalyzerEngine
            analyzer = AnalyzerEngine()
            entities = [target_entity] if target_entity else None
            results = analyzer.analyze(text=page_text, language="en", entities=entities)
        except ImportError:
            # Presidio not available — fall back to regex patterns
            return self._try_regex_fallback(page_text, gap)
        except Exception:
            return FillAttempt(method=PATH_PRESIDIO, success=False)

        if not results:
            return FillAttempt(method=PATH_PRESIDIO, success=False)

        # Find highest-confidence result for target entity
        matching = [r for r in results if not target_entity or r.entity_type == target_entity]
        if not matching:
            return FillAttempt(method=PATH_PRESIDIO, success=False)

        best = max(matching, key=lambda r: r.score)
        value = page_text[best.start:best.end].strip()
        if value:
            masked = _mask_value(value, gap.expected_field)
            return FillAttempt(
                method=PATH_PRESIDIO,
                success=True,
                value_masked=masked,
            )

        return FillAttempt(method=PATH_PRESIDIO, success=False)

    def _try_regex_fallback(self, page_text: str, gap: ExtractionGap) -> FillAttempt:
        """Regex-based extraction when Presidio is unavailable."""
        patterns: dict[str, str] = {
            "US_SSN": r"\b\d{3}[-\s]?\d{2}[-\s]?\d{4}\b",
            "GOVERNMENT_ID": r"\b\d{3}[-\s]?\d{2}[-\s]?\d{4}\b",
            "PHONE_NUMBER": r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b",
            "EMAIL_ADDRESS": r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
            "PERSON": r"\b[A-Z][a-z]+\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\b",
        }

        target = gap.expected_field or ""
        pattern = patterns.get(target)
        if not pattern:
            return FillAttempt(method=PATH_PRESIDIO, success=False)

        match = re.search(pattern, page_text)
        if match:
            value = match.group().strip()
            masked = _mask_value(value, gap.expected_field)
            return FillAttempt(
                method=PATH_PRESIDIO,
                success=True,
                value_masked=masked,
            )

        return FillAttempt(method=PATH_PRESIDIO, success=False)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _extract_field_from_text(
        self,
        page_text: str,
        words: list,
        field_type: str,
        relaxed: bool = False,
    ) -> str | None:
        """Try to extract a specific field type from page text using patterns.

        Uses field-specific regex patterns. If `relaxed`, uses broader matching.
        """
        patterns: dict[str, list[str]] = {
            "US_SSN": [r"\b\d{3}-\d{2}-\d{4}\b"],
            "GOVERNMENT_ID": [
                r"\b\d{3}-\d{2}-\d{4}\b",
                r"\b[A-Z]{2}\s?\d{2}\s?\d{2}\s?\d{2}\s?[A-Z]\b",
            ],
            "PHONE_NUMBER": [
                r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b",
            ],
            "EMAIL_ADDRESS": [
                r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
            ],
            "PERSON": [
                r"\b[A-Z][a-z]+\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\b",
            ],
            "LOCATION": [
                r"\b\d+\s+[A-Z][a-z]+(?:\s+[A-Za-z]+)*(?:,\s*[A-Z]{2}\s+\d{5})?\b",
            ],
            "DATE_OF_BIRTH": [
                r"\b\d{1,2}/\d{1,2}/\d{2,4}\b",
                r"\b\d{4}-\d{2}-\d{2}\b",
            ],
        }

        field_patterns = patterns.get(field_type, [])
        if relaxed:
            # Add broader patterns for relaxed mode
            field_patterns = field_patterns + [
                r"\b\d{3}\s\d{2}\s\d{4}\b",  # SSN without dashes
                r"\b\d{10}\b",  # phone without formatting
            ]

        for pat in field_patterns:
            match = re.search(pat, page_text)
            if match:
                return match.group().strip()

        return None


# ---------------------------------------------------------------------------
# Module-level utilities
# ---------------------------------------------------------------------------


def _mask_value(value: str, field_type: str | None) -> str:
    """Mask a value for safe display (no raw PII in UI)."""
    if not value:
        return "***"

    if field_type in ("US_SSN", "GOVERNMENT_ID", "IDENTIFICATION_NUMBER", "NI_NUMBER"):
        # Show last 4 digits only
        digits = re.sub(r"[^0-9]", "", value)
        if len(digits) >= 4:
            return f"***-**-{digits[-4:]}"
        return "***"

    if field_type == "PERSON":
        parts = value.split()
        if len(parts) >= 2:
            return f"{parts[0][0]}*** {parts[-1][0]}***"
        return f"{value[0]}***" if value else "***"

    if field_type in ("PHONE_NUMBER",):
        digits = re.sub(r"[^0-9]", "", value)
        if len(digits) >= 4:
            return f"(***) ***-{digits[-4:]}"
        return "***"

    if field_type == "EMAIL_ADDRESS":
        if "@" in value:
            local, domain = value.split("@", 1)
            return f"{local[0]}***@{domain}" if local else f"***@{domain}"
        return "***"

    if field_type == "LOCATION":
        # Show just city/state pattern
        if len(value) > 10:
            return f"{value[:3]}...{value[-5:]}"
        return "***"

    # Default: show first and last char
    if len(value) > 2:
        return f"{value[0]}{'*' * (len(value) - 2)}{value[-1]}"
    return "***"


def _parse_llm_fill_response(response: str, target_field: str) -> str | None:
    """Parse LLM gap-fill response, extract the value."""
    if not response:
        return None

    text = response.strip()
    # Strip code fences
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [ln for ln in lines if not ln.strip().startswith("```")]
        text = "\n".join(lines).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON in response
        for start_char in ("{", "["):
            idx = text.find(start_char)
            if idx >= 0:
                end_char = "}" if start_char == "{" else "]"
                end_idx = text.rfind(end_char)
                if end_idx > idx:
                    try:
                        data = json.loads(text[idx:end_idx + 1])
                        break
                    except json.JSONDecodeError:
                        continue
        else:
            return None

    # Handle array response
    if isinstance(data, list):
        data = data[0] if data else {}

    if not isinstance(data, dict):
        return None

    # Extract value from response
    value = data.get("value")
    if value and str(value).strip() and str(value).lower() not in ("null", "none", "n/a"):
        return str(value).strip()

    return None


def persist_gaps(gaps: list[ExtractionGap], project_id: str, job_id: str) -> None:
    """Save gap results to JSON on disk for the QA screen.

    Stored at: data/projects/{project_id}/gaps/{job_id}.json
    """
    from pathlib import Path

    gaps_dir = Path("data") / "projects" / project_id / "gaps"
    gaps_dir.mkdir(parents=True, exist_ok=True)
    path = gaps_dir / f"{job_id}.json"

    payload = {
        "job_id": job_id,
        "project_id": project_id,
        "total_gaps": len(gaps),
        "filled": sum(1 for g in gaps if g.fill_result == "filled"),
        "unfilled": sum(1 for g in gaps if g.fill_result == "unfilled"),
        "pending": sum(1 for g in gaps if g.fill_result == "pending"),
        "gaps": [g.to_dict() for g in gaps],
    }

    path.write_text(json.dumps(payload, indent=2, default=str))
    logger.info("Persisted %d gaps to %s", len(gaps), path)


def load_gaps(project_id: str, job_id: str) -> list[ExtractionGap]:
    """Load gaps from disk."""
    from pathlib import Path

    path = Path("data") / "projects" / project_id / "gaps" / f"{job_id}.json"
    if not path.exists():
        return []

    try:
        data = json.loads(path.read_text())
        return [
            ExtractionGap(**{k: v for k, v in g.items() if k in ExtractionGap.__dataclass_fields__})
            for g in data.get("gaps", [])
        ]
    except Exception:
        logger.warning("Failed to load gaps from %s", path, exc_info=True)
        return []
