"""Tests for app/readers/docx_reader.py.

Covers:
- Paragraphs emitted as prose blocks
- Empty paragraphs skipped
- Table first row emitted as table_header
- Table data rows emitted as table_cell
- Empty data cells skipped
- All cells in a table share the same table_id
- col_header attached to data cells from header row
- bbox is always None
- page_or_sheet is always 0
- file_type is "docx"
- Empty document returns empty list
- Multiple tables each get distinct table_id
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub python-docx before any project import
# ---------------------------------------------------------------------------
_DOCX_STUB = MagicMock(name="docx_stub")
sys.modules.setdefault("docx", _DOCX_STUB)

from app.readers.docx_reader import DOCXReader  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers to build mock python-docx objects
# ---------------------------------------------------------------------------

def _para(text: str) -> MagicMock:
    p = MagicMock()
    p.text = text
    return p


def _cell(text: str) -> MagicMock:
    c = MagicMock()
    c.text = text
    return c


def _row(cells: list[str]) -> MagicMock:
    r = MagicMock()
    r.cells = [_cell(t) for t in cells]
    return r


def _table(rows: list[list[str]]) -> MagicMock:
    t = MagicMock()
    t.rows = [_row(cells) for cells in rows]
    return t


def _make_doc(paragraphs: list[str], tables: list[list[list[str]]] | None = None) -> MagicMock:
    doc = MagicMock()
    doc.paragraphs = [_para(p) for p in paragraphs]
    doc.tables = [_table(rows) for rows in (tables or [])]
    return doc


def _run(paragraphs: list[str], tables: list[list[list[str]]] | None = None):
    mock_doc = _make_doc(paragraphs, tables)
    with patch("app.readers.docx_reader.docx") as mock_docx:
        mock_docx.Document.return_value = mock_doc
        reader = DOCXReader("test.docx")
        return reader.read()


# ---------------------------------------------------------------------------
# Paragraphs
# ---------------------------------------------------------------------------

def test_single_paragraph_emitted_as_prose():
    blocks = _run(["Hello world"])
    assert len(blocks) == 1
    assert blocks[0].text == "Hello world"
    assert blocks[0].block_type == "prose"


def test_empty_paragraph_skipped():
    blocks = _run(["Hello", "   ", ""])
    assert len(blocks) == 1
    assert blocks[0].text == "Hello"


def test_multiple_paragraphs_all_emitted():
    blocks = _run(["First", "Second", "Third"])
    assert len(blocks) == 3
    texts = [b.text for b in blocks]
    assert texts == ["First", "Second", "Third"]


def test_paragraph_block_file_type_is_docx():
    blocks = _run(["text"])
    assert blocks[0].file_type == "docx"


def test_paragraph_block_page_or_sheet_is_0():
    blocks = _run(["text"])
    assert blocks[0].page_or_sheet == 0


def test_paragraph_block_bbox_is_none():
    blocks = _run(["text"])
    assert blocks[0].bbox is None


# ---------------------------------------------------------------------------
# Tables — header row
# ---------------------------------------------------------------------------

def test_table_header_row_emitted_as_table_header():
    blocks = _run([], [
        [["Name", "SSN"], ["Alice", "123-45-6789"]],
    ])
    header_blocks = [b for b in blocks if b.block_type == "table_header"]
    assert len(header_blocks) == 2
    assert header_blocks[0].text == "Name"
    assert header_blocks[1].text == "SSN"


def test_table_header_block_has_col_header_equal_to_text():
    blocks = _run([], [[["Email", "Phone"]]])
    for b in blocks:
        if b.block_type == "table_header":
            assert b.col_header == b.text


def test_table_data_row_emitted_as_table_cell():
    blocks = _run([], [[["Col"], ["value"]]])
    data_blocks = [b for b in blocks if b.block_type == "table_cell"]
    assert len(data_blocks) == 1
    assert data_blocks[0].text == "value"


def test_empty_data_cell_skipped():
    blocks = _run([], [[["A", "B"], ["val1", ""]]])
    data_blocks = [b for b in blocks if b.block_type == "table_cell"]
    assert len(data_blocks) == 1
    assert data_blocks[0].text == "val1"


# ---------------------------------------------------------------------------
# Tables — col_header attached to data cells
# ---------------------------------------------------------------------------

def test_col_header_attached_to_data_cells():
    blocks = _run([], [[["Name", "SSN"], ["Alice", "123-45-6789"]]])
    data_blocks = [b for b in blocks if b.block_type == "table_cell"]
    by_text = {b.text: b for b in data_blocks}
    assert by_text["Alice"].col_header == "Name"
    assert by_text["123-45-6789"].col_header == "SSN"


# ---------------------------------------------------------------------------
# Tables — shared table_id within a table
# ---------------------------------------------------------------------------

def test_all_cells_in_table_share_table_id():
    blocks = _run([], [[["A"], ["v1"], ["v2"]]])
    table_blocks = [b for b in blocks if b.table_id is not None]
    ids = {b.table_id for b in table_blocks}
    assert len(ids) == 1


def test_multiple_tables_have_distinct_table_ids():
    blocks = _run([], [
        [["Col1"], ["r1"]],
        [["Col2"], ["r2"]],
    ])
    table_ids = [b.table_id for b in blocks if b.table_id is not None]
    unique_ids = set(table_ids)
    assert len(unique_ids) == 2


# ---------------------------------------------------------------------------
# Tables — metadata
# ---------------------------------------------------------------------------

def test_table_cell_bbox_is_none():
    blocks = _run([], [[["H"], ["v"]]])
    for b in blocks:
        assert b.bbox is None


def test_table_cell_page_or_sheet_is_0():
    blocks = _run([], [[["H"], ["v"]]])
    for b in blocks:
        assert b.page_or_sheet == 0


def test_table_cell_file_type_is_docx():
    blocks = _run([], [[["H"], ["v"]]])
    for b in blocks:
        assert b.file_type == "docx"


def test_row_and_column_set_on_table_cells():
    blocks = _run([], [[["A", "B"], ["r1c1", "r1c2"]]])
    data_blocks = {(b.row, b.column): b for b in blocks if b.block_type == "table_cell"}
    assert (1, 0) in data_blocks
    assert (1, 1) in data_blocks


# ---------------------------------------------------------------------------
# Empty document
# ---------------------------------------------------------------------------

def test_empty_document_returns_empty_list():
    blocks = _run([], [])
    assert blocks == []


# ---------------------------------------------------------------------------
# Paragraphs and tables combined
# ---------------------------------------------------------------------------

def test_paragraphs_and_tables_both_emitted():
    blocks = _run(["Intro text"], [[["Header"], ["data"]]])
    prose = [b for b in blocks if b.block_type == "prose"]
    tables = [b for b in blocks if b.block_type in ("table_header", "table_cell")]
    assert len(prose) == 1
    assert len(tables) == 2


# ---------------------------------------------------------------------------
# source_path stored
# ---------------------------------------------------------------------------

def test_source_path_stored_on_blocks():
    blocks = _run(["text"])
    assert blocks[0].source_path == "test.docx"
