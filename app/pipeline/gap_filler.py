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
        """Attempt to fill gaps using batched page-level LLM extraction.

        Optimized approach:
        1. Group gaps by page (3 gaps on page 5 = 1 page read)
        2. Open PDF once for all pages
        3. For each unique page, send text to LLM in batches of 5-10
        4. Parse results and match back to gaps

        Falls back to per-gap processing only for non-LLM methods.
        """
        if not gaps:
            return gaps

        # Sort: high severity first
        severity_order = {"high": 0, "medium": 1, "low": 2}
        type_order = {"empty_page": 0, "missing_field": 1, "truncated": 2, "stitching": 3}
        sorted_gaps = sorted(
            gaps,
            key=lambda g: (severity_order.get(g.severity, 9), type_order.get(g.gap_type, 9)),
        )

        # Mark stitching gaps as not applicable
        results: list[ExtractionGap] = []
        fillable_gaps: list[ExtractionGap] = []
        for gap in sorted_gaps:
            if gap.gap_type == "stitching":
                results.append(replace(gap, fill_attempted=True, fill_result="not_applicable",
                                       context="Stitching gaps require manual review"))
            else:
                fillable_gaps.append(gap)

        # Group by page — deduplicate page reads
        from collections import defaultdict
        gaps_by_page: dict[int, list[ExtractionGap]] = defaultdict(list)
        for gap in fillable_gaps:
            gaps_by_page[gap.page_num].append(gap)

        # Try batched LLM text extraction (fast path)
        filled_pages: set[int] = set()
        logger.info(
            "Gap fill check: ollama_client=%s, llm_calls_used=%d, max_llm_total=%d, gaps_by_page_count=%d, doc_path=%s",
            type(self.ollama_client).__name__ if self.ollama_client else None,
            self._llm_calls_used, self.max_llm_total, len(gaps_by_page), self.doc_path,
        )
        if self.ollama_client and self._llm_calls_used < self.max_llm_total:
            filled_pages = self._fill_batched_text(gaps_by_page, fillable_gaps, results)

        # --- Self-correcting loop ---
        # If fill rate is low, diagnose WHY pages were missed and retry
        # with adjusted prompts. Costs 1 diagnostic call + re-extraction.
        if (
            self.ollama_client
            and self._llm_calls_used < self.max_llm_total
            and len(gaps_by_page) > 5
        ):
            _filled_so_far = sum(1 for r in results if r.fill_result == "filled")
            _fill_rate = _filled_so_far / max(len(fillable_gaps), 1)
            if _fill_rate < 0.3:  # less than 30% filled — something is wrong
                _new_fills = self._self_correct(gaps_by_page, filled_pages, results)
                filled_pages.update(_new_fills)

        # Remaining unfilled gaps → mark as unfilled
        for gap in fillable_gaps:
            if gap.page_num not in filled_pages:
                already = any(r.page_num == gap.page_num and r.gap_type == gap.gap_type for r in results)
                if not already:
                    results.append(replace(gap, fill_attempted=True, fill_result="unfilled"))

        # Log summary
        filled_count = sum(1 for g in results if g.fill_result == "filled")
        unfilled_count = sum(1 for g in results if g.fill_result == "unfilled")
        logger.info(
            "Gap fill complete: %d filled, %d unfilled, %d LLM calls used (budget: %d)",
            filled_count, unfilled_count, self._llm_calls_used, self.max_llm_total,
        )

        return results

    def _fill_batched_text(
        self,
        gaps_by_page: dict[int, list[ExtractionGap]],
        all_gaps: list[ExtractionGap],
        results: list[ExtractionGap],
    ) -> set[int]:
        """Fill gaps using batched text LLM calls — send 5 pages per call.

        Returns set of page numbers that were successfully processed.
        """
        filled_pages: set[int] = set()

        # Read page texts from PDF — ONE open for all pages
        page_texts: dict[int, str] = {}
        try:
            import fitz
            doc = fitz.open(self.doc_path)
            for page_num in gaps_by_page:
                page_idx = page_num - 1  # gaps use 1-indexed
                if 0 <= page_idx < doc.page_count:
                    page_texts[page_num] = doc[page_idx].get_text()
                    doc._forget_page(page_idx)
            doc.close()
        except Exception:
            logger.warning("Gap fill: could not read PDF %s", self.doc_path, exc_info=True)
            return filled_pages

        if not page_texts:
            return filled_pages

        # Batch pages into groups of 5
        page_nums = sorted(page_texts.keys())
        batch_size = 5

        for batch_start in range(0, len(page_nums), batch_size):
            if self._llm_calls_used >= self.max_llm_total:
                break

            batch_pages = page_nums[batch_start:batch_start + batch_size]
            batch_text = ""
            for pn in batch_pages:
                text = page_texts.get(pn, "")
                if text.strip():
                    batch_text += f"\n--- PAGE {pn} ---\n{text[:3000]}\n"

            if not batch_text.strip():
                continue

            # Build prompt
            missing_fields = set()
            for pn in batch_pages:
                for gap in gaps_by_page.get(pn, []):
                    if gap.expected_field:
                        missing_fields.add(gap.expected_field)
                    else:
                        missing_fields.update(["PERSON", "LOCATION"])

            fields_str = ", ".join(sorted(missing_fields)) or "PERSON, LOCATION"
            # Ask for everything useful so gap-fill can synthesize records
            # for people who didn't make it through the main paths
            # (BIG_FIXES #A3). Keys kept lowercase for stable JSON output.
            prompt = (
                f"Extract personal information from these {len(batch_pages)} pages.\n"
                f"For EACH page, extract the PRIMARY SUBJECT only (not teachers, "
                f"doctors, providers, or institutional staff).\n"
                f"Fields hint (prioritize these): {fields_str}\n"
                f"Return a JSON array with one object per page:\n"
                f'[{{"page": 5, "name": "Full Name", "gov_id": "ID number if any", '
                f'"dob": "date of birth", "address": "full mailing address"}}, ...]\n'
                f"Use null for any field not present. "
                f"If a page has no extractable person, omit it from the array.\n\n"
                f"{batch_text}"
            )

            try:
                response = self.ollama_client.generate(
                    prompt=prompt,
                    system="You are a document data extraction assistant. Extract only the primary subject's information.",
                    use_case="gap_fill_batch",
                    document_id=self.document_id,
                )
                self._llm_calls_used += 1

                # Parse response
                import json
                cleaned = response.strip()
                if cleaned.startswith("```"):
                    lines = cleaned.split("\n")
                    lines = [l for l in lines if not l.strip().startswith("```")]
                    cleaned = "\n".join(lines)

                records = json.loads(cleaned)
                if not isinstance(records, list):
                    records = [records]

                for rec in records:
                    page_num = rec.get("page")
                    if page_num and page_num in gaps_by_page:
                        # Accept both old (PERSON/LOCATION) and new
                        # (name/gov_id/dob/address) keys so a mid-deploy
                        # prompt-format change doesn't drop fills.
                        person = rec.get("name") or rec.get("PERSON", "") or ""
                        location = rec.get("address") or rec.get("LOCATION", "") or ""
                        gov_id = rec.get("gov_id") or rec.get("US_SSN") or ""
                        dob = rec.get("dob") or rec.get("DATE_OF_BIRTH") or ""
                        # Structured fill_data keeps raw values for
                        # downstream record synthesis (BIG_FIXES #A3).
                        raw_fill: dict = {}
                        if person and person not in (None, "null", ""):
                            raw_fill["name"] = str(person).strip()
                        if location and location not in (None, "null", ""):
                            raw_fill["address"] = str(location).strip()
                        if gov_id and gov_id not in (None, "null", ""):
                            raw_fill["gov_id"] = str(gov_id).strip()
                        if dob and dob not in (None, "null", ""):
                            raw_fill["dob"] = str(dob).strip()

                        if raw_fill:
                            filled_pages.add(page_num)
                            for gap in gaps_by_page[page_num]:
                                value = ""
                                if gap.expected_field == "PERSON" and person:
                                    value = person
                                elif gap.expected_field == "LOCATION" and location:
                                    value = location
                                elif gap.gap_type == "empty_page" and person:
                                    value = person

                                results.append(replace(
                                    gap,
                                    fill_attempted=True,
                                    fill_method="text_batch",
                                    fill_result="filled",
                                    filled_value_masked=_mask_value(
                                        value or raw_fill.get("name", ""),
                                        gap.expected_field or "PERSON",
                                    ),
                                    fill_data=raw_fill,
                                ))
                        else:
                            for gap in gaps_by_page[page_num]:
                                results.append(replace(gap, fill_attempted=True, fill_result="unfilled"))

            except Exception:
                logger.warning("Gap fill batch failed for pages %s", batch_pages, exc_info=True)

        return filled_pages

    def _self_correct(
        self,
        gaps_by_page: dict[int, list[ExtractionGap]],
        already_filled: set[int],
        results: list[ExtractionGap],
    ) -> set[int]:
        """Self-correcting loop: diagnose why extraction missed pages, then retry.

        1. Pick 2-3 unfilled pages as samples
        2. Send them to LLM with diagnostic prompt: "what PII is here and
           in what format?"
        3. Use the diagnosis to build an adjusted extraction prompt
        4. Re-extract all unfilled pages with the adjusted prompt
        """
        unfilled_pages = sorted(p for p in gaps_by_page if p not in already_filled)
        if not unfilled_pages or self._llm_calls_used >= self.max_llm_total:
            return set()

        new_fills: set[int] = set()

        # Step 1: Read sample pages — pick from beginning, middle, and end
        # to get diverse page types (not just the first 3 which may all be boilerplate)
        _n = len(unfilled_pages)
        sample_indices = sorted(set([0, _n // 2, _n - 1]))
        sample_pages = [unfilled_pages[i] for i in sample_indices if i < _n]
        page_texts: dict[int, str] = {}
        try:
            import fitz
            doc = fitz.open(self.doc_path)
            for pn in sample_pages:
                pg_idx = pn - 1
                if 0 <= pg_idx < doc.page_count:
                    page_texts[pn] = doc[pg_idx].get_text()
                    doc._forget_page(pg_idx)
            doc.close()
        except Exception:
            return set()

        if not page_texts:
            return set()

        # Step 2: Diagnostic prompt
        sample_text = ""
        for pn in sample_pages:
            if pn in page_texts:
                sample_text += f"\n--- PAGE {pn} ---\n{page_texts[pn][:2000]}\n"

        try:
            diag_prompt = (
                f"I tried to extract personal information (names, addresses, SSNs, "
                f"dates of birth, phone numbers) from this document but got zero "
                f"results on many pages.\n\n"
                f"Here are {len(sample_pages)} sample pages that returned nothing:\n"
                f"{sample_text}\n\n"
                f"Analyze these pages and tell me:\n"
                f"1. Is there personal information on these pages? If yes, what fields?\n"
                f"2. What format is it in? (tabular, key-value, free text, etc.)\n"
                f"3. What labels or markers precede the personal data?\n"
                f"4. If there is NO personal information, say NO_PII.\n\n"
                f"Be specific and concise."
            )
            diag_resp = self.ollama_client.generate(
                prompt=diag_prompt,
                system="You are a document structure analyst. Be specific and concise.",
                use_case="self_correct_diagnosis",
                document_id=self.document_id,
            )
            self._llm_calls_used += 1
        except Exception:
            logger.debug("Self-correct diagnosis failed", exc_info=True)
            return set()

        if not diag_resp or "NO_PII" in diag_resp.upper():
            logger.info(
                "Self-correct: LLM confirms no PII on sample pages — skipping re-extraction"
            )
            return set()

        logger.info(
            "Self-correct: LLM diagnosis for %d unfilled pages: %s",
            len(unfilled_pages), diag_resp[:200],
        )

        # Step 3: Re-extract unfilled pages with adjusted prompt
        # Read all unfilled page texts
        try:
            import fitz
            doc = fitz.open(self.doc_path)
            for pn in unfilled_pages:
                if pn not in page_texts:
                    pg_idx = pn - 1
                    if 0 <= pg_idx < doc.page_count:
                        page_texts[pn] = doc[pg_idx].get_text()
                        doc._forget_page(pg_idx)
            doc.close()
        except Exception:
            return set()

        # Re-extract in batches of 5 with the diagnosis context
        import json as _json
        batch_size = 5
        re_extract_pages = sorted(pn for pn in unfilled_pages if pn in page_texts)

        for batch_start in range(0, len(re_extract_pages), batch_size):
            if self._llm_calls_used >= self.max_llm_total:
                break

            batch = re_extract_pages[batch_start:batch_start + batch_size]
            batch_text = ""
            for pn in batch:
                text = page_texts.get(pn, "")
                if text.strip():
                    batch_text += f"\n--- PAGE {pn} ---\n{text[:3000]}\n"

            if not batch_text.strip():
                continue

            try:
                re_prompt = (
                    f"Extract personal information from these {len(batch)} pages.\n\n"
                    f"CONTEXT from document analysis:\n{diag_resp[:500]}\n\n"
                    f"Based on the above analysis, extract ALL personal records.\n"
                    f"Return a JSON array with one object per person:\n"
                    f'[{{"page": 5, "name": "Full Name", "address": "Street, City ST ZIP", '
                    f'"ssn": "123-45-6789", "dob": "01/15/1980", "phone": "555-123-4567"}}]\n'
                    f"Use null for fields not found. If a page has no personal data, omit it.\n\n"
                    f"{batch_text}"
                )
                resp = self.ollama_client.generate(
                    prompt=re_prompt,
                    system="You are a data extraction assistant. Return only JSON.",
                    use_case="self_correct_extract",
                    document_id=self.document_id,
                )
                self._llm_calls_used += 1

                # Parse response
                cleaned = resp.strip()
                if cleaned.startswith("```"):
                    lines = cleaned.split("\n")
                    lines = [ln for ln in lines if not ln.strip().startswith("```")]
                    cleaned = "\n".join(lines)

                # Try to extract JSON even from partial responses
                try:
                    records = _json.loads(cleaned)
                except _json.JSONDecodeError:
                    import re
                    match = re.search(r'\[.*\]', cleaned, re.DOTALL)
                    if match:
                        try:
                            records = _json.loads(match.group())
                        except _json.JSONDecodeError:
                            continue
                    else:
                        continue

                if not isinstance(records, list):
                    records = [records]

                for rec in records:
                    page_num = rec.get("page")
                    if not page_num or page_num not in gaps_by_page:
                        continue
                    person = rec.get("name", "")
                    if person and len(person) > 2:
                        new_fills.add(page_num)
                        for gap in gaps_by_page[page_num]:
                            if gap.page_num not in already_filled:
                                value = person
                                if gap.expected_field and gap.expected_field != "PERSON":
                                    value = rec.get(
                                        gap.expected_field.lower().replace("us_", ""),
                                        rec.get("ssn", rec.get("address", person))
                                    )
                                if value:
                                    results.append(replace(
                                        gap,
                                        fill_attempted=True,
                                        fill_method="self_correct",
                                        fill_result="filled",
                                        filled_value_masked=_mask_value(
                                            str(value), gap.expected_field or "PERSON"
                                        ),
                                    ))

            except Exception:
                logger.debug("Self-correct batch failed for pages %s", batch, exc_info=True)

        # --- Vision fallback for pages where text extraction completely failed ---
        # If text re-extraction still left many pages unfilled and we have a
        # vision model, render those pages as images and send to the 90B model.
        still_unfilled = [p for p in unfilled_pages if p not in new_fills and p not in already_filled]
        if (
            still_unfilled
            and self.vision_model
            and self.ollama_client
            and self._llm_calls_used < self.max_llm_total
            and len(still_unfilled) >= 3  # worth the overhead
        ):
            vision_fills = self._try_vision_fallback(still_unfilled, gaps_by_page, results)
            new_fills.update(vision_fills)

        logger.info(
            "Self-correct: filled %d additional pages from %d unfilled (%d LLM calls)",
            len(new_fills), len(unfilled_pages), self._llm_calls_used,
        )
        return new_fills

    def _try_vision_fallback(
        self,
        unfilled_pages: list[int],
        gaps_by_page: dict[int, list[ExtractionGap]],
        results: list[ExtractionGap],
    ) -> set[int]:
        """Render unfilled pages as images and send to vision model.

        This is the last resort for pages where text extraction failed
        (OCR-degraded forms, complex tabular layouts).  Only called when
        text-based self-correction also failed.
        """
        new_fills: set[int] = set()

        try:
            from app.pdf.renderer import render_page_to_image
        except ImportError:
            logger.debug("PDF renderer not available for vision fallback")
            return new_fills

        # Cap at 15 pages to avoid excessive vision calls
        pages_to_try = unfilled_pages[:15]
        logger.info(
            "Vision fallback: trying %d pages on %s with model %s",
            len(pages_to_try), self.doc_path, self.vision_model,
        )

        import json as _json

        for page_num in pages_to_try:
            if self._llm_calls_used >= self.max_llm_total:
                break

            try:
                page_idx = page_num - 1  # 1-indexed → 0-indexed
                image_b64 = render_page_to_image(self.doc_path, page_idx, dpi=150)

                prompt = (
                    "Extract ALL personal information from this document page.\n"
                    "Look for: names, Social Security Numbers (SSN/TIN), "
                    "addresses, dates of birth, phone numbers, account numbers.\n\n"
                    "Return a JSON array:\n"
                    f'[{{"page": {page_num}, "name": "Full Name", '
                    f'"ssn": "123-45-6789", "address": "Street, City ST ZIP", '
                    f'"dob": "01/15/1980", "phone": "555-123-4567"}}]\n\n'
                    "If no personal data is visible, return an empty array: []\n"
                    "Return ONLY JSON."
                )

                resp = self.ollama_client.generate_with_images(
                    prompt=prompt,
                    images=[image_b64],
                    use_case="vision_fallback_gap_fill",
                    document_id=self.document_id,
                    model_override=self.vision_model,
                )
                self._llm_calls_used += 1

                # Parse response
                cleaned = resp.strip()
                if cleaned.startswith("```"):
                    lines = cleaned.split("\n")
                    lines = [ln for ln in lines if not ln.strip().startswith("```")]
                    cleaned = "\n".join(lines)

                try:
                    records = _json.loads(cleaned)
                except _json.JSONDecodeError:
                    import re
                    match = re.search(r'\[.*\]', cleaned, re.DOTALL)
                    if match:
                        try:
                            records = _json.loads(match.group())
                        except _json.JSONDecodeError:
                            continue
                    else:
                        continue

                if not isinstance(records, list):
                    records = [records]

                for rec in records:
                    person = rec.get("name", "")
                    if person and len(person) > 2 and page_num in gaps_by_page:
                        new_fills.add(page_num)
                        for gap in gaps_by_page[page_num]:
                            value = person
                            if gap.expected_field and gap.expected_field != "PERSON":
                                value = rec.get(
                                    gap.expected_field.lower().replace("us_", ""),
                                    rec.get("ssn", rec.get("address", person))
                                )
                            if value:
                                results.append(replace(
                                    gap,
                                    fill_attempted=True,
                                    fill_method="vision_fallback",
                                    fill_result="filled",
                                    filled_value_masked=_mask_value(
                                        str(value), gap.expected_field or "PERSON"
                                    ),
                                ))

            except Exception:
                logger.debug("Vision fallback failed for page %d", page_num, exc_info=True)

        logger.info(
            "Vision fallback: filled %d of %d pages (%d LLM calls)",
            len(new_fills), len(pages_to_try), self._llm_calls_used,
        )
        return new_fills

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
            doc._forget_page(page_idx)
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
            doc._forget_page(page_idx)
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
            doc._forget_page(page_idx)
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
    """Mask a value for QA display — show enough for auditor verification.

    The auditor needs to verify gap-fill correctness against the page text
    shown alongside, so masking must be recognizable, not fully opaque.
    """
    if not value:
        return "***"

    if field_type in ("US_SSN", "GOVERNMENT_ID", "IDENTIFICATION_NUMBER", "NI_NUMBER"):
        digits = re.sub(r"[^0-9]", "", value)
        if len(digits) >= 4:
            return f"***-**-{digits[-4:]}"
        return "***"

    if field_type == "PERSON":
        # Show first name + last initial for auditor recognition
        parts = value.split()
        if len(parts) >= 2:
            return f"{parts[0]} {parts[-1][0]}."
        return value[0] + "***" if value else "***"

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
        # Show street number + first word for auditor to match against page
        parts = value.split(",")[0].split() if "," in value else value.split()
        if len(parts) >= 2:
            return f"{parts[0]} {parts[1]}..."
        return value[:8] + "..." if len(value) > 8 else value

    # Default: show enough to identify
    if len(value) > 4:
        return f"{value[:4]}...{value[-2:]}"
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
