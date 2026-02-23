"""Tests for app/readers/ocr.py — OCREngine.

paddleocr and numpy are injected into sys.modules before the module under
test is imported so that the top-level imports succeed without those
packages being installed.  Individual tests then patch
``app.readers.ocr.PaddleOCR`` to control model initialisation and OCR
results precisely.

PaddleOCR result format (mocked):
    result[0] is a list of detected lines.
    Each line: [box, (text, confidence)]
    box: [[x1,y1], [x2,y2], [x3,y3], [x4,y4]]  (4 corners, clockwise)

Covers:
  - ocr_page_image returns list[ExtractedBlock]
  - Each block has correct page_or_sheet, file_type, block_type
  - bbox is derived from PaddleOCR corner coordinates (axis-aligned rect)
  - Empty PaddleOCR result (None / empty list) returns []
  - PaddleOCR initialised once at __init__, not per ocr_page_image call
  - use_angle_cls=False, show_log=False passed to PaddleOCR constructor
  - lang parameter forwarded correctly
  - det_model_dir / rec_model_dir forwarded when provided
  - Whitespace-only detections are dropped
  - cls=False passed to every ocr() call
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Stub heavy dependencies in sys.modules BEFORE importing the module so that
# top-level `import numpy as np` and `from paddleocr import PaddleOCR`
# succeed without those packages installed.
# ---------------------------------------------------------------------------
_NUMPY_STUB = MagicMock(name="numpy_stub")
_PADDLEOCR_STUB = MagicMock(name="paddleocr_stub")
sys.modules.setdefault("numpy", _NUMPY_STUB)
sys.modules.setdefault("paddleocr", _PADDLEOCR_STUB)

from app.readers.ocr import OCREngine  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers / constants
# ---------------------------------------------------------------------------

# Canonical PaddleOCR single-line result: two text lines detected
_BOX_A = [[10, 20], [100, 20], [100, 40], [10, 40]]   # x∈[10,100] y∈[20,40]
_BOX_B = [[15, 55], [90, 55], [90, 70], [15, 70]]      # x∈[15,90]  y∈[55,70]
_PADDLE_RESULT_TWO_LINES = [
    [
        [_BOX_A, ("Hello world", 0.95)],
        [_BOX_B, ("Second line", 0.88)],
    ]
]

_PADDLE_RESULT_ONE_LINE = [
    [
        [_BOX_A, ("Only line", 0.97)],
    ]
]


def _make_pixmap() -> MagicMock:
    """Return a mock PyMuPDF Pixmap with numeric attributes."""
    pix = MagicMock()
    pix.samples = b"\x00" * (100 * 100 * 3)
    pix.height = 100
    pix.width = 100
    pix.n = 3
    return pix


def _engine_with_result(paddle_result) -> tuple[OCREngine, MagicMock]:
    """Return (OCREngine, mock_paddle_instance) with ocr() preset."""
    with patch("app.readers.ocr.PaddleOCR") as MockPaddleOCR:
        MockPaddleOCR.return_value.ocr.return_value = paddle_result
        engine = OCREngine()
    return engine, MockPaddleOCR.return_value


# ---------------------------------------------------------------------------
# 1. Return type and basic structure
# ---------------------------------------------------------------------------

def test_ocr_page_image_returns_list():
    engine, mock_paddle = _engine_with_result(_PADDLE_RESULT_TWO_LINES)
    result = engine.ocr_page_image(_make_pixmap(), 0, "test.pdf")
    assert isinstance(result, list)


def test_ocr_page_image_returns_extracted_blocks():
    from app.readers.base import ExtractedBlock
    engine, mock_paddle = _engine_with_result(_PADDLE_RESULT_TWO_LINES)
    result = engine.ocr_page_image(_make_pixmap(), 0, "test.pdf")
    for block in result:
        assert isinstance(block, ExtractedBlock)


def test_ocr_page_image_count_matches_detected_lines():
    engine, _ = _engine_with_result(_PADDLE_RESULT_TWO_LINES)
    result = engine.ocr_page_image(_make_pixmap(), 0, "test.pdf")
    assert len(result) == 2


def test_ocr_single_line_returns_one_block():
    engine, _ = _engine_with_result(_PADDLE_RESULT_ONE_LINE)
    result = engine.ocr_page_image(_make_pixmap(), 0, "test.pdf")
    assert len(result) == 1


# ---------------------------------------------------------------------------
# 2. Correct field values on each block
# ---------------------------------------------------------------------------

def test_block_page_or_sheet_matches_page_num():
    engine, _ = _engine_with_result(_PADDLE_RESULT_ONE_LINE)
    result = engine.ocr_page_image(_make_pixmap(), 7, "test.pdf")
    assert result[0].page_or_sheet == 7


def test_block_file_type_is_pdf():
    engine, _ = _engine_with_result(_PADDLE_RESULT_ONE_LINE)
    result = engine.ocr_page_image(_make_pixmap(), 0, "test.pdf")
    assert result[0].file_type == "pdf"


def test_block_block_type_is_prose():
    engine, _ = _engine_with_result(_PADDLE_RESULT_TWO_LINES)
    result = engine.ocr_page_image(_make_pixmap(), 0, "test.pdf")
    for block in result:
        assert block.block_type == "prose"


def test_block_source_path_is_forwarded():
    engine, _ = _engine_with_result(_PADDLE_RESULT_ONE_LINE)
    result = engine.ocr_page_image(_make_pixmap(), 0, "/docs/invoice.pdf")
    assert result[0].source_path == "/docs/invoice.pdf"


def test_block_text_matches_detected_text():
    engine, _ = _engine_with_result(_PADDLE_RESULT_ONE_LINE)
    result = engine.ocr_page_image(_make_pixmap(), 0, "test.pdf")
    assert result[0].text == "Only line"


def test_blocks_text_order_preserved():
    engine, _ = _engine_with_result(_PADDLE_RESULT_TWO_LINES)
    result = engine.ocr_page_image(_make_pixmap(), 0, "test.pdf")
    assert result[0].text == "Hello world"
    assert result[1].text == "Second line"


def test_block_table_id_is_none():
    engine, _ = _engine_with_result(_PADDLE_RESULT_ONE_LINE)
    result = engine.ocr_page_image(_make_pixmap(), 0, "test.pdf")
    assert result[0].table_id is None


def test_block_col_header_is_none():
    engine, _ = _engine_with_result(_PADDLE_RESULT_ONE_LINE)
    result = engine.ocr_page_image(_make_pixmap(), 0, "test.pdf")
    assert result[0].col_header is None


# ---------------------------------------------------------------------------
# 3. bbox derived from PaddleOCR corner coordinates
# ---------------------------------------------------------------------------

def test_bbox_is_axis_aligned_rect_from_corners():
    # _BOX_A corners: (10,20),(100,20),(100,40),(10,40)
    # Expected bbox: (x_min, y_min, x_max, y_max) = (10, 20, 100, 40)
    engine, _ = _engine_with_result(_PADDLE_RESULT_TWO_LINES)
    result = engine.ocr_page_image(_make_pixmap(), 0, "test.pdf")
    assert result[0].bbox == (10.0, 20.0, 100.0, 40.0)


def test_bbox_second_line_correct():
    # _BOX_B corners: (15,55),(90,55),(90,70),(15,70)
    engine, _ = _engine_with_result(_PADDLE_RESULT_TWO_LINES)
    result = engine.ocr_page_image(_make_pixmap(), 0, "test.pdf")
    assert result[1].bbox == (15.0, 55.0, 90.0, 70.0)


def test_bbox_is_tuple_of_four_floats():
    engine, _ = _engine_with_result(_PADDLE_RESULT_ONE_LINE)
    result = engine.ocr_page_image(_make_pixmap(), 0, "test.pdf")
    bbox = result[0].bbox
    assert isinstance(bbox, tuple) and len(bbox) == 4
    assert all(isinstance(v, float) for v in bbox)


def test_bbox_non_axis_aligned_box_uses_extremes():
    """Rotated box: bounding rect covers all four corners."""
    rotated_box = [[20, 10], [80, 5], [90, 45], [30, 50]]
    result_data = [[[rotated_box, ("Rotated text", 0.9)]]]
    engine, _ = _engine_with_result(result_data)
    result = engine.ocr_page_image(_make_pixmap(), 0, "test.pdf")
    bbox = result[0].bbox
    # x: min=20, max=90; y: min=5, max=50
    assert bbox == (20.0, 5.0, 90.0, 50.0)


# ---------------------------------------------------------------------------
# 4. Empty / None PaddleOCR result → empty list
# ---------------------------------------------------------------------------

def test_empty_result_returns_empty_list():
    engine, _ = _engine_with_result([[]])
    result = engine.ocr_page_image(_make_pixmap(), 0, "test.pdf")
    assert result == []


def test_none_result_returns_empty_list():
    engine, _ = _engine_with_result(None)
    result = engine.ocr_page_image(_make_pixmap(), 0, "test.pdf")
    assert result == []


def test_result_with_empty_inner_list_returns_empty():
    engine, _ = _engine_with_result([[]])
    result = engine.ocr_page_image(_make_pixmap(), 0, "test.pdf")
    assert result == []


def test_whitespace_only_line_is_dropped():
    whitespace_result = [[[_BOX_A, ("   ", 0.60)]]]
    engine, _ = _engine_with_result(whitespace_result)
    result = engine.ocr_page_image(_make_pixmap(), 0, "test.pdf")
    assert result == []


def test_mixed_whitespace_and_real_text_keeps_real():
    mixed = [
        [
            [_BOX_A, ("  ", 0.50)],
            [_BOX_B, ("Real text", 0.91)],
        ]
    ]
    engine, _ = _engine_with_result(mixed)
    result = engine.ocr_page_image(_make_pixmap(), 0, "test.pdf")
    assert len(result) == 1
    assert result[0].text == "Real text"


# ---------------------------------------------------------------------------
# 5. PaddleOCR initialised once at __init__, not per ocr_page_image call
# ---------------------------------------------------------------------------

def test_paddle_ocr_initialised_once_on_engine_creation():
    with patch("app.readers.ocr.PaddleOCR") as MockPaddleOCR:
        MockPaddleOCR.return_value.ocr.return_value = [[]]
        engine = OCREngine()
        assert MockPaddleOCR.call_count == 1


def test_paddle_ocr_not_reinitialised_on_multiple_calls():
    with patch("app.readers.ocr.PaddleOCR") as MockPaddleOCR:
        MockPaddleOCR.return_value.ocr.return_value = [[]]
        engine = OCREngine()
        pix = _make_pixmap()
        engine.ocr_page_image(pix, 0, "test.pdf")
        engine.ocr_page_image(pix, 1, "test.pdf")
        engine.ocr_page_image(pix, 2, "test.pdf")
        # Still exactly one PaddleOCR() constructor call
        assert MockPaddleOCR.call_count == 1


def test_two_engines_each_initialise_once():
    with patch("app.readers.ocr.PaddleOCR") as MockPaddleOCR:
        MockPaddleOCR.return_value.ocr.return_value = [[]]
        OCREngine()
        OCREngine()
        assert MockPaddleOCR.call_count == 2


# ---------------------------------------------------------------------------
# 6. Constructor keyword arguments (no network calls)
# ---------------------------------------------------------------------------

def test_use_angle_cls_is_false():
    with patch("app.readers.ocr.PaddleOCR") as MockPaddleOCR:
        OCREngine()
    kwargs = MockPaddleOCR.call_args.kwargs
    assert kwargs["use_angle_cls"] is False


def test_show_log_is_false():
    with patch("app.readers.ocr.PaddleOCR") as MockPaddleOCR:
        OCREngine()
    kwargs = MockPaddleOCR.call_args.kwargs
    assert kwargs["show_log"] is False


def test_use_gpu_is_false():
    with patch("app.readers.ocr.PaddleOCR") as MockPaddleOCR:
        OCREngine()
    kwargs = MockPaddleOCR.call_args.kwargs
    assert kwargs["use_gpu"] is False


def test_default_lang_is_en():
    with patch("app.readers.ocr.PaddleOCR") as MockPaddleOCR:
        OCREngine()
    kwargs = MockPaddleOCR.call_args.kwargs
    assert kwargs["lang"] == "en"


def test_custom_lang_forwarded():
    with patch("app.readers.ocr.PaddleOCR") as MockPaddleOCR:
        OCREngine(lang="ch")
    kwargs = MockPaddleOCR.call_args.kwargs
    assert kwargs["lang"] == "ch"


def test_det_model_dir_forwarded_when_given():
    with patch("app.readers.ocr.PaddleOCR") as MockPaddleOCR:
        OCREngine(det_model_dir="/models/det")
    kwargs = MockPaddleOCR.call_args.kwargs
    assert kwargs["det_model_dir"] == "/models/det"


def test_rec_model_dir_forwarded_when_given():
    with patch("app.readers.ocr.PaddleOCR") as MockPaddleOCR:
        OCREngine(rec_model_dir="/models/rec")
    kwargs = MockPaddleOCR.call_args.kwargs
    assert kwargs["rec_model_dir"] == "/models/rec"


def test_det_model_dir_absent_when_not_given():
    with patch("app.readers.ocr.PaddleOCR") as MockPaddleOCR:
        OCREngine()
    kwargs = MockPaddleOCR.call_args.kwargs
    assert "det_model_dir" not in kwargs


def test_rec_model_dir_absent_when_not_given():
    with patch("app.readers.ocr.PaddleOCR") as MockPaddleOCR:
        OCREngine()
    kwargs = MockPaddleOCR.call_args.kwargs
    assert "rec_model_dir" not in kwargs


# ---------------------------------------------------------------------------
# 7. ocr() call arguments
# ---------------------------------------------------------------------------

def test_ocr_called_with_cls_false():
    with patch("app.readers.ocr.PaddleOCR") as MockPaddleOCR:
        MockPaddleOCR.return_value.ocr.return_value = [[]]
        engine = OCREngine()
        engine.ocr_page_image(_make_pixmap(), 0, "test.pdf")
    _, call_kwargs = MockPaddleOCR.return_value.ocr.call_args
    assert call_kwargs.get("cls") is False


def test_ocr_called_once_per_page():
    with patch("app.readers.ocr.PaddleOCR") as MockPaddleOCR:
        MockPaddleOCR.return_value.ocr.return_value = [[]]
        engine = OCREngine()
        pix = _make_pixmap()
        engine.ocr_page_image(pix, 0, "test.pdf")
        engine.ocr_page_image(pix, 1, "test.pdf")
    assert MockPaddleOCR.return_value.ocr.call_count == 2


# ---------------------------------------------------------------------------
# 8. page_num forwarded correctly across multiple calls
# ---------------------------------------------------------------------------

def test_page_num_forwarded_to_each_block():
    with patch("app.readers.ocr.PaddleOCR") as MockPaddleOCR:
        MockPaddleOCR.return_value.ocr.return_value = _PADDLE_RESULT_TWO_LINES
        engine = OCREngine()
        pix = _make_pixmap()
        blocks_p3 = engine.ocr_page_image(pix, 3, "test.pdf")
        blocks_p9 = engine.ocr_page_image(pix, 9, "test.pdf")

    assert all(b.page_or_sheet == 3 for b in blocks_p3)
    assert all(b.page_or_sheet == 9 for b in blocks_p9)
