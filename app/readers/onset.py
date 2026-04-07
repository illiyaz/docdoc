"""Content onset detection: find the first page containing real data.

Scans the document from page 0 and returns the page index where
meaningful content begins. Cover pages, tables of contents, and
legal disclaimers are skipped using penalty-based scoring.

The returned onset_page is stored in the DocumentCatalog record.
The extraction pipeline always starts from onset_page — never from
page 0 by default.

SCORING SYSTEM (proven on 34 real breach documents, March 2026):
  Cover page signals: -20 per match (Report Summary, Account Criteria, etc.)
  Data signals: +30 per name/SSN/DOB pattern found
  Diversity bonus: +15 per distinct PII type found on page
  Volume bonus: +2 per line of text (more content = more likely data page)
  Winner: page with highest score, minimum threshold of 20
"""
from __future__ import annotations

import re

# ── COVER PAGE SIGNALS (penalty: -20 each) ──────────────────
# These indicate summary/metadata pages, NOT data pages
COVER_SIGNALS: list[re.Pattern[str]] = [re.compile(p, re.I) for p in [
    r"report\s+summary", r"account\s+criteria", r"report\s+style",
    r"items\s+displayed", r"total\s+accounts?\s+in\s+report",
    r"total\s+(?:cert|dr)?\s*shares", r"share\s+(?:dating|types?)",
    r"date\s+(?:closed|opened)\s+range", r"sorting\s+name",
    r"table\s+of\s+contents", r"confidential", r"prepared\s+(?:by|for)",
    r"disclaimer", r"legal\s+notice", r"copyright\s+\d{4}",
    r"page\s+\d+\s+of\s+\d+", r"run\s+(?:time|date)",
]]

# ── DATA SIGNALS (positive scores) ──────────────────────────
DATA_SIGNALS: dict[str, tuple[re.Pattern[str], int]] = {
    "SSN": (re.compile(r"\d{3}-\d{2}-\d{4}"), 30),
    "DOB_DATE": (re.compile(r"\d{1,2}/\d{1,2}/\d{2,4}"), 10),
    "NAME_LAST_FIRST": (re.compile(r"[A-Z][a-z]+,\s*[A-Z]"), 15),
    "NAME_LABEL": (re.compile(r"\b(?:name|employee|patient|client)\s*:", re.I), 20),
    "SSN_LABEL": (re.compile(r"\b(?:ssn|soc\s*sec|social\s+security)\b", re.I), 25),
    "DOB_LABEL": (re.compile(r"\b(?:d\.?o\.?b\.?|date\s+of\s+birth|birth\s*date)\b", re.I), 20),
    "ADDRESS_LABEL": (re.compile(r"\b(?:address|street|city|state|zip)\s*:", re.I), 10),
    "ACCOUNT_LABEL": (re.compile(r"\b(?:account|acct|policy)\s*[#:]", re.I), 15),
    "ALL_CAPS_NAME": (re.compile(r"^[A-Z]{2,}\s+[A-Z]\s+[A-Z]{2,}$", re.M), 15),
    "PHONE": (re.compile(r"\(\d{3}\)\s*\d{3}[-.]?\d{4}"), 8),
    "EMAIL": (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), 8),
    "GOV_ID": (re.compile(r"[A-Z]{2}\s?\d{2}\s?\d{2}\s?\d{2}\s?[A-Z]"), 20),
}

# Legacy simple patterns (kept for backward compatibility)
ONSET_SIGNALS: list[str] = [
    r'\b(name|ssn|date of birth|dob|address|account|policy)\b',
    r'\d{3}-\d{2}-\d{4}',    # SSN pattern
    r'\b[A-Z]{2}\d{6,}\b',   # ID number pattern
]

# Pre-compiled for efficiency; re.I so keyword patterns match any case.
_COMPILED_SIGNALS: list[re.Pattern[str]] = [
    re.compile(sig, re.IGNORECASE) for sig in ONSET_SIGNALS
]


def _score_page(text: str) -> tuple[int, dict[str, int]]:
    """Score a page for data content. Higher = more likely to be a data page.
    
    Returns (score, signal_counts_by_type).
    """
    score = 0
    signals_found: dict[str, int] = {}
    
    # Cover page penalty
    for pat in COVER_SIGNALS:
        matches = len(pat.findall(text))
        if matches:
            score -= 20 * matches
            signals_found["COVER_PENALTY"] = signals_found.get("COVER_PENALTY", 0) + matches
    
    # Data signals
    types_found = set()
    for sig_name, (pat, points) in DATA_SIGNALS.items():
        matches = len(pat.findall(text))
        if matches:
            score += points * min(matches, 5)  # cap at 5 matches per type
            signals_found[sig_name] = matches
            types_found.add(sig_name)
    
    # Diversity bonus: more distinct PII types = more likely data
    if len(types_found) >= 2:
        score += 15 * (len(types_found) - 1)
    
    # Volume bonus: pages with more text are more likely data pages
    line_count = len([l for l in text.split("\n") if l.strip()])
    score += min(line_count, 50) * 2  # cap at 50 lines
    
    return score, signals_found


def find_data_onset(doc: object, max_scan: int = 30) -> int:
    """Return the page index where extraction should begin.

    Uses penalty-based scoring: cover pages get negative scores,
    data pages get positive scores. Returns the page with the
    highest score above threshold.

    Falls back to legacy signal matching if scoring finds nothing.
    
    Memory rule: doc._forget_page(page_num) is called after each page
    to release memory immediately (CLAUDE.md § 2).
    """
    scores: list[tuple[int, int]] = []  # [(page_num, score)]
    
    for page_num in range(min(len(doc), max_scan)):
        text = doc.load_page(page_num).get_text()
        doc._forget_page(page_num)
        
        score, _ = _score_page(text)
        scores.append((page_num, score))
    
    if not scores:
        return 0

    # Find the FIRST page that meets the data threshold (score >= 20).
    # Previous logic picked the HIGHEST scoring page, which could be
    # page 11 in a doc where ALL pages have data — skipping pages 0-10.
    for page_num, score in scores:
        if score >= 20:
            return page_num

    # No page met threshold — fall through to legacy signal matching.
    # (Don't use positive-score fallback here — volume-bonus alone can
    # produce misleading low scores like 2 on blank-ish pages.)

    # Fallback: legacy signal matching (backward compatible)
    for page_num in range(min(len(doc), max_scan)):
        text = doc.load_page(page_num).get_text()
        doc._forget_page(page_num)
        if any(pattern.search(text) for pattern in _COMPILED_SIGNALS):
            return max(0, page_num - 1)
    
    return 0
