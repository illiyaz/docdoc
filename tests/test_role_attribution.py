"""Tests for entity_role plumbing through the extraction pipeline (Step 30e-4).

Verifies that entity_role flows from:
1. DetectionResult → PIIRecord (via record_mapper)
2. FieldMapping → CoordinateExtractor → PIIRecord
3. Composite records aggregate role from detections
4. Merge prevention uses entity_role correctly
5. FieldMapping serialization/deserialization preserves entity_role
"""
from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

import pytest


# ---------------------------------------------------------------------------
# 1. DetectionResult has entity_role
# ---------------------------------------------------------------------------


class TestDetectionResultRole:

    def test_presidio_detection_result_has_entity_role(self):
        """Presidio DetectionResult should have an entity_role field."""
        from app.pii.presidio_engine import DetectionResult

        # Create a mock ExtractedBlock
        @dataclass
        class MockBlock:
            text: str = "John Smith 123-45-6789"
            page_or_sheet: int = 0

        det = DetectionResult(
            block=MockBlock(),
            entity_type="PERSON",
            start=0,
            end=10,
            score=0.95,
            pattern_used="",
            geography="US",
            regulatory_framework="HIPAA",
            entity_role="primary_subject",
        )
        assert det.entity_role == "primary_subject"

    def test_entity_role_defaults_to_none(self):
        """entity_role should default to None if not specified."""
        from app.pii.presidio_engine import DetectionResult

        @dataclass
        class MockBlock:
            text: str = "John Smith"
            page_or_sheet: int = 0

        det = DetectionResult(
            block=MockBlock(),
            entity_type="PERSON",
            start=0,
            end=10,
            score=0.95,
            pattern_used="",
            geography="US",
            regulatory_framework="HIPAA",
        )
        assert det.entity_role is None


# ---------------------------------------------------------------------------
# 2. record_mapper passes entity_role through
# ---------------------------------------------------------------------------


class TestRecordMapperRole:

    def _make_detection(self, entity_type="PERSON", text="John Smith", role=None):
        from app.pii.presidio_engine import DetectionResult

        @dataclass
        class MockBlock:
            text: str = ""
            page_or_sheet: int = 0

        block = MockBlock(text=text)
        return DetectionResult(
            block=block,
            entity_type=entity_type,
            start=0,
            end=len(text),
            score=0.95,
            pattern_used="",
            geography="US",
            regulatory_framework="HIPAA",
            entity_role=role,
        )

    def test_single_detection_copies_role(self):
        """detection_to_pii_record should copy entity_role from DetectionResult."""
        from app.pipeline.record_mapper import detection_to_pii_record

        det = self._make_detection(role="primary_subject")
        rec = detection_to_pii_record(det, "doc-001")
        assert rec.entity_role == "primary_subject"

    def test_single_detection_none_role(self):
        """detection_to_pii_record with no role should produce entity_role=None."""
        from app.pipeline.record_mapper import detection_to_pii_record

        det = self._make_detection(role=None)
        rec = detection_to_pii_record(det, "doc-001")
        assert rec.entity_role is None

    def test_composite_record_majority_role(self):
        """build_composite_record should use majority-vote role."""
        from app.pipeline.record_mapper import build_composite_record

        dets = [
            self._make_detection("PERSON", "John Smith", "primary_subject"),
            self._make_detection("EMAIL_ADDRESS", "john@example.com", "primary_subject"),
            self._make_detection("PHONE_NUMBER", "555-123-4567", "secondary_contact"),
        ]
        rec = build_composite_record(dets, "doc-001")
        # 2 primary_subject vs 1 secondary_contact → primary_subject wins
        assert rec.entity_role == "primary_subject"

    def test_composite_record_all_none_role(self):
        """build_composite_record with all None roles should produce None."""
        from app.pipeline.record_mapper import build_composite_record

        dets = [
            self._make_detection("PERSON", "John Smith", None),
            self._make_detection("EMAIL_ADDRESS", "john@example.com", None),
        ]
        rec = build_composite_record(dets, "doc-001")
        assert rec.entity_role is None

    def test_composite_record_mixed_none_role(self):
        """build_composite_record with some None roles ignores Nones."""
        from app.pipeline.record_mapper import build_composite_record

        dets = [
            self._make_detection("PERSON", "John Smith", "institutional"),
            self._make_detection("EMAIL_ADDRESS", "john@example.com", None),
        ]
        rec = build_composite_record(dets, "doc-001")
        assert rec.entity_role == "institutional"


# ---------------------------------------------------------------------------
# 3. FieldMapping has entity_role
# ---------------------------------------------------------------------------


