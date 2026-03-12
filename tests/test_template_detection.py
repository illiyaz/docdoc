"""Tests for Step 17 — template detection, composite records, and multi-page grouping."""
from __future__ import annotations

import pytest
from dataclasses import dataclass

from app.structure.document_schema import (
    DocumentSchema,
    DocumentTemplate,
    FieldContext,
    PageRole,
)
from app.core.constants import PROTOCOL_LLM_CONFIG, DEFAULT_LLM_PAGES_TO_READ
from app.rra.entity_resolver import PIIRecord


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@dataclass
class _FakeBlock:
    text: str
    page_or_sheet: int | str
    source_path: str = ""
    file_type: str = "pdf"


@dataclass
class _FakeDetection:
    entity_type: str
    start: int
    end: int
    score: float
    block: _FakeBlock


def _make_detection(entity_type: str, text: str, page: int, score: float = 0.9) -> _FakeDetection:
    block = _FakeBlock(text=text, page_or_sheet=page)
    return _FakeDetection(
        entity_type=entity_type,
        start=0,
        end=len(text),
        score=score,
        block=block,
    )


def _make_schema(template: DocumentTemplate | None = None) -> DocumentSchema:
    return DocumentSchema(
        document_type="financial_statement",
        document_subtype="pension_transfer",
        issuing_entity="Test Corp",
        field_map=[],
        people=[],
        organizations=[],
        date_contexts=[],
        tables=[],
        suppression_hints=[],
        extraction_notes="",
        schema_confidence=0.85,
        detected_by="llm",
        template=template,
    )


# ---------------------------------------------------------------------------
# DocumentTemplate + PageRole
# ---------------------------------------------------------------------------

class TestDocumentTemplate:
    def test_get_instance_pages_basic(self):
        tpl = DocumentTemplate(
            template_name="pension", pages_per_instance=3,
            total_instances_estimate=2,
        )
        result = tpl.get_instance_pages(6)
        assert result == [[0, 1, 2], [3, 4, 5]]

    def test_get_instance_pages_partial_last(self):
        tpl = DocumentTemplate(
            template_name="report", pages_per_instance=3,
            total_instances_estimate=2,
        )
        result = tpl.get_instance_pages(7)
        assert result == [[0, 1, 2], [3, 4, 5], [6]]

    def test_get_instance_pages_single_page(self):
        tpl = DocumentTemplate(
            template_name="form", pages_per_instance=1,
            total_instances_estimate=3,
        )
        result = tpl.get_instance_pages(3)
        assert result == [[0], [1], [2]]

    def test_get_instance_pages_empty(self):
        tpl = DocumentTemplate(
            template_name="empty", pages_per_instance=3,
            total_instances_estimate=0,
        )
        result = tpl.get_instance_pages(0)
        assert result == []

    def test_find_instance_boundaries_basic(self):
        """Marker-based boundary detection finds correct variable-length instances."""
        tpl = DocumentTemplate(
            template_name="pension", pages_per_instance=3,
            total_instances_estimate=3,
            instance_marker="SUMMARY OF DETAILS IN RESPECT OF",
        )
        page_texts = {
            0: "STATEMENT\n1. SUMMARY OF DETAILS IN RESPECT OF:\nMr A Smith",
            1: "2. MEMBER DETAILS\nMr A Smith",
            2: "3. BENEFITS",
            3: "STATEMENT\n1. SUMMARY OF DETAILS IN RESPECT OF:\nMrs B Jones",
            4: "2. MEMBER DETAILS\nMrs B Jones",
            5: "3. BENEFITS",
            6: "4. EXTRA PAGE FOR MRS B JONES",  # variable length
            7: "STATEMENT\n1. SUMMARY OF DETAILS IN RESPECT OF:\nMr C Brown",
            8: "2. MEMBER DETAILS\nMr C Brown",
        }
        instances = tpl.find_instance_boundaries(page_texts)
        assert len(instances) == 3
        assert instances[0] == [0, 1, 2]    # Mr A Smith: 3 pages
        assert instances[1] == [3, 4, 5, 6]  # Mrs B Jones: 4 pages (variable!)
        assert instances[2] == [7, 8]         # Mr C Brown: 2 pages (end of doc)

    def test_find_instance_boundaries_no_marker(self):
        """Without a marker, falls back to get_instance_pages()."""
        tpl = DocumentTemplate(
            template_name="pension", pages_per_instance=3,
            total_instances_estimate=2,
        )
        page_texts = {0: "page 0", 1: "page 1", 2: "page 2", 3: "page 3", 4: "page 4", 5: "page 5"}
        instances = tpl.find_instance_boundaries(page_texts)
        assert instances == [[0, 1, 2], [3, 4, 5]]

    def test_find_instance_boundaries_marker_not_found(self):
        """Marker set but not found in any page — falls back to fixed stride."""
        tpl = DocumentTemplate(
            template_name="pension", pages_per_instance=3,
            total_instances_estimate=2,
            instance_marker="NONEXISTENT MARKER",
        )
        page_texts = {0: "page 0", 1: "page 1", 2: "page 2", 3: "page 3", 4: "page 4", 5: "page 5"}
        instances = tpl.find_instance_boundaries(page_texts)
        assert instances == [[0, 1, 2], [3, 4, 5]]

    def test_find_instance_boundaries_case_insensitive(self):
        """Marker search is case-insensitive."""
        tpl = DocumentTemplate(
            template_name="t", pages_per_instance=2,
            total_instances_estimate=2,
            instance_marker="NEW RECORD",
        )
        page_texts = {
            0: "new record\nPerson A",
            1: "details A",
            2: "New Record\nPerson B",
            3: "details B",
        }
        instances = tpl.find_instance_boundaries(page_texts)
        assert len(instances) == 2
        assert instances[0] == [0, 1]
        assert instances[1] == [2, 3]

    def test_page_role_creation(self):
        pr = PageRole(
            page_offset=1,
            role="member_details",
            pii_fields_expected=["PERSON", "LOCATION", "DATE_OF_BIRTH"],
            is_identity_page=True,
        )
        assert pr.page_offset == 1
        assert pr.is_identity_page is True
        assert "PERSON" in pr.pii_fields_expected

    def test_template_with_page_roles(self):
        tpl = DocumentTemplate(
            template_name="pension_transfer",
            pages_per_instance=3,
            total_instances_estimate=2,
            page_roles=[
                PageRole(0, "financial_summary", ["PERSON"], True),
                PageRole(1, "member_details", ["LOCATION", "DATE_OF_BIRTH"], False),
                PageRole(2, "benefit_details", [], False),
            ],
            identity_page_offset=0,
        )
        assert len(tpl.page_roles) == 3
        assert tpl.page_roles[0].is_identity_page is True
        assert tpl.identity_page_offset == 0


