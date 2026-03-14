"""Tests for LLM extraction preview in analysis phase.

Covers:
- Preview prompt builder (build_preview_extraction_prompt)
- Preview response parser (_parse_preview_response)
- Preview dict structure (fields_found with per-field page numbers)
- Preview stored on DocumentAnalysisReview
- Instance count from find_instance_boundaries
- Empty/fallback scenarios
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db import models
from app.db.models import DocumentAnalysisReview
from app.llm.extraction_prompts import (
    ALWAYS_EXTRACT_IF_PRESENT,
    build_preview_extraction_prompt,
)
from app.pipeline.two_phase import _parse_preview_response
from app.rra.entity_resolver import PIIRecord
from app.structure.document_schema import (
    DocumentSchema,
    DocumentTemplate,
    PageRole,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_template_schema(
    instances: int = 5,
    pages_per: int = 3,
    instance_marker: str = "",
) -> DocumentSchema:
    """Pension-like template schema."""
    return DocumentSchema(
        document_type="pension_transfer_statement",
        document_subtype=None,
        issuing_entity="Mercer",
        field_map=[],
        people=[],
        organizations=["Mercer"],
        date_contexts=[],
        tables=[],
        suppression_hints=[],
        extraction_notes="",
        schema_confidence=0.9,
        detected_by="llm",
        template=DocumentTemplate(
            template_name="pension",
            pages_per_instance=pages_per,
            total_instances_estimate=instances,
            page_roles=[
                PageRole(
                    page_offset=0,
                    role="identity",
                    pii_fields_expected=["PERSON", "NI_NUMBER"],
                    is_identity_page=True,
                ),
                PageRole(
                    page_offset=1,
                    role="details",
                    pii_fields_expected=["LOCATION", "DATE_OF_BIRTH"],
                ),
            ],
            identity_page_offset=0,
            instance_marker=instance_marker,
        ),
    )


# ---------------------------------------------------------------------------
# Tests: Preview Prompt
# ---------------------------------------------------------------------------


class TestPreviewPrompt:
    """Test build_preview_extraction_prompt generates correct prompts."""

    def test_prompt_includes_all_pages(self):
        """All page texts appear in the prompt."""
        page_roles = [
            PageRole(0, "identity", ["PERSON", "NI_NUMBER"], True),
            PageRole(1, "details", ["LOCATION", "DATE_OF_BIRTH"]),
        ]
        prompt = build_preview_extraction_prompt(
            page_texts=["Page 1 text", "Page 2 text", "Page 3 text"],
            page_numbers_1indexed=[1, 2, 3],
            page_roles=page_roles,
            document_type="pension_statement",
        )
        assert "PAGE 1" in prompt
        assert "PAGE 2" in prompt
        assert "PAGE 3" in prompt
        assert "Page 1 text" in prompt
        assert "Page 2 text" in prompt
        assert "Page 3 text" in prompt

    def test_prompt_asks_for_page_numbers(self):
        """Prompt requests per-field page numbers."""
        page_roles = [
            PageRole(0, "identity", ["PERSON"], True),
        ]
        prompt = build_preview_extraction_prompt(
            page_texts=["text"],
            page_numbers_1indexed=[1],
            page_roles=page_roles,
            document_type="test",
        )
        assert '"page"' in prompt
        assert "page_number" in prompt

    def test_prompt_includes_always_extract_fields(self):
        """ALWAYS_EXTRACT_IF_PRESENT fields are in the prompt."""
        page_roles = [PageRole(0, "identity", ["PERSON"], True)]
        prompt = build_preview_extraction_prompt(
            page_texts=["text"],
            page_numbers_1indexed=[1],
            page_roles=page_roles,
            document_type="test",
        )
        assert "DATE_OF_BIRTH" in prompt
        assert "NI_NUMBER" in prompt
        assert "LOCATION" in prompt

    def test_prompt_multi_page_instructions(self):
        """Prompt emphasises checking all pages."""
        page_roles = [PageRole(0, "id", ["PERSON"], True)]
        prompt = build_preview_extraction_prompt(
            page_texts=["a", "b"],
            page_numbers_1indexed=[5, 6],
            page_roles=page_roles,
            document_type="test",
        )
        assert "EVERY page" in prompt
        assert "2 pages" in prompt
        assert "PAGE 5" in prompt
        assert "PAGE 6" in prompt


# ---------------------------------------------------------------------------
# Tests: Preview Response Parser
# ---------------------------------------------------------------------------


class TestParsePreviewResponse:
    """Test _parse_preview_response parses LLM output correctly."""

    def test_parse_with_page_numbers(self):
        """LLM returns {field: {value, page}} format."""
        response = json.dumps({
            "PERSON": {"value": "John Smith", "page": 1},
            "LOCATION": {"value": "123 Main St", "page": 2},
            "DATE_OF_BIRTH": {"value": "15/01/1980", "page": 2},
            "NI_NUMBER": {"value": "AB123456C", "page": 1},
        })
        result = _parse_preview_response(response, [1, 2, 3])

        assert result["PERSON"]["value"] == "John Smith"
        assert result["PERSON"]["page"] == 1
        assert result["LOCATION"]["value"] == "123 Main St"
        assert result["LOCATION"]["page"] == 2
        assert result["DATE_OF_BIRTH"]["value"] == "15/01/1980"
        assert result["DATE_OF_BIRTH"]["page"] == 2
        assert result["GOVERNMENT_ID"]["value"] == "AB123456C"
        assert result["GOVERNMENT_ID"]["page"] == 1

    def test_parse_flat_fallback(self):
        """LLM returns flat {field: value} format → default page."""
        response = json.dumps({
            "PERSON": "Jane Doe",
            "LOCATION": "456 Oak Rd",
        })
        result = _parse_preview_response(response, [5, 6])

        assert result["PERSON"]["value"] == "Jane Doe"
        assert result["PERSON"]["page"] == 5  # default = first valid page
        assert result["LOCATION"]["value"] == "456 Oak Rd"
        assert result["LOCATION"]["page"] == 5

    def test_parse_null_fields_excluded(self):
        """Null/empty values are excluded from result."""
        response = json.dumps({
            "PERSON": {"value": "Alice", "page": 1},
            "LOCATION": {"value": None, "page": None},
            "DATE_OF_BIRTH": {"value": "", "page": 2},
            "EMAIL_ADDRESS": {"value": "null", "page": 1},
        })
        result = _parse_preview_response(response, [1, 2])

        assert "PERSON" in result
        assert "LOCATION" not in result
        assert "DATE_OF_BIRTH" not in result
        assert "EMAIL" not in result

    def test_parse_invalid_page_uses_default(self):
        """Invalid page number → default to first valid page."""
        response = json.dumps({
            "PERSON": {"value": "Bob", "page": 99},
        })
        result = _parse_preview_response(response, [3, 4])

        assert result["PERSON"]["page"] == 3  # default

    def test_parse_markdown_fenced_json(self):
        """LLM wraps JSON in markdown code fences."""
        response = '```json\n{"PERSON": {"value": "Alice", "page": 1}}\n```'
        result = _parse_preview_response(response, [1, 2])
        assert result["PERSON"]["value"] == "Alice"

    def test_parse_empty_response(self):
        """Empty/invalid response → empty dict."""
        assert _parse_preview_response("", [1]) == {}
        assert _parse_preview_response("not json", [1]) == {}

    def test_parse_array_response(self):
        """Array response → take first element."""
        response = json.dumps([
            {"PERSON": {"value": "Alice", "page": 1}},
        ])
        result = _parse_preview_response(response, [1])
        assert result["PERSON"]["value"] == "Alice"

    def test_canonical_field_mapping(self):
        """LLM field names map to canonical preview fields."""
        response = json.dumps({
            "EMAIL_ADDRESS": {"value": "a@b.com", "page": 1},
            "PHONE_NUMBER": {"value": "555-1234", "page": 2},
            "US_SSN": {"value": "123-45-6789", "page": 1},
        })
        result = _parse_preview_response(response, [1, 2])

        assert "EMAIL" in result
        assert "PHONE" in result
        assert "GOVERNMENT_ID" in result
        assert result["EMAIL"]["value"] == "a@b.com"


# ---------------------------------------------------------------------------
# Tests: Preview Dict Structure
# ---------------------------------------------------------------------------


class TestPreviewDictStructure:
    """Test the preview dict has the expected shape."""

    def test_preview_fields_from_llm_response(self):
        """LLM response with per-field pages → correct fields_found."""
        response = json.dumps({
            "PERSON": {"value": "John Smith", "page": 1},
            "NI_NUMBER": {"value": "AB123456C", "page": 1},
            "DATE_OF_BIRTH": {"value": "1980-01-15", "page": 2},
            "LOCATION": {"value": "123 Main St", "page": 2},
        })
        fields_found = _parse_preview_response(response, [1, 2, 3])

        assert fields_found["PERSON"]["value"] == "John Smith"
        assert fields_found["PERSON"]["page"] == 1
        assert fields_found["DATE_OF_BIRTH"]["value"] == "1980-01-15"
        assert fields_found["DATE_OF_BIRTH"]["page"] == 2
        assert fields_found["LOCATION"]["value"] == "123 Main St"
        assert fields_found["LOCATION"]["page"] == 2
        assert fields_found["GOVERNMENT_ID"]["value"] == "AB123456C"
        assert fields_found["GOVERNMENT_ID"]["page"] == 1

    def test_preview_fields_missing_computed(self):
        """Fields in schema but not extracted → fields_missing."""
        fields_found = {
            "PERSON": {"value": "John Smith", "page": 1},
            "GOVERNMENT_ID": {"value": "AB123456C", "page": 1},
        }
        expected_fields = {"PERSON", "NI_NUMBER", "LOCATION", "DATE_OF_BIRTH"}
        expected_fields.update(ALWAYS_EXTRACT_IF_PRESENT)

        fields_missing = sorted(expected_fields - set(fields_found.keys()))
        assert "LOCATION" in fields_missing
        assert "DATE_OF_BIRTH" in fields_missing
        assert "PERSON" not in fields_missing

    def test_preview_pages_read(self):
        """pages_read contains 1-indexed page numbers of first instance."""
        first_instance = [0, 1, 2]
        pages_1 = sorted(int(p) + 1 for p in first_instance)
        assert pages_1 == [1, 2, 3]


# ---------------------------------------------------------------------------
# Tests: Instance Count
# ---------------------------------------------------------------------------


class TestPreviewInstanceCount:
    """Test instance count uses find_instance_boundaries."""

    def test_marker_based_instance_count(self):
        """find_instance_boundaries returns correct count."""
        schema = _make_template_schema(instances=5, pages_per=3, instance_marker="IN RESPECT OF:")
        page_texts = {}
        for i in range(15):
            if i % 3 == 0:
                page_texts[i] = f"IN RESPECT OF: Person {i // 3 + 1}\nDetails here"
            else:
                page_texts[i] = f"Page {i} content"

        instances = schema.template.find_instance_boundaries(page_texts)
        assert len(instances) == 5
        assert instances[0] == [0, 1, 2]
        assert instances[4] == [12, 13, 14]

    def test_stride_instance_count(self):
        """get_instance_pages for non-marker templates."""
        schema = _make_template_schema(instances=5, pages_per=3)
        instances = schema.template.get_instance_pages(15)
        assert len(instances) == 5

    def test_marker_variable_length_instances(self):
        """Marker-based boundaries handle variable-length instances."""
        schema = _make_template_schema(instances=3, pages_per=3, instance_marker="RECORD:")
        page_texts = {
            0: "RECORD: Person 1",
            1: "Details",
            2: "More details",
            3: "Extra page",
            4: "RECORD: Person 2",
            5: "Details",
            6: "RECORD: Person 3",
            7: "Details",
            8: "More details",
        }
        instances = schema.template.find_instance_boundaries(page_texts)
        assert len(instances) == 3
        assert instances[0] == [0, 1, 2, 3]  # 4 pages
        assert instances[1] == [4, 5]          # 2 pages
        assert instances[2] == [6, 7, 8]       # 3 pages


# ---------------------------------------------------------------------------
# Tests: DB Storage
# ---------------------------------------------------------------------------


class TestPreviewOnReviewRecord:
    """Test that extraction_preview is stored on DocumentAnalysisReview."""

    def test_extraction_preview_column_exists(self):
        """Column exists in schema."""
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(bind=engine)
        inspector = inspect(engine)
        cols = {c["name"]: c for c in inspector.get_columns("document_analysis_reviews")}
        assert "extraction_preview" in cols
        assert cols["extraction_preview"]["nullable"] is True

    def test_review_record_stores_preview_json(self):
        """DocumentAnalysisReview can store preview as JSON."""
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(bind=engine)
        from sqlalchemy.orm import sessionmaker
        SessionLocal = sessionmaker(bind=engine)
        db = SessionLocal()

        run = models.IngestionRun(
            source_path="/tmp/test",
            config_hash="abc123",
            code_version="1.0",
            initiated_by="test",
            status="analyzed",
        )
        db.add(run)
        db.flush()

        doc = models.Document(
            ingestion_run_id=run.id,
            source_path="/tmp/test/doc.pdf",
            file_name="doc.pdf",
            file_type="pdf",
            sha256="abc123def456",
        )
        db.add(doc)
        db.flush()

        preview = {
            "preview_instance": 0,
            "pages": "1-3",
            "fields_found": {
                "PERSON": {"value": "John Smith", "page": 1},
                "GOVERNMENT_ID": {"value": "AB123456C", "page": 1},
                "DATE_OF_BIRTH": {"value": "15/01/1980", "page": 2},
                "LOCATION": {"value": "123 Main St, London", "page": 2},
            },
            "fields_missing": [],
            "pages_read": [1, 2, 3],
            "total_instances_estimate": 5,
            "extraction_method": "llm_template",
            "pages_per_instance": 3,
        }

        review = DocumentAnalysisReview(
            document_id=doc.id,
            ingestion_run_id=run.id,
            status="pending_review",
            extraction_preview=preview,
        )
        db.add(review)
        db.commit()

        loaded = db.query(DocumentAnalysisReview).filter_by(id=review.id).one()
        assert loaded.extraction_preview is not None
        assert loaded.extraction_preview["fields_found"]["PERSON"]["value"] == "John Smith"
        assert loaded.extraction_preview["fields_found"]["PERSON"]["page"] == 1
        assert loaded.extraction_preview["fields_found"]["DATE_OF_BIRTH"]["page"] == 2
        assert loaded.extraction_preview["fields_found"]["LOCATION"]["page"] == 2
        assert loaded.extraction_preview["total_instances_estimate"] == 5
        assert loaded.extraction_preview["pages_read"] == [1, 2, 3]

        db.close()

    def test_review_record_null_preview_when_no_template(self):
        """Non-template docs have extraction_preview=None."""
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(bind=engine)
        from sqlalchemy.orm import sessionmaker
        SessionLocal = sessionmaker(bind=engine)
        db = SessionLocal()

        run = models.IngestionRun(
            source_path="/tmp/test",
            config_hash="abc123",
            code_version="1.0",
            initiated_by="test",
            status="analyzed",
        )
        db.add(run)
        db.flush()

        doc = models.Document(
            ingestion_run_id=run.id,
            source_path="/tmp/test/doc.pdf",
            file_name="doc.pdf",
            file_type="pdf",
            sha256="abc123def456",
        )
        db.add(doc)
        db.flush()

        review = DocumentAnalysisReview(
            document_id=doc.id,
            ingestion_run_id=run.id,
            status="auto_approved",
            extraction_preview=None,
        )
        db.add(review)
        db.commit()

        loaded = db.query(DocumentAnalysisReview).filter_by(id=review.id).one()
        assert loaded.extraction_preview is None

        db.close()
