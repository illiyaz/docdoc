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

    def run(
        self,
        blocks: list[ExtractedBlock],
        document_id: str,
        *,
        db_session=None,
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

        Returns
        -------
        DocumentStructureAnalysis
            Complete analysis with document type, sections, and entity roles.
        """
        # Always run heuristic
        heuristic_result = self._heuristic.analyze(blocks, document_id)

        # Optionally run LLM
        settings = get_settings()
        llm_result = None
        if settings.llm_assist_enabled:
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
