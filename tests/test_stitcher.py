"""Tests for app/readers/stitcher.py — PageStitcher cross-page stitching.

Covers every requirement listed in the task:
  - First page: no tail prepended, tail_buffer_len == 0
  - Second page: first page's tail is prepended correctly
  - stitched_text == tail_text + "\\n" + page_text when buffer exists
  - tail_buffer_len == len("\\n".join(tail_lines))
  - spans_pages logic: start_char < tail_len  → (page_num-1, page_num)
  - spans_pages logic: start_char >= tail_len → None
  - reset() clears buffer; next stitch behaves like first page
  - Buffer keeps only last 5 lines after each stitch
  - Empty page_text leaves the buffer empty after that stitch
  - tail_buffer property returns a copy, not the internal list
  - TAIL_BUFFER_LINES constant == 5
"""
from __future__ import annotations

import pytest

from app.readers.stitcher import TAIL_BUFFER_LINES, PageStitcher


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def stitcher() -> PageStitcher:
    return PageStitcher()


# Concrete page texts used across multiple tests.
PAGE0 = "alpha\nbeta\ngamma"           # 3 lines
PAGE0_TAIL_TEXT = "alpha\nbeta\ngamma" # what "\n".join(["alpha","beta","gamma"]) gives
PAGE0_TAIL_LEN = len(PAGE0_TAIL_TEXT)  # 16

PAGE1 = "delta\nepsilon"               # 2 lines


# ---------------------------------------------------------------------------
# 1. TAIL_BUFFER_LINES constant
# ---------------------------------------------------------------------------

def test_tail_buffer_lines_constant_is_5():
    assert TAIL_BUFFER_LINES == 5


# ---------------------------------------------------------------------------
# 2. First page — no buffer, tail_buffer_len == 0
# ---------------------------------------------------------------------------

def test_first_page_tail_buffer_len_is_zero(stitcher):
    _, tail_len = stitcher.stitch(0, PAGE0)
    assert tail_len == 0


def test_first_page_stitched_equals_page_text(stitcher):
    stitched, _ = stitcher.stitch(0, PAGE0)
    assert stitched == PAGE0


def test_first_page_no_extra_prefix(stitcher):
    stitched, _ = stitcher.stitch(0, PAGE0)
    assert not stitched.startswith("\n")


def test_first_page_with_single_line(stitcher):
    text = "one line only"
    stitched, tail_len = stitcher.stitch(0, text)
    assert stitched == text
    assert tail_len == 0


def test_first_page_with_empty_text(stitcher):
    stitched, tail_len = stitcher.stitch(0, "")
    assert stitched == ""
    assert tail_len == 0


# ---------------------------------------------------------------------------
# 3. Second page — tail prepended, exact string equality
# ---------------------------------------------------------------------------

def test_second_page_is_tail_newline_page(stitcher):
    stitcher.stitch(0, PAGE0)
    stitched, _ = stitcher.stitch(1, PAGE1)
    expected = PAGE0_TAIL_TEXT + "\n" + PAGE1
    assert stitched == expected


def test_second_page_starts_with_tail_text(stitcher):
    stitcher.stitch(0, PAGE0)
    stitched, _ = stitcher.stitch(1, PAGE1)
    assert stitched.startswith(PAGE0_TAIL_TEXT)


def test_second_page_ends_with_page_text(stitcher):
    stitcher.stitch(0, PAGE0)
    stitched, _ = stitcher.stitch(1, PAGE1)
    assert stitched.endswith(PAGE1)


def test_second_page_separator_is_single_newline(stitcher):
    stitcher.stitch(0, PAGE0)
    stitched, _ = stitcher.stitch(1, PAGE1)
    # tail ends at PAGE0_TAIL_LEN; next char is "\n"; then PAGE1 begins
    assert stitched[PAGE0_TAIL_LEN] == "\n"
    assert stitched[PAGE0_TAIL_LEN + 1 :] == PAGE1


