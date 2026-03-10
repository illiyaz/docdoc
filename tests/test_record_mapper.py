"""Tests for app.pipeline.record_mapper — DetectionResult → PIIRecord mapping."""
from __future__ import annotations

import pytest

from app.pipeline.record_mapper import detection_to_pii_record
from app.pii.presidio_engine import DetectionResult
from app.readers.base import ExtractedBlock
from app.rra.entity_resolver import PIIRecord


def _make_block(text: str, page: int = 1) -> ExtractedBlock:
    return ExtractedBlock(text=text, page_or_sheet=page, source_path="test.pdf", file_type="pdf")


def _make_detection(
    block: ExtractedBlock,
    entity_type: str,
    start: int,
    end: int,
    score: float = 0.90,
) -> DetectionResult:
    return DetectionResult(
        block=block,
        entity_type=entity_type,
        start=start,
        end=end,
        score=score,
        pattern_used="test",
        geography="US",
        regulatory_framework="HIPAA",
    )


class TestDetectionToPiiRecord:
    """Each entity type maps to the correct raw_* field."""

    def test_person_maps_to_raw_name(self):
        block = _make_block("John Smith is a patient", page=3)
        det = _make_detection(block, "PERSON", 0, 10)
        rec = detection_to_pii_record(det, "doc-1")

        assert isinstance(rec, PIIRecord)
        assert rec.raw_name == "John Smith"
        assert rec.raw_email is None
        assert rec.raw_phone is None
        assert rec.raw_dob is None
        assert rec.raw_address is None
        assert rec.source_document_id == "doc-1"
        assert rec.page_or_sheet == 3
        assert rec.entity_type == "PERSON"

    def test_person_name_maps_to_raw_name(self):
        block = _make_block("Jane Doe", page=1)
        det = _make_detection(block, "PERSON_NAME", 0, 8)
        rec = detection_to_pii_record(det, "doc-2")
        assert rec.raw_name == "Jane Doe"

    def test_email_address_maps_to_raw_email(self):
        block = _make_block("contact: john@example.com please", page=2)
        det = _make_detection(block, "EMAIL_ADDRESS", 9, 25)
        rec = detection_to_pii_record(det, "doc-3")

        assert rec.raw_email == "john@example.com"
        assert rec.raw_name is None
        assert rec.entity_type == "EMAIL_ADDRESS"

    def test_email_variant_maps_to_raw_email(self):
        block = _make_block("EMAIL: test@test.com")
        det = _make_detection(block, "EMAIL", 7, 20)
        rec = detection_to_pii_record(det, "doc-4")
        assert rec.raw_email == "test@test.com"

    def test_phone_number_maps_to_raw_phone(self):
        block = _make_block("Call 555-123-4567 now")
        det = _make_detection(block, "PHONE_NUMBER", 5, 17)
        rec = detection_to_pii_record(det, "doc-5")

        assert rec.raw_phone == "555-123-4567"
        assert rec.raw_name is None
        assert rec.raw_email is None

    def test_phone_us_maps_to_raw_phone(self):
        block = _make_block("(800) 555-0199")
        det = _make_detection(block, "PHONE_US", 0, 14)
        rec = detection_to_pii_record(det, "doc-6")
        assert rec.raw_phone == "(800) 555-0199"

    def test_phone_intl_maps_to_raw_phone(self):
        block = _make_block("+44 20 7946 0958")
        det = _make_detection(block, "PHONE_INTL", 0, 16)
        rec = detection_to_pii_record(det, "doc-7")
        assert rec.raw_phone == "+44 20 7946 0958"

    def test_date_of_birth_maps_to_raw_dob(self):
        block = _make_block("DOB: 01/15/1990 recorded")
        det = _make_detection(block, "DATE_OF_BIRTH", 5, 15)
        rec = detection_to_pii_record(det, "doc-8")

        assert rec.raw_dob == "01/15/1990"
        assert rec.raw_name is None

    def test_dob_mdy_maps_to_raw_dob(self):
        block = _make_block("03/25/1985")
        det = _make_detection(block, "DATE_OF_BIRTH_MDY", 0, 10)
        rec = detection_to_pii_record(det, "doc-9")
        assert rec.raw_dob == "03/25/1985"

    def test_dob_dmy_maps_to_raw_dob(self):
        block = _make_block("25/03/1985")
        det = _make_detection(block, "DATE_OF_BIRTH_DMY", 0, 10)
        rec = detection_to_pii_record(det, "doc-10")
        assert rec.raw_dob == "25/03/1985"

    def test_location_maps_to_raw_address(self):
        block = _make_block("Lives at 123 Main St")
        det = _make_detection(block, "LOCATION", 9, 20)
        rec = detection_to_pii_record(det, "doc-11")

        assert rec.raw_address == {"raw": "123 Main St"}
        assert rec.raw_name is None

    def test_address_maps_to_raw_address(self):
        block = _make_block("456 Oak Ave, Springfield")
        det = _make_detection(block, "ADDRESS", 0, 24)
        rec = detection_to_pii_record(det, "doc-12")
        assert rec.raw_address == {"raw": "456 Oak Ave, Springfield"}

    def test_unknown_entity_type_all_raw_fields_none(self):
        """Entity types not in the mapping should leave all raw_* fields None."""
        block = _make_block("SSN 123-45-6789")
        det = _make_detection(block, "US_SSN", 4, 15)
        rec = detection_to_pii_record(det, "doc-13")

        assert rec.raw_name is None
        assert rec.raw_email is None
        assert rec.raw_phone is None
        assert rec.raw_dob is None
        assert rec.raw_address is None
        assert rec.entity_type == "US_SSN"
        assert rec.normalized_value == "123-45-6789"

    def test_record_id_is_unique(self):
        block = _make_block("John Smith")
        det = _make_detection(block, "PERSON", 0, 10)
        r1 = detection_to_pii_record(det, "doc-1")
        r2 = detection_to_pii_record(det, "doc-1")
        assert r1.record_id != r2.record_id

    def test_record_is_frozen(self):
        """PIIRecord is frozen=True; field assignment should raise."""
        block = _make_block("John Smith")
        det = _make_detection(block, "PERSON", 0, 10)
        rec = detection_to_pii_record(det, "doc-1")
        with pytest.raises(AttributeError):
            rec.raw_name = "changed"  # type: ignore[misc]

    def test_normalized_value_matches_detected_text(self):
        block = _make_block("Hello john@test.com world")
        det = _make_detection(block, "EMAIL_ADDRESS", 6, 19)
        rec = detection_to_pii_record(det, "doc-14")
        assert rec.normalized_value == "john@test.com"
