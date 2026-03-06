"""Tests for Phase 14b: LLM Document Understanding + Schema Filter.

Covers:
- DocumentSchema creation, serialization, and deserialization
- TableSchema / TableColumn dataclasses
- SchemaFilter: field_map suppression, reclassification, passthrough, audit log
- Table filtering: non-PII table suppresses all, mixed table keeps PII only
- Flattened text: header proximity suppression
- Date context filtering
- People reclassification (ORGANIZATION → PERSON)
- Suppression hints
- Safety valve: low-confidence schema → no filtering
- LLM fallback: None schema → Presidio output unchanged
- UNDERSTAND_DOCUMENT prompt template registered
- LLMDocumentUnderstanding._parse_response
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.structure.document_schema import (
    DateContext,
    DocumentSchema,
    FieldContext,
    PersonContext,
    TableColumn,
    TableSchema,
)
from app.pii.schema_filter import SchemaFilter, FilterResult, SuppressionEntry


# ---------------------------------------------------------------------------
# Helpers: mock DetectionResult-like objects
# ---------------------------------------------------------------------------

@dataclass
class _MockBlock:
    text: str
    page_or_sheet: int = 0


@dataclass
class _MockDetection:
    entity_type: str
    start: int
    end: int
    score: float
    block: _MockBlock


def _make_detection(text: str, entity_type: str, score: float = 0.85) -> _MockDetection:
    """Create a mock detection for the given text in a block."""
    block = _MockBlock(text=text)
    return _MockDetection(
        entity_type=entity_type,
        start=0,
        end=len(text),
        score=score,
        block=block,
    )


def _make_detection_in_context(
    full_text: str, value: str, entity_type: str, score: float = 0.85,
) -> _MockDetection:
    """Create a mock detection for a value within surrounding context."""
    start = full_text.find(value)
    if start < 0:
        start = 0
    end = start + len(value)
    block = _MockBlock(text=full_text)
    return _MockDetection(
        entity_type=entity_type,
        start=start,
        end=end,
        score=score,
        block=block,
    )


def _make_schema(**overrides) -> DocumentSchema:
    """Create a DocumentSchema with sensible defaults + overrides."""
    defaults = dict(
        document_type="financial_statement",
        document_subtype="royalty_statement",
        issuing_entity="Boosey & Hawkes, Inc.",
        field_map=[],
        people=[],
        organizations=["Boosey & Hawkes, Inc."],
        date_contexts=[],
        tables=[],
        suppression_hints=[],
        extraction_notes="Single-individual financial statement",
        schema_confidence=0.85,
        detected_by="llm",
    )
    defaults.update(overrides)
    return DocumentSchema(**defaults)


# ---------------------------------------------------------------------------
# DocumentSchema creation and serialization
# ---------------------------------------------------------------------------

class TestDocumentSchema:
    """DocumentSchema dataclass basics."""

    def test_create_minimal(self) -> None:
        schema = _make_schema()
        assert schema.document_type == "financial_statement"
        assert schema.schema_confidence == 0.85

    def test_to_dict_round_trip(self) -> None:
        schema = _make_schema(
            field_map=[
                FieldContext(
                    label="Tax No.",
                    value_example="285-07-5085",
                    semantic_type="tax_identification_number",
                    is_pii=True,
                    presidio_override="US_SSN",
                    suppress_types=["COMPANY_NUMBER_UK"],
                )
            ],
            people=[
                PersonContext(
                    name="Adeline Chandler",
                    role="primary_subject",
                    context="Named on statement",
                    is_pii_subject=True,
                )
            ],
            date_contexts=[
                DateContext(
                    value="30/06/2020",
                    semantic_type="statement_period_end",
                    is_pii=False,
                )
            ],
            tables=[
                TableSchema(
                    columns=[
                        TableColumn("Date", "transaction_date", False, None),
                        TableColumn("Ref.", "reference_number", False, None),
                        TableColumn("Amount", "currency_amount", False, None),
                    ],
                    row_count_estimate=15,
                    table_context="Transaction history table",
                )
            ],
        )
        d = schema.to_dict()
        restored = DocumentSchema.from_dict(d)
        assert restored.document_type == schema.document_type
        assert restored.issuing_entity == schema.issuing_entity
        assert len(restored.field_map) == 1
        assert restored.field_map[0].presidio_override == "US_SSN"
        assert len(restored.people) == 1
        assert len(restored.date_contexts) == 1
        assert len(restored.tables) == 1
        assert len(restored.tables[0].columns) == 3

    def test_from_dict_defaults(self) -> None:
        schema = DocumentSchema.from_dict({})
        assert schema.document_type == "unknown"
        assert schema.field_map == []
        assert schema.schema_confidence == 0.0

    def test_table_schema_auto_has_pii(self) -> None:
        table = TableSchema(
            columns=[
                TableColumn("Name", "person_name", True, "PERSON"),
                TableColumn("Amount", "currency_amount", False, None),
            ],
            row_count_estimate=5,
            table_context="payroll",
        )
        assert table.has_pii_columns is True

    def test_table_schema_no_pii(self) -> None:
        table = TableSchema(
            columns=[
                TableColumn("Date", "transaction_date", False, None),
                TableColumn("Amount", "currency_amount", False, None),
            ],
            row_count_estimate=5,
            table_context="transactions",
        )
        assert table.has_pii_columns is False


# ---------------------------------------------------------------------------
# SchemaFilter: field_map suppression
# ---------------------------------------------------------------------------

class TestFieldMapSuppression:
    """SchemaFilter field_map matching."""

    def test_non_pii_field_suppresses(self) -> None:
        schema = _make_schema(field_map=[
            FieldContext("Client:", "001968", "account_number", is_pii=False),
        ])
        sf = SchemaFilter(schema)
        det = _make_detection("001968", "COMPANY_NUMBER_UK")
        result = sf.filter_detections([det])
        assert len(result.kept) == 0
        assert len(result.suppressed) == 1
        assert len(result.suppression_log) == 1
        assert "account_number" in result.suppression_log[0].reason

    def test_suppress_types_field(self) -> None:
        schema = _make_schema(field_map=[
            FieldContext(
                "Tax No.", "285-07-5085", "tax_identification_number",
                is_pii=True, presidio_override="US_SSN",
                suppress_types=["COMPANY_NUMBER_UK", "US_DRIVER_LICENSE"],
            ),
        ])
        sf = SchemaFilter(schema)
        det = _make_detection("285-07-5085", "COMPANY_NUMBER_UK")
        result = sf.filter_detections([det])
        assert len(result.kept) == 0
        assert len(result.suppressed) == 1

    def test_presidio_override_reclassifies(self) -> None:
        schema = _make_schema(field_map=[
            FieldContext(
                "Tax No.", "285-07-5085", "tax_identification_number",
                is_pii=True, presidio_override="US_SSN",
            ),
        ])
        sf = SchemaFilter(schema)
        det = _make_detection("285-07-5085", "PHONE_NUMBER")
        result = sf.filter_detections([det])
        assert len(result.kept) == 1
        assert len(result.reclassified) == 1
        assert det.entity_type == "US_SSN"

    def test_pii_field_without_override_passes(self) -> None:
        schema = _make_schema(field_map=[
            FieldContext(
                "Tax No.", "285-07-5085", "tax_identification_number",
                is_pii=True,
            ),
        ])
        sf = SchemaFilter(schema)
        det = _make_detection("285-07-5085", "US_SSN")
        result = sf.filter_detections([det])
        assert len(result.kept) == 1
        assert len(result.suppressed) == 0


# ---------------------------------------------------------------------------
# SchemaFilter: table filtering
# ---------------------------------------------------------------------------

class TestTableFiltering:
    """Table-aware filtering."""

    def test_non_pii_table_suppresses_all(self) -> None:
        """Fully non-PII table: all detections in table region suppressed."""
        schema = _make_schema(tables=[
            TableSchema(
                columns=[
                    TableColumn("Date", "transaction_date", False, None),
                    TableColumn("Ref.", "reference_number", False, None),
                    TableColumn("Description", "description_text", False, None),
                    TableColumn("Amount", "currency_amount", False, None),
                ],
                row_count_estimate=15,
                table_context="Transaction history table",
            ),
        ])
        sf = SchemaFilter(schema)
        # Detection in context with table headers nearby
        full_text = "Date Ref. Description Amount 30/06/2020 001967 Heirs Transfer 65.29 CR"
        det = _make_detection_in_context(full_text, "001967", "DRIVER_LICENSE_US")
        result = sf.filter_detections([det])
        assert len(result.kept) == 0
        assert len(result.suppressed) == 1

    def test_non_pii_table_date_suppressed(self) -> None:
        schema = _make_schema(tables=[
            TableSchema(
                columns=[
                    TableColumn("Date", "transaction_date", False, None),
                    TableColumn("Amount", "currency_amount", False, None),
                ],
                row_count_estimate=10,
                table_context="Transaction table",
            ),
        ])
        sf = SchemaFilter(schema)
        full_text = "Date Amount 30/06/2020 65.29"
        det = _make_detection_in_context(full_text, "30/06/2020", "DATE_OF_BIRTH_DMY")
        result = sf.filter_detections([det])
        assert len(result.suppressed) == 1

    def test_mixed_table_suppresses_non_pii_column(self) -> None:
        """Mixed table: non-PII column detections suppressed when no PII header nearby."""
        schema = _make_schema(tables=[
            TableSchema(
                columns=[
                    TableColumn("Name", "person_name", True, "PERSON"),
                    TableColumn("Dept", "department", False, None),
                    TableColumn("Salary", "currency_amount", False, None),
                ],
                row_count_estimate=20,
                table_context="Employee payroll records",
                has_pii_columns=True,
            ),
        ])
        sf = SchemaFilter(schema)
        # Detection near "Salary" header but NOT near "Name"
        full_text = "Dept Salary Engineering 95000"
        det = _make_detection_in_context(full_text, "95000", "US_BANK_NUMBER")
        result = sf.filter_detections([det])
        assert len(result.suppressed) == 1

    def test_pii_table_column_passes(self) -> None:
        """Detections matching PII columns are kept."""
        schema = _make_schema(tables=[
            TableSchema(
                columns=[
                    TableColumn("Name", "person_name", True, "PERSON"),
                    TableColumn("SSN", "government_id", True, "US_SSN"),
                ],
                row_count_estimate=10,
                table_context="Employee records",
                has_pii_columns=True,
            ),
        ])
        sf = SchemaFilter(schema)
        # Detection near both "Name" and "SSN" (PII columns)
        full_text = "Name SSN John Smith 123-45-6789"
        det = _make_detection_in_context(full_text, "123-45-6789", "US_SSN")
        result = sf.filter_detections([det])
        # PII header nearby, so it shouldn't be suppressed
        assert len(result.kept) == 1

    def test_no_table_passes_through(self) -> None:
        """No tables in schema → detection passes through."""
        schema = _make_schema(tables=[])
        sf = SchemaFilter(schema)
        det = _make_detection("001967", "DRIVER_LICENSE_US")
        result = sf.filter_detections([det])
        assert len(result.kept) == 1


# ---------------------------------------------------------------------------
# SchemaFilter: date context filtering
# ---------------------------------------------------------------------------

class TestDateContextFiltering:
    """Date context-based filtering."""

    def test_non_pii_date_suppressed(self) -> None:
        schema = _make_schema(date_contexts=[
            DateContext("30/06/2020", "statement_period_end", is_pii=False),
        ])
        sf = SchemaFilter(schema)
        det = _make_detection("30/06/2020", "DATE_OF_BIRTH_DMY")
        result = sf.filter_detections([det])
        assert len(result.suppressed) == 1
        assert "statement_period_end" in result.suppression_log[0].reason

    def test_pii_date_passes(self) -> None:
        schema = _make_schema(date_contexts=[
            DateContext("15/03/1985", "date_of_birth", is_pii=True),
        ])
        sf = SchemaFilter(schema)
        det = _make_detection("15/03/1985", "DATE_OF_BIRTH_DMY")
        result = sf.filter_detections([det])
        assert len(result.kept) == 1

    def test_non_date_entity_type_passes(self) -> None:
        """Non-date entity types are not affected by date context."""
        schema = _make_schema(date_contexts=[
            DateContext("30/06/2020", "statement_period_end", is_pii=False),
        ])
        sf = SchemaFilter(schema)
        det = _make_detection("30/06/2020", "US_SSN")  # Not a date type
        result = sf.filter_detections([det])
        assert len(result.kept) == 1


# ---------------------------------------------------------------------------
# SchemaFilter: people reclassification
# ---------------------------------------------------------------------------

class TestPeopleReclassification:
    """ORGANIZATION → PERSON reclassification."""

    def test_known_person_reclassified(self) -> None:
        schema = _make_schema(people=[
            PersonContext("CLIFFORD BARNES", "related_party", "Transfer", True),
        ])
        sf = SchemaFilter(schema)
        det = _make_detection("CLIFFORD BARNES", "ORGANIZATION")
        result = sf.filter_detections([det])
        assert len(result.kept) == 1
        assert len(result.reclassified) == 1
        assert det.entity_type == "PERSON"

    def test_unknown_name_not_reclassified(self) -> None:
        schema = _make_schema(people=[
            PersonContext("ADELINE CHANDLER", "primary_subject", "Named", True),
        ])
        sf = SchemaFilter(schema)
        det = _make_detection("UNKNOWN COMPANY", "ORGANIZATION")
        result = sf.filter_detections([det])
        assert len(result.kept) == 1
        assert len(result.reclassified) == 0
        assert det.entity_type == "ORGANIZATION"

    def test_non_organization_not_reclassified(self) -> None:
        """Only ORGANIZATION entities are candidates for reclassification."""
        schema = _make_schema(people=[
            PersonContext("CLIFFORD BARNES", "related_party", "Transfer", True),
        ])
        sf = SchemaFilter(schema)
        det = _make_detection("CLIFFORD BARNES", "PERSON")
        result = sf.filter_detections([det])
        assert len(result.kept) == 1
        assert len(result.reclassified) == 0  # already PERSON, no reclassification


# ---------------------------------------------------------------------------
# SchemaFilter: suppression hints
# ---------------------------------------------------------------------------

class TestSuppressionHints:
    """Suppression hints keyword matching."""

    def test_hint_suppresses(self) -> None:
        schema = _make_schema(
            suppression_hints=["001968 is client account number, not ID"],
        )
        sf = SchemaFilter(schema)
        det = _make_detection("001968", "COMPANY_NUMBER_UK")
        result = sf.filter_detections([det])
        assert len(result.suppressed) == 1
        assert "suppression_hint" in result.suppression_log[0].reason

    def test_no_matching_hint_passes(self) -> None:
        schema = _make_schema(
            suppression_hints=["001968 is client account number"],
        )
        sf = SchemaFilter(schema)
        det = _make_detection("285-07-5085", "US_SSN")
        result = sf.filter_detections([det])
        assert len(result.kept) == 1


# ---------------------------------------------------------------------------
# SchemaFilter: safety valve + passthrough
# ---------------------------------------------------------------------------

class TestSafetyValve:
    """Low-confidence schema → no filtering."""

    def test_low_confidence_passes_all(self) -> None:
        schema = _make_schema(
            schema_confidence=0.30,
            field_map=[
                FieldContext("Client:", "001968", "account_number", is_pii=False),
            ],
        )
        sf = SchemaFilter(schema)
        det = _make_detection("001968", "COMPANY_NUMBER_UK")
        result = sf.filter_detections([det])
        assert len(result.kept) == 1
        assert len(result.suppressed) == 0

    def test_exact_threshold_filters(self) -> None:
        schema = _make_schema(
            schema_confidence=0.50,
            field_map=[
                FieldContext("Client:", "001968", "account_number", is_pii=False),
            ],
        )
        sf = SchemaFilter(schema)
        det = _make_detection("001968", "COMPANY_NUMBER_UK")
        result = sf.filter_detections([det])
        assert len(result.suppressed) == 1


class TestNoSchemaPassthrough:
    """Without a schema, all detections pass through unfiltered."""

    def test_empty_schema_passes_all(self) -> None:
        schema = _make_schema(schema_confidence=0.85)
        sf = SchemaFilter(schema)
        dets = [
            _make_detection("001968", "COMPANY_NUMBER_UK"),
            _make_detection("285-07-5085", "US_SSN"),
            _make_detection("30/06/2020", "DATE_OF_BIRTH_DMY"),
        ]
        result = sf.filter_detections(dets)
        assert len(result.kept) == 3
        assert len(result.suppressed) == 0


# ---------------------------------------------------------------------------
# SchemaFilter: audit log
# ---------------------------------------------------------------------------

class TestAuditLog:
    """Suppression log for audit trail."""

    def test_suppression_log_populated(self) -> None:
        schema = _make_schema(field_map=[
            FieldContext("Client:", "001968", "account_number", is_pii=False),
        ])
        sf = SchemaFilter(schema)
        det = _make_detection("001968", "COMPANY_NUMBER_UK")
        sf.filter_detections([det])
        log = sf.get_suppression_log()
        assert len(log) == 1
        assert log[0].action == "suppress"
        assert log[0].entity_type == "COMPANY_NUMBER_UK"
        # Detected text should be masked
        assert "001968"[:2] not in log[0].detected_text  # first chars masked

    def test_reclassification_logged(self) -> None:
        schema = _make_schema(field_map=[
            FieldContext(
                "Tax No.", "285-07-5085", "tax_id",
                is_pii=True, presidio_override="US_SSN",
            ),
        ])
        sf = SchemaFilter(schema)
        det = _make_detection("285-07-5085", "PHONE_NUMBER")
        sf.filter_detections([det])
        log = sf.get_suppression_log()
        assert len(log) == 1
        assert log[0].action == "reclassify"
        assert log[0].new_entity_type == "US_SSN"


# ---------------------------------------------------------------------------
# Prompt template registration
# ---------------------------------------------------------------------------

class TestPromptTemplate:
    """UNDERSTAND_DOCUMENT prompt template is registered."""

    def test_template_registered(self) -> None:
        from app.llm.prompts import PROMPT_TEMPLATES
        assert "understand_document" in PROMPT_TEMPLATES

    def test_template_has_placeholders(self) -> None:
        from app.llm.prompts import UNDERSTAND_DOCUMENT
        for placeholder in [
            "{file_name}", "{file_type}", "{structure_class}",
            "{heuristic_doc_type}", "{onset_page}", "{page_text}",
        ]:
            assert placeholder in UNDERSTAND_DOCUMENT

    def test_six_templates_total(self) -> None:
        from app.llm.prompts import PROMPT_TEMPLATES
        assert len(PROMPT_TEMPLATES) == 6


# ---------------------------------------------------------------------------
# LLMDocumentUnderstanding._parse_response
# ---------------------------------------------------------------------------

class TestLLMDocumentUnderstandingParser:
    """Test the JSON parser for LLM responses."""

    def _parse(self, response_text: str) -> DocumentSchema:
        from app.structure.llm_document_understanding import LLMDocumentUnderstanding
        ldu = LLMDocumentUnderstanding.__new__(LLMDocumentUnderstanding)
        return ldu._parse_response(response_text)

    def test_parse_valid_response(self) -> None:
        response = """{
            "document_type": "financial_statement",
            "document_subtype": "royalty_statement",
            "issuing_entity": "Boosey & Hawkes",
            "field_map": [
                {
                    "label": "Client:",
                    "value_example": "001968",
                    "semantic_type": "account_number",
                    "is_pii": false,
                    "presidio_override": null,
                    "suppress_types": ["COMPANY_NUMBER_UK"]
                }
            ],
            "people": [
                {
                    "name": "Adeline Chandler",
                    "role": "primary_subject",
                    "context": "Named on statement",
                    "is_pii_subject": true
                }
            ],
            "organizations": ["Boosey & Hawkes"],
            "date_contexts": [
                {"value": "30/06/2020", "semantic_type": "statement_period", "is_pii": false}
            ],
            "tables": [
                {
                    "columns": [
                        {"header": "Date", "semantic_type": "transaction_date", "contains_pii": false, "pii_type": null},
                        {"header": "Amount", "semantic_type": "currency_amount", "contains_pii": false, "pii_type": null}
                    ],
                    "row_count_estimate": 15,
                    "table_context": "Transaction history",
                    "has_pii_columns": false
                }
            ],
            "suppression_hints": ["001968 is a client account number"],
            "extraction_notes": "Single individual royalty statement",
            "schema_confidence": 0.90
        }"""
        schema = self._parse(response)
        assert schema.document_type == "financial_statement"
        assert schema.issuing_entity == "Boosey & Hawkes"
        assert len(schema.field_map) == 1
        assert schema.field_map[0].is_pii is False
        assert len(schema.people) == 1
        assert schema.people[0].is_pii_subject is True
        assert len(schema.tables) == 1
        assert schema.tables[0].has_pii_columns is False
        assert schema.schema_confidence == 0.90
        assert schema.detected_by == "llm"

    def test_parse_with_markdown_fences(self) -> None:
        response = """```json
        {"document_type": "medical_record", "schema_confidence": 0.75}
        ```"""
        schema = self._parse(response)
        assert schema.document_type == "medical_record"

    def test_parse_minimal_response(self) -> None:
        response = '{"document_type": "unknown"}'
        schema = self._parse(response)
        assert schema.document_type == "unknown"
        assert schema.field_map == []
        assert schema.schema_confidence == 0.5  # default

    def test_parse_clamps_confidence(self) -> None:
        response = '{"schema_confidence": 2.5}'
        schema = self._parse(response)
        assert schema.schema_confidence == 1.0

    def test_parse_negative_confidence(self) -> None:
        response = '{"schema_confidence": -0.5}'
        schema = self._parse(response)
        assert schema.schema_confidence == 0.0


# ---------------------------------------------------------------------------
# LLMDocumentUnderstanding.understand fallback
# ---------------------------------------------------------------------------

class TestLLMFallback:
    """LLM returns None when disabled or fails."""

    def test_empty_blocks_returns_none(self) -> None:
        from app.structure.llm_document_understanding import LLMDocumentUnderstanding
        ldu = LLMDocumentUnderstanding(db_session=None)
        result = ldu.understand([])
        assert result is None


# ---------------------------------------------------------------------------
# Integration: schema + detections → filtered output
# ---------------------------------------------------------------------------

class TestIntegration:
    """End-to-end: DocumentSchema + SchemaFilter → filtered detections."""

    def test_boosey_hawkes_scenario(self) -> None:
        """Simulate the Boosey & Hawkes financial statement FP scenario."""
        schema = _make_schema(
            field_map=[
                FieldContext("Client:", "001968", "account_number", is_pii=False),
                FieldContext(
                    "Statement Nr.:", "1121799", "reference_number", is_pii=False,
                ),
                FieldContext(
                    "Tax No.:", "285-07-5085", "tax_identification_number",
                    is_pii=True, presidio_override="US_SSN",
                    suppress_types=["COMPANY_NUMBER_UK", "US_DRIVER_LICENSE"],
                ),
            ],
            people=[
                PersonContext(
                    "ADELINE CHANDLER", "primary_subject", "Named", True,
                ),
                PersonContext(
                    "CLIFFORD BARNES", "related_party", "Transfer", True,
                ),
            ],
            date_contexts=[
                DateContext("30/06/2020", "statement_period_end", is_pii=False),
            ],
            tables=[
                TableSchema(
                    columns=[
                        TableColumn("Date", "transaction_date", False, None),
                        TableColumn("Ref.", "reference_number", False, None),
                        TableColumn("Description", "description_text", False, None),
                        TableColumn("Amount", "currency_amount", False, None),
                    ],
                    row_count_estimate=15,
                    table_context="Transaction history table",
                ),
            ],
            suppression_hints=[
                "001968 is client account number, not a government ID",
            ],
        )

        sf = SchemaFilter(schema)

        # Simulate Presidio detections
        detections = [
            # Should be SUPPRESSED: non-PII field
            _make_detection("001968", "COMPANY_NUMBER_UK", 0.70),
            # Should be SUPPRESSED: reference number field
            _make_detection("1121799", "DRIVER_LICENSE_US", 0.65),
            # Should be KEPT (reclassified): tax ID → US_SSN
            _make_detection("285-07-5085", "PHONE_NUMBER", 0.60),
            # Should be SUPPRESSED: transaction date (not DOB)
            _make_detection("30/06/2020", "DATE_OF_BIRTH_DMY", 0.70),
            # Should be KEPT: real person
            _make_detection("ADELINE CHANDLER", "PERSON", 0.90),
        ]

        result = sf.filter_detections(detections)

        assert len(result.suppressed) == 3  # 001968, 1121799, 30/06/2020
        assert len(result.kept) == 2  # 285-07-5085 (reclassified), ADELINE CHANDLER
        assert len(result.reclassified) == 1  # 285-07-5085
        assert result.reclassified[0].entity_type == "US_SSN"
        assert len(result.suppression_log) == 4  # 3 suppressions + 1 reclassification
