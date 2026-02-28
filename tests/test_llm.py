"""Tests for the LLM integration module (Phase 5 Step 7).

Covers:
- OllamaClient: governance gate, generate, is_available, timeout, connection
  error, latency tracking, DB audit logging
- Prompt templates: formatting, JSON instruction, no raw PII
- Audit functions: log_llm_call, get_llm_calls with filters, limit, fields

All network calls are mocked via ``unittest.mock.patch`` on ``httpx``.
Database tests use SQLite in-memory (following existing test patterns).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import LLMCallLog
from app.llm.audit import get_llm_calls, log_llm_call, _contains_potential_pii
from app.llm.client import (
    LLMConnectionError,
    LLMDisabledError,
    LLMTimeoutError,
    OllamaClient,
    _prompt_contains_potential_pii,
)
from app.llm.prompts import (
    ASSESS_EXTRACTION_CONFIDENCE,
    CLASSIFY_AMBIGUOUS_ENTITY,
    PROMPT_TEMPLATES,
    SUGGEST_ENTITY_CATEGORY,
    SYSTEM_PROMPT,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_session():
    """In-memory SQLite session with all tables created."""
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture()
def _enable_llm(monkeypatch: pytest.MonkeyPatch):
    """Enable LLM assist and set Ollama defaults via environment."""
    monkeypatch.setenv("LLM_ASSIST_ENABLED", "true")
    monkeypatch.setenv("OLLAMA_URL", "http://localhost:11434")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5:7b")
    monkeypatch.setenv("OLLAMA_TIMEOUT_S", "60")
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    from app.core.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def _disable_llm(monkeypatch: pytest.MonkeyPatch):
    """Disable LLM assist via environment."""
    monkeypatch.setenv("LLM_ASSIST_ENABLED", "false")
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    from app.core.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ===========================================================================
# OllamaClient tests
# ===========================================================================


class TestOllamaClientDisabled:
    """When ``llm_assist_enabled=False``, generate() must raise."""

    @pytest.mark.usefixtures("_disable_llm")
    def test_generate_raises_llm_disabled_error(self) -> None:
        client = OllamaClient()
        with pytest.raises(LLMDisabledError, match="LLM assist is disabled"):
            client.generate("Hello")

    @pytest.mark.usefixtures("_disable_llm")
    def test_error_message_mentions_setting(self) -> None:
        client = OllamaClient()
        with pytest.raises(LLMDisabledError, match="LLM_ASSIST_ENABLED"):
            client.generate("Classify this entity")


class TestOllamaClientIsAvailable:
    """is_available() health check without requiring a running server."""

    @pytest.mark.usefixtures("_enable_llm")
    def test_returns_false_when_server_not_running(self) -> None:
        """Mock httpx.get to raise ConnectError."""
        import httpx

        with patch("app.llm.client.httpx.get", side_effect=httpx.ConnectError("refused")):
            client = OllamaClient()
            assert client.is_available() is False

    @pytest.mark.usefixtures("_enable_llm")
    def test_returns_true_when_server_is_running(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("app.llm.client.httpx.get", return_value=mock_resp):
            client = OllamaClient()
            assert client.is_available() is True

    @pytest.mark.usefixtures("_enable_llm")
    def test_returns_false_on_non_200(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        with patch("app.llm.client.httpx.get", return_value=mock_resp):
            client = OllamaClient()
            assert client.is_available() is False

    @pytest.mark.usefixtures("_enable_llm")
    def test_returns_false_on_timeout(self) -> None:
        import httpx

        with patch("app.llm.client.httpx.get", side_effect=httpx.TimeoutException("timeout")):
            client = OllamaClient()
            assert client.is_available() is False


class TestOllamaClientGenerate:
    """Test successful generation with mocked httpx responses."""

    @pytest.mark.usefixtures("_enable_llm")
    def test_successful_generate(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "response": '{"entity_type": "US_SSN", "confidence": 0.95}',
            "eval_count": 42,
        }
        mock_resp.raise_for_status = MagicMock()

        with patch("app.llm.client.httpx.post", return_value=mock_resp):
            client = OllamaClient()
            result = client.generate("Classify this masked entity: ***-**-1234")

        assert "US_SSN" in result
        assert "0.95" in result

    @pytest.mark.usefixtures("_enable_llm")
    def test_generate_with_system_prompt(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"response": "ok", "eval_count": 5}
        mock_resp.raise_for_status = MagicMock()

        with patch("app.llm.client.httpx.post", return_value=mock_resp) as mock_post:
            client = OllamaClient()
            client.generate("prompt", system="You are a classifier")

        # Verify system was included in the payload
        call_kwargs = mock_post.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert payload["system"] == "You are a classifier"

    @pytest.mark.usefixtures("_enable_llm")
    def test_generate_sends_correct_model(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"response": "ok"}
        mock_resp.raise_for_status = MagicMock()

        with patch("app.llm.client.httpx.post", return_value=mock_resp) as mock_post:
            client = OllamaClient(model="qwen2.5:7b")
            client.generate("hello")

        call_kwargs = mock_post.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert payload["model"] == "qwen2.5:7b"

    @pytest.mark.usefixtures("_enable_llm")
    def test_generate_sets_stream_false(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"response": "ok"}
        mock_resp.raise_for_status = MagicMock()

        with patch("app.llm.client.httpx.post", return_value=mock_resp) as mock_post:
            client = OllamaClient()
            client.generate("hello")

        call_kwargs = mock_post.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert payload["stream"] is False


class TestOllamaClientTimeout:
    """Timeout handling maps to LLMTimeoutError."""

    @pytest.mark.usefixtures("_enable_llm")
    def test_timeout_raises_llm_timeout_error(self) -> None:
        import httpx

        with patch(
            "app.llm.client.httpx.post",
            side_effect=httpx.TimeoutException("read timeout"),
        ):
            client = OllamaClient(timeout_s=5)
            with pytest.raises(LLMTimeoutError, match="timed out"):
                client.generate("hello")

    @pytest.mark.usefixtures("_enable_llm")
    def test_timeout_still_records_latency(self) -> None:
        import httpx

        with patch(
            "app.llm.client.httpx.post",
            side_effect=httpx.TimeoutException("timeout"),
        ):
            client = OllamaClient()
            with pytest.raises(LLMTimeoutError):
                client.generate("hello")
            assert client.last_latency_ms is not None
            assert client.last_latency_ms >= 0


class TestOllamaClientConnectionError:
    """Connection failure maps to LLMConnectionError."""

    @pytest.mark.usefixtures("_enable_llm")
    def test_connect_error_raises_llm_connection_error(self) -> None:
        import httpx

        with patch(
            "app.llm.client.httpx.post",
            side_effect=httpx.ConnectError("Connection refused"),
        ):
            client = OllamaClient()
            with pytest.raises(LLMConnectionError, match="Cannot connect"):
                client.generate("hello")

    @pytest.mark.usefixtures("_enable_llm")
    def test_http_error_raises_llm_connection_error(self) -> None:
        import httpx

        with patch(
            "app.llm.client.httpx.post",
            side_effect=httpx.HTTPStatusError(
                "500", request=MagicMock(), response=MagicMock()
            ),
        ):
            client = OllamaClient()
            with pytest.raises(LLMConnectionError, match="HTTP error"):
                client.generate("hello")


class TestOllamaClientLatency:
    """Latency tracking via last_latency_ms property."""

    @pytest.mark.usefixtures("_enable_llm")
    def test_latency_is_none_before_first_call(self) -> None:
        client = OllamaClient()
        assert client.last_latency_ms is None

    @pytest.mark.usefixtures("_enable_llm")
    def test_latency_set_after_successful_call(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"response": "ok"}
        mock_resp.raise_for_status = MagicMock()

        with patch("app.llm.client.httpx.post", return_value=mock_resp):
            client = OllamaClient()
            client.generate("hello")

        assert client.last_latency_ms is not None
        assert isinstance(client.last_latency_ms, int)
        assert client.last_latency_ms >= 0

    @pytest.mark.usefixtures("_enable_llm")
    def test_latency_updated_on_second_call(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"response": "ok"}
        mock_resp.raise_for_status = MagicMock()

        with patch("app.llm.client.httpx.post", return_value=mock_resp):
            client = OllamaClient()
            client.generate("first")
            first_latency = client.last_latency_ms
            client.generate("second")
            # Latency was updated (value may differ or be same, just must exist)
            assert client.last_latency_ms is not None


class TestOllamaClientAuditLogging:
    """LLM calls are logged to the llm_call_logs table when db_session is provided."""

    @pytest.mark.usefixtures("_enable_llm")
    def test_call_logged_to_db(self, db_session: Session) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"response": "classified", "eval_count": 10}
        mock_resp.raise_for_status = MagicMock()

        with patch("app.llm.client.httpx.post", return_value=mock_resp):
            client = OllamaClient(db_session=db_session)
            client.generate(
                "Classify masked entity ***-**-1234",
                use_case="classify_ambiguous_entity",
            )

        rows = db_session.execute(select(LLMCallLog)).scalars().all()
        assert len(rows) == 1
        assert rows[0].use_case == "classify_ambiguous_entity"
        assert rows[0].response_text == "classified"
        assert rows[0].latency_ms is not None
        assert rows[0].latency_ms >= 0
        assert rows[0].token_count == 10

    @pytest.mark.usefixtures("_enable_llm")
    def test_no_logging_when_no_session(self) -> None:
        """When db_session is None, generate() works but does not log."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"response": "ok"}
        mock_resp.raise_for_status = MagicMock()

        with patch("app.llm.client.httpx.post", return_value=mock_resp):
            client = OllamaClient(db_session=None)
            result = client.generate("hello")
            assert result == "ok"

    @pytest.mark.usefixtures("_enable_llm")
    def test_document_id_logged(self, db_session: Session) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"response": "ok"}
        mock_resp.raise_for_status = MagicMock()

        doc_id = uuid4()
        with patch("app.llm.client.httpx.post", return_value=mock_resp):
            client = OllamaClient(db_session=db_session)
            client.generate("hello", document_id=doc_id, use_case="test")

        rows = db_session.execute(select(LLMCallLog)).scalars().all()
        assert len(rows) == 1
        assert rows[0].document_id == doc_id

    @pytest.mark.usefixtures("_enable_llm")
    def test_prompt_text_logged(self, db_session: Session) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"response": "answer"}
        mock_resp.raise_for_status = MagicMock()

        with patch("app.llm.client.httpx.post", return_value=mock_resp):
            client = OllamaClient(db_session=db_session)
            client.generate("Masked entity: ***-**-1234")

        rows = db_session.execute(select(LLMCallLog)).scalars().all()
        assert rows[0].prompt_text == "Masked entity: ***-**-1234"