# ---------------------------------------------------------------------------
# DocumentSchema serialization with template
# ---------------------------------------------------------------------------

class TestSchemaTemplateRoundTrip:
    def test_to_dict_with_template(self):
        tpl = DocumentTemplate(
            template_name="pension",
            pages_per_instance=3,
            total_instances_estimate=2,
            page_roles=[
                PageRole(0, "summary", ["PERSON"], True),
                PageRole(1, "details", ["LOCATION"], False),
            ],
            identity_page_offset=0,
        )
        schema = _make_schema(template=tpl)
        d = schema.to_dict()
        assert d["template"] is not None
        assert d["template"]["template_name"] == "pension"
        assert d["template"]["pages_per_instance"] == 3
        assert len(d["template"]["page_roles"]) == 2

    def test_to_dict_without_template(self):
        schema = _make_schema(template=None)
        d = schema.to_dict()
        assert d["template"] is None

    def test_from_dict_with_template(self):
        d = {
            "document_type": "financial_statement",
            "template": {
                "template_name": "pension",
                "pages_per_instance": 3,
                "total_instances_estimate": 2,
                "identity_page_offset": 0,
                "page_roles": [
                    {
                        "page_offset": 0,
                        "role": "summary",
                        "pii_fields_expected": ["PERSON"],
                        "is_identity_page": True,
                    },
                ],
            },
        }
        schema = DocumentSchema.from_dict(d)
        assert schema.template is not None
        assert schema.template.template_name == "pension"
        assert schema.template.pages_per_instance == 3
        assert len(schema.template.page_roles) == 1
        assert schema.template.page_roles[0].is_identity_page is True

    def test_from_dict_without_template(self):
        d = {"document_type": "unknown"}
        schema = DocumentSchema.from_dict(d)
        assert schema.template is None

    def test_from_dict_with_null_fields(self):
        """LLM may return null for optional template fields — should not crash."""
        d = {
            "document_type": "financial_statement",
            "template": {
                "template_name": "pension",
                "pages_per_instance": 3,
                "total_instances_estimate": None,  # LLM returned null
                "identity_page_offset": None,
                "page_roles": [
                    {
                        "page_offset": 0,
                        "role": "summary",
                        "pii_fields_expected": None,
                        "is_identity_page": True,
                    },
                ],
            },
        }
        schema = DocumentSchema.from_dict(d)
        assert schema.template is not None
        assert schema.template.pages_per_instance == 3
        assert schema.template.total_instances_estimate == 1  # default
        assert schema.template.identity_page_offset == 0  # default
        assert schema.template.page_roles[0].pii_fields_expected == []

    def test_roundtrip(self):
        tpl = DocumentTemplate(
            template_name="pension", pages_per_instance=3,
            total_instances_estimate=2,
            page_roles=[PageRole(0, "summary", ["PERSON"], True)],
            instance_marker="IN RESPECT OF:",
        )
        schema = _make_schema(template=tpl)
        d = schema.to_dict()
        assert d["template"]["instance_marker"] == "IN RESPECT OF:"
        schema2 = DocumentSchema.from_dict(d)
        assert schema2.template is not None
        assert schema2.template.template_name == schema.template.template_name
        assert schema2.template.pages_per_instance == schema.template.pages_per_instance
        assert schema2.template.instance_marker == "IN RESPECT OF:"


