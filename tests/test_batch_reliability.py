"""Tests for batch reliability, configurable dedup, and dedup API.

30 tests covering:
- Retry: first attempt fails, second succeeds → record extracted
- Retry: batch fails 3x → splits to individual, retries each
- Extraction summary: logs X/Y instances extracted
- _parse_json: handles truncated JSON, "Extra data", markdown fences
- _unload_unused_models: best-effort model unloading
- Dedup with ssn anchor: same NI from 5 instances → 1 row
- Dedup with name_dob: same name+DOB → merged
- Dedup fallback: no anchor match → instance-aware
- Active anchors read from protocol config
- Page ranges concatenated on merge
- _build_anchor_key: various anchor combinations
- API returns dedup_anchors in analysis response
"""
from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import MagicMock, patch, call

import pytest

from app.rra.entity_resolver import PIIRecord
from app.structure.llm_template_extractor import (
    LLMTemplateExtractor,
    _build_anchor_key,
    _deduplicate_records,
    _parse_json,
    _try_close_truncated,
    _unload_unused_models,
    MAX_RETRIES,
    RETRY_BACKOFF_SECONDS,
)
from app.structure.document_schema import (
    DocumentSchema,
    DocumentTemplate,
    PageRole,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_record(
    name: str = "Alice Smith",
    gov_id: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    dob: str | None = None,
    address: dict | None = None,
    page_range: str = "1-3",
    doc_id: str = "doc1",
) -> PIIRecord:
    return PIIRecord(
        record_id=f"rec-{name.lower().replace(' ', '-')}-{page_range}",
        entity_type="PERSON",
        normalized_value=name,
        raw_name=name,
        raw_government_id=gov_id,
        raw_email=email,
        raw_phone=phone,
        raw_dob=dob,
        raw_address=address,
        page_range=page_range,
        source_document_id=doc_id,
    )


def _make_schema(pages_per_instance: int = 3, total: int = 2) -> DocumentSchema:
    return DocumentSchema(
        document_type="pension_statement",
        document_subtype=None,
        issuing_entity="Test Ltd",
        field_map=[],
        people=[],
        organizations=[],
        date_contexts=[],
        tables=[],
        suppression_hints=[],
        extraction_notes="",
        schema_confidence=0.9,
        detected_by="llm",
        template=DocumentTemplate(
            template_name="pension",
            pages_per_instance=pages_per_instance,
            total_instances_estimate=total,
            page_roles=[
                PageRole(page_offset=0, role="identity", pii_fields_expected=["PERSON", "NI_NUMBER"]),
            ],
        ),
    )


def _make_client(responses=None, fail_count=0):
    """Create a mock OllamaClient that returns responses or raises."""
    client = MagicMock()
    client.model = "qwen2.5:7b"
    client.base_url = "http://localhost:11434"

    call_counter = {"n": 0}
    response_list = responses or []

    def generate_side_effect(prompt, system=None, *, use_case="general", document_id=None):
        n = call_counter["n"]
        call_counter["n"] += 1
        if n < fail_count:
            raise TimeoutError(f"Simulated timeout on call {n}")
        if response_list:
            idx = min(n - fail_count, len(response_list) - 1)
            return response_list[idx]
        return '{"PERSON": "Test Person", "NI_NUMBER": "AB123456C"}'

    client.generate = MagicMock(side_effect=generate_side_effect)
    return client


# ---------------------------------------------------------------------------
# _parse_json tests
# ---------------------------------------------------------------------------

class TestParseJson:

    def test_clean_json_array(self):
        result = _parse_json('[{"PERSON": "Alice"}]')
        assert isinstance(result, list)
        assert result[0]["PERSON"] == "Alice"

    def test_markdown_fences(self):
        text = '```json\n[{"PERSON": "Bob"}]\n```'
        result = _parse_json(text)
        assert isinstance(result, list)
        assert result[0]["PERSON"] == "Bob"

    def test_extra_data_error(self):
        """Two JSON arrays concatenated — should parse the first one."""
        text = '[{"PERSON": "A"}]\n[{"PERSON": "B"}]'
        result = _parse_json(text)
        assert isinstance(result, list)
        assert result[0]["PERSON"] == "A"

    def test_truncated_json_closes_brackets(self):
        """Truncated array — should close the open bracket."""
        text = '[{"PERSON": "Alice", "NI_NUMBER": "AB123456C"}'
        result = _parse_json(text)
        assert isinstance(result, list)
        assert result[0]["PERSON"] == "Alice"

    def test_truncated_deep_nesting(self):
        """Array with one complete and one truncated object."""
        text = '[{"PERSON": "A", "NI_NUMBER": "X1"}, {"PERSON": "B"'
        result = _parse_json(text)
        # Should at least get the first object
        assert isinstance(result, list)
        assert len(result) >= 1

    def test_markdown_fences_with_extra_data(self):
        """Markdown fences + extra data after closing fence."""
        text = '```json\n[{"PERSON": "Test"}]\n```\nSome extra text'
        result = _parse_json(text)
        assert isinstance(result, list)

    def test_empty_returns_none(self):
        assert _parse_json("") is None

    def test_garbage_returns_none(self):
        assert _parse_json("not json at all") is None


# ---------------------------------------------------------------------------
# _try_close_truncated tests
# ---------------------------------------------------------------------------

class TestTryCloseTruncated:

    def test_closes_single_bracket(self):
        result = _try_close_truncated('[{"a": 1}')
        assert isinstance(result, list)

    def test_closes_nested(self):
        result = _try_close_truncated('[{"a": {"b": 1}')
        assert isinstance(result, list)

    def test_no_open_brackets(self):
        result = _try_close_truncated('{"a": 1}')
        assert result is None

    def test_truncated_mid_value(self):
        """Truncated in middle of string value — may fail gracefully."""
        result = _try_close_truncated('[{"PERSON": "Ali')
        # Either parses or returns None — should not crash
        assert result is None or isinstance(result, list)


# ---------------------------------------------------------------------------
# Retry tests
# ---------------------------------------------------------------------------

class TestRetryBehavior:

    @patch("app.structure.llm_template_extractor.time.sleep")
    @patch("app.structure.llm_template_extractor._unload_unused_models")
    def test_sequential_retry_succeeds_second_attempt(self, mock_unload, mock_sleep):
        """First attempt fails, second succeeds → record extracted."""
        client = _make_client(fail_count=1)
        extractor = LLMTemplateExtractor(client, batch_size=1)
        schema = _make_schema(pages_per_instance=3, total=1)
        page_texts = {0: "Page 0 text", 1: "Page 1 text", 2: "Page 2 text"}

        records = extractor.extract_all_instances(schema, page_texts, "doc1", 3)
        assert len(records) == 1
        assert records[0].raw_name == "Test Person"
        # Should have slept once for retry
        assert mock_sleep.called

    @patch("app.structure.llm_template_extractor.time.sleep")
    @patch("app.structure.llm_template_extractor._unload_unused_models")
    def test_batch_retry_splits_to_individual(self, mock_unload, mock_sleep):
        """Batch fails 3x → splits to individual, each succeeds."""
        client = MagicMock()
        client.model = "qwen2.5:7b"
        client.base_url = "http://localhost:11434"

        batch_call_count = {"n": 0}

        def gen(prompt, system=None, *, use_case="general", document_id=None):
            if use_case == "template_extraction_batch":
                batch_call_count["n"] += 1
                raise TimeoutError("batch always fails")
            # Individual calls succeed
            return '{"PERSON": "Person X", "NI_NUMBER": "AA111111A"}'

        client.generate = MagicMock(side_effect=gen)
        extractor = LLMTemplateExtractor(client, batch_size=3)
        schema = _make_schema(pages_per_instance=3, total=2)
        page_texts = {i: f"Page {i}" for i in range(6)}

        records = extractor.extract_all_instances(schema, page_texts, "doc1", 6)
        # All 3 batch attempts failed, then split to 2 individual calls
        assert batch_call_count["n"] == MAX_RETRIES
        assert len(records) >= 1  # individual calls succeed

    @patch("app.structure.llm_template_extractor.time.sleep")
    @patch("app.structure.llm_template_extractor._unload_unused_models")
    def test_extraction_summary_logged(self, mock_unload, mock_sleep):
        """Extraction logs X/Y instances extracted."""
        client = _make_client()
        extractor = LLMTemplateExtractor(client, batch_size=1)
        schema = _make_schema(pages_per_instance=3, total=2)
        page_texts = {i: f"Page {i}" for i in range(6)}

        with patch("app.structure.llm_template_extractor.logger") as mock_logger:
            extractor.extract_all_instances(schema, page_texts, "doc1", 6)
            # Should log extraction summary
            info_calls = [c for c in mock_logger.info.call_args_list
                         if "Extracted" in str(c) and "instances" in str(c)]
            assert len(info_calls) >= 1


# ---------------------------------------------------------------------------
# Dedup with anchors tests
# ---------------------------------------------------------------------------

class TestDedupWithAnchors:

    def test_ssn_anchor_merges_same_gov_id(self):
        """Same NI from 5 instances → 1 row when ssn anchor active."""
        records = [
            _make_record("Mr A Smith", gov_id="AB123456C", page_range=f"{i*3+1}-{i*3+3}")
            for i in range(5)
        ]
        result = _deduplicate_records(records, active_anchors=["ssn"])
        assert len(result) == 1
        # Page ranges should be concatenated
        assert "1-3" in result[0].page_range
        assert "13-15" in result[0].page_range

    def test_name_dob_anchor_merges(self):
        """Same name+DOB → merged when name_dob anchor active."""
        records = [
            _make_record("Alice Smith", dob="1990-01-01", page_range="1-3"),
            _make_record("Alice Smith", dob="1990-01-01", page_range="4-6"),
        ]
        result = _deduplicate_records(records, active_anchors=["name_dob"])
        assert len(result) == 1

    def test_email_anchor_merges(self):
        """Same email → merged."""
        records = [
            _make_record("A Smith", email="a@b.com", page_range="1-3"),
            _make_record("A Smith", email="a@b.com", page_range="4-6"),
        ]
        result = _deduplicate_records(records, active_anchors=["email"])
        assert len(result) == 1

    def test_fallback_instance_aware(self):
        """No matching anchor → falls back to (name, page_range)."""
        records = [
            _make_record("Alice Smith", page_range="1-3"),
            _make_record("Alice Smith", page_range="4-6"),
        ]
        # ssn anchor active but no gov_id → fallback to instance-aware
        result = _deduplicate_records(records, active_anchors=["ssn"])
        assert len(result) == 2  # NOT merged (different page ranges)

    def test_default_no_anchors_instance_aware(self):
        """No active_anchors → instance-aware dedup (backward compatible)."""
        records = [
            _make_record("Alice Smith", gov_id="AB123456C", page_range="1-3"),
            _make_record("Alice Smith", gov_id="AB123456C", page_range="4-6"),
        ]
        result = _deduplicate_records(records, active_anchors=None)
        assert len(result) == 2  # Different page ranges = different people

    def test_page_ranges_concatenated_on_merge(self):
        """Merged records have concatenated page ranges for lineage."""
        records = [
            _make_record("Alice Smith", gov_id="AB123456C", page_range="1-3"),
            _make_record("Alice Smith", gov_id="AB123456C", page_range="7-9"),
        ]
        result = _deduplicate_records(records, active_anchors=["ssn"])
        assert len(result) == 1
        assert "1-3" in result[0].page_range
        assert "7-9" in result[0].page_range

    def test_different_gov_ids_not_merged(self):
        """Different NI numbers → not merged even with ssn anchor."""
        records = [
            _make_record("Alice Smith", gov_id="AB123456C", page_range="1-3"),
            _make_record("Bob Jones", gov_id="XY987654D", page_range="4-6"),
        ]
        result = _deduplicate_records(records, active_anchors=["ssn"])
        assert len(result) == 2


# ---------------------------------------------------------------------------
# _build_anchor_key tests
# ---------------------------------------------------------------------------

class TestBuildAnchorKey:

    def test_ssn_anchor_with_gov_id(self):
        rec = _make_record(gov_id="AB123456C")
        key = _build_anchor_key(rec, ["ssn"])
        assert key == ("gov", "AB123456C")

    def test_ssn_anchor_without_gov_id_falls_back(self):
        rec = _make_record()
        key = _build_anchor_key(rec, ["ssn"])
        # Falls back to (name, page_range)
        assert isinstance(key, tuple)
        assert "alice smith" in key

    def test_name_dob_anchor(self):
        rec = _make_record(dob="1990-01-01")
        key = _build_anchor_key(rec, ["name_dob"])
        assert key == ("name_dob", "alice smith", "1990-01-01")

    def test_email_anchor(self):
        rec = _make_record(email="test@example.com")
        key = _build_anchor_key(rec, ["email"])
        assert key == ("email", "test@example.com")

    def test_phone_anchor(self):
        rec = _make_record(phone="+441234567890")
        key = _build_anchor_key(rec, ["phone"])
        assert key == ("phone", "+441234567890")

    def test_no_anchors_returns_instance_key(self):
        rec = _make_record(page_range="1-3")
        key = _build_anchor_key(rec, None)
        assert key == ("alice smith", "1-3")

    def test_priority_ssn_over_email(self):
        """SSN takes priority over email when both present."""
        rec = _make_record(gov_id="AB123456C", email="a@b.com")
        key = _build_anchor_key(rec, ["ssn", "email"])
        assert key[0] == "gov"


# ---------------------------------------------------------------------------
# Unload models test
# ---------------------------------------------------------------------------

class TestUnloadModels:

    def test_unloads_non_active_model(self):
        import httpx as _httpx
        client = MagicMock()
        client.base_url = "http://localhost:11434"
        client.model = "qwen2.5:7b"

        with patch.object(_httpx, "get") as mock_get, \
             patch.object(_httpx, "post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "models": [
                    {"name": "qwen2.5:7b"},
                    {"name": "llava:7b"},
                ]
            }
            mock_get.return_value = mock_resp
            mock_post.return_value = MagicMock()

            _unload_unused_models(client)

            # Should POST keep_alive=0 for llava:7b but NOT for qwen2.5:7b
            assert mock_post.call_count == 1
            assert "llava:7b" in str(mock_post.call_args_list[0])

    def test_unload_handles_connection_error(self):
        """Best-effort — doesn't raise on error."""
        import httpx as _httpx
        client = MagicMock()
        client.base_url = "http://localhost:11434"
        client.model = "qwen2.5:7b"

        with patch.object(_httpx, "get", side_effect=Exception("Connection refused")):
            # Should not raise
            _unload_unused_models(client)


