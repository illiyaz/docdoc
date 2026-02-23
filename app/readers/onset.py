"""Content onset detection: find the first page containing real data.

Scans the document from page 0 and returns the page index where
meaningful content begins (one page before the first signal match).
Cover pages, tables of contents, and legal disclaimers are skipped.

The returned onset_page is stored in the DocumentCatalog record.
The extraction pipeline always starts from onset_page — never from
page 0 by default.

Signal patterns are defined in ONSET_SIGNALS (CLAUDE.md § 2).
"""
from __future__ import annotations

import re

ONSET_SIGNALS: list[str] = [
    r'\b(name|ssn|date of birth|dob|address|account|policy)\b',
    r'\d{3}-\d{2}-\d{4}',    # SSN pattern
    r'\b[A-Z]{2}\d{6,}\b',   # ID number pattern
]

# Pre-compiled for efficiency; re.I so keyword patterns match any case.
_COMPILED_SIGNALS: list[re.Pattern[str]] = [
    re.compile(sig, re.IGNORECASE) for sig in ONSET_SIGNALS
]


def find_data_onset(doc: object) -> int:
    """Return the page index where extraction should begin.

    Scans pages 0..N-1 in order. On the first page that contains any
    ONSET_SIGNAL match, returns max(0, page_num - 1) so the pipeline
    starts one page before the first data signal.

    Returns 0 if no signals are found anywhere in the document.
    Memory rule: doc._forget_page(page_num) is called after each page
    to release memory immediately (CLAUDE.md § 2).
    """
    for page_num in range(len(doc)):
        text = doc.load_page(page_num).get_text()
        doc._forget_page(page_num)
        if any(pattern.search(text) for pattern in _COMPILED_SIGNALS):
            return max(0, page_num - 1)
    return 0
