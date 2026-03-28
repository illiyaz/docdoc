"""Tests for LiteParse spatial text routing (Step 26).

Covers:
- Graceful fallback when LiteParse not installed
- Fallback for scanned PDFs (word_count < 50)
- Successful spatial text routing with mocked LiteParse + OllamaClient
- 4000-char cap on spatial text sent to LLM
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# liteparse_adapter tests
# ---------------------------------------------------------------------------


class TestLiteParseAdapter:
    """Tests for app.readers.liteparse_adapter."""

    def test_get_spatial_text_fallback_when_not_installed(self):
        """get_spatial_text returns None when LiteParse import fails."""
        from app.readers import liteparse_adapter

        # Simulate ImportError by making the function's internal import fail
        original_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

        def _mock_import(name, *args, **kwargs):
            if name == "liteparse":
                raise ImportError("No module named 'liteparse'")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=_mock_import):
            result = liteparse_adapter.get_spatial_text("/fake/doc.pdf", 0)
            assert result is None

    def test_is_available_false_when_not_installed(self):
        """is_available returns False when LiteParse import fails."""
        from app.readers import liteparse_adapter

        original_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

        def _mock_import(name, *args, **kwargs):
            if name == "liteparse":
                raise ImportError("No module named 'liteparse'")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=_mock_import):
            assert liteparse_adapter.is_available() is False

    def test_get_spatial_text_returns_page_text(self):
        """get_spatial_text returns text from the requested page."""
        from app.readers import liteparse_adapter

        mock_page = MagicMock()
        mock_page.text = "Name: John Doe\nSSN: 123-45-6789\nAddress: 123 Main St"

        mock_result = MagicMock()
        mock_result.pages = [mock_page]

        mock_parser = MagicMock()
        mock_parser.parse.return_value = mock_result

        with patch("liteparse.LiteParse", return_value=mock_parser):
            result = liteparse_adapter.get_spatial_text("/test/doc.pdf", 0)
            assert result == mock_page.text
            mock_parser.parse.assert_called_once_with("/test/doc.pdf", max_pages=1)

    def test_get_spatial_text_returns_none_on_exception(self):
        """get_spatial_text returns None if parse raises an exception."""
        from app.readers import liteparse_adapter

        mock_parser = MagicMock()
        mock_parser.parse.side_effect = RuntimeError("parse failed")

        with patch("liteparse.LiteParse", return_value=mock_parser):
            result = liteparse_adapter.get_spatial_text("/test/doc.pdf", 0)
            assert result is None


# ---------------------------------------------------------------------------
# VisionRouter spatial text routing tests
# ---------------------------------------------------------------------------


def _make_router(
    generate_response: str | None = None,
    generate_side_effect: Exception | None = None,
):
    """Create a VisionRouter with a mocked OllamaClient."""
    mock_client = MagicMock()
    mock_client.model = "qwen2.5:7b"

    if generate_side_effect:
        mock_client.generate.side_effect = generate_side_effect
    else:
        mock_client.generate.return_value = generate_response or ""

    from app.pipeline.vision_router import VisionRouter
    router = VisionRouter(
        ollama_client=mock_client,
        vision_model="qwen2.5vl:32b",
        fallback_model="llama3.2-vision",
    )
    return router, mock_client


class TestSpatialTextRouting:
    """Tests for VisionRouter._route_via_spatial_text."""

    def test_spatial_text_fallback_when_not_installed(self):
        """Returns None when LiteParse is not installed."""
        router, _ = _make_router()

        with patch("app.pipeline.vision_router.VisionRouter._route_via_spatial_text") as orig:
            # Call the real method but mock liteparse import
            orig.side_effect = None  # disable mock
            pass

        # Actually test via the real method with mocked import
        with patch(
            "app.readers.liteparse_adapter.get_spatial_text", return_value=None,
        ):
            result = router._route_via_spatial_text("/fake/doc.pdf", 0, 100)
            assert result is None

    def test_spatial_text_fallback_on_short_text(self):
        """Returns None when spatial text is too short (< 100 chars)."""
        router, _ = _make_router()
        with patch(
            "app.readers.liteparse_adapter.get_spatial_text",
            return_value="Short text",
        ):
            result = router._route_via_spatial_text("/fake/doc.pdf", 0, 100)
            assert result is None

    def test_spatial_text_routing_success(self):
        """Successful routing via spatial text + text LLM."""
        llm_response = json.dumps({
            "pii_fields": [
                {"type": "PERSON", "value": "John Doe", "label": "Name:", "position": "top_left"},
                {"type": "US_SSN", "value": "123-45-6789", "label": "SSN:", "position": "middle_left"},
            ],
            "structure_type": "fixed_single_page",
            "records_per_page": 1,
            "cross_page_data": False,
            "pages_per_instance": 1,
        })

        router, mock_client = _make_router(generate_response=llm_response)

        spatial_text = (
            "Name:           John Doe\n"
            "SSN:            123-45-6789\n"
            "Date of Birth:  01/15/1985\n"
            "Address:        123 Main Street, Anytown, NY 10001\n"
            "Phone:          (555) 123-4567\n"
            "Email:          john.doe@example.com\n"
        )
        assert len(spatial_text.strip()) >= 100  # Sanity check

        with patch(
            "app.readers.liteparse_adapter.get_spatial_text",
            return_value=spatial_text,
        ):
            result = router._route_via_spatial_text("/test/doc.pdf", 0, 100)

        assert result is not None
        assert len(result.pii_fields) == 2
        assert result.structure_type == "fixed_single_page"
        assert result.model_used == "qwen2.5:7b:spatial_text"
        assert result.recommended_path == "coordinate"

        # Verify text LLM was called (not vision)
        mock_client.generate.assert_called_once()
        call_kwargs = mock_client.generate.call_args
        assert call_kwargs[1]["use_case"] == "spatial_text_routing"

    def test_spatial_text_caps_at_4000_chars(self):
        """Spatial text is truncated to 4000 chars in the prompt."""
        llm_response = json.dumps({
            "pii_fields": [{"type": "PERSON", "value": "Test", "label": "Name:", "position": "top"}],
            "structure_type": "fixed_single_page",
            "records_per_page": 1,
            "cross_page_data": False,
            "pages_per_instance": 1,
        })
        router, mock_client = _make_router(generate_response=llm_response)

        # Create spatial text longer than 4000 chars
        long_text = "Name: John Doe  SSN: 123-45-6789\n" * 200  # ~6600 chars
        assert len(long_text) > 4000

        with patch(
            "app.readers.liteparse_adapter.get_spatial_text",
            return_value=long_text,
        ):
            result = router._route_via_spatial_text("/test/doc.pdf", 0, 100)

        assert result is not None
        # Verify the prompt was capped
        prompt_sent = mock_client.generate.call_args[1].get("prompt") or mock_client.generate.call_args[0][0]
        # The spatial text portion should be at most 4000 chars
        spatial_marker = "Here is the document page content with spatial layout preserved:\n\n"
        idx = prompt_sent.find(spatial_marker)
        assert idx >= 0
        spatial_portion = prompt_sent[idx + len(spatial_marker):]
        assert len(spatial_portion) <= 4000

    def test_spatial_text_fallback_on_llm_error(self):
        """Returns None when the text LLM call raises an exception."""
        router, _ = _make_router(generate_side_effect=RuntimeError("LLM down"))

        spatial_text = (
            "Name:           John Doe\n"
            "SSN:            123-45-6789\n"
            "Date of Birth:  01/15/1985\n"
            "Address:        123 Main Street, Anytown, NY 10001\n"
            "Phone:          (555) 123-4567\n"
        )

        with patch(
            "app.readers.liteparse_adapter.get_spatial_text",
            return_value=spatial_text,
        ):
            result = router._route_via_spatial_text("/test/doc.pdf", 0, 100)
            assert result is None

    def test_spatial_text_fallback_on_empty_pii_fields(self):
        """Returns None when text LLM returns no PII fields."""
        llm_response = json.dumps({
            "pii_fields": [],
            "structure_type": "variable",
            "records_per_page": 1,
        })
        router, _ = _make_router(generate_response=llm_response)

        spatial_text = (
            "This is a document with no PII content whatsoever. "
            "It contains legal boilerplate and general terms of service. "
            "No names, addresses, or identification numbers appear anywhere.\n"
        )

        with patch(
            "app.readers.liteparse_adapter.get_spatial_text",
            return_value=spatial_text,
        ):
            result = router._route_via_spatial_text("/test/doc.pdf", 0, 100)
            assert result is None


class TestAnalyzeDocumentSpatialPath:
    """Tests for the spatial text fast-path in analyze_document()."""

    def test_scanned_pdf_skips_spatial_text(self):
        """Scanned PDFs bypass spatial text routing entirely."""
        router, mock_client = _make_router()

        vision_response = json.dumps({
            "pii_fields": [{"type": "PERSON", "value": "Test", "label": "N", "position": "top"}],
            "structure_type": "variable",
            "records_per_page": 1,
        })
        mock_client.generate_with_images.return_value = vision_response

        with patch("app.pipeline.vision_router.render_page_to_image", return_value="base64img"):
            result = router.analyze_document(
                "/test/doc.pdf", onset_page=0, total_pages=10, is_scanned=True,
            )

        # Should NOT have tried spatial text routing (text LLM)
        mock_client.generate.assert_not_called()
        # Should have used vision path
        mock_client.generate_with_images.assert_called()

    def test_text_pdf_tries_spatial_first(self):
        """Text PDFs attempt spatial text routing before vision."""
        llm_response = json.dumps({
            "pii_fields": [
                {"type": "PERSON", "value": "Jane Smith", "label": "Name:", "position": "top_left"},
            ],
            "structure_type": "fixed_single_page",
            "records_per_page": 1,
            "cross_page_data": False,
            "pages_per_instance": 1,
        })
        router, mock_client = _make_router(generate_response=llm_response)

        # Mock fitz to report enough text (word_count > 50)
        mock_page = MagicMock()
        mock_page.get_text.return_value = " ".join(["word"] * 100)  # 100 words
        mock_doc = MagicMock()
        mock_doc.__getitem__ = MagicMock(return_value=mock_page)
        mock_doc.page_count = 100

        spatial_text = (
            "Name:           Jane Smith\n"
            "Date of Birth:  01/01/1990\n"
            "SSN:            987-65-4321\n"
            "Address:        456 Oak Avenue, Springfield, IL 62701\n"
            "Phone:          (555) 987-6543\n"
        )

        with patch("fitz.open", return_value=mock_doc), \
             patch(
                 "app.readers.liteparse_adapter.get_spatial_text",
                 return_value=spatial_text,
             ):
            result = router.analyze_document(
                "/test/doc.pdf", onset_page=0, total_pages=100, is_scanned=False,
            )

        # Should have used spatial text path
        assert result.model_used == "qwen2.5:7b:spatial_text"
        assert len(result.pii_fields) == 1
        # Should NOT have used vision path
        mock_client.generate_with_images.assert_not_called()

    def test_low_word_count_skips_spatial(self):
        """PDFs with < 50 words skip spatial text and use vision."""
        router, mock_client = _make_router()

        mock_page = MagicMock()
        mock_page.get_text.return_value = "few words only"  # < 50 words
        mock_doc = MagicMock()
        mock_doc.__getitem__ = MagicMock(return_value=mock_page)
        mock_doc.page_count = 100

        vision_response = json.dumps({
            "pii_fields": [{"type": "PERSON", "value": "X", "label": "N", "position": "top"}],
            "structure_type": "variable",
            "records_per_page": 1,
        })
        mock_client.generate_with_images.return_value = vision_response

        with patch("fitz.open", return_value=mock_doc), \
             patch("app.pipeline.vision_router.render_page_to_image", return_value="base64img"):
            result = router.analyze_document(
                "/test/doc.pdf", onset_page=0, total_pages=100, is_scanned=False,
            )

        # Should NOT have tried text LLM (generate)
        mock_client.generate.assert_not_called()
        # Should have used vision path
        mock_client.generate_with_images.assert_called()