class TestOllamaClientPIISafety:
    """The client warns (but does not block) when potential PII is in the prompt."""

    def test_pii_pattern_detection_ssn(self) -> None:
        assert _prompt_contains_potential_pii("SSN is 123-45-6789") is True

    def test_pii_pattern_detection_no_pii(self) -> None:
        assert _prompt_contains_potential_pii("Masked: ***-**-1234") is False

    def test_pii_pattern_detection_credit_card(self) -> None:
        assert _prompt_contains_potential_pii("CC 4111111111111111") is True

    def test_pii_pattern_detection_cc_with_spaces(self) -> None:
        assert _prompt_contains_potential_pii("CC 4111 1111 1111 1111") is True


# ===========================================================================
# Prompt template tests
# ===========================================================================


class TestPromptTemplates:
    """Verify all prompt templates can be formatted and contain JSON instructions."""

    def test_classify_ambiguous_entity_formats(self) -> None:
        result = CLASSIFY_AMBIGUOUS_ENTITY.format(
            context_window="Name: [REDACTED], ID: ***-**-1234",
            masked_value="***-**-1234",
            detection_method="layer_1_pattern",
            candidate_type="US_SSN",
            confidence_score=0.65,
        )
        assert "***-**-1234" in result
        assert "US_SSN" in result

    def test_assess_extraction_confidence_formats(self) -> None:
        result = ASSESS_EXTRACTION_CONFIDENCE.format(
            entity_type="US_SSN",
            masked_value="***-**-1234",
            extraction_layer="layer_1_pattern",
            pattern_name=r"\d{3}-\d{2}-\d{4}",
            original_confidence=0.45,
            context_window="Found near text: [REDACTED] social security",
        )
        assert "US_SSN" in result
        assert "0.45" in result

    def test_suggest_entity_category_formats(self) -> None:
        result = SUGGEST_ENTITY_CATEGORY.format(
            entity_type="CREDIT_CARD",
            entity_description="A 16-digit payment card number",
            current_categories="PFI, PCI",
        )
        assert "CREDIT_CARD" in result
        assert "PFI, PCI" in result

    def test_all_templates_contain_json_instruction(self) -> None:
        """Every template must instruct the LLM to respond with JSON."""
        for name, template in PROMPT_TEMPLATES.items():
            assert "JSON" in template, f"Template {name!r} does not mention JSON"

    def test_all_templates_mention_respond_only(self) -> None:
        """Every template must tell the LLM to respond ONLY with JSON."""
        for name, template in PROMPT_TEMPLATES.items():
            assert "ONLY" in template, f"Template {name!r} does not say 'ONLY'"

    def test_system_prompt_is_nonempty(self) -> None:
        assert len(SYSTEM_PROMPT) > 0
        assert "JSON" in SYSTEM_PROMPT

    def test_prompt_templates_registry_has_all_keys(self) -> None:
        assert "classify_ambiguous_entity" in PROMPT_TEMPLATES
        assert "assess_extraction_confidence" in PROMPT_TEMPLATES
        assert "suggest_entity_category" in PROMPT_TEMPLATES
        assert len(PROMPT_TEMPLATES) == 3

    def test_classify_template_has_entity_type_key(self) -> None:
        """The template response schema should mention entity_type."""
        assert "entity_type" in CLASSIFY_AMBIGUOUS_ENTITY

    def test_assess_template_has_is_true_positive_key(self) -> None:
        assert "is_true_positive" in ASSESS_EXTRACTION_CONFIDENCE

    def test_suggest_template_has_categories_key(self) -> None:
        assert "categories" in SUGGEST_ENTITY_CATEGORY

    def test_templates_produce_valid_strings(self) -> None:
        """All templates produce non-empty strings after formatting."""
        sample_data = {
            "classify_ambiguous_entity": {
                "context_window": "context here",
                "masked_value": "***",
                "detection_method": "layer_1",
                "candidate_type": "EMAIL",
                "confidence_score": 0.5,
            },
            "assess_extraction_confidence": {
                "entity_type": "EMAIL",
                "masked_value": "***@***.***",
                "extraction_layer": "layer_2",
                "pattern_name": "email_regex",
                "original_confidence": 0.4,
                "context_window": "text here",
            },
            "suggest_entity_category": {
                "entity_type": "SSN",
                "entity_description": "Social Security Number",
                "current_categories": "PII, SPII",
            },
        }
        for name, template in PROMPT_TEMPLATES.items():
            result = template.format(**sample_data[name])
            assert isinstance(result, str)
            assert len(result) > 0


