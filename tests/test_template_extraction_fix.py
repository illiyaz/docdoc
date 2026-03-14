"""Tests for template extraction fixes: address assembly, same-doc dedup, exclusive paths.

Covers:
- build_composite_record: multiple LOCATIONs → single joined address
- build_composite_record: all raw_* fields populated
- Same-doc same-name dedup safety net in EntityResolver
- Template active: only composite records, no per-detection records
- Template inactive: per-detection records (backward compatible)
- End-to-end: 6-page pension template → 2 NotificationSubjects
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

import pytest

from app.pipeline.record_mapper import (
    _ADDRESS_TYPES,
    _DOB_TYPES,
    _GOV_ID_FIELD_TYPES,
    _PERSON_TYPES,
    build_composite_record,
    detection_to_pii_record,
    extract_with_template,
)
from app.rra.entity_resolver import EntityResolver, PIIRecord, build_confidence
from app.structure.document_schema import DocumentSchema, DocumentTemplate, PageRole


# ---------------------------------------------------------------------------
# Helpers: lightweight DetectionResult mock
# ---------------------------------------------------------------------------


@dataclass
class MockBlock:
    text: str
    page_or_sheet: int = 0


@dataclass
class MockDetection:
    entity_type: str
    start: int
    end: int
    score: float
    block: MockBlock | None = None


def _det(text: str, entity_type: str, page: int = 0, score: float = 0.85) -> MockDetection:
    """Create a mock DetectionResult with the given text, type, and page."""
    block = MockBlock(text=text, page_or_sheet=page)
    return MockDetection(
        entity_type=entity_type,
        start=0,
        end=len(text),
        score=score,
        block=block,
    )


def _make_template_schema(pages_per_instance: int = 3) -> DocumentSchema:
    """Create a minimal DocumentSchema with a repeating template."""
    return DocumentSchema(
        document_type="pension_statement",
        document_subtype="transfer_value",
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
        template=DocumentTemplate(
            template_name="pension_transfer",
            pages_per_instance=pages_per_instance,
            total_instances_estimate=2,
            page_roles=[
                PageRole(page_offset=0, role="summary", is_identity_page=True),
                PageRole(page_offset=1, role="details"),
                PageRole(page_offset=2, role="benefits"),
            ],
            identity_page_offset=0,
        ),
    )


# ===========================================================================
# Address assembly: join multiple LOCATIONs
# ===========================================================================


class TestAddressAssembly:
    def test_multiple_locations_joined(self):
        """4 LOCATION detections from same page → 1 joined address."""
        dets = [
            _det("85 Waltings Gardens", "LOCATION", page=0, score=0.90),
            _det("Shoot Up Hill", "LOCATION", page=0, score=0.85),
            _det("London", "LOCATION", page=0, score=0.80),
            _det("NW2 3UD", "LOCATION", page=0, score=0.75),
        ]
        record = build_composite_record(dets, "doc1")
        assert record.raw_address is not None
        addr = record.raw_address.get("raw", "")
        assert "85 Waltings Gardens" in addr
        assert "Shoot Up Hill" in addr
        assert "London" in addr
        assert "NW2 3UD" in addr
        # Should be comma-separated
        assert ", " in addr

    def test_single_location_kept(self):
        """Single LOCATION → kept as-is."""
        dets = [_det("London", "LOCATION", page=0)]
        record = build_composite_record(dets, "doc1")
        assert record.raw_address == {"raw": "London"}

    def test_duplicate_locations_deduped(self):
        """Same text appearing twice (e.g. from different detectors) → kept once."""
        dets = [
            _det("London", "LOCATION", page=0, score=0.90),
            _det("London", "LOCATION", page=0, score=0.80),
        ]
        record = build_composite_record(dets, "doc1")
        addr = record.raw_address.get("raw", "")
        assert addr == "London"  # not "London, London"

    def test_cross_page_locations_joined(self):
        """LOCATIONs from different pages → all joined."""
        dets = [
            _det("85 Waltings Gardens", "LOCATION", page=0),
            _det("London", "LOCATION", page=1),
        ]
        record = build_composite_record(dets, "doc1")
        addr = record.raw_address.get("raw", "")
        assert "85 Waltings Gardens" in addr
        assert "London" in addr


# ===========================================================================
# Composite record: all raw_* fields populated
# ===========================================================================


class TestCompositeRecordFields:
    def test_all_fields_populated(self):
        """Composite record from mixed detections has all raw_* fields set."""
        dets = [
            _det("K P Acheampong", "PERSON", page=0),
            _det("85 Waltings Gardens", "LOCATION", page=0),
            _det("10-Aug-1959", "DATE_OF_BIRTH_DMY", page=1),
            _det("NE724362D", "NI_NUMBER", page=1),
            _det("jane@example.com", "EMAIL_ADDRESS", page=2),
            _det("+44 20 7946 0958", "PHONE_NUMBER", page=2),
        ]
        record = build_composite_record(dets, "doc1")

        assert record.raw_name == "K P Acheampong"
        assert record.raw_address is not None
        assert "85 Waltings Gardens" in record.raw_address.get("raw", "")
        assert record.raw_dob == "10-Aug-1959"
        assert record.raw_government_id == "NE724362D"
        assert record.raw_email == "jane@example.com"
        assert record.raw_phone == "+44 20 7946 0958"

    def test_page_range_multi_page(self):
        """Composite from pages 0,1,2 → page_range '1-3' (1-indexed)."""
        dets = [
            _det("Name", "PERSON", page=0),
            _det("Address", "LOCATION", page=1),
            _det("01/01/1990", "DATE_OF_BIRTH_DMY", page=2),
        ]
        record = build_composite_record(dets, "doc1")
        assert record.page_range == "1-3"

    def test_entity_types_found(self):
        """All entity types collected in entity_types_found tuple."""
        dets = [
            _det("Name", "PERSON", page=0),
            _det("NE724362D", "NI_NUMBER", page=1),
        ]
        record = build_composite_record(dets, "doc1")
        assert "PERSON" in record.entity_types_found
        assert "NI_NUMBER" in record.entity_types_found


# ===========================================================================
# Same-doc same-name dedup safety net
# ===========================================================================


class TestSameDocSameNameDedup:
    def test_same_doc_same_name_high_confidence(self):
        """Two records with same source_doc and same name → 0.95 confidence."""
        r1 = PIIRecord(
            record_id="1", entity_type="PERSON", normalized_value="K P Acheampong",
            raw_name="K P Acheampong", source_document_id="doc1-test-pension-statement",
        )
        r2 = PIIRecord(
            record_id="2", entity_type="PERSON", normalized_value="K P Acheampong",
            raw_name="K P Acheampong", source_document_id="doc1-test-pension-statement",
        )
        conf = build_confidence(r1, r2)
        assert conf >= 0.95

    def test_same_doc_same_name_case_insensitive(self):
        """Case-insensitive name matching for same-doc dedup."""
        r1 = PIIRecord(
            record_id="1", entity_type="PERSON", normalized_value="k p acheampong",
            raw_name="K P Acheampong", source_document_id="doc1-test-pension-statement",
        )
        r2 = PIIRecord(
            record_id="2", entity_type="PERSON", normalized_value="K P ACHEAMPONG",
            raw_name="k p acheampong", source_document_id="doc1-test-pension-statement",
        )
        conf = build_confidence(r1, r2)
        assert conf >= 0.95

    def test_same_doc_same_name_whitespace_normalized(self):
        """Extra whitespace ignored in same-doc dedup."""
        r1 = PIIRecord(
            record_id="1", entity_type="PERSON", normalized_value="K P Acheampong",
            raw_name="K  P  Acheampong", source_document_id="doc1-test-pension-statement",
        )
        r2 = PIIRecord(
            record_id="2", entity_type="PERSON", normalized_value="K P Acheampong",
            raw_name="K P Acheampong", source_document_id="doc1-test-pension-statement",
        )
        conf = build_confidence(r1, r2)
        assert conf >= 0.95

    def test_different_docs_same_name_not_boosted(self):
        """Same name but different source docs → normal confidence (0.10 name-only)."""
        r1 = PIIRecord(
            record_id="1", entity_type="PERSON", normalized_value="K P Acheampong",
            raw_name="K P Acheampong", source_document_id="doc1-test-pension-statement",
        )
        r2 = PIIRecord(
            record_id="2", entity_type="PERSON", normalized_value="K P Acheampong",
            raw_name="K P Acheampong", source_document_id="doc2-other-statement",
        )
        conf = build_confidence(r1, r2)
        assert conf < 0.95  # Should be normal confidence, not boosted

    def test_same_doc_different_names_not_merged(self):
        """Different names from same doc → NOT merged."""
        r1 = PIIRecord(
            record_id="1", entity_type="PERSON", normalized_value="K P Acheampong",
            raw_name="K P Acheampong", source_document_id="doc1-test-pension-statement",
        )
        r2 = PIIRecord(
            record_id="2", entity_type="PERSON", normalized_value="M S Alcock",
            raw_name="M S Alcock", source_document_id="doc1-test-pension-statement",
        )
        conf = build_confidence(r1, r2)
        assert conf < 0.30  # Should not be merged

    def test_resolver_merges_same_doc_same_name(self):
        """EntityResolver merges same-doc same-name records into one group."""
        records = [
            PIIRecord(
                record_id="1", entity_type="PERSON", normalized_value="K P Acheampong",
                raw_name="K P Acheampong", source_document_id="doc1-test-pension-statement",
                raw_address={"raw": "85 Waltings Gardens"},
            ),
            PIIRecord(
                record_id="2", entity_type="PERSON", normalized_value="K P Acheampong",
                raw_name="K P Acheampong", source_document_id="doc1-test-pension-statement",
            ),
            PIIRecord(
                record_id="3", entity_type="PERSON", normalized_value="K P Acheampong",
                raw_name="K P Acheampong", source_document_id="doc1-test-pension-statement",
                raw_dob="10-Aug-1959",
            ),
        ]
        resolver = EntityResolver()
        groups = resolver.resolve(records)

        # All 3 records should be in one group (no page_range → safety net fires)
        assert len(groups) == 1
        assert len(groups[0].records) == 3
        assert groups[0].merge_confidence >= 0.80


class TestCrossInstanceMergePrevention:
    """Cross-instance records must NEVER merge, even with identical names."""

    def test_same_name_different_page_range_zero_confidence(self):
        """Same doc, same name, different page_range → 0.0 confidence."""
        r1 = PIIRecord(
            record_id="1", entity_type="PERSON", normalized_value="P Davies",
            raw_name="P Davies", source_document_id="doc1-test-pension",
            page_range="1-3",
        )
        r2 = PIIRecord(
            record_id="2", entity_type="PERSON", normalized_value="P Davies",
            raw_name="P Davies", source_document_id="doc1-test-pension",
            page_range="4-6",
        )
        conf = build_confidence(r1, r2)
        assert conf == 0.0

    def test_similar_names_different_instances_not_merged(self):
        """P Davie vs P Davies from different instances → 0.0."""
        r1 = PIIRecord(
            record_id="1", entity_type="PERSON", normalized_value="P Davie",
            raw_name="P Davie", source_document_id="doc1-test-pension",
            page_range="1-3", raw_dob="01-Jan-1960",
        )
        r2 = PIIRecord(
            record_id="2", entity_type="PERSON", normalized_value="P Davies",
            raw_name="P Davies", source_document_id="doc1-test-pension",
            page_range="4-6", raw_dob="01-Jan-1960",
        )
        conf = build_confidence(r1, r2)
        assert conf == 0.0

    def test_resolver_keeps_149_instances_separate(self):
        """149 template instances with some duplicate names → 149 groups."""
        records = []
        for i in range(149):
            records.append(PIIRecord(
                record_id=str(i),
                entity_type="PERSON",
                normalized_value=f"Person {i % 50}",  # only 50 unique names for 149 people
                raw_name=f"Person {i % 50}",
                source_document_id="doc1-pension-statement-long-id",
                page_range=f"{i*3+1}-{i*3+3}",
                raw_government_id=f"AB{i:06d}C",
            ))

        resolver = EntityResolver()
        groups = resolver.resolve(records)
        # Each instance is a separate person — must get 149 groups
        assert len(groups) == 149

    def test_same_instance_same_name_still_merges(self):
        """Same doc, same name, SAME page_range → still merges (0.95)."""
        r1 = PIIRecord(
            record_id="1", entity_type="PERSON", normalized_value="P Davies",
            raw_name="P Davies", source_document_id="doc1-test-pension",
            page_range="1-3",
        )
        r2 = PIIRecord(
            record_id="2", entity_type="PERSON", normalized_value="P Davies",
            raw_name="P Davies", source_document_id="doc1-test-pension",
            page_range="1-3",
        )
        conf = build_confidence(r1, r2)
        assert conf >= 0.95

    def test_different_docs_same_page_range_not_blocked(self):
        """Different docs, same page_range → normal matching (not blocked)."""
        r1 = PIIRecord(
            record_id="1", entity_type="PERSON", normalized_value="P Davies",
            raw_name="P Davies", source_document_id="doc1-pension",
            page_range="1-3", raw_dob="01-Jan-1960",
        )
        r2 = PIIRecord(
            record_id="2", entity_type="PERSON", normalized_value="P Davies",
            raw_name="P Davies", source_document_id="doc2-pension",
            page_range="1-3", raw_dob="01-Jan-1960",
        )
        conf = build_confidence(r1, r2)
        # Different docs → cross-instance check doesn't fire → normal matching
        assert conf > 0.0


# ===========================================================================
# Template active: only composite records
# ===========================================================================


class TestTemplateExclusive:
    def test_template_returns_composite_records_only(self):
        """extract_with_template returns 1 composite per instance, not per-detection."""
        schema = _make_template_schema(pages_per_instance=3)

        # 6-page doc, 2 individuals, 3 pages each
        dets = [
            # Person 1 (pages 0-2)
            _det("K P Acheampong", "PERSON", page=0),
            _det("85 Waltings Gardens", "LOCATION", page=0),
            _det("London", "LOCATION", page=0),
            _det("10-Aug-1959", "DATE_OF_BIRTH_DMY", page=1),
            _det("NE724362D", "NI_NUMBER", page=1),
            # Person 2 (pages 3-5)
            _det("M S Alcock", "PERSON", page=3),
            _det("3 Whitworth Road", "LOCATION", page=3),
            _det("Wellingborough", "LOCATION", page=3),
            _det("29-Aug-1960", "DATE_OF_BIRTH_DMY", page=4),
            _det("WK393925C", "NI_NUMBER", page=4),
        ]

        records = extract_with_template(dets, schema, "pension.pdf", total_pages=6)

        # Should be exactly 2 composite records
        assert len(records) == 2

        # Record 1: K P Acheampong
        r1 = records[0]
        assert r1.raw_name == "K P Acheampong"
        assert r1.raw_address is not None
        assert "85 Waltings Gardens" in r1.raw_address.get("raw", "")
        assert "London" in r1.raw_address.get("raw", "")
        assert r1.raw_dob == "10-Aug-1959"
        assert r1.raw_government_id == "NE724362D"
        assert r1.page_range == "1-3"

        # Record 2: M S Alcock
        r2 = records[1]
        assert r2.raw_name == "M S Alcock"
        assert r2.raw_address is not None
        assert "3 Whitworth Road" in r2.raw_address.get("raw", "")
        assert "Wellingborough" in r2.raw_address.get("raw", "")
        assert r2.raw_dob == "29-Aug-1960"
        assert r2.raw_government_id == "WK393925C"
        assert r2.page_range == "4-6"

    def test_no_template_uses_per_detection(self):
        """Without template, extract_with_template falls back to per-detection."""
        schema = _make_template_schema(pages_per_instance=1)

        dets = [
            _det("K P Acheampong", "PERSON", page=0),
            _det("85 Waltings Gardens", "LOCATION", page=0),
        ]

        records = extract_with_template(dets, schema, "doc.pdf", total_pages=1)

        # Falls back to per-detection: 2 records
        assert len(records) == 2


# ===========================================================================
# End-to-end: template → EntityResolver → 2 groups
# ===========================================================================


class TestEndToEndTemplate:
    def test_pension_template_two_subjects(self):
        """6-page pension doc → 2 composite records → 2 resolved groups."""
        schema = _make_template_schema(pages_per_instance=3)

        dets = [
            # Person 1 (pages 0-2)
            _det("K P Acheampong", "PERSON", page=0),
            _det("85 Waltings Gardens", "LOCATION", page=0),
            _det("Shoot Up Hill", "LOCATION", page=0),
            _det("London", "LOCATION", page=0),
            _det("NW2 3UD", "LOCATION", page=0),
            _det("10-Aug-1959", "DATE_OF_BIRTH_DMY", page=1),
            _det("NE724362D", "NI_NUMBER", page=1),
            # Person 2 (pages 3-5)
            _det("M S Alcock", "PERSON", page=3),
            _det("3 Whitworth Road", "LOCATION", page=3),
            _det("Wellingborough", "LOCATION", page=3),
            _det("Northants", "LOCATION", page=3),
            _det("NN8 1QQ", "LOCATION", page=3),
            _det("29-Aug-1960", "DATE_OF_BIRTH_DMY", page=4),
            _det("WK393925C", "NI_NUMBER", page=4),
        ]

        records = extract_with_template(dets, schema, "pension.pdf", total_pages=6)
        assert len(records) == 2

        # EntityResolver should keep them as 2 separate groups
        resolver = EntityResolver()
        groups = resolver.resolve(records)
        assert len(groups) == 2

        # Each group has exactly 1 composite record
        for g in groups:
            assert len(g.records) == 1
            rec = g.records[0]
            assert rec.raw_name is not None
            assert rec.raw_address is not None
            assert rec.raw_dob is not None
            assert rec.raw_government_id is not None

    def test_duplicate_per_detection_records_merged_by_safety_net(self):
        """If template fails, per-detection records with same name + doc → merged."""
        # Simulate what happens if template grouping doesn't work:
        # 10 detections for same person, all as per-detection PIIRecords
        records = []
        for i in range(10):
            records.append(PIIRecord(
                record_id=str(uuid4()),
                entity_type="PERSON",
                normalized_value="K P Acheampong",
                raw_name="K P Acheampong",
                source_document_id="pension.pdf",
                page_or_sheet=i % 3,
            ))
        # Add one record with address
        records.append(PIIRecord(
            record_id=str(uuid4()),
            entity_type="LOCATION",
            normalized_value="85 Waltings Gardens",
            raw_address={"raw": "85 Waltings Gardens"},
            source_document_id="pension.pdf",
            page_or_sheet=0,
        ))

        resolver = EntityResolver()
        groups = resolver.resolve(records)

        # All 10 PERSON records should merge into 1 group
        # The LOCATION record has no raw_name so stays separate (that's OK)
        person_groups = [g for g in groups if any(r.raw_name for r in g.records)]
        assert len(person_groups) == 1
        assert len(person_groups[0].records) == 10


# ===========================================================================
# Deduplicator integration
# ===========================================================================


class TestDeduplicatorWithCompositeRecords:
    def test_composite_records_produce_populated_subjects(self, tmp_path):
        """Composite records from template → NotificationSubjects with all fields."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        from app.db.base import Base
        from app.rra.deduplicator import Deduplicator

        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=engine)
        SessionLocal = sessionmaker(bind=engine)
        session = SessionLocal()

        try:
            # Build composite records like template extraction would
            schema = _make_template_schema(pages_per_instance=3)
            dets = [
                _det("K P Acheampong", "PERSON", page=0),
                _det("85 Waltings Gardens", "LOCATION", page=0),
                _det("London", "LOCATION", page=0),
                _det("10-Aug-1959", "DATE_OF_BIRTH_DMY", page=1),
                _det("NE724362D", "NI_NUMBER", page=1),
                _det("M S Alcock", "PERSON", page=3),
                _det("3 Whitworth Road", "LOCATION", page=3),
                _det("29-Aug-1960", "DATE_OF_BIRTH_DMY", page=4),
                _det("WK393925C", "NI_NUMBER", page=4),
            ]
            records = extract_with_template(dets, schema, "pension.pdf", total_pages=6)

            # Resolve
            resolver = EntityResolver()
            groups = resolver.resolve(records)

            # Deduplicate
            dedup = Deduplicator(session)
            subjects = dedup.build_subjects(groups)

            assert len(subjects) == 2

            names = sorted(s.canonical_name for s in subjects if s.canonical_name)
            assert "K P Acheampong" in names[0] or "Acheampong" in names[0]

            # Both should have address populated
            for s in subjects:
                assert s.canonical_address is not None
                assert s.pii_types_list is not None
                assert s.source_page_range is not None
        finally:
            session.close()
