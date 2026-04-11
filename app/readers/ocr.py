"""OCR integration for scanned and corrupted PDF pages.

Called by pdf_reader.py when classifier.py labels a page "scanned" or
"corrupted". Renders the page to a raster image via PyMuPDF and feeds it
to the OCR engine, which returns per-line text with bounding boxes.

Primary engine: **docTR** (Apache 2.0, word-level bboxes, 16-767x faster
than alternatives per April 2026 evaluation).
Fallback: PaddleOCR (activated only if docTR is unavailable).
Tesseract must not be introduced (CLAUDE.md § 4, § 12).

Output blocks carry bbox in pixel coordinates matching the rendered image
resolution. page_or_sheet is inherited from the calling PDFReader.

Air-gap rule
------------
Model weights must be pre-staged locally. No outbound network calls at
runtime.  Set ``PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True`` if using
PaddleOCR fallback.

Performance note
----------------
Import and model instantiation are deferred until first use.  A module-level
singleton (``get_ocr_engine()``) is reused across all callers within the
same process, so text-PDF pipelines never pay the init cost.
"""
from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod

import numpy as np

from app.readers.base import ExtractedBlock

logger = logging.getLogger(__name__)

# Module-level singleton — created on first use, reused across all callers.
_default_engine: "BaseOCREngine | None" = None
_engine_lock = threading.Lock()


def get_ocr_engine(lang: str = "en") -> "BaseOCREngine":
    """Return the module-level singleton OCR engine, creating it on first call.

    Tries docTR first (faster, word-level bboxes).  Falls back to PaddleOCR
    if docTR is not installed.  Thread-safe.
    """
    global _default_engine
    if _default_engine is not None and _default_engine._lang == lang:
        return _default_engine
    with _engine_lock:
        # Double-check inside lock
        if _default_engine is not None and _default_engine._lang == lang:
            return _default_engine
        try:
            _default_engine = DocTREngine(lang=lang)
            logger.info("OCR engine: docTR (primary)")
        except (ImportError, ModuleNotFoundError):
            logger.warning("docTR not available — falling back to PaddleOCR")
            _default_engine = PaddleOCREngine(lang=lang)
        return _default_engine


# Also expose for legacy callers that import OCREngine directly
OCREngine = None  # set after class definitions


# ──────────────────────────────────────────────────────────────
# Base class
# ──────────────────────────────────────────────────────────────

class BaseOCREngine(ABC):
    """Abstract OCR engine interface.

    Subclasses must implement ``ocr_page_image()`` which converts a
    PyMuPDF Pixmap into a list of ``ExtractedBlock`` objects.
    """

    _lang: str

    @abstractmethod
    def ocr_page_image(
        self,
        image: object,      # PyMuPDF fitz.Pixmap
        page_num: int,
        source_path: str,
    ) -> list[ExtractedBlock]:
        """Run OCR on a rendered page image and return prose blocks with bbox."""
        ...


# ──────────────────────────────────────────────────────────────
# docTR engine (primary)
# ──────────────────────────────────────────────────────────────

class DocTREngine(BaseOCREngine):
    """OCR engine backed by docTR (Apache 2.0).

    Uses ``db_resnet50`` (detection) + ``crnn_vgg16_bn`` (recognition).
    Word-level bounding boxes are aggregated into line-level blocks to
    match the interface expected by the rest of the pipeline.

    Automatically uses MPS (Apple Silicon) or CUDA when available.
    """

    def __init__(self, lang: str = "en") -> None:
        from doctr.models import ocr_predictor
        import torch

        self._lang = lang

        # Pick best available device
        if torch.cuda.is_available():
            self._device = torch.device("cuda")
        elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            self._device = torch.device("mps")
        else:
            self._device = torch.device("cpu")

        self._predictor = ocr_predictor(
            det_arch="db_resnet50",
            reco_arch="crnn_vgg16_bn",
            pretrained=True,
            assume_straight_pages=True,
            preserve_aspect_ratio=True,
        )

        if self._device.type != "cpu":
            self._predictor = self._predictor.to(self._device)

        logger.info("docTR loaded: device=%s", self._device.type)

    def ocr_page_image(
        self,
        image: object,
        page_num: int,
        source_path: str,
    ) -> list[ExtractedBlock]:
        """Run docTR on a rendered page image; return line-level blocks."""
        img_array = np.frombuffer(image.samples, dtype=np.uint8).reshape(
            image.height, image.width, image.n,
        )
        # docTR expects RGB; drop alpha channel if present
        if img_array.shape[2] == 4:
            img_array = img_array[:, :, :3]

        result = self._predictor([img_array])
        export = result.export()

        if not export.get("pages"):
            return []

        page_data = export["pages"][0]
        page_h, page_w = page_data.get("dimensions", (image.height, image.width))

        blocks: list[ExtractedBlock] = []
        for block in page_data.get("blocks", []):
            for line in block.get("lines", []):
                words = line.get("words", [])
                if not words:
                    continue

                # Aggregate words into a single line
                line_text = " ".join(w.get("value", "") for w in words)
                if not line_text.strip():
                    continue

                # Compute line bbox from word bboxes (convert normalized → pixel)
                x_mins, y_mins, x_maxs, y_maxs = [], [], [], []
                confidences: list[float] = []
                for w in words:
                    geom = w.get("geometry", [])
                    conf = w.get("confidence", 0.0)
                    confidences.append(conf)
                    if geom and len(geom) >= 2:
                        x_mins.append(geom[0][0] * page_w)
                        y_mins.append(geom[0][1] * page_h)
                        x_maxs.append(geom[1][0] * page_w)
                        y_maxs.append(geom[1][1] * page_h)

                if not x_mins:
                    continue

                bbox = (
                    float(min(x_mins)),
                    float(min(y_mins)),
                    float(max(x_maxs)),
                    float(max(y_maxs)),
                )
                avg_conf = sum(confidences) / len(confidences) if confidences else None

                blocks.append(ExtractedBlock(
                    text=line_text,
                    page_or_sheet=page_num,
                    source_path=source_path,
                    file_type="pdf",
                    block_type="prose",
                    bbox=bbox,
                    ocr_confidence=avg_conf,
                ))

        return blocks


