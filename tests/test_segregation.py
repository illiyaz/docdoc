"""Tests for LLM-first file segregation engine (Step 30e-1).

Tests classification logic, response parsing, rendering helpers,
and text extraction — all without requiring a live LLM.
"""
from __future__ import annotations

import json
import os
import pytest
from unittest.mock import MagicMock, patch

from app.pipeline.segregation import (
    SegregationEngine,
    SegregationField,
    SegregationResult,
    _get_file_type,
    _get_page_count,
    apply_corrections,
    _inject_corrections_into_prompt,
    load_segregation_corrections,
)


# ---------------------------------------------------------------------------
# SegregationResult tests
# ---------------------------------------------------------------------------

class TestSegregationResult:
    """Test SegregationResult dataclass and properties."""

    def test_empty_result(self):
        r = SegregationResult(
            file_path="/tmp/test.pdf",
            file_name="test.pdf",
            file_type="pdf",
            total_pages=1,
        )
        assert r.pii_detected is False
        assert r.confidence == 0.0
        assert r.field_inventory == []
        assert r.role_map == {}
        assert r.primary_fields == []
        assert r.secondary_fields == []

    def test_field_inventory(self):
        r = SegregationResult(
            file_path="/tmp/test.pdf",
            file_name="test.pdf",
            file_type="pdf",
            total_pages=1,
            fields=[
                SegregationField(name="Name", type="PERSON", role="primary_subject"),
                SegregationField(name="SSN", type="US_SSN", role="primary_subject"),
                SegregationField(name="Parent Name", type="PERSON", role="secondary_contact"),
            ],
        )
        # Sorted unique types
        assert r.field_inventory == ["PERSON", "US_SSN"]

    def test_role_map(self):
        r = SegregationResult(
            file_path="/tmp/test.pdf",
            file_name="test.pdf",
            file_type="pdf",
            total_pages=1,
            fields=[
                SegregationField(name="Student Name", type="PERSON", role="primary_subject"),
                SegregationField(name="Parent Name", type="PERSON", role="secondary_contact"),
            ],
        )
        assert r.role_map == {
            "Student Name": "primary_subject",
            "Parent Name": "secondary_contact",
        }

    def test_primary_secondary_fields(self):
        r = SegregationResult(
            file_path="/tmp/test.pdf",
            file_name="test.pdf",
            file_type="pdf",
            total_pages=3,
            fields=[
                SegregationField(name="Patient Name", type="PERSON", role="primary_subject"),
                SegregationField(name="SSN", type="US_SSN", role="primary_subject"),
                SegregationField(name="Emergency Contact", type="PERSON", role="secondary_contact"),
                SegregationField(name="Physician", type="PERSON", role="secondary_contact"),
            ],
        )
        assert len(r.primary_fields) == 2
        assert len(r.secondary_fields) == 2

    def test_to_dict(self):
        r = SegregationResult(
            file_path="/tmp/test.pdf",
            file_name="test.pdf",
            file_type="pdf",
            total_pages=1,
            pii_detected=True,
            confidence=0.95,
            document_type="medical_form",
        )
        d = r.to_dict()
        assert d["pii_detected"] is True
        assert d["confidence"] == 0.95
        assert "field_inventory" in d
        assert "role_map" in d


# ---------------------------------------------------------------------------
# Response parsing tests
# ---------------------------------------------------------------------------

