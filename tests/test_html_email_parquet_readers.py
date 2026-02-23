"""Tests for app/readers/html_reader.py, email_reader.py, parquet_reader.py.

HTML reader covers:
- Visible text extracted as prose blocks
- Script and style tags removed
- Empty lines skipped
- bbox is None, page_or_sheet is 0
- file_type derived from extension

Email reader covers:
- Plain-text body extracted as prose blocks
- HTML body stripped via BeautifulSoup and extracted
- Attachments skipped (Content-Disposition: attachment)
- MIME part index stored as page_or_sheet
- Empty/null payload returns no blocks

Parquet reader covers:
- Cells emitted as table_cell blocks per row-group
- Null values skipped
- col_header from schema column names
- page_or_sheet is the row-group index
- bbox is None
- file_type derived from extension
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub bs4 and pyarrow.parquet before any project import
# ---------------------------------------------------------------------------
for _mod in ("bs4", "pyarrow", "pyarrow.parquet"):
    sys.modules.setdefault(_mod, MagicMock())

from app.readers.html_reader import HTMLReader  # noqa: E402
from app.readers.email_reader import EmailReader  # noqa: E402
from app.readers.parquet_reader import ParquetReader  # noqa: E402


# ===========================================================================
# HTMLReader
# ===========================================================================

def _run_html(visible_lines: list[str], filename: str = "page.html") -> list:
    """Run HTMLReader with BeautifulSoup returning specific visible text."""
    visible_text = "\n".join(visible_lines)
    mock_soup = MagicMock()
    mock_soup.return_value = []          # soup([...]) → empty, nothing to decompose
    mock_soup.get_text.return_value = visible_text

    with (
        patch("app.readers.html_reader.BeautifulSoup", return_value=mock_soup),
        patch("pathlib.Path.read_text", return_value="<html>stub</html>"),
    ):
        reader = HTMLReader(filename)
        return reader.read()


class TestHTMLReader:

    def test_text_lines_become_prose_blocks(self):
        blocks = _run_html(["Hello World", "Second line"])
        texts = [b.text for b in blocks]
        assert "Hello World" in texts
        assert "Second line" in texts

    def test_empty_lines_skipped(self):
        blocks = _run_html(["Line1", "", "  ", "Line2"])
        assert len(blocks) == 2

    def test_block_type_is_prose(self):
        blocks = _run_html(["Some text"])
        assert all(b.block_type == "prose" for b in blocks)

    def test_bbox_is_none(self):
        blocks = _run_html(["text"])
        for b in blocks:
            assert b.bbox is None

    def test_page_or_sheet_is_0(self):
        blocks = _run_html(["text"])
        for b in blocks:
            assert b.page_or_sheet == 0

    def test_file_type_from_extension_html(self):
        blocks = _run_html(["text"], "page.html")
        assert all(b.file_type == "html" for b in blocks)

    def test_file_type_from_extension_htm(self):
        blocks = _run_html(["text"], "page.htm")
        assert all(b.file_type == "htm" for b in blocks)

    def test_empty_html_returns_empty_list(self):
        blocks = _run_html([])
        assert blocks == []

    def test_source_path_stored(self):
        blocks = _run_html(["text"], "mypage.html")
        for b in blocks:
            assert "mypage.html" in b.source_path


# ===========================================================================
# EmailReader
# ===========================================================================

def _make_text_part(text: str, charset: str = "utf-8") -> MagicMock:
    part = MagicMock()
    part.get_content_type.return_value = "text/plain"
    part.get.return_value = ""   # no Content-Disposition
    part.get_payload.return_value = text.encode(charset)
    part.get_content_charset.return_value = charset
    return part


def _make_html_part(visible_text: str) -> MagicMock:
    part = MagicMock()
    part.get_content_type.return_value = "text/html"
    part.get.return_value = ""
    part.get_payload.return_value = f"<p>{visible_text}</p>".encode("utf-8")
    part.get_content_charset.return_value = "utf-8"
    return part


def _make_attachment_part() -> MagicMock:
    part = MagicMock()
    part.get_content_type.return_value = "application/pdf"
    part.get.return_value = "attachment; filename=doc.pdf"
    return part


def _run_email(msg: MagicMock, path: str = "msg.eml") -> list:
    """Run EmailReader with a fully mocked email message.

    BeautifulSoup is also stubbed so HTML body text comes through as-is.
    """
    mock_soup = MagicMock()
    mock_soup.return_value = []
    # get_text returns the raw text passed via the html payload
    mock_soup.get_text.side_effect = lambda sep="": "stripped html"

    with (
        patch("app.readers.email_reader._email_lib.message_from_bytes", return_value=msg),
        patch("app.readers.email_reader.BeautifulSoup", return_value=mock_soup),
        patch("pathlib.Path.read_bytes", return_value=b"stub"),
    ):
        reader = EmailReader(path)
        return reader.read()


class TestEmailReader:

    def test_plain_text_body_extracted(self):
        msg = MagicMock()
        msg.is_multipart.return_value = False
        msg.get_content_type.return_value = "text/plain"
        msg.get.return_value = ""
        msg.get_payload.return_value = b"Hello from email"
        msg.get_content_charset.return_value = "utf-8"
        blocks = _run_email(msg)
        texts = [b.text for b in blocks]
        assert "Hello from email" in texts

    def test_multipart_text_part_extracted(self):
        text_part = _make_text_part("Body content")
        msg = MagicMock()
        msg.is_multipart.return_value = True
        msg.walk.return_value = [text_part]
        blocks = _run_email(msg)
        texts = [b.text for b in blocks]
        assert "Body content" in texts

    def test_attachment_skipped(self):
        text_part = _make_text_part("Body")
        attachment = _make_attachment_part()
        msg = MagicMock()
        msg.is_multipart.return_value = True
        msg.walk.return_value = [text_part, attachment]
        blocks = _run_email(msg)
        # Only text body → all prose blocks
        assert all(b.block_type == "prose" for b in blocks)
        texts = [b.text for b in blocks]
        assert "Body" in texts

    def test_empty_payload_returns_no_blocks(self):
        msg = MagicMock()
        msg.is_multipart.return_value = False
        msg.get_content_type.return_value = "text/plain"
        msg.get.return_value = ""
        msg.get_payload.return_value = None
        msg.get_content_charset.return_value = "utf-8"
        blocks = _run_email(msg)
        assert blocks == []

    def test_page_or_sheet_is_part_index_multipart(self):
        p1 = _make_text_part("Part one")
        p2 = _make_text_part("Part two")
        msg = MagicMock()
        msg.is_multipart.return_value = True
        msg.walk.return_value = [p1, p2]
        blocks = _run_email(msg)
        indices = sorted({b.page_or_sheet for b in blocks})
        assert 0 in indices
        assert 1 in indices

    def test_block_type_is_prose(self):
        msg = MagicMock()
        msg.is_multipart.return_value = False
        msg.get_content_type.return_value = "text/plain"
        msg.get.return_value = ""
        msg.get_payload.return_value = b"text"
        msg.get_content_charset.return_value = "utf-8"
        blocks = _run_email(msg)
        assert all(b.block_type == "prose" for b in blocks)

    def test_bbox_is_none(self):
        msg = MagicMock()
        msg.is_multipart.return_value = False
        msg.get_content_type.return_value = "text/plain"
        msg.get.return_value = ""
        msg.get_payload.return_value = b"value"
        msg.get_content_charset.return_value = "utf-8"
        blocks = _run_email(msg)
        for b in blocks:
            assert b.bbox is None

    def test_source_path_stored(self):
        msg = MagicMock()
        msg.is_multipart.return_value = False
        msg.get_content_type.return_value = "text/plain"
        msg.get.return_value = ""
        msg.get_payload.return_value = b"hello"
        msg.get_content_charset.return_value = "utf-8"
        blocks = _run_email(msg, "inbox/msg.eml")
        for b in blocks:
            assert "msg.eml" in b.source_path


# ===========================================================================
# ParquetReader
# ===========================================================================

def _make_pq(num_row_groups: int, col_names: list[str],
             values_per_group: list[list[list]]) -> MagicMock:
    """Build a mock pyarrow ParquetFile."""
    pf = MagicMock()
    pf.metadata.num_row_groups = num_row_groups

    tables = []
    for group_values in values_per_group:
        table = MagicMock()
        table.schema.names = col_names

        def _make_col(vals):
            items = [MagicMock(as_py=lambda v=v: v) for v in vals]
            col = MagicMock()
            col.__iter__ = lambda self, _i=items: iter(_i)
            return col

        table.column.side_effect = lambda i, _gv=group_values: _make_col(_gv[i])
        tables.append(table)

    pf.read_row_group.side_effect = lambda i: tables[i]
    return pf


def _run_parquet(pf: MagicMock, path: str = "data.parquet") -> list:
    with patch("app.readers.parquet_reader.pq") as mock_pq:
        mock_pq.ParquetFile.return_value = pf
        reader = ParquetReader(path)
        return reader.read()


class TestParquetReader:

    def test_cells_emitted_as_table_cell(self):
        pf = _make_pq(1, ["name", "ssn"],
                      [[["Alice", "Bob"], ["123-45-6789", "987-65-4321"]]])
        blocks = _run_parquet(pf)
        assert all(b.block_type == "table_cell" for b in blocks)
        texts = [b.text for b in blocks]
        assert "Alice" in texts
        assert "123-45-6789" in texts

    def test_null_values_skipped(self):
        pf = _make_pq(1, ["col"], [[[None, "valid"]]])
        blocks = _run_parquet(pf)
        assert len(blocks) == 1
        assert blocks[0].text == "valid"

    def test_col_header_from_schema(self):
        pf = _make_pq(1, ["email"], [[["user@example.com"]]])
        blocks = _run_parquet(pf)
        assert blocks[0].col_header == "email"

    def test_page_or_sheet_is_row_group_index(self):
        pf = _make_pq(2, ["v"], [[["a"]], [["b"]]])
        blocks = _run_parquet(pf)
        indices = [b.page_or_sheet for b in blocks]
        assert 0 in indices
        assert 1 in indices

    def test_bbox_is_none(self):
        pf = _make_pq(1, ["col"], [[["val"]]])
        blocks = _run_parquet(pf)
        for b in blocks:
            assert b.bbox is None

    def test_file_type_from_extension_parquet(self):
        pf = _make_pq(1, ["col"], [[["val"]]])
        blocks = _run_parquet(pf, "data.parquet")
        assert all(b.file_type == "parquet" for b in blocks)

    def test_file_type_from_extension_avro(self):
        pf = _make_pq(1, ["col"], [[["val"]]])
        blocks = _run_parquet(pf, "data.avro")
        assert all(b.file_type == "avro" for b in blocks)

    def test_empty_parquet_returns_empty_list(self):
        pf = _make_pq(0, [], [])
        blocks = _run_parquet(pf)
        assert blocks == []

    def test_source_path_stored(self):
        pf = _make_pq(1, ["col"], [[["val"]]])
        blocks = _run_parquet(pf, "my/file.parquet")
        for b in blocks:
            assert "my/file.parquet" in b.source_path

    def test_empty_string_value_skipped(self):
        pf = _make_pq(1, ["col"], [[["", "nonempty"]]])
        blocks = _run_parquet(pf)
        assert len(blocks) == 1
        assert blocks[0].text == "nonempty"
