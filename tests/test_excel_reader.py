"""Tests for app/readers/excel_reader.py.

openpyxl is injected into sys.modules before the module is imported so that
`import openpyxl` at the top of excel_reader.py succeeds without the package
being installed.  PageStitcher is patched per-test so reset() call counts can
be asserted precisely.

Covers:
  - Single sheet: headers (table_header) + data (table_cell) blocks
  - Hidden sheet skipped entirely (no blocks, no stitcher.reset())
  - Empty sheet (no rows / all-None) skipped entirely
  - page_or_sheet == sheet name (str) on every block
  - table_id shared within a sheet, distinct across sheets
  - stitcher.reset() called exactly once per visible sheet
  - _is_structured_id_column() unit tests (>80% threshold)
  - Flagged column: col_header prefixed with "[REVIEW] "
  - bbox=None on every block
  - load_workbook called with read_only=True, data_only=True
  - file_type derived from path extension
  - row / column / row_index provenance on blocks
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, Mock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub openpyxl in sys.modules BEFORE importing the module under test
# ---------------------------------------------------------------------------
_OPENPYXL_STUB = MagicMock(name="openpyxl_stub")
sys.modules.setdefault("openpyxl", _OPENPYXL_STUB)

from app.readers.excel_reader import ExcelReader, _STRUCTURED_ID_PATTERNS  # noqa: E402


# ---------------------------------------------------------------------------
# Mock cell / sheet / workbook helpers
# ---------------------------------------------------------------------------

def _cell(value, row: int, col: int) -> MagicMock:
    c = MagicMock()
    c.value = value
    c.row = row
    c.column = col
    return c


def _make_ws(rows_data: list[list], sheet_state: str = "visible") -> MagicMock:
    """Build a mock openpyxl worksheet.

    rows_data is a list-of-lists; each inner list is the cell values for that
    row (1-indexed).  Cell .row and .column are set automatically.
    """
    ws = MagicMock()
    ws.sheet_state = sheet_state
    cell_rows = [
        [_cell(val, row_idx, col_idx)
         for col_idx, val in enumerate(row, start=1)]
        for row_idx, row in enumerate(rows_data, start=1)
    ]
    ws.iter_rows.return_value = cell_rows
    return ws


def _make_wb(sheets: dict[str, MagicMock]) -> MagicMock:
    """Build a mock openpyxl Workbook."""
    wb = MagicMock()
    wb.sheetnames = list(sheets.keys())
    wb.__getitem__.side_effect = lambda name: sheets[name]
    wb.close = Mock()
    return wb


def _run(
    wb: MagicMock,
    path: str = "test.xlsx",
    patch_stitcher: bool = True,
) -> tuple[list, MagicMock | None]:
    """Run ExcelReader.read() against a mock workbook; return (blocks, mock_stitcher_instance)."""
    mock_stitcher_instance = MagicMock()
    with (
        patch("app.readers.excel_reader.openpyxl") as mock_openpyxl,
        patch("app.readers.excel_reader.PageStitcher", return_value=mock_stitcher_instance)
        if patch_stitcher
        else patch("builtins.open"),  # dummy — never reached
    ):
        mock_openpyxl.load_workbook.return_value = wb
        reader = ExcelReader(path)
        blocks = reader.read()
        if patch_stitcher:
            return blocks, mock_stitcher_instance, mock_openpyxl
    return blocks, None, None  # unreachable but makes type checker happy


def _run_reader(wb, path="test.xlsx"):
    """Convenience wrapper; returns (blocks, stitcher_mock, openpyxl_mock)."""
    return _run(wb, path=path, patch_stitcher=True)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

SIMPLE_ROWS = [
    ["Name", "Age"],    # row 1 — headers
    ["Alice", 30],      # row 2 — data
    ["Bob", 25],        # row 3 — data
]


# ---------------------------------------------------------------------------
# 1. Single sheet — block count, types, and content
# ---------------------------------------------------------------------------

def test_single_sheet_total_block_count():
    wb = _make_wb({"Sheet1": _make_ws(SIMPLE_ROWS)})
    blocks, _, _ = _run_reader(wb)
    # 2 headers + 2×2 data cells = 6
    assert len(blocks) == 6


def test_header_blocks_are_table_header():
    wb = _make_wb({"Sheet1": _make_ws(SIMPLE_ROWS)})
    blocks, _, _ = _run_reader(wb)
    headers = [b for b in blocks if b.block_type == "table_header"]
    assert len(headers) == 2


def test_data_blocks_are_table_cell():
    wb = _make_wb({"Sheet1": _make_ws(SIMPLE_ROWS)})
    blocks, _, _ = _run_reader(wb)
    cells = [b for b in blocks if b.block_type == "table_cell"]
    assert len(cells) == 4


def test_header_block_text_values():
    wb = _make_wb({"Sheet1": _make_ws(SIMPLE_ROWS)})
    blocks, _, _ = _run_reader(wb)
    header_texts = {b.text for b in blocks if b.block_type == "table_header"}
    assert header_texts == {"Name", "Age"}


def test_data_block_text_values():
    wb = _make_wb({"Sheet1": _make_ws(SIMPLE_ROWS)})
    blocks, _, _ = _run_reader(wb)
    cell_texts = {b.text for b in blocks if b.block_type == "table_cell"}
    assert cell_texts == {"Alice", "30", "Bob", "25"}


def test_none_cell_value_becomes_empty_string():
    ws = _make_ws([["Col"], [None]])
    wb = _make_wb({"S1": ws})
    blocks, _, _ = _run_reader(wb)
    cell = next(b for b in blocks if b.block_type == "table_cell")
    assert cell.text == ""


def test_header_col_header_equals_text():
    wb = _make_wb({"Sheet1": _make_ws(SIMPLE_ROWS)})
    blocks, _, _ = _run_reader(wb)
    for b in blocks:
        if b.block_type == "table_header":
            assert b.col_header == b.text


def test_data_cell_col_header_matches_header_row():
    wb = _make_wb({"Sheet1": _make_ws(SIMPLE_ROWS)})
    blocks, _, _ = _run_reader(wb)
    alice = next(b for b in blocks if b.block_type == "table_cell" and b.text == "Alice")
    assert alice.col_header == "Name"
    age_cell = next(b for b in blocks if b.block_type == "table_cell" and b.text == "30")
    assert age_cell.col_header == "Age"


# ---------------------------------------------------------------------------
# 2. Hidden sheet skipped entirely
# ---------------------------------------------------------------------------

def test_hidden_sheet_produces_no_blocks():
    ws = _make_ws(SIMPLE_ROWS, sheet_state="hidden")
    wb = _make_wb({"HiddenSheet": ws})
    blocks, _, _ = _run_reader(wb)
    assert blocks == []


def test_very_hidden_sheet_produces_no_blocks():
    ws = _make_ws(SIMPLE_ROWS, sheet_state="veryHidden")
    wb = _make_wb({"VeryHidden": ws})
    blocks, _, _ = _run_reader(wb)
    assert blocks == []


def test_hidden_sheet_stitcher_reset_not_called():
    ws = _make_ws(SIMPLE_ROWS, sheet_state="hidden")
    wb = _make_wb({"Hidden": ws})
    _, stitcher_mock, _ = _run_reader(wb)
    stitcher_mock.reset.assert_not_called()


def test_visible_and_hidden_sheets_only_visible_blocks():
    visible_ws = _make_ws(SIMPLE_ROWS)
    hidden_ws = _make_ws([["Secret", "Data"], ["x", "y"]], sheet_state="hidden")
    wb = _make_wb({"Visible": visible_ws, "Hidden": hidden_ws})
    blocks, _, _ = _run_reader(wb)
    assert all(b.page_or_sheet == "Visible" for b in blocks)


# ---------------------------------------------------------------------------
# 3. Empty sheet skipped entirely
# ---------------------------------------------------------------------------

def test_no_rows_sheet_produces_no_blocks():
    ws = _make_ws([])   # iter_rows returns []
    wb = _make_wb({"Empty": ws})
    blocks, _, _ = _run_reader(wb)
    assert blocks == []


def test_all_none_sheet_produces_no_blocks():
    ws = _make_ws([[None, None], [None, None]])
    wb = _make_wb({"AllNone": ws})
    blocks, _, _ = _run_reader(wb)
    assert blocks == []


def test_empty_sheet_after_non_empty_does_not_affect_result():
    non_empty_ws = _make_ws(SIMPLE_ROWS)
    empty_ws = _make_ws([])
    wb = _make_wb({"Real": non_empty_ws, "Empty": empty_ws})
    blocks, _, _ = _run_reader(wb)
    assert all(b.page_or_sheet == "Real" for b in blocks)


# ---------------------------------------------------------------------------
# 4. page_or_sheet == sheet name (str) on every block
# ---------------------------------------------------------------------------

def test_page_or_sheet_is_sheet_name_string():
    wb = _make_wb({"MySheet": _make_ws(SIMPLE_ROWS)})
    blocks, _, _ = _run_reader(wb)
    assert all(b.page_or_sheet == "MySheet" for b in blocks)


def test_page_or_sheet_is_string_not_int():
    wb = _make_wb({"Sheet1": _make_ws(SIMPLE_ROWS)})
    blocks, _, _ = _run_reader(wb)
    assert all(isinstance(b.page_or_sheet, str) for b in blocks)


def test_page_or_sheet_preserved_across_two_sheets():
    ws1 = _make_ws(SIMPLE_ROWS)
    ws2 = _make_ws([["X"], [1]])
    wb = _make_wb({"Alpha": ws1, "Beta": ws2})
    blocks, _, _ = _run_reader(wb)
    names = {b.page_or_sheet for b in blocks}
    assert names == {"Alpha", "Beta"}


# ---------------------------------------------------------------------------
# 5. table_id shared within sheet, distinct across sheets
# ---------------------------------------------------------------------------

def test_table_id_shared_within_single_sheet():
    wb = _make_wb({"Sheet1": _make_ws(SIMPLE_ROWS)})
    blocks, _, _ = _run_reader(wb)
    ids = {b.table_id for b in blocks}
    assert len(ids) == 1


def test_table_id_is_uuid_string():
    import re
    wb = _make_wb({"Sheet1": _make_ws(SIMPLE_ROWS)})
    blocks, _, _ = _run_reader(wb)
    tid = blocks[0].table_id
    assert re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", tid
    )


def test_table_id_distinct_across_two_sheets():
    ws1 = _make_ws(SIMPLE_ROWS)
    ws2 = _make_ws([["A"], [1]])
    wb = _make_wb({"S1": ws1, "S2": ws2})
    blocks, _, _ = _run_reader(wb)
    s1_ids = {b.table_id for b in blocks if b.page_or_sheet == "S1"}
    s2_ids = {b.table_id for b in blocks if b.page_or_sheet == "S2"}
    assert s1_ids and s2_ids
    assert s1_ids.isdisjoint(s2_ids)


def test_table_id_not_none_on_any_block():
    wb = _make_wb({"Sheet1": _make_ws(SIMPLE_ROWS)})
    blocks, _, _ = _run_reader(wb)
    assert all(b.table_id is not None for b in blocks)


# ---------------------------------------------------------------------------
# 6. stitcher.reset() called exactly once per visible, non-hidden sheet
# ---------------------------------------------------------------------------

def test_stitcher_reset_called_once_for_single_sheet():
    wb = _make_wb({"S1": _make_ws(SIMPLE_ROWS)})
    _, stitcher_mock, _ = _run_reader(wb)
    stitcher_mock.reset.assert_called_once()


def test_stitcher_reset_called_for_each_visible_sheet():
    ws1 = _make_ws(SIMPLE_ROWS)
    ws2 = _make_ws([["X"], [1]])
    ws3 = _make_ws([["Y"], [2]])
    wb = _make_wb({"A": ws1, "B": ws2, "C": ws3})
    _, stitcher_mock, _ = _run_reader(wb)
    assert stitcher_mock.reset.call_count == 3


def test_stitcher_reset_not_called_for_hidden_sheet():
    visible = _make_ws(SIMPLE_ROWS)
    hidden = _make_ws(SIMPLE_ROWS, sheet_state="hidden")
    wb = _make_wb({"V": visible, "H": hidden})
    _, stitcher_mock, _ = _run_reader(wb)
    assert stitcher_mock.reset.call_count == 1


def test_stitcher_reset_called_for_empty_visible_sheet():
    """reset() is called before every visible sheet, even if it turns out empty."""
    visible_empty = _make_ws([])
    wb = _make_wb({"Empty": visible_empty})
    _, stitcher_mock, _ = _run_reader(wb)
    stitcher_mock.reset.assert_called_once()


# ---------------------------------------------------------------------------
# 7. _is_structured_id_column unit tests
# ---------------------------------------------------------------------------

# Use a real ExcelReader instance (no workbook needed for the method)
@pytest.fixture
def reader():
    with patch("app.readers.excel_reader.openpyxl"):
        return ExcelReader("test.xlsx")


def test_is_structured_id_all_match(reader):
    values = ["123-45-6789"] * 5
    assert reader._is_structured_id_column(values, r"\d{3}-\d{2}-\d{4}") is True


def test_is_structured_id_90_percent_match(reader):
    # 9/10 = 90% > 80%
    values = ["123-45-6789"] * 9 + ["not-a-ssn"]
    assert reader._is_structured_id_column(values, r"\d{3}-\d{2}-\d{4}") is True


def test_is_structured_id_exactly_80_is_not_flagged(reader):
    # 4/5 = 80.0% which is NOT > 80%
    values = ["123-45-6789"] * 4 + ["text"]
    assert reader._is_structured_id_column(values, r"\d{3}-\d{2}-\d{4}") is False


def test_is_structured_id_below_80_not_flagged(reader):
    # 3/5 = 60% < 80%
    values = ["123-45-6789"] * 3 + ["text", "more text"]
    assert reader._is_structured_id_column(values, r"\d{3}-\d{2}-\d{4}") is False


def test_is_structured_id_empty_list(reader):
    assert reader._is_structured_id_column([], r"\d{3}-\d{2}-\d{4}") is False


def test_is_structured_id_all_whitespace(reader):
    assert reader._is_structured_id_column(["  ", ""], r"\d{3}-\d{2}-\d{4}") is False


def test_is_structured_id_none_match(reader):
    values = ["Alice", "Bob", "Charlie"]
    assert reader._is_structured_id_column(values, r"\d{3}-\d{2}-\d{4}") is False


def test_is_structured_id_case_insensitive(reader):
    # Product-ID pattern [A-Z]{2,3}-\d{4,} — test with lowercase
    values = ["ab-12345"] * 5
    assert reader._is_structured_id_column(values, r"[A-Z]{2,3}-\d{4,}") is True


def test_is_structured_id_long_numeric(reader):
    values = ["123456789"] * 9 + ["short"]
    assert reader._is_structured_id_column(values, r"\d{6,}") is True


def test_is_structured_id_strips_whitespace(reader):
    """Leading/trailing whitespace in values must not prevent matching."""
    values = [" 123-45-6789 "] * 9 + ["text"]
    assert reader._is_structured_id_column(values, r"\d{3}-\d{2}-\d{4}") is True


# ---------------------------------------------------------------------------
# 8. False-positive guard — [REVIEW] prefix on flagged column cells
# ---------------------------------------------------------------------------

def test_flagged_column_cells_get_review_prefix():
    # 9 out of 10 data values match SSN pattern → column flagged
    ssn_values = ["123-45-6789"] * 9 + ["other"]
    rows = [["SSN", "Name"]] + [[v, f"Person{i}"] for i, v in enumerate(ssn_values)]
    wb = _make_wb({"Sheet1": _make_ws(rows)})
    blocks, _, _ = _run_reader(wb)
    ssn_cells = [
        b for b in blocks
        if b.block_type == "table_cell" and b.column == 1
    ]
    assert all(b.col_header.startswith("[REVIEW] ") for b in ssn_cells)


def test_flagged_column_review_prefix_includes_original_header():
    ssn_values = ["123-45-6789"] * 9 + ["other"]
    rows = [["SSN", "Name"]] + [[v, f"P{i}"] for i, v in enumerate(ssn_values)]
    wb = _make_wb({"Sheet1": _make_ws(rows)})
    blocks, _, _ = _run_reader(wb)
    ssn_cell = next(
        b for b in blocks if b.block_type == "table_cell" and b.column == 1
    )
    assert ssn_cell.col_header == "[REVIEW] SSN"


def test_non_flagged_column_no_review_prefix():
    ssn_values = ["123-45-6789"] * 9 + ["other"]
    rows = [["SSN", "Name"]] + [[v, f"Person{i}"] for i, v in enumerate(ssn_values)]
    wb = _make_wb({"Sheet1": _make_ws(rows)})
    blocks, _, _ = _run_reader(wb)
    name_cells = [
        b for b in blocks if b.block_type == "table_cell" and b.column == 2
    ]
    assert all("[REVIEW]" not in b.col_header for b in name_cells)


def test_header_blocks_not_given_review_prefix():
    """Header row blocks themselves are never prefixed."""
    ssn_values = ["123-45-6789"] * 9 + ["other"]
    rows = [["SSN"]] + [[v] for v in ssn_values]
    wb = _make_wb({"Sheet1": _make_ws(rows)})
    blocks, _, _ = _run_reader(wb)
    header = next(b for b in blocks if b.block_type == "table_header")
    assert header.col_header == "SSN"
    assert not header.col_header.startswith("[REVIEW]")


def test_unflagged_80_percent_column_no_review():
    # Exactly 80% (= not >) → NOT flagged
    ssn_values = ["123-45-6789"] * 4 + ["text"]
    rows = [["SSN"]] + [[v] for v in ssn_values]
    wb = _make_wb({"Sheet1": _make_ws(rows)})
    blocks, _, _ = _run_reader(wb)
    cells = [b for b in blocks if b.block_type == "table_cell"]
    assert all("[REVIEW]" not in (b.col_header or "") for b in cells)


# ---------------------------------------------------------------------------
# 9. bbox=None on all blocks
# ---------------------------------------------------------------------------

def test_all_blocks_have_none_bbox():
    wb = _make_wb({"Sheet1": _make_ws(SIMPLE_ROWS)})
    blocks, _, _ = _run_reader(wb)
    assert all(b.bbox is None for b in blocks)


# ---------------------------------------------------------------------------
# 10. load_workbook called with read_only=True, data_only=True
# ---------------------------------------------------------------------------

def test_load_workbook_read_only_true():
    wb = _make_wb({"S": _make_ws(SIMPLE_ROWS)})
    _, _, mock_openpyxl = _run_reader(wb)
    _, kwargs = mock_openpyxl.load_workbook.call_args
    assert kwargs.get("read_only") is True


def test_load_workbook_data_only_true():
    wb = _make_wb({"S": _make_ws(SIMPLE_ROWS)})
    _, _, mock_openpyxl = _run_reader(wb)
    _, kwargs = mock_openpyxl.load_workbook.call_args
    assert kwargs.get("data_only") is True


def test_load_workbook_called_with_path_string():
    wb = _make_wb({"S": _make_ws(SIMPLE_ROWS)})
    _, _, mock_openpyxl = _run_reader(wb, path="invoices.xlsx")
    args, _ = mock_openpyxl.load_workbook.call_args
    assert args[0] == "invoices.xlsx"


def test_workbook_is_closed_after_read():
    wb = _make_wb({"S": _make_ws(SIMPLE_ROWS)})
    _run_reader(wb)
    wb.close.assert_called_once()


# ---------------------------------------------------------------------------
# 11. file_type derived from extension
# ---------------------------------------------------------------------------

def test_file_type_xlsx():
    wb = _make_wb({"S": _make_ws(SIMPLE_ROWS)})
    blocks, _, _ = _run_reader(wb, path="data.xlsx")
    assert all(b.file_type == "xlsx" for b in blocks)


def test_file_type_xls():
    wb = _make_wb({"S": _make_ws(SIMPLE_ROWS)})
    blocks, _, _ = _run_reader(wb, path="legacy.xls")
    assert all(b.file_type == "xls" for b in blocks)


# ---------------------------------------------------------------------------
# 12. Provenance fields: row, column, row_index
# ---------------------------------------------------------------------------

def test_header_row_is_1():
    wb = _make_wb({"S": _make_ws(SIMPLE_ROWS)})
    blocks, _, _ = _run_reader(wb)
    headers = [b for b in blocks if b.block_type == "table_header"]
    assert all(b.row == 1 for b in headers)


def test_header_row_index_is_0():
    wb = _make_wb({"S": _make_ws(SIMPLE_ROWS)})
    blocks, _, _ = _run_reader(wb)
    headers = [b for b in blocks if b.block_type == "table_header"]
    assert all(b.row_index == 0 for b in headers)


def test_data_row_2_row_index_is_1():
    wb = _make_wb({"S": _make_ws(SIMPLE_ROWS)})
    blocks, _, _ = _run_reader(wb)
    row2 = [b for b in blocks if b.block_type == "table_cell" and b.row == 2]
    assert all(b.row_index == 1 for b in row2)


def test_column_numbers_are_1_based():
    wb = _make_wb({"S": _make_ws(SIMPLE_ROWS)})
    blocks, _, _ = _run_reader(wb)
    first_col = [b for b in blocks if b.column == 1]
    second_col = [b for b in blocks if b.column == 2]
    assert first_col and second_col


def test_structured_id_patterns_constant_is_nonempty():
    assert isinstance(_STRUCTURED_ID_PATTERNS, list)
    assert len(_STRUCTURED_ID_PATTERNS) > 0