# ---------------------------------------------------------------------------
# API dedup_anchors test
# ---------------------------------------------------------------------------

class TestAnalysisApiDedupAnchors:

    def test_analysis_returns_dedup_anchors(self):
        """GET /jobs/{id}/analysis should include dedup_anchors."""
        from uuid import uuid4, UUID

        from fastapi.testclient import TestClient
        from fastapi import FastAPI, Depends
        from app.api.routes.analysis_review import router, get_db

        app = FastAPI()

        job_id = str(uuid4())
        doc_id = uuid4()
        pc_id = str(uuid4())

        mock_run = MagicMock()
        mock_run.config_snapshot = {
            "protocol_id": "state_breach",
            "protocol_config_id": pc_id,
        }

        mock_pc = MagicMock()
        mock_pc.config_json = {"dedup_anchors": ["ssn", "email"]}

        mock_doc = MagicMock()
        mock_doc.id = doc_id
        mock_doc.ingestion_run_id = UUID(job_id)
        mock_doc.file_name = "test.pdf"
        mock_doc.file_type = "pdf"
        mock_doc.structure_class = None
        mock_doc.structure_analysis = {}
        mock_doc.entity_analysis = {}
        mock_doc.sample_onset_page = 0
        mock_doc.sample_extraction_count = 0
        mock_doc.analysis_phase_status = "approved"

        mock_db = MagicMock()

        def mock_get(model, id_val):
            name = getattr(model, "__name__", "")
            if name == "IngestionRun":
                return mock_run
            if name == "ProtocolConfig":
                return mock_pc
            return mock_run

        mock_db.get = MagicMock(side_effect=mock_get)
        mock_db.query.return_value.filter.return_value.all.return_value = [mock_doc]
        mock_db.query.return_value.filter.return_value.first.return_value = None

        app.dependency_overrides[get_db] = lambda: mock_db
        app.include_router(router)

        with patch("app.api.routes.analysis_review.get_settings") as mock_settings:
            mock_settings.return_value.pii_masking_enabled = True
            test_client = TestClient(app)
            resp = test_client.get(f"/jobs/{job_id}/analysis")

        data = resp.json()
        assert "dedup_anchors" in data
        assert data["dedup_anchors"] == ["ssn", "email"]
        assert data["protocol_name"] == "state_breach"
        assert "documents" in data

    def test_analysis_falls_back_to_protocol_llm_config(self):
        """When no ProtocolConfig exists, dedup_anchors should come from PROTOCOL_LLM_CONFIG."""
        from uuid import uuid4

        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        from app.api.routes.analysis_review import router, get_db

        app = FastAPI()
        job_id = str(uuid4())

        mock_run = MagicMock()
        mock_run.config_snapshot = {"protocol_id": "hipaa"}

        mock_db = MagicMock()
        mock_db.get = MagicMock(return_value=mock_run)
        mock_db.query.return_value.filter.return_value.all.return_value = []

        app.dependency_overrides[get_db] = lambda: mock_db
        app.include_router(router)

        with patch("app.api.routes.analysis_review.get_settings") as mock_settings:
            mock_settings.return_value.pii_masking_enabled = True
            resp = TestClient(app).get(f"/jobs/{job_id}/analysis")

        data = resp.json()
        assert data["dedup_anchors"] == ["ssn", "name_dob"]
        assert data["protocol_name"] == "hipaa"
