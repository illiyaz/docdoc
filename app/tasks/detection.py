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

from dataclasses import dataclass, field

from app.readers.base import ExtractedBlock
from app.structure.models import DocumentStructureAnalysis


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
    entity_role: str | None = None
    entity_role_confidence: float | None = None


def annotate_results_with_structure(
    results: list[DetectionResult],
    blocks: list[ExtractedBlock],
    structure: DocumentStructureAnalysis | None,
) -> list[DetectionResult]:
    """Annotate detection results with entity roles from structure analysis.

    Mutates results in-place and returns them.  If structure is None,
    results are returned unchanged.
    """
    if structure is None:
        return results

    # Build block identity → index mapping
    block_id_map: dict[int, int] = {id(b): i for i, b in enumerate(blocks)}

    for result in results:
        block_idx = block_id_map.get(id(result.source_block))
        if block_idx is not None:
            result.entity_role = structure.get_role_for_block(block_idx)
            result.entity_role_confidence = structure.get_role_confidence_for_block(block_idx)

    return results


class DetectionTask:
    """Prefect task: run all PII extraction layers and return scored results."""

    def run(
        self,
        blocks: list[ExtractedBlock],
        *,
        structure: DocumentStructureAnalysis | None = None,
    ) -> list[DetectionResult]:
        """Detect PII in a list of ExtractedBlocks; return scored candidates.

        Parameters
        ----------
        blocks:
            All ExtractedBlocks from the document.
        structure:
            Optional structure analysis result.  When provided, each
            DetectionResult is annotated with ``entity_role`` and
            ``entity_role_confidence`` from the analysis.
        """
        ...
