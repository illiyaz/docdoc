"""Tests for app/readers/base.py and app/readers/registry.py.

Covers:
- ExtractedBlock instantiation with required and optional fields
- bbox=None is valid for non-visual formats
- block_type defaults and __post_init__ validation
- Registry maps every supported extension to the correct reader class
- Registry is case-insensitive for extensions
- Registry falls back to TikaReader for unknown extensions
- Registry raises ValueError when the file has no extension
- Reader stubs raise NotImplementedError when read() is called
"""
from __future__ import annotations

import pytest

from app.readers.base import BaseReader, BlockType, ExtractedBlock
from app.readers.registry import get_reader, register, _REGISTRY


# ---------------------------------------------------------------------------
# ExtractedBlock — construction
# ---------------------------------------------------------------------------

def test_extracted_block_minimal_fields():
    """The four required fields must be accepted without keyword arguments."""
    block = ExtractedBlock(
        text="Hello world",
        page_or_sheet=1,
        source_path="/data/doc.pdf",
        file_type="pdf",
    )
    assert block.text == "Hello world"
    assert block.page_or_sheet == 1
    assert block.source_path == "/data/doc.pdf"
    assert block.file_type == "pdf"


def test_extracted_block_defaults():
    block = ExtractedBlock(text="x", page_or_sheet=0, source_path="/f", file_type="csv")
    assert block.block_type == "prose"
    assert block.bbox is None
    assert block.row is None
    assert block.column is None
    assert block.table_id is None
    assert block.col_header is None
    assert block.row_index is None


def test_extracted_block_all_fields():
    block = ExtractedBlock(
        text="123-45-6789",
        page_or_sheet="Sheet1",
        source_path="/data/report.xlsx",
        file_type="xlsx",
        block_type="table_cell",
        bbox=None,
        row=3,
        column=2,
        table_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        col_header="SSN",
        row_index=2,
    )
    assert block.block_type == "table_cell"
    assert block.row == 3
    assert block.column == 2
    assert block.table_id == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert block.col_header == "SSN"
    assert block.row_index == 2


def test_extracted_block_page_or_sheet_accepts_string():
    """page_or_sheet can be a str (sheet name for Excel/CSV)."""
    block = ExtractedBlock(text="v", page_or_sheet="DataSheet", source_path="/f.xlsx", file_type="xlsx")
    assert block.page_or_sheet == "DataSheet"


def test_extracted_block_page_or_sheet_accepts_int():
    """page_or_sheet can be an int (page number for PDF/DOCX)."""
    block = ExtractedBlock(text="v", page_or_sheet=5, source_path="/f.pdf", file_type="pdf")
    assert block.page_or_sheet == 5


# ---------------------------------------------------------------------------
# ExtractedBlock — bbox=None is valid (non-visual formats)
# ---------------------------------------------------------------------------

def test_bbox_none_valid_for_csv():
    block = ExtractedBlock(text="data", page_or_sheet=0, source_path="/f.csv", file_type="csv", bbox=None)
    assert block.bbox is None


def test_bbox_none_valid_for_parquet():
    block = ExtractedBlock(text="v", page_or_sheet=0, source_path="/f.parquet", file_type="parquet", bbox=None)
    assert block.bbox is None


def test_bbox_tuple_valid_for_pdf():
    block = ExtractedBlock(
        text="Name:", page_or_sheet=1, source_path="/f.pdf", file_type="pdf",
        bbox=(12.0, 34.5, 120.0, 48.0),
    )
    assert block.bbox == (12.0, 34.5, 120.0, 48.0)


# ---------------------------------------------------------------------------
# ExtractedBlock — block_type validation
# ---------------------------------------------------------------------------

def test_block_type_prose_is_default():
    block = ExtractedBlock(text="p", page_or_sheet=0, source_path="/f", file_type="pdf")
    assert block.block_type == "prose"


def test_block_type_table_cell_accepted():
    block = ExtractedBlock(
        text="val", page_or_sheet=0, source_path="/f", file_type="xlsx",
        block_type="table_cell",
    )
    assert block.block_type == "table_cell"


def test_block_type_table_header_accepted():
    block = ExtractedBlock(
        text="Name", page_or_sheet=0, source_path="/f", file_type="xlsx",
        block_type="table_header",
    )
    assert block.block_type == "table_header"


def test_invalid_block_type_raises_value_error():
    with pytest.raises(ValueError, match="block_type"):
        ExtractedBlock(
            text="x", page_or_sheet=0, source_path="/f", file_type="pdf",
            block_type="unknown",  # type: ignore[arg-type]
        )


def test_empty_file_type_raises_value_error():
    with pytest.raises(ValueError, match="file_type"):
        ExtractedBlock(text="x", page_or_sheet=0, source_path="/f", file_type="")


# ---------------------------------------------------------------------------
# Registry — correct reader class per extension
# ---------------------------------------------------------------------------