# ---------------------------------------------------------------------------
# 4. tail_buffer_len computation
# ---------------------------------------------------------------------------

def test_tail_buffer_len_matches_joined_tail(stitcher):
    """tail_buffer_len must equal len("\\n".join(tail_lines))."""
    stitcher.stitch(0, PAGE0)
    _, tail_len = stitcher.stitch(1, PAGE1)
    assert tail_len == len("\n".join(["alpha", "beta", "gamma"]))
    assert tail_len == PAGE0_TAIL_LEN  # 16


def test_tail_buffer_len_single_line_tail(stitcher):
    stitcher.stitch(0, "only one line")
    _, tail_len = stitcher.stitch(1, PAGE1)
    assert tail_len == len("only one line")


def test_tail_buffer_len_five_line_tail(stitcher):
    page0 = "L1\nL2\nL3\nL4\nL5"
    stitcher.stitch(0, page0)
    _, tail_len = stitcher.stitch(1, "whatever")
    expected = len("L1\nL2\nL3\nL4\nL5")  # 14
    assert tail_len == expected


def test_tail_buffer_len_after_buffer_trimmed_to_5(stitcher):
    """When the tail has exactly 5 lines (trimmed from 6), tail_len is correct."""
    page0 = "L1\nL2\nL3\nL4\nL5\nL6"   # 6 lines → tail keeps last 5
    stitcher.stitch(0, page0)
    _, tail_len = stitcher.stitch(1, "x")
    # tail is ["L2","L3","L4","L5","L6"] → joined = "L2\nL3\nL4\nL5\nL6"
    assert tail_len == len("L2\nL3\nL4\nL5\nL6")


# ---------------------------------------------------------------------------
# 5. spans_pages logic — start_char < tail_len → cross-page
# ---------------------------------------------------------------------------

def _spans(start_char: int, tail_len: int, page_num: int) -> tuple[int, int] | None:
    """Reproduce the caller-side spans_pages rule from CLAUDE.md § 2."""
    return (page_num - 1, page_num) if start_char < tail_len else None


def test_start_char_zero_is_cross_page(stitcher):
    stitcher.stitch(0, PAGE0)
    _, tail_len = stitcher.stitch(1, PAGE1)
    assert _spans(0, tail_len, 1) == (0, 1)


def test_start_char_middle_of_tail_is_cross_page(stitcher):
    stitcher.stitch(0, PAGE0)
    _, tail_len = stitcher.stitch(1, PAGE1)
    mid = tail_len // 2
    assert _spans(mid, tail_len, 1) == (0, 1)


def test_start_char_last_char_of_tail_is_cross_page(stitcher):
    stitcher.stitch(0, PAGE0)
    _, tail_len = stitcher.stitch(1, PAGE1)
    # tail_len - 1 is the last character of the tail text
    assert _spans(tail_len - 1, tail_len, 1) == (0, 1)


# ---------------------------------------------------------------------------
# 6. spans_pages logic — start_char >= tail_len → None
# ---------------------------------------------------------------------------

def test_start_char_at_separator_newline_is_not_cross_page(stitcher):
    """The joining '\\n' is at index tail_len; strict < means it is NOT cross-page."""
    stitcher.stitch(0, PAGE0)
    _, tail_len = stitcher.stitch(1, PAGE1)
    # start_char == tail_len → 16 < 16 is False → None
    assert _spans(tail_len, tail_len, 1) is None


def test_start_char_in_current_page_is_not_cross_page(stitcher):
    stitcher.stitch(0, PAGE0)
    _, tail_len = stitcher.stitch(1, PAGE1)
    # First character of PAGE1 within stitched is at tail_len + 1
    assert _spans(tail_len + 1, tail_len, 1) is None


def test_start_char_far_into_current_page_is_not_cross_page(stitcher):
    stitcher.stitch(0, PAGE0)
    _, tail_len = stitcher.stitch(1, PAGE1)
    assert _spans(tail_len + 100, tail_len, 1) is None


