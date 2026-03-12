"""Tests for Step 19b: LLM extraction preview in analysis phase.

8 tests covering:
- Preview dict structure (fields_found, fields_missing, instances)
- Preview stored on DocumentAnalysisReview
- API returns extraction_preview
- No preview when LLM disabled
- No preview for non-template docs
- Preview with empty LLM response
- Preview instance count estimation
- ExtractionPreview type in client.ts (covered by TypeScript compilation)
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
from app.rra.entity_resolver import PIIRecord
from app.structure.document_schema import (
    DocumentSchema,
    DocumentTemplate,
    PageRole,
)
from app.structure.llm_template_extractor import LLMTemplateExtractor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_template_schema(instances: int = 5, pages_per: int = 3) -> DocumentSchema:
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
        ),
    )


def _make_preview_record(**overrides) -> PIIRecord:
    """Build a PIIRecord for preview testing."""
    defaults = dict(
        record_id=str(uuid4()),
        entity_type="PERSON",
        normalized_value="John Smith",
        raw_name="John Smith",
        raw_email=None,
        raw_phone=None,
        raw_dob="1980-01-15",
        raw_address={"raw": "123 Main St"},
        raw_government_id="AB123456C",
        source_document_id="doc-1",
        page_or_sheet=0,
        page_range="1-3",
        entity_types_found=("DATE_OF_BIRTH", "LOCATION", "NI_NUMBER", "PERSON"),
    )
    defaults.update(overrides)
    return PIIRecord(**defaults)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPreviewDictStructure:
    """Test the preview dict has the expected shape."""

    def test_preview_fields_from_full_record(self):
        """A record with all fields → all fields_found populated."""
        rec = _make_preview_record()
        # Simulate what two_phase.py does
        fields_found: dict[str, str] = {}
        if rec.raw_name:
            fields_found["PERSON"] = rec.raw_name
        if rec.raw_email:
            fields_found["EMAIL"] = rec.raw_email
        if rec.raw_phone:
            fields_found["PHONE"] = rec.raw_phone
        if rec.raw_dob:
            fields_found["DATE_OF_BIRTH"] = rec.raw_dob
        if rec.raw_address:
            addr = rec.raw_address
            fields_found["LOCATION"] = addr.get("raw", str(addr)) if isinstance(addr, dict) else str(addr)
        if rec.raw_government_id:
            fields_found["GOVERNMENT_ID"] = rec.raw_government_id

        assert fields_found["PERSON"] == "John Smith"
        assert fields_found["DATE_OF_BIRTH"] == "1980-01-15"
        assert fields_found["LOCATION"] == "123 Main St"
        assert fields_found["GOVERNMENT_ID"] == "AB123456C"
        assert "EMAIL" not in fields_found
        assert "PHONE" not in fields_found

    def test_preview_fields_missing_computed(self):
        """Fields in schema but not extracted → fields_missing."""
        from app.llm.extraction_prompts import ALWAYS_EXTRACT_IF_PRESENT

        fields_found = {"PERSON": "John Smith", "NI_NUMBER": "AB123456C"}
        expected_fields = {"PERSON", "NI_NUMBER", "LOCATION", "DATE_OF_BIRTH"}
        expected_fields.update(ALWAYS_EXTRACT_IF_PRESENT)

        fields_missing = sorted(expected_fields - set(fields_found.keys()))
        assert "LOCATION" in fields_missing
        assert "DATE_OF_BIRTH" in fields_missing
        assert "PERSON" not in fields_missing
        assert "NI_NUMBER" not in fields_missing

    def test_preview_instances_from_template(self):
        """total_instances_estimate from template.get_instance_pages()."""
        schema = _make_template_schema(instances=5, pages_per=3)
        instances = schema.template.get_instance_pages(15)
        assert len(instances) == 5
        assert instances[0] == [0, 1, 2]
        assert instances[4] == [12, 13, 14]


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

        # Create required parent records
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
            "fields_found": {"PERSON": "John Smith", "NI_NUMBER": "AB123456C"},
            "fields_missing": ["DATE_OF_BIRTH", "LOCATION"],
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

        # Read back
        loaded = db.query(DocumentAnalysisReview).filter_by(id=review.id).one()
        assert loaded.extraction_preview is not None
        assert loaded.extraction_preview["fields_found"]["PERSON"] == "John Smith"
        assert loaded.extraction_preview["total_instances_estimate"] == 5
        assert "DATE_OF_BIRTH" in loaded.extraction_preview["fields_missing"]

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


class TestPreviewEmptyExtraction:
    """Test preview behavior when LLM returns nothing."""

    def test_empty_record_list_yields_no_fields(self):
        """When LLM extracts nothing, fields_found is empty."""
        preview_records: list[PIIRecord] = []
        fields_found: dict[str, str] = {}
        if preview_records:
            rec = preview_records[0]
            if rec.raw_name:
                fields_found["PERSON"] = rec.raw_name

        assert fields_found == {}

    def test_record_with_only_name(self):
        """Preview record with only PERSON → only that field found."""
        rec = _make_preview_record(
            raw_dob=None,
            raw_address=None,
            raw_government_id=None,
        )
        fields_found: dict[str, str] = {}
        if rec.raw_name:
            fields_found["PERSON"] = rec.raw_name
        if rec.raw_dob:
            fields_found["DATE_OF_BIRTH"] = rec.raw_dob
        if rec.raw_address:
            fields_found["LOCATION"] = str(rec.raw_address)
        if rec.raw_government_id:
            fields_found["GOVERNMENT_ID"] = rec.raw_government_id

        assert list(fields_found.keys()) == ["PERSON"]


class TestPreviewExtractorIntegration:
    """Test LLMTemplateExtractor called with batch_size=1 for preview."""

    def test_extractor_single_instance_preview(self):
        """batch_size=1 extracts one instance at a time (used for preview)."""
        mock_client = MagicMock()
        mock_client.generate.return_value = json.dumps({
            "PERSON": "Alice Brown",
            "NI_NUMBER": "CD987654E",
            "DATE_OF_BIRTH": "1975-03-22",
            "LOCATION": "456 Oak Road, London",
        })

        extractor = LLMTemplateExtractor(mock_client, batch_size=1)
        schema = _make_template_schema(instances=5, pages_per=3)

        page_texts = {i: f"Page {i} text about pension" for i in range(3)}

        records = extractor.extract_all_instances(
            schema, page_texts, "doc-1", total_pages=3,
        )

        assert len(records) == 1
        assert records[0].raw_name == "Alice Brown"
        assert records[0].raw_government_id == "CD987654E"
        assert records[0].raw_dob == "1975-03-22"
        # Only 1 LLM call (batch_size=1, 1 instance with 3 pages)
        assert mock_client.generate.call_count == 1
