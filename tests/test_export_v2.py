"""Tests for the auditor-ready CSV export (Step 18).

Covers:
- _mask_gov_id() masking
- ExportColumn / EXPORT_SCHEMAS definitions
- SubjectRow.from_orm with lineage fields
- CSVExporter with auditor schema
- Lineage: source_document_name, source_page_range, government_id_type
- Pipe-delimited pii_types_list
- Minimal schema
- Full schema rejects STRICT mode
- Preview endpoint
- Empty project
- individual_id auto-incrementing
"""
from __future__ import annotations

import csv
import io
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db
from app.db.base import Base
from app.db.models import (
    ExportJob,
    NotificationSubject,
    Project,
)
from app.export.csv_exporter import (
    CSVExporter,
    SubjectRow,
    _mask_gov_id,
    build_csv_content_v2,
)
from app.export.export_schema import (
    AUDITOR_EXPORT_COLUMNS,
    EXPORT_SCHEMAS,
    FULL_EXPORT_COLUMNS,
    MINIMAL_EXPORT_COLUMNS,
    ExportColumn,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_session():
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
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_project(db: Session, name: str = "Test Project") -> Project:
    project = Project(id=uuid4(), name=name)
    db.add(project)
    db.flush()
    return project


def _make_subject(
    db: Session,
    project_id=None,
    *,
    name: str = "Jane Doe",
    email: str | None = "jane@example.com",
    phone: str | None = "+12025551234",
    status: str = "AI_PENDING",
    merge_confidence: float = 0.95,
    pii_types: list | None = None,
    source_document_name: str | None = None,
    source_page_range: str | None = None,
    government_id_type: str | None = None,
    extraction_confidence: float | None = None,
    pii_types_list: str | None = None,
) -> NotificationSubject:
    ns = NotificationSubject(
        subject_id=uuid4(),
        project_id=project_id,
        canonical_name=name,
        canonical_email=email,
        canonical_phone=phone,
        canonical_address={"street": "123 Main St", "city": "DC", "state": "DC", "zip": "20001"},
        pii_types_found=pii_types or ["US_SSN"],
        merge_confidence=merge_confidence,
        notification_required=True,
        review_status=status,
        source_document_name=source_document_name,
        source_page_range=source_page_range,
        government_id_type=government_id_type,
        extraction_confidence=extraction_confidence,
        pii_types_list=pii_types_list,
    )
    db.add(ns)
    db.flush()
    return ns


# ===========================================================================
# _mask_gov_id
# ===========================================================================


class TestMaskGovId:
    def test_nino(self):
        assert _mask_gov_id("NE724362D") == "NE7****2D"

    def test_ssn(self):
        assert _mask_gov_id("123456789") == "123****89"

    def test_short_returns_stars(self):
        assert _mask_gov_id("AB1") == "***"

    def test_empty_returns_stars(self):
        assert _mask_gov_id("") == "***"

    def test_none_returns_stars(self):
        assert _mask_gov_id(None) == "***"

    def test_exactly_five_chars(self):
        assert _mask_gov_id("ABCDE") == "ABC" + "DE"


# ===========================================================================
# ExportColumn / EXPORT_SCHEMAS
# ===========================================================================


class TestExportSchema:
    def test_auditor_has_15_columns(self):
        assert len(AUDITOR_EXPORT_COLUMNS) == 15

    def test_minimal_has_3_columns(self):
        assert len(MINIMAL_EXPORT_COLUMNS) == 3

    def test_full_is_auditor_plus_4(self):
        assert len(FULL_EXPORT_COLUMNS) == 15 + 4

    def test_schemas_dict(self):
        assert set(EXPORT_SCHEMAS.keys()) == {"auditor", "minimal", "full"}

    def test_auditor_column_names(self):
        names = [c.name for c in AUDITOR_EXPORT_COLUMNS]
        assert "individual_id" in names
        assert "name" in names
        assert "government_id" in names
        assert "source_document" in names
        assert "source_pages" in names

    def test_minimal_column_names(self):
        names = [c.name for c in MINIMAL_EXPORT_COLUMNS]
        assert names == ["name", "notification_required", "review_status"]

    def test_export_column_frozen(self):
        col = ExportColumn(name="test", source_field="test")
        assert col.required is False
        assert col.mask_strategy is None


# ===========================================================================
# SubjectRow (Step 18)
# ===========================================================================


class TestSubjectRowV2:
    def test_from_orm_with_lineage(self, db_session):
        project = _make_project(db_session)
        ns = _make_subject(
            db_session, project.id,
            source_document_name="test.pdf",
            source_page_range="1-3",
            government_id_type="NI_NUMBER",
            extraction_confidence=0.92,
            pii_types_list="NI_NUMBER|PERSON",
        )
        row = SubjectRow.from_orm(ns, individual_id=1)
        assert row.individual_id == 1
        assert row.source_document_name == "test.pdf"
        assert row.source_page_range == "1-3"
        assert row.government_id_type == "NI_NUMBER"
        assert row.extraction_confidence == 0.92
        assert row.pii_types_list == "NI_NUMBER|PERSON"

    def test_from_orm_defaults(self, db_session):
        project = _make_project(db_session)
        ns = _make_subject(db_session, project.id)
        row = SubjectRow.from_orm(ns)
        assert row.individual_id == 0
        assert row.source_document_name is None
        assert row.source_page_range is None


# ===========================================================================
# CSVExporter with auditor schema
# ===========================================================================


class TestCSVExporterAuditor:
    def test_two_subjects_two_rows(self, db_session, tmp_path):
        project = _make_project(db_session)
        _make_subject(db_session, project.id, name="Alice", pii_types_list="PERSON|US_SSN")
        _make_subject(db_session, project.id, name="Bob", pii_types_list="PERSON")

        exporter = CSVExporter(db_session)
        job = exporter.run(project.id, output_dir=tmp_path, export_schema="auditor")

        assert job.status == "completed"
        assert job.row_count == 2

        content = Path(job.file_path).read_text(encoding="utf-8")
        reader = csv.reader(io.StringIO(content))
        header = next(reader)
        assert len(header) == 15
        assert header[0] == "individual_id"
        assert header[1] == "name"

        rows = list(reader)
        assert len(rows) == 2
        # individual_ids should be 1 and 2
        assert rows[0][0] == "1"
        assert rows[1][0] == "2"

    def test_correct_column_order(self, db_session, tmp_path):
        project = _make_project(db_session)
        _make_subject(db_session, project.id)

        exporter = CSVExporter(db_session)
        job = exporter.run(project.id, output_dir=tmp_path, export_schema="auditor")

        content = Path(job.file_path).read_text(encoding="utf-8")
        reader = csv.reader(io.StringIO(content))
        header = next(reader)
        expected = [c.name for c in AUDITOR_EXPORT_COLUMNS]
        assert header == expected


class TestLineage:
    def test_source_document_name(self, db_session, tmp_path):
        project = _make_project(db_session)
        _make_subject(
            db_session, project.id,
            source_document_name="pension_statement.pdf",
            source_page_range="1-3",
        )

        exporter = CSVExporter(db_session)
        job = exporter.run(project.id, output_dir=tmp_path, export_schema="auditor")

        content = Path(job.file_path).read_text(encoding="utf-8")
        assert "pension_statement.pdf" in content
        assert "1-3" in content


class TestGovIdType:
    def test_government_id_type_in_csv(self, db_session, tmp_path):
        project = _make_project(db_session)
        _make_subject(
            db_session, project.id,
            government_id_type="NI_NUMBER",
        )

        exporter = CSVExporter(db_session)
        job = exporter.run(project.id, output_dir=tmp_path, export_schema="auditor")

        content = Path(job.file_path).read_text(encoding="utf-8")
        assert "NI_NUMBER" in content


class TestPiiTypesList:
    def test_pipe_delimited(self, db_session, tmp_path):
        project = _make_project(db_session)
        _make_subject(
            db_session, project.id,
            pii_types_list="EMAIL_ADDRESS|PERSON|US_SSN",
        )

        exporter = CSVExporter(db_session)
        job = exporter.run(project.id, output_dir=tmp_path, export_schema="auditor")

        content = Path(job.file_path).read_text(encoding="utf-8")
        # Should be pipe-delimited, not JSON array
        assert "EMAIL_ADDRESS|PERSON|US_SSN" in content
        assert '["EMAIL_ADDRESS"' not in content


class TestMinimalSchema:
    def test_only_three_columns(self, db_session, tmp_path):
        project = _make_project(db_session)
        _make_subject(db_session, project.id)

        exporter = CSVExporter(db_session)
        job = exporter.run(project.id, output_dir=tmp_path, export_schema="minimal")

        content = Path(job.file_path).read_text(encoding="utf-8")
        reader = csv.reader(io.StringIO(content))
        header = next(reader)
        assert header == ["name", "notification_required", "review_status"]


class TestFullSchemaRejectsStrict:
    def test_raises_in_strict_mode(self, db_session, tmp_path):
        """STRICT mode (pii_masking_enabled=True) should reject full schema."""
        project = _make_project(db_session)
        _make_subject(db_session, project.id)

        exporter = CSVExporter(db_session)
        with pytest.raises(ValueError, match="not available in STRICT mode"):
            exporter.run(project.id, output_dir=tmp_path, export_schema="full")


class TestEmptyProject:
    def test_headers_only_zero_rows(self, db_session, tmp_path):
        project = _make_project(db_session)

        exporter = CSVExporter(db_session)
        job = exporter.run(project.id, output_dir=tmp_path, export_schema="auditor")

        assert job.status == "completed"
        assert job.row_count == 0

        content = Path(job.file_path).read_text(encoding="utf-8")
        reader = csv.reader(io.StringIO(content))
        header = next(reader)
        assert len(header) == 15
        remaining = list(reader)
        assert remaining == []


# ===========================================================================
# build_csv_content_v2 (pure function)
# ===========================================================================


class TestBuildCsvContentV2:
    def test_header_and_rows(self):
        cols = [
            ExportColumn(name="name", source_field="canonical_name"),
            ExportColumn(name="status", source_field="review_status"),
        ]
        row = SubjectRow(
            subject_id="x", canonical_name="Test", canonical_email=None,
            canonical_phone=None, canonical_address=None, pii_types_found=None,
            source_records=None, merge_confidence=None, notification_required=False,
            review_status="APPROVED",
        )
        content = build_csv_content_v2([row], cols)
        reader = csv.reader(io.StringIO(content))
        header = next(reader)
        assert header == ["name", "status"]
        data = next(reader)
        assert data == ["Test", "APPROVED"]


# ===========================================================================
# Preview endpoint
# ===========================================================================


class TestPreviewEndpoint:
    def test_returns_first_5_rows(self, db_session, client):
        project = _make_project(db_session)
        for i in range(8):
            _make_subject(db_session, project.id, name=f"Person {i}")

        # Create export
        create_resp = client.post(
            f"/api/projects/{project.id}/exports",
            json={"export_schema": "auditor"},
        )
        assert create_resp.status_code == 200
        export_id = create_resp.json()["id"]

        # Preview
        resp = client.get(f"/api/projects/{project.id}/exports/{export_id}/preview?rows=5")
        assert resp.status_code == 200
        data = resp.json()
        assert data["preview_count"] == 5
        assert data["total_rows"] == 8
        assert len(data["rows"]) == 5
        assert len(data["columns"]) == 15

    def test_preview_not_completed(self, db_session, client):
        project = _make_project(db_session)
        # Create a pending export job manually
        ej = ExportJob(
            project_id=project.id, export_type="csv", status="pending",
        )
        db_session.add(ej)
        db_session.flush()

        resp = client.get(f"/api/projects/{project.id}/exports/{ej.id}/preview")
        assert resp.status_code == 400


# ===========================================================================
# API: export_schema parameter
# ===========================================================================


class TestExportSchemaAPI:
    def test_create_with_auditor_schema(self, db_session, client):
        project = _make_project(db_session)
        _make_subject(db_session, project.id)

        resp = client.post(
            f"/api/projects/{project.id}/exports",
            json={"export_schema": "auditor"},
        )
        assert resp.status_code == 200
        assert resp.json()["row_count"] == 1

    def test_create_with_minimal_schema(self, db_session, client):
        project = _make_project(db_session)
        _make_subject(db_session, project.id)

        resp = client.post(
            f"/api/projects/{project.id}/exports",
            json={"export_schema": "minimal"},
        )
        assert resp.status_code == 200

        # Download and check columns
        export_id = resp.json()["id"]
        dl = client.get(f"/api/projects/{project.id}/exports/{export_id}/download")
        reader = csv.reader(io.StringIO(dl.text))
        header = next(reader)
        assert header == ["name", "notification_required", "review_status"]

    def test_invalid_schema_returns_400(self, db_session, client):
        project = _make_project(db_session)
        resp = client.post(
            f"/api/projects/{project.id}/exports",
            json={"export_schema": "nonexistent"},
        )
        assert resp.status_code == 400
