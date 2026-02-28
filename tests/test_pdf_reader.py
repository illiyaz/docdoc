"""Tests for app/readers/pdf_reader.py.

All external dependencies (fitz, pdfplumber, OCREngine, PageStitcher,
find_data_onset, classify_page) are mocked.  No real PDF files required.

fitz and pdfplumber are injected into sys.modules before the module under
test is imported so that `import fitz` / `import pdfplumber` at the top of
pdf_reader.py succeeds without those packages being installed.

Covers:
  - Digital page → prose ExtractedBlock objects via get_text("dict")
  - Scanned/corrupted page → OCREngine called, not get_text("dict")
  - Table page → table_header + table_cell blocks with correct fields
  - table_id is shared across all blocks from the same table
  - doc._forget_page called once per processed page
  - Onset page respected — pages before onset_page are never loaded
  - PageStitcher.stitch called once per processed page
  - Checkpoint updated after every page
  - Prose blocks overlapping table regions are excluded
  - _bbox_overlaps helper correctness
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, Mock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Stub fitz and pdfplumber in sys.modules BEFORE importing the module under
# test.  This prevents ImportError when those packages are absent and lets
# patch() swap the name in the module namespace during each test.
# ---------------------------------------------------------------------------
_FITZ_STUB = MagicMock(name="fitz_stub")
_PLUMBER_STUB = MagicMock(name="pdfplumber_stub")
sys.modules.setdefault("fitz", _FITZ_STUB)
sys.modules.setdefault("pdfplumber", _PLUMBER_STUB)
# ocr.py (imported by pdf_reader) needs paddleocr + numpy
sys.modules.setdefault("paddleocr", MagicMock(name="paddleocr_stub"))
sys.modules.setdefault("numpy", MagicMock(name="numpy_stub"))

from app.readers.pdf_reader import PDFReader, _bbox_overlaps  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PROSE_DICT_BLOCK = {
    "type": 0,
    "bbox": (10.0, 20.0, 200.0, 40.0),
    "lines": [
        {"spans": [{"text": "Hello world"}]},
        {"spans": [{"text": "Second line"}]},
    ],
}

_TABLE_BBOX = (5.0, 5.0, 205.0, 105.0)
_TABLE_ROWS = [
    ["Name", "Value"],    # row 0 → headers
    ["Alice", "123"],     # row 1 → data
]


def _make_fitz_mock(num_pages: int = 1, page_dict: dict | None = None):
    """Return (mock_fitz, mock_doc, mock_page)."""
    mock_fitz = MagicMock(name="fitz")
    mock_doc = MagicMock()
    mock_doc.__len__ = Mock(return_value=num_pages)
    mock_page = MagicMock()
    if page_dict is not None:
        mock_page.get_text.return_value = page_dict
    mock_doc.load_page.return_value = mock_page
    mock_fitz.open.return_value = mock_doc
    return mock_fitz, mock_doc, mock_page


def _make_plumber_mock(tables=None, table_data=None):
    """Return (mock_plumber, mock_plumber_page).

    tables     : list of mock Table objects with .bbox attribute
    table_data : list of row-lists returned by extract_tables()
    """
    tables = tables or []
    table_data = table_data or []
    mock_plumber = MagicMock(name="pdfplumber")
    mock_plumber_page = MagicMock()
    mock_plumber_page.find_tables.return_value = tables
    mock_plumber_page.extract_tables.return_value = table_data
    mock_plumber_doc = MagicMock()
    mock_plumber_doc.pages = [mock_plumber_page]  # index by page_num
    mock_plumber.open.return_value.__enter__.return_value = mock_plumber_doc
    mock_plumber.open.return_value.__exit__.return_value = False
    return mock_plumber, mock_plumber_page


def _run_reader(
    *,
    num_pages: int = 1,
    onset: int = 0,
    classify_returns: str = "digital",
    page_dict: dict | None = None,
    tables=None,
    table_data=None,
    ocr_blocks=None,
) -> tuple[PDFReader, list, dict]:
    """Run PDFReader.read() with fully mocked deps; return (reader, blocks, mocks)."""
    if page_dict is None:
        page_dict = {"blocks": [_PROSE_DICT_BLOCK]}

    mock_fitz, mock_doc, mock_page = _make_fitz_mock(num_pages, page_dict)
    mock_plumber, mock_plumber_page = _make_plumber_mock(tables, table_data)

    # For multi-page docs each page needs the same plumber page mock
    mock_plumber.open.return_value.__enter__.return_value.pages = (
        [mock_plumber_page] * num_pages
    )

    mock_ocr_instance = MagicMock()
    mock_ocr_instance.ocr_page_image.return_value = ocr_blocks or []

    mock_stitcher_instance = MagicMock()

    with (
        patch("app.readers.pdf_reader.fitz", mock_fitz),
        patch("app.readers.pdf_reader.pdfplumber", mock_plumber),
        patch("app.readers.pdf_reader.find_data_onset", return_value=onset),
        patch("app.readers.pdf_reader.classify_page", return_value=classify_returns),
        patch("app.readers.ocr.OCREngine", return_value=mock_ocr_instance),
        patch("app.readers.pdf_reader.PageStitcher", return_value=mock_stitcher_instance),
    ):
        reader = PDFReader("test.pdf")
        blocks = reader.read()

    mocks = {
        "fitz": mock_fitz,
        "doc": mock_doc,
        "page": mock_page,
        "plumber": mock_plumber,
        "plumber_page": mock_plumber_page,
        "ocr": mock_ocr_instance,
        "stitcher": mock_stitcher_instance,
    }
    return reader, blocks, mocks


# ---------------------------------------------------------------------------
# _bbox_overlaps unit tests
# ---------------------------------------------------------------------------

def test_bbox_overlaps_fully_inside():
    assert _bbox_overlaps((20, 20, 100, 80), (10, 10, 200, 100)) is True


def test_bbox_overlaps_partial_overlap():
    # Right edge of block overlaps left edge of table
    assert _bbox_overlaps((0, 0, 50, 50), (40, 0, 200, 100)) is True


def test_bbox_no_overlap_left():
    assert _bbox_overlaps((0, 0, 30, 50), (50, 0, 200, 100)) is False


def test_bbox_no_overlap_right():
    assert _bbox_overlaps((210, 0, 300, 100), (10, 0, 200, 100)) is False


def test_bbox_no_overlap_above():
    assert _bbox_overlaps((10, 0, 100, 5), (10, 10, 100, 100)) is False


def test_bbox_no_overlap_below():
    assert _bbox_overlaps((10, 110, 100, 200), (10, 10, 100, 100)) is False


def test_bbox_touching_edge_not_overlap():
    # Blocks that share only an edge (bx1 == tx0) do NOT overlap
    assert _bbox_overlaps((0, 0, 50, 100), (50, 0, 200, 100)) is False


# ---------------------------------------------------------------------------
# 1. Digital page → prose blocks
# ---------------------------------------------------------------------------

def test_digital_page_returns_prose_blocks():
    _, blocks, _ = _run_reader(classify_returns="digital")
    prose = [b for b in blocks if b.block_type == "prose"]
    assert len(prose) == 1
    assert prose[0].text == "Hello world\nSecond line"


def test_digital_page_block_has_correct_fields():
    _, blocks, _ = _run_reader(classify_returns="digital")
    b = blocks[0]
    assert b.file_type == "pdf"
    assert b.page_or_sheet == 0
    assert b.source_path == "test.pdf"
    assert b.bbox == (10.0, 20.0, 200.0, 40.0)
    assert b.block_type == "prose"
    assert b.table_id is None
    assert b.col_header is None


def test_digital_page_does_not_call_ocr():
    _, _, mocks = _run_reader(classify_returns="digital")
    mocks["ocr"].ocr_page_image.assert_not_called()


def test_digital_page_calls_get_text_dict():
    _, _, mocks = _run_reader(classify_returns="digital")
    mocks["page"].get_text.assert_called_with("dict")


def test_image_blocks_are_skipped():
    page_dict = {
        "blocks": [
            {"type": 1, "bbox": (0, 0, 100, 100), "lines": []},   # image
            _PROSE_DICT_BLOCK,
        ]
    }
    _, blocks, _ = _run_reader(classify_returns="digital", page_dict=page_dict)
    assert len([b for b in blocks if b.block_type == "prose"]) == 1


def test_empty_span_text_blocks_skipped():
    page_dict = {
        "blocks": [
            {
                "type": 0,
                "bbox": (0, 0, 100, 20),
                "lines": [{"spans": [{"text": ""}]}],
            }
        ]
    }
    _, blocks, _ = _run_reader(classify_returns="digital", page_dict=page_dict)
    assert blocks == []


# ---------------------------------------------------------------------------
# 2. Scanned/corrupted page → OCREngine called, not get_text("dict")
# ---------------------------------------------------------------------------

def test_scanned_page_calls_ocr():
    _, _, mocks = _run_reader(classify_returns="scanned")
    mocks["ocr"].ocr_page_image.assert_called_once()


def test_corrupted_page_calls_ocr():
    _, _, mocks = _run_reader(classify_returns="corrupted")
    mocks["ocr"].ocr_page_image.assert_called_once()


def test_scanned_page_does_not_call_get_text_dict():
    _, _, mocks = _run_reader(classify_returns="scanned")
    # get_text("dict") must not be called for scanned pages
    for call_args in mocks["page"].get_text.call_args_list:
        assert call_args != call("dict"), "get_text('dict') called on scanned page"


def test_scanned_page_calls_get_pixmap():
    _, _, mocks = _run_reader(classify_returns="scanned")
    mocks["page"].get_pixmap.assert_called_once()


def test_scanned_page_get_pixmap_receives_matrix():
    mock_fitz = MagicMock(name="fitz")
    mock_doc = MagicMock()
    mock_doc.__len__ = Mock(return_value=1)
    mock_page = MagicMock()
    mock_doc.load_page.return_value = mock_page
    mock_fitz.open.return_value = mock_doc
    mock_plumber, _ = _make_plumber_mock()

    with (
        patch("app.readers.pdf_reader.fitz", mock_fitz),
        patch("app.readers.pdf_reader.pdfplumber", mock_plumber),
        patch("app.readers.pdf_reader.find_data_onset", return_value=0),
        patch("app.readers.pdf_reader.classify_page", return_value="scanned"),
        patch("app.readers.ocr.OCREngine", return_value=MagicMock()),
        patch("app.readers.pdf_reader.PageStitcher", return_value=MagicMock()),
    ):
        PDFReader("test.pdf").read()

    mock_fitz.Matrix.assert_called_with(2, 2)
    matrix_instance = mock_fitz.Matrix.return_value
    mock_page.get_pixmap.assert_called_once_with(matrix=matrix_instance)


def test_scanned_page_ocr_result_returned():
    from app.readers.base import ExtractedBlock
    ocr_block = ExtractedBlock(
        text="OCR text",
        page_or_sheet=0,
        source_path="test.pdf",
        file_type="pdf",
    )
    _, blocks, _ = _run_reader(classify_returns="scanned", ocr_blocks=[ocr_block])
    assert any(b.text == "OCR text" for b in blocks)


# ---------------------------------------------------------------------------
# 3. Table page → table_header + table_cell blocks
# ---------------------------------------------------------------------------

def _make_table_mock(bbox=_TABLE_BBOX):
    t = MagicMock()
    t.bbox = bbox
    return t


def test_table_page_produces_table_header_blocks():
    mock_table = _make_table_mock()
    _, blocks, _ = _run_reader(
        page_dict={"blocks": []},  # no prose
        tables=[mock_table],
        table_data=[_TABLE_ROWS],
    )
    headers = [b for b in blocks if b.block_type == "table_header"]
    assert len(headers) == 2  # "Name", "Value"


def test_table_page_produces_table_cell_blocks():
    mock_table = _make_table_mock()
    _, blocks, _ = _run_reader(
        page_dict={"blocks": []},
        tables=[mock_table],
        table_data=[_TABLE_ROWS],
    )
    cells = [b for b in blocks if b.block_type == "table_cell"]
    assert len(cells) == 2  # "Alice", "123"


def test_table_header_block_fields():
    mock_table = _make_table_mock()
    _, blocks, _ = _run_reader(
        page_dict={"blocks": []},
        tables=[mock_table],
        table_data=[_TABLE_ROWS],
    )
    header = next(b for b in blocks if b.block_type == "table_header" and b.text == "Name")
    assert header.row == 0
    assert header.column == 0
    assert header.row_index == 0
    assert header.col_header == "Name"
    assert header.file_type == "pdf"


def test_table_cell_has_correct_col_header():
    mock_table = _make_table_mock()
    _, blocks, _ = _run_reader(
        page_dict={"blocks": []},
        tables=[mock_table],
        table_data=[_TABLE_ROWS],
    )
    alice = next(b for b in blocks if b.block_type == "table_cell" and b.text == "Alice")
    assert alice.col_header == "Name"
    assert alice.column == 0
    assert alice.row_index == 1


def test_table_cell_value_col_header():
    mock_table = _make_table_mock()
    _, blocks, _ = _run_reader(
        page_dict={"blocks": []},
        tables=[mock_table],
        table_data=[_TABLE_ROWS],
    )
    val = next(b for b in blocks if b.block_type == "table_cell" and b.text == "123")
    assert val.col_header == "Value"
    assert val.column == 1


def test_none_cell_text_becomes_empty_string():
    rows = [["A", "B"], [None, "data"]]
    mock_table = _make_table_mock()
    _, blocks, _ = _run_reader(
        page_dict={"blocks": []},
        tables=[mock_table],
        table_data=[rows],
    )
    none_cell = next(b for b in blocks if b.block_type == "table_cell" and b.column == 0)
    assert none_cell.text == ""


# ---------------------------------------------------------------------------
# 4. table_id shared across all cells in the same table
# ---------------------------------------------------------------------------

def test_table_id_shared_within_one_table():
    mock_table = _make_table_mock()
    _, blocks, _ = _run_reader(
        page_dict={"blocks": []},
        tables=[mock_table],
        table_data=[_TABLE_ROWS],
    )
    table_blocks = [b for b in blocks if b.table_id is not None]
    ids = {b.table_id for b in table_blocks}
    assert len(ids) == 1, "All blocks from one table must share a single table_id"


def test_table_id_is_uuid_string():
    import re
    mock_table = _make_table_mock()
    _, blocks, _ = _run_reader(
        page_dict={"blocks": []},
        tables=[mock_table],
        table_data=[_TABLE_ROWS],
    )
    tid = next(b.table_id for b in blocks if b.table_id is not None)
    assert re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        tid,
    ), f"table_id is not a valid UUID: {tid!r}"


def test_two_tables_have_different_table_ids():
    mock_table_a = _make_table_mock(bbox=(0.0, 0.0, 100.0, 50.0))
    mock_table_b = _make_table_mock(bbox=(0.0, 200.0, 100.0, 300.0))
    rows_a = [["Col1"], ["v1"]]
    rows_b = [["Col2"], ["v2"]]
    _, blocks, _ = _run_reader(
        page_dict={"blocks": []},
        tables=[mock_table_a, mock_table_b],
        table_data=[rows_a, rows_b],
    )
    ids = {b.table_id for b in blocks if b.table_id is not None}
    assert len(ids) == 2, "Different tables must have distinct table_ids"


def test_prose_blocks_have_no_table_id():
    _, blocks, _ = _run_reader(classify_returns="digital")
    prose = [b for b in blocks if b.block_type == "prose"]
    for b in prose:
        assert b.table_id is None


# ---------------------------------------------------------------------------
# 5. doc._forget_page called after every processed page
# ---------------------------------------------------------------------------

def test_forget_page_called_once_for_single_page():
    _, _, mocks = _run_reader(num_pages=1, onset=0)
    mocks["doc"]._forget_page.assert_called_once_with(0)


def test_forget_page_called_for_all_pages():
    _, _, mocks = _run_reader(num_pages=3, onset=0)
    forget_calls = [c.args[0] for c in mocks["doc"]._forget_page.call_args_list]
    assert forget_calls == [0, 1, 2]


def test_forget_page_called_after_each_load_page():
    """_forget_page(n) must be called with the same n as load_page(n)."""
    _, _, mocks = _run_reader(num_pages=3, onset=0)
    loaded = [c.args[0] for c in mocks["doc"].load_page.call_args_list]
    forgotten = [c.args[0] for c in mocks["doc"]._forget_page.call_args_list]
    assert loaded == forgotten


# ---------------------------------------------------------------------------
# 6. Onset page respected — pages before onset are never loaded
# ---------------------------------------------------------------------------

def test_onset_2_skips_pages_0_and_1():
    mock_fitz, mock_doc, mock_page = _make_fitz_mock(num_pages=4)
    mock_plumber, _ = _make_plumber_mock()
    mock_plumber.open.return_value.__enter__.return_value.pages = [
        MagicMock(find_tables=Mock(return_value=[]),
                  extract_tables=Mock(return_value=[])),
    ] * 4

    with (
        patch("app.readers.pdf_reader.fitz", mock_fitz),
        patch("app.readers.pdf_reader.pdfplumber", mock_plumber),
        patch("app.readers.pdf_reader.find_data_onset", return_value=2),
        patch("app.readers.pdf_reader.classify_page", return_value="digital"),
        patch("app.readers.ocr.OCREngine", return_value=MagicMock()),
        patch("app.readers.pdf_reader.PageStitcher", return_value=MagicMock()),
    ):
        mock_page.get_text.return_value = {"blocks": []}
        PDFReader("test.pdf").read()

    loaded = [c.args[0] for c in mock_doc.load_page.call_args_list]
    assert 0 not in loaded
    assert 1 not in loaded
    assert 2 in loaded
    assert 3 in loaded


def test_onset_0_loads_all_pages():
    _, _, mocks = _run_reader(num_pages=3, onset=0)
    loaded = [c.args[0] for c in mocks["doc"].load_page.call_args_list]
    assert loaded == [0, 1, 2]


def test_onset_equal_to_num_pages_loads_nothing():
    """If onset >= num_pages there are no pages to process."""
    _, blocks, mocks = _run_reader(num_pages=2, onset=2)
    assert blocks == []
    mocks["doc"].load_page.assert_not_called()


# ---------------------------------------------------------------------------
# 7. PageStitcher.stitch called once per processed page
# ---------------------------------------------------------------------------

def test_stitcher_called_for_every_page():
    _, _, mocks = _run_reader(num_pages=3, onset=0)
    assert mocks["stitcher"].stitch.call_count == 3


def test_stitcher_not_called_when_no_pages_processed():
    _, _, mocks = _run_reader(num_pages=2, onset=2)
    mocks["stitcher"].stitch.assert_not_called()


def test_stitcher_called_with_page_num():
    _, _, mocks = _run_reader(num_pages=2, onset=0)
    call_page_nums = [c.args[0] for c in mocks["stitcher"].stitch.call_args_list]
    assert call_page_nums == [0, 1]


def test_stitcher_receives_prose_text():
    """Stitcher must receive the joined prose text, not an empty string."""
    _, _, mocks = _run_reader(classify_returns="digital")
    _, text_arg = mocks["stitcher"].stitch.call_args.args
    assert "Hello world" in text_arg


# ---------------------------------------------------------------------------
# 8. Checkpoint updated after every page
# ---------------------------------------------------------------------------

def test_checkpoint_updated_after_single_page():
    reader, _, _ = _run_reader(num_pages=1, onset=0)
    assert reader.checkpoint["last_completed_page"] == 0
    assert reader.checkpoint["document_id"] == "test.pdf"


def test_checkpoint_last_page_is_final_page():
    reader, _, _ = _run_reader(num_pages=3, onset=0)
    assert reader.checkpoint["last_completed_page"] == 2


def test_checkpoint_empty_before_read():
    reader = PDFReader("test.pdf")
    assert reader.checkpoint == {}


def test_checkpoint_returns_copy():
    reader, _, _ = _run_reader(num_pages=1, onset=0)
    cp = reader.checkpoint
    cp["last_completed_page"] = 999
    assert reader.checkpoint["last_completed_page"] == 0


# ---------------------------------------------------------------------------
# 9. Prose blocks overlapping table regions are excluded
# ---------------------------------------------------------------------------

def test_prose_block_inside_table_bbox_excluded():
    """A prose block whose bbox is fully inside a table region must be dropped."""
    prose_inside_table = {
        "type": 0,
        "bbox": (10.0, 10.0, 100.0, 50.0),   # inside _TABLE_BBOX
        "lines": [{"spans": [{"text": "inside table"}]}],
    }
    mock_table = _make_table_mock(bbox=_TABLE_BBOX)
    _, blocks, _ = _run_reader(
        classify_returns="digital",
        page_dict={"blocks": [prose_inside_table]},
        tables=[mock_table],
        table_data=[_TABLE_ROWS],
    )
    assert not any(b.text == "inside table" for b in blocks)


def test_prose_block_outside_table_bbox_included():
    """A prose block clearly outside all table regions must be kept."""
    prose_outside = {
        "type": 0,
        "bbox": (300.0, 300.0, 500.0, 320.0),  # far from _TABLE_BBOX
        "lines": [{"spans": [{"text": "outside table"}]}],
    }
    mock_table = _make_table_mock(bbox=_TABLE_BBOX)
    _, blocks, _ = _run_reader(
        classify_returns="digital",
        page_dict={"blocks": [prose_outside]},
        tables=[mock_table],
        table_data=[_TABLE_ROWS],
    )
    assert any(b.text == "outside table" for b in blocks)


# ---------------------------------------------------------------------------
# 10. PostgreSQL checkpointing
# ---------------------------------------------------------------------------

def _run_reader_with_db(db_session, db_document_id, *, num_pages=1):
    """Like _run_reader but passes a DB session to PDFReader.__init__."""
    mock_fitz, mock_doc, mock_page = _make_fitz_mock(
        num_pages, {"blocks": [_PROSE_DICT_BLOCK]}
    )
    mock_plumber, _ = _make_plumber_mock()
    mock_plumber.open.return_value.__enter__.return_value.pages = [MagicMock()] * num_pages
    mock_stitcher = MagicMock()

    with (
        patch("app.readers.pdf_reader.fitz", mock_fitz),
        patch("app.readers.pdf_reader.pdfplumber", mock_plumber),
        patch("app.readers.pdf_reader.find_data_onset", return_value=0),
        patch("app.readers.pdf_reader.classify_page", return_value="digital"),
        patch("app.readers.ocr.OCREngine", return_value=MagicMock()),
        patch("app.readers.pdf_reader.PageStitcher", return_value=mock_stitcher),
    ):
        reader = PDFReader("test.pdf", db_session=db_session, db_document_id=db_document_id)
        blocks = reader.read()
    return reader, blocks


def test_db_checkpoint_flushes_metadata_json():
    """When db_session + db_document_id are provided, metadata_json is updated."""
    mock_doc_record = MagicMock()
    mock_doc_record.metadata_json = {}
    mock_session = MagicMock()
    mock_session.get.return_value = mock_doc_record

    # Stub the local import inside _persist_checkpoint_to_db
    with patch.dict(sys.modules, {"app.db.models": MagicMock()}):
        reader, _ = _run_reader_with_db(mock_session, "test-uuid-1234", num_pages=1)

    mock_session.get.assert_called()
    assert mock_doc_record.metadata_json.get("last_completed_page") == 0
    mock_session.flush.assert_called()


def test_db_checkpoint_not_called_without_session():
    """Without db_session, no DB calls are made."""
    reader, _, _ = _run_reader(num_pages=1)
    assert reader._db_session is None
    assert reader._db_document_id is None


def test_db_checkpoint_skips_when_document_not_found():
    """If session.get returns None, checkpoint write is skipped silently."""
    mock_session = MagicMock()
    mock_session.get.return_value = None

    with patch.dict(sys.modules, {"app.db.models": MagicMock()}):
        reader, blocks = _run_reader_with_db(mock_session, "missing-uuid", num_pages=1)

    mock_session.flush.assert_not_called()


def test_db_checkpoint_handles_db_error_gracefully():
    """DB errors during checkpoint write never crash the pipeline."""
    mock_session = MagicMock()
    mock_session.get.side_effect = RuntimeError("connection lost")

    with patch.dict(sys.modules, {"app.db.models": MagicMock()}):
        reader, blocks = _run_reader_with_db(mock_session, "uuid-1", num_pages=1)

    assert isinstance(blocks, list)


def test_db_checkpoint_updated_per_page():
    """metadata_json.last_completed_page advances with each page."""
    mock_doc_record = MagicMock()
    mock_doc_record.metadata_json = {}
    mock_session = MagicMock()
    mock_session.get.return_value = mock_doc_record

    with patch.dict(sys.modules, {"app.db.models": MagicMock()}):
        reader, _ = _run_reader_with_db(mock_session, "uuid-2", num_pages=3)

    assert mock_session.flush.call_count == 3
    assert mock_doc_record.metadata_json["last_completed_page"] == 2
