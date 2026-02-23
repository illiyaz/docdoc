"""spaCy context window classifier for Layer 2 PII type inference.

Invoked only when a Presidio result has confidence < 0.75 after Layer 1.
Examines the CONTEXT_WINDOW_CHARS characters surrounding the match and
uses a local spaCy model to infer PII type from context.

The spaCy model is pre-packaged in models/ (en_core_web_trf or equivalent).
HuggingFace Hub must not be called at inference time (air-gap safe).
"""
from __future__ import annotations

CONTEXT_WINDOW_CHARS: int = 100


class SpaCyContextClassifier:
    """Context window classifier backed by a local spaCy model."""

    def __init__(self, model_path: str | None = None) -> None:
        ...

    def classify(self, text: str, match_start: int, match_end: int) -> dict:
        """Return {'entity_type': str, 'confidence': float} for the match context."""
        ...

    def _extract_window(self, text: str, start: int, end: int) -> str:
        """Return the ±CONTEXT_WINDOW_CHARS text window around [start, end]."""
        ...
