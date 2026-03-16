"""Post-extraction verification for coordinate extraction (Step 22d).

After bulk coordinate extraction runs on all pages, this module:
1. Counts successes vs failures
2. Categorizes failure reasons (no anchor, no value, pattern mismatch, blank page)
3. Reports a summary for the auditor
4. Decides whether reconciliation quality is acceptable
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.rra.entity_resolver import PIIRecord

logger = logging.getLogger(__name__)


@dataclass
class ExtractionVerification:
    """Result of post-extraction verification."""

    total_pages: int = 0
    successful_pages: int = 0
    failed_pages: int = 0
    blank_pages: int = 0  # pages with no text (separator pages)
    reconciled_pages: int = 0  # pages recovered by LLM reconciliation

    # Per-field success rates
    field_rates: dict[str, float] = field(default_factory=dict)
    # e.g., {"PERSON": 1.0, "US_SSN": 0.47, "LOCATION": 0.82}

    # Quality assessment
    success_rate: float = 0.0  # successful / (total - blank)
    is_acceptable: bool = True  # True if success_rate >= threshold

    # Failed page details (first 20 for debugging)
    failed_page_samples: list[dict] = field(default_factory=list)
    # [{"page": 5, "reason": "anchor_not_found", "anchor": "Client:"}]

    # Summary message for auditor
    summary: str = ""


# Mapping from FieldMapping.field_type to PIIRecord attribute
_FIELD_TO_ATTR: dict[str, str] = {
    "PERSON": "raw_name",
    "LOCATION": "raw_address",
    "US_SSN": "raw_government_id",
    "GOVERNMENT_ID": "raw_government_id",
    "DATE_OF_BIRTH": "raw_dob",
    "EMAIL_ADDRESS": "raw_email",
    "PHONE_NUMBER": "raw_phone",
    "NI_NUMBER": "raw_government_id",
}


class ExtractionVerifier:
    """Verify extraction completeness after coordinate bulk extraction."""

    ACCEPTABLE_RATE = 0.90  # 90% success is acceptable

    def verify(
        self,
        records: list[PIIRecord],
        failed_pages: list[int],
        reconciled_records: list[PIIRecord],
        total_pages: int,
        field_map: list,  # FieldMapping list
    ) -> ExtractionVerification:
        """Verify extraction results and produce summary.

        Parameters
        ----------
        records: Successfully extracted records from coordinate path
        failed_pages: Pages that failed coordinate extraction
        reconciled_records: Records recovered by LLM reconciliation
        total_pages: Total pages in the document
        field_map: The FieldMapping list used for extraction
        """
        result = ExtractionVerification(total_pages=total_pages)

        all_records = records + reconciled_records
        result.successful_pages = len(records)
        result.reconciled_pages = len(reconciled_records)
        result.failed_pages = len(failed_pages) - len(reconciled_records)
        if result.failed_pages < 0:
            result.failed_pages = 0

        # Calculate per-field rates
        field_types: set[str] = set()
        for fm in field_map:
            field_types.add(fm.field_type)

        for ft in field_types:
            attr = _FIELD_TO_ATTR.get(ft.upper())
            if attr and all_records:
                populated = sum(1 for r in all_records if getattr(r, attr, None))
                result.field_rates[ft] = populated / len(all_records)

        # Overall success rate
        if total_pages > 0:
            result.success_rate = len(all_records) / total_pages

        result.is_acceptable = result.success_rate >= self.ACCEPTABLE_RATE

        # Build summary
        result.summary = self._build_summary(result)

        return result

    def _build_summary(self, result: ExtractionVerification) -> str:
        """Build human-readable summary for auditor."""
        total_extracted = result.successful_pages + result.reconciled_pages
        lines = [
            f"Extraction complete: {total_extracted}/{result.total_pages} pages",
            f"  Coordinate extraction: {result.successful_pages} pages",
        ]
        if result.reconciled_pages:
            lines.append(f"  LLM reconciliation: {result.reconciled_pages} pages recovered")
        if result.failed_pages > 0:
            lines.append(f"  Failed: {result.failed_pages} pages")

        if result.field_rates:
            lines.append("  Field rates:")
            for ft, rate in sorted(result.field_rates.items()):
                lines.append(f"    {ft}: {rate:.0%}")

        if result.is_acceptable:
            lines.append(f"  Quality: ACCEPTABLE ({result.success_rate:.0%})")
        else:
            lines.append(f"  Quality: BELOW THRESHOLD ({result.success_rate:.0%} < {self.ACCEPTABLE_RATE:.0%})")

        return "\n".join(lines)
