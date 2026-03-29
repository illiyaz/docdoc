"""PaddleOCR integration for scanned and corrupted PDF pages.

Called by pdf_reader.py when classifier.py labels a page "scanned" or
"corrupted". Renders the page to a raster image via PyMuPDF and feeds it
to PaddleOCR, which returns per-line text with bounding boxes.

PaddleOCR is the only permitted OCR engine. Tesseract must not be
introduced as an alternative (CLAUDE.md § 4, § 12).

Output blocks carry bbox in pixel coordinates matching the rendered image
resolution. page_or_sheet is inherited from the calling PDFReader.

Air-gap rule
------------
PaddleOCR is initialised with use_angle_cls=False and show_log=False.
Model weights must be pre-staged in the local models/ directory and
supplied via det_model_dir / rec_model_dir at deployment time so that
no outbound network call is ever made at runtime.
"""
from __future__ import annotations

import logging

import numpy as np
from paddleocr import PaddleOCR

from app.readers.base import ExtractedBlock

logger = logging.getLogger(__name__)


class OCREngine:
    """Thin wrapper around PaddleOCR.

    The PaddleOCR model is loaded exactly once during __init__.
    Re-using a single instance across many pages avoids the significant
    per-call initialisation cost of loading neural network weights.

    Not thread-safe: create one instance per concurrent document worker.
    """

    def __init__(
        self,
        lang: str = "en",
        det_model_dir: str | None = None,
        rec_model_dir: str | None = None,
    ) -> None:
        """Load PaddleOCR model weights.

        Parameters
        ----------
        lang:
            OCR language code passed to PaddleOCR (default "en").
        det_model_dir:
            Path to a locally staged text detection model directory.
            If None, PaddleOCR uses its own default cache location.
            Must be set to a local path in air-gap deployments.
        rec_model_dir:
            Path to a locally staged text recognition model directory.
            If None, PaddleOCR uses its own default cache location.
            Must be set to a local path in air-gap deployments.
        """
        self._lang = lang
        kwargs: dict[str, object] = {
            "lang": lang,
        }
        # PaddleOCR v3 removed show_log and use_angle_cls — only add if supported
        import inspect
        _paddle_params = inspect.signature(PaddleOCR.__init__).parameters
        if "show_log" in _paddle_params:
            kwargs["show_log"] = False
        if "use_angle_cls" in _paddle_params:
            kwargs["use_angle_cls"] = False
        if "use_gpu" in _paddle_params:
            kwargs["use_gpu"] = False
        if det_model_dir is not None:
            kwargs["det_model_dir"] = det_model_dir
        if rec_model_dir is not None:
            kwargs["rec_model_dir"] = rec_model_dir

        self._ocr = PaddleOCR(**kwargs)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ocr_page_image(
        self,
        image: object,      # PyMuPDF fitz.Pixmap
        page_num: int,
        source_path: str,
    ) -> list[ExtractedBlock]:
        """Run PaddleOCR on a rendered page image; return prose blocks with bbox.

        Parameters
        ----------
        image:
            PyMuPDF Pixmap produced by ``page.get_pixmap()``.  Converted to
            a numpy uint8 array of shape (H, W, channels) before inference.
        page_num:
            0-based page index forwarded to ``page_or_sheet`` on each block.
        source_path:
            Absolute path to the originating PDF file.

        Returns
        -------
        list[ExtractedBlock]
            One block per detected text line.  bbox is in pixel coordinates
            of the rendered image.  Whitespace-only detections are dropped.
        """
        img_array = np.frombuffer(image.samples, dtype=np.uint8).reshape(
            image.height, image.width, image.n
        )
        # PaddleOCR v3 renamed ocr() to predict() and removed cls param
        _ocr_fn = getattr(self._ocr, "predict", None) or self._ocr.ocr
        try:
            result = _ocr_fn(img_array)
        except TypeError:
            # Fallback for older PaddleOCR versions that accept cls
            result = self._ocr.ocr(img_array, cls=False)

        if not result or not result[0]:
            return []

        blocks: list[ExtractedBlock] = []
        for line in result[0]:
            # PaddleOCR v2: line = (box, (text, confidence))
            # PaddleOCR v3: line may be dict or different structure
            try:
                if isinstance(line, dict):
                    text = str(line.get("rec_text", line.get("text", "")))
                    box = line.get("dt_polys", line.get("box", [[0, 0], [0, 0], [0, 0], [0, 0]]))
                elif isinstance(line, (list, tuple)) and len(line) >= 2:
                    box = line[0]
                    text_data = line[1]
                    text = text_data[0] if isinstance(text_data, (list, tuple)) else str(text_data)
                else:
                    continue
            except (IndexError, TypeError, ValueError):
                continue
            if not text.strip():
                continue
            xs = [p[0] if isinstance(p, (list, tuple)) else 0 for p in box]
            ys = [p[1] if isinstance(p, (list, tuple)) else 0 for p in box]
            bbox = (float(min(xs)), float(min(ys)), float(max(xs)), float(max(ys)))
            blocks.append(ExtractedBlock(
                text=text,
                page_or_sheet=page_num,
                source_path=source_path,
                file_type="pdf",
                block_type="prose",
                bbox=bbox,
            ))

        return blocks


def ocr_pdf_to_blocks(
    pdf_path: str,
    dpi: int = 200,
    lang: str = "en",
) -> list[ExtractedBlock]:
    """Run PaddleOCR on every page of a scanned PDF.

    Opens the PDF with PyMuPDF, renders each page to a raster image,
    runs PaddleOCR, and returns ``ExtractedBlock`` objects compatible
    with the rest of the pipeline.

    Pages are streamed one at a time and released after OCR to keep
    memory usage bounded.

    Parameters
    ----------
    pdf_path:
        Absolute path to the PDF file.
    dpi:
        Render resolution.  200 DPI balances OCR accuracy and speed.
    lang:
        PaddleOCR language code (default ``"en"``).

    Returns
    -------
    list[ExtractedBlock]
        Blocks from all pages, ordered by page number then reading order.
    """
    import fitz  # PyMuPDF — lazy import to avoid top-level dependency

    engine = OCREngine(lang=lang)
    all_blocks: list[ExtractedBlock] = []

    doc = fitz.open(pdf_path)
    try:
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        for page_num in range(doc.page_count):
            page = doc[page_num]
            pix = page.get_pixmap(matrix=mat)
            page_blocks = engine.ocr_page_image(pix, page_num, pdf_path)
            all_blocks.extend(page_blocks)
            doc._forget_page(page)  # release memory
            logger.debug(
                "OCR page %d/%d: %d blocks",
                page_num + 1, doc.page_count, len(page_blocks),
            )
    finally:
        doc.close()

    logger.info(
        "OCR complete for %s: %d blocks across %d pages",
        pdf_path, len(all_blocks), doc.page_count,
    )
    return all_blocks