@pytest.mark.parametrize("start_char,expected", [
    (0,                      (1, 2)),   # first char of tail → cross
    (PAGE0_TAIL_LEN - 1,     (1, 2)),   # last char of tail → cross
    (PAGE0_TAIL_LEN,         None),     # separator newline → same page
    (PAGE0_TAIL_LEN + 1,     None),     # first char of current page → same page
])
def test_spans_pages_boundary_parametrize(stitcher, start_char, expected):
    stitcher.stitch(0, PAGE0)          # fills tail buffer
    stitcher.stitch(1, PAGE1)          # advances one more page so we test page 2
    # Reload for page 2 to use page 1's tail
    stitcher2 = PageStitcher()
    stitcher2.stitch(1, PAGE0)         # fill tail with same PAGE0 text
    _, tail_len = stitcher2.stitch(2, PAGE1)
    result = _spans(start_char, tail_len, 2)
    assert result == expected


# ---------------------------------------------------------------------------
# 7. Buffer trimming — last 5 lines only
# ---------------------------------------------------------------------------

def test_buffer_holds_exactly_5_lines_from_6_line_page(stitcher):
    stitcher.stitch(0, "L1\nL2\nL3\nL4\nL5\nL6")
    assert stitcher.tail_buffer == ["L2", "L3", "L4", "L5", "L6"]
    assert len(stitcher.tail_buffer) == 5


def test_buffer_holds_all_lines_when_fewer_than_5(stitcher):
    stitcher.stitch(0, "L1\nL2\nL3")
    assert stitcher.tail_buffer == ["L1", "L2", "L3"]
    assert len(stitcher.tail_buffer) == 3


def test_buffer_holds_exactly_5_lines_from_exactly_5_line_page(stitcher):
    stitcher.stitch(0, "L1\nL2\nL3\nL4\nL5")
    assert stitcher.tail_buffer == ["L1", "L2", "L3", "L4", "L5"]
    assert len(stitcher.tail_buffer) == 5


def test_buffer_does_not_accumulate_across_pages(stitcher):
    """Processing two pages must not merge their lines into a >5-line buffer."""
    stitcher.stitch(0, "A\nB\nC\nD\nE")   # 5 lines → tail has 5
    stitcher.stitch(1, "F\nG\nH\nI\nJ")   # 5 more lines → tail still has 5
    buf = stitcher.tail_buffer
    assert len(buf) == 5
    assert buf == ["F", "G", "H", "I", "J"]


def test_buffer_updates_on_every_stitch(stitcher):
    stitcher.stitch(0, "page0_line1\npage0_line2")
    assert "page0_line1" in stitcher.tail_buffer

    stitcher.stitch(1, "page1_line1\npage1_line2")
    buf = stitcher.tail_buffer
    assert "page1_line1" in buf
    # Old page 0 lines must be gone once page 1 is processed
    assert "page0_line1" not in buf


def test_buffer_preserves_last_5_from_long_page(stitcher):
    lines = [f"line{i}" for i in range(20)]
    stitcher.stitch(0, "\n".join(lines))
    assert stitcher.tail_buffer == lines[-5:]


# ---------------------------------------------------------------------------
# 8. Empty page_text — buffer becomes empty
# ---------------------------------------------------------------------------

def test_empty_page_after_non_empty_clears_buffer(stitcher):
    stitcher.stitch(0, PAGE0)
    assert stitcher.tail_buffer != []
    stitcher.stitch(1, "")
    assert stitcher.tail_buffer == []


def test_empty_page_as_first_page_leaves_buffer_empty(stitcher):
    stitcher.stitch(0, "")
    assert stitcher.tail_buffer == []


def test_empty_page_stitched_text_is_tail_newline_empty(stitcher):
    """When buffer exists but page_text is empty, stitched ends with '\\n'."""
    stitcher.stitch(0, PAGE0)
    stitched, _ = stitcher.stitch(1, "")
    assert stitched == PAGE0_TAIL_TEXT + "\n"


