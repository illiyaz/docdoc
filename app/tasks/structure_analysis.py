"""Document Structure Analysis pipeline task.

Runs AFTER cataloger, BEFORE detection.  Produces a
``DocumentStructureAnalysis`` annotation overlay that the detection
and RRA stages consume to attribute PII to person roles.

Deterministic-first: the ``HeuristicAnalyzer`` always runs.
When ``llm_assist_enabled=True``, the ``LLMStructureAnalyzer`` runs
and its results are merged (heuristic wins on conflict).
"""
from __future__ import annotations

import logging

from app.core.settings import get_settings
from app.readers.base import ExtractedBlock
from app.structure.heuristics import HeuristicAnalyzer
from app.structure.llm_analyzer import LLMStructureAnalyzer, merge_analyses
from app.structure.models import DocumentStructureAnalysis

logger = logging.getLogger(__name__)


class StructureAnalysisTask:
    """Prefect task: analyze document structure before PII detection."""

    def __init__(self) -> None:
        self._heuristic = HeuristicAnalyzer()

    # Minimum segregation confidence to skip redundant LLM structure call
    _SEG_CONFIDENCE_THRESHOLD = 0.80

    def run(
        self,
        blocks: list[ExtractedBlock],
        document_id: str,
        *,
        db_session=None,
        segregation_result: dict | None = None,
    ) -> DocumentStructureAnalysis:
        """Analyze document structure and return annotations.

        Parameters
        ----------
        blocks:
            All ExtractedBlocks from the document, in order.
        document_id:
            UUID string of the document being analyzed.
        db_session:
            Optional SQLAlchemy session for LLM audit logging.
        segregation_result:
            Optional dict from Stage 1.7 segregation (LLM vision).
            When present with high confidence, the LLM structure
            analysis call is skipped (redundant) and segregation's
            document type is merged into the heuristic result instead.
            Saves ~110s per document.

        Returns
        -------
        DocumentStructureAnalysis
            Complete analysis with document type, sections, and entity roles.
        """
        # Always run heuristic (fast, produces sections + block roles)
        heuristic_result = self._heuristic.analyze(blocks, document_id)

        # When segregation already classified with high confidence,
        # skip the LLM structure call — it would produce a redundant
        # document_type classification.  The heuristic still provides
        # sections and per-block entity roles that segregation doesn't.
        seg_confidence = (segregation_result or {}).get("confidence", 0)
        seg_doc_type = (segregation_result or {}).get("document_type", "")
        skip_llm = (
            segregation_result is not None
            and seg_confidence >= self._SEG_CONFIDENCE_THRESHOLD
            and seg_doc_type
            and seg_doc_type != "unknown"
        )

        settings = get_settings()
        llm_result = None

        if skip_llm:
            # Enrich heuristic with segregation's document type
            if (
                heuristic_result.document_type_confidence < seg_confidence
                or heuristic_result.document_type == "unknown"
            ):
                # Map segregation string to closest valid DocumentType
                from app.structure.models import VALID_DOCUMENT_TYPES
                seg_lower = seg_doc_type.lower().replace(" ", "_").replace("-", "_")
                matched = False
                for vdt in VALID_DOCUMENT_TYPES:
                    if vdt == seg_lower or seg_lower in vdt or vdt in seg_lower:
                        heuristic_result.document_type = vdt
                        heuristic_result.document_type_confidence = seg_confidence
                        matched = True
                        break
                if not matched and seg_doc_type != "unknown":
                    # Keep segregation's type as-is if it's reasonable
                    heuristic_result.document_type = "unknown"
                    heuristic_result.document_type_confidence = seg_confidence
                heuristic_result.detected_by = "heuristic+segregation"
            logger.info(
                "Structure analysis: skipping LLM (segregation confidence=%.2f, type=%s)",
                seg_confidence, seg_doc_type,
            )
        elif settings.llm_assist_enabled:
            try:
                llm_analyzer = LLMStructureAnalyzer(db_session=db_session)
                llm_result = llm_analyzer.analyze(blocks, document_id)
            except Exception:
                logger.exception("LLM structure analysis failed; using heuristic only")

        # Merge results
        if llm_result is not None:
            result = merge_analyses(heuristic_result, llm_result)
        else:
            result = heuristic_result

        logger.info(
            "Structure analysis complete: doc_type=%s sections=%d roles=%d detected_by=%s",
            result.document_type,
            len(result.sections),
            len(result.entity_roles),
            result.detected_by,
        )

        return result
