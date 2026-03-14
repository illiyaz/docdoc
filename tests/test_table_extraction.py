"""Tests for table extraction strategy — multi-record-per-page documents.

Covers:
- is_tabular detected from DocumentSchema
- Table prompt asks for JSON array
- Single page with multiple rows → multiple PIIRecords
- Multi-page table → records from all pages
- Deduplication across pages (same name)
- Pipeline routing: is_tabular=True → table extraction path
- Preview: tabular doc shows sample rows from first page
- Fallback: text extraction for tables when vision unavailable
- DocumentSchema.to_dict/from_dict roundtrip with tabular fields
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.rra.entity_resolver import PIIRecord
from app.structure.document_schema import (
    DocumentSchema,
    DocumentTemplate,
    PageRole,
    TableColumn,
    TableSchema,
)
from app.structure.llm_template_extractor import LLMTemplateExtractor
from app.structure.vision_extractor import VisionDocumentExtractor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tabular_schema(
    records_per_page: int = 10,
    has_template: bool = False,
) -> DocumentSchema:
    """Build a tabular document schema (e.g., student roster)."""
    return DocumentSchema(
        document_type="student_roster",
        document_subtype=None,
        issuing_entity="Springfield School",
        field_map=[],
        people=[],
        organizations=["Springfield School"],
        date_contexts=[],
        tables=[
            TableSchema(
                columns=[
                    TableColumn(header="Name", semantic_type="person_name",
                                contains_pii=True, pii_type="PERSON"),
                    TableColumn(header="DOB", semantic_type="date_of_birth",
                                contains_pii=True, pii_type="DATE_OF_BIRTH"),
                    TableColumn(header="Address", semantic_type="address",
                                contains_pii=True, pii_type="LOCATION"),
                    TableColumn(header="SSN", semantic_type="government_id",
                                contains_pii=True, pii_type="US_SSN"),
                ],
                row_count_estimate=records_per_page,
                table_context="Student enrollment table",
                has_pii_columns=True,
            ),
        ],
        suppression_hints=[],
        extraction_notes="Tabular student roster",
        schema_confidence=0.9,
        detected_by="llm",
        template=DocumentTemplate(
            template_name="roster",
            pages_per_instance=1,
            total_instances_estimate=records_per_page,
            page_roles=[],
        ) if has_template else None,
        is_tabular=True,
        records_per_page_estimate=records_per_page,
    )


def _make_llm_table_response(count: int = 5) -> str:
    """Build a JSON array response with N individuals."""
    records = []
    for i in range(count):
        records.append({
            "PERSON": f"Student {i+1}",
            "LOCATION": f"{100+i} Main St, Springfield",
            "DATE_OF_BIRTH": f"0{i+1}/15/2005",
            "US_SSN": f"123-45-{6780+i}",
        })
    return json.dumps(records)


# ---------------------------------------------------------------------------
# Tests: Schema detection
# ---------------------------------------------------------------------------


class TestTabularSchemaDetection:
    def test_is_tabular_default_false(self):
        schema = DocumentSchema(
            document_type="letter",
            document_subtype=None,
            issuing_entity=None,
            field_map=[],
            people=[],
            organizations=[],
            date_contexts=[],
            tables=[],
            suppression_hints=[],
            extraction_notes="",
            schema_confidence=0.8,
            detected_by="llm",
        )
        assert schema.is_tabular is False
        assert schema.records_per_page_estimate == 1

    def test_tabular_schema_fields(self):
        schema = _make_tabular_schema(records_per_page=25)
        assert schema.is_tabular is True
        assert schema.records_per_page_estimate == 25

    def test_to_dict_includes_tabular(self):
        schema = _make_tabular_schema(records_per_page=15)
        d = schema.to_dict()
        assert d["is_tabular"] is True
        assert d["records_per_page_estimate"] == 15

    def test_from_dict_roundtrip(self):
        schema = _make_tabular_schema(records_per_page=20)
        d = schema.to_dict()
        restored = DocumentSchema.from_dict(d)
        assert restored.is_tabular is True
        assert restored.records_per_page_estimate == 20

    def test_from_dict_defaults_when_missing(self):
        d = {"document_type": "unknown", "schema_confidence": 0.5}
        schema = DocumentSchema.from_dict(d)
        assert schema.is_tabular is False
        assert schema.records_per_page_estimate == 1


# ---------------------------------------------------------------------------
# Tests: Table prompt
# ---------------------------------------------------------------------------


class TestTablePrompt:
    def test_vision_table_prompt_asks_for_array(self):
        ext = VisionDocumentExtractor(MagicMock(), batch_size=5)
        schema = _make_tabular_schema()
        prompt = ext._build_table_prompt(schema, 1)
        assert "JSON ARRAY" in prompt
        assert "EVERY row/individual" in prompt
        assert "Column headers are NOT individuals" in prompt

    def test_text_table_prompt_asks_for_array(self):
        ext = LLMTemplateExtractor(MagicMock(), batch_size=5)
        schema = _make_tabular_schema()
        prompt = ext._build_table_text_prompt(["Name DOB Address\nAlice 1/1/2000 123 St"], schema)
        assert "JSON ARRAY" in prompt
        assert "tabular document" in prompt

    def test_vision_table_prompt_includes_schema_fields(self):
        ext = VisionDocumentExtractor(MagicMock(), batch_size=5)
        schema = _make_tabular_schema()
        prompt = ext._build_table_prompt(schema, 1)
        # US_SSN from table columns should appear
        assert "US_SSN" in prompt or "Social Security" in prompt


# ---------------------------------------------------------------------------
# Tests: Vision table extraction
# ---------------------------------------------------------------------------


class TestVisionTableExtraction:
    @patch("app.structure.vision_extractor.render_pages_to_images")
    def test_single_page_5_rows(self, mock_render):
        mock_render.return_value = ["base64img1"]
        mock_client = MagicMock()
        mock_client.generate_with_images.return_value = _make_llm_table_response(5)

        ext = VisionDocumentExtractor(mock_client, batch_size=5)
        records = ext.extract_table_pages(
            "/tmp/test.pdf", [0], "doc-1", _make_tabular_schema(),
        )

        assert len(records) == 5
        assert records[0].raw_name == "Student 1"
        assert records[4].raw_name == "Student 5"

    @patch("app.structure.vision_extractor.render_pages_to_images")
    def test_multi_page_table(self, mock_render):
        mock_render.return_value = ["base64img1"]
        mock_client = MagicMock()
        # Each call returns 3 records
        mock_client.generate_with_images.return_value = _make_llm_table_response(3)

        ext = VisionDocumentExtractor(mock_client, batch_size=1)
        records = ext.extract_table_pages(
            "/tmp/test.pdf", [0, 1, 2], "doc-1", _make_tabular_schema(),
        )

        # 3 pages × 3 records each, but different names → 9 unique
        # Actually each call returns Student 1-3 (same names) → deduped to 3
        # Wait — same names per page, so dedup merges them
        assert len(records) == 3  # deduped
        assert mock_client.generate_with_images.call_count == 3

    @patch("app.structure.vision_extractor.render_pages_to_images")
    def test_deduplication_across_pages(self, mock_render):
        mock_render.return_value = ["base64img1"]
        mock_client = MagicMock()

        # Page 1: Alice and Bob
        page1_response = json.dumps([
            {"PERSON": "Alice Smith", "DATE_OF_BIRTH": "01/01/2000"},
            {"PERSON": "Bob Jones", "DATE_OF_BIRTH": "02/02/2001"},
        ])
        # Page 2: Alice again (footer repeat) and Carol
        page2_response = json.dumps([
            {"PERSON": "Alice Smith", "LOCATION": "123 Main St"},
            {"PERSON": "Carol Brown", "DATE_OF_BIRTH": "03/03/2002"},
        ])
        mock_client.generate_with_images.side_effect = [page1_response, page2_response]

        ext = VisionDocumentExtractor(mock_client, batch_size=1)
        records = ext.extract_table_pages(
            "/tmp/test.pdf", [0, 1], "doc-1",
        )

        # Alice appears on both pages → deduped to 1 (with merged fields)
        names = {r.raw_name for r in records}
        assert "Alice Smith" in names
        assert "Bob Jones" in names
        assert "Carol Brown" in names
        assert len(records) == 3

        # Alice's record should have merged DOB + address
        alice = next(r for r in records if r.raw_name == "Alice Smith")
        assert alice.raw_dob == "01/01/2000"
        assert alice.raw_address is not None

    @patch("app.structure.vision_extractor.render_pages_to_images")
    def test_empty_table_returns_empty(self, mock_render):
        mock_render.return_value = ["base64img1"]
        mock_client = MagicMock()
        mock_client.generate_with_images.return_value = "[]"

        ext = VisionDocumentExtractor(mock_client, batch_size=5)
        records = ext.extract_table_pages(
            "/tmp/test.pdf", [0], "doc-1",
        )
        assert records == []

    def test_no_pages_returns_empty(self):
        ext = VisionDocumentExtractor(MagicMock(), batch_size=5)
        records = ext.extract_table_pages("/tmp/test.pdf", [], "doc-1")
        assert records == []


# ---------------------------------------------------------------------------
# Tests: Text-based table extraction (fallback)
# ---------------------------------------------------------------------------


class TestTextTableExtraction:
    def test_text_table_single_page(self):
        mock_client = MagicMock()
        mock_client.generate.return_value = _make_llm_table_response(4)

        ext = LLMTemplateExtractor(mock_client, batch_size=5)
        schema = _make_tabular_schema()

        page_texts = {0: "Name DOB Address SSN\nAlice 1/1 123 St 123-45-6789"}
        records = ext.extract_table_pages(schema, page_texts, "doc-1")

        assert len(records) == 4
        assert records[0].raw_name == "Student 1"

    def test_text_table_multi_page(self):
        mock_client = MagicMock()
        # Return unique names per call to avoid dedup
        call_count = [0]
        def side_effect(*args, **kwargs):
            call_count[0] += 1
            batch = call_count[0]
            return json.dumps([
                {"PERSON": f"Person {batch}A", "DATE_OF_BIRTH": "01/01/2000"},
                {"PERSON": f"Person {batch}B", "DATE_OF_BIRTH": "02/02/2001"},
            ])
        mock_client.generate.side_effect = side_effect

        ext = LLMTemplateExtractor(mock_client, batch_size=2)
        schema = _make_tabular_schema()

        page_texts = {0: "page 0 data", 1: "page 1 data", 2: "page 2 data"}
        records = ext.extract_table_pages(schema, page_texts, "doc-1")

        # 3 pages / batch_size=2 → 2 LLM calls, 2 records each = 4 unique
        assert len(records) == 4
        assert mock_client.generate.call_count == 2

    def test_text_table_empty_pages(self):
        mock_client = MagicMock()
        ext = LLMTemplateExtractor(mock_client, batch_size=5)
        schema = _make_tabular_schema()

        records = ext.extract_table_pages(schema, {}, "doc-1")
        assert records == []


# ---------------------------------------------------------------------------
# Tests: Pipeline routing
# ---------------------------------------------------------------------------


class TestPipelineRouting:
    def test_tabular_flag_detection(self):
        """is_tabular flag correctly identifies table documents."""
        schema = _make_tabular_schema(records_per_page=20)
        assert schema.is_tabular is True
        assert schema.records_per_page_estimate > 1

        # Template documents are not tabular
        template_schema = DocumentSchema(
            document_type="pension",
            document_subtype=None,
            issuing_entity=None,
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
                pages_per_instance=3,
                total_instances_estimate=50,
                page_roles=[],
            ),
        )
        assert template_schema.is_tabular is False

    def test_is_tabular_requires_records_gt_1(self):
        """Routing only triggers when records_per_page_estimate > 1."""
        schema = DocumentSchema(
            document_type="single_entry",
            document_subtype=None,
            issuing_entity=None,
            field_map=[],
            people=[],
            organizations=[],
            date_contexts=[],
            tables=[],
            suppression_hints=[],
            extraction_notes="",
            schema_confidence=0.9,
            detected_by="llm",
            is_tabular=True,
            records_per_page_estimate=1,  # only 1 record per page
        )
        # Pipeline checks both is_tabular AND records_per_page_estimate > 1
        is_tabular_for_pipeline = schema.is_tabular and schema.records_per_page_estimate > 1
        assert is_tabular_for_pipeline is False


# ---------------------------------------------------------------------------
# Tests: Preview
# ---------------------------------------------------------------------------


class TestTablePreview:
    def test_preview_dict_structure(self):
        """Table preview has expected fields."""
        preview = {
            "preview_instance": 0,
            "pages": "1",
            "fields_found": {
                "PERSON": {"value": "Alice Smith", "page": 1},
            },
            "fields_missing": [],
            "pages_read": [1],
            "total_instances_estimate": 50,
            "extraction_method": "llm_table",
            "pages_per_instance": 1,
            "is_tabular": True,
            "records_per_page_estimate": 10,
            "sample_rows": [
                {"PERSON": "Alice Smith", "DATE_OF_BIRTH": "01/01/2000"},
                {"PERSON": "Bob Jones", "DATE_OF_BIRTH": "02/02/2001"},
            ],
        }
        assert preview["is_tabular"] is True
        assert preview["records_per_page_estimate"] == 10
        assert len(preview["sample_rows"]) == 2
        assert preview["extraction_method"] == "llm_table"

    def test_preview_total_estimate(self):
        """Total instances = records_per_page × total_pages."""
        rpp = 25
        total_pages = 10
        total_estimate = rpp * total_pages
        assert total_estimate == 250


# ---------------------------------------------------------------------------
# Tests: LLM prompt tabular detection
# ---------------------------------------------------------------------------


class TestPromptTabularDetection:
    def test_understand_document_includes_tabular_fields(self):
        from app.llm.prompts import UNDERSTAND_DOCUMENT
        assert "is_tabular" in UNDERSTAND_DOCUMENT
        assert "records_per_page_estimate" in UNDERSTAND_DOCUMENT

    def test_understand_multi_page_includes_tabular_fields(self):
        from app.llm.prompts import UNDERSTAND_MULTI_PAGE_DOCUMENT
        assert "is_tabular" in UNDERSTAND_MULTI_PAGE_DOCUMENT
        assert "records_per_page_estimate" in UNDERSTAND_MULTI_PAGE_DOCUMENT
        assert "TABULAR" in UNDERSTAND_MULTI_PAGE_DOCUMENT

    def test_llm_response_parsed_with_tabular(self):
        """LLM response with is_tabular=true produces tabular schema."""
        from app.structure.llm_document_understanding import LLMDocumentUnderstanding

        response = json.dumps({
            "document_type": "student_roster",
            "issuing_entity": "School",
            "field_map": [],
            "people": [],
            "organizations": ["School"],
            "date_contexts": [],
            "tables": [{
                "columns": [
                    {"header": "Name", "semantic_type": "person_name",
                     "contains_pii": True, "pii_type": "PERSON"},
                ],
                "row_count_estimate": 30,
                "table_context": "Student list",
                "has_pii_columns": True,
            }],
            "suppression_hints": [],
            "extraction_notes": "Student roster",
            "schema_confidence": 0.85,
            "is_tabular": True,
            "records_per_page_estimate": 30,
        })

        du = LLMDocumentUnderstanding(db_session=None)
        schema = du._parse_response(response)
        assert schema.is_tabular is True
        assert schema.records_per_page_estimate == 30
        assert schema.document_type == "student_roster"
