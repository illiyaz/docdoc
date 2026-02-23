"""PII detection task: run the three-layer extraction stack and score entities.

Receives ExtractedBlock lists from readers, runs them through the three
PII extraction layers (app/pii/), and returns DetectionResult objects with
confidence scores and full provenance.

Entities with confidence < 0.75 after Layer 1 are passed to Layer 2.
Layer 3 is applied for tabular blocks where col_header is present.

Input : list[ExtractedBlock]
Output: list[DetectionResult]
"""
from __future__ import annotations

from dataclasses import dataclass

from app.readers.base import ExtractedBlock


@dataclass
class DetectionResult:
    """A candidate PII entity with full provenance metadata."""

    entity_type: str
    value: str             # raw value — never persisted; passed to security layer only
    confidence: float
    extraction_layer: str  # "layer_1_pattern" | "layer_2_context" | "layer_3_positional"
    pattern_used: str | None
    source_block: ExtractedBlock
    start_char: int
    end_char: int
    spans_pages: tuple[int, int] | None = None


class DetectionTask:
    """Prefect task: run all PII extraction layers and return scored results."""

    def run(self, blocks: list[ExtractedBlock]) -> list[DetectionResult]:
        """Detect PII in a list of ExtractedBlocks; return scored candidates."""
        ...
