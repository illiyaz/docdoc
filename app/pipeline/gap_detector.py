"""Automated extraction gap detection (Step 30e-6).

After extraction completes, scans for:
1. Page-level gaps — pages with zero records on repeating templates
2. Field-level gaps — expected fields missing from extracted records
3. Truncation — records with incomplete data (name without last name, etc.)

Returns a list of ExtractionGap objects for auto-fill or manual review.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, asdict
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ExtractionGap dataclass
# ---------------------------------------------------------------------------


@dataclass
class ExtractionGap:
    """A single extraction gap detected post-extraction."""

    document_id: str
    document_name: str
    page_num: int                           # 1-indexed page number
    gap_type: str                           # "empty_page" | "missing_field" | "truncated" | "stitching"
    severity: str = "medium"                # "high" | "medium" | "low"
    expected_field: Optional[str] = None    # field type that was expected (e.g., "US_SSN")
    actual_fields: list[str] = field(default_factory=list)  # fields that WERE extracted
    context: Optional[str] = None           # human-readable description of the gap

    # Fill status (updated by GapFiller)
    fill_attempted: bool = False
    fill_method: Optional[str] = None       # "coordinate_relaxed" | "llm_template" | "vision" | "presidio"
    fill_result: str = "pending"            # "pending" | "filled" | "unfilled" | "not_applicable"
    filled_value_masked: Optional[str] = None  # masked version of filled value (for display)
    filled_by: str = "system"               # "system" | "manual"
    # Raw fill data — structured values extracted by gap filler. Used by
    # two_phase.py to synthesize new PIIRecords when the gap represents
    # a person who didn't make it through the main extraction paths
    # (BIG_FIXES #A3). Keys follow the LLM prompt output:
    # {"name": "...", "gov_id": "...", "dob": "...", "address": "..."}.
    fill_data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# GapDetector
# ---------------------------------------------------------------------------


class GapDetector:
    """Detect extraction gaps by comparing results against expectations.

    Usage:
        detector = GapDetector()
        gaps = detector.detect(
            records=extracted_records,
            field_inventory=["PERSON", "US_SSN", "LOCATION"],
            total_pages=100,
            onset_page=0,
            document_id="doc-001",
            document_name="file.pdf",
        )
    """

    def detect(
        self,
        records: list[dict],
        field_inventory: list[str],
        total_pages: int,
        onset_page: int,
        document_id: str,
        document_name: str,
        pages_per_instance: int = 1,
        content_pages: set[int] | None = None,
        structural_estimate: int | None = None,
    ) -> list[ExtractionGap]:
        """Run all gap detection checks.

        Args:
            records: List of extracted record dicts, each with at least
                     'page_range' (str) and 'entity_types_found' (list).
            field_inventory: Expected PII field types for this document.
            total_pages: Total pages in the document.
            onset_page: 0-indexed page where PII content starts.
            document_id: Document UUID string.
            document_name: Human-readable file name.
            pages_per_instance: Pages per repeating template instance.

        Returns:
            List of ExtractionGap objects.
        """
        gaps: list[ExtractionGap] = []

        # Build page → records mapping
        page_records = self._build_page_map(records, total_pages)

        # 1. Page-level gap detection (skip blank pages)
        page_gaps = self._detect_page_gaps(
            page_records, total_pages, onset_page,
            document_id, document_name, pages_per_instance,
            content_pages=content_pages,
        )
        gaps.extend(page_gaps)

        # 2. Field-level gap detection
        if field_inventory:
            field_gaps = self._detect_field_gaps(
                page_records, field_inventory, onset_page,
                document_id, document_name,
            )
            gaps.extend(field_gaps)

        # 3. Truncation detection
        trunc_gaps = self._detect_truncation(
            records, document_id, document_name,
        )
        gaps.extend(trunc_gaps)

        # E4 (BIG_FIXES): structural sanity check. If the PDF-structure
        # estimator says this doc has ~N members and we extracted M, log
        # the delta prominently. Auditors + downstream observability
        # get a visible "we may be missing K subjects" signal without
        # parsing the full gap list.
        unique_subjects = len({
            (r.get("raw_name") or r.get("normalized_value") or "").strip().lower()
            for r in records
            if r.get("raw_name") or r.get("normalized_value")
        })
        if structural_estimate and structural_estimate > 0:
            delta = structural_estimate - unique_subjects
            logger.info(
                "[E4] Structural vs extracted for %s: %d subjects extracted, "
                "~%d expected from PDF structure, delta=%+d",
                document_name, unique_subjects, structural_estimate, -delta,
            )
            if delta > 0:
                logger.warning(
                    "[E4] %s may have %d missing subject(s) — "
                    "gap-fill + vision fallback should close this",
                    document_name, delta,
                )

        logger.info(
            "Gap detection for %s: %d gaps found (%d page, %d field, %d truncation)",
            document_name,
            len(gaps),
            len(page_gaps),
            len(field_gaps) if field_inventory else 0,
            len(trunc_gaps),
        )
        return gaps

    # ------------------------------------------------------------------
    # Page-level gaps
    # ------------------------------------------------------------------

    @staticmethod
    def _build_page_map(
        records: list[dict],
        total_pages: int,
    ) -> dict[int, list[dict]]:
        """Map 1-indexed page number → records on that page."""
        page_map: dict[int, list[dict]] = {p: [] for p in range(1, total_pages + 1)}
        for rec in records:
            page_range = rec.get("page_range", "")
            pages = _parse_page_range(page_range)
            for p in pages:
                if p in page_map:
                    page_map[p].append(rec)
        return page_map

    def _detect_page_gaps(
        self,
        page_records: dict[int, list[dict]],
        total_pages: int,
        onset_page: int,
        document_id: str,
        document_name: str,
        pages_per_instance: int,
        content_pages: set[int] | None = None,
    ) -> list[ExtractionGap]:
        """Find content pages after onset that produced zero records.

        Blank pages (no text content) are NOT gaps — they're expected
        in documents with alternating content/blank pages.
        """
        gaps = []
        onset_1 = onset_page + 1  # convert to 1-indexed

        for page_num in range(onset_1, total_pages + 1):
            # Skip blank pages — they have no content to extract
            if content_pages is not None:
                page_0 = page_num - 1  # content_pages uses 0-indexed
                if page_0 not in content_pages:  # blank page (<10 chars)
                    continue

            recs = page_records.get(page_num, [])
            if len(recs) == 0:
                is_boundary = (
                    pages_per_instance > 1
                    and (page_num - onset_1) % pages_per_instance != 0
                )
                if is_boundary:
                    continue

                gaps.append(ExtractionGap(
                    document_id=document_id,
                    document_name=document_name,
                    page_num=page_num,
                    gap_type="empty_page",
                    severity="high",
                    context=f"Page {page_num} has content but produced zero records",
                ))

        return gaps

    # ------------------------------------------------------------------
    # Field-level gaps
    # ------------------------------------------------------------------

    def _detect_field_gaps(
        self,
        page_records: dict[int, list[dict]],
        field_inventory: list[str],
        onset_page: int,
        document_id: str,
        document_name: str,
    ) -> list[ExtractionGap]:
        """Find pages where expected fields are missing from records.

        Only flags a field as missing if it appears on >=30% of pages
        that have records — this prevents false gaps for fields that
        the document simply doesn't contain on most pages.
        """
        gaps = []
        onset_1 = onset_page + 1
        required_fields = set(f.upper() for f in field_inventory)

        # First pass: count how many pages each field appears on
        pages_with_records = 0
        field_page_count: dict[str, int] = {f: 0 for f in required_fields}
        for page_num, recs in page_records.items():
            if page_num < onset_1 or not recs:
                continue
            pages_with_records += 1
            page_types: set[str] = set()
            for rec in recs:
                for et in rec.get("entity_types_found", []):
                    page_types.add(et.upper())
            for ft in required_fields:
                if ft in page_types:
                    field_page_count[ft] += 1

        if pages_with_records == 0:
            return gaps

        # Only check fields that appear on >=30% of pages (they're
        # genuinely part of this document's pattern, not inventory noise).
        # For small docs (<=5 pages), trust the full inventory — not enough
        # data to determine prevalence.
        if pages_with_records <= 5:
            prevalent_fields = required_fields
        else:
            prevalent_fields = {
                ft for ft, count in field_page_count.items()
                if count >= max(1, pages_with_records * 0.3)
            }
            # PERSON is always expected if in inventory
            if "PERSON" in required_fields:
                prevalent_fields.add("PERSON")

        if not prevalent_fields:
            return gaps

        # Second pass: flag missing prevalent fields
        for page_num, recs in page_records.items():
            if page_num < onset_1 or not recs:
                continue

            found_types: set[str] = set()
            for rec in recs:
                for et in rec.get("entity_types_found", []):
                    found_types.add(et.upper())

            for expected in prevalent_fields:
                if expected not in found_types:
                    severity = "high" if expected in (
                        "PERSON", "US_SSN", "GOVERNMENT_ID",
                    ) else "medium"

                    gaps.append(ExtractionGap(
                        document_id=document_id,
                        document_name=document_name,
                        page_num=page_num,
                        gap_type="missing_field",
                        severity=severity,
                        expected_field=expected,
                        actual_fields=sorted(found_types),
                        context=f"Page {page_num}: expected {expected} but not found (extracted: {', '.join(sorted(found_types))})",
                    ))

        return gaps

    # ------------------------------------------------------------------
    # Truncation detection
    # ------------------------------------------------------------------

    def _detect_truncation(
        self,
        records: list[dict],
        document_id: str,
        document_name: str,
    ) -> list[ExtractionGap]:
        """Find records with incomplete/truncated data."""
        gaps = []

        for rec in records:
            page_range = rec.get("page_range", "1")
            pages = _parse_page_range(page_range)
            page_num = pages[0] if pages else 1

            # Check for truncated names (single word)
            raw_name = rec.get("raw_name", "")
            if raw_name and len(raw_name.split()) < 2 and len(raw_name) > 2:
                gaps.append(ExtractionGap(
                    document_id=document_id,
                    document_name=document_name,
                    page_num=page_num,
                    gap_type="truncated",
                    severity="low",
                    expected_field="PERSON",
                    context=f"Page {page_num}: name appears truncated (single word: '***')",
                ))

            # Check for truncated phone (less than 10 digits)
            raw_phone = rec.get("raw_phone", "")
            if raw_phone:
                digits = re.sub(r"[^0-9]", "", raw_phone)
                if 4 < len(digits) < 10:
                    gaps.append(ExtractionGap(
                        document_id=document_id,
                        document_name=document_name,
                        page_num=page_num,
                        gap_type="truncated",
                        severity="low",
                        expected_field="PHONE_NUMBER",
                        context=f"Page {page_num}: phone number appears truncated ({len(digits)} digits)",
                    ))

            # Check for truncated address (too short)
            raw_address = rec.get("raw_address")
            if isinstance(raw_address, dict):
                addr_raw = raw_address.get("raw", "")
                if addr_raw and len(addr_raw) < 10:
                    gaps.append(ExtractionGap(
                        document_id=document_id,
                        document_name=document_name,
                        page_num=page_num,
                        gap_type="truncated",
                        severity="low",
                        expected_field="LOCATION",
                        context=f"Page {page_num}: address appears truncated",
                    ))

        return gaps


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


def _parse_page_range(page_range: str) -> list[int]:
    """Parse a page range string into a list of 1-indexed page numbers.

    Examples:
        "5" → [5]
        "5-8" → [5, 6, 7, 8]
        "12" → [12]
    """
    if not page_range:
        return [1]
    page_range = page_range.strip()
    if "-" in page_range:
        parts = page_range.split("-", 1)
        try:
            start = int(parts[0])
            end = int(parts[1])
            return list(range(start, end + 1))
        except (ValueError, IndexError):
            pass
    try:
        return [int(page_range)]
    except ValueError:
        return [1]
