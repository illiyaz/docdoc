"""Parquet / Avro reader: pyarrow row-group streaming.

Rules
-----
- Use the pyarrow ParquetFile streaming API; process row-group by row-group.
  Never load the full file into memory.
- Column names from the schema are used as col_header values.
- bbox is None for all blocks (non-visual format).
- page_or_sheet is set to the row-group index.
"""
from __future__ import annotations

from pathlib import Path

import pyarrow.parquet as pq

from app.readers.base import BaseReader, ExtractedBlock


class ParquetReader(BaseReader):
    """Stream a Parquet file row-group by row-group and emit ExtractedBlock objects."""

    def __init__(self, path: str | Path) -> None:
        super().__init__(path)

    def read(self) -> list[ExtractedBlock]:
        """Iterate over row groups and yield one ExtractedBlock per non-null cell."""
        source = str(self.path)
        file_type = self.path.suffix.lstrip(".").lower() or "parquet"

        pf = pq.ParquetFile(str(self.path))
        all_blocks: list[ExtractedBlock] = []

        for row_group_index in range(pf.metadata.num_row_groups):
            table = pf.read_row_group(row_group_index)
            col_names = table.schema.names

            for col_index, col_name in enumerate(col_names):
                col_array = table.column(col_index)
                for value in col_array:
                    py_val = value.as_py()
                    if py_val is None:
                        continue
                    str_val = str(py_val).strip()
                    if not str_val:
                        continue
                    all_blocks.append(ExtractedBlock(
                        text=str_val,
                        page_or_sheet=row_group_index,
                        source_path=source,
                        file_type=file_type,
                        block_type="table_cell",
                        bbox=None,
                        col_header=col_name,
                    ))

        return all_blocks
