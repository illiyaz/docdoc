"""Document grouping and sampling for segregation review (Step 30e-2).

After LLM segregation classifies each file, this module:
1. Groups documents by document_type + field_inventory similarity.
2. Separates PII from non-PII documents.
3. Picks representative samples per group (3-5 files).

Output: list[DocumentGroup] for the Segregation Review UI.
"""
from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from typing import Optional

from app.pipeline.segregation import SegregationResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Number of sample files to pick per group
DEFAULT_SAMPLE_SIZE = 5
MIN_SAMPLE_SIZE = 1
MAX_SAMPLE_SIZE = 10

# Minimum field overlap ratio to consider two results "same group"
# (Jaccard similarity of field_inventory sets)
FIELD_SIMILARITY_THRESHOLD = 0.6


# ---------------------------------------------------------------------------
# DocumentGroup dataclass
# ---------------------------------------------------------------------------


@dataclass
class DocumentGroup:
    """A group of similar documents from segregation."""
    group_id: str
    group_name: str                          # human-readable name
    document_type: str                       # from segregation (e.g., "medical_form")
    is_pii: bool                             # True if group contains PII docs

    # Members
    file_paths: list[str] = field(default_factory=list)
    file_count: int = 0

    # Representative samples (file paths)
    sample_file_paths: list[str] = field(default_factory=list)

    # Aggregated field inventory (union of all members' fields)
    field_inventory: list[str] = field(default_factory=list)

    # Role attribution summary (most common role per field type)
    role_summary: dict[str, str] = field(default_factory=dict)

    # Primary subject type (most common across members)
    primary_subject_type: Optional[str] = None

    # Confidence (average across members)
    confidence_avg: float = 0.0
    confidence_min: float = 0.0

    # Review status
    status: str = "pending_review"           # pending_review | approved | rejected

    # Issuing entities seen
    issuing_entities: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Grouping logic
# ---------------------------------------------------------------------------


def group_documents(
    results: list[SegregationResult],
    sample_size: int = DEFAULT_SAMPLE_SIZE,
) -> list[DocumentGroup]:
    """Group segregation results into DocumentGroups.

    Strategy:
    1. Separate PII from non-PII results.
    2. Within PII results, group by document_type.
    3. Within same document_type, split if field inventories are very different.
    4. Non-PII results go into a single "Non-PII" group.
    5. Pick representative samples per group.

    Returns list of DocumentGroup sorted by: PII groups first (largest first),
    then non-PII group last.
    """
    sample_size = max(MIN_SAMPLE_SIZE, min(sample_size, MAX_SAMPLE_SIZE))

    pii_results = [r for r in results if r.pii_detected]
    non_pii_results = [r for r in results if not r.pii_detected]

    groups: list[DocumentGroup] = []

    # --- Group PII results by document_type, then refine by field similarity ---
    if pii_results:
        type_buckets: dict[str, list[SegregationResult]] = defaultdict(list)
        for r in pii_results:
            type_buckets[r.document_type].append(r)

        for doc_type, bucket in type_buckets.items():
            # Try to split bucket further by field similarity
            sub_groups = _split_by_field_similarity(bucket)

            for sub_idx, sub_bucket in enumerate(sub_groups):
                suffix = f" (variant {sub_idx + 1})" if len(sub_groups) > 1 else ""
                group = _build_group(
                    results=sub_bucket,
                    doc_type=doc_type,
                    is_pii=True,
                    name_suffix=suffix,
                    sample_size=sample_size,
                )
                groups.append(group)

    # --- Non-PII group ---
    if non_pii_results:
        non_pii_group = _build_group(
            results=non_pii_results,
            doc_type="non_pii",
            is_pii=False,
            name_suffix="",
            sample_size=sample_size,
        )
        non_pii_group.group_name = "Non-PII Documents"
        groups.append(non_pii_group)

    # Sort: PII groups largest first, non-PII last
    groups.sort(key=lambda g: (not g.is_pii, -g.file_count))

    return groups


def _build_group(
    results: list[SegregationResult],
    doc_type: str,
    is_pii: bool,
    name_suffix: str,
    sample_size: int,
) -> DocumentGroup:
    """Build a DocumentGroup from a list of SegregationResults."""
    file_paths = [r.file_path for r in results]

    # Aggregate field inventory (union)
    all_fields: set[str] = set()
    for r in results:
        all_fields.update(r.field_inventory)

    # Role summary: for each field type, most common role across results
    role_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in results:
        for f in r.fields:
            role_counts[f.type][f.role] += 1
    role_summary = {}
    for ftype, roles in role_counts.items():
        role_summary[ftype] = max(roles, key=roles.get)

    # Primary subject type (most common)
    subject_counts: dict[str, int] = defaultdict(int)
    for r in results:
        if r.primary_subject_type:
            subject_counts[r.primary_subject_type] += 1
    primary_subject = (
        max(subject_counts, key=subject_counts.get) if subject_counts else None
    )

    # Confidence stats
    confidences = [r.confidence for r in results if r.confidence > 0]
    conf_avg = sum(confidences) / len(confidences) if confidences else 0.0
    conf_min = min(confidences) if confidences else 0.0

    # Issuing entities (unique)
    entities = sorted(set(
        r.issuing_entity for r in results if r.issuing_entity
    ))

    # Pick samples
    samples = _select_samples(results, sample_size)

    # Generate human-readable name
    name = _generate_group_name(doc_type, primary_subject, len(results))
    name += name_suffix

    return DocumentGroup(
        group_id=str(uuid.uuid4()),
        group_name=name,
        document_type=doc_type,
        is_pii=is_pii,
        file_paths=file_paths,
        file_count=len(file_paths),
        sample_file_paths=[r.file_path for r in samples],
        field_inventory=sorted(all_fields),
        role_summary=role_summary,
        primary_subject_type=primary_subject,
        confidence_avg=round(conf_avg, 3),
        confidence_min=round(conf_min, 3),
        issuing_entities=entities,
    )


