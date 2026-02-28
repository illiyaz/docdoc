"""Presidio engine: primary PII detection wrapper (Layer 1).

Wraps Microsoft Presidio AnalyzerEngine with all custom recognisers from
layer1_patterns.py.  Presidio's built-in recognisers (EMAIL_ADDRESS,
PHONE_NUMBER, CREDIT_CARD, IP_ADDRESS, …) are also active.

Air-gap rule
------------
spaCy model weights are loaded from the local models/ directory — no
outbound network calls are made at runtime.  The model must be pre-staged
before deployment via `python -m spacy download en_core_web_trf` (or
equivalent offline transfer).

Never log raw text values — only entity_type and score appear in log output.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from presidio_analyzer import AnalyzerEngine, RecognizerRegistry
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_analyzer.pattern import Pattern
from presidio_analyzer.pattern_recognizer import PatternRecognizer

from app.readers.base import ExtractedBlock
from app.pii.layer1_patterns import GEOGRAPHY_GLOBAL, PatternDefinition, get_all_patterns

logger = logging.getLogger(__name__)

def _resolve_spacy_model() -> str:
    """Pick the best available spaCy model: trf > lg > md > sm."""
    try:
        import spacy.util
        for name in ("en_core_web_trf", "en_core_web_lg", "en_core_web_md", "en_core_web_sm"):
            if spacy.util.is_package(name):
                return name
    except (ImportError, ModuleNotFoundError):
        pass
    return "en_core_web_trf"  # default; Presidio raises a clear error at init time

_SPACY_MODEL = _resolve_spacy_model()
_LAYER2_SCORE_THRESHOLD = 0.75


# ---------------------------------------------------------------------------
# DetectionResult
# ---------------------------------------------------------------------------

@dataclass
class DetectionResult:
    """A single PII detection produced by PresidioEngine.analyze().

    Fields
    ------
    block:                The ExtractedBlock the text came from.
    entity_type:          Presidio entity type string.
    start / end:          Character offsets within block.text.
    score:                Confidence score from the firing recogniser.
    pattern_used:         Regex that matched (empty string for Presidio built-ins).
    geography:            Jurisdiction scope of the firing pattern.
    regulatory_framework: Applicable regulation(s).
    extraction_layer:     Always "layer_1_pattern" for results from this engine.
    needs_layer2:         True when score < 0.75 — result must be forwarded to
                          Layer 2 context classifier before being acted upon.
    """
    block: ExtractedBlock
    entity_type: str
    start: int
    end: int
    score: float
    pattern_used: str
    geography: str
    regulatory_framework: str
    extraction_layer: str = "layer_1_pattern"
    needs_layer2: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.needs_layer2 = self.score < _LAYER2_SCORE_THRESHOLD


# ---------------------------------------------------------------------------
# PresidioEngine
# ---------------------------------------------------------------------------

class PresidioEngine:
    """Thin wrapper around Presidio AnalyzerEngine with custom recognisers.

    One instance should be created per process (model loading is expensive).
    Not thread-safe — create one instance per concurrent worker.
    """

    def __init__(self, geographies: list[str] | None = None) -> None:
        """Load spaCy model and initialise Presidio with custom recognisers.

        Parameters
        ----------
        geographies:
            If None, load patterns for all geographies.
            Otherwise load only GLOBAL patterns plus the listed codes.
        """
        patterns = get_all_patterns(geographies)
        # Build lookup so analyse() can attach PatternDefinition metadata
        self._pattern_map: dict[str, PatternDefinition] = {
            p.entity_type: p for p in patterns
        }

        # NLP engine — spaCy model loaded from local installation
        nlp_configuration = {
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "en", "model_name": _SPACY_MODEL}],
        }
        provider = NlpEngineProvider(nlp_configuration=nlp_configuration)
        nlp_engine = provider.create_engine()

        # Recogniser registry: built-ins + custom patterns
        registry = RecognizerRegistry()
        registry.load_predefined_recognizers(nlp_engine=nlp_engine)

        for pat_def in patterns:
            recogniser = PatternRecognizer(
                supported_entity=pat_def.entity_type,
                name=pat_def.name,
                patterns=[
                    Pattern(
                        name=pat_def.name,
                        regex=pat_def.regex,
                        score=pat_def.score,
                    )
                ],
            )
            registry.add_recognizer(recogniser)

        self._analyzer = AnalyzerEngine(
            registry=registry,
            nlp_engine=nlp_engine,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze(self, blocks: list[ExtractedBlock]) -> list[DetectionResult]:
        """Run Presidio analysis on every block; return DetectionResult list.

        Safety rule: raw text values are never logged.  Only entity_type and
        score appear in log output so no PII leaks into structured logs.

        Parameters
        ----------
        blocks:
            ExtractedBlock objects produced by any reader.

        Returns
        -------
        list[DetectionResult]
            One entry per Presidio hit across all blocks.
            Results with score < 0.75 have needs_layer2=True.
        """
        results: list[DetectionResult] = []

        for block in blocks:
            presidio_hits = self._analyzer.analyze(
                text=block.text,
                language="en",
            )
            for hit in presidio_hits:
                pat_def = self._pattern_map.get(hit.entity_type)
                geography = pat_def.geography if pat_def else GEOGRAPHY_GLOBAL
                regulatory_framework = pat_def.regulatory_framework if pat_def else ""
                pattern_used = pat_def.regex if pat_def else ""

                # SAFETY: log only metadata — never the matched text span
                logger.debug(
                    "PII detected: entity_type=%s score=%.3f needs_layer2=%s",
                    hit.entity_type,
                    hit.score,
                    hit.score < _LAYER2_SCORE_THRESHOLD,
                )

                results.append(DetectionResult(
                    block=block,
                    entity_type=hit.entity_type,
                    start=hit.start,
                    end=hit.end,
                    score=hit.score,
                    pattern_used=pattern_used,
                    geography=geography,
                    regulatory_framework=regulatory_framework,
                ))

        return results
