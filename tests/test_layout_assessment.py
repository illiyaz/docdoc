"""Tests for Step 21 Run 1: Layout assessment + FieldMapping model.

Tests:
- FieldMapping dataclass creation and defaults
- DocumentSchema with layout fields (to_dict/from_dict roundtrip)
- Fixed layout doc → layout_field_map populated
- Variable layout doc → layout_field_map None
- _parse_response() parses layout_type and layout_field_map
- Safety: fixed layout without field map → downgraded to variable
- Defensive: bad layout_field_map data → defaults gracefully
- LLM prompts include layout_type and layout_field_map
"""
from __future__ import annotations

import json

import pytest

from app.structure.document_schema import (
    DocumentSchema,
    FieldContext,
    FieldMapping,
)
from app.structure.llm_document_understanding import LLMDocumentUnderstanding


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse(data: dict) -> DocumentSchema:
    """Shortcut: serialize data to JSON and parse via _parse_response."""
    du = LLMDocumentUnderstanding(db_session=None)
    return du._parse_response(json.dumps(data))


def _minimal_data(**overrides) -> dict:
    base = {
        "document_type": "financial_statement",
        "issuing_entity": "ACME Corp",
        "field_map": [],
        "people": [],
        "organizations": ["ACME Corp"],
        "date_contexts": [],
        "tables": [],
        "suppression_hints": [],
        "extraction_notes": "",
        "schema_confidence": 0.85,
    }
    base.update(overrides)
    return base


SAMPLE_FIELD_MAP = [
    {
        "field_type": "PERSON",
        "anchor_text": "Client:",
        "spatial_relationship": "same_line_right",
        "value_pattern": None,
        "sample_bbox": [150, 120, 450, 140],
        "line_count": 1,
        "skip_pattern": "\\(\\d+\\)",
    },
    {
        "field_type": "GOVERNMENT_ID",
        "anchor_text": "Tax No",
        "spatial_relationship": "line_below",
        "value_pattern": "\\d{3}-\\d{2}-\\d{4}",
        "sample_bbox": [400, 85, 520, 100],
        "line_count": 1,
        "skip_pattern": None,
    },
    {
        "field_type": "LOCATION",
        "anchor_text": "In Account with",
        "spatial_relationship": "lines_below_4",
        "value_pattern": None,
        "sample_bbox": [300, 140, 550, 220],
        "line_count": 4,
        "skip_pattern": None,
    },
]


# ---------------------------------------------------------------------------
# FieldMapping dataclass
# ---------------------------------------------------------------------------

class TestFieldMapping:

    def test_creation_with_all_fields(self):
        fm = FieldMapping(
            field_type="PERSON",
            anchor_text="Client:",
            spatial_relationship="same_line_right",
            value_pattern=None,
            sample_bbox=[150.0, 120.0, 450.0, 140.0],
            line_count=1,
            skip_pattern="\\(\\d+\\)",
        )
        assert fm.field_type == "PERSON"
        assert fm.anchor_text == "Client:"
        assert fm.spatial_relationship == "same_line_right"
        assert fm.line_count == 1
        assert fm.skip_pattern == "\\(\\d+\\)"

    def test_defaults(self):
        fm = FieldMapping(
            field_type="PERSON",
            anchor_text="Name:",
            spatial_relationship="same_line_right",
        )
        assert fm.value_pattern is None
        assert fm.sample_bbox == []
        assert fm.line_count == 1
        assert fm.skip_pattern is None


# ---------------------------------------------------------------------------
# DocumentSchema layout fields
# ---------------------------------------------------------------------------

