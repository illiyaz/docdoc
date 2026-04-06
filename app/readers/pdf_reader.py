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
from app.readers.onset import find_data_onset
from app.readers.stitcher import PageStitcher

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def get_pdf_page_count(path: str | Path) -> int:
    """Return the number of pages in a PDF without reading content.

    Returns 0 on any error (missing file, corrupt PDF, etc.).
    """
    try:
        doc = fitz.open(str(path))
        count = doc.page_count
        doc.close()
        return count
    except Exception:
        return 0


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

    def read_pages(self, page_numbers: list[int]) -> list[ExtractedBlock]:
        """Read only the specified pages (0-based) and return blocks.

        Uses PyMuPDF random access — pages not in ``page_numbers`` are never
        loaded.  ``_forget_page()`` is called after each page for memory
        efficiency.  Out-of-range page numbers are silently skipped.
        """
        if not page_numbers:
            return []

        doc = fitz.open(str(self.path))
        stitcher = PageStitcher()
        ocr_engine = None
        all_blocks: list[ExtractedBlock] = []

        with pdfplumber.open(str(self.path)) as plumber_doc:
            for page_num in page_numbers:
                if page_num < 0 or page_num >= len(doc):
                    continue
                page = doc.load_page(page_num)
                page_blocks, ocr_engine = self._process_page(
                    page, page_num, plumber_doc, stitcher, ocr_engine
                )
                all_blocks.extend(page_blocks)
                doc._forget_page(page_num)

        doc.close()
        return all_blocks

    _LARGE_DOC_THRESHOLD = 500  # pages

    def read(self) -> list[ExtractedBlock]:
        """Process all pages from onset_page onward and return blocks.

        Streams pages one at a time; doc._forget_page is called after each
        page to release memory immediately (CLAUDE.md § 2 memory rule).

        For large docs (>500 pages), uses sampled approach:
        1. Sample 10 pages to check if text layer exists
        2. If text layer present: fast text-only path (no OCR, no pdfplumber)
        3. If scanned (no text): bounded OCR on sampled pages only
           (every Nth page to stay under memory budget)
        """
        doc = fitz.open(str(self.path))
        onset_page = find_data_onset(doc)
        stitcher = PageStitcher()
        ocr_engine = None
        document_id = str(self.path)
        all_blocks: list[ExtractedBlock] = []
        is_large = doc.page_count > self._LARGE_DOC_THRESHOLD

        if is_large:
            # --- Sampled read for large docs ---
            text_layer_ratio = self._sample_text_layer(doc, onset_page)
            logger.info(
                "Large PDF (%d pages): text layer on %.0f%% of sampled pages",
                doc.page_count, text_layer_ratio * 100,
            )

            if text_layer_ratio >= 0.5:
                # Majority text — fast path: PyMuPDF text only, no OCR/pdfplumber
                all_blocks = self._read_text_only(doc, onset_page, stitcher, document_id)
            else:
                # Mostly scanned — bounded OCR: every Nth page to cap at ~100 pages
                stride = max(1, doc.page_count // 100)
                logger.info(
                    "Scanned large PDF: OCR every %d pages (~%d pages total)",
                    stride, doc.page_count // stride,
                )
                all_blocks = self._read_sampled_ocr(
                    doc, onset_page, stitcher, document_id, stride,
                )
            doc.close()
            return all_blocks

        # --- Standard read for normal docs ---
        with pdfplumber.open(str(self.path)) as plumber_doc:
            for page_num in range(onset_page, len(doc)):
                page = doc.load_page(page_num)
                page_blocks, ocr_engine = self._process_page(
                    page, page_num, plumber_doc, stitcher, ocr_engine,
                )
                all_blocks.extend(page_blocks)
                doc._forget_page(page_num)
                self._write_checkpoint(document_id, page_num, all_blocks)

        doc.close()
        return all_blocks

    def _sample_text_layer(self, doc: object, onset_page: int, n: int = 10) -> float:
        """Sample N pages to check what fraction has a text layer.

        Returns ratio (0.0–1.0) of sampled pages with ≥50 words.
        """
        total_pages = doc.page_count
        sample_pages = []
        stride = max(1, (total_pages - onset_page) // n)
        for i in range(n):
            pg = onset_page + i * stride
            if pg < total_pages:
                sample_pages.append(pg)

        if not sample_pages:
            return 1.0

        text_count = 0
        for pg in sample_pages:
            page = doc[pg]
            words = page.get_text("words")
            if len(words) >= 50:
                text_count += 1
            doc._forget_page(pg)

        return text_count / len(sample_pages)

    def _sample_tables(
        self, doc: object, onset_page: int, n: int = 5,
    ) -> list[str]:
        """Sample N pages with pdfplumber to detect table column headers.

        Opens pdfplumber briefly on a few pages, extracts column headers
        from any tables found, then closes.  Returns a deduplicated list
        of header strings (lowercased).  Returns empty list if no tables
        found or pdfplumber fails.

        This is lightweight — pdfplumber only processes 5 pages, not 3000.
        """
        total_pages = doc.page_count
        stride = max(1, (total_pages - onset_page) // n)
        sample_pages = []
        for i in range(n):
            pg = onset_page + i * stride
            if pg < total_pages:
                sample_pages.append(pg)

        if not sample_pages:
            return []

        headers: set[str] = set()
        try:
            with pdfplumber.open(str(self.path)) as plumber_doc:
                for pg in sample_pages:
                    if pg >= len(plumber_doc.pages):
                        continue
                    plumber_page = plumber_doc.pages[pg]
                    tables = plumber_page.extract_tables()
                    for rows in tables:
                        if not rows or not rows[0]:
                            continue
                        for cell in rows[0]:
                            if cell and isinstance(cell, str) and len(cell.strip()) >= 2:
                                headers.add(cell.strip().lower())
        except Exception:
            logger.debug("Table sampling failed for %s", self.path, exc_info=True)
            return []

        if headers:
            logger.info(
                "Large PDF table sampling: found %d column headers on %d sample pages: %s",
                len(headers), len(sample_pages),
                ", ".join(sorted(headers)[:10]),  # log first 10
            )
        return sorted(headers)

    def _read_text_only(
        self, doc: object, onset_page: int,
        stitcher: PageStitcher, document_id: str,
    ) -> list[ExtractedBlock]:
        """Fast path: extract text layer only via PyMuPDF. No OCR, no pdfplumber.

        If table column headers were detected during sampling, tags prose
        blocks with col_header context by matching header text against each
        block's content.  This preserves field classification context for
        downstream PII detection without running pdfplumber on every page.
        """
        # Sample for tables first (lightweight — 3 pages only)
        table_headers = self._sample_tables(doc, onset_page)

        all_blocks: list[ExtractedBlock] = []
        for page_num in range(onset_page, doc.page_count):
            page = doc.load_page(page_num)
            prose_blocks = self._extract_prose(page, page_num, set())

            # If table headers found, tag blocks with matching col_header
            if table_headers:
                prose_blocks = self._tag_table_context(prose_blocks, table_headers)

            page_text = "\n".join(b.text for b in prose_blocks)
            stitcher.stitch(page_num, page_text)
            all_blocks.extend(prose_blocks)
            doc._forget_page(page_num)
        return all_blocks

    def _tag_table_context(
        self,
        blocks: list[ExtractedBlock],
        headers: list[str],
    ) -> list[ExtractedBlock]:
        """Tag prose blocks whose text matches a known table column header.

        When a block's text (lowercased, stripped) exactly matches a learned
        column header, set its col_header field.  When a block's text
        contains a header as a prefix label (e.g. "Name: John Smith"),
        set col_header to the header portion.

        This gives downstream PII detection the same field-classification
        context that pdfplumber table extraction would provide, but without
        the per-page cost.
        """
        if not headers:
            return blocks

        header_set = frozenset(headers)
        tagged: list[ExtractedBlock] = []
        for block in blocks:
            text_lower = block.text.strip().lower()

            # Exact match: the block IS a header label
            if text_lower in header_set:
                tagged.append(ExtractedBlock(
                    text=block.text,
                    page_or_sheet=block.page_or_sheet,
                    source_path=block.source_path,
                    file_type=block.file_type,
                    block_type="table_header",
                    bbox=block.bbox,
                    col_header=block.text.strip(),
                    row=block.row,
                    column=block.column,
                    table_id=block.table_id,
                    row_index=block.row_index,
                ))
                continue

            # Prefix match: "Name: John Smith" → col_header="name"
            matched_header = None
            for h in headers:
                if text_lower.startswith(h) and len(text_lower) > len(h):
                    # Check delimiter after header (colon, tab, 2+ spaces)
                    rest = block.text.strip()[len(h):]
                    if rest and rest[0] in (":", "\t", " "):
                        matched_header = h
                        break

            if matched_header:
                tagged.append(ExtractedBlock(
                    text=block.text,
                    page_or_sheet=block.page_or_sheet,
                    source_path=block.source_path,
                    file_type=block.file_type,
                    block_type=block.block_type,
                    bbox=block.bbox,
                    col_header=matched_header,
                    row=block.row,
                    column=block.column,
                    table_id=block.table_id,
                    row_index=block.row_index,
                ))
            else:
                tagged.append(block)

        return tagged

    def _read_sampled_ocr(
        self, doc: object, onset_page: int,
        stitcher: PageStitcher, document_id: str, stride: int,
    ) -> list[ExtractedBlock]:
        """Bounded OCR for large scanned docs: every Nth page.

        Caps total OCR'd pages at ~100 to stay under memory budget.
        Text pages (if any) are still extracted via fast path.
        """
        all_blocks: list[ExtractedBlock] = []
        ocr_engine = None
        source = str(self.path)

        for page_num in range(onset_page, doc.page_count):
            page = doc.load_page(page_num)
            label = classify_page(page)

            if label == "digital":
                # Has text — extract it regardless of stride
                prose_blocks = self._extract_prose(page, page_num, set())
                all_blocks.extend(prose_blocks)
            elif (page_num - onset_page) % stride == 0:
                # Scanned page on stride boundary — OCR it
                try:
                    if ocr_engine is None:
                        from app.readers.ocr import OCREngine
                        ocr_engine = OCREngine()
                    mat = fitz.Matrix(2, 2)
                    pix = page.get_pixmap(matrix=mat)
                    ocr_blocks = ocr_engine.ocr_page_image(pix, page_num, source)
                    all_blocks.extend(ocr_blocks)
                except Exception:
                    pass  # Skip page on OCR failure
            # else: scanned page not on stride — skip (sampled)

            page_text = "\n".join(b.text for b in all_blocks if b.page_or_sheet == page_num)
            stitcher.stitch(page_num, page_text)
            doc._forget_page(page_num)

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
        ocr_engine: object | None,
    ) -> tuple[list[ExtractedBlock], object | None]:
        """Classify the page and dispatch to the appropriate extraction path.

        Returns (blocks, ocr_engine) — ocr_engine may be lazily created.
        """
        label = classify_page(page)
        source = str(self.path)

        # Table extraction via pdfplumber (permitted for table detection only)
        table_blocks, table_bboxes = self._extract_tables(
            plumber_doc.pages[page_num], page_num
        )

        if label == "digital":
            prose_blocks = self._extract_prose(page, page_num, table_bboxes)
        elif ocr_engine == "SKIP":
            # Large doc — skip OCR, extract whatever text layer exists
            prose_blocks = self._extract_prose(page, page_num, table_bboxes)
        else:
            # scanned or corrupted: render to raster image and OCR
            try:
                if ocr_engine is None:
                    from app.readers.ocr import OCREngine
                    ocr_engine = OCREngine()
                mat = fitz.Matrix(2, 2)  # 2× zoom improves OCR accuracy
                pix = page.get_pixmap(matrix=mat)
                prose_blocks = ocr_engine.ocr_page_image(pix, page_num, source)
            except (ImportError, ModuleNotFoundError):
                # PaddleOCR not installed — fall back to whatever text
                # PyMuPDF can extract (may be sparse but better than crashing)
                logger.warning(
                    "PaddleOCR not available; falling back to PyMuPDF text "
                    "for %s page %d (classified as %s)",
                    label, page_num, label,
                )
                prose_blocks = self._extract_prose(page, page_num, table_bboxes)

        # Feed prose text through the tail-buffer stitcher so cross-page PII
        # boundaries are tracked for the downstream PII extraction stage.
        page_text = "\n".join(b.text for b in prose_blocks)
        stitcher.stitch(page_num, page_text)

        return table_blocks + prose_blocks, ocr_engine

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