# ---------------------------------------------------------------------------
# 9. reset() — clears buffer; next stitch behaves like first page
# ---------------------------------------------------------------------------

def test_reset_clears_tail_buffer(stitcher):
    stitcher.stitch(0, PAGE0)
    assert stitcher.tail_buffer != []
    stitcher.reset()
    assert stitcher.tail_buffer == []


def test_reset_makes_next_stitch_behave_like_first_page(stitcher):
    stitcher.stitch(0, PAGE0)
    stitcher.reset()
    stitched, tail_len = stitcher.stitch(1, PAGE1)
    assert stitched == PAGE1
    assert tail_len == 0


def test_reset_between_excel_sheets(stitcher):
    """Excel tab isolation: reset must prevent sheet A's tail leaking into sheet B."""
    stitcher.stitch(0, "Sheet A row 1\nSheet A row 2")
    stitcher.reset()                          # simulate new worksheet
    stitched, tail_len = stitcher.stitch(0, "Sheet B row 1")
    assert tail_len == 0
    assert "Sheet A" not in stitched


def test_reset_is_idempotent(stitcher):
    """Calling reset() twice must not raise and must leave buffer empty."""
    stitcher.reset()
    stitcher.reset()
    assert stitcher.tail_buffer == []


def test_stitch_after_reset_repopulates_buffer(stitcher):
    stitcher.stitch(0, PAGE0)
    stitcher.reset()
    stitcher.stitch(1, "new\nlines")
    assert stitcher.tail_buffer == ["new", "lines"]


# ---------------------------------------------------------------------------
# 10. tail_buffer property — returns a copy, not the internal list
# ---------------------------------------------------------------------------

def test_tail_buffer_property_returns_copy(stitcher):
    stitcher.stitch(0, PAGE0)
    buf = stitcher.tail_buffer
    buf.append("injected")
    assert "injected" not in stitcher.tail_buffer


def test_tail_buffer_property_mutation_does_not_affect_stitching(stitcher):
    stitcher.stitch(0, PAGE0)
    buf = stitcher.tail_buffer
    buf.clear()                          # mutate the copy aggressively
    # Internal state must be intact: next stitch still prepends PAGE0 tail
    stitched, tail_len = stitcher.stitch(1, PAGE1)
    assert tail_len == PAGE0_TAIL_LEN    # buffer was not cleared internally
    assert stitched.startswith(PAGE0_TAIL_TEXT)


def test_tail_buffer_property_content_matches_internal(stitcher):
    stitcher.stitch(0, "L1\nL2\nL3")
    assert stitcher.tail_buffer == ["L1", "L2", "L3"]


# ---------------------------------------------------------------------------
# 11. Multi-page sequence (integration)
# ---------------------------------------------------------------------------

def test_three_page_sequence(stitcher):
    """Verify buffer evolves correctly across three consecutive pages."""
    # Page 0 — 3 lines
    s0, tl0 = stitcher.stitch(0, "A\nB\nC")
    assert s0 == "A\nB\nC" and tl0 == 0
    assert stitcher.tail_buffer == ["A", "B", "C"]

    # Page 1 — tail = page 0's 3 lines
    s1, tl1 = stitcher.stitch(1, "D\nE")
    assert s1 == "A\nB\nC\nD\nE"
    assert tl1 == len("A\nB\nC")        # 5
    assert stitcher.tail_buffer == ["D", "E"]

    # Page 2 — tail = page 1's 2 lines
    s2, tl2 = stitcher.stitch(2, "F")
    assert s2 == "D\nE\nF"
    assert tl2 == len("D\nE")           # 3
    assert stitcher.tail_buffer == ["F"]


def test_new_instance_starts_with_empty_buffer():
    s = PageStitcher()
    assert s.tail_buffer == []
