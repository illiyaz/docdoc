"""PII extraction task: apply storage policy and persist extraction records.

Receives DetectionResult objects from the detection task, applies the
configured storage policy (STRICT or INVESTIGATION) via
ExtractionRepository.create_with_policy(), and records full audit metadata.

Critical rule: ExtractionRepository.create() must never be called directly.
Always use create_with_policy() to ensure hashing/encryption is applied and
the raw value is never persisted.
"""
from __future__ import annotations

from app.tasks.detection import DetectionResult


class ExtractionTask:
    """Prefect task: persist detected PII under the active storage policy."""

    def run(
        self,
        results: list[DetectionResult],
        document_id: str,
        run_id: str,
    ) -> list[str]:
        """Persist each result via create_with_policy(); return extraction UUIDs."""
        ...
