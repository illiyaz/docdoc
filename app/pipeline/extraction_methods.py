"""Extraction Method Wrappers (A0 Phase 2).

Each method wraps an existing extraction implementation and exposes
a uniform interface for the competition loop.

Methods:
  - CoordinateMethod: anchor-relative field maps (30ms/page)
  - PresidioSmartGroup: Presidio NER + smart grouping (200ms/page)
  - LLMTemplateMethod: LLM template extraction (5s/page)
  - LLMTableMethod: LLM table extraction (5s/page)
  - VisionMethod: Vision model extraction (20s/page)
  - OCRPresidioMethod: OCR + Presidio pipeline (2s/page)
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.pipeline.extraction_selector import DocumentProfile
    from app.readers.base import ExtractedBlock
    from app.rra.entity_resolver import PIIRecord
    from app.structure.document_schema import DocumentSchema

logger = logging.getLogger(__name__)


@dataclass
class MethodResult:
    """Result of running an extraction method on sample pages."""
    records: list[PIIRecord]
    speed_ms_per_page: float = 0.0
    error: str | None = None
    method_name: str = ""


class ExtractionMethod(ABC):
    """Base class for extraction methods."""

    name: str = "base"
    estimated_speed_ms: float = 1000  # estimated ms per page
    supports_learn_mode: bool = False

    @abstractmethod
    def extract(
        self,
        pages: list[int | str],
        blocks: list[ExtractedBlock],
        profile: DocumentProfile,
        schema: DocumentSchema | None = None,
        **kwargs: Any,
    ) -> list[PIIRecord]:
        """Extract PII records from the given pages."""
        ...

    def is_applicable(self, profile: DocumentProfile) -> bool:
        """Return True if this method can work on the given document."""
        return True

    def run_on_sample(
        self,
        pages: list[int | str],
        blocks: list[ExtractedBlock],
        profile: DocumentProfile,
        schema: DocumentSchema | None = None,
        **kwargs: Any,
    ) -> MethodResult:
        """Run extraction on sample pages and return timed result."""
        _start = time.time()
        try:
            records = self.extract(pages, blocks, profile, schema, **kwargs)
            _elapsed = (time.time() - _start) * 1000
            speed = _elapsed / len(pages) if pages else 0
            logger.info(
                "METHOD %s: %d records from %d pages in %.0fms (%.0fms/page)",
                self.name, len(records), len(pages), _elapsed, speed,
            )
            return MethodResult(
                records=records,
                speed_ms_per_page=speed,
                method_name=self.name,
            )
        except Exception as e:
            _elapsed = (time.time() - _start) * 1000
            logger.warning(
                "METHOD %s: FAILED on sample (%s) in %.0fms",
                self.name, type(e).__name__, _elapsed,
            )
            return MethodResult(
                records=[],
                speed_ms_per_page=_elapsed / max(len(pages), 1),
                error=str(e),
                method_name=self.name,
            )


# ============================================================
# Concrete Methods
# ============================================================

class CoordinateMethod(ExtractionMethod):
    """Field-map based coordinate extraction.  Fastest path (~30ms/page)."""

    name = "coordinate"
    estimated_speed_ms = 30
    supports_learn_mode = False

    def __init__(self, field_map: list | None = None, doc_path: str = ""):
        self._field_map = field_map
        self._doc_path = doc_path

    def is_applicable(self, profile: DocumentProfile) -> bool:
        # Needs a text layer and a field map
        return profile.has_text_layer and self._field_map is not None and len(self._field_map) > 0

    def extract(
        self,
        pages: list[int | str],
        blocks: list[ExtractedBlock],
        profile: DocumentProfile,
        schema: DocumentSchema | None = None,
        **kwargs: Any,
    ) -> list[PIIRecord]:
        from app.pipeline.coordinate_extractor import CoordinateExtractor

        extractor = CoordinateExtractor(self._field_map)
        page_set = set(pages)
        # Filter blocks to sample pages
        sample_blocks = [b for b in blocks if b.page_or_sheet in page_set]
        return extractor.extract_all(sample_blocks, self._doc_path)


class PresidioSmartGroupMethod(ExtractionMethod):
    """Presidio NER + smart spatial grouping.  Good general-purpose path."""

    name = "presidio_smart"
    estimated_speed_ms = 200

    def __init__(self, engine: Any = None, target_entities: list[str] | None = None):
        self._engine = engine
        self._target_entities = target_entities

    def is_applicable(self, profile: DocumentProfile) -> bool:
        return profile.has_text_layer and self._engine is not None

    def extract(
        self,
        pages: list[int | str],
        blocks: list[ExtractedBlock],
        profile: DocumentProfile,
        schema: DocumentSchema | None = None,
        **kwargs: Any,
    ) -> list[PIIRecord]:
        from app.pipeline.smart_grouping import smart_group_detections
        from app.pipeline.record_mapper import detections_to_pii_records

        page_set = set(pages)
        sample_blocks = [b for b in blocks if b.page_or_sheet in page_set]

        # Run Presidio on each block
        all_detections = []
        for block in sample_blocks:
            try:
                detections = self._engine.analyze(
                    block.text,
                    entities=self._target_entities,
                )
                for d in detections:
                    d.page_or_sheet = block.page_or_sheet
                    d.source_path = block.source_path
                all_detections.extend(detections)
            except Exception:
                continue

        if not all_detections:
            return []

        # Group detections into records
        groups = smart_group_detections(all_detections, sample_blocks)
        return detections_to_pii_records(groups, source_document_id="")


class LLMTemplateMethod(ExtractionMethod):
    """LLM template extraction — sends page text to LLM for structured extraction."""

    name = "llm_template"
    estimated_speed_ms = 5000

    def __init__(self, llm_client: Any = None):
        self._llm_client = llm_client

    def is_applicable(self, profile: DocumentProfile) -> bool:
        return profile.has_text_layer and self._llm_client is not None

    def extract(
        self,
        pages: list[int | str],
        blocks: list[ExtractedBlock],
        profile: DocumentProfile,
        schema: DocumentSchema | None = None,
        **kwargs: Any,
    ) -> list[PIIRecord]:
        from app.structure.llm_template_extractor import LLMTemplateExtractor

        page_set = set(pages)
        sample_blocks = [b for b in blocks if b.page_or_sheet in page_set]

        extractor = LLMTemplateExtractor(self._llm_client)
        return extractor.extract_from_blocks(sample_blocks, schema=schema)


class LLMTableMethod(ExtractionMethod):
    """LLM table extraction — for tabular PII layouts."""

    name = "llm_table"
    estimated_speed_ms = 5000

    def __init__(self, llm_client: Any = None):
        self._llm_client = llm_client

    def is_applicable(self, profile: DocumentProfile) -> bool:
        return profile.has_text_layer and self._llm_client is not None

    def extract(
        self,
        pages: list[int | str],
        blocks: list[ExtractedBlock],
        profile: DocumentProfile,
        schema: DocumentSchema | None = None,
        **kwargs: Any,
    ) -> list[PIIRecord]:
        # Uses existing LLM table extraction from two_phase
        # This will be wired when integrated
        logger.debug("LLMTableMethod.extract called on %d pages", len(pages))
        return []  # Placeholder — will delegate to existing code


class VisionMethod(ExtractionMethod):
    """Vision model extraction — for scanned/image PDFs."""

    name = "vision"
    estimated_speed_ms = 20000

    def __init__(self, llm_client: Any = None, doc_path: str = ""):
        self._llm_client = llm_client
        self._doc_path = doc_path

    def is_applicable(self, profile: DocumentProfile) -> bool:
        return self._llm_client is not None and (
            profile.text_ratio < 0.8 or not profile.has_text_layer
        )

    def extract(
        self,
        pages: list[int | str],
        blocks: list[ExtractedBlock],
        profile: DocumentProfile,
        schema: DocumentSchema | None = None,
        **kwargs: Any,
    ) -> list[PIIRecord]:
        from app.structure.vision_extractor import VisionDocumentExtractor

        extractor = VisionDocumentExtractor(self._llm_client)
        int_pages = [p for p in pages if isinstance(p, int)]
        return extractor.extract_pages(self._doc_path, int_pages)


class OCRPresidioMethod(ExtractionMethod):
    """OCR + Presidio — for scanned docs where vision is too slow."""

    name = "ocr_presidio"
    estimated_speed_ms = 2000

    def __init__(self, engine: Any = None, target_entities: list[str] | None = None):
        self._engine = engine
        self._target_entities = target_entities

    def is_applicable(self, profile: DocumentProfile) -> bool:
        return self._engine is not None and profile.text_ratio < 0.5

    def extract(
        self,
        pages: list[int | str],
        blocks: list[ExtractedBlock],
        profile: DocumentProfile,
        schema: DocumentSchema | None = None,
        **kwargs: Any,
    ) -> list[PIIRecord]:
        # OCR blocks are already in blocks list if OCR was run
        # Just run Presidio on them
        presidio = PresidioSmartGroupMethod(self._engine, self._target_entities)
        return presidio.extract(pages, blocks, profile, schema, **kwargs)


# ============================================================
# Method Competition
# ============================================================

def get_applicable_methods(
    profile: DocumentProfile,
    field_map: list | None = None,
    engine: Any = None,
    llm_client: Any = None,
    target_entities: list[str] | None = None,
) -> list[ExtractionMethod]:
    """Return all extraction methods applicable to this document profile."""
    candidates: list[ExtractionMethod] = []

    # Always try coordinate if field map exists
    coord = CoordinateMethod(field_map=field_map, doc_path=profile.source_path)
    if coord.is_applicable(profile):
        candidates.append(coord)

    # Presidio smart group — works on any text doc
    presidio = PresidioSmartGroupMethod(engine=engine, target_entities=target_entities)
    if presidio.is_applicable(profile):
        candidates.append(presidio)

    # LLM methods — for docs with text, when LLM is available
    if profile.text_ratio > 0.5:
        llm_tmpl = LLMTemplateMethod(llm_client=llm_client)
        if llm_tmpl.is_applicable(profile):
            candidates.append(llm_tmpl)

        llm_tbl = LLMTableMethod(llm_client=llm_client)
        if llm_tbl.is_applicable(profile):
            candidates.append(llm_tbl)

    # Vision methods — for scanned/mixed docs
    if profile.text_ratio < 0.8:
        vision = VisionMethod(llm_client=llm_client, doc_path=profile.source_path)
        if vision.is_applicable(profile):
            candidates.append(vision)

        ocr = OCRPresidioMethod(engine=engine, target_entities=target_entities)
        if ocr.is_applicable(profile):
            candidates.append(ocr)

    return candidates


def compete_methods(
    profile: DocumentProfile,
    blocks: list[ExtractedBlock],
    sample_pages: list[int | str],
    schema: DocumentSchema | None = None,
    field_map: list | None = None,
    engine: Any = None,
    llm_client: Any = None,
    target_entities: list[str] | None = None,
) -> tuple[ExtractionMethod | None, dict[str, MethodResult]]:
    """Run all applicable methods on sample pages and return the winner.

    Returns
    -------
    tuple
        (winning_method, results_dict) where results_dict maps method name
        to MethodResult.  Returns (None, {}) if no methods are applicable.
    """
    from app.pipeline.quality_scorer import score_quality

    candidates = get_applicable_methods(
        profile, field_map, engine, llm_client, target_entities,
    )

    if not candidates:
        logger.warning("No extraction methods applicable for %s", profile.file_name)
        return None, {}

    logger.info(
        "COMPETE: %s | %d methods: %s | %d sample pages",
        profile.file_name,
        len(candidates),
        ", ".join(m.name for m in candidates),
        len(sample_pages),
    )

    results: dict[str, MethodResult] = {}
    scores: dict[str, float] = {}

    for method in candidates:
        result = method.run_on_sample(pages=sample_pages, blocks=blocks, profile=profile, schema=schema)
        results[method.name] = result

        if result.error:
            scores[method.name] = 0.0
        else:
            qs = score_quality(result.records, profile, sample_pages, blocks)
            scores[method.name] = qs.total
            result.records  # Keep records for potential reuse

        logger.info(
            "  %s: score=%.1f | records=%d | speed=%.0fms/page%s",
            method.name, scores.get(method.name, 0),
            len(result.records), result.speed_ms_per_page,
            f" | ERROR: {result.error}" if result.error else "",
        )

    if not scores or max(scores.values()) == 0:
        logger.warning("All methods scored 0 for %s — flagging for manual review", profile.file_name)
        return None, results

    # Pick winner: highest score, speed as tiebreaker
    winner_name = max(scores, key=lambda name: (
        scores[name],
        -results[name].speed_ms_per_page,  # faster is better as tiebreak
    ))

    winner = next(m for m in candidates if m.name == winner_name)
    logger.info(
        "WINNER: %s (score=%.1f, speed=%.0fms/page) for %s",
        winner_name, scores[winner_name],
        results[winner_name].speed_ms_per_page, profile.file_name,
    )

    return winner, results
