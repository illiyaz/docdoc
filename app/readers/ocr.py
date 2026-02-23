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

import numpy as np
from paddleocr import PaddleOCR

from app.readers.base import ExtractedBlock


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
            "use_angle_cls": False,  # angle classifier not needed; avoids loading a third model
            "lang": lang,
            "show_log": False,       # suppress PaddleOCR stdout chatter
            "use_gpu": False,        # CPU inference; GPU support is opt-in at deploy time
        }
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
        # cls=False: do not run angle classifier (model not loaded at init)
        result = self._ocr.ocr(img_array, cls=False)

        if not result or not result[0]:
            return []

        blocks: list[ExtractedBlock] = []
        for line in result[0]:
            box, (text, _confidence) = line
            if not text.strip():
                continue
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
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