class TestResponseParsing:
    """Test LLM response parsing logic."""

    def _make_engine(self):
        return SegregationEngine(db_session=None)

    def _make_result(self):
        return SegregationResult(
            file_path="/tmp/test.pdf",
            file_name="test.pdf",
            file_type="pdf",
            total_pages=1,
        )

    def test_parse_pii_document(self):
        engine = self._make_engine()
        result = self._make_result()

        response = json.dumps({
            "pii": True,
            "confidence": 0.92,
            "document_type": "medical_form",
            "document_subtype": "intake",
            "issuing_entity": "C&R Vision",
            "fields": [
                {"name": "Patient Name", "type": "PERSON", "role": "primary_subject", "value_visible": True},
                {"name": "Address", "type": "LOCATION", "role": "primary_subject", "value_visible": True},
                {"name": "Account Number", "type": "OTHER_ID", "role": "primary_subject", "value_visible": True},
            ],
            "primary_subject_type": "patient",
            "summary": "Medical billing statement with patient name, address, and account",
        })

        engine._parse_response(response, result)

        assert result.pii_detected is True
        assert result.confidence == 0.92
        assert result.document_type == "medical_form"
        assert result.issuing_entity == "C&R Vision"
        assert len(result.fields) == 3
        assert result.primary_subject_type == "patient"
        assert result.fields[0].name == "Patient Name"
        assert result.fields[0].role == "primary_subject"

    def test_parse_non_pii_document(self):
        engine = self._make_engine()
        result = self._make_result()

        response = json.dumps({
            "pii": False,
            "confidence": 0.88,
            "document_type": "shipping_document",
            "document_subtype": "bill_of_lading",
            "issuing_entity": "DAIKIN COMFORT TECHNOLOGIES",
            "fields": [],
            "primary_subject_type": None,
            "summary": "Commercial bill of lading with product serial numbers",
        })

        engine._parse_response(response, result)

        assert result.pii_detected is False
        assert result.document_type == "shipping_document"
        assert len(result.fields) == 0

    def test_parse_with_role_attribution(self):
        engine = self._make_engine()
        result = self._make_result()

        response = json.dumps({
            "pii": True,
            "confidence": 0.95,
            "document_type": "school_record",
            "fields": [
                {"name": "Student Name", "type": "PERSON", "role": "primary_subject"},
                {"name": "Student ID", "type": "STUDENT_ID", "role": "primary_subject"},
                {"name": "Parent Name", "type": "PERSON", "role": "secondary_contact"},
                {"name": "Parent Phone", "type": "PHONE_NUMBER", "role": "secondary_contact"},
            ],
            "primary_subject_type": "student",
            "summary": "School enrollment form with student and parent info",
        })

        engine._parse_response(response, result)

        assert len(result.primary_fields) == 2
        assert len(result.secondary_fields) == 2
        assert result.role_map["Student Name"] == "primary_subject"
        assert result.role_map["Parent Name"] == "secondary_contact"

    def test_parse_markdown_fenced_json(self):
        """LLM sometimes wraps JSON in markdown code fences."""
        engine = self._make_engine()
        result = self._make_result()

        response = '```json\n{"pii": true, "confidence": 0.9, "document_type": "tax_form", "fields": []}\n```'

        engine._parse_response(response, result)

        assert result.pii_detected is True
        assert result.document_type == "tax_form"

    def test_parse_invalid_json(self):
        engine = self._make_engine()
        result = self._make_result()

        engine._parse_response("not valid json", result)

        assert result.pii_detected is False
        assert result.error is not None
        assert "Invalid JSON" in result.error

    def test_parse_missing_fields_graceful(self):
        """Partial response should still populate what's available."""
        engine = self._make_engine()
        result = self._make_result()

        response = json.dumps({"pii": True, "confidence": 0.5})

        engine._parse_response(response, result)

        assert result.pii_detected is True
        assert result.confidence == 0.5
        assert result.document_type == "unknown"
        assert len(result.fields) == 0

    def test_parse_fields_bad_format(self):
        """fields containing non-dict entries should be skipped."""
        engine = self._make_engine()
        result = self._make_result()

        response = json.dumps({
            "pii": True,
            "confidence": 0.8,
            "document_type": "form",
            "fields": [
                {"name": "Name", "type": "PERSON", "role": "primary_subject"},
                "not a dict",
                42,
                {"name": "SSN", "type": "US_SSN"},  # missing role — should default
            ],
        })

        engine._parse_response(response, result)

        assert len(result.fields) == 2
        assert result.fields[1].role == "primary_subject"  # default


# ---------------------------------------------------------------------------
# Utility function tests
# ---------------------------------------------------------------------------

class TestUtilityFunctions:

    def test_get_file_type(self):
        assert _get_file_type("/path/to/file.pdf") == "pdf"
        assert _get_file_type("/path/to/file.PDF") == "pdf"
        assert _get_file_type("/path/to/file.xlsx") == "xlsx"
        assert _get_file_type("image.HEIC") == "heic"
        assert _get_file_type("no_extension") == ""

    def test_get_page_count_image(self):
        assert _get_page_count("/tmp/photo.jpg", "jpg") == 1
        assert _get_page_count("/tmp/photo.png", "png") == 1
        assert _get_page_count("/tmp/photo.heic", "heic") == 1

    def test_get_page_count_unknown(self):
        assert _get_page_count("/tmp/data.xyz", "xyz") == 1


# ---------------------------------------------------------------------------
# Engine classification flow tests (mocked LLM)
# ---------------------------------------------------------------------------