# ---------------------------------------------------------------------------
# PROTOCOL_LLM_CONFIG
# ---------------------------------------------------------------------------

class TestProtocolLLMConfig:
    def test_all_protocols_present(self):
        expected = {"hipaa", "gdpr", "ccpa", "hitech", "ferpa", "state_breach_generic", "bipa", "dpdpa"}
        assert set(PROTOCOL_LLM_CONFIG.keys()) == expected

    def test_each_protocol_has_required_keys(self):
        for protocol, config in PROTOCOL_LLM_CONFIG.items():
            assert "llm_pages_to_read" in config, f"{protocol} missing llm_pages_to_read"
            assert "expect_multi_page_records" in config, f"{protocol} missing expect_multi_page_records"
            assert isinstance(config["llm_pages_to_read"], int)
            assert config["llm_pages_to_read"] >= 1

    def test_default_pages_to_read(self):
        assert DEFAULT_LLM_PAGES_TO_READ == 3


# ---------------------------------------------------------------------------
# PIIRecord raw_government_id field
# ---------------------------------------------------------------------------

class TestPIIRecordGovId:
    def test_pii_record_has_raw_government_id(self):
        rec = PIIRecord(
            record_id="r1", entity_type="PERSON", normalized_value="test",
            raw_government_id="NE724362D",
        )
        assert rec.raw_government_id == "NE724362D"

    def test_pii_record_default_none(self):
        rec = PIIRecord(record_id="r1", entity_type="PERSON", normalized_value="test")
        assert rec.raw_government_id is None


# ---------------------------------------------------------------------------
# build_composite_record
# ---------------------------------------------------------------------------

class TestBuildCompositeRecord:
    def test_merges_multiple_types(self):
        from app.pipeline.record_mapper import build_composite_record

        dets = [
            _make_detection("PERSON", "K P Acheampong", page=0),
            _make_detection("LOCATION", "85 Waltings Gardens London NW2 3UD", page=1),
            _make_detection("DATE_OF_BIRTH_DMY", "10-Aug-1959", page=1, score=0.95),
        ]
        rec = build_composite_record(dets, "doc-1")

        assert rec.raw_name == "K P Acheampong"
        assert rec.raw_address == {"raw": "85 Waltings Gardens London NW2 3UD"}
        assert rec.raw_dob == "10-Aug-1959"
        assert rec.source_document_id == "doc-1"

    def test_takes_highest_confidence(self):
        from app.pipeline.record_mapper import build_composite_record

        dets = [
            _make_detection("PERSON", "K Acheampong", page=0, score=0.7),
            _make_detection("PERSON", "K P Acheampong", page=1, score=0.95),
        ]
        rec = build_composite_record(dets, "doc-1")
        assert rec.raw_name == "K P Acheampong"

    def test_handles_government_id(self):
        from app.pipeline.record_mapper import build_composite_record

        dets = [
            _make_detection("PERSON", "K P Acheampong", page=0),
            _make_detection("UK_NINO", "NE724362D", page=1, score=0.95),
        ]
        rec = build_composite_record(dets, "doc-1")
        assert rec.raw_name == "K P Acheampong"
        assert rec.raw_government_id == "NE724362D"

    def test_empty_detections(self):
        from app.pipeline.record_mapper import build_composite_record

        rec = build_composite_record([], "doc-1")
        assert rec.entity_type == "UNKNOWN"
        assert rec.raw_name is None

    def test_single_detection(self):
        from app.pipeline.record_mapper import build_composite_record

        dets = [_make_detection("PERSON", "John Smith", page=0)]
        rec = build_composite_record(dets, "doc-1")
        assert rec.raw_name == "John Smith"
        assert rec.raw_email is None
        assert rec.raw_phone is None


# ---------------------------------------------------------------------------
# extract_with_template
# ---------------------------------------------------------------------------