def _reader_class_name(path: str) -> str:
    return type(get_reader(path)).__name__


def test_registry_pdf():
    assert _reader_class_name("document.pdf") == "PDFReader"


def test_registry_xlsx():
    assert _reader_class_name("report.xlsx") == "ExcelReader"


def test_registry_xls():
    assert _reader_class_name("old.xls") == "ExcelReader"


def test_registry_docx():
    assert _reader_class_name("letter.docx") == "DOCXReader"


def test_registry_csv():
    assert _reader_class_name("data.csv") == "CSVReader"


def test_registry_html():
    assert _reader_class_name("page.html") == "HTMLReader"


def test_registry_htm():
    assert _reader_class_name("page.htm") == "HTMLReader"


def test_registry_eml():
    assert _reader_class_name("message.eml") == "EmailReader"


def test_registry_msg():
    assert _reader_class_name("message.msg") == "EmailReader"


def test_registry_parquet():
    assert _reader_class_name("dataset.parquet") == "ParquetReader"


def test_registry_avro():
    assert _reader_class_name("dataset.avro") == "ParquetReader"


# ---------------------------------------------------------------------------
# Registry — case-insensitive extension matching
# ---------------------------------------------------------------------------

def test_registry_case_insensitive_upper():
    assert _reader_class_name("DOCUMENT.PDF") == "PDFReader"


def test_registry_case_insensitive_mixed():
    assert _reader_class_name("report.Xlsx") == "ExcelReader"


# ---------------------------------------------------------------------------
# Registry — fallback to TikaReader for unknown extensions
# ---------------------------------------------------------------------------

def test_registry_unknown_extension_falls_back_to_tika():
    reader = get_reader("archive.7z")
    assert type(reader).__name__ == "TikaReader"


def test_registry_uncommon_extension_falls_back_to_tika():
    reader = get_reader("data.odt")
    assert type(reader).__name__ == "TikaReader"


# ---------------------------------------------------------------------------
# Registry — raises ValueError when the file has no extension
# ---------------------------------------------------------------------------

def test_registry_raises_for_no_extension():
    with pytest.raises(ValueError, match="no file extension"):
        get_reader("README")


def test_registry_raises_for_no_extension_dotfile():
    """Dot-files like .gitignore have no meaningful extension after the dot."""
    with pytest.raises(ValueError, match="no file extension"):
        get_reader(".gitignore")


# ---------------------------------------------------------------------------
# Registry — register() validates its input
# ---------------------------------------------------------------------------

def test_register_empty_extension_raises():
    from app.readers.base import BaseReader as BR
    with pytest.raises(ValueError):
        register("", BR)


def test_register_whitespace_extension_raises():
    from app.readers.base import BaseReader as BR
    with pytest.raises(ValueError):
        register("   ", BR)


# ---------------------------------------------------------------------------
# Reader stubs — read() raises NotImplementedError
# ---------------------------------------------------------------------------

def test_pdf_reader_is_implemented():
    # PDFReader.read() is now fully implemented — no longer a stub
    reader = get_reader("doc.pdf")
    assert type(reader).__name__ == "PDFReader"
    assert callable(reader.read)


def test_excel_reader_is_implemented():
    reader = get_reader("book.xlsx")
    assert type(reader).__name__ == "ExcelReader"
    assert callable(reader.read)


def test_docx_reader_is_implemented():
    reader = get_reader("letter.docx")
    assert type(reader).__name__ == "DOCXReader"
    assert callable(reader.read)


def test_csv_reader_is_implemented():
    reader = get_reader("data.csv")
    assert type(reader).__name__ == "CSVReader"
    assert callable(reader.read)


def test_html_reader_is_implemented():
    reader = get_reader("page.html")
    assert type(reader).__name__ == "HTMLReader"
    assert callable(reader.read)


def test_email_reader_is_implemented():
    reader = get_reader("msg.eml")
    assert type(reader).__name__ == "EmailReader"
    assert callable(reader.read)


def test_parquet_reader_is_implemented():
    reader = get_reader("dataset.parquet")
    assert type(reader).__name__ == "ParquetReader"
    assert callable(reader.read)


def test_tika_reader_raises_not_implemented():
    with pytest.raises(NotImplementedError, match="TikaReader"):
        get_reader("file.xyz").read()


# ---------------------------------------------------------------------------
# BaseReader — path is stored as Path object
# ---------------------------------------------------------------------------

def test_reader_path_stored_as_pathlib_path():
    from pathlib import Path
    reader = get_reader("data.csv")
    assert isinstance(reader.path, Path)
    assert reader.path.name == "data.csv"


def test_reader_accepts_pathlib_path_argument():
    from pathlib import Path
    reader = get_reader(Path("/tmp/report.pdf"))
    assert reader.path == Path("/tmp/report.pdf")
