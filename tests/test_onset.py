"""Tests for app/readers/onset.py.

All tests use MagicMock document objects — no real PDF files required.
MagicMock is used instead of Mock so that len() and attribute access work
without extra configuration.

Scoring-based onset detection:
  Primary: score every page (cover penalties + data signals + diversity + volume).
           Return the page with the highest score if score >= 20.
  Fallback: legacy signal matching returns max(0, first_signal_page - 1).
  Default: 0 if nothing found.
"""
from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

from app.readers.onset import ONSET_SIGNALS, find_data_onset


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _doc(pages: list[str]) -> MagicMock:
    """Return a mock fitz.Document with the given page texts."""
    doc = MagicMock()
    doc.__len__.return_value = len(pages)

    def _load_page(n: int) -> MagicMock:
        page = MagicMock()
        page.get_text.return_value = pages[n]
        return page

    doc.load_page.side_effect = _load_page
    return doc


# ---------------------------------------------------------------------------
# No signals → always 0
# ---------------------------------------------------------------------------

def test_no_signals_empty_doc_returns_0():
    assert find_data_onset(_doc([])) == 0


def test_no_signals_one_page_returns_0():
    assert find_data_onset(_doc(["Just a cover page with nothing relevant"])) == 0


def test_no_signals_many_pages_returns_0():
    pages = ["Introduction", "Table of Contents", "Preface", "Legal disclaimer"]
    assert find_data_onset(_doc(pages)) == 0


# ---------------------------------------------------------------------------
# Signal position → scoring returns the highest-scoring page directly
# ---------------------------------------------------------------------------

def test_signal_on_page_0_returns_0():
    # Page 0 scores highest (NAME_LABEL + volume) → returns 0
    assert find_data_onset(_doc(["Name: John Doe"])) == 0


def test_signal_on_page_1_returns_1():
    # Page 1 scores highest (NAME_LABEL) → returns 1 directly
    assert find_data_onset(_doc(["Cover", "Name: John Doe"])) == 1


def test_signal_on_page_2_returns_2():
    # Page 2 scores highest → returns 2 directly
    assert find_data_onset(_doc(["Cover", "TOC", "Name: John Doe"])) == 2


def test_signal_on_page_3_returns_3():
    # Page 3 scores highest → returns 3 directly
    assert find_data_onset(_doc(["Cover", "TOC", "Legal", "Name: John Doe"])) == 3


def test_signal_on_last_page_of_five():
    # Page 4 has SSN + SSN_LABEL → highest score → returns 4
    pages = ["p0", "p1", "p2", "p3", "SSN: 123-45-6789"]
    assert find_data_onset(_doc(pages)) == 4


def test_onset_never_negative():
    # Even with signal on page 0 the result must be >= 0
    for text in ["Name: Alice", "123-45-6789", "account 12345", "AB123456"]:
        assert find_data_onset(_doc([text])) >= 0


# ---------------------------------------------------------------------------
# Each ONSET_SIGNAL pattern triggers detection
# ---------------------------------------------------------------------------

def test_keyword_name_triggers():
    # Scoring: NAME_LABEL on page 2 scores highest → returns 2
    assert find_data_onset(_doc(["Cover", "TOC", "name: Alice Smith"])) == 2


def test_keyword_ssn_word_triggers():
    # Scoring: SSN_LABEL on page 1 scores highest → returns 1
    assert find_data_onset(_doc(["Cover", "ssn required"])) == 1


def test_keyword_dob_triggers():
    # Scoring: DOB_LABEL on page 2 scores highest → returns 2
    assert find_data_onset(_doc(["Cover", "TOC", "dob: 1980-01-15"])) == 2


def test_keyword_date_of_birth_triggers():
    # Scoring: DOB_LABEL on page 1 scores highest → returns 1
    assert find_data_onset(_doc(["Cover", "date of birth: unknown"])) == 1


def test_keyword_address_triggers():
    assert find_data_onset(_doc(["Cover", "TOC", "address: 123 Main St"])) == 1


