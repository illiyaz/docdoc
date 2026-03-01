"""Layer 2: context window classification for low-confidence Layer 1 results.

Invoked for any DetectionResult where needs_layer2=True (score < 0.75).
Examines the 100 characters surrounding the match in the block text and
applies deterministic keyword-based context analysis to boost confidence.

Phase 1 uses keyword lookup only.  Phase 2 will replace this with a
fine-tuned spaCy text classifier trained on human-reviewed labels.

Safety rule: raw text and context window content are never logged — only
entity_type, score delta, and whether a signal was found.
"""
from __future__ import annotations

import logging

from app.pii.presidio_engine import DetectionResult

logger = logging.getLogger(__name__)

CONFIDENCE_THRESHOLD: float = 0.75
_CONTEXT_WINDOW_CHARS: int = 100
_BOOST_AMOUNT: float = 0.20
_MAX_SCORE: float = 1.0
_INSTITUTIONAL_PENALTY: float = 0.15
_PRIMARY_BOOST: float = 0.05

# Keywords that corroborate a given entity type when found in the context window.
_CONTEXT_SIGNALS: dict[str, list[str]] = {
    "SSN": ["ssn", "social security", "sin", "tax id", "tin"],
    "PERSON": ["name", "employee", "patient", "client", "staff", "person"],
    "EMAIL_ADDRESS": ["email", "e-mail", "contact", "mailto"],
    "PHONE_NUMBER": ["phone", "tel", "telephone", "call", "fax", "mobile", "cell"],
    "LOCATION": ["address", "addr", "city", "state", "zip", "postal", "street"],
    "DATE_TIME": ["date", "dob", "born", "birth", "hired", "since"],
    "CREDIT_CARD": ["card", "visa", "mastercard", "amex", "cc", "credit", "payment"],
    "FINANCIAL_ACCOUNT": ["account", "acct", "iban", "routing", "bank", "swift"],
    "IP_ADDRESS": ["ip address", "host", "server", "network"],
    "DRIVER_LICENSE_US": ["license", "licence", "dl", "driver", "dmv"],
    "PASSPORT": ["passport", "travel document"],
    "AADHAAR": ["aadhaar", "aadhar", "uid"],
    "PAN_IN": ["pan", "permanent account"],
    "NI_UK": ["national insurance", "nino"],
}


class Layer2ContextClassifier:
    """Apply context window classification to low-confidence Layer 1 results.

    One instance may be shared across calls — this class holds no mutable state.
    """

    def classify(
        self,
        result: DetectionResult,
        full_text: str,
        *,
        entity_role: str | None = None,
    ) -> DetectionResult:
        """Examine a 100-char window around the match and return an updated result.

        Parameters
        ----------
        result:
            Layer 1 DetectionResult, typically with needs_layer2=True.
        full_text:
            Complete text of the block the match came from.  Never logged.
        entity_role:
            Optional entity role from structure analysis.  When provided,
            ``"institutional"`` reduces score by 0.15 and
            ``"primary_subject"`` boosts score by 0.05.

        Returns
        -------
        DetectionResult
            Always has extraction_layer="layer_2_context".
            Score is boosted by _BOOST_AMOUNT (capped at 1.0) when a signal
            keyword is found in the context window; otherwise score unchanged.
            needs_layer2 is recalculated from the new score in __post_init__.
        """
        window_start = max(0, result.start - _CONTEXT_WINDOW_CHARS)
        window_end = min(len(full_text), result.end + _CONTEXT_WINDOW_CHARS)
        context_lower = full_text[window_start:window_end].lower()

        signals = _CONTEXT_SIGNALS.get(result.entity_type, [])
        matched_signal = next((s for s in signals if s in context_lower), None)

        new_score = result.score
        if matched_signal:
            new_score = min(_MAX_SCORE, result.score + _BOOST_AMOUNT)

        # Apply entity role confidence nudge
        if entity_role == "institutional":
            new_score = max(0.0, new_score - _INSTITUTIONAL_PENALTY)
        elif entity_role == "primary_subject":
            new_score = min(_MAX_SCORE, new_score + _PRIMARY_BOOST)

        # SAFETY: never log raw text — only entity_type, scores, and bool flag
        logger.debug(
            "Layer2: entity_type=%s old_score=%.3f new_score=%.3f signal_found=%s role=%s",
            result.entity_type,
            result.score,
            new_score,
            matched_signal is not None,
            entity_role,
        )

        return DetectionResult(
            block=result.block,
            entity_type=result.entity_type,
            start=result.start,
            end=result.end,
            score=new_score,
            pattern_used=result.pattern_used,
            geography=result.geography,
            regulatory_framework=result.regulatory_framework,
            extraction_layer="layer_2_context",
        )
