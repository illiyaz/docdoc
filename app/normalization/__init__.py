"""Normalization package.

One normalizer per PII field type.  Each normalizer takes a raw string
value and returns a canonical form that is safe to store as
``Extraction.normalized_value``.

All normalizers follow the same contract::

    def normalize(raw: str) -> str:
        ...

Raises ``NotImplementedError`` until Phase 2 implementation is complete.
"""
