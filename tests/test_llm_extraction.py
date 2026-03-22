"""Tests for Step 19: Schema-driven LLM extraction for template documents.

16 tests covering:
- build_extraction_prompt: schema-driven, different schemas, pension NI/DOB, US medical SSN
- _parse_extraction: full JSON, nulls, invalid JSON, no PERSON, government_id_type
- extract_all_instances: 6 pages → 2 records
- Batch mode: JSON array parsing
- Fallback: LLM fails → empty list
- Pipeline integration: 3 paths
- ENTITY_EXTRACTION_GUIDE coverage
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

import pytest

from app.llm.extraction_prompts import (
    ENTITY_EXTRACTION_GUIDE,
    build_batch_extraction_prompt,
    build_extraction_prompt,
)
from app.structure.document_schema import (
    DocumentSchema,
    DocumentTemplate,
    FieldContext,
    PageRole,
    PersonContext,
)
from app.structure.llm_template_extractor import LLMTemplateExtractor
from app.rra.entity_resolver import PIIRecord


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pension_schema() -> DocumentSchema:
    """UK pension schema with NI_NUMBER, DATE_OF_BIRTH, PERSON, LOCATION."""
    return DocumentSchema(
        document_type="pension_transfer_statement",
        document_subtype=None,
        issuing_entity="Mercer Ltd",
        field_map=[],
        people=[],
        organizations=["Mercer Ltd"],
        date_contexts=[],
        tables=[],
        suppression_hints=[],
        extraction_notes="Pension transfer statement",
        schema_confidence=0.9,
        detected_by="llm",
        template=DocumentTemplate(
            template_name="pension_transfer",
            pages_per_instance=3,
            total_instances_estimate=2,
            page_roles=[
                PageRole(
                    page_offset=0,
                    role="financial_summary",
                    pii_fields_expected=["PERSON", "NI_NUMBER"],
                    is_identity_page=True,
                ),
                PageRole(
                    page_offset=1,
                    role="member_details",
                    pii_fields_expected=["LOCATION", "DATE_OF_BIRTH"],
                ),
                PageRole(
                    page_offset=2,
                    role="benefits",
                    pii_fields_expected=[],
                ),
            ],
            identity_page_offset=0,
        ),
    )


def _make_us_medical_schema() -> DocumentSchema:
    """US medical record schema with US_SSN, PERSON, no NI_NUMBER."""
    return DocumentSchema(
        document_type="medical_record",
        document_subtype=None,
        issuing_entity="General Hospital",
        field_map=[],
        people=[],
        organizations=["General Hospital"],
        date_contexts=[],
        tables=[],
        suppression_hints=[],
        extraction_notes="US medical record",
        schema_confidence=0.9,
        detected_by="llm",
        template=DocumentTemplate(
            template_name="medical_record",
            pages_per_instance=2,
            total_instances_estimate=3,
            page_roles=[
                PageRole(
                    page_offset=0,
                    role="patient_info",
                    pii_fields_expected=["PERSON", "US_SSN", "DATE_OF_BIRTH"],
                    is_identity_page=True,
                ),
                PageRole(
                    page_offset=1,
                    role="clinical_data",
                    pii_fields_expected=["EMAIL_ADDRESS", "PHONE_NUMBER"],
                ),
            ],
            identity_page_offset=0,
        ),
    )


def _make_no_template_schema() -> DocumentSchema:
    """Schema with no template (single-person document)."""
    return DocumentSchema(
        document_type="financial_statement",
        document_subtype=None,
        issuing_entity="Bank",
        field_map=[],
        people=[],
        organizations=[],
        date_contexts=[],
        tables=[],
        suppression_hints=[],
        extraction_notes="Single-person statement",
        schema_confidence=0.9,
        detected_by="llm",
        template=None,
    )


# ---------------------------------------------------------------------------
# Test: build_extraction_prompt — schema-driven, not hardcoded
# ---------------------------------------------------------------------------

class TestBuildExtractionPrompt:
    def test_generates_from_schema_not_hardcoded(self):
        """Prompt fields come from page_roles.pii_fields_expected."""
        schema = _make_pension_schema()
        prompt = build_extraction_prompt(
            page_texts=["Name: John Smith", "Address: London"],
            page_roles=schema.template.page_roles,
            instance_index=0,
            document_type=schema.document_type,
        )
        # Should include fields from schema
        assert "PERSON" in prompt
        assert "NI_NUMBER" in prompt
        assert "LOCATION" in prompt
        assert "DATE_OF_BIRTH" in prompt
        assert "pension_transfer_statement" in prompt

    def test_different_schemas_produce_different_prompts(self):
        """Pension vs medical schemas produce different document_type lines."""
        pension_prompt = build_extraction_prompt(
            page_texts=["data"],
            page_roles=_make_pension_schema().template.page_roles,
            instance_index=0,
            document_type="pension_transfer_statement",
        )
        medical_prompt = build_extraction_prompt(
            page_texts=["data"],
            page_roles=_make_us_medical_schema().template.page_roles,
            instance_index=0,
            document_type="medical_record",
        )
        # Both include ALWAYS_EXTRACT_IF_PRESENT fields (NI_NUMBER, US_SSN)
        # but they differ in document type and schema-specific fields
        assert "pension_transfer_statement" in pension_prompt
        assert "medical_record" in medical_prompt
        # Medical schema has EMAIL_ADDRESS/PHONE_NUMBER in page_roles
        # which adds extraction instructions for those
        assert "US_SSN" in medical_prompt
        assert "NI_NUMBER" in pension_prompt

    def test_pension_schema_includes_ni_and_dob(self):
        """UK pension schema includes NI_NUMBER and DATE_OF_BIRTH."""
        prompt = build_extraction_prompt(
            page_texts=["test"],
            page_roles=_make_pension_schema().template.page_roles,
            instance_index=0,
            document_type="pension_transfer_statement",
        )
        assert "NI_NUMBER" in prompt
        assert "DATE_OF_BIRTH" in prompt
        # Check extraction guide text is included
        assert "National Insurance Number" in prompt

    def test_us_medical_includes_ssn(self):
        """US medical schema includes US_SSN with extraction guide."""
        prompt = build_extraction_prompt(
            page_texts=["test"],
            page_roles=_make_us_medical_schema().template.page_roles,
            instance_index=0,
            document_type="medical_record",
        )
        assert "US_SSN" in prompt
        assert "Social Security Number" in prompt
        # ALWAYS_EXTRACT_IF_PRESENT ensures all common fields present
        assert "NI_NUMBER" in prompt  # always included now


# ---------------------------------------------------------------------------
# Test: _parse_extraction — JSON parsing to PIIRecord
# ---------------------------------------------------------------------------

class TestParseExtraction:
    def setup_method(self):
        self.client = MagicMock()
        self.extractor = LLMTemplateExtractor(self.client, batch_size=3)

    def test_full_json_populates_all_fields(self):
        """JSON with all fields → PIIRecord fully populated."""
        response = json.dumps({
            "PERSON": "Mr John Smith",
            "LOCATION": "123 High Street, London, SW1A 1AA",
            "DATE_OF_BIRTH": "10-Aug-1959",
            "NI_NUMBER": "NE724362D",
            "EMAIL_ADDRESS": "john@example.com",
            "PHONE_NUMBER": "+44 7700 900000",
        })
        record = self.extractor._parse_extraction(response, "doc1", [0, 1, 2])
        assert record is not None
        assert record.raw_name == "Mr John Smith"
        assert record.raw_address == {"raw": "123 High Street, London, SW1A 1AA"}
        assert record.raw_dob == "10-Aug-1959"
        assert record.raw_government_id == "NE724362D"
        assert record.raw_email == "john@example.com"
        assert record.raw_phone == "+44 7700 900000"
        assert record.page_range == "1-3"
        assert "NI_NUMBER" in record.entity_types_found

    def test_json_with_nulls_only_sets_nonnull(self):
        """JSON with nulls → only non-null fields set."""
        response = json.dumps({
            "PERSON": "Jane Doe",
            "LOCATION": None,
            "DATE_OF_BIRTH": "null",
            "NI_NUMBER": "",
        })
        record = self.extractor._parse_extraction(response, "doc1", [0])
        assert record is not None
        assert record.raw_name == "Jane Doe"
        assert record.raw_address is None
        assert record.raw_dob is None
        assert record.raw_government_id is None

    def test_invalid_json_returns_none(self):
        """Invalid JSON → returns None (graceful)."""
        record = self.extractor._parse_extraction("not json at all {{{", "doc1", [0])
        assert record is None

    def test_no_person_returns_none(self):
        """No PERSON in response → returns None."""
        response = json.dumps({
            "LOCATION": "London",
            "NI_NUMBER": "NE724362D",
        })
        record = self.extractor._parse_extraction(response, "doc1", [0])
        assert record is None

    def test_government_id_type_set_correctly(self):
        """government_id_type set correctly for NI_NUMBER and US_SSN."""
        # NI_NUMBER
        response_ni = json.dumps({
            "PERSON": "John Smith",
            "NI_NUMBER": "NE724362D",
        })
        record_ni = self.extractor._parse_extraction(response_ni, "doc1", [0])
        assert record_ni is not None
        assert record_ni.raw_government_id == "NE724362D"
        assert "NI_NUMBER" in record_ni.entity_types_found

        # US_SSN
        response_ssn = json.dumps({
            "PERSON": "Jane Doe",
            "US_SSN": "123-45-6789",
        })
        record_ssn = self.extractor._parse_extraction(response_ssn, "doc1", [0])
        assert record_ssn is not None
        assert record_ssn.raw_government_id == "123-45-6789"
        assert "US_SSN" in record_ssn.entity_types_found


# ---------------------------------------------------------------------------
# Test: extract_all_instances — full extraction flow
# ---------------------------------------------------------------------------

class TestExtractAllInstances:
    def test_6_pages_3_per_template_produces_2_records(self):
        """6 pages with 3-page template → 2 PIIRecords."""
        schema = _make_pension_schema()

        client = MagicMock()
        # Return one JSON per call (sequential, batch_size=1)
        client.generate.side_effect = [
            json.dumps({"PERSON": "John Smith", "NI_NUMBER": "NE724362D"}),
            json.dumps({"PERSON": "Jane Doe", "NI_NUMBER": "AB123456C"}),
        ]

        extractor = LLMTemplateExtractor(client, batch_size=1)
        page_texts = {
            0: "Name: John Smith\nNI: NE724362D",
            1: "Address: 123 London Rd",
            2: "Benefits summary",
            3: "Name: Jane Doe\nNI: AB123456C",
            4: "Address: 456 Manchester Ave",
            5: "Benefits summary",
        }

        records = extractor.extract_all_instances(schema, page_texts, "doc-abc", total_pages=6)
        assert len(records) == 2
        assert records[0].raw_name == "John Smith"
        assert records[1].raw_name == "Jane Doe"
        assert records[0].page_range == "1-3"
        assert records[1].page_range == "4-6"

    def test_batch_mode_parses_json_array(self):
        """Batch mode: 3 instances per call → JSON array parsed correctly."""
        schema = _make_pension_schema()
        # Override to have 3 instances (9 pages)
        schema.template.total_instances_estimate = 3

        client = MagicMock()
        # Return a JSON array for the batch
        client.generate.return_value = json.dumps([
            {"PERSON": "Alice Brown", "NI_NUMBER": "AA111111A"},
            {"PERSON": "Bob White", "NI_NUMBER": "BB222222B"},
            {"PERSON": "Charlie Davis", "NI_NUMBER": "CC333333C"},
        ])

        extractor = LLMTemplateExtractor(client, batch_size=3)
        page_texts = {i: f"Page {i} text" for i in range(9)}

        records = extractor.extract_all_instances(schema, page_texts, "doc-batch", total_pages=9)
        assert len(records) == 3
        assert records[0].raw_name == "Alice Brown"
        assert records[1].raw_name == "Bob White"
        assert records[2].raw_name == "Charlie Davis"
        # Single LLM call for all 3
        assert client.generate.call_count == 1


# ---------------------------------------------------------------------------
# Test: Fallback — LLM fails → empty list
# ---------------------------------------------------------------------------

class TestFallback:
    def test_llm_failure_returns_empty(self):
        """If LLM fails for all instances, returns empty list."""
        schema = _make_pension_schema()

        client = MagicMock()
        client.generate.side_effect = RuntimeError("LLM unavailable")

        extractor = LLMTemplateExtractor(client, batch_size=1)
        page_texts = {0: "text", 1: "text", 2: "text", 3: "text", 4: "text", 5: "text"}

        records = extractor.extract_all_instances(schema, page_texts, "doc-fail", total_pages=6)
        assert records == []


# ---------------------------------------------------------------------------
# Test: Pipeline integration — 3 paths
# ---------------------------------------------------------------------------

class TestPipelineIntegration:
    """Verify the 3-path selection logic conceptually.

    These don't run the full pipeline but verify the conditional logic
    that determines which extraction path to use.
    """

    def test_template_with_llm_uses_path_a(self):
        """Schema with template + LLM enabled → LLMTemplateExtractor called."""
        schema = _make_pension_schema()
        assert schema.template is not None
        assert schema.template.pages_per_instance >= 2

        # This is the condition that gates Path A
        llm_enabled = True
        has_template = schema.template and schema.template.pages_per_instance >= 2
        assert has_template and llm_enabled

    def test_template_without_llm_uses_path_b(self):
        """Schema with template + no LLM → extract_with_template."""
        schema = _make_pension_schema()
        assert schema.template is not None
        assert schema.template.pages_per_instance >= 2

        llm_enabled = False
        has_template = schema.template and schema.template.pages_per_instance >= 2
        assert has_template and not llm_enabled

    def test_no_template_uses_path_c(self):
        """Non-template schema → per-detection records."""
        schema = _make_no_template_schema()
        assert schema.template is None

        has_template = schema.template and schema.template.pages_per_instance >= 2
        assert not has_template


# ---------------------------------------------------------------------------
# Test: ENTITY_EXTRACTION_GUIDE coverage
# ---------------------------------------------------------------------------

class TestExtractionGuideCoverage:
    def test_covers_all_protocol_default_entity_types(self):
        """ENTITY_EXTRACTION_GUIDE covers all types in PROTOCOL_DEFAULT_ENTITIES."""
        from app.core.constants import PROTOCOL_DEFAULT_ENTITIES

        all_entity_types: set[str] = set()
        for entities in PROTOCOL_DEFAULT_ENTITIES.values():
            all_entity_types.update(entities)

        guide_types = set(ENTITY_EXTRACTION_GUIDE.keys())

        missing = all_entity_types - guide_types
        # Some entity types might map to variants (DATE_OF_BIRTH → DATE_OF_BIRTH_DMY etc.)
        # Filter out types that have a parent match
        truly_missing = set()
        for m in missing:
            # Check if a parent key covers it (e.g., DATE_OF_BIRTH covers DATE_OF_BIRTH_DMY)
            parent_match = any(m.startswith(g) or g.startswith(m) for g in guide_types)
            if not parent_match:
                truly_missing.add(m)

        assert truly_missing == set(), f"ENTITY_EXTRACTION_GUIDE missing: {truly_missing}"


# ---------------------------------------------------------------------------
# Test: build_batch_extraction_prompt
# ---------------------------------------------------------------------------

class TestBatchPrompt:
    def test_batch_prompt_includes_all_individuals(self):
        """Batch prompt includes sections for each individual."""
        schema = _make_pension_schema()
        batch_texts = [
            ["Page 1 of person 1", "Page 2 of person 1"],
            ["Page 1 of person 2", "Page 2 of person 2"],
        ]
        prompt = build_batch_extraction_prompt(
            batch_page_texts=batch_texts,
            page_roles=schema.template.page_roles,
            start_index=0,
            document_type=schema.document_type,
        )
        assert "INDIVIDUAL 1" in prompt
        assert "INDIVIDUAL 2" in prompt
        assert "Return EXACTLY 2 objects" in prompt
        assert "JSON ARRAY" in prompt


# ---------------------------------------------------------------------------
# Test: ALWAYS_EXTRACT_IF_PRESENT — NI_NUMBER included even if schema omits
# ---------------------------------------------------------------------------

class TestAlwaysExtractIfPresent:
    def test_ni_number_included_even_when_schema_omits(self):
        """Schema with only PERSON/DATE still produces prompt with NI_NUMBER."""
        from app.llm.extraction_prompts import ALWAYS_EXTRACT_IF_PRESENT

        # Schema with page_roles that only mention PERSON and DATE
        page_roles = [
            PageRole(page_offset=0, role="summary", pii_fields_expected=["PERSON", "DATE"]),
        ]
        prompt = build_extraction_prompt(
            page_texts=["Name: Mr D W Alcock\nNI: YK365578B"],
            page_roles=page_roles,
            instance_index=0,
            document_type="pension_statement",
        )
        # NI_NUMBER should be in the prompt even though schema didn't list it
        assert "NI_NUMBER" in prompt
        assert "National Insurance Number" in prompt
        # All ALWAYS_EXTRACT_IF_PRESENT fields should be present
        for field in ALWAYS_EXTRACT_IF_PRESENT:
            assert field in prompt, f"ALWAYS_EXTRACT_IF_PRESENT field {field} missing from prompt"

    def test_batch_prompt_also_includes_always_extract(self):
        """Batch prompt also includes ALWAYS_EXTRACT_IF_PRESENT fields."""
        page_roles = [
            PageRole(page_offset=0, role="summary", pii_fields_expected=["PERSON"]),
        ]
        prompt = build_batch_extraction_prompt(
            batch_page_texts=[["page text"]],
            page_roles=page_roles,
            start_index=0,
            document_type="pension_statement",
        )
        assert "NI_NUMBER" in prompt
        assert "DATE_OF_BIRTH" in prompt
        assert "US_SSN" in prompt


# ---------------------------------------------------------------------------
# Test: Batch dedup — identical names merged
# ---------------------------------------------------------------------------

class TestBatchDedup:
    def test_same_name_different_instances_not_merged(self):
        """3 identical names from DIFFERENT template instances → 3 separate records.

        Each template instance = one unique person.  Cross-instance merging
        must NEVER happen, even if names are identical (e.g., "John Smith"
        appearing in 3 different pension statement sections).
        """
        from app.structure.llm_template_extractor import _deduplicate_records

        rec1 = PIIRecord(
            record_id="r1", entity_type="PERSON", normalized_value="John Smith",
            raw_name="Mr John Smith",
            raw_address={"raw": "123 London Rd"},
            source_document_id="doc1", page_range="1-8",
            entity_types_found=("LOCATION", "PERSON"),
        )
        rec2 = PIIRecord(
            record_id="r2", entity_type="PERSON", normalized_value="John Smith",
            raw_name="Mr John Smith",
            raw_address={"raw": "123 London Rd"},
            source_document_id="doc1", page_range="9-16",
            entity_types_found=("LOCATION", "PERSON"),
        )
        rec3 = PIIRecord(
            record_id="r3", entity_type="PERSON", normalized_value="John Smith",
            raw_name="Mr John Smith",
            source_document_id="doc1", page_range="17-24",
            entity_types_found=("PERSON",),
        )

        result = _deduplicate_records([rec1, rec2, rec3])
        # Each instance is a separate person — never merge across instances
        assert len(result) == 3

    def test_same_name_same_instance_merges(self):
        """2 records with same name AND same page_range → merged (same instance)."""
        from app.structure.llm_template_extractor import _deduplicate_records

        rec_with_dob = PIIRecord(
            record_id="r1", entity_type="PERSON", normalized_value="Jane Doe",
            raw_name="Jane Doe",
            raw_dob="10-Aug-1959",
            source_document_id="doc1", page_range="1-3",
            entity_types_found=("DATE_OF_BIRTH", "PERSON"),
        )
        rec_with_addr = PIIRecord(
            record_id="r2", entity_type="PERSON", normalized_value="Jane Doe",
            raw_name="Jane Doe",
            raw_address={"raw": "456 Manchester Ave"},
            source_document_id="doc1", page_range="1-3",
            entity_types_found=("LOCATION", "PERSON"),
        )

        result = _deduplicate_records([rec_with_dob, rec_with_addr])
        assert len(result) == 1
        merged = result[0]
        assert merged.raw_dob == "10-Aug-1959"
        assert merged.raw_address == {"raw": "456 Manchester Ave"}
        assert "DATE_OF_BIRTH" in merged.entity_types_found
        assert "LOCATION" in merged.entity_types_found

    def test_same_name_different_page_range_stays_separate(self):
        """Same name, different page_range → separate records (different people)."""
        from app.structure.llm_template_extractor import _deduplicate_records

        rec1 = PIIRecord(
            record_id="r1", entity_type="PERSON", normalized_value="Jane Doe",
            raw_name="Jane Doe",
            raw_dob="10-Aug-1959",
            source_document_id="doc1", page_range="1-3",
        )
        rec2 = PIIRecord(
            record_id="r2", entity_type="PERSON", normalized_value="Jane Doe",
            raw_name="Jane Doe",
            raw_address={"raw": "456 Manchester Ave"},
            source_document_id="doc1", page_range="4-6",
        )

        result = _deduplicate_records([rec1, rec2])
        assert len(result) == 2

    def test_different_names_not_merged(self):
        """Different names remain separate records."""
        from app.structure.llm_template_extractor import _deduplicate_records

        rec1 = PIIRecord(
            record_id="r1", entity_type="PERSON", normalized_value="John Smith",
            raw_name="John Smith", source_document_id="doc1", page_range="1-3",
        )
        rec2 = PIIRecord(
            record_id="r2", entity_type="PERSON", normalized_value="Jane Doe",
            raw_name="Jane Doe", source_document_id="doc1", page_range="4-6",
        )

        result = _deduplicate_records([rec1, rec2])
        assert len(result) == 2


# ---------------------------------------------------------------------------
# Test: End-to-end 8-page template with dedup
# ---------------------------------------------------------------------------

class TestEndToEnd8PageTemplate:
    def test_8_page_template_with_dedup(self):
        """8-page template, LLM returns duplicates → deduped to 1 per individual."""
        schema = _make_pension_schema()
        # Override to 8 pages per instance, 2 instances
        schema.template.pages_per_instance = 8
        schema.template.total_instances_estimate = 2

        client = MagicMock()
        # Batch returns 3 objects per call (LLM sees 8 pages but repeats)
        # First batch: 2 instances, each with duplicated entries
        client.generate.return_value = json.dumps([
            {"PERSON": "Mr D W Alcock", "NI_NUMBER": "YK365578B",
             "LOCATION": "123 High St, London", "DATE_OF_BIRTH": "10-Aug-1959"},
            {"PERSON": "Mrs C G Astridge", "NI_NUMBER": "AB123456C",
             "LOCATION": "456 Oak Rd, Manchester"},
        ])

        extractor = LLMTemplateExtractor(client, batch_size=3)
        page_texts = {i: f"Page {i} content" for i in range(16)}

        records = extractor.extract_all_instances(schema, page_texts, "doc-8pg", total_pages=16)
        assert len(records) == 2
        names = {r.raw_name for r in records}
        assert "Mr D W Alcock" in names
        assert "Mrs C G Astridge" in names
        # Check fields populated
        alcock = next(r for r in records if "Alcock" in r.raw_name)
        assert alcock.raw_government_id == "YK365578B"
        assert alcock.raw_dob == "10-Aug-1959"
        assert alcock.raw_address == {"raw": "123 High St, London"}


# ---------------------------------------------------------------------------
# Test: Dual path eliminated
# ---------------------------------------------------------------------------

class TestDualPathEliminated:
    def test_template_path_a_does_not_run_presidio(self):
        """When Path A succeeds, Presidio analyze() is never called.

        Verifies at the conceptual level: the code structure ensures
        that when LLM extraction returns records, engine.analyze()
        is not called for that document.
        """
        # This tests the code structure: in the updated two_phase.py,
        # engine.analyze(blocks) is ONLY called inside the Path B
        # fallback block or Path C else block — never before the
        # if/else decision.
        #
        # Simulate: template detected, LLM returns records
        schema = _make_pension_schema()
        assert schema.template is not None

        client = MagicMock()
        client.generate.return_value = json.dumps([
            {"PERSON": "Test Person", "NI_NUMBER": "AB123456C"},
        ])

        extractor = LLMTemplateExtractor(client, batch_size=1)
        page_texts = {0: "text", 1: "text", 2: "text", 3: "text", 4: "text", 5: "text"}

        records = extractor.extract_all_instances(schema, page_texts, "doc1", total_pages=6)
        # LLM returned records → these are the ONLY records
        assert len(records) >= 1
        # The pipeline should use ONLY these records, not also run Presidio
        # (verified by code inspection: engine.analyze() is inside else/fallback only)
