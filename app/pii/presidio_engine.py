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
from app.pii.context_deny_list import is_likely_false_positive

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Text preprocessing: join line-break-split numbers before regex matching
# ---------------------------------------------------------------------------

import re

# Patterns that rejoin numbers split across lines by hyphens/whitespace.
# E.g. "123-\n45-\n6789" → "123-45-6789", "078 1234\n5678" → "078 12345678"
_LINEBREAK_SPLIT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Digit-hyphen-newline-digit: "123-\n45" → "123-45"
    (re.compile(r"(\d)-\s*\n\s*(\d)"), r"\1-\2"),
    # Digit-newline-hyphen-digit: "123\n-45" → "123-45"
    (re.compile(r"(\d)\s*\n\s*-(\d)"), r"\1-\2"),
    # Digit-newline-digit (no separator, within a number context):
    # only rejoin when surrounded by number-like context (phone, SSN etc.)
    (re.compile(r"(\d)\s*\n\s*(\d)"), r"\1\2"),
    # Plus-newline-digit for international phone: "+\n44" → "+44"
    (re.compile(r"(\+)\s*\n\s*(\d)"), r"\1\2"),
]


def preprocess_block_text(text: str) -> str:
    """Rejoin number patterns split across line breaks.

    OCR and PDF extraction frequently insert newlines mid-number. This
    preprocessing step stitches those fragments back together so the
    regex engine can match complete patterns like SSNs, phone numbers,
    and credit card numbers.

    The original block text is never modified — this returns a cleaned
    copy used only for detection matching.
    """
    if "\n" not in text:
        return text
    result = text
    for pattern, replacement in _LINEBREAK_SPLIT_PATTERNS:
        result = pattern.sub(replacement, result)
    return result

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
MIN_DETECTION_CONFIDENCE = 0.10  # Phase 14c: drop detections below 10%


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

    def analyze(
        self,
        blocks: list[ExtractedBlock],
        *,
        target_entity_types: list[str] | None = None,
    ) -> list[DetectionResult]:
        """Run Presidio analysis on every block; return DetectionResult list.

        Safety rule: raw text values are never logged.  Only entity_type and
        score appear in log output so no PII leaks into structured logs.

        Parameters
        ----------
        blocks:
            ExtractedBlock objects produced by any reader.
        target_entity_types:
            If provided, only run recognizers for these entity types.
            Dramatically reduces false positives for multi-geo deployments
            by disabling irrelevant recognizers.  If None or empty, run all
            recognizers (backward compatible).

        Returns
        -------
        list[DetectionResult]
            One entry per Presidio hit across all blocks.
            Results with score < 0.75 have needs_layer2=True.
        """
        entities = target_entity_types if target_entity_types else None
        results: list[DetectionResult] = []

        for block in blocks:
            # Preprocess: rejoin numbers split across line breaks
            cleaned_text = preprocess_block_text(block.text)
            presidio_hits = self._analyzer.analyze(
                text=cleaned_text,
                language="en",
                entities=entities,
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

        # Phase 14c: drop detections below minimum confidence threshold
        pre_count = len(results)
        results = [
            det for det in results if det.score >= MIN_DETECTION_CONFIDENCE
        ]
        dropped = pre_count - len(results)
        if dropped:
            logger.debug("Min confidence filter dropped %d detection(s) below %.0f%%", dropped, MIN_DETECTION_CONFIDENCE * 100)

        # Phase 14a: post-filter through context deny-list
        filtered: list[DetectionResult] = []
        for det in results:
            # Use cleaned text for FP checks (offsets are from cleaned copy)
            block_text = preprocess_block_text(det.block.text)
            detected_text = block_text[det.start:det.end]
            # Build surrounding context (~120 chars before/after)
            ctx_start = max(0, det.start - 120)
            ctx_end = min(len(block_text), det.end + 120)
            surrounding = block_text[ctx_start:ctx_end]

            is_fp, reason = is_likely_false_positive(
                detected_text, det.entity_type, surrounding,
            )
            if is_fp:
                logger.debug(
                    "FP suppressed: entity_type=%s reason=%s",
                    det.entity_type, reason,
                )
                continue
            filtered.append(det)

        # Suppress US_BANK_NUMBER when the same span is already detected as US_SSN.
        # US_BANK_NUMBER (Presidio built-in) matches any 8-17 digit number, which
        # overlaps with SSNs, causing massive false positives.  If a value is
        # already classified as US_SSN (more specific), drop the BANK_NUMBER hit.
        ssn_spans: set[tuple[int, int, int]] = set()
        for det in filtered:
            if det.entity_type in ("US_SSN", "TAX_ID"):
                pg = det.block.page_or_sheet if hasattr(det, "block") and det.block else 0
                ssn_spans.add((pg, det.start, det.end))

        if ssn_spans:
            pre_bank = len(filtered)
            filtered = [
                det for det in filtered
                if det.entity_type != "US_BANK_NUMBER"
                or (
                    det.block.page_or_sheet if hasattr(det, "block") and det.block else 0,
                    det.start,
                    det.end,
                ) not in ssn_spans
            ]
            bank_dropped = pre_bank - len(filtered)
            if bank_dropped:
                logger.debug(
                    "Suppressed %d US_BANK_NUMBER detection(s) overlapping with US_SSN",
                    bank_dropped,
                )

        # Phase 14c: deduplicate — keep highest confidence per (value, entity_type, page)
        filtered = deduplicate_detections(filtered)

        # Cross-page header dedup: collapse repeated header/footer PII
        filtered = deduplicate_cross_page_headers(filtered)

        return filtered

    def analyze_metadata(
        self,
        source_path: str,
        *,
        pdf_info: dict[str, str] | None = None,
        target_entity_types: list[str] | None = None,
    ) -> list[DetectionResult]:
        """Run PII detection on filename and optional PDF document info fields.

        Scans the basename of source_path and any values in pdf_info
        (Title, Author, Subject, Creator, Producer, Keywords).  Returns
        detections tagged with extraction_layer="metadata".

        Parameters
        ----------
        source_path:
            Absolute path to the file — only the basename is scanned.
        pdf_info:
            Optional dict from PyMuPDF ``doc.metadata``.  Keys like
            "title", "author", "subject" etc.
        target_entity_types:
            Restrict to these entity types (same as analyze()).
        """
        import os

        entities = target_entity_types if target_entity_types else None
        results: list[DetectionResult] = []

        # Build synthetic text fragments to scan
        fragments: list[tuple[str, str]] = []  # (label, text)
        basename = os.path.basename(source_path)
        # Strip extension, replace separators with spaces for better matching
        name_cleaned = os.path.splitext(basename)[0].replace("_", " ").replace("-", " ")
        if name_cleaned.strip():
            fragments.append(("filename", name_cleaned))

        if pdf_info:
            for key in ("title", "author", "subject", "creator", "keywords"):
                val = pdf_info.get(key, "")
                if val and val.strip():
                    fragments.append((f"pdf_{key}", val))

        for label, text in fragments:
            # Build a synthetic block for provenance
            meta_block = ExtractedBlock(
                text=text,
                page_or_sheet=0,
                source_path=source_path,
                file_type="metadata",
                block_type="prose",
            )
            presidio_hits = self._analyzer.analyze(
                text=text,
                language="en",
                entities=entities,
            )
            for hit in presidio_hits:
                if hit.score < MIN_DETECTION_CONFIDENCE:
                    continue
                pat_def = self._pattern_map.get(hit.entity_type)
                geography = pat_def.geography if pat_def else GEOGRAPHY_GLOBAL
                regulatory_framework = pat_def.regulatory_framework if pat_def else ""
                pattern_used = pat_def.regex if pat_def else ""

                logger.debug(
                    "Metadata PII: source=%s entity_type=%s score=%.3f",
                    label, hit.entity_type, hit.score,
                )
                results.append(DetectionResult(
                    block=meta_block,
                    entity_type=hit.entity_type,
                    start=hit.start,
                    end=hit.end,
                    score=hit.score,
                    pattern_used=pattern_used,
                    geography=geography,
                    regulatory_framework=regulatory_framework,
                    extraction_layer="metadata",
                ))

        return results


_CROSS_PAGE_HEADER_THRESHOLD = 3  # appear on >N pages to be considered header


def deduplicate_cross_page_headers(
    detections: list[DetectionResult],
    *,
    threshold: int = _CROSS_PAGE_HEADER_THRESHOLD,
) -> list[DetectionResult]:
    """Remove detections whose (text, entity_type) appears on more than *threshold* pages.

    This catches header/footer PII (company phone, report dates, preparer names)
    that repeat across many pages.  Unlike static_filter.py (which works
    post-extraction on PIIRecords), this runs at detection time and reduces
    noise before extraction even begins.

    Protected entity types (SSN, GOVERNMENT_ID) are never removed.

    Returns
    -------
    list[DetectionResult]
        Detections with cross-page headers collapsed.
    """
    if not detections:
        return detections

    PROTECTED = frozenset({"SSN", "SSN_NODASH", "SSN_PARTIAL", "SSN_LAST_FOUR", "GOVERNMENT_ID"})

    # Count distinct pages per (text, entity_type)
    page_sets: dict[tuple[str, str], set[int | str]] = {}
    for det in detections:
        text = preprocess_block_text(det.block.text)[det.start:det.end]
        key = (text.strip(), det.entity_type)
        page_sets.setdefault(key, set()).add(det.block.page_or_sheet)

    # Identify header keys
    header_keys = {
        key for key, pages in page_sets.items()
        if len(pages) > threshold and key[1] not in PROTECTED
    }

    if not header_keys:
        return detections

    # Keep only one representative per header key (highest score)
    kept: list[DetectionResult] = []
    best_per_header: dict[tuple[str, str], DetectionResult] = {}
    for det in detections:
        text = preprocess_block_text(det.block.text)[det.start:det.end]
        key = (text.strip(), det.entity_type)
        if key in header_keys:
            existing = best_per_header.get(key)
            if existing is None or det.score > existing.score:
                best_per_header[key] = det
        else:
            kept.append(det)

    kept.extend(best_per_header.values())
    removed = len(detections) - len(kept)
    if removed:
        logger.debug(
            "Cross-page header dedup removed %d detection(s) across %d header pattern(s)",
            removed, len(header_keys),
        )
    return kept


def deduplicate_detections(detections: list[DetectionResult]) -> list[DetectionResult]:
    """Remove duplicate detections, keeping the highest confidence per group.

    Groups by (detected_text, entity_type, page_or_sheet).  For each group,
    only the detection with the highest score is retained.

    Returns
    -------
    list[DetectionResult]
        Deduplicated list.
    """
    if not detections:
        return detections

    groups: dict[tuple, DetectionResult] = {}
    for det in detections:
        text = det.block.text[det.start:det.end]
        page = det.block.page_or_sheet
        key = (text, det.entity_type, page)
        existing = groups.get(key)
        if existing is None or det.score > existing.score:
            groups[key] = det

    deduped = list(groups.values())
    removed = len(detections) - len(deduped)
    if removed:
        logger.debug("Dedup removed %d duplicate detection(s)", removed)
    return deduped
