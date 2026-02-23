"""Canonical output dataclass shared by all document readers.

Every reader in app/readers/ must yield ExtractedBlock objects — never
raw strings. Downstream PII stages (app/pii/) consume only ExtractedBlock
lists and must not know which reader produced them.

Field contract
--------------
text          : extracted text content of this block
page_or_sheet : page number (int) for PDF/DOCX; sheet name (str) for Excel/CSV
bbox          : (x0, y0, x1, y1) in points/pixels; None for non-visual formats
row           : source row number (spreadsheet row or table row); None for prose
column        : source column number; None for prose
source_path   : absolute path to the originating file
file_type     : lowercase extension without dot, e.g. "pdf", "xlsx", "csv"
block_type    : "prose" | "table_cell" | "table_header"
table_id      : UUID string grouping all ExtractedBlocks from the same table;
                None for prose blocks
col_header    : column header text for this cell; None for prose
row_index     : 0-based row number within the detected table; None for prose
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

BlockType = Literal["prose", "table_cell", "table_header"]
_VALID_BLOCK_TYPES: frozenset[str] = frozenset({"prose", "table_cell", "table_header"})


@dataclass
class ExtractedBlock:
    """Canonical unit of content emitted by every reader.

    Readers must not pass raw strings downstream. All content must be
    wrapped in ExtractedBlock so provenance is always preserved.
    """

    # Core content
    text: str
    page_or_sheet: int | str
    source_path: str
    file_type: str

    # Layout / block classification
    block_type: BlockType = "prose"

    # Visual provenance (None for non-visual formats such as CSV, Parquet)
    bbox: tuple[float, float, float, float] | None = None

    # Spreadsheet / tabular provenance
    row: int | None = None
    column: int | None = None

    # Table grouping (populated for table_cell and table_header blocks)
    table_id: str | None = None    # UUID shared by all cells in one table
    col_header: str | None = None  # column header text for this cell
    row_index: int | None = None   # 0-based row position within the table

    def __post_init__(self) -> None:
        if self.block_type not in _VALID_BLOCK_TYPES:
            raise ValueError(
                f"block_type must be one of {sorted(_VALID_BLOCK_TYPES)!r}; "
                f"got {self.block_type!r}"
            )
        if not self.file_type:
            raise ValueError("file_type must be a non-empty string")


class BaseReader:
    """Base class for all document readers.

    Subclasses must override read() to return a list of ExtractedBlock
    objects. Unimplemented stubs inherit this class and raise
    NotImplementedError when read() is called.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def read(self) -> list[ExtractedBlock]:
        """Return ExtractedBlock objects for all content in the document.

        Raises NotImplementedError until the reader is implemented.
        """
        raise NotImplementedError(
            f"{type(self).__name__}.read() is not yet implemented"
        )