def test_keyword_account_triggers():
    assert find_data_onset(_doc(["Cover", "account number 98765"])) == 0


def test_keyword_policy_triggers():
    assert find_data_onset(_doc(["Cover", "TOC", "Policy: ABC-001"])) == 1


def test_ssn_numeric_pattern_triggers():
    # SSN pattern on page 2 scores 30+2=32 → returns 2 directly
    assert find_data_onset(_doc(["Cover", "TOC", "ID: 123-45-6789"])) == 2


def test_id_number_pattern_triggers():
    # \b[A-Z]{2}\d{6,}\b
    assert find_data_onset(_doc(["Cover", "Ref: AB123456"])) == 0


def test_id_number_long_digits_triggers():
    assert find_data_onset(_doc(["Cover", "TOC", "XY9876543"])) == 1


# ---------------------------------------------------------------------------
# Case-insensitive matching
# ---------------------------------------------------------------------------

def test_keyword_uppercase_triggers():
    # NAME_LABEL on page 2 scores highest → returns 2
    assert find_data_onset(_doc(["Cover", "TOC", "NAME: JOHN DOE"])) == 2


def test_keyword_mixed_case_triggers():
    # DOB_LABEL on page 1 scores highest → returns 1
    assert find_data_onset(_doc(["Cover", "Date Of Birth: unknown"])) == 1


def test_keyword_all_caps_ssn_triggers():
    # SSN_LABEL on page 2 scores highest → returns 2
    assert find_data_onset(_doc(["Cover", "TOC", "SSN: provided above"])) == 2


# ---------------------------------------------------------------------------
# Highest score wins — scoring scans all pages to find the best
# ---------------------------------------------------------------------------

def test_first_data_page_wins():
    # Page 2 has NAME_LABEL (first page above threshold) → returns page 2
    # Even though page 4 has SSN + SSN_LABEL (higher score), we want the
    # FIRST data page to avoid skipping valid records on earlier pages.
    pages = ["p0", "p1", "Name: Alice", "p3", "SSN: 123-45-6789"]
    assert find_data_onset(_doc(pages)) == 2


def test_all_pages_scanned_for_scoring():
    """Scoring phase loads all pages (up to max_scan) to find the best."""
    pages = ["Cover", "TOC", "Name: Bob", "p3", "p4"]
    doc = _doc(pages)
    find_data_onset(doc)
    loaded = [c.args[0] for c in doc.load_page.call_args_list]
    # All 5 pages loaded in the scoring loop
    assert 0 in loaded and 1 in loaded and 2 in loaded and 3 in loaded and 4 in loaded


# ---------------------------------------------------------------------------
# _forget_page is called on every loaded page
# ---------------------------------------------------------------------------

def test_forget_page_called_for_each_loaded_page():
    """CLAUDE.md requires _forget_page after every load_page call."""
    pages = ["Cover", "TOC", "Name: Alice"]
    doc = _doc(pages)
    find_data_onset(doc)
    forget_calls = [c.args[0] for c in doc._forget_page.call_args_list]
    # Pages 0, 1, 2 were loaded; all three must have been forgotten
    assert forget_calls == [0, 1, 2]


def test_forget_page_called_on_matching_page():
    """The page where the signal is found must also be forgotten."""
    doc = _doc(["Name: Alice"])
    find_data_onset(doc)
    doc._forget_page.assert_called_once_with(0)


# ---------------------------------------------------------------------------
# ONSET_SIGNALS constant
# ---------------------------------------------------------------------------

def test_onset_signals_is_nonempty_list():
    assert isinstance(ONSET_SIGNALS, list)
    assert len(ONSET_SIGNALS) > 0


def test_onset_signals_contains_ssn_numeric_pattern():
    import re
    ssn_patterns = [s for s in ONSET_SIGNALS if re.search(r"\\d.*\\d", s)]
    assert len(ssn_patterns) >= 1, "Expected at least one numeric SSN-style pattern"


def test_onset_signals_contains_keyword_pattern():
    assert any("name" in sig.lower() for sig in ONSET_SIGNALS)