class TestExtractWithTemplate:
    def test_groups_by_template_instance(self):
        from app.pipeline.record_mapper import extract_with_template

        tpl = DocumentTemplate(
            template_name="pension", pages_per_instance=3,
            total_instances_estimate=2,
        )
        schema = _make_schema(template=tpl)

        # 2 individuals across 6 pages
        dets = [
            _make_detection("PERSON", "K P Acheampong", page=0),
            _make_detection("LOCATION", "85 Waltings Gardens", page=1),
            _make_detection("DATE_OF_BIRTH_DMY", "10-Aug-1959", page=1),
            _make_detection("PERSON", "M S Alcock", page=3),
            _make_detection("LOCATION", "3 Whitworth Road", page=4),
            _make_detection("DATE_OF_BIRTH_DMY", "29-Aug-1960", page=4),
        ]

        records = extract_with_template(dets, schema, "doc-1", total_pages=6)

        assert len(records) == 2
        assert records[0].raw_name == "K P Acheampong"
        assert records[0].raw_address == {"raw": "85 Waltings Gardens"}
        assert records[0].raw_dob == "10-Aug-1959"
        assert records[1].raw_name == "M S Alcock"
        assert records[1].raw_address == {"raw": "3 Whitworth Road"}
        assert records[1].raw_dob == "29-Aug-1960"

    def test_no_template_fallback(self):
        from app.pipeline.record_mapper import extract_with_template

        schema = _make_schema(template=None)
        dets = [
            _make_detection("PERSON", "John Smith", page=0),
            _make_detection("EMAIL_ADDRESS", "john@test.com", page=0),
        ]

        records = extract_with_template(dets, schema, "doc-1", total_pages=1)

        # Without template, falls back to per-detection records
        assert len(records) == 2
        assert any(r.raw_name == "John Smith" for r in records)
        assert any(r.raw_email == "john@test.com" for r in records)

    def test_empty_instance_pages_skipped(self):
        from app.pipeline.record_mapper import extract_with_template

        tpl = DocumentTemplate(
            template_name="pension", pages_per_instance=3,
            total_instances_estimate=2,
        )
        schema = _make_schema(template=tpl)

        # Only detections on pages 0-2, none on pages 3-5
        dets = [
            _make_detection("PERSON", "K P Acheampong", page=0),
        ]

        records = extract_with_template(dets, schema, "doc-1", total_pages=6)

        # Only 1 composite record (instance 2 has no detections → skipped)
        assert len(records) == 1
        assert records[0].raw_name == "K P Acheampong"


# ---------------------------------------------------------------------------
# LLM Document Understanding multi-page
# ---------------------------------------------------------------------------

class TestLLMMultiPageConfig:
    def test_resolve_pages_default(self):
        from app.structure.llm_document_understanding import LLMDocumentUnderstanding

        du = LLMDocumentUnderstanding(db_session=None)
        assert du._resolve_pages_to_read("unknown", None) == 3

    def test_resolve_pages_protocol_default(self):
        from app.structure.llm_document_understanding import LLMDocumentUnderstanding

        du = LLMDocumentUnderstanding(db_session=None)
        assert du._resolve_pages_to_read("hipaa", None) == 5
        assert du._resolve_pages_to_read("bipa", None) == 2

    def test_resolve_pages_config_override(self):
        from app.structure.llm_document_understanding import LLMDocumentUnderstanding

        du = LLMDocumentUnderstanding(db_session=None)
        config = {"llm_pages_to_read": 10}
        assert du._resolve_pages_to_read("hipaa", config) == 10

    def test_resolve_pages_invalid_config_fallback(self):
        from app.structure.llm_document_understanding import LLMDocumentUnderstanding

        du = LLMDocumentUnderstanding(db_session=None)
        config = {"llm_pages_to_read": "invalid"}
        # Falls back to protocol default
        assert du._resolve_pages_to_read("hipaa", config) == 5


# ---------------------------------------------------------------------------
# Government ID matching in build_confidence
# ---------------------------------------------------------------------------

class TestGovIdMatching:
    def test_raw_government_id_match(self):
        from app.rra.entity_resolver import build_confidence

        r1 = PIIRecord(
            record_id="r1", entity_type="PERSON",
            normalized_value="K Acheampong",
            raw_name="K Acheampong",
            raw_government_id="NE724362D",
        )
        r2 = PIIRecord(
            record_id="r2", entity_type="PERSON",
            normalized_value="K P Acheampong",
            raw_name="K P Acheampong",
            raw_government_id="NE724362D",
        )
        conf = build_confidence(r1, r2)
        # Should get gov ID match (+0.50) + name match (+0.10)
        assert conf >= 0.50

    def test_raw_government_id_no_match(self):
        from app.rra.entity_resolver import build_confidence

        r1 = PIIRecord(
            record_id="r1", entity_type="PERSON",
            normalized_value="K Acheampong",
            raw_government_id="NE724362D",
        )
        r2 = PIIRecord(
            record_id="r2", entity_type="PERSON",
            normalized_value="M Alcock",
            raw_government_id="WK393925C",
        )
        conf = build_confidence(r1, r2)
        assert conf < 0.50  # Different gov IDs — no match
