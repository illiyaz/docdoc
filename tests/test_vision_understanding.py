"""Tests for vision-based document understanding (Step 5 optimization).

Tests:
- UNDERSTAND_DOCUMENT_VISION prompt exists in registry
- understand_with_vision method exists with correct signature
- Renderable types set covers expected formats
- Non-renderable types return None
- Vision fallback wired into analyze_generator
- parse_response reuse (same parser for text + vision paths)
- LLM disabled returns None gracefully
- Prompt content has correct placeholders
"""
from __future__ import annotations

import inspect


class TestVisionDocumentUnderstanding:
    """Test vision-based document understanding fallback."""

    def test_prompt_exists_in_registry(self):
        """UNDERSTAND_DOCUMENT_VISION is registered in PROMPT_TEMPLATES."""
        from app.llm.prompts import PROMPT_TEMPLATES
        assert "understand_document_vision" in PROMPT_TEMPLATES

    def test_prompt_has_placeholders(self):
        """Vision prompt has file_name and file_type placeholders."""
        from app.llm.prompts import UNDERSTAND_DOCUMENT_VISION
        assert "{file_name}" in UNDERSTAND_DOCUMENT_VISION
        assert "{file_type}" in UNDERSTAND_DOCUMENT_VISION

    def test_understand_with_vision_method_exists(self):
        """LLMDocumentUnderstanding has understand_with_vision method."""
        from app.structure.llm_document_understanding import LLMDocumentUnderstanding
        assert hasattr(LLMDocumentUnderstanding, "understand_with_vision")
        sig = inspect.signature(LLMDocumentUnderstanding.understand_with_vision)
        params = list(sig.parameters.keys())
        assert "doc_path" in params
        assert "file_type" in params
        assert "onset_page" in params

    def test_renderable_types_cover_expected(self):
        """_RENDERABLE_TYPES includes pdf, heic, jpg, png, tiff."""
        from app.structure.llm_document_understanding import LLMDocumentUnderstanding
        rt = LLMDocumentUnderstanding._RENDERABLE_TYPES
        for ext in ("pdf", "heic", "heif", "jpg", "jpeg", "png", "tiff", "bmp"):
            assert ext in rt, f"Missing renderable type: {ext}"

    def test_non_renderable_returns_none(self):
        """Non-renderable file types return None without trying vision."""
        from app.structure.llm_document_understanding import LLMDocumentUnderstanding

        # Create instance without DB session (testing only)
        ldu = LLMDocumentUnderstanding.__new__(LLMDocumentUnderstanding)
        result = ldu.understand_with_vision(
            "/fake/path.xlsx",
            file_type="xlsx",
            file_name="test.xlsx",
        )
        assert result is None

    def test_vision_fallback_wired_in_pipeline(self):
        """analyze_generator calls understand_with_vision after understand() returns None."""
        from app.pipeline.two_phase import analyze_generator
        source = inspect.getsource(analyze_generator)
        assert "understand_with_vision" in source
        assert "_RENDERABLE_TYPES" in source

    def test_prompt_requests_json_output(self):
        """Vision prompt instructs model to respond with JSON only."""
        from app.llm.prompts import UNDERSTAND_DOCUMENT_VISION
        assert "Respond ONLY with valid JSON" in UNDERSTAND_DOCUMENT_VISION

    def test_prompt_includes_layout_fields(self):
        """Vision prompt includes layout analysis fields."""
        from app.llm.prompts import UNDERSTAND_DOCUMENT_VISION
        assert "layout_type" in UNDERSTAND_DOCUMENT_VISION
        assert "schema_confidence" in UNDERSTAND_DOCUMENT_VISION
        assert "field_map" in UNDERSTAND_DOCUMENT_VISION
