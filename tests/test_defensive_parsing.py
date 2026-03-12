"""Tests for defensive LLM response parsing (critical bugfix).

The LLM returns unpredictable JSON shapes — strings where dicts are expected,
missing keys, flat lists, etc.  _parse_response() must NEVER crash on any of
these.  A partial schema is always better than None.

8 tests:
- date_contexts as strings → parsed without crash
- date_contexts as dicts → parsed normally
- date_contexts as mixed → both parsed
- field_map with string entry → wrapped gracefully
- people with string entry → wrapped gracefully
- Complete garbage JSON → returns partial schema (not None)
- Template still parsed when other fields fail
- "Blunt NH828286D" → split into name + NI number
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from app.structure.llm_document_understanding import (
    LLMDocumentUnderstanding,
    _safe_parse_list,
    _parse_table,
)
from app.structure.document_schema import (
    DateContext,
    DocumentSchema,
    FieldContext,
    PersonContext,
)
from app.pipeline.record_mapper import detection_to_pii_record


# ---------------------------------------------------------------------------
# Helper: build a response JSON and parse it
# ---------------------------------------------------------------------------

def _parse(data: dict) -> DocumentSchema:
    """Shortcut: serialize data to JSON and parse via _parse_response."""
    du = LLMDocumentUnderstanding(db_session=None)
    return du._parse_response(json.dumps(data))


def _minimal_data(**overrides) -> dict:
    """Minimal valid LLM response with overrides."""
    base = {
        "document_type": "pension_statement",
        "issuing_entity": "Mercer",
        "field_map": [],
        "people": [],
        "organizations": ["Mercer"],
        "date_contexts": [],
        "tables": [],
        "suppression_hints": [],
        "extraction_notes": "",
        "schema_confidence": 0.8,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Tests: date_contexts
# ---------------------------------------------------------------------------

class TestDateContextsParsing:

    def test_date_contexts_as_strings(self):
        """LLM returns date_contexts as plain strings — should not crash."""
        data = _minimal_data(date_contexts=["2024-01-15", "Statement Date", "DOB"])
        schema = _parse(data)
        assert len(schema.date_contexts) == 3
        assert schema.date_contexts[0].value == "2024-01-15"
        assert schema.date_contexts[0].semantic_type == "unknown"
        assert schema.date_contexts[0].is_pii is False

    def test_date_contexts_as_dicts(self):
        """Normal dict format — parsed as before."""
        data = _minimal_data(date_contexts=[
            {"value": "1980-03-22", "semantic_type": "date_of_birth", "is_pii": True},
            {"value": "2024-01-01", "semantic_type": "statement_date", "is_pii": False},
        ])
        schema = _parse(data)
        assert len(schema.date_contexts) == 2
        assert schema.date_contexts[0].value == "1980-03-22"
        assert schema.date_contexts[0].is_pii is True

    def test_date_contexts_mixed(self):
        """Mix of strings and dicts — both parsed."""
        data = _minimal_data(date_contexts=[
            "2024-01-15",
            {"value": "1980-03-22", "semantic_type": "date_of_birth", "is_pii": True},
            42,  # garbage — should be skipped
        ])
        schema = _parse(data)
        assert len(schema.date_contexts) == 2
        assert schema.date_contexts[0].value == "2024-01-15"
        assert schema.date_contexts[1].value == "1980-03-22"


# ---------------------------------------------------------------------------
# Tests: field_map and people
# ---------------------------------------------------------------------------

class TestFieldMapPeopleParsing:

    def test_field_map_string_entries(self):
        """LLM returns field labels as plain strings."""
        data = _minimal_data(field_map=[
            "Name",
            "Address",
            {"label": "NI Number", "semantic_type": "government_id", "is_pii": True},
        ])
        schema = _parse(data)
        assert len(schema.field_map) == 3
        assert schema.field_map[0].label == "Name"
        assert schema.field_map[0].semantic_type == "unknown"
        assert schema.field_map[2].label == "NI Number"
        assert schema.field_map[2].is_pii is True

    def test_people_string_entries(self):
        """LLM returns people as plain name strings."""
        data = _minimal_data(people=[
            "John Smith",
            {"name": "Jane Doe", "role": "beneficiary", "is_pii_subject": True},
        ])
        schema = _parse(data)
        assert len(schema.people) == 2
        assert schema.people[0].name == "John Smith"
        assert schema.people[0].role == "unknown"
        assert schema.people[1].name == "Jane Doe"
        assert schema.people[1].is_pii_subject is True


# ---------------------------------------------------------------------------
# Tests: garbage and partial schemas
# ---------------------------------------------------------------------------

class TestPartialSchemaRecovery:

    def test_garbage_fields_returns_partial_schema(self):
        """Unrecognized/malformed fields → partial schema, never None."""
        data = {
            "document_type": "unknown_doc",
            "field_map": "not a list",      # wrong type
            "people": 42,                    # wrong type
            "date_contexts": None,           # null
            "organizations": "Mercer Ltd",   # string instead of list
            "tables": True,                  # wrong type
        }
        schema = _parse(data)
        assert schema is not None
        assert schema.document_type == "unknown_doc"
        assert schema.field_map == []
        assert schema.people == []
        assert schema.date_contexts == []
        assert schema.tables == []

    def test_template_parsed_when_other_fields_fail(self):
        """Template is parsed even when field_map/people/date_contexts are garbage."""
        data = {
            "document_type": "pension_transfer",
            "field_map": "not a list",
            "people": None,
            "date_contexts": 123,
            "template": {
                "template_name": "pension_3page",
                "pages_per_instance": 3,
                "total_instances_estimate": 50,
                "page_roles": [
                    {"page_offset": 0, "role": "identity", "pii_fields_expected": ["PERSON", "NI_NUMBER"]},
                    {"page_offset": 1, "role": "details", "pii_fields_expected": ["LOCATION"]},
                ],
                "identity_page_offset": 0,
            },
        }
        schema = _parse(data)
        assert schema is not None
        assert schema.template is not None
        assert schema.template.pages_per_instance == 3
        assert schema.template.total_instances_estimate == 50
        assert len(schema.template.page_roles) == 2


# ---------------------------------------------------------------------------
# Tests: NI number splitting in detection_to_pii_record
# ---------------------------------------------------------------------------

class TestNINumberSplit:

    @staticmethod
    def _make_det(text: str, entity_type: str = "PERSON", score: float = 0.85):
        from app.pii.presidio_engine import DetectionResult
        from app.readers.base import ExtractedBlock

        block = ExtractedBlock(
            text=text,
            page_or_sheet=103,
            source_path="/tmp/test.pdf",
            file_type="pdf",
        )
        return DetectionResult(
            entity_type=entity_type,
            start=0,
            end=len(block.text),
            score=score,
            block=block,
            pattern_used="",
            geography="UK",
            regulatory_framework="gdpr",
        )

    def test_blunt_nh828286d_split(self):
        """'Blunt NH828286D' → raw_name='Blunt', raw_government_id='NH828286D'."""
        det = self._make_det("Blunt                     NH828286D")
        rec = detection_to_pii_record(det, "doc-1")
        assert rec.raw_name == "Blunt"
        assert rec.raw_government_id == "NH828286D"

    def test_name_without_ni_unchanged(self):
        """'Mrs A A Blunt' with no NI number — name unchanged, no gov ID."""
        det = self._make_det("Mrs A A Blunt")
        rec = detection_to_pii_record(det, "doc-1")
        assert rec.raw_name == "Mrs A A Blunt"
        assert rec.raw_government_id is None

    def test_ni_only_no_name(self):
        """If text is ONLY an NI number with no name prefix, raw_name stays as-is."""
        det = self._make_det("NH828286D", score=0.6)
        rec = detection_to_pii_record(det, "doc-1")
        # Empty name before the NI match → original text stays as raw_name
        assert rec.raw_name == "NH828286D"
        assert rec.raw_government_id == "NH828286D"


# ---------------------------------------------------------------------------
# Tests: _safe_parse_list helper
# ---------------------------------------------------------------------------

class TestSafeParseList:

    def test_non_list_returns_empty(self):
        assert _safe_parse_list("not a list", lambda d: d) == []
        assert _safe_parse_list(None, lambda d: d) == []
        assert _safe_parse_list(42, lambda d: d) == []

    def test_mixed_items(self):
        results = _safe_parse_list(
            [{"v": 1}, "hello", 42, {"v": 2}],
            parser_func=lambda d: d["v"],
            fallback_func=lambda s: s.upper(),
        )
        assert results == [1, "HELLO", 2]

    def test_malformed_dict_skipped(self):
        """Dict that causes parser to raise → skipped."""
        def bad_parser(d):
            return d["required_key"]  # will KeyError

        results = _safe_parse_list(
            [{"other": 1}, {"required_key": 42}],
            parser_func=bad_parser,
        )
        assert results == [42]
