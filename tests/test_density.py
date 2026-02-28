"""Tests for the density scoring task (Step 4 — PII density summaries).

Covers:
- classify_entity_type() backward-compat single-category function
- classify_entity_categories() multi-category mapping
- compute_confidence() pure function: high/partial/low thresholds
- _compute_density() pure function: end-to-end density computation (multi-category)
- DensityTask.run() ORM integration: per-document and project-level rows
- DensityTask with empty extractions
- Density API endpoint returns correct data
"""
from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import (
    DensitySummary,
    Document,
    Extraction,
    IngestionRun,
    Project,
)
from app.tasks.density import (
    ConfidenceResult,
    DensityTask,
    ExtractionInput,
    _compute_density,
    classify_entity_categories,
    classify_entity_type,
    compute_confidence,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_session():
    """In-memory SQLite session with all tables created."""
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = SessionLocal()
    yield session
    session.close()


def _make_project(db: Session, name: str = "Test Project") -> Project:
    project = Project(id=uuid4(), name=name)
    db.add(project)
    db.flush()
    return project


def _make_ingestion_run(db: Session, project_id=None) -> IngestionRun:
    run = IngestionRun(
        id=uuid4(),
        project_id=project_id,
        source_path="/tmp/test",
        config_hash="abc123",
        code_version="1.0.0",
        initiated_by="test",
        mode="strict",
        status="pending",
    )
    db.add(run)
    db.flush()
    return run


def _make_document(
    db: Session,
    ingestion_run_id,
    file_name: str = "test.pdf",
    file_type: str = "pdf",
) -> Document:
    sha = hashlib.sha256(f"{file_name}-{uuid4()}".encode()).hexdigest()
    doc = Document(
        id=uuid4(),
        ingestion_run_id=ingestion_run_id,
        source_path=f"/tmp/{file_name}",
        file_name=file_name,
        file_type=file_type,
        size_bytes=1024,
        sha256=sha,
        status="discovered",
    )
    db.add(doc)
    db.flush()
    return doc


def _make_extraction(
    db: Session,
    document_id,
    pii_type: str = "SSN",
    confidence_score: float | None = 0.90,
) -> Extraction:
    ext = Extraction(
        id=uuid4(),
        document_id=document_id,
        pii_type=pii_type,
        sensitivity="high",
        hashed_value=hashlib.sha256(f"value-{uuid4()}".encode()).hexdigest(),
        confidence_score=confidence_score,
    )
    db.add(ext)
    db.flush()
    return ext


# ===========================================================================
# classify_entity_type() — pure function tests
# ===========================================================================


class TestClassifyEntityType:
    """Verify entity type → primary category mapping (backward compat)."""

    # PHI types — primary category is PHI
    @pytest.mark.parametrize(
        "entity_type",
        ["MRN", "NPI", "DEA_NUMBER", "HICN", "ICD10_CODE",
         "HEALTH_PLAN_BENEFICIARY", "MEDICARE_BENEFICIARY_ID"],
    )
    def test_phi_types(self, entity_type: str) -> None:
        assert classify_entity_type(entity_type) == "PHI"

    # PFI types — primary category is PFI
    @pytest.mark.parametrize(
        "entity_type",
        ["CREDIT_CARD", "BANK_ROUTING_US", "FINANCIAL_ACCOUNT_PAIR",
         "IBAN"],
    )
    def test_pfi_types(self, entity_type: str) -> None:
        assert classify_entity_type(entity_type) == "PFI"

    # PII types — primary category is PII
    @pytest.mark.parametrize(
        "entity_type",
        ["SSN", "EMAIL", "PHONE_US", "PHONE_INTL", "DRIVER_LICENSE_US",
         "DATE_OF_BIRTH_MDY", "PASSPORT_ICAO", "AADHAAR", "PAN",
         "NATIONAL_INSURANCE_UK", "STUDENT_ID", "BIOMETRIC_IDENTIFIER"],
    )
    def test_pii_types(self, entity_type: str) -> None:
        assert classify_entity_type(entity_type) == "PII"

    def test_case_insensitive(self) -> None:
        """Entity types should match case-insensitively."""
        assert classify_entity_type("mrn") == "PHI"
        assert classify_entity_type("credit_card") == "PFI"
        assert classify_entity_type("ssn") == "PII"

    def test_unknown_type_defaults_to_pii(self) -> None:
        assert classify_entity_type("UNKNOWN") == "PII"
        assert classify_entity_type("") == "PII"
        assert classify_entity_type("SOME_NEW_TYPE") == "PII"


class TestClassifyEntityCategories:
    """Verify multi-category classification."""

    def test_ssn_maps_to_pii_and_spii(self) -> None:
        cats = classify_entity_categories("SSN")
        assert "PII" in cats
        assert "SPII" in cats

    def test_credit_card_maps_to_pfi_and_pci(self) -> None:
        cats = classify_entity_categories("CREDIT_CARD")
        assert "PFI" in cats
        assert "PCI" in cats

    def test_mrn_maps_to_phi_only(self) -> None:
        assert classify_entity_categories("MRN") == ["PHI"]

    def test_email_maps_to_pii_only(self) -> None:
        assert classify_entity_categories("EMAIL") == ["PII"]

    def test_password_maps_to_credentials(self) -> None:
        assert classify_entity_categories("PASSWORD") == ["CREDENTIALS"]

    def test_unknown_defaults_to_pii(self) -> None:
        assert classify_entity_categories("UNKNOWN") == ["PII"]


# ===========================================================================
# compute_confidence() — pure function tests
# ===========================================================================


class TestComputeConfidence:
    """Verify confidence aggregation logic."""

    def test_high_confidence(self) -> None:
        """All scores >= 0.75 → high."""
        scores = [0.90, 0.85, 0.80, 0.75, 0.95]
        result = compute_confidence(scores)
        assert result.label == "high"

    def test_high_threshold_boundary(self) -> None:
        """Exactly 81% high scores → high (>80% threshold)."""
        # 81 high, 19 medium = 81% high
        scores = [0.80] * 81 + [0.60] * 19
        result = compute_confidence(scores)
        assert result.label == "high"

    def test_high_threshold_not_met(self) -> None:
        """Exactly 80% high scores → NOT high (must be >80%)."""
        # 80 high, 20 medium = 80% → not > 80%, so partial
        scores = [0.80] * 80 + [0.60] * 20
        result = compute_confidence(scores)
        assert result.label == "partial"

    def test_low_confidence(self) -> None:
        """Many low scores → low."""
        # 40 low, 60 high = 40% low → >30% → low
        scores = [0.30] * 40 + [0.80] * 60
        result = compute_confidence(scores)
        assert result.label == "low"
        assert any("low-confidence" in n for n in result.notes)
        assert any("OCR" in n for n in result.notes)

    def test_low_threshold_boundary(self) -> None:
        """Exactly 31% low → low (>30% threshold)."""
        scores = [0.40] * 31 + [0.70] * 69
        result = compute_confidence(scores)
        assert result.label == "low"

    def test_low_threshold_not_met(self) -> None:
        """Exactly 30% low → NOT low (must be >30%)."""
        # 30 low, 70 medium → 30% low which is not > 30% → partial
        scores = [0.40] * 30 + [0.70] * 70
        result = compute_confidence(scores)
        assert result.label == "partial"

    def test_partial_confidence(self) -> None:
        """Mixed scores → partial."""
        scores = [0.90, 0.60, 0.70, 0.50, 0.80]
        result = compute_confidence(scores)
        assert result.label == "partial"

    def test_empty_scores(self) -> None:
        """No scores → high with note."""
        result = compute_confidence([])
        assert result.label == "high"
        assert len(result.notes) > 0

    def test_none_scores(self) -> None:
        """All None scores → partial with notes about missing."""
        result = compute_confidence([None, None, None])
        assert result.label == "partial"
        assert any("no confidence score" in n for n in result.notes)

    def test_mixed_none_and_valid(self) -> None:
        """Some None scores mixed with valid → counts None separately."""
        scores = [0.90, None, 0.85, 0.80]
        result = compute_confidence(scores)
        # 3 valid, all >= 0.75 → 100% high → "high"
        assert result.label == "high"
        assert any("1 extraction(s) with no confidence score" in n for n in result.notes)

    def test_low_confidence_notes_mention_count(self) -> None:
        """Notes should mention the number of low-confidence extractions."""
        scores = [0.30, 0.40, 0.90, 0.85, 0.80]
        result = compute_confidence(scores)
        assert any("2 low-confidence" in n for n in result.notes)


# ===========================================================================
# _compute_density() — pure function tests
# ===========================================================================


class TestComputeDensity:
    """Verify end-to-end density computation."""

    def test_basic_density(self) -> None:
        doc_id = uuid4()
        inputs = [
            ExtractionInput(document_id=doc_id, pii_type="SSN", confidence_score=0.90),
            ExtractionInput(document_id=doc_id, pii_type="EMAIL", confidence_score=0.85),
            ExtractionInput(document_id=doc_id, pii_type="MRN", confidence_score=0.80),
            ExtractionInput(document_id=doc_id, pii_type="CREDIT_CARD", confidence_score=0.75),
        ]
        total, by_cat, by_typ, conf = _compute_density(inputs)

        assert total == 4
        # SSN -> PII+SPII, EMAIL -> PII, MRN -> PHI, CREDIT_CARD -> PFI+PCI
        assert by_cat["PII"] == 2       # SSN + EMAIL
        assert by_cat["SPII"] == 1      # SSN
        assert by_cat["PHI"] == 1       # MRN
        assert by_cat["PFI"] == 1       # CREDIT_CARD
        assert by_cat["PCI"] == 1       # CREDIT_CARD
        assert by_typ == {"SSN": 1, "EMAIL": 1, "MRN": 1, "CREDIT_CARD": 1}
        assert conf.label == "high"

    def test_empty_extractions(self) -> None:
        total, by_cat, by_typ, conf = _compute_density([])
        assert total == 0
        assert by_cat == {}
        assert by_typ == {}
        assert conf.label == "high"

    def test_duplicate_types(self) -> None:
        doc_id = uuid4()
        inputs = [
            ExtractionInput(document_id=doc_id, pii_type="SSN", confidence_score=0.90),
            ExtractionInput(document_id=doc_id, pii_type="SSN", confidence_score=0.85),
            ExtractionInput(document_id=doc_id, pii_type="SSN", confidence_score=0.80),
        ]
        total, by_cat, by_typ, conf = _compute_density(inputs)
        assert total == 3
        assert by_typ == {"SSN": 3}
        # SSN -> PII + SPII, so 3 of each
        assert by_cat == {"PII": 3, "SPII": 3}

    def test_all_phi_types(self) -> None:
        doc_id = uuid4()
        inputs = [
            ExtractionInput(document_id=doc_id, pii_type="MRN", confidence_score=0.80),
            ExtractionInput(document_id=doc_id, pii_type="NPI", confidence_score=0.70),
            ExtractionInput(document_id=doc_id, pii_type="HICN", confidence_score=0.75),
        ]
        total, by_cat, by_typ, conf = _compute_density(inputs)
        assert by_cat == {"PHI": 3}
        assert "PII" not in by_cat


# ===========================================================================
# DensityTask.run() — ORM integration
# ===========================================================================


class TestDensityTask:
    """Verify DensityTask persists density summaries correctly."""

    def test_creates_per_document_and_project_summaries(self, db_session: Session) -> None:
        """Should create one row per document + one project-level row."""
        project = _make_project(db_session)
        run = _make_ingestion_run(db_session, project_id=project.id)
        doc1 = _make_document(db_session, run.id, "a.pdf", "pdf")
        doc2 = _make_document(db_session, run.id, "b.pdf", "pdf")

        inputs = [
            ExtractionInput(document_id=doc1.id, pii_type="SSN", confidence_score=0.90),
            ExtractionInput(document_id=doc1.id, pii_type="EMAIL", confidence_score=0.85),
            ExtractionInput(document_id=doc2.id, pii_type="MRN", confidence_score=0.80),
        ]

        task = DensityTask(db_session)
        summaries = task.run(project.id, extraction_inputs=inputs)

        # 2 per-document + 1 project-level = 3
        assert len(summaries) == 3

        # Project-level summary has document_id=NULL
        project_summary = [s for s in summaries if s.document_id is None]
        assert len(project_summary) == 1
        assert project_summary[0].total_entities == 3

        # Per-document summaries
        doc_summaries = [s for s in summaries if s.document_id is not None]
        assert len(doc_summaries) == 2

    def test_project_level_summary_has_null_document_id(self, db_session: Session) -> None:
        project = _make_project(db_session)
        run = _make_ingestion_run(db_session, project_id=project.id)
        doc = _make_document(db_session, run.id, "a.pdf", "pdf")

        inputs = [
            ExtractionInput(document_id=doc.id, pii_type="SSN", confidence_score=0.90),
        ]

        summaries = DensityTask(db_session).run(project.id, extraction_inputs=inputs)
        project_summary = [s for s in summaries if s.document_id is None]
        assert len(project_summary) == 1
        assert project_summary[0].document_id is None

    def test_per_document_totals_correct(self, db_session: Session) -> None:
        project = _make_project(db_session)
        run = _make_ingestion_run(db_session, project_id=project.id)
        doc1 = _make_document(db_session, run.id, "a.pdf", "pdf")
        doc2 = _make_document(db_session, run.id, "b.pdf", "pdf")

        inputs = [
            ExtractionInput(document_id=doc1.id, pii_type="SSN", confidence_score=0.90),
            ExtractionInput(document_id=doc1.id, pii_type="EMAIL", confidence_score=0.85),
            ExtractionInput(document_id=doc1.id, pii_type="MRN", confidence_score=0.80),
            ExtractionInput(document_id=doc2.id, pii_type="CREDIT_CARD", confidence_score=0.75),
        ]

        summaries = DensityTask(db_session).run(project.id, extraction_inputs=inputs)
        doc_summaries = {s.document_id: s for s in summaries if s.document_id is not None}

        assert doc_summaries[doc1.id].total_entities == 3
        assert doc_summaries[doc2.id].total_entities == 1

    def test_by_category_and_by_type_persisted(self, db_session: Session) -> None:
        project = _make_project(db_session)
        run = _make_ingestion_run(db_session, project_id=project.id)
        doc = _make_document(db_session, run.id, "a.pdf", "pdf")

        inputs = [
            ExtractionInput(document_id=doc.id, pii_type="SSN", confidence_score=0.90),
            ExtractionInput(document_id=doc.id, pii_type="MRN", confidence_score=0.80),
            ExtractionInput(document_id=doc.id, pii_type="CREDIT_CARD", confidence_score=0.75),
        ]

        summaries = DensityTask(db_session).run(project.id, extraction_inputs=inputs)
        project_summary = [s for s in summaries if s.document_id is None][0]

        # SSN -> PII+SPII, MRN -> PHI, CREDIT_CARD -> PFI+PCI
        assert project_summary.by_category == {"PII": 1, "SPII": 1, "PHI": 1, "PFI": 1, "PCI": 1}
        assert project_summary.by_type == {"SSN": 1, "MRN": 1, "CREDIT_CARD": 1}

    def test_confidence_persisted(self, db_session: Session) -> None:
        project = _make_project(db_session)
        run = _make_ingestion_run(db_session, project_id=project.id)
        doc = _make_document(db_session, run.id, "a.pdf", "pdf")

        inputs = [
            ExtractionInput(document_id=doc.id, pii_type="SSN", confidence_score=0.90),
            ExtractionInput(document_id=doc.id, pii_type="EMAIL", confidence_score=0.85),
        ]

        summaries = DensityTask(db_session).run(project.id, extraction_inputs=inputs)
        project_summary = [s for s in summaries if s.document_id is None][0]

        assert project_summary.confidence == "high"
        assert project_summary.confidence_notes is not None

    def test_empty_extractions_still_creates_project_summary(self, db_session: Session) -> None:
        """Running with no extractions should still create a project-level summary."""
        project = _make_project(db_session)

        summaries = DensityTask(db_session).run(project.id, extraction_inputs=[])

        assert len(summaries) == 1
        assert summaries[0].document_id is None
        assert summaries[0].total_entities == 0

    def test_summaries_persisted_in_db(self, db_session: Session) -> None:
        """After run(), summaries are queryable from the DB."""
        project = _make_project(db_session)
        run = _make_ingestion_run(db_session, project_id=project.id)
        doc = _make_document(db_session, run.id, "a.pdf", "pdf")

        inputs = [
            ExtractionInput(document_id=doc.id, pii_type="SSN", confidence_score=0.90),
        ]

        DensityTask(db_session).run(project.id, extraction_inputs=inputs)
        db_session.commit()

        from sqlalchemy import select

        all_summaries = db_session.execute(
            select(DensitySummary).where(DensitySummary.project_id == project.id)
        ).scalars().all()

        # 1 per-document + 1 project-level
        assert len(all_summaries) == 2

    def test_loads_from_db_when_no_inputs_provided(self, db_session: Session) -> None:
        """DensityTask queries DB extractions when extraction_inputs is None."""
        project = _make_project(db_session)
        run = _make_ingestion_run(db_session, project_id=project.id)
        doc = _make_document(db_session, run.id, "a.pdf", "pdf")
        _make_extraction(db_session, doc.id, "SSN", 0.90)
        _make_extraction(db_session, doc.id, "EMAIL", 0.85)
        db_session.commit()

        summaries = DensityTask(db_session).run(project.id, extraction_inputs=None)

        # 1 per-document + 1 project-level
        assert len(summaries) == 2
        project_summary = [s for s in summaries if s.document_id is None][0]
        assert project_summary.total_entities == 2

    def test_low_confidence_persisted(self, db_session: Session) -> None:
        """A batch of low-confidence extractions gets 'low' label."""
        project = _make_project(db_session)
        run = _make_ingestion_run(db_session, project_id=project.id)
        doc = _make_document(db_session, run.id, "a.pdf", "pdf")

        # 40% low, 60% high → >30% low → "low"
        inputs = [
            ExtractionInput(document_id=doc.id, pii_type="SSN", confidence_score=0.30),
            ExtractionInput(document_id=doc.id, pii_type="SSN", confidence_score=0.30),
            ExtractionInput(document_id=doc.id, pii_type="EMAIL", confidence_score=0.90),
            ExtractionInput(document_id=doc.id, pii_type="EMAIL", confidence_score=0.90),
            ExtractionInput(document_id=doc.id, pii_type="EMAIL", confidence_score=0.90),
        ]

        summaries = DensityTask(db_session).run(project.id, extraction_inputs=inputs)
        project_summary = [s for s in summaries if s.document_id is None][0]
        assert project_summary.confidence == "low"

    def test_confidence_notes_stored_as_json(self, db_session: Session) -> None:
        """confidence_notes should be a JSON list of strings."""
        project = _make_project(db_session)
        run = _make_ingestion_run(db_session, project_id=project.id)
        doc = _make_document(db_session, run.id, "a.pdf", "pdf")

        inputs = [
            ExtractionInput(document_id=doc.id, pii_type="SSN", confidence_score=0.30),
            ExtractionInput(document_id=doc.id, pii_type="EMAIL", confidence_score=0.90),
        ]

        summaries = DensityTask(db_session).run(project.id, extraction_inputs=inputs)
        project_summary = [s for s in summaries if s.document_id is None][0]
        notes = json.loads(project_summary.confidence_notes)
        assert isinstance(notes, list)
        assert len(notes) > 0

    def test_multiple_documents_different_categories(self, db_session: Session) -> None:
        """Multiple documents with different entity types produce correct aggregates."""
        project = _make_project(db_session)
        run = _make_ingestion_run(db_session, project_id=project.id)
        doc1 = _make_document(db_session, run.id, "medical.pdf", "pdf")
        doc2 = _make_document(db_session, run.id, "financial.pdf", "pdf")

        inputs = [
            ExtractionInput(document_id=doc1.id, pii_type="MRN", confidence_score=0.80),
            ExtractionInput(document_id=doc1.id, pii_type="NPI", confidence_score=0.70),
            ExtractionInput(document_id=doc2.id, pii_type="CREDIT_CARD", confidence_score=0.85),
            ExtractionInput(document_id=doc2.id, pii_type="IBAN", confidence_score=0.90),
        ]

        summaries = DensityTask(db_session).run(project.id, extraction_inputs=inputs)

        doc_summaries = {s.document_id: s for s in summaries if s.document_id is not None}
        # MRN -> PHI, NPI -> PHI
        assert doc_summaries[doc1.id].by_category == {"PHI": 2}
        # CREDIT_CARD -> PFI+PCI, IBAN -> PFI+NPI
        assert doc_summaries[doc2.id].by_category == {"PFI": 2, "PCI": 1, "NPI": 1}

        project_summary = [s for s in summaries if s.document_id is None][0]
        assert project_summary.by_category == {"PHI": 2, "PFI": 2, "PCI": 1, "NPI": 1}


# ===========================================================================
# Density API endpoint integration
# ===========================================================================


class TestDensityEndpointIntegration:
    """Verify the GET /projects/{id}/density endpoint works with DensityTask output."""

    @pytest.fixture()
    def api_session(self):
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=engine)
        SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
        session = SessionLocal()
        yield session
        session.close()

    @pytest.fixture()
    def client(self, api_session: Session, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")

        from app.core.settings import get_settings

        get_settings.cache_clear()

        from app.api.deps import get_db
        from app.api.main import app

        def _override():
            yield api_session

        app.dependency_overrides[get_db] = _override
        from fastapi.testclient import TestClient

        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
        app.dependency_overrides.clear()
        get_settings.cache_clear()

    def test_density_endpoint_returns_summaries(
        self, api_session: Session, client
    ) -> None:
        """Create density summaries via DensityTask, verify endpoint response."""
        project = _make_project(api_session)
        run = _make_ingestion_run(api_session, project_id=project.id)
        doc = _make_document(api_session, run.id, "report.pdf", "pdf")

        inputs = [
            ExtractionInput(document_id=doc.id, pii_type="SSN", confidence_score=0.90),
            ExtractionInput(document_id=doc.id, pii_type="EMAIL", confidence_score=0.85),
            ExtractionInput(document_id=doc.id, pii_type="MRN", confidence_score=0.80),
        ]

        DensityTask(api_session).run(project.id, extraction_inputs=inputs)
        api_session.commit()

        resp = client.get(f"/projects/{project.id}/density")
        assert resp.status_code == 200
        data = resp.json()

        assert data["project_id"] == str(project.id)

        # Project summary
        ps = data["project_summary"]
        assert ps is not None
        assert ps["total_entities"] == 3
        # SSN -> PII+SPII, EMAIL -> PII, MRN -> PHI
        assert ps["by_category"] == {"PII": 2, "SPII": 1, "PHI": 1}
        assert ps["by_type"] == {"SSN": 1, "EMAIL": 1, "MRN": 1}
        assert ps["confidence"] == "high"
        assert ps["document_id"] is None

        # Document summaries
        assert len(data["document_summaries"]) == 1
        ds = data["document_summaries"][0]
        assert ds["document_id"] == str(doc.id)
        assert ds["total_entities"] == 3

    def test_density_endpoint_empty_project(
        self, api_session: Session, client
    ) -> None:
        """A project with no density summaries returns None + empty list."""
        project = _make_project(api_session)
        api_session.commit()

        resp = client.get(f"/projects/{project.id}/density")
        assert resp.status_code == 200
        data = resp.json()

        assert data["project_summary"] is None
        assert data["document_summaries"] == []

    def test_density_endpoint_404_for_unknown_project(self, client) -> None:
        resp = client.get(f"/projects/{uuid4()}/density")
        assert resp.status_code == 404
