"""Quality assurance task: validate extraction completeness and correctness.

Applies a deterministic rule set to persisted extraction records for a
document run. Flags missing required fields, low-confidence extractions
below a configurable threshold, and any schema invariant violations.

Output is a QAReport per document. Issues are persisted and surfaced in
the human review queue by ascending confidence score.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class QAReport:
    """Summary of validation results for one document run."""

    document_id: str
    run_id: str
    total_extractions: int
    flagged_count: int
    issues: list[dict] = field(default_factory=list)


class QATask:
    """Prefect task: validate extractions and produce a QA report."""

    def run(self, document_id: str, run_id: str) -> QAReport:
        """Load extractions for the run, apply all validation rules, return report."""
        ...