class TestDocumentSchemaLayout:

    def test_default_layout_type_is_variable(self):
        schema = DocumentSchema(
            document_type="financial_statement",
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
        assert schema.layout_type == "variable"
        assert schema.layout_field_map is None
        assert schema.layout_confidence == 0.0

    def test_to_dict_includes_layout_fields(self):
        fm = FieldMapping(
            field_type="PERSON",
            anchor_text="Client:",
            spatial_relationship="same_line_right",
        )
        schema = DocumentSchema(
            document_type="financial_statement",
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
            layout_type="fixed",
            layout_field_map=[fm],
            layout_confidence=0.95,
        )
        d = schema.to_dict()
        assert d["layout_type"] == "fixed"
        assert d["layout_confidence"] == 0.95
        assert len(d["layout_field_map"]) == 1
        assert d["layout_field_map"][0]["field_type"] == "PERSON"
        assert d["layout_field_map"][0]["anchor_text"] == "Client:"

    def test_to_dict_variable_layout_null_field_map(self):
        schema = DocumentSchema(
            document_type="correspondence",
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
        d = schema.to_dict()
        assert d["layout_type"] == "variable"
        assert d["layout_field_map"] is None
        assert d["layout_confidence"] == 0.0

    def test_roundtrip_fixed_layout(self):
        fm = FieldMapping(
            field_type="GOVERNMENT_ID",
            anchor_text="Tax No",
            spatial_relationship="line_below",
            value_pattern="\\d{3}-\\d{2}-\\d{4}",
            sample_bbox=[400.0, 85.0, 520.0, 100.0],
            line_count=1,
        )
        schema = DocumentSchema(
            document_type="financial_statement",
            document_subtype="accounting_statement",
            issuing_entity="ACME",
            field_map=[],
            people=[],
            organizations=["ACME"],
            date_contexts=[],
            tables=[],
            suppression_hints=[],
            extraction_notes="",
            schema_confidence=0.9,
            detected_by="llm",
            layout_type="fixed",
            layout_field_map=[fm],
            layout_confidence=0.95,
        )
        d = schema.to_dict()
        restored = DocumentSchema.from_dict(d)
        assert restored.layout_type == "fixed"
        assert restored.layout_confidence == 0.95
        assert restored.layout_field_map is not None
        assert len(restored.layout_field_map) == 1
        assert restored.layout_field_map[0].field_type == "GOVERNMENT_ID"
        assert restored.layout_field_map[0].anchor_text == "Tax No"
        assert restored.layout_field_map[0].value_pattern == "\\d{3}-\\d{2}-\\d{4}"
        assert restored.layout_field_map[0].sample_bbox == [400.0, 85.0, 520.0, 100.0]

    def test_roundtrip_variable_layout(self):
        schema = DocumentSchema(
            document_type="correspondence",
            document_subtype=None,
            issuing_entity=None,
            field_map=[],
            people=[],
            organizations=[],
            date_contexts=[],
            tables=[],
            suppression_hints=[],
            extraction_notes="",
            schema_confidence=0.7,
            detected_by="llm",
        )
        d = schema.to_dict()
        restored = DocumentSchema.from_dict(d)
        assert restored.layout_type == "variable"
        assert restored.layout_field_map is None
        assert restored.layout_confidence == 0.0


# ---------------------------------------------------------------------------
# _parse_response() — layout parsing
# ---------------------------------------------------------------------------

class TestParseResponseLayout:

    def test_fixed_layout_with_field_map(self):
        data = _minimal_data(
            layout_type="fixed",
            layout_confidence=0.95,
            layout_field_map=SAMPLE_FIELD_MAP,
        )
        schema = _parse(data)
        assert schema.layout_type == "fixed"
        assert schema.layout_confidence == 0.95
        assert schema.layout_field_map is not None
        assert len(schema.layout_field_map) == 3
        assert schema.layout_field_map[0].field_type == "PERSON"
        assert schema.layout_field_map[0].anchor_text == "Client:"
        assert schema.layout_field_map[1].field_type == "GOVERNMENT_ID"
        assert schema.layout_field_map[1].value_pattern == "\\d{3}-\\d{2}-\\d{4}"
        assert schema.layout_field_map[2].spatial_relationship == "lines_below_4"
        assert schema.layout_field_map[2].line_count == 4

    def test_variable_layout_no_field_map(self):
        data = _minimal_data(
            layout_type="variable",
            layout_confidence=0.1,
        )
        schema = _parse(data)
        assert schema.layout_type == "variable"
        assert schema.layout_field_map is None

    def test_default_layout_when_not_specified(self):
        data = _minimal_data()
        schema = _parse(data)
        assert schema.layout_type == "variable"
        assert schema.layout_field_map is None
        assert schema.layout_confidence == 0.0

    def test_fixed_without_field_map_downgrades_to_variable(self):
        """Safety: if LLM says fixed but provides no field map, downgrade."""
        data = _minimal_data(
            layout_type="fixed",
            layout_confidence=0.9,
            layout_field_map=None,
        )
        schema = _parse(data)
        assert schema.layout_type == "variable"
        assert schema.layout_confidence == 0.0

    def test_fixed_with_empty_field_map_downgrades(self):
        data = _minimal_data(
            layout_type="fixed",
            layout_confidence=0.9,
            layout_field_map=[],
        )
        schema = _parse(data)
        assert schema.layout_type == "variable"
        assert schema.layout_field_map is None

    def test_template_with_drift_layout(self):
        data = _minimal_data(
            layout_type="template_with_drift",
            layout_confidence=0.8,
            layout_field_map=SAMPLE_FIELD_MAP[:1],
        )
        schema = _parse(data)
        assert schema.layout_type == "template_with_drift"
        assert schema.layout_confidence == 0.8
        assert schema.layout_field_map is not None
        assert len(schema.layout_field_map) == 1

    def test_invalid_layout_type_defaults_to_variable(self):
        data = _minimal_data(
            layout_type="unknown_type",
            layout_confidence=0.5,
            layout_field_map=SAMPLE_FIELD_MAP,
        )
        schema = _parse(data)
        assert schema.layout_type == "variable"

    def test_bad_field_map_items_skipped(self):
        """Non-dict items in layout_field_map are skipped."""
        data = _minimal_data(
            layout_type="fixed",
            layout_confidence=0.9,
            layout_field_map=[
                SAMPLE_FIELD_MAP[0],
                "not a dict",
                42,
                SAMPLE_FIELD_MAP[1],
            ],
        )
        schema = _parse(data)
        assert schema.layout_type == "fixed"
        assert schema.layout_field_map is not None
        assert len(schema.layout_field_map) == 2

    def test_bad_bbox_handled_gracefully(self):
        data = _minimal_data(
            layout_type="fixed",
            layout_confidence=0.9,
            layout_field_map=[{
                "field_type": "PERSON",
                "anchor_text": "Name:",
                "spatial_relationship": "same_line_right",
                "sample_bbox": "not a list",
            }],
        )
        schema = _parse(data)
        assert schema.layout_type == "fixed"
        assert schema.layout_field_map is not None
        assert schema.layout_field_map[0].sample_bbox == []

    def test_layout_confidence_clamped(self):
        data = _minimal_data(
            layout_type="fixed",
            layout_confidence=5.0,
            layout_field_map=SAMPLE_FIELD_MAP[:1],
        )
        schema = _parse(data)
        assert schema.layout_confidence == 1.0


# ---------------------------------------------------------------------------
# _parse_layout_field_map static method
# ---------------------------------------------------------------------------

class TestParseLayoutFieldMap:

    def test_none_input(self):
        assert DocumentSchema._parse_layout_field_map(None) is None

    def test_empty_list(self):
        assert DocumentSchema._parse_layout_field_map([]) is None

    def test_not_a_list(self):
        assert DocumentSchema._parse_layout_field_map("bad") is None

    def test_valid_input(self):
        result = DocumentSchema._parse_layout_field_map(SAMPLE_FIELD_MAP)
        assert result is not None
        assert len(result) == 3
        assert all(isinstance(fm, FieldMapping) for fm in result)

    def test_all_non_dict_items_returns_none(self):
        assert DocumentSchema._parse_layout_field_map(["a", "b"]) is None

    def test_bbox_truncated_to_4(self):
        result = DocumentSchema._parse_layout_field_map([{
            "field_type": "PERSON",
            "anchor_text": "Name:",
            "spatial_relationship": "same_line_right",
            "sample_bbox": [1, 2, 3, 4, 5, 6],
        }])
        assert result is not None
        assert result[0].sample_bbox == [1.0, 2.0, 3.0, 4.0]


# ---------------------------------------------------------------------------
# Prompt templates include layout fields
# ---------------------------------------------------------------------------

class TestPromptsIncludeLayout:

    def test_understand_document_mentions_layout(self):
        from app.llm.prompts import UNDERSTAND_DOCUMENT
        assert "layout_type" in UNDERSTAND_DOCUMENT
        assert "layout_field_map" in UNDERSTAND_DOCUMENT
        assert "layout_confidence" in UNDERSTAND_DOCUMENT
        assert "fixed" in UNDERSTAND_DOCUMENT
        assert "template_with_drift" in UNDERSTAND_DOCUMENT

    def test_understand_multi_page_mentions_layout(self):
        from app.llm.prompts import UNDERSTAND_MULTI_PAGE_DOCUMENT
        assert "layout_type" in UNDERSTAND_MULTI_PAGE_DOCUMENT
        assert "layout_field_map" in UNDERSTAND_MULTI_PAGE_DOCUMENT
        assert "layout_confidence" in UNDERSTAND_MULTI_PAGE_DOCUMENT
