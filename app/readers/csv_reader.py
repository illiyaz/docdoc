"""CSV reader: pandas chunksize streaming.

Rules
-----
- Never call pd.read_csv() on the full file — always use the chunksize
  iterator so arbitrarily large files are processed without loading them
  entirely into memory.
- Row 0 is treated as column headers; header values are attached as
  col_header to every block in that column.
- bbox is None for all blocks (non-visual format).
- page_or_sheet is set to 0 (CSV has no sheet concept).
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pandas as pd

from app.readers.base import BaseReader, ExtractedBlock

CHUNK_SIZE: int = 1_000  # rows per pandas iterator chunk


class CSVReader(BaseReader):
    """Stream a CSV file in chunks and emit ExtractedBlock objects."""

    def __init__(self, path: str | Path) -> None:
        super().__init__(path)

    def read(self) -> list[ExtractedBlock]:
        """Iterate over chunks and yield one ExtractedBlock per non-empty cell."""
        source = str(self.path)
        all_blocks: list[ExtractedBlock] = []
        table_id = str(uuid.uuid4())
        header_emitted = False

        for chunk in pd.read_csv(
            str(self.path),
            chunksize=CHUNK_SIZE,
            dtype=str,
            keep_default_na=False,
        ):
            col_names = list(chunk.columns)

            # Emit table_header blocks once (from the first chunk's column names)
            if not header_emitted:
                for col_index, col_name in enumerate(col_names):
                    all_blocks.append(ExtractedBlock(
                        text=col_name,
                        page_or_sheet=0,
                        source_path=source,
                        file_type="csv",
                        block_type="table_header",
                        bbox=None,
                        table_id=table_id,
                        col_header=col_name,
                        row=0,
                        column=col_index,
                        row_index=0,
                    ))
                header_emitted = True

            for row_offset, (_, row) in enumerate(chunk.iterrows()):
                for col_index, col_name in enumerate(col_names):
                    val = row[col_name]
                    if pd.isna(val) or str(val).strip() == "":
                        continue
                    all_blocks.append(ExtractedBlock(
                        text=str(val).strip(),
                        page_or_sheet=0,
                        source_path=source,
                        file_type="csv",
                        block_type="table_cell",
                        bbox=None,
                        table_id=table_id,
                        col_header=col_name,
                    ))

        return all_blocks
