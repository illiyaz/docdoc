"""Smart QA sampling for extraction review (Step 30e-7).

Selects a curated (not random) set of sample records that build auditor
confidence in extraction quality.  Categories:

1. Largest document group — bulk of the work
2. Gap-filled records — show recovery method
3. Merged records — show merge explanation
4. Cross-type coverage — different document types
5. Edge cases — shortest records, lowest confidence, most fields

Each sample carries enough context for the auditor to verify against
the source page (document_id, page_num, extraction_method).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class QASample:
    """A single curated sample for auditor review."""

    record_id: str
    document_id: str
    document_name: str
    page_num: int
    category: str               # "largest_group" | "gap_filled" | "merged" | "cross_type" | "edge_case"
    category_reason: str        # human-readable reason for selection
    extraction_method: str      # "coordinate" | "llm_template" | "vision" | "presidio" | "manual"

    # Extracted fields (masked for display)
    fields: dict[str, str] = field(default_factory=dict)  # {"PERSON": "J*** S***", "US_SSN": "***-**-6789"}

    # Merge info (if merged record)
    merge_group_id: str | None = None
    merge_confidence: float | None = None
    merge_explanation: str | None = None

    # Gap-fill info (if gap-filled)
    gap_type: str | None = None
    gap_fill_method: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class QASampler:
    """Select curated samples from extraction results.

    Usage:
        sampler = QASampler(max_samples=20)
        samples = sampler.select(
            records=pii_records_as_dicts,
            gaps=filled_gaps,
            merge_groups=merge_group_dicts,
        )
    """

    def __init__(self, max_samples: int = 20):
        self.max_samples = max_samples

    def select(
        self,
        records: list[dict],
        gaps: list[dict] | None = None,
        merge_groups: list[dict] | None = None,
        document_groups: list[dict] | None = None,
    ) -> list[QASample]:
        """Select a curated set of samples across all categories.

        Allocates budget per category, then fills from each.
        """
        gaps = gaps or []
        merge_groups = merge_groups or []
        document_groups = document_groups or []

        samples: list[QASample] = []
        used_record_ids: set[str] = set()

        # Budget allocation (proportional)
        budget = {
            "largest_group": max(3, self.max_samples // 4),
            "gap_filled": max(2, self.max_samples // 5),
            "merged": max(2, self.max_samples // 5),
            "cross_type": max(2, self.max_samples // 5),
            "edge_case": max(2, self.max_samples // 5),
        }

        # 1. Largest group samples
        lg_samples = self._sample_largest_group(records, document_groups, budget["largest_group"])
        for s in lg_samples:
            if s.record_id not in used_record_ids:
                samples.append(s)
                used_record_ids.add(s.record_id)

        # 2. Gap-filled records
        gf_samples = self._sample_gap_filled(gaps, records, budget["gap_filled"])
        for s in gf_samples:
            if s.record_id not in used_record_ids:
                samples.append(s)
                used_record_ids.add(s.record_id)

        # 3. Merged records
        mr_samples = self._sample_merged(merge_groups, records, budget["merged"])
        for s in mr_samples:
            if s.record_id not in used_record_ids:
                samples.append(s)
                used_record_ids.add(s.record_id)

        # 4. Cross-type coverage
        ct_samples = self._sample_cross_type(records, used_record_ids, budget["cross_type"])
        for s in ct_samples:
            if s.record_id not in used_record_ids:
                samples.append(s)
                used_record_ids.add(s.record_id)

        # 5. Edge cases
        ec_samples = self._sample_edge_cases(records, used_record_ids, budget["edge_case"])
        for s in ec_samples:
            if s.record_id not in used_record_ids:
                samples.append(s)
                used_record_ids.add(s.record_id)

        logger.info(
            "QA sampling: selected %d samples (budget %d) — "
            "largest_group=%d, gap_filled=%d, merged=%d, cross_type=%d, edge_case=%d",
            len(samples), self.max_samples,
            sum(1 for s in samples if s.category == "largest_group"),
            sum(1 for s in samples if s.category == "gap_filled"),
            sum(1 for s in samples if s.category == "merged"),
            sum(1 for s in samples if s.category == "cross_type"),
            sum(1 for s in samples if s.category == "edge_case"),
        )

        return samples[:self.max_samples]

    # ------------------------------------------------------------------
    # Category samplers
    # ------------------------------------------------------------------

    def _sample_largest_group(
        self,
        records: list[dict],
        document_groups: list[dict],
        budget: int,
    ) -> list[QASample]:
        """Pick records from the document group with the most records."""
        if not records:
            return []

        # Find largest group by document count
        if document_groups:
            largest = max(document_groups, key=lambda g: g.get("file_count", 0))
            largest_doc_ids = set(largest.get("document_ids", []))
            group_records = [r for r in records if r.get("source_document_id") in largest_doc_ids]
            group_name = largest.get("group_name", "Largest group")
        else:
            # No groups — use all records, group by document
            doc_counts: dict[str, int] = {}
            for r in records:
                did = r.get("source_document_id", "")
                doc_counts[did] = doc_counts.get(did, 0) + 1
            if doc_counts:
                largest_doc = max(doc_counts, key=doc_counts.get)
                group_records = [r for r in records if r.get("source_document_id") == largest_doc]
            else:
                group_records = records
            group_name = "Primary document"

        # Evenly distribute across pages
        samples = []
        if group_records:
            step = max(1, len(group_records) // budget)
            for i in range(0, len(group_records), step):
                if len(samples) >= budget:
                    break
                r = group_records[i]
                samples.append(self._record_to_sample(
                    r, "largest_group",
                    f"From {group_name} ({len(group_records)} records)",
                ))
        return samples

    def _sample_gap_filled(
        self,
        gaps: list[dict],
        records: list[dict],
        budget: int,
    ) -> list[QASample]:
        """Pick records associated with auto-filled gaps."""
        filled_gaps = [g for g in gaps if g.get("fill_result") == "filled"]
        if not filled_gaps:
            return []

        # Sort by severity (high first)
        filled_gaps.sort(key=lambda g: {"high": 0, "medium": 1, "low": 2}.get(g.get("severity", ""), 9))

        samples = []
        for gap_idx, gap in enumerate(filled_gaps[:budget]):
            # Find a record on this page
            page_num = gap.get("page_num", 0)
            doc_id = gap.get("document_id", "")
            matching = [
                r for r in records
                if r.get("source_document_id") == doc_id
                and str(r.get("page_range", "")) == str(page_num)
            ]
            if matching:
                s = self._record_to_sample(
                    matching[0], "gap_filled",
                    f"Gap-filled via {gap.get('fill_method', 'unknown')}: {gap.get('expected_field', '?')}",
                )
                # Use unique gap-based ID to avoid dedup conflicts with other categories
                s.record_id = f"gapfill-{doc_id}-p{page_num}-{gap_idx}"
                s.gap_type = gap.get("gap_type")
                s.gap_fill_method = gap.get("fill_method")
                samples.append(s)
            else:
                # Create a placeholder sample from gap data
                samples.append(QASample(
                    record_id=f"gap-{doc_id}-p{page_num}",
                    document_id=doc_id,
                    document_name=gap.get("document_name", ""),
                    page_num=page_num,
                    category="gap_filled",
                    category_reason=f"Gap-filled via {gap.get('fill_method', 'unknown')}",
                    extraction_method=gap.get("fill_method", "unknown"),
                    fields={"expected": gap.get("expected_field", ""), "masked_value": gap.get("filled_value_masked", "")},
                    gap_type=gap.get("gap_type"),
                    gap_fill_method=gap.get("fill_method"),
                ))

        return samples

    def _sample_merged(
        self,
        merge_groups: list[dict],
        records: list[dict],
        budget: int,
    ) -> list[QASample]:
        """Pick records that were merged by RRA."""
        if not merge_groups:
            return []

        # Sort by merge group size (largest first — most impactful)
        merge_groups.sort(key=lambda g: g.get("member_count", 0), reverse=True)

        samples = []
        for group in merge_groups[:budget]:
            member_ids = group.get("member_ids", [])
            # Find a record from this group
            matching = [r for r in records if r.get("record_id") in set(member_ids)]
            if matching:
                s = self._record_to_sample(
                    matching[0], "merged",
                    f"Merged from {group.get('member_count', len(member_ids))} records",
                )
                s.merge_group_id = group.get("group_id")
                s.merge_confidence = group.get("confidence")
                s.merge_explanation = group.get("explanation")
                samples.append(s)

        return samples

    def _sample_cross_type(
        self,
        records: list[dict],
        used_ids: set[str],
        budget: int,
    ) -> list[QASample]:
        """Pick one record per unique document type."""
        type_seen: set[str] = set()
        samples = []
        for r in records:
            if len(samples) >= budget:
                break
            rid = r.get("record_id", "")
            if rid in used_ids:
                continue
            doc_type = r.get("document_type", r.get("source_document_name", "unknown"))
            # Use document extension as proxy for type
            ext = doc_type.rsplit(".", 1)[-1].lower() if "." in doc_type else doc_type
            if ext not in type_seen:
                type_seen.add(ext)
                samples.append(self._record_to_sample(
                    r, "cross_type",
                    f"Cross-type coverage: {ext} document",
                ))
        return samples

    def _sample_edge_cases(
        self,
        records: list[dict],
        used_ids: set[str],
        budget: int,
    ) -> list[QASample]:
        """Pick edge-case records: fewest fields, most fields, shortest name."""
        available = [r for r in records if r.get("record_id", "") not in used_ids]
        if not available:
            return []

        samples = []

        # Fewest fields (potential incomplete extraction)
        def field_count(r: dict) -> int:
            count = 0
            for k in ("raw_name", "raw_phone", "raw_email", "raw_dob", "raw_government_id", "raw_address"):
                if r.get(k):
                    count += 1
            return count

        sorted_by_fields = sorted(available, key=field_count)
        if sorted_by_fields:
            r = sorted_by_fields[0]
            samples.append(self._record_to_sample(
                r, "edge_case",
                f"Fewest fields extracted ({field_count(r)} fields)",
            ))

        # Most fields (potential over-extraction or best case)
        if len(sorted_by_fields) > 1:
            r = sorted_by_fields[-1]
            if r.get("record_id", "") not in {s.record_id for s in samples}:
                samples.append(self._record_to_sample(
                    r, "edge_case",
                    f"Most fields extracted ({field_count(r)} fields)",
                ))

        # Shortest name (potential truncation)
        named = [r for r in available if r.get("raw_name")]
        if named:
            shortest = min(named, key=lambda r: len(r.get("raw_name", "")))
            if shortest.get("record_id", "") not in {s.record_id for s in samples}:
                samples.append(self._record_to_sample(
                    shortest, "edge_case",
                    f"Shortest name: {len(shortest.get('raw_name', ''))} chars",
                ))

        return samples[:budget]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _record_to_sample(record: dict, category: str, reason: str) -> QASample:
        """Convert a record dict to a QASample."""
        from app.pipeline.gap_filler import _mask_value

        fields: dict[str, str] = {}
        if record.get("raw_name"):
            fields["PERSON"] = _mask_value(record["raw_name"], "PERSON")
        if record.get("raw_government_id"):
            fields["GOVERNMENT_ID"] = _mask_value(record["raw_government_id"], "GOVERNMENT_ID")
        if record.get("raw_phone"):
            fields["PHONE_NUMBER"] = _mask_value(record["raw_phone"], "PHONE_NUMBER")
        if record.get("raw_email"):
            fields["EMAIL_ADDRESS"] = _mask_value(record["raw_email"], "EMAIL_ADDRESS")
        if record.get("raw_dob"):
            fields["DATE_OF_BIRTH"] = _mask_value(record["raw_dob"], "DATE_OF_BIRTH")
        if record.get("raw_address"):
            addr = record["raw_address"]
            if isinstance(addr, dict):
                addr = addr.get("raw", "")
            if addr:
                fields["LOCATION"] = _mask_value(str(addr), "LOCATION")

        page_str = str(record.get("page_range", record.get("page_or_sheet", "1")))
        try:
            page_num = int(page_str.split("-")[0])
        except (ValueError, IndexError):
            page_num = 1

        return QASample(
            record_id=record.get("record_id", ""),
            document_id=record.get("source_document_id", ""),
            document_name=record.get("source_document_name", record.get("document_name", "")),
            page_num=page_num,
            category=category,
            category_reason=reason,
            extraction_method=record.get("extraction_method", "unknown"),
            fields=fields,
        )
