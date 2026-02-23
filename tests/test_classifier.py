"""Tests for app/readers/classifier.py.

All tests use Mock page objects — no real PDF files required.

Boundary table (per CLAUDE.md § 2):
  word_count   label
  ----------   --------
  0 – 5        scanned
  6 – 50       corrupted
  51+          digital
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.readers.classifier import classify_page, PageClass


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _page(text: str) -> MagicMock:
    """Return a mock PyMuPDF page whose get_text() returns *text*."""
    page = MagicMock()
    page.get_text.return_value = text
    return page


def _page_words(n: int) -> MagicMock:
    """Return a mock page with exactly *n* whitespace-separated words."""
    return _page(" ".join(["word"] * n))


# ---------------------------------------------------------------------------
# "digital" — word_count > 50
# ---------------------------------------------------------------------------

def test_classify_51_words_is_digital():
    assert classify_page(_page_words(51)) == "digital"


def test_classify_100_words_is_digital():
    assert classify_page(_page_words(100)) == "digital"


def test_classify_large_page_is_digital():
    # A realistic dense page has hundreds of words
    assert classify_page(_page_words(400)) == "digital"


def test_classify_real_prose_is_digital():
    prose = " ".join(["The quick brown fox jumps over the lazy dog"] * 10)
    assert classify_page(_page(prose)) == "digital"


# ---------------------------------------------------------------------------
# "corrupted" — 5 < word_count <= 50
# ---------------------------------------------------------------------------

def test_classify_6_words_is_corrupted():
    assert classify_page(_page_words(6)) == "corrupted"


def test_classify_25_words_is_corrupted():
    assert classify_page(_page_words(25)) == "corrupted"


def test_classify_50_words_is_corrupted():
    # 50 is NOT > 50, so falls into the > 5 branch
    assert classify_page(_page_words(50)) == "corrupted"


def test_classify_sparse_text_is_corrupted():
    # A few garbled OCR tokens — classic corrupted page
    assert classify_page(_page("Joh n Doe 12 34 5 6 78")) == "corrupted"


# ---------------------------------------------------------------------------
# "scanned" — word_count <= 5
# ---------------------------------------------------------------------------

def test_classify_empty_text_is_scanned():
    assert classify_page(_page("")) == "scanned"


def test_classify_1_word_is_scanned():
    assert classify_page(_page_words(1)) == "scanned"


def test_classify_5_words_is_scanned():
    # 5 is NOT > 5, so falls to the default branch
    assert classify_page(_page_words(5)) == "scanned"


def test_classify_whitespace_only_is_scanned():
    # split() on whitespace-only strings yields []
    assert classify_page(_page("   \t\n  ")) == "scanned"


def test_classify_single_newline_is_scanned():
    assert classify_page(_page("\n")) == "scanned"


# ---------------------------------------------------------------------------
# Boundary precision
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("n,expected", [
    (5,  "scanned"),
    (6,  "corrupted"),
    (50, "corrupted"),
    (51, "digital"),
])
def test_classify_boundary_values(n: int, expected: PageClass):
    assert classify_page(_page_words(n)) == expected


# ---------------------------------------------------------------------------
# Interface contract
# ---------------------------------------------------------------------------

def test_classify_calls_get_text_once():
    page = _page_words(60)
    classify_page(page)
    page.get_text.assert_called_once_with()


def test_classify_does_not_call_get_text_dict():
    """classifier must use page.get_text() with no args, not get_text('dict')."""
    page = _page_words(60)
    classify_page(page)
    args, kwargs = page.get_text.call_args
    assert args == () and kwargs == {}


def test_classify_return_type_is_string():
    result = classify_page(_page_words(10))
    assert isinstance(result, str)


def test_classify_all_valid_labels():
    valid = {"digital", "scanned", "corrupted"}
    for n in (0, 1, 5, 6, 50, 51, 200):
        label = classify_page(_page_words(n))
        assert label in valid, f"Unexpected label {label!r} for {n} words"
