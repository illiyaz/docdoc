"""Reader registry: maps file extension to the correct reader class.

Usage
-----
    from app.readers.registry import get_reader

    reader = get_reader("/data/report.pdf")
    for block in reader.read():
        ...

Rules
-----
- Always route document loading through get_reader().
- Apache Tika (TikaReader) is the automatic fallback for formats
  that have no dedicated reader registered.
- Never instantiate a reader class directly in pipeline code.
- get_reader() raises ValueError when the file has no extension at all.
"""
from __future__ import annotations

from pathlib import Path

from app.readers.base import BaseReader, ExtractedBlock  # noqa: F401 — re-exported

# Extension → reader class mapping; populated by _register_defaults() at import time.
_REGISTRY: dict[str, type[BaseReader]] = {}


def register(extension: str, reader_cls: type[BaseReader]) -> None:
    """Register a reader class for a file extension.

    extension must be a non-empty lowercase string without a leading dot,
    e.g. "pdf", "xlsx".  Raises ValueError for invalid input.
    """
    if not extension or not extension.strip():
        raise ValueError(
            "extension must be a non-empty string without a leading dot (e.g. 'pdf')"
        )
    _REGISTRY[extension.lower()] = reader_cls


def get_reader(path: str | Path) -> BaseReader:
    """Return an instantiated reader for the given file path.

    Looks up the file extension in the registry.  Falls back to TikaReader
    for extensions that have no dedicated reader.  Raises ValueError when
    the file has no extension at all (e.g. "Makefile", "README").
    """
    p = Path(path)
    ext = p.suffix.lstrip(".").lower()
    if not ext:
        raise ValueError(
            f"Cannot determine file type: {p.name!r} has no file extension. "
            "Provide a file with an extension or register a default reader."
        )
    reader_cls = _REGISTRY.get(ext)
    if reader_cls is None:
        from app.readers.tika_reader import TikaReader
        reader_cls = TikaReader
    return reader_cls(p)


def _register_defaults() -> None:
    """Populate _REGISTRY with all built-in readers.

    Imports are deferred to this function to avoid circular imports at
    module load time (reader modules import from base.py, not registry.py).
    """
    from app.readers.csv_reader import CSVReader
    from app.readers.docx_reader import DOCXReader
    from app.readers.email_reader import EmailReader
    from app.readers.excel_reader import ExcelReader
    from app.readers.html_reader import HTMLReader
    from app.readers.parquet_reader import ParquetReader
    from app.readers.pdf_reader import PDFReader

    for ext in ("pdf",):
        register(ext, PDFReader)
    for ext in ("xlsx", "xls"):
        register(ext, ExcelReader)
    for ext in ("docx",):
        register(ext, DOCXReader)
    for ext in ("csv",):
        register(ext, CSVReader)
    for ext in ("html", "htm", "xml"):
        register(ext, HTMLReader)
    for ext in ("eml", "msg"):
        register(ext, EmailReader)
    for ext in ("parquet", "avro"):
        register(ext, ParquetReader)


_register_defaults()
