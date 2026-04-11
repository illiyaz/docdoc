"""Tests for document grouping and sampling logic (Step 30e-2).

Tests grouping by type, field similarity splitting, sample selection,
and edge cases — all without requiring a live LLM.
"""
from __future__ import annotations

import pytest

from app.pipeline.segregation import SegregationField, SegregationResult
from app.pipeline.grouping import (
    DocumentGroup,
    group_documents,
    _jaccard,
    _select_samples,
    _split_by_field_similarity,
    _generate_group_name,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_result(
    file_path: str = "/tmp/test.pdf",
    pii: bool = True,
    doc_type: str = "medical_form",
    confidence: float = 0.9,
    fields: list[tuple[str, str, str]] | None = None,
    subject: str | None = "patient",
    entity: str | None = None,
) -> SegregationResult:
    """Build a SegregationResult for testing."""
    seg_fields = []
    if fields:
        for name, ftype, role in fields:
            seg_fields.append(SegregationField(name=name, type=ftype, role=role))
    return SegregationResult(
        file_path=file_path,
        file_name=file_path.split("/")[-1],
        file_type="pdf",
        total_pages=1,
        pii_detected=pii,
        confidence=confidence,
        document_type=doc_type,
        primary_subject_type=subject,
        issuing_entity=entity,
        fields=seg_fields,
    )


# ---------------------------------------------------------------------------
# Jaccard similarity
# ---------------------------------------------------------------------------

class TestJaccard:

    def test_identical_sets(self):
        assert _jaccard({"a", "b"}, {"a", "b"}) == 1.0

    def test_disjoint_sets(self):
        assert _jaccard({"a", "b"}, {"c", "d"}) == 0.0

    def test_partial_overlap(self):
        assert _jaccard({"a", "b", "c"}, {"b", "c", "d"}) == pytest.approx(0.5)

    def test_empty_sets(self):
        assert _jaccard(set(), set()) == 1.0

    def test_one_empty(self):
        assert _jaccard({"a"}, set()) == 0.0


# ---------------------------------------------------------------------------
# Group naming
# ---------------------------------------------------------------------------

class TestGroupNaming:

    def test_known_type(self):
        name = _generate_group_name("medical_form", "patient", 10)
        assert name == "Medical Forms (Patient)"

    def test_unknown_type(self):
        name = _generate_group_name("weird_format", None, 5)
        assert name == "Weird Format"

    def test_no_subject(self):
        name = _generate_group_name("billing_statement", None, 3)
        assert name == "Billing Statements"


# ---------------------------------------------------------------------------
# Field similarity splitting
# ---------------------------------------------------------------------------

class TestFieldSimilarity:

    def test_single_result(self):
        results = [_make_result(fields=[("Name", "PERSON", "primary_subject")])]
        clusters = _split_by_field_similarity(results)
        assert len(clusters) == 1

    def test_similar_fields_same_cluster(self):
        r1 = _make_result(
            file_path="/tmp/a.pdf",
            fields=[
                ("Name", "PERSON", "primary_subject"),
                ("SSN", "US_SSN", "primary_subject"),
                ("DOB", "DATE_OF_BIRTH", "primary_subject"),
            ],
        )
        r2 = _make_result(
            file_path="/tmp/b.pdf",
            fields=[
                ("Name", "PERSON", "primary_subject"),
                ("SSN", "US_SSN", "primary_subject"),
                ("Address", "LOCATION", "primary_subject"),
            ],
        )
        clusters = _split_by_field_similarity([r1, r2])
        # Jaccard({PERSON,US_SSN,DOB}, {PERSON,US_SSN,LOCATION}) = 2/4 = 0.5
        # But threshold is 0.6, so they might split
        # Actually 2 common out of 4 total = 0.5 < 0.6 — separate clusters
        assert len(clusters) == 2

    def test_identical_fields_same_cluster(self):
        r1 = _make_result(
            file_path="/tmp/a.pdf",
            fields=[
                ("Name", "PERSON", "primary_subject"),
                ("SSN", "US_SSN", "primary_subject"),
            ],
        )
        r2 = _make_result(
            file_path="/tmp/b.pdf",
            fields=[
                ("Patient Name", "PERSON", "primary_subject"),
                ("Social Security", "US_SSN", "primary_subject"),
            ],
        )
        clusters = _split_by_field_similarity([r1, r2])
        # Both have {PERSON, US_SSN} — Jaccard = 1.0
        assert len(clusters) == 1
        assert len(clusters[0]) == 2

    def test_completely_different_fields(self):
        r1 = _make_result(
            file_path="/tmp/a.pdf",
            fields=[("Name", "PERSON", "primary_subject")],
        )
        r2 = _make_result(
            file_path="/tmp/b.pdf",
            fields=[("Account", "BANK_ACCOUNT", "primary_subject")],
        )
        clusters = _split_by_field_similarity([r1, r2])
        assert len(clusters) == 2


# ---------------------------------------------------------------------------
# Sample selection
# ---------------------------------------------------------------------------

class TestSampleSelection:

    def test_small_group_returns_all(self):
        results = [
            _make_result(file_path=f"/tmp/{i}.pdf", confidence=0.9 - i * 0.1)
            for i in range(3)
        ]
        samples = _select_samples(results, sample_size=5)
        assert len(samples) == 3

    def test_large_group_respects_limit(self):
        results = [
            _make_result(file_path=f"/tmp/{i}.pdf", confidence=0.5 + i * 0.01)
            for i in range(100)
        ]
        samples = _select_samples(results, sample_size=5)
        assert len(samples) == 5

    def test_includes_highest_and_lowest_confidence(self):
        results = [
            _make_result(file_path="/tmp/high.pdf", confidence=0.99),
            _make_result(file_path="/tmp/mid.pdf", confidence=0.75),
            _make_result(file_path="/tmp/low.pdf", confidence=0.50),
        ]
        samples = _select_samples(results, sample_size=2)
        paths = [s.file_path for s in samples]
        assert "/tmp/high.pdf" in paths
        assert "/tmp/low.pdf" in paths

    def test_diverse_entities(self):
        results = [
            _make_result(file_path="/tmp/a.pdf", entity="Hospital A", confidence=0.9),
            _make_result(file_path="/tmp/b.pdf", entity="Hospital B", confidence=0.85),
            _make_result(file_path="/tmp/c.pdf", entity="Hospital A", confidence=0.8),
            _make_result(file_path="/tmp/d.pdf", entity="Hospital C", confidence=0.75),
            _make_result(file_path="/tmp/e.pdf", entity="Hospital A", confidence=0.7),
            _make_result(file_path="/tmp/f.pdf", entity="Hospital B", confidence=0.65),
        ]
        samples = _select_samples(results, sample_size=4)
        entities = set(s.issuing_entity for s in samples)
        # Should include representatives from multiple entities
        assert len(entities) >= 2


# ---------------------------------------------------------------------------
# Main grouping logic
# ---------------------------------------------------------------------------

class TestGroupDocuments:

    def test_empty_input(self):
        groups = group_documents([])
        assert groups == []

    def test_all_pii_same_type(self):
        results = [
            _make_result(
                file_path=f"/tmp/med{i}.pdf",
                pii=True,
                doc_type="medical_form",
                fields=[("Name", "PERSON", "primary_subject"), ("SSN", "US_SSN", "primary_subject")],
            )
            for i in range(10)
        ]
        groups = group_documents(results)

        assert len(groups) == 1
        assert groups[0].is_pii is True
        assert groups[0].file_count == 10
        assert groups[0].document_type == "medical_form"
        assert "PERSON" in groups[0].field_inventory
        assert "US_SSN" in groups[0].field_inventory

    def test_pii_and_non_pii_separate(self):
        results = [
            _make_result(file_path="/tmp/pii1.pdf", pii=True, doc_type="medical_form"),
            _make_result(file_path="/tmp/pii2.pdf", pii=True, doc_type="medical_form"),
            _make_result(file_path="/tmp/nopii1.pdf", pii=False, doc_type="shipping_document"),
            _make_result(file_path="/tmp/nopii2.pdf", pii=False, doc_type="invoice"),
        ]
        groups = group_documents(results)

        assert len(groups) == 2  # one PII, one non-PII
        pii_groups = [g for g in groups if g.is_pii]
        non_pii_groups = [g for g in groups if not g.is_pii]
        assert len(pii_groups) == 1
        assert pii_groups[0].file_count == 2
        assert len(non_pii_groups) == 1
        assert non_pii_groups[0].file_count == 2
        assert non_pii_groups[0].group_name == "Non-PII Documents"

    def test_different_doc_types_different_groups(self):
        results = [
            _make_result(file_path="/tmp/med.pdf", pii=True, doc_type="medical_form"),
            _make_result(file_path="/tmp/tax.pdf", pii=True, doc_type="tax_form"),
            _make_result(file_path="/tmp/loan.pdf", pii=True, doc_type="loan_application"),
        ]
        groups = group_documents(results)

        pii_groups = [g for g in groups if g.is_pii]
        assert len(pii_groups) == 3
        types = {g.document_type for g in pii_groups}
        assert types == {"medical_form", "tax_form", "loan_application"}

    def test_sorting_pii_first_largest_first(self):
        results = [
            _make_result(file_path=f"/tmp/small{i}.pdf", pii=True, doc_type="tax_form")
            for i in range(2)
        ] + [
            _make_result(file_path=f"/tmp/big{i}.pdf", pii=True, doc_type="medical_form")
            for i in range(10)
        ] + [
            _make_result(file_path="/tmp/nopii.pdf", pii=False, doc_type="invoice"),
        ]
        groups = group_documents(results)

        # PII groups first, largest first
        assert groups[0].is_pii is True
        assert groups[0].file_count == 10  # medical_form is largest
        assert groups[-1].is_pii is False  # non-PII last

    def test_samples_selected(self):
        results = [
            _make_result(file_path=f"/tmp/{i}.pdf", pii=True, doc_type="pay_stub")
            for i in range(50)
        ]
        groups = group_documents(results, sample_size=5)

        assert len(groups) == 1
        assert len(groups[0].sample_file_paths) == 5
        assert groups[0].file_count == 50

    def test_single_file(self):
        results = [
            _make_result(
                file_path="/tmp/only.pdf",
                pii=True,
                doc_type="billing_statement",
                confidence=0.95,
                entity="C&R Vision",
            ),
        ]
        groups = group_documents(results)

        assert len(groups) == 1
        assert groups[0].file_count == 1
        assert groups[0].sample_file_paths == ["/tmp/only.pdf"]

    def test_role_summary(self):
        results = [
            _make_result(
                file_path=f"/tmp/{i}.pdf",
                pii=True,
                doc_type="school_record",
                fields=[
                    ("Student Name", "PERSON", "primary_subject"),
                    ("Parent Name", "PERSON", "secondary_contact"),
                    ("Student ID", "STUDENT_ID", "primary_subject"),
                ],
            )
            for i in range(5)
        ]
        groups = group_documents(results)

        assert len(groups) == 1
        # PERSON appears as both roles; most common for PERSON type depends on count
        # 5 primary_subject + 5 secondary_contact = tie, but primary_subject comes first
        assert "STUDENT_ID" in groups[0].role_summary
        assert groups[0].role_summary["STUDENT_ID"] == "primary_subject"

    def test_confidence_stats(self):
        results = [
            _make_result(file_path="/tmp/a.pdf", confidence=0.95),
            _make_result(file_path="/tmp/b.pdf", confidence=0.85),
            _make_result(file_path="/tmp/c.pdf", confidence=0.75),
        ]
        groups = group_documents(results)

        assert groups[0].confidence_avg == pytest.approx(0.85, abs=0.01)
        assert groups[0].confidence_min == 0.75

    def test_issuing_entities_aggregated(self):
        results = [
            _make_result(file_path="/tmp/a.pdf", entity="Hospital A"),
            _make_result(file_path="/tmp/b.pdf", entity="Hospital B"),
            _make_result(file_path="/tmp/c.pdf", entity="Hospital A"),
        ]
        groups = group_documents(results)

        assert sorted(groups[0].issuing_entities) == ["Hospital A", "Hospital B"]

    def test_all_non_pii(self):
        results = [
            _make_result(file_path=f"/tmp/{i}.pdf", pii=False, doc_type="invoice")
            for i in range(5)
        ]
        groups = group_documents(results)

        assert len(groups) == 1
        assert groups[0].is_pii is False
        assert groups[0].group_name == "Non-PII Documents"
        assert groups[0].file_count == 5


# ---------------------------------------------------------------------------
# DocumentGroup dataclass
# ---------------------------------------------------------------------------

class TestDocumentGroup:

    def test_to_dict(self):
        g = DocumentGroup(
            group_id="test-123",
            group_name="Test Group",
            document_type="medical_form",
            is_pii=True,
            file_count=10,
        )
        d = g.to_dict()
        assert d["group_id"] == "test-123"
        assert d["is_pii"] is True
        assert d["status"] == "pending_review"

    def test_default_status(self):
        g = DocumentGroup(
            group_id="x",
            group_name="x",
            document_type="x",
            is_pii=True,
        )
        assert g.status == "pending_review"