class TestSegregationEngine:
    """Test engine classification logic with mocked LLM client."""

    def _make_engine(self):
        engine = SegregationEngine(db_session=None)
        # Mock the client
        mock_client = MagicMock()
        engine._client = mock_client
        return engine, mock_client

    def test_classify_vision_pii(self, tmp_path):
        """Vision classification finds PII."""
        engine, mock_client = self._make_engine()

        mock_client.generate_with_images.return_value = json.dumps({
            "pii": True,
            "confidence": 0.95,
            "document_type": "billing_statement",
            "fields": [
                {"name": "Karen Craft", "type": "PERSON", "role": "primary_subject"},
                {"name": "Address", "type": "LOCATION", "role": "primary_subject"},
            ],
            "primary_subject_type": "patient",
            "summary": "Medical billing statement",
        })

        # Create a dummy PDF-like file (we'll mock the renderer)
        test_file = tmp_path / "test.pdf"
        test_file.write_bytes(b"fake pdf content")

        with patch("app.pipeline.segregation.SegregationEngine._render_page") as mock_render:
            mock_render.return_value = "base64_fake_image"
            result = engine.classify(str(test_file))

        assert result is not None
        assert result.pii_detected is True
        assert result.confidence == 0.95
        assert result.document_type == "billing_statement"
        assert len(result.fields) == 2
        assert result.classification_method == "vision"
        assert result.processing_time_ms > 0

    def test_classify_vision_no_pii(self, tmp_path):
        """Vision classification — commercial doc, no PII."""
        engine, mock_client = self._make_engine()

        mock_client.generate_with_images.return_value = json.dumps({
            "pii": False,
            "confidence": 0.88,
            "document_type": "shipping_document",
            "fields": [],
            "summary": "Bill of lading",
        })

        test_file = tmp_path / "shipping.pdf"
        test_file.write_bytes(b"fake pdf")

        with patch("app.pipeline.segregation.SegregationEngine._render_page") as mock_render:
            mock_render.return_value = "base64_fake"
            result = engine.classify(str(test_file))

        assert result is not None
        assert result.pii_detected is False
        assert result.document_type == "shipping_document"

    def test_classify_text_fallback(self, tmp_path):
        """Non-renderable file type falls back to text classification."""
        engine, mock_client = self._make_engine()

        mock_client.generate.return_value = json.dumps({
            "pii": True,
            "confidence": 0.85,
            "document_type": "spreadsheet_export",
            "fields": [
                {"name": "Employee Name", "type": "PERSON", "role": "primary_subject"},
                {"name": "SSN", "type": "US_SSN", "role": "primary_subject"},
            ],
            "primary_subject_type": "employee",
            "summary": "Employee data export",
        })

        test_file = tmp_path / "data.xlsx"
        test_file.write_bytes(b"fake xlsx")

        with patch("app.pipeline.segregation.SegregationEngine._extract_text") as mock_extract:
            mock_extract.return_value = "Employee Name\tSSN\tAddress\nJohn Smith\t123-45-6789\t123 Main St"
            result = engine.classify(str(test_file))

        assert result is not None
        assert result.pii_detected is True
        assert result.classification_method == "text"

    def test_classify_unsupported_type(self, tmp_path):
        """Unsupported file type returns result with error."""
        engine, _ = self._make_engine()

        test_file = tmp_path / "data.xyz"
        test_file.write_bytes(b"unknown")

        result = engine.classify(str(test_file))

        assert result is not None
        assert result.classification_method == "fallback"
        assert result.error is not None

    def test_classify_llm_failure(self, tmp_path):
        """LLM failure returns result with error, not exception."""
        engine, mock_client = self._make_engine()

        mock_client.generate_with_images.side_effect = Exception("Connection refused")

        test_file = tmp_path / "test.pdf"
        test_file.write_bytes(b"fake pdf")

        with patch("app.pipeline.segregation.SegregationEngine._render_page") as mock_render:
            mock_render.return_value = "base64_fake"
            # Should also try text fallback which will fail
            with patch("app.pipeline.segregation.SegregationEngine._extract_text") as mock_text:
                mock_text.return_value = None
                result = engine.classify(str(test_file))

        assert result is not None
        assert result.error is not None
        # Should not raise

    def test_classify_batch(self, tmp_path):
        """Batch classification processes all files."""
        engine, mock_client = self._make_engine()

        mock_client.generate_with_images.return_value = json.dumps({
            "pii": True,
            "confidence": 0.9,
            "document_type": "form",
            "fields": [],
        })

        files = []
        for i in range(3):
            f = tmp_path / f"file{i}.pdf"
            f.write_bytes(b"fake")
            files.append(str(f))

        with patch("app.pipeline.segregation.SegregationEngine._render_page") as mock_render:
            mock_render.return_value = "base64_fake"
            results = engine.classify_batch(files)

        assert len(results) == 3
        assert all(r.pii_detected for r in results)

    def test_page2_retry_when_page1_no_pii(self, tmp_path):
        """If page 1 shows no PII, tries page 2 (e.g., cover page scenario)."""
        engine, mock_client = self._make_engine()

        # Page 1: no PII (cover page)
        # Page 2: PII found
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return json.dumps({
                    "pii": False,
                    "confidence": 0.6,
                    "document_type": "cover_page",
                    "fields": [],
                })
            else:
                return json.dumps({
                    "pii": True,
                    "confidence": 0.9,
                    "document_type": "medical_form",
                    "fields": [
                        {"name": "Patient Name", "type": "PERSON", "role": "primary_subject"},
                    ],
                    "primary_subject_type": "patient",
                })

        mock_client.generate_with_images.side_effect = side_effect

        test_file = tmp_path / "test.pdf"
        test_file.write_bytes(b"fake pdf")

        with patch("app.pipeline.segregation.SegregationEngine._render_page") as mock_render:
            mock_render.return_value = "base64_fake"
            with patch("app.pipeline.segregation._get_page_count") as mock_pages:
                mock_pages.return_value = 5
                result = engine.classify(str(test_file))

        assert result is not None
        assert result.pii_detected is True
        assert result.document_type == "medical_form"
        assert call_count[0] == 2  # both pages tried