class TestFieldMappingRole:

    def test_field_mapping_has_entity_role(self):
        """FieldMapping should have entity_role field."""
        from app.structure.document_schema import FieldMapping

        fm = FieldMapping(
            field_type="PERSON",
            anchor_text="Name:",
            spatial_relationship="same_line_right",
            entity_role="primary_subject",
        )
        assert fm.entity_role == "primary_subject"

    def test_field_mapping_entity_role_defaults_none(self):
        """FieldMapping entity_role should default to None."""
        from app.structure.document_schema import FieldMapping

        fm = FieldMapping(
            field_type="PERSON",
            anchor_text="Name:",
            spatial_relationship="same_line_right",
        )
        assert fm.entity_role is None

    def test_field_mapping_roundtrip_serialization(self):
        """FieldMapping should survive dict serialization and reconstruction."""
        from app.structure.document_schema import FieldMapping

        fm = FieldMapping(
            field_type="PERSON",
            anchor_text="Name:",
            spatial_relationship="same_line_right",
            value_pattern=None,
            sample_bbox=[100, 200, 300, 220],
            line_count=1,
            skip_pattern=None,
            entity_role="primary_subject",
        )

        # Serialize (matching two_phase.py pattern)
        fm_dict = {
            "field_type": fm.field_type,
            "anchor_text": fm.anchor_text,
            "spatial_relationship": fm.spatial_relationship,
            "value_pattern": fm.value_pattern,
            "sample_bbox": fm.sample_bbox,
            "line_count": fm.line_count,
            "skip_pattern": fm.skip_pattern,
            "entity_role": getattr(fm, "entity_role", None),
        }

        # Deserialize
        fm2 = FieldMapping(**fm_dict)
        assert fm2.entity_role == "primary_subject"
        assert fm2.field_type == "PERSON"

    def test_field_mapping_backward_compat_no_role(self):
        """Old field map dicts without entity_role should still work."""
        from app.structure.document_schema import FieldMapping

        old_dict = {
            "field_type": "PERSON",
            "anchor_text": "Name:",
            "spatial_relationship": "same_line_right",
        }
        fm = FieldMapping(**old_dict)
        assert fm.entity_role is None


# ---------------------------------------------------------------------------
# 4. Merge prevention uses entity_role
# ---------------------------------------------------------------------------


class TestMergePreventionWithRole:

    def test_cross_role_merge_blocked(self):
        """Records with primary_subject vs institutional roles should not merge."""
        from app.rra.entity_resolver import PIIRecord, build_confidence

        r1 = PIIRecord(
            record_id=str(uuid4()),
            entity_type="PERSON",
            normalized_value="John Smith",
            raw_name="John Smith",
            raw_email="john@example.com",
            source_document_id="doc1",
            entity_role="primary_subject",
        )
        r2 = PIIRecord(
            record_id=str(uuid4()),
            entity_type="PERSON",
            normalized_value="John Smith",
            raw_name="John Smith",
            raw_email="john@example.com",
            source_document_id="doc1",
            entity_role="institutional",
        )
        conf = build_confidence(r1, r2)
        assert conf == 0.0, "primary_subject + institutional should never merge"

    def test_cross_role_merge_blocked_provider(self):
        """Records with primary_subject vs provider roles should not merge."""
        from app.rra.entity_resolver import PIIRecord, build_confidence

        r1 = PIIRecord(
            record_id=str(uuid4()),
            entity_type="PERSON",
            normalized_value="Jane Doe",
            raw_name="Jane Doe",
            source_document_id="doc1",
            entity_role="primary_subject",
        )
        r2 = PIIRecord(
            record_id=str(uuid4()),
            entity_type="PERSON",
            normalized_value="Jane Doe",
            raw_name="Jane Doe",
            source_document_id="doc1",
            entity_role="provider",
        )
        conf = build_confidence(r1, r2)
        assert conf == 0.0, "primary_subject + provider should never merge"

    def test_same_role_merge_allowed(self):
        """Records with same role should be allowed to merge (based on data match)."""
        from app.rra.entity_resolver import PIIRecord, build_confidence

        r1 = PIIRecord(
            record_id=str(uuid4()),
            entity_type="PERSON",
            normalized_value="John Smith",
            raw_name="John Smith",
            raw_government_id="123-45-6789",
            source_document_id="doc1",
            entity_role="primary_subject",
            entity_types_found=("PERSON", "US_SSN"),
        )
        r2 = PIIRecord(
            record_id=str(uuid4()),
            entity_type="PERSON",
            normalized_value="John Smith",
            raw_name="John Smith",
            raw_government_id="123-45-6789",
            source_document_id="doc2",
            entity_role="primary_subject",
            entity_types_found=("PERSON", "US_SSN"),
        )
        conf = build_confidence(r1, r2)
        assert conf > 0.0, "Same role with matching data should merge"

    def test_none_role_does_not_block(self):
        """Records with None role should not trigger merge prevention."""
        from app.rra.entity_resolver import PIIRecord, build_confidence

        r1 = PIIRecord(
            record_id=str(uuid4()),
            entity_type="PERSON",
            normalized_value="John Smith",
            raw_name="John Smith",
            raw_government_id="123-45-6789",
            source_document_id="doc1",
            entity_role=None,
            entity_types_found=("PERSON", "US_SSN"),
        )
        r2 = PIIRecord(
            record_id=str(uuid4()),
            entity_type="PERSON",
            normalized_value="John Smith",
            raw_name="John Smith",
            raw_government_id="123-45-6789",
            source_document_id="doc2",
            entity_role=None,
            entity_types_found=("PERSON", "US_SSN"),
        )
        conf = build_confidence(r1, r2)
        assert conf > 0.0, "None roles should not block merging"


# ---------------------------------------------------------------------------
# 5. Safety: no raw PII in role fields
# ---------------------------------------------------------------------------


class TestRoleSafety:

    def test_entity_role_contains_no_pii(self):
        """entity_role must only contain category labels, never PII values."""
        valid_roles = {
            "primary_subject", "secondary_contact", "institutional",
            "provider", "related_party", None,
        }
        from app.rra.entity_resolver import PIIRecord

        for role in valid_roles:
            rec = PIIRecord(
                record_id=str(uuid4()),
                entity_type="PERSON",
                normalized_value="test",
                entity_role=role,
            )
            if rec.entity_role is not None:
                # Role should be a short category label
                assert len(rec.entity_role) < 30
                assert not any(c.isdigit() for c in rec.entity_role)