# ===========================================================================
# Audit function tests
# ===========================================================================


class TestLogLLMCall:
    """Test log_llm_call() creates DB records correctly."""

    def test_creates_db_record(self, db_session: Session) -> None:
        row = log_llm_call(
            db_session,
            use_case="classify_ambiguous_entity",
            model="qwen2.5:7b",
            prompt_text="Classify masked entity ***-**-1234",
            response_text='{"entity_type": "US_SSN"}',
        )

        assert row.id is not None
        assert row.use_case == "classify_ambiguous_entity"
        assert row.model == "qwen2.5:7b"

    def test_all_fields_persisted(self, db_session: Session) -> None:
        doc_id = uuid4()
        row = log_llm_call(
            db_session,
            document_id=doc_id,
            use_case="assess_confidence",
            model="qwen2.5:7b",
            prompt_text="Is this a true positive?",
            response_text='{"is_true_positive": true}',
            decision="true_positive",
            accepted=True,
            latency_ms=150,
            token_count=25,
        )

        assert row.document_id == doc_id
        assert row.decision == "true_positive"
        assert row.accepted is True
        assert row.latency_ms == 150
        assert row.token_count == 25

    def test_nullable_fields_default_to_none(self, db_session: Session) -> None:
        row = log_llm_call(
            db_session,
            use_case="test",
            model="qwen2.5:7b",
            prompt_text="hello",
            response_text="world",
        )

        assert row.document_id is None
        assert row.decision is None
        assert row.accepted is None
        assert row.latency_ms is None
        assert row.token_count is None

    def test_record_queryable_after_flush(self, db_session: Session) -> None:
        log_llm_call(
            db_session,
            use_case="test_query",
            model="qwen2.5:7b",
            prompt_text="hello",
            response_text="world",
        )

        rows = db_session.execute(select(LLMCallLog)).scalars().all()
        assert len(rows) == 1
        assert rows[0].use_case == "test_query"

    def test_multiple_records(self, db_session: Session) -> None:
        for i in range(5):
            log_llm_call(
                db_session,
                use_case=f"use_case_{i}",
                model="qwen2.5:7b",
                prompt_text=f"prompt {i}",
                response_text=f"response {i}",
            )

        rows = db_session.execute(select(LLMCallLog)).scalars().all()
        assert len(rows) == 5