# ---------------------------------------------------------------------------
# Safety: no raw PII in segregation results
# ---------------------------------------------------------------------------

class TestSegregationSafety:
    """Verify segregation does not store raw PII values."""

    def test_fields_have_no_raw_values(self):
        """SegregationField stores field names/types, not PII values."""
        field = SegregationField(
            name="Patient Name",
            type="PERSON",
            role="primary_subject",
            value_visible=True,
        )
        d = field.to_dict()
        # The 'name' field is the label ("Patient Name"), not the value ("Karen Craft")
        assert "name" in d
        assert d["name"] == "Patient Name"
        # No raw_value field exists
        assert "value" not in d
        assert "raw_value" not in d


# ---------------------------------------------------------------------------
# Correction memory tests (Step 30e-5)
# ---------------------------------------------------------------------------

class TestApplyCorrections:
    """Test apply_corrections() with stored auditor corrections."""

    def test_reclassify_changes_type(self):
        """Reclassify correction should change document_type."""
        result = SegregationResult(
            file_path="/tmp/test.pdf",
            file_name="test.pdf",
            file_type="pdf",
            total_pages=1,
            pii_detected=True,
            document_type="invoice",
        )
        corrections = [
            {"document_type": "invoice", "action": "reclassify",
             "new_document_type": "billing_statement", "new_is_pii": True},
        ]
        updated = apply_corrections(result, corrections)
        assert updated.document_type == "billing_statement"
        assert updated.pii_detected is True

    def test_reclassify_changes_pii_status(self):
        """Reclassify can flip PII status."""
        result = SegregationResult(
            file_path="/tmp/test.pdf",
            file_name="test.pdf",
            file_type="pdf",
            total_pages=1,
            pii_detected=False,
            document_type="spreadsheet_export",
        )
        corrections = [
            {"document_type": "spreadsheet_export", "action": "reclassify",
             "new_document_type": "payroll_data", "new_is_pii": True},
        ]
        updated = apply_corrections(result, corrections)
        assert updated.document_type == "payroll_data"
        assert updated.pii_detected is True

    def test_reject_marks_non_pii(self):
        """Reject correction should set pii_detected to False."""
        result = SegregationResult(
            file_path="/tmp/test.pdf",
            file_name="test.pdf",
            file_type="pdf",
            total_pages=1,
            pii_detected=True,
            document_type="shipping_document",
        )
        corrections = [
            {"document_type": "shipping_document", "action": "reject"},
        ]
        updated = apply_corrections(result, corrections)
        assert updated.pii_detected is False

    def test_no_matching_correction(self):
        """Result unchanged if no correction matches."""
        result = SegregationResult(
            file_path="/tmp/test.pdf",
            file_name="test.pdf",
            file_type="pdf",
            total_pages=1,
            pii_detected=True,
            document_type="medical_form",
        )
        corrections = [
            {"document_type": "invoice", "action": "reject"},
        ]
        updated = apply_corrections(result, corrections)
        assert updated.pii_detected is True
        assert updated.document_type == "medical_form"

    def test_empty_corrections(self):
        """Empty corrections list should return result unchanged."""
        result = SegregationResult(
            file_path="/tmp/test.pdf",
            file_name="test.pdf",
            file_type="pdf",
            total_pages=1,
            pii_detected=True,
        )
        updated = apply_corrections(result, [])
        assert updated is result

    def test_first_matching_correction_wins(self):
        """First matching correction should be applied, not later ones."""
        result = SegregationResult(
            file_path="/tmp/test.pdf",
            file_name="test.pdf",
            file_type="pdf",
            total_pages=1,
            pii_detected=True,
            document_type="invoice",
        )
        corrections = [
            {"document_type": "invoice", "action": "reclassify",
             "new_document_type": "billing_statement"},
            {"document_type": "invoice", "action": "reject"},
        ]
        updated = apply_corrections(result, corrections)
        assert updated.document_type == "billing_statement"
        assert updated.pii_detected is True  # not rejected


