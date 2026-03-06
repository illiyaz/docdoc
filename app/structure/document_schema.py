"""Document schema dataclasses for LLM Document Understanding (Phase 14b).

A ``DocumentSchema`` is a structured understanding of a document's layout,
field meanings, people, dates, and tables.  It is produced by the LLM and
used by ``SchemaFilter`` to post-process Presidio detections.

The schema is a READ-ONLY overlay — it never modifies Presidio's engine or
patterns.  When the LLM is disabled or fails, the schema is ``None`` and
all Presidio detections pass through unfiltered.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FieldContext:
    """A labeled field in the document and its semantic meaning."""

    label: str                          # "Tax No.", "Client:", "Statement Nr."
    value_example: str                  # "285-07-5085", "001968", "1121799"
    semantic_type: str                  # "tax_identification_number", "account_number", etc.
    is_pii: bool                        # True for tax_id, False for account_number
    presidio_override: str | None = None  # "US_SSN" — what Presidio SHOULD classify this as
    suppress_types: list[str] = field(default_factory=list)  # types to suppress for this value


@dataclass
class PersonContext:
    """A person identified in the document with their role."""

    name: str               # "Clifford Barnes"
    role: str               # "related_party", "primary_subject", "institutional"
    context: str            # "Named in transfer transaction"
    is_pii_subject: bool    # True if this person's PII should be extracted


@dataclass
class DateContext:
    """A date value in the document with its semantic meaning."""

    value: str              # "30/06/2020"
    semantic_type: str      # "transaction_date", "statement_period_end", "date_of_birth"
    is_pii: bool            # False for transaction_date, True for DOB


@dataclass
class TableColumn:
    """A single column in a table with its semantic classification."""

    header: str             # "Date", "Ref.", "Description", "Amount"
    semantic_type: str      # "transaction_date", "reference_number", etc.
    contains_pii: bool      # True for PII columns (e.g., "SSN", "Name")
    pii_type: str | None    # Presidio type if contains_pii, else None


@dataclass
class TableSchema:
    """Schema for a table detected on the page."""

    columns: list[TableColumn]
    row_count_estimate: int             # approximate number of data rows
    table_context: str                  # "Transaction history table"
    table_location: str | None = None   # "page_1_lower_half"
    has_pii_columns: bool = False       # convenience: any column has contains_pii=True

    def __post_init__(self) -> None:
        if not self.has_pii_columns:
            self.has_pii_columns = any(c.contains_pii for c in self.columns)


@dataclass
class DocumentSchema:
    """LLM-produced understanding of a document's structure and field semantics.

    Used by ``SchemaFilter`` to post-process Presidio output.  When
    ``schema_confidence < 0.50``, filtering is skipped (safety valve).
    """

    document_type: str                  # "financial_statement", "medical_record", etc.
    document_subtype: str | None        # "royalty_statement", "bank_statement"
    issuing_entity: str | None          # "Boosey & Hawkes, Inc."

    field_map: list[FieldContext]       # labeled fields and what they actually are
    people: list[PersonContext]         # people identified with roles
    organizations: list[str]            # orgs identified (issuer, employers, etc.)
    date_contexts: list[DateContext]    # dates with their actual meaning
    tables: list[TableSchema]          # zero or more tables detected on the page

    suppression_hints: list[str]        # free-text hints about non-PII values
    extraction_notes: str               # "This is a single-individual financial statement"

    schema_confidence: float            # 0.0-1.0
    detected_by: str                    # "llm" | "heuristic" | "llm+heuristic"

    def to_dict(self) -> dict:
        """Serialize to a JSON-compatible dict."""
        return {
            "document_type": self.document_type,
            "document_subtype": self.document_subtype,
            "issuing_entity": self.issuing_entity,
            "field_map": [
                {
                    "label": f.label,
                    "value_example": f.value_example,
                    "semantic_type": f.semantic_type,
                    "is_pii": f.is_pii,
                    "presidio_override": f.presidio_override,
                    "suppress_types": f.suppress_types,
                }
                for f in self.field_map
            ],
            "people": [
                {
                    "name": p.name,
                    "role": p.role,
                    "context": p.context,
                    "is_pii_subject": p.is_pii_subject,
                }
                for p in self.people
            ],
            "organizations": list(self.organizations),
            "date_contexts": [
                {
                    "value": d.value,
                    "semantic_type": d.semantic_type,
                    "is_pii": d.is_pii,
                }
                for d in self.date_contexts
            ],
            "tables": [
                {
                    "columns": [
                        {
                            "header": c.header,
                            "semantic_type": c.semantic_type,
                            "contains_pii": c.contains_pii,
                            "pii_type": c.pii_type,
                        }
                        for c in t.columns
                    ],
                    "row_count_estimate": t.row_count_estimate,
                    "table_context": t.table_context,
                    "table_location": t.table_location,
                    "has_pii_columns": t.has_pii_columns,
                }
                for t in self.tables
            ],
            "suppression_hints": list(self.suppression_hints),
            "extraction_notes": self.extraction_notes,
            "schema_confidence": self.schema_confidence,
            "detected_by": self.detected_by,
        }

    @classmethod
    def from_dict(cls, data: dict) -> DocumentSchema:
        """Deserialize from a JSON-compatible dict."""
        return cls(
            document_type=data.get("document_type", "unknown"),
            document_subtype=data.get("document_subtype"),
            issuing_entity=data.get("issuing_entity"),
            field_map=[
                FieldContext(
                    label=f["label"],
                    value_example=f["value_example"],
                    semantic_type=f["semantic_type"],
                    is_pii=f["is_pii"],
                    presidio_override=f.get("presidio_override"),
                    suppress_types=f.get("suppress_types", []),
                )
                for f in data.get("field_map", [])
            ],
            people=[
                PersonContext(
                    name=p["name"],
                    role=p["role"],
                    context=p["context"],
                    is_pii_subject=p["is_pii_subject"],
                )
                for p in data.get("people", [])
            ],
            organizations=data.get("organizations", []),
            date_contexts=[
                DateContext(
                    value=d["value"],
                    semantic_type=d["semantic_type"],
                    is_pii=d["is_pii"],
                )
                for d in data.get("date_contexts", [])
            ],
            tables=[
                TableSchema(
                    columns=[
                        TableColumn(
                            header=c["header"],
                            semantic_type=c["semantic_type"],
                            contains_pii=c["contains_pii"],
                            pii_type=c.get("pii_type"),
                        )
                        for c in t.get("columns", [])
                    ],
                    row_count_estimate=t.get("row_count_estimate", 0),
                    table_context=t.get("table_context", ""),
                    table_location=t.get("table_location"),
                    has_pii_columns=t.get("has_pii_columns", False),
                )
                for t in data.get("tables", [])
            ],
            suppression_hints=data.get("suppression_hints", []),
            extraction_notes=data.get("extraction_notes", ""),
            schema_confidence=data.get("schema_confidence", 0.0),
            detected_by=data.get("detected_by", "unknown"),
        )
