"""Tests for Step 15: Field-level detection review + protocol mapping.

Verifies:
- PROTOCOL_REQUIRED_FIELDS coverage for all protocols
- DetectionReviewDecision model + migration 0009
- Protocol mapping endpoint (field status: detected/missing/needs_review)
- Approve with detection_decisions stores decisions + selected_entity_types
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db
from app.core.constants import PROTOCOL_REQUIRED_FIELDS
from app.db.base import Base
from app.db.models import (
    DetectionReviewDecision,
    Document,
    DocumentAnalysisReview,
    Extraction,
    IngestionRun,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_RUN_DEFAULTS = dict(
    source_path="/tmp",
    config_hash="abc",
    code_version="0.1.0",
    initiated_by="test",
)


def _make_run(db: Session, *, protocol_id: str = "hipaa", **kw) -> IngestionRun:
    defaults = {
        **_RUN_DEFAULTS,
        "id": uuid4(),
        "status": "analyzed",
        "pipeline_mode": "two_phase",
        "config_snapshot": {"protocol_id": protocol_id},
    }
    defaults.update(kw)
    run = IngestionRun(**defaults)
    db.add(run)
    db.flush()
    return run


def _make_doc(db: Session, run: IngestionRun, *, file_name: str = "test.pdf", **kw) -> Document:
    defaults = {
        "id": uuid4(),
        "ingestion_run_id": run.id,
        "file_name": file_name,
        "file_type": "pdf",
        "source_path": f"/tmp/{file_name}",
        "sha256": uuid4().hex,
    }
    defaults.update(kw)
    doc = Document(**defaults)
    db.add(doc)
    db.flush()
    return doc


def _make_review(db: Session, doc: Document, run: IngestionRun, *, status: str = "pending_review") -> DocumentAnalysisReview:
    review = DocumentAnalysisReview(
        id=uuid4(), document_id=doc.id, ingestion_run_id=run.id, status=status,
    )
    db.add(review)
    db.flush()
    return review


def _make_extraction(db: Session, doc: Document, run: IngestionRun, *, pii_type: str = "PERSON", **kw) -> Extraction:
    defaults = {
        "id": uuid4(),
        "document_id": doc.id,
        "pii_type": pii_type,
        "sensitivity": "high",
        "hashed_value": uuid4().hex,
        "masked_value": "***",
        "confidence_score": 0.90,
        "is_sample": True,
    }
    defaults.update(kw)
    ext = Extraction(**defaults)
    db.add(ext)
    db.flush()
    return ext


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


@pytest.fixture()
def client(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """TestClient with get_db overridden to use the in-memory session."""
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    from app.core.settings import get_settings
    get_settings.cache_clear()
    from app.api.main import app

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# PROTOCOL_REQUIRED_FIELDS
# ---------------------------------------------------------------------------

class TestProtocolRequiredFields:
    """All protocols have valid field requirements."""

    ALL_PROTOCOLS = list(PROTOCOL_REQUIRED_FIELDS.keys())

    def test_all_twelve_protocols_present(self) -> None:
        """12 protocol keys are defined."""
        assert len(PROTOCOL_REQUIRED_FIELDS) == 12

    @pytest.mark.parametrize("protocol_id", ALL_PROTOCOLS)
    def test_protocol_has_required_key(self, protocol_id: str) -> None:
        """Each protocol has a 'required' key with a list."""
        fields = PROTOCOL_REQUIRED_FIELDS[protocol_id]
        assert "required" in fields
        assert isinstance(fields["required"], list)
        assert len(fields["required"]) >= 2

    @pytest.mark.parametrize("protocol_id", ALL_PROTOCOLS)
    def test_field_structure(self, protocol_id: str) -> None:
        """Each field has: field (str), entity_types (list), criticality (str)."""
        for field_def in PROTOCOL_REQUIRED_FIELDS[protocol_id]["required"]:
            assert "field" in field_def
            assert "entity_types" in field_def
            assert "criticality" in field_def
            assert isinstance(field_def["entity_types"], list)
            assert field_def["criticality"] in ("required", "if_available")

    @pytest.mark.parametrize("protocol_id", ALL_PROTOCOLS)
    def test_has_person_field(self, protocol_id: str) -> None:
        """Every protocol requires at least a name/person field."""
        fields = PROTOCOL_REQUIRED_FIELDS[protocol_id]["required"]
        has_person = any("PERSON" in f["entity_types"] for f in fields)
        assert has_person, f"{protocol_id} has no PERSON field"

    def test_hipaa_has_medical_record(self) -> None:
        """HIPAA includes medical record field."""
        fields = PROTOCOL_REQUIRED_FIELDS["hipaa"]["required"]
        field_names = [f["field"] for f in fields]
        assert "Medical Record" in field_names

    def test_ferpa_has_student_id(self) -> None:
        """FERPA includes student ID field."""
        fields = PROTOCOL_REQUIRED_FIELDS["ferpa"]["required"]
        field_names = [f["field"] for f in fields]
        assert "Student ID" in field_names

    def test_pci_dss_has_credit_card(self) -> None:
        """PCI DSS requires card number field."""
        fields = PROTOCOL_REQUIRED_FIELDS["pci_dss"]["required"]
        card_field = [f for f in fields if f["field"] == "Card Number"]
        assert len(card_field) == 1
        assert card_field[0]["criticality"] == "required"
        assert "CREDIT_CARD" in card_field[0]["entity_types"]


# ---------------------------------------------------------------------------
# Schema: migration 0009
# ---------------------------------------------------------------------------

class TestDetectionReviewSchema:
    """Migration 0009 creates detection_review_decisions table."""

    def test_detection_review_decisions_table_exists(self, db_session: Session) -> None:
        """detection_review_decisions table is in the metadata."""
        inspector = inspect(db_session.bind)
        tables = inspector.get_table_names()
        assert "detection_review_decisions" in tables

    def test_detection_review_decisions_columns(self, db_session: Session) -> None:
        """Table has all required columns."""
        inspector = inspect(db_session.bind)
        cols = {c["name"] for c in inspector.get_columns("detection_review_decisions")}
        expected = {
            "id", "document_analysis_review_id", "entity_type",
            "detected_value_masked", "confidence", "page",
            "include_in_extraction", "decision_reason", "decided_by",
            "decided_at", "decision_source",
        }
        assert expected.issubset(cols)

    def test_selected_entity_types_column(self, db_session: Session) -> None:
        """document_analysis_reviews has selected_entity_types column."""
        inspector = inspect(db_session.bind)
        cols = {c["name"] for c in inspector.get_columns("document_analysis_reviews")}
        assert "selected_entity_types" in cols


# ---------------------------------------------------------------------------
# DetectionReviewDecision model
# ---------------------------------------------------------------------------

class TestDetectionReviewDecisionModel:
    """Model creates and reads correctly."""

    def test_create_decision(self, db_session: Session) -> None:
        """Create a detection review decision."""
        run = _make_run(db_session)
        doc = _make_doc(db_session, run)
        review = _make_review(db_session, doc, run)

        decision = DetectionReviewDecision(
            id=uuid4(), document_analysis_review_id=review.id,
            entity_type="PERSON", detected_value_masked="John D***",
            confidence=0.85, page=1, include_in_extraction=True, decision_source="individual",
        )
        db_session.add(decision)
        db_session.flush()

        fetched = db_session.get(DetectionReviewDecision, decision.id)
        assert fetched is not None
        assert fetched.entity_type == "PERSON"
        assert fetched.include_in_extraction is True
        assert fetched.decision_source == "individual"

    def test_decision_defaults(self, db_session: Session) -> None:
        """Default: include=True, source=default."""
        run = _make_run(db_session)
        doc = _make_doc(db_session, run)
        review = _make_review(db_session, doc, run)

        decision = DetectionReviewDecision(id=uuid4(), document_analysis_review_id=review.id, entity_type="PHONE_NUMBER")
        db_session.add(decision)
        db_session.flush()

        fetched = db_session.get(DetectionReviewDecision, decision.id)
        assert fetched.include_in_extraction is True
        assert fetched.decision_source == "default"

    def test_selected_entity_types_json(self, db_session: Session) -> None:
        """selected_entity_types stores a JSON list on review."""
        run = _make_run(db_session)
        doc = _make_doc(db_session, run)

        review = DocumentAnalysisReview(
            id=uuid4(), document_id=doc.id, ingestion_run_id=run.id,
            status="approved", selected_entity_types=["PERSON", "US_SSN"],
        )
        db_session.add(review)
        db_session.flush()

        fetched = db_session.get(DocumentAnalysisReview, review.id)
        assert fetched.selected_entity_types == ["PERSON", "US_SSN"]


# ---------------------------------------------------------------------------
# API: approve with detection decisions
# ---------------------------------------------------------------------------

class TestApproveWithDecisions:
    """Approve endpoint stores detection decisions."""

    def test_approve_stores_decisions(self, db_session: Session, client: TestClient) -> None:
        """Approving with detection_decisions creates decision records."""
        run = _make_run(db_session)
        doc = _make_doc(db_session, run)
        review = _make_review(db_session, doc, run)

        resp = client.post(
            f"/jobs/{run.id}/documents/{doc.id}/approve",
            json={
                "reviewer_id": "test-reviewer",
                "rationale": "Approved with selections",
                "detection_decisions": [
                    {"entity_type": "PERSON", "detected_value_masked": "John D***", "page": 1, "include": True},
                    {"entity_type": "PHONE_NUMBER", "detected_value_masked": "153.84***", "page": 1, "include": False, "reason": "false positive"},
                ],
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"

        decisions = db_session.query(DetectionReviewDecision).filter(
            DetectionReviewDecision.document_analysis_review_id == review.id,
        ).all()
        assert len(decisions) == 2

        person_dec = [d for d in decisions if d.entity_type == "PERSON"][0]
        assert person_dec.include_in_extraction is True

        phone_dec = [d for d in decisions if d.entity_type == "PHONE_NUMBER"][0]
        assert phone_dec.include_in_extraction is False
        assert phone_dec.decision_reason == "false positive"

        db_session.refresh(review)
        assert review.selected_entity_types == ["PERSON"]

    def test_approve_without_decisions(self, db_session: Session, client: TestClient) -> None:
        """Approving without decisions works (backward compatible)."""
        run = _make_run(db_session)
        doc = _make_doc(db_session, run)
        review = _make_review(db_session, doc, run)

        resp = client.post(
            f"/jobs/{run.id}/documents/{doc.id}/approve",
            json={"reviewer_id": "test-reviewer"},
        )
        assert resp.status_code == 200

        db_session.refresh(review)
        assert review.selected_entity_types is None


# ---------------------------------------------------------------------------
# API: protocol mapping endpoint
# ---------------------------------------------------------------------------

class TestProtocolMappingEndpoint:
    """GET /jobs/{id}/documents/{doc_id}/protocol-mapping."""

    def test_detected_field_status(self, db_session: Session, client: TestClient) -> None:
        """Field with matching extraction -> status 'detected'."""
        run = _make_run(db_session, protocol_id="hipaa")
        doc = _make_doc(db_session, run, file_name="medical.pdf")
        _make_extraction(db_session, doc, run, pii_type="PERSON", masked_value="Jane D***")

        resp = client.get(f"/jobs/{run.id}/documents/{doc.id}/protocol-mapping")
        assert resp.status_code == 200

        data = resp.json()
        assert data["protocol"] == "hipaa"
        name_field = [f for f in data["field_mapping"] if f["field"] == "Individual Name"]
        assert len(name_field) == 1
        assert name_field[0]["status"] == "detected"
        assert len(name_field[0]["matched_detections"]) >= 1

    def test_missing_required_field(self, db_session: Session, client: TestClient) -> None:
        """Required field with no matching extraction -> status 'missing'."""
        run = _make_run(db_session, protocol_id="state_breach")
        doc = _make_doc(db_session, run, file_name="doc.pdf")

        resp = client.get(f"/jobs/{run.id}/documents/{doc.id}/protocol-mapping")
        assert resp.status_code == 200

        data = resp.json()
        ssn_field = [f for f in data["field_mapping"] if f["field"] == "SSN"]
        assert len(ssn_field) == 1
        assert ssn_field[0]["status"] == "missing"
        assert ssn_field[0]["criticality"] == "required"

    def test_completeness_percentage(self, db_session: Session, client: TestClient) -> None:
        """Coverage stats show correct completeness."""
        run = _make_run(db_session, protocol_id="pci_dss")
        doc = _make_doc(db_session, run, file_name="card.pdf")
        _make_extraction(db_session, doc, run, pii_type="PERSON", masked_value="Cardholder")

        resp = client.get(f"/jobs/{run.id}/documents/{doc.id}/protocol-mapping")
        data = resp.json()
        coverage = data["coverage"]

        assert coverage["required_fields"] == 2
        assert coverage["required_detected"] == 1
        assert coverage["required_missing"] == 1
        assert coverage["completeness_pct"] == 50


# ---------------------------------------------------------------------------
# Bulk type toggle behavior
# ---------------------------------------------------------------------------

class TestBulkTypeToggle:
    """Type-level bulk toggle semantics."""

    def test_bulk_exclude_then_approve(self, db_session: Session, client: TestClient) -> None:
        """Excluding all PHONE_NUMBER detections via decisions."""
        run = _make_run(db_session)
        doc = _make_doc(db_session, run)
        review = _make_review(db_session, doc, run)

        resp = client.post(
            f"/jobs/{run.id}/documents/{doc.id}/approve",
            json={
                "reviewer_id": "reviewer",
                "detection_decisions": [
                    {"entity_type": "PERSON", "include": True},
                    {"entity_type": "LOCATION", "include": True},
                    {"entity_type": "PHONE_NUMBER", "include": False, "reason": "bulk excluded"},
                    {"entity_type": "PHONE_NUMBER", "include": False, "reason": "bulk excluded"},
                ],
            },
        )
        assert resp.status_code == 200

        db_session.refresh(review)
        assert "PHONE_NUMBER" not in (review.selected_entity_types or [])
        assert "PERSON" in (review.selected_entity_types or [])
        assert "LOCATION" in (review.selected_entity_types or [])

    def test_individual_re_include_after_bulk_off(self, db_session: Session, client: TestClient) -> None:
        """One PHONE re-included after bulk off."""
        run = _make_run(db_session)
        doc = _make_doc(db_session, run)
        review = _make_review(db_session, doc, run)

        resp = client.post(
            f"/jobs/{run.id}/documents/{doc.id}/approve",
            json={
                "reviewer_id": "reviewer",
                "detection_decisions": [
                    {"entity_type": "PERSON", "include": True},
                    {"entity_type": "PHONE_NUMBER", "include": False},
                    {"entity_type": "PHONE_NUMBER", "include": True, "reason": "keep this"},
                ],
            },
        )
        assert resp.status_code == 200
        db_session.refresh(review)
        # PHONE_NUMBER included because at least one is included
        assert "PHONE_NUMBER" in (review.selected_entity_types or [])
