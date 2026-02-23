"""Page classifier: determine the processing path for each PDF page.

Returns one of three labels:
  "digital"   — real text layer present; use PyMuPDF directly
  "scanned"   — pure image page; must OCR with PaddleOCR
  "corrupted" — degraded or sparse text layer; re-OCR with PaddleOCR

Thresholds (from CLAUDE.md § 2):
  word_count > 50  → digital
  word_count > 5   → corrupted
  else             → scanned
"""
from __future__ import annotations

from typing import Literal

PageClass = Literal["digital", "scanned", "corrupted"]


def classify_page(page: object) -> PageClass:
    """Classify a PyMuPDF page object and return the processing-path label.

    Calls page.get_text() with no arguments (plain text, not dict mode)
    and counts whitespace-separated tokens to determine the label.
    """
    word_count = len(page.get_text().split())
    if word_count > 50:
        return "digital"
    if word_count > 5:
        return "corrupted"
    return "scanned"