class TestInjectCorrections:
    """Test few-shot correction injection into prompts."""

    def test_inject_reject_correction(self):
        prompt = "Test prompt. Respond with ONLY this JSON"
        corrections = [
            {"document_type": "shipping_document", "action": "reject"},
        ]
        result = _inject_corrections_into_prompt(prompt, corrections)
        assert "shipping_document" in result
        assert "do NOT contain PII" in result
        assert "Respond with ONLY this JSON" in result

    def test_inject_reclassify_correction(self):
        prompt = "Test prompt. Respond with ONLY this JSON"
        corrections = [
            {"document_type": "invoice", "action": "reclassify",
             "new_document_type": "medical_billing", "new_is_pii": True},
        ]
        result = _inject_corrections_into_prompt(prompt, corrections)
        assert "invoice" in result
        assert "medical_billing" in result
        assert "(PII: yes)" in result

    def test_inject_deduplicates(self):
        prompt = "Test prompt. Respond with ONLY this JSON"
        corrections = [
            {"document_type": "invoice", "action": "reject"},
            {"document_type": "invoice", "action": "reject"},
            {"document_type": "invoice", "action": "reject"},
        ]
        result = _inject_corrections_into_prompt(prompt, corrections)
        # Should only appear once despite 3 identical corrections
        assert result.count("invoice") == 1

    def test_inject_max_examples(self):
        prompt = "Test prompt. Respond with ONLY this JSON"
        corrections = [
            {"document_type": f"type_{i}", "action": "reject"}
            for i in range(20)
        ]
        result = _inject_corrections_into_prompt(prompt, corrections, max_examples=3)
        # Should have at most 3 correction lines
        assert result.count("do NOT contain PII") <= 3

    def test_inject_empty_corrections(self):
        prompt = "Test prompt."
        result = _inject_corrections_into_prompt(prompt, [])
        assert result == prompt

    def test_inject_preserves_prompt_structure(self):
        prompt = "Before. Respond with ONLY this JSON (no markdown):\n{{\n  \"pii\": true\n}}"
        corrections = [
            {"document_type": "test", "action": "reject"},
        ]
        result = _inject_corrections_into_prompt(prompt, corrections)
        # Original JSON format should still be present
        assert "\"pii\": true" in result
        assert "Respond with ONLY this JSON" in result


class TestLoadCorrections:
    """Test loading corrections from JSONL file."""

    def test_load_from_file(self, tmp_path, monkeypatch):
        """Loads corrections from JSONL."""
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
        from app.core.settings import get_settings
        get_settings.cache_clear()

        corrections_dir = tmp_path / "corrections"
        corrections_dir.mkdir()
        filepath = corrections_dir / "proj123_segregation_corrections.jsonl"
        filepath.write_text(
            '{"document_type": "invoice", "action": "reject"}\n'
            '{"document_type": "form", "action": "reclassify", "new_document_type": "medical"}\n'
        )

        result = load_segregation_corrections("proj123")
        assert len(result) == 2
        assert result[0]["document_type"] == "invoice"
        assert result[1]["action"] == "reclassify"
        get_settings.cache_clear()

    def test_load_missing_file(self, tmp_path, monkeypatch):
        """Returns empty list if file doesn't exist."""
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
        from app.core.settings import get_settings
        get_settings.cache_clear()
        result = load_segregation_corrections("nonexistent_project")
        assert result == []
        get_settings.cache_clear()