def _split_by_field_similarity(
    results: list[SegregationResult],
) -> list[list[SegregationResult]]:
    """Split a bucket of same-type results into sub-groups by field similarity.

    Uses simple greedy clustering: assign each result to the first cluster
    whose field inventory has Jaccard similarity >= threshold.
    If no match, create a new cluster.
    """
    if len(results) <= 1:
        return [results]

    clusters: list[list[SegregationResult]] = []
    cluster_fields: list[set[str]] = []

    for r in results:
        r_fields = set(r.field_inventory)
        placed = False

        for i, cf in enumerate(cluster_fields):
            sim = _jaccard(r_fields, cf)
            if sim >= FIELD_SIMILARITY_THRESHOLD:
                clusters[i].append(r)
                # Update cluster field set (union)
                cluster_fields[i] = cf | r_fields
                placed = True
                break

        if not placed:
            clusters.append([r])
            cluster_fields.append(r_fields)

    return clusters


def _jaccard(set_a: set, set_b: set) -> float:
    """Jaccard similarity between two sets."""
    if not set_a and not set_b:
        return 1.0  # both empty = identical
    union = set_a | set_b
    if not union:
        return 0.0
    return len(set_a & set_b) / len(union)


# ---------------------------------------------------------------------------
# Sample selection
# ---------------------------------------------------------------------------


def _select_samples(
    results: list[SegregationResult],
    sample_size: int,
) -> list[SegregationResult]:
    """Select representative samples from a group.

    Strategy: prioritize diversity over randomness:
    1. Include the highest-confidence result (best representative).
    2. Include the lowest-confidence result (edge case, needs auditor review).
    3. If multiple issuing entities, include one from each.
    4. Fill remaining slots from the middle of the confidence range.
    """
    if len(results) <= sample_size:
        return results

    selected: list[SegregationResult] = []
    selected_paths: set[str] = set()

    def _add(r: SegregationResult):
        if r.file_path not in selected_paths:
            selected.append(r)
            selected_paths.add(r.file_path)

    # Sort by confidence
    sorted_results = sorted(results, key=lambda r: r.confidence, reverse=True)

    # 1. Highest confidence
    _add(sorted_results[0])

    # 2. Lowest confidence
    if len(sorted_results) > 1:
        _add(sorted_results[-1])

    # 3. One per issuing entity (if diverse)
    entity_reps: dict[str, SegregationResult] = {}
    for r in sorted_results:
        entity = r.issuing_entity or "unknown"
        if entity not in entity_reps:
            entity_reps[entity] = r
    for r in entity_reps.values():
        if len(selected) >= sample_size:
            break
        _add(r)

    # 4. Fill from middle
    if len(selected) < sample_size:
        mid_start = len(sorted_results) // 4
        for r in sorted_results[mid_start:]:
            if len(selected) >= sample_size:
                break
            _add(r)

    return selected


# ---------------------------------------------------------------------------
# Group naming
# ---------------------------------------------------------------------------

_TYPE_NAMES = {
    "medical_form": "Medical Forms",
    "billing_statement": "Billing Statements",
    "loan_application": "Loan Applications",
    "tax_form": "Tax Forms",
    "pay_stub": "Pay Stubs",
    "insurance_claim": "Insurance Claims",
    "school_record": "School Records",
    "shipping_document": "Shipping Documents",
    "invoice": "Invoices",
    "correspondence": "Correspondence",
    "legal_filing": "Legal Filings",
    "report": "Reports",
    "spreadsheet_export": "Spreadsheet Exports",
    "non_pii": "Non-PII Documents",
}

_SUBJECT_LABELS = {
    "patient": "Patient",
    "student": "Student",
    "employee": "Employee",
    "account_holder": "Account Holder",
    "applicant": "Applicant",
    "claimant": "Claimant",
    "taxpayer": "Taxpayer",
}


def _generate_group_name(
    doc_type: str,
    primary_subject_type: Optional[str],
    count: int,
) -> str:
    """Generate a human-readable group name."""
    type_label = _TYPE_NAMES.get(doc_type, doc_type.replace("_", " ").title())
    subject_label = _SUBJECT_LABELS.get(primary_subject_type or "", "")

    if subject_label:
        return f"{type_label} ({subject_label})"
    return type_label