# ──────────────────────────────────────────────────────────────
# PaddleOCR engine (fallback)
# ──────────────────────────────────────────────────────────────

class PaddleOCREngine(BaseOCREngine):
    """OCR engine backed by PaddleOCR (fallback if docTR unavailable)."""

    def __init__(
        self,
        lang: str = "en",
        det_model_dir: str | None = None,
        rec_model_dir: str | None = None,
    ) -> None:
        import os
        os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
        from paddleocr import PaddleOCR

        self._lang = lang
        kwargs: dict[str, object] = {"lang": lang}

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

    def ocr_page_image(
        self,
        image: object,
        page_num: int,
        source_path: str,
    ) -> list[ExtractedBlock]:
        """Run PaddleOCR on a rendered page image; return prose blocks with bbox."""
        img_array = np.frombuffer(image.samples, dtype=np.uint8).reshape(
            image.height, image.width, image.n,
        )
        _ocr_fn = getattr(self._ocr, "predict", None) or self._ocr.ocr
        try:
            result = _ocr_fn(img_array)
        except TypeError:
            result = self._ocr.ocr(img_array, cls=False)

        if not result or not result[0]:
            return []

        blocks: list[ExtractedBlock] = []
        for line in result[0]:
            confidence: float | None = None
            try:
                if isinstance(line, dict):
                    text = str(line.get("rec_text", line.get("text", "")))
                    box = line.get("dt_polys", line.get("box", [[0, 0]] * 4))
                    conf_val = line.get("rec_score", line.get("confidence", None))
                    if conf_val is not None:
                        confidence = float(conf_val)
                elif isinstance(line, (list, tuple)) and len(line) >= 2:
                    box = line[0]
                    text_data = line[1]
                    text = text_data[0] if isinstance(text_data, (list, tuple)) else str(text_data)
                    if isinstance(text_data, (list, tuple)) and len(text_data) >= 2:
                        try:
                            confidence = float(text_data[1])
                        except (ValueError, TypeError):
                            pass
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
                ocr_confidence=confidence,
            ))

        return blocks


# Legacy alias — callers that do ``from app.readers.ocr import OCREngine``
# get whichever engine ``get_ocr_engine()`` would pick.
OCREngine = DocTREngine


# ──────────────────────────────────────────────────────────────
# Convenience: full-PDF OCR
# ──────────────────────────────────────────────────────────────

def ocr_pdf_to_blocks(
    pdf_path: str,
    dpi: int = 200,
    lang: str = "en",
) -> list[ExtractedBlock]:
    """Run OCR on every page of a scanned PDF.

    Opens the PDF with PyMuPDF, renders each page to a raster image,
    runs OCR, and returns ``ExtractedBlock`` objects compatible with
    the rest of the pipeline.

    Pages are streamed one at a time and released after OCR to keep
    memory usage bounded.
    """
    import fitz

    engine = get_ocr_engine(lang=lang)
    all_blocks: list[ExtractedBlock] = []

    doc = fitz.open(pdf_path)
    try:
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        for page_num in range(doc.page_count):
            page = doc[page_num]
            pix = page.get_pixmap(matrix=mat)
            page_blocks = engine.ocr_page_image(pix, page_num, pdf_path)
            all_blocks.extend(page_blocks)
            doc._forget_page(page)
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
