"""PDF reader: PyMuPDF streaming with dual-path (digital vs. scanned/corrupted).

Architecture
------------
Every page is classified by classifier.py before processing:
  - digital   → text extracted directly via PyMuPDF get_text("dict")
  - scanned   → rendered to image, passed to PaddleOCR (ocr.py)
  - corrupted → sparse/degraded text layer; re-OCR'd with PaddleOCR

Table extraction
----------------
pdfplumber is permitted exclusively for table detection on each page
(find_tables / extract_tables only). All other text comes from PyMuPDF.
Table cells are emitted as ExtractedBlock with block_type="table_cell" or
"table_header", a shared table_id, and col_header/row_index populated.

Memory rule
-----------
doc._forget_page(n) is called immediately after each page is processed.
The full document is never resident in memory at once.
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import fitz  # PyMuPDF
import pdfplumber

from app.readers.base import BaseReader, ExtractedBlock
from app.readers.classifier import classify_page
from app.readers.ocr import OCREngine
from app.readers.onset import find_data_onset
from app.readers.stitcher import PageStitcher

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _bbox_overlaps(
    block_bbox: tuple[float, float, float, float],
    table_bbox: tuple[float, float, float, float],
) -> bool:
    """Return True if block_bbox and table_bbox share any area."""
    bx0, by0, bx1, by1 = block_bbox
    tx0, ty0, tx1, ty1 = table_bbox
    return not (bx1 <= tx0 or bx0 >= tx1 or by1 <= ty0 or by0 >= ty1)


class PDFReader(BaseReader):
    """Stream a PDF file page-by-page and emit ExtractedBlock objects."""

    def __init__(
        self,
        path: str | Path,
        db_session: Session | None = None,
        db_document_id: str | None = None,
    ) -> None:
        """Create a PDFReader.

        Parameters
        ----------
        path:
            Path to the PDF file.
        db_session:
            Optional SQLAlchemy Session.  When provided together with
            db_document_id, checkpoint data is persisted to the Document
            record's metadata_json after every completed page.
        db_document_id:
            UUID string of the Document ORM record for this file.  Required
            when db_session is provided; ignored otherwise.
        """
        super().__init__(path)
        self._checkpoint: dict[str, Any] = {}
        self._db_session = db_session
        self._db_document_id = db_document_id

    @property
    def checkpoint(self) -> dict[str, Any]:
        """Current in-memory checkpoint (document_id, last_completed_page)."""
        return dict(self._checkpoint)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def read(self) -> list[ExtractedBlock]:
        """Process all pages from onset_page onward and return blocks.

        Streams pages one at a time; doc._forget_page is called after each
        page to release memory immediately (CLAUDE.md § 2 memory rule).
        """
        doc = fitz.open(str(self.path))
        onset_page = find_data_onset(doc)
        stitcher = PageStitcher()
        ocr_engine = OCREngine()
        document_id = str(self.path)
        all_blocks: list[ExtractedBlock] = []

        with pdfplumber.open(str(self.path)) as plumber_doc:
            for page_num in range(onset_page, len(doc)):
                page = doc.load_page(page_num)
                page_blocks = self._process_page(
                    page, page_num, plumber_doc, stitcher, ocr_engine
                )
                all_blocks.extend(page_blocks)
                doc._forget_page(page_num)
                self._write_checkpoint(document_id, page_num, all_blocks)

        doc.close()
        return all_blocks

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _process_page(
        self,
        page: object,
        page_num: int,
        plumber_doc: object,
        stitcher: PageStitcher,
        ocr_engine: OCREngine,
    ) -> list[ExtractedBlock]:
        """Classify the page and dispatch to the appropriate extraction path."""
        label = classify_page(page)
        source = str(self.path)

        # Table extraction via pdfplumber (permitted for table detection only)
        table_blocks, table_bboxes = self._extract_tables(
            plumber_doc.pages[page_num], page_num
        )

        if label == "digital":
            prose_blocks = self._extract_prose(page, page_num, table_bboxes)
        else:
            # scanned or corrupted: render to raster image and OCR
            mat = fitz.Matrix(2, 2)  # 2× zoom improves OCR accuracy
            pix = page.get_pixmap(matrix=mat)
            prose_blocks = ocr_engine.ocr_page_image(pix, page_num, source)

        # Feed prose text through the tail-buffer stitcher so cross-page PII
        # boundaries are tracked for the downstream PII extraction stage.
        page_text = "\n".join(b.text for b in prose_blocks)
        stitcher.stitch(page_num, page_text)

        return table_blocks + prose_blocks

    def _extract_tables(
        self,
        plumber_page: object,
        page_num: int,
    ) -> tuple[list[ExtractedBlock], list[tuple[float, float, float, float]]]:
        """Use pdfplumber to detect tables; emit table_cell/table_header blocks.

        Returns (blocks, table_bboxes).  table_bboxes is forwarded to
        _extract_prose so overlapping text blocks are excluded from prose.
        """
        source = str(self.path)
        blocks: list[ExtractedBlock] = []
        table_bboxes: list[tuple[float, float, float, float]] = []

        detected_tables = plumber_page.find_tables()
        all_table_data = plumber_page.extract_tables()

        for table_obj, rows in zip(detected_tables, all_table_data):
            if not rows:
                continue

            table_id = str(uuid.uuid4())
            bbox = tuple(table_obj.bbox)
            table_bboxes.append(bbox)

            # Row 0 is the header row
            headers = [
                str(cell) if cell is not None else "" for cell in rows[0]
            ]
            for col_idx, cell_text in enumerate(headers):
                blocks.append(ExtractedBlock(
                    text=cell_text,
                    page_or_sheet=page_num,
                    source_path=source,
                    file_type="pdf",
                    block_type="table_header",
                    bbox=bbox,
                    row=0,
                    column=col_idx,
                    table_id=table_id,
                    col_header=cell_text,
                    row_index=0,
                ))

            # Rows 1+ are data rows
            for row_idx, row in enumerate(rows[1:], start=1):
                for col_idx, cell_text in enumerate(row):
                    col_header = headers[col_idx] if col_idx < len(headers) else ""
                    blocks.append(ExtractedBlock(
                        text=str(cell_text) if cell_text is not None else "",
                        page_or_sheet=page_num,
                        source_path=source,
                        file_type="pdf",
                        block_type="table_cell",
                        bbox=bbox,
                        row=row_idx,
                        column=col_idx,
                        table_id=table_id,
                        col_header=col_header,
                        row_index=row_idx,
                    ))

        return blocks, table_bboxes

    def _extract_prose(
        self,
        page: object,
        page_num: int,
        table_bboxes: list[tuple[float, float, float, float]],
    ) -> list[ExtractedBlock]:
        """Use PyMuPDF get_text('dict') to extract non-table text blocks.

        Blocks whose bounding box overlaps any detected table region are
        skipped — their content is already captured via _extract_tables.
        """
        source = str(self.path)
        blocks: list[ExtractedBlock] = []

        raw = page.get_text("dict")
        for block in raw.get("blocks", []):
            if block.get("type") != 0:  # 0 = text; 1 = image — skip images
                continue

            bbox = tuple(block["bbox"])
            if any(_bbox_overlaps(bbox, tb) for tb in table_bboxes):
                continue

            # Concatenate all span text within this block
            lines_text: list[str] = []
            for line in block.get("lines", []):
                span_text = "".join(
                    span.get("text", "") for span in line.get("spans", [])
                )
                if span_text:
                    lines_text.append(span_text)

            text = "\n".join(lines_text)
            if not text:
                continue

            blocks.append(ExtractedBlock(
                text=text,
                page_or_sheet=page_num,
                source_path=source,
                file_type="pdf",
                block_type="prose",
                bbox=bbox,
            ))

        return blocks

    def _write_checkpoint(
        self,
        document_id: str,
        page_num: int,
        partial: list[ExtractedBlock],
    ) -> None:
        """Write checkpoint after each completed page.

        Always updates the in-memory checkpoint dict.  When a db_session and
        db_document_id were provided at construction, also persists
        last_completed_page to Document.metadata_json so crashed jobs can
        resume from the correct page.

        Schema: {"document_id": str, "last_completed_page": int}
        """
        self._checkpoint = {
            "document_id": document_id,
            "last_completed_page": page_num,
        }

        if self._db_session is not None and self._db_document_id is not None:
            self._persist_checkpoint_to_db(page_num)

    def _persist_checkpoint_to_db(self, page_num: int) -> None:
        """Flush checkpoint to Document.metadata_json in the database.

        The Document record must already exist (created by DiscoveryTask).
        If the record is not found, a warning is logged and the operation
        is skipped — never raise from a checkpoint write.
        """
        from app.db.models import Document  # local import avoids circular deps
        try:
            doc = self._db_session.get(Document, self._db_document_id)
            if doc is None:
                logger.warning(
                    "Checkpoint skipped: Document id=%s not found in DB",
                    self._db_document_id,
                )
                return
            current_meta: dict = doc.metadata_json or {}
            current_meta["last_completed_page"] = page_num
            doc.metadata_json = current_meta
            self._db_session.flush()
        except Exception as exc:  # noqa: BLE001
            # Checkpoint writes must never crash the pipeline
            logger.warning("Checkpoint DB write failed (page=%d): %s", page_num, exc)