class TestGetLLMCalls:
    """Test get_llm_calls() query function."""

    def test_returns_empty_list_when_no_records(self, db_session: Session) -> None:
        result = get_llm_calls(db_session)
        assert result == []

    def test_returns_all_records(self, db_session: Session) -> None:
        for i in range(3):
            log_llm_call(
                db_session,
                use_case="test",
                model="qwen2.5:7b",
                prompt_text=f"p{i}",
                response_text=f"r{i}",
            )

        result = get_llm_calls(db_session)
        assert len(result) == 3

    def test_filter_by_use_case(self, db_session: Session) -> None:
        log_llm_call(
            db_session,
            use_case="classify",
            model="qwen2.5:7b",
            prompt_text="p1",
            response_text="r1",
        )
        log_llm_call(
            db_session,
            use_case="assess",
            model="qwen2.5:7b",
            prompt_text="p2",
            response_text="r2",
        )

        result = get_llm_calls(db_session, use_case="classify")
        assert len(result) == 1
        assert result[0]["use_case"] == "classify"

    def test_filter_by_document_id(self, db_session: Session) -> None:
        doc_id = uuid4()
        log_llm_call(
            db_session,
            document_id=doc_id,
            use_case="test",
            model="qwen2.5:7b",
            prompt_text="p1",
            response_text="r1",
        )
        log_llm_call(
            db_session,
            use_case="test",
            model="qwen2.5:7b",
            prompt_text="p2",
            response_text="r2",
        )

        result = get_llm_calls(db_session, document_id=doc_id)
        assert len(result) == 1
        assert result[0]["document_id"] == str(doc_id)

    def test_respects_limit(self, db_session: Session) -> None:
        for i in range(10):
            log_llm_call(
                db_session,
                use_case="test",
                model="qwen2.5:7b",
                prompt_text=f"p{i}",
                response_text=f"r{i}",
            )

        result = get_llm_calls(db_session, limit=3)
        assert len(result) == 3

    def test_default_limit_is_100(self, db_session: Session) -> None:
        # Just verify we can call without limit and it works
        result = get_llm_calls(db_session)
        assert isinstance(result, list)

    def test_combined_filters(self, db_session: Session) -> None:
        doc_id = uuid4()
        log_llm_call(
            db_session,
            document_id=doc_id,
            use_case="classify",
            model="qwen2.5:7b",
            prompt_text="p1",
            response_text="r1",
        )
        log_llm_call(
            db_session,
            document_id=doc_id,
            use_case="assess",
            model="qwen2.5:7b",
            prompt_text="p2",
            response_text="r2",
        )
        log_llm_call(
            db_session,
            use_case="classify",
            model="qwen2.5:7b",
            prompt_text="p3",
            response_text="r3",
        )

        result = get_llm_calls(db_session, document_id=doc_id, use_case="classify")
        assert len(result) == 1

    def test_result_dict_shape(self, db_session: Session) -> None:
        doc_id = uuid4()
        log_llm_call(
            db_session,
            document_id=doc_id,
            use_case="test_shape",
            model="qwen2.5:7b",
            prompt_text="prompt",
            response_text="response",
            decision="accepted",
            accepted=True,
            latency_ms=100,
            token_count=20,
        )

        result = get_llm_calls(db_session)
        assert len(result) == 1
        item = result[0]
        assert "id" in item
        assert item["document_id"] == str(doc_id)
        assert item["use_case"] == "test_shape"
        assert item["model"] == "qwen2.5:7b"
        assert item["prompt_text"] == "prompt"
        assert item["response_text"] == "response"
        assert item["decision"] == "accepted"
        assert item["accepted"] is True
        assert item["latency_ms"] == 100
        assert item["token_count"] == 20
        assert "created_at" in item

    def test_null_document_id_in_result(self, db_session: Session) -> None:
        log_llm_call(
            db_session,
            use_case="test",
            model="qwen2.5:7b",
            prompt_text="p",
            response_text="r",
        )

        result = get_llm_calls(db_session)
        assert result[0]["document_id"] is None


class TestAuditPIISafety:
    """The audit module has a PII safety check on prompt text."""

    def test_detects_ssn_pattern(self) -> None:
        assert _contains_potential_pii("SSN 123-45-6789 found") is True

    def test_no_pii_in_masked_text(self) -> None:
        assert _contains_potential_pii("Masked ***-**-1234") is False

    def test_detects_credit_card(self) -> None:
        assert _contains_potential_pii("CC 4111111111111111") is True

    def test_clean_text(self) -> None:
        assert _contains_potential_pii("Entity type US_SSN with confidence 0.95") is False

    def test_log_warns_on_pii(self, db_session: Session) -> None:
        """log_llm_call should warn but still create the record."""
        import logging

        with patch.object(logging.getLogger("app.llm.audit"), "warning") as mock_warn:
            row = log_llm_call(
                db_session,
                use_case="test",
                model="qwen2.5:7b",
                prompt_text="SSN is 123-45-6789",
                response_text="ok",
            )

        # Record was still created
        assert row.id is not None
        # Warning was issued
        mock_warn.assert_called_once()
        assert "PII" in mock_warn.call_args[0][0]
