"""Tests for VisionRouter — vision-based document routing (Step 22a)."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from app.pipeline.vision_router import (
    VisionRouter,
    VisionRoutingResult,
    _ROUTING_PROMPT,
    _parse_json,
    _safe_int,
)


# ------------------------------------------------------------------
# VisionRoutingResult dataclass
# ------------------------------------------------------------------


class TestVisionRoutingResult:
    """Test VisionRoutingResult dataclass creation."""

    def test_create_with_all_fields(self):
        result = VisionRoutingResult(
            structure_type="fixed_single_page",
            structure_confidence=0.9,
            pii_fields=[{"type": "PERSON", "value": "John Doe", "label": "Name:"}],
            records_per_page=1,
            cross_page_data=False,
            pages_per_instance=1,
            recommended_path="coordinate",
            raw_response='{"structure_type": "fixed_single_page"}',
        )
        assert result.structure_type == "fixed_single_page"
        assert result.structure_confidence == 0.9
        assert len(result.pii_fields) == 1
        assert result.records_per_page == 1
        assert result.cross_page_data is False
        assert result.pages_per_instance == 1
        assert result.recommended_path == "coordinate"
        assert result.raw_response != ""

    def test_defaults(self):
        result = VisionRoutingResult(
            structure_type="variable",
            structure_confidence=0.5,
        )
        assert result.pii_fields == []
        assert result.records_per_page == 1
        assert result.cross_page_data is False
        assert result.pages_per_instance == 1
        assert result.recommended_path == "presidio"
        assert result.raw_response == ""


# ------------------------------------------------------------------
# _determine_path
# ------------------------------------------------------------------


class TestDeterminePath:
    """Test routing logic in _determine_path."""

    def _make_router(self):
        client = MagicMock(spec=["generate_with_images"])
        return VisionRouter(client)

    def _make_result(self, structure_type="variable", pii_fields=None):
        return VisionRoutingResult(
            structure_type=structure_type,
            structure_confidence=0.8,
            pii_fields=pii_fields or [],
        )

    def test_single_page_returns_vision_direct(self):
        router = self._make_router()
        result = self._make_result("fixed_single_page", [{"type": "PERSON"}])
        assert router._determine_path(result, total_pages=1, is_scanned=False) == "vision_direct"

    def test_small_doc_3_pages_returns_vision_direct(self):
        router = self._make_router()
        result = self._make_result("fixed_single_page", [{"type": "PERSON"}])
        assert router._determine_path(result, total_pages=3, is_scanned=False) == "vision_direct"

    def test_small_doc_5_pages_returns_vision_direct(self):
        router = self._make_router()
        result = self._make_result("table", [{"type": "PERSON"}])
        assert router._determine_path(result, total_pages=5, is_scanned=False) == "vision_direct"

    def test_large_fixed_single_page_returns_coordinate(self):
        router = self._make_router()
        result = self._make_result(
            "fixed_single_page",
            [{"type": "PERSON", "value": "ADELINE CHANDLER"}],
        )
        assert router._determine_path(result, total_pages=1354, is_scanned=False) == "coordinate"

    def test_large_multi_page_template_returns_llm_template(self):
        router = self._make_router()
        result = self._make_result("multi_page_template")
        assert router._determine_path(result, total_pages=453, is_scanned=False) == "llm_template"

    def test_large_table_returns_llm_table(self):
        router = self._make_router()
        result = self._make_result("table")
        assert router._determine_path(result, total_pages=50, is_scanned=False) == "llm_table"

    def test_large_variable_returns_presidio(self):
        router = self._make_router()
        result = self._make_result("variable")
        assert router._determine_path(result, total_pages=100, is_scanned=False) == "presidio"

    def test_scanned_returns_vision_direct(self):
        router = self._make_router()
        result = self._make_result("fixed_single_page", [{"type": "PERSON"}])
        assert router._determine_path(result, total_pages=500, is_scanned=True) == "vision_direct"

    def test_fixed_no_pii_fields_returns_presidio(self):
        """fixed_single_page but no PII fields detected → not enough info for coordinate."""
        router = self._make_router()
        result = self._make_result("fixed_single_page", pii_fields=[])
        assert router._determine_path(result, total_pages=100, is_scanned=False) == "presidio"


# ------------------------------------------------------------------
# _build_routing_prompt
# ------------------------------------------------------------------


class TestBuildRoutingPrompt:
    """Test prompt construction."""

    def _make_router(self):
        client = MagicMock(spec=["generate_with_images"])
        return VisionRouter(client)

    def test_prompt_contains_required_json_fields(self):
        router = self._make_router()
        prompt = router._build_routing_prompt(total_pages=100)
        for field in [
            "pii_fields",
            "structure_type",
            "records_per_page",
            "cross_page_data",
            "pages_per_instance",
        ]:
            assert field in prompt

    def test_prompt_contains_structure_types(self):
        router = self._make_router()
        prompt = router._build_routing_prompt(total_pages=100)
        for stype in [
            "fixed_single_page",
            "multi_page_template",
            "table",
            "variable",
        ]:
            assert stype in prompt

    def test_prompt_contains_pii_types(self):
        router = self._make_router()
        prompt = router._build_routing_prompt(total_pages=100)
        for pii in ["PERSON", "LOCATION", "US_SSN", "DATE_OF_BIRTH"]:
            assert pii in prompt

    def test_prompt_includes_page_count_for_multipage(self):
        router = self._make_router()
        prompt = router._build_routing_prompt(total_pages=250)
        assert "250 total pages" in prompt

    def test_prompt_no_page_suffix_for_single_page(self):
        router = self._make_router()
        prompt = router._build_routing_prompt(total_pages=1)
        assert "total pages" not in prompt


# ------------------------------------------------------------------
# _parse_routing_response
# ------------------------------------------------------------------


class TestParseRoutingResponse:
    """Test response parsing."""

    def _make_router(self):
        client = MagicMock(spec=["generate_with_images"])
        return VisionRouter(client)

    def test_valid_json_fixed_single_page(self):
        router = self._make_router()
        response = json.dumps({
            "pii_fields": [
                {"type": "PERSON", "value": "ADELINE CHANDLER", "label": "Client:", "position": "top_left"},
                {"type": "LOCATION", "value": "123 Oak St, London", "label": "Address:", "position": "middle_left"},
            ],
            "structure_type": "fixed_single_page",
            "records_per_page": 1,
            "cross_page_data": False,
            "pages_per_instance": 1,
        })
        result = router._parse_routing_response(response, total_pages=100)
        assert result.structure_type == "fixed_single_page"
        assert len(result.pii_fields) == 2
        assert result.records_per_page == 1
        assert result.cross_page_data is False
        assert result.pages_per_instance == 1
        assert result.structure_confidence == 0.8  # has pii_fields

    def test_valid_json_multi_page_template(self):
        router = self._make_router()
        response = json.dumps({
            "pii_fields": [{"type": "PERSON", "value": "John Doe"}],
            "structure_type": "multi_page_template",
            "records_per_page": 1,
            "cross_page_data": True,
            "pages_per_instance": 3,
        })
        result = router._parse_routing_response(response, total_pages=100)
        assert result.structure_type == "multi_page_template"
        assert result.cross_page_data is True
        assert result.pages_per_instance == 3

    def test_malformed_json_returns_variable(self):
        router = self._make_router()
        result = router._parse_routing_response("not json at all!!!", total_pages=10)
        assert result.structure_type == "variable"
        assert result.structure_confidence == 0.0
        assert result.raw_response == "not json at all!!!"

    def test_empty_response_returns_variable(self):
        router = self._make_router()
        result = router._parse_routing_response("", total_pages=10)
        assert result.structure_type == "variable"
        assert result.structure_confidence == 0.0

    def test_empty_pii_fields(self):
        router = self._make_router()
        response = json.dumps({
            "pii_fields": [],
            "structure_type": "fixed_single_page",
            "records_per_page": 1,
            "cross_page_data": False,
            "pages_per_instance": 1,
        })
        result = router._parse_routing_response(response, total_pages=100)
        assert result.pii_fields == []
        assert result.structure_confidence == 0.3  # no pii_fields

    def test_invalid_structure_type_defaults_to_variable(self):
        router = self._make_router()
        response = json.dumps({
            "pii_fields": [],
            "structure_type": "something_unknown",
            "records_per_page": 1,
        })
        result = router._parse_routing_response(response, total_pages=10)
        assert result.structure_type == "variable"

    def test_json_in_code_fence(self):
        router = self._make_router()
        response = '```json\n{"structure_type": "table", "pii_fields": [{"type": "PERSON"}], "records_per_page": 5}\n```'
        result = router._parse_routing_response(response, total_pages=10)
        assert result.structure_type == "table"
        assert result.records_per_page == 5

    def test_json_with_leading_text(self):
        router = self._make_router()
        response = 'Here is the analysis:\n{"structure_type": "fixed_single_page", "pii_fields": [{"type": "PERSON"}]}'
        result = router._parse_routing_response(response, total_pages=10)
        assert result.structure_type == "fixed_single_page"

    def test_pii_fields_not_list_ignored(self):
        router = self._make_router()
        response = json.dumps({
            "pii_fields": "not a list",
            "structure_type": "fixed_single_page",
        })
        result = router._parse_routing_response(response, total_pages=10)
        assert result.pii_fields == []

    def test_pii_fields_non_dict_entries_filtered(self):
        router = self._make_router()
        response = json.dumps({
            "pii_fields": [{"type": "PERSON"}, "not_a_dict", 42, {"type": "LOCATION"}],
            "structure_type": "table",
        })
        result = router._parse_routing_response(response, total_pages=10)
        assert len(result.pii_fields) == 2


# ------------------------------------------------------------------
# analyze_document (mock integration)
# ------------------------------------------------------------------


class TestAnalyzeDocument:
    """Test full analyze_document flow with mocked dependencies."""

    def test_mock_vision_fixed_single_page(self):
        client = MagicMock()
        client.generate_with_images.return_value = json.dumps({
            "pii_fields": [
                {"type": "PERSON", "value": "ADELINE CHANDLER", "label": "Client:"},
                {"type": "US_SSN", "value": "123-45-6789", "label": "SSN:"},
            ],
            "structure_type": "fixed_single_page",
            "records_per_page": 1,
            "cross_page_data": False,
            "pages_per_instance": 1,
        })

        router = VisionRouter(client, vision_model="llama3.2-vision:latest")
        with patch("app.pipeline.vision_router.render_page_to_image", return_value="base64img"):
            result = router.analyze_document(
                doc_path="/tmp/test.pdf",
                onset_page=0,
                total_pages=1354,
                is_scanned=False,
            )

        assert result.structure_type == "fixed_single_page"
        assert result.recommended_path == "coordinate"
        assert len(result.pii_fields) == 2
        assert result.pii_fields[0]["value"] == "ADELINE CHANDLER"

    def test_mock_vision_multi_page_template(self):
        client = MagicMock()
        client.generate_with_images.return_value = json.dumps({
            "pii_fields": [{"type": "PERSON", "value": "Jane Smith", "label": "Name:"}],
            "structure_type": "multi_page_template",
            "records_per_page": 1,
            "cross_page_data": True,
            "pages_per_instance": 3,
        })

        router = VisionRouter(client, vision_model="llama3.2-vision:latest")
        with patch("app.pipeline.vision_router.render_page_to_image", return_value="base64img"):
            result = router.analyze_document(
                doc_path="/tmp/test.pdf",
                onset_page=2,
                total_pages=453,
                is_scanned=False,
            )

        assert result.structure_type == "multi_page_template"
        assert result.recommended_path == "llm_template"
        assert result.pages_per_instance == 3

    def test_render_failure_returns_safe_default(self):
        client = MagicMock()
        router = VisionRouter(client)
        with patch(
            "app.pipeline.vision_router.render_page_to_image",
            side_effect=RuntimeError("PDF corrupted"),
        ):
            result = router.analyze_document(
                doc_path="/tmp/bad.pdf",
                onset_page=0,
                total_pages=100,
            )

        assert result.structure_type == "variable"
        assert result.structure_confidence == 0.0
        assert result.recommended_path == "presidio"

    def test_vision_model_failure_returns_safe_default(self):
        client = MagicMock()
        client.generate_with_images.side_effect = ConnectionError("Ollama down")
        router = VisionRouter(client)
        with patch("app.pipeline.vision_router.render_page_to_image", return_value="base64img"):
            result = router.analyze_document(
                doc_path="/tmp/test.pdf",
                onset_page=0,
                total_pages=100,
            )

        assert result.structure_type == "variable"
        assert result.recommended_path == "presidio"

    def test_uses_vision_model_override(self):
        client = MagicMock()
        client.generate_with_images.return_value = json.dumps({
            "pii_fields": [],
            "structure_type": "variable",
        })
        router = VisionRouter(client, vision_model="custom-model:7b")
        with patch("app.pipeline.vision_router.render_page_to_image", return_value="base64img"):
            router.analyze_document("/tmp/test.pdf", total_pages=10)

        call_kwargs = client.generate_with_images.call_args
        assert call_kwargs.kwargs.get("model_override") == "custom-model:7b"

    def test_uses_correct_dpi(self):
        """Vision routing uses 200 DPI for better text recognition."""
        client = MagicMock()
        client.generate_with_images.return_value = json.dumps({
            "pii_fields": [],
            "structure_type": "variable",
        })
        router = VisionRouter(client, vision_model="qwen2.5vl:32b")
        with patch("app.pipeline.vision_router.render_page_to_image", return_value="base64img") as mock_render, \
             patch("fitz.open") as mock_fitz:
            # Mock fitz to avoid spatial text path trying to open the file
            mock_page = MagicMock()
            mock_page.get_text.return_value = ""  # No text → skip spatial, go to vision
            mock_doc = MagicMock()
            mock_doc.__getitem__ = MagicMock(return_value=mock_page)
            mock_doc.page_count = 10
            mock_fitz.return_value = mock_doc
            router.analyze_document("/tmp/test.pdf", total_pages=10)

        mock_render.assert_called_once_with("/tmp/test.pdf", 0, dpi=200)


# ------------------------------------------------------------------
# Helper functions
# ------------------------------------------------------------------


class TestParseJson:
    """Test the _parse_json helper."""

    def test_valid_json(self):
        assert _parse_json('{"key": "value"}') == {"key": "value"}

    def test_code_fence(self):
        assert _parse_json('```json\n{"key": "value"}\n```') == {"key": "value"}

    def test_leading_text(self):
        assert _parse_json('Here: {"key": "value"}') == {"key": "value"}

    def test_invalid(self):
        assert _parse_json("no json here") is None

    def test_empty(self):
        assert _parse_json("") is None

    def test_array_returns_list(self):
        """_parse_json returns lists (for batch responses) and dicts."""
        assert _parse_json('[1, 2, 3]') == [1, 2, 3]
        assert _parse_json('[{"a": 1}]') == [{"a": 1}]


class TestSafeInt:
    def test_normal(self):
        assert _safe_int(5) == 5

    def test_none(self):
        assert _safe_int(None) == 1

    def test_string_number(self):
        assert _safe_int("3") == 3

    def test_invalid_string(self):
        assert _safe_int("abc", default=2) == 2

    def test_zero_becomes_one(self):
        assert _safe_int(0) == 1

    def test_negative_becomes_one(self):
        assert _safe_int(-5) == 1
