"""Tests for app/readers/csv_reader.py.

Covers:
- Table header blocks emitted once from column names
- Data cells emitted as table_cell blocks
- Empty cells skipped
- All blocks share a single table_id
- col_header attached to all data cells
- bbox is None for all blocks
- page_or_sheet is 0 for all blocks
- file_type is "csv"
- CHUNK_SIZE constant is 1000
- Multiple chunks produce correct blocks (no duplicate headers)
- Empty CSV returns only header blocks (no data)
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Stub pandas before project imports
# ---------------------------------------------------------------------------
_PANDAS_STUB = MagicMock(name="pandas_stub")
sys.modules.setdefault("pandas", _PANDAS_STUB)

from app.readers.csv_reader import CSVReader, CHUNK_SIZE  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_chunk(columns: list[str], rows: list[list[str]]) -> MagicMock:
    """Build a mock pandas DataFrame chunk."""
    import pandas as pd  # this is the stub
    chunk = MagicMock()
    chunk.columns = columns
    # iterrows → list of (index, row) tuples
    mock_rows = []
    for row_data in rows:
        row = MagicMock()
        row.__getitem__ = lambda self, key, _data=dict(zip(columns, row_data)): _data[key]
        mock_rows.append((0, row))
    chunk.iterrows.return_value = iter(mock_rows)
    return chunk


def _run(chunks: list[MagicMock], path: str = "data.csv"):
    import pandas as pd  # stub
    pd.isna.side_effect = lambda v: v == "" or v is None
    pd.read_csv.return_value = iter(chunks)
    reader = CSVReader(path)
    with patch("app.readers.csv_reader.pd", pd):
        return reader.read()


# ---------------------------------------------------------------------------
# CHUNK_SIZE constant
# ---------------------------------------------------------------------------

def test_chunk_size_is_1000():
    assert CHUNK_SIZE == 1_000


# ---------------------------------------------------------------------------
# Header blocks
# ---------------------------------------------------------------------------

def test_header_blocks_emitted_once():
    chunk = _make_chunk(["Name", "SSN"], [["Alice", "123-45-6789"]])
    blocks = _run([chunk])
    headers = [b for b in blocks if b.block_type == "table_header"]
    assert len(headers) == 2


def test_header_block_text_equals_column_name():
    chunk = _make_chunk(["Name", "Email"], [])
    blocks = _run([chunk])
    headers = [b for b in blocks if b.block_type == "table_header"]
    texts = [b.text for b in headers]
    assert "Name" in texts
    assert "Email" in texts


def test_headers_emitted_only_once_across_multiple_chunks():
    c1 = _make_chunk(["A", "B"], [["a1", "b1"]])
    c2 = _make_chunk(["A", "B"], [["a2", "b2"]])
    blocks = _run([c1, c2])
    headers = [b for b in blocks if b.block_type == "table_header"]
    assert len(headers) == 2


def test_header_block_type_is_table_header():
    chunk = _make_chunk(["Col"], [])
    blocks = _run([chunk])
    headers = [b for b in blocks if b.block_type == "table_header"]
    assert all(b.block_type == "table_header" for b in headers)


def test_header_col_header_equals_text():
    chunk = _make_chunk(["Name"], [])
    blocks = _run([chunk])
    for b in blocks:
        if b.block_type == "table_header":
            assert b.col_header == b.text


# ---------------------------------------------------------------------------
# Data cells
# ---------------------------------------------------------------------------

def test_data_cells_emitted_as_table_cell():
    chunk = _make_chunk(["Name"], [["Alice"]])
    blocks = _run([chunk])
    data = [b for b in blocks if b.block_type == "table_cell"]
    assert len(data) == 1
    assert data[0].text == "Alice"


def test_empty_cell_skipped():
    import pandas as pd
    pd.isna.side_effect = lambda v: v == "" or v is None
    chunk = _make_chunk(["A", "B"], [["hello", ""]])
    blocks = _run([chunk])
    data = [b for b in blocks if b.block_type == "table_cell"]
    # "hello" emitted, "" skipped
    assert len(data) == 1
    assert data[0].text == "hello"


def test_col_header_attached_to_data_cells():
    chunk = _make_chunk(["SSN"], [["123-45-6789"]])
    blocks = _run([chunk])
    data = [b for b in blocks if b.block_type == "table_cell"]
    assert data[0].col_header == "SSN"


# ---------------------------------------------------------------------------
# Shared table_id
# ---------------------------------------------------------------------------

def test_all_blocks_share_single_table_id():
    chunk = _make_chunk(["A", "B"], [["v1", "v2"]])
    blocks = _run([chunk])
    ids = {b.table_id for b in blocks if b.table_id is not None}
    assert len(ids) == 1


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def test_bbox_is_none_for_all_blocks():
    chunk = _make_chunk(["Col"], [["val"]])
    blocks = _run([chunk])
    for b in blocks:
        assert b.bbox is None


def test_page_or_sheet_is_0():
    chunk = _make_chunk(["Col"], [["val"]])
    blocks = _run([chunk])
    for b in blocks:
        assert b.page_or_sheet == 0


def test_file_type_is_csv():
    chunk = _make_chunk(["Col"], [["val"]])
    blocks = _run([chunk])
    for b in blocks:
        assert b.file_type == "csv"


def test_source_path_stored():
    chunk = _make_chunk(["Col"], [["val"]])
    blocks = _run([chunk], path="mydata.csv")
    for b in blocks:
        assert b.source_path == "mydata.csv"


# ---------------------------------------------------------------------------
# Multiple chunks
# ---------------------------------------------------------------------------

def test_data_from_multiple_chunks_all_emitted():
    c1 = _make_chunk(["Name"], [["Alice"]])
    c2 = _make_chunk(["Name"], [["Bob"]])
    blocks = _run([c1, c2])
    data = [b for b in blocks if b.block_type == "table_cell"]
    texts = [b.text for b in data]
    assert "Alice" in texts
    assert "Bob" in texts


# ---------------------------------------------------------------------------
# Empty CSV (no data rows)
# ---------------------------------------------------------------------------

def test_empty_csv_returns_only_header_blocks():
    chunk = _make_chunk(["A", "B"], [])
    blocks = _run([chunk])
    assert all(b.block_type == "table_header" for b in blocks)
    assert len(blocks) == 2
