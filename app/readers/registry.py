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

import importlib
from pathlib import Path

from app.readers.base import BaseReader, ExtractedBlock  # noqa: F401 — re-exported

# Extension → (module_path, class_name) mapping.
# Actual imports are deferred to get_reader() so missing optional
# dependencies (python-docx, pyarrow, etc.) don't break import time.
_LAZY_REGISTRY: dict[str, tuple[str, str]] = {}

# Extension → eagerly registered reader class (for programmatic register()).
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

    # Check eagerly registered readers first
    reader_cls = _REGISTRY.get(ext)
    if reader_cls is not None:
        return reader_cls(p)

    # Check lazy registry
    lazy_entry = _LAZY_REGISTRY.get(ext)
    if lazy_entry is not None:
        module_path, class_name = lazy_entry
        mod = importlib.import_module(module_path)
        reader_cls = getattr(mod, class_name)
        return reader_cls(p)

    # Fallback to Tika
    from app.readers.tika_reader import TikaReader
    return TikaReader(p)


def _register_defaults() -> None:
    """Populate _LAZY_REGISTRY with all built-in readers.

    No actual imports happen here — only module path + class name strings
    are stored.  The real import is deferred to get_reader() call time.
    """
    _LAZY_REGISTRY["pdf"] = ("app.readers.pdf_reader", "PDFReader")
    _LAZY_REGISTRY["xlsx"] = ("app.readers.excel_reader", "ExcelReader")
    _LAZY_REGISTRY["xls"] = ("app.readers.excel_reader", "ExcelReader")
    _LAZY_REGISTRY["docx"] = ("app.readers.docx_reader", "DOCXReader")
    _LAZY_REGISTRY["csv"] = ("app.readers.csv_reader", "CSVReader")
    _LAZY_REGISTRY["html"] = ("app.readers.html_reader", "HTMLReader")
    _LAZY_REGISTRY["htm"] = ("app.readers.html_reader", "HTMLReader")
    _LAZY_REGISTRY["xml"] = ("app.readers.html_reader", "HTMLReader")
    _LAZY_REGISTRY["eml"] = ("app.readers.email_reader", "EmailReader")
    _LAZY_REGISTRY["msg"] = ("app.readers.email_reader", "EmailReader")
    _LAZY_REGISTRY["parquet"] = ("app.readers.parquet_reader", "ParquetReader")
    _LAZY_REGISTRY["avro"] = ("app.readers.parquet_reader", "ParquetReader")


_register_defaults()
