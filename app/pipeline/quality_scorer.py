"""Extraction Quality Scorer (A0 Phase 2).

Scores extraction results from competing methods on sample pages.
The method with the highest score wins and is used for full extraction.

Five scoring dimensions:
  1. COVERAGE — did we find records on the sample pages?
  2. DENSITY MATCH — does yield match expected density?
  3. FIELD COMPLETENESS — how many PII fields per record?
  4. ANCHOR VALIDATION — do extracted values exist in source text?
  5. FORMAT CONSISTENCY — do values match expected PII patterns?
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.pipeline.extraction_selector import DocumentProfile
    from app.rra.entity_resolver import PIIRecord
    from app.readers.base import ExtractedBlock

logger = logging.getLogger(__name__)


@dataclass
class QualityScore:
    """Detailed breakdown of an extraction quality score."""
    total: float = 0.0
    coverage: float = 0.0        # 0-25
    density_match: float = 0.0   # 0-20
    field_completeness: float = 0.0  # 0-20
    anchor_validation: float = 0.0   # 0-25
    format_consistency: float = 0.0  # 0-10
    record_count: int = 0
    pages_with_records: int = 0
    avg_fields: float = 0.0
    anchor_ratio: float = 0.0
    hallucination_count: int = 0


def score_quality(
    records: list[PIIRecord],
    profile: DocumentProfile,
    sample_pages: list[int | str],
    blocks: list[ExtractedBlock],
) -> QualityScore:
    """Score extraction quality.  Higher total = better.  Max 100.

    Parameters
    ----------
    records : list[PIIRecord]
        Records extracted by one method from sample pages.
    profile : DocumentProfile
        Document profile from Phase 1.
    sample_pages : list
        The sample pages that were extracted.
    blocks : list[ExtractedBlock]
        All text blocks (for anchor validation).

    Returns
    -------
    QualityScore
        Detailed score breakdown.
    """
    qs = QualityScore(record_count=len(records))

    if not records:
        return qs

    # Build page text index for anchor validation
    page_text_map: dict[str, str] = {}
    for b in blocks:
        key = str(b.page_or_sheet)
        if key not in page_text_map:
            page_text_map[key] = ""
        page_text_map[key] += " " + b.text

    # ------------------------------------------------------------------
    # 1. COVERAGE: Did we find records on the sample pages? (0-25 points)
    # ------------------------------------------------------------------
    record_pages = set()
    for r in records:
        # PIIRecord uses page_or_sheet or page_range
        pg = getattr(r, "page_or_sheet", None)
        if pg is not None:
            record_pages.add(str(pg))
        pr = getattr(r, "page_range", "")
        if pr:
            # page_range might be "5" or "5-10"
            for part in str(pr).split("-"):
                try:
                    record_pages.add(part.strip())
                except Exception:
                    pass

    sample_page_strs = {str(p) for p in sample_pages}
    pages_with_records = len(record_pages & sample_page_strs) if sample_page_strs else 0
    coverage = pages_with_records / len(sample_pages) if sample_pages else 0
    qs.coverage = coverage * 25
    qs.pages_with_records = pages_with_records

    # ------------------------------------------------------------------
    # 2. DENSITY MATCH: Does yield match expected density? (0-20 points)
    # ------------------------------------------------------------------
    actual_density = len(records) / len(sample_pages) if sample_pages else 0
    expected_density = profile.density.persons_per_page

    if expected_density > 0:
        # Ratio capped at 1.0 (over-extraction doesn't score higher)
        density_ratio = min(actual_density / expected_density, 1.0)
        qs.density_match = density_ratio * 20
    else:
        qs.density_match = 10  # No prior expectation, give half credit

    # ------------------------------------------------------------------
    # 3. FIELD COMPLETENESS: How many PII fields per record? (0-20 points)
    # ------------------------------------------------------------------
    fields_per_record = []
    for r in records:
        count = 0
        if getattr(r, "raw_name", None):
            count += 1
        if getattr(r, "raw_government_id", None):
            count += 1
        if getattr(r, "raw_dob", None):
            count += 1
        if getattr(r, "raw_address", None):
            count += 1
        if getattr(r, "raw_phone", None):
            count += 1
        if getattr(r, "raw_email", None):
            count += 1
        fields_per_record.append(count)

    avg_fields = sum(fields_per_record) / len(fields_per_record) if fields_per_record else 0
    qs.avg_fields = avg_fields
    # 3+ fields = full score
    qs.field_completeness = min(avg_fields / 3, 1.0) * 20

    # ------------------------------------------------------------------
    # 4. ANCHOR VALIDATION: Do extracted values exist in source text? (0-25 points)
    #    This catches LLM hallucinations — if a name doesn't appear in the
    #    page text, it was likely fabricated.
    # ------------------------------------------------------------------
    validated = 0
    hallucination_count = 0
    for r in records:
        name = getattr(r, "raw_name", None)
        if not name:
            continue

        # Look up page text
        pg_key = str(getattr(r, "page_or_sheet", ""))
        if not pg_key:
            pg_key = str(getattr(r, "page_range", "")).split("-")[0]

        page_text = page_text_map.get(pg_key, "")

        if name in page_text:
            validated += 1
        elif name.lower() in page_text.lower():
            validated += 0.8  # Case-insensitive match — still valid
        else:
            # Check partial match (first + last name individually)
            name_parts = name.split()
            if len(name_parts) >= 2 and all(part in page_text for part in name_parts):
                validated += 0.6
            else:
                validated -= 0.5  # Likely hallucinated
                hallucination_count += 1

    anchor_ratio = max(validated / len(records), 0) if records else 0
    qs.anchor_validation = anchor_ratio * 25
    qs.anchor_ratio = anchor_ratio
    qs.hallucination_count = hallucination_count

    # ------------------------------------------------------------------
    # 5. FORMAT CONSISTENCY: Do values match expected PII patterns? (0-10 points)
    # ------------------------------------------------------------------
    valid_formats = 0
    format_checks = 0
    for r in records:
        gov_id = getattr(r, "raw_government_id", None)
        if gov_id:
            format_checks += 1
            if re.match(r"\d{3}-\d{2}-\d{4}", gov_id):
                valid_formats += 1

        dob = getattr(r, "raw_dob", None)
        if dob:
            format_checks += 1
            if re.match(r"\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}", dob):
                valid_formats += 1

        email = getattr(r, "raw_email", None)
        if email:
            format_checks += 1
            if "@" in email and "." in email.split("@")[-1]:
                valid_formats += 1

        phone = getattr(r, "raw_phone", None)
        if phone:
            format_checks += 1
            # Accept various phone formats
            digits = re.sub(r"\D", "", phone)
            if 10 <= len(digits) <= 11:
                valid_formats += 1

    if format_checks > 0:
        qs.format_consistency = min(valid_formats / format_checks, 1.0) * 10
    else:
        qs.format_consistency = 5  # No format-checkable fields, half credit

    # ------------------------------------------------------------------
    # Total
    # ------------------------------------------------------------------
    qs.total = round(
        qs.coverage + qs.density_match + qs.field_completeness +
        qs.anchor_validation + qs.format_consistency,
        1,
    )

    logger.debug(
        "SCORE: %.1f | cov=%.1f dens=%.1f fields=%.1f anchor=%.1f fmt=%.1f | "
        "records=%d pages_hit=%d/%d avg_fields=%.1f halluc=%d",
        qs.total, qs.coverage, qs.density_match, qs.field_completeness,
        qs.anchor_validation, qs.format_consistency,
        len(records), pages_with_records, len(sample_pages),
        avg_fields, hallucination_count,
    )

    return qs
