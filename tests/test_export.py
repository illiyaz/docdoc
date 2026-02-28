"""Tests for the CSV export module (Step 6).

Covers:
- _mask_email(), _mask_phone(), _mask_address() pure masking functions
- _format_value() field formatting with masking
- resolve_export_fields() column resolution from protocol config
- build_csv_content() pure CSV generation (no DB)
- CSVExporter.run() ORM integration: ExportJob lifecycle, file writing
- API endpoints: create, list, get, download, 404s, filters
- No raw PII ever appears in CSV output
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
    ProtocolConfig,
)
from app.export.csv_exporter import (
    ALLOWED_EXPORT_FIELDS,
    DEFAULT_EXPORT_FIELDS,
    CSVExporter,
    SubjectRow,
    _mask_address,
    _mask_email,
    _mask_phone,
    _format_value,
    build_csv_content,
    resolve_export_fields,
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
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_project(db: Session, name: str = "Test Project") -> Project:
    project = Project(id=uuid4(), name=name)
    db.add(project)
    db.flush()
    return project


def _make_protocol_config(
    db: Session,
    project_id,
    config_json: dict | None = None,
) -> ProtocolConfig:
    pc = ProtocolConfig(
        id=uuid4(),
        project_id=project_id,
        name="Test Config",
        config_json=config_json or {},
    )
    db.add(pc)
    db.flush()
    return pc


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
    )
    db.add(ns)
    db.flush()
    return ns


# ===========================================================================
# Pure masking functions
# ===========================================================================


class TestMaskEmail:
    def test_masks_real_email(self):
        assert _mask_email("jane@example.com") == "***@***.***"

    def test_none_returns_empty(self):
        assert _mask_email(None) == ""

    def test_empty_returns_empty(self):
        assert _mask_email("") == ""


class TestMaskPhone:
    def test_masks_e164(self):
        result = _mask_phone("+12025551234")
        assert result == "***-***-1234"

    def test_masks_formatted(self):
        result = _mask_phone("(202) 555-1234")
        assert result == "***-***-1234"

    def test_none_returns_empty(self):
        assert _mask_phone(None) == ""

    def test_empty_returns_empty(self):
        assert _mask_phone("") == ""

    def test_short_phone(self):
        result = _mask_phone("123")
        assert result == "***"


class TestMaskAddress:
    def test_masks_full_address(self):
        addr = {"street": "123 Main St", "city": "DC", "state": "DC", "zip": "20001"}
        result = _mask_address(addr)
        assert "DC" in result
        assert "20001" in result
        assert "123 Main St" not in result

    def test_none_returns_empty(self):
        assert _mask_address(None) == ""

    def test_empty_dict(self):
        assert _mask_address({}) == "***"

    def test_state_only(self):
        result = _mask_address({"state": "CA"})
        assert result == "CA"


# ===========================================================================
# _format_value
# ===========================================================================


class TestFormatValue:
    def test_none_returns_empty(self):
        assert _format_value("canonical_name", None) == ""

    def test_email_masked(self):
        assert _format_value("canonical_email", "a@b.com") == "***@***.***"

    def test_phone_masked(self):
        result = _format_value("canonical_phone", "+12025551234")
        assert "1234" in result
        assert "+12025551234" not in result

    def test_address_masked(self):
        result = _format_value("canonical_address", {"street": "123 Main", "state": "NY", "zip": "10001"})
        assert "123 Main" not in result
        assert "NY" in result

    def test_list_as_json(self):
        result = _format_value("pii_types_found", ["US_SSN", "EMAIL_ADDRESS"])
        assert result == '["US_SSN","EMAIL_ADDRESS"]'

    def test_float_formatted(self):
        result = _format_value("merge_confidence", 0.95123456)
        assert result == "0.9512"

    def test_bool_formatted(self):
        assert _format_value("notification_required", True) == "True"

    def test_string_passthrough(self):
        assert _format_value("review_status", "APPROVED") == "APPROVED"

    def test_uuid_as_string(self):
        uid = uuid4()
        assert _format_value("subject_id", uid) == str(uid)


# ===========================================================================
# resolve_export_fields
# ===========================================================================


class TestResolveExportFields:
    def test_default_when_no_config(self):
        fields = resolve_export_fields(None)
        assert fields == DEFAULT_EXPORT_FIELDS

    def test_from_protocol_config(self, db_session):
        project = _make_project(db_session)
        pc = _make_protocol_config(
            db_session,
            project.id,
            config_json={"export_fields": ["canonical_name", "canonical_email"]},
        )
        fields = resolve_export_fields(pc)
        assert fields == ["canonical_name", "canonical_email"]

    def test_unknown_fields_dropped(self, db_session):
        project = _make_project(db_session)
        pc = _make_protocol_config(
            db_session,
            project.id,
            config_json={"export_fields": ["canonical_name", "raw_value", "secret"]},
        )
        fields = resolve_export_fields(pc)
        assert fields == ["canonical_name"]

    def test_all_unknown_falls_back_to_default(self, db_session):
        project = _make_project(db_session)
        pc = _make_protocol_config(
            db_session,
            project.id,
            config_json={"export_fields": ["raw_value", "secret"]},
        )
        fields = resolve_export_fields(pc)
        assert fields == DEFAULT_EXPORT_FIELDS

    def test_empty_export_fields_falls_back(self, db_session):
        project = _make_project(db_session)
        pc = _make_protocol_config(
            db_session,
            project.id,
            config_json={"export_fields": []},
        )
        fields = resolve_export_fields(pc)
        assert fields == DEFAULT_EXPORT_FIELDS

    def test_no_export_fields_key_falls_back(self, db_session):
        project = _make_project(db_session)
        pc = _make_protocol_config(
            db_session,
            project.id,
            config_json={"sampling_rate": 0.1},
        )
        fields = resolve_export_fields(pc)
        assert fields == DEFAULT_EXPORT_FIELDS


# ===========================================================================
# build_csv_content (pure function)
# ===========================================================================


class TestBuildCSVContent:
    def test_header_row(self):
        csv_text = build_csv_content([], ["canonical_name", "canonical_email"])
        reader = csv.reader(io.StringIO(csv_text))
        header = next(reader)
        assert header == ["canonical_name", "canonical_email"]

    def test_data_rows(self):
        row = SubjectRow(
            subject_id="abc-123",
            canonical_name="Jane Doe",
            canonical_email="jane@example.com",
            canonical_phone="+12025551234",
            canonical_address=None,
            pii_types_found=["US_SSN"],
            source_records=None,
            merge_confidence=0.95,
            notification_required=True,
            review_status="APPROVED",
        )
        fields = ["canonical_name", "canonical_email", "review_status"]
        csv_text = build_csv_content([row], fields)
        reader = csv.reader(io.StringIO(csv_text))
        next(reader)  # skip header
        data = next(reader)
        assert data[0] == "Jane Doe"
        assert data[1] == "***@***.***"  # masked
        assert data[2] == "APPROVED"

    def test_no_raw_email_in_output(self):
        row = SubjectRow(
            subject_id="x",
            canonical_name="Test",
            canonical_email="secret@corp.com",
            canonical_phone=None,
            canonical_address=None,
            pii_types_found=None,
            source_records=None,
            merge_confidence=None,
            notification_required=False,
            review_status="AI_PENDING",
        )
        csv_text = build_csv_content([row], DEFAULT_EXPORT_FIELDS)
        assert "secret@corp.com" not in csv_text

    def test_no_raw_phone_in_output(self):
        row = SubjectRow(
            subject_id="x",
            canonical_name="Test",
            canonical_email=None,
            canonical_phone="+18005551234",
            canonical_address=None,
            pii_types_found=None,
            source_records=None,
            merge_confidence=None,
            notification_required=False,
            review_status="AI_PENDING",
        )
        csv_text = build_csv_content([row], DEFAULT_EXPORT_FIELDS)
        assert "+18005551234" not in csv_text

    def test_empty_rows(self):
        csv_text = build_csv_content([], DEFAULT_EXPORT_FIELDS)
        reader = csv.reader(io.StringIO(csv_text))
        header = next(reader)
        assert header == DEFAULT_EXPORT_FIELDS
        remaining = list(reader)
        assert remaining == []

    def test_multiple_rows(self):
        rows = [
            SubjectRow(
                subject_id=str(i),
                canonical_name=f"Person {i}",
                canonical_email=f"p{i}@test.com",
                canonical_phone=None,
                canonical_address=None,
                pii_types_found=["US_SSN"],
                source_records=None,
                merge_confidence=0.9,
                notification_required=True,
                review_status="APPROVED",
            )
            for i in range(5)
        ]
        csv_text = build_csv_content(rows, ["canonical_name", "review_status"])
        reader = csv.reader(io.StringIO(csv_text))
        next(reader)  # skip header
        data_rows = list(reader)
        assert len(data_rows) == 5


# ===========================================================================
# SubjectRow
# ===========================================================================


class TestSubjectRow:
    def test_from_orm(self, db_session):
        project = _make_project(db_session)
        ns = _make_subject(db_session, project.id)
        row = SubjectRow.from_orm(ns)
        assert row.canonical_name == "Jane Doe"
        assert row.canonical_email == "jane@example.com"
        assert row.review_status == "AI_PENDING"

    def test_get_returns_value(self):
        row = SubjectRow(
            subject_id="x",
            canonical_name="Test",
            canonical_email=None,
            canonical_phone=None,
            canonical_address=None,
            pii_types_found=None,
            source_records=None,
            merge_confidence=None,
            notification_required=False,
            review_status="AI_PENDING",
        )
        assert row.get("canonical_name") == "Test"
        assert row.get("nonexistent") is None


# ===========================================================================
# CSVExporter ORM integration
# ===========================================================================


class TestCSVExporter:
    def test_creates_export_job(self, db_session, tmp_path):
        project = _make_project(db_session)
        _make_subject(db_session, project.id)

        exporter = CSVExporter(db_session)
        job = exporter.run(project.id, output_dir=tmp_path)

        assert job.status == "completed"
        assert job.export_type == "csv"
        assert job.row_count == 1
        assert job.file_path is not None
        assert job.completed_at is not None

    def test_file_is_written(self, db_session, tmp_path):
        project = _make_project(db_session)
        _make_subject(db_session, project.id)

        exporter = CSVExporter(db_session)
        job = exporter.run(project.id, output_dir=tmp_path)

        path = Path(job.file_path)
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert "canonical_name" in content  # header present

    def test_no_raw_email_in_file(self, db_session, tmp_path):
        project = _make_project(db_session)
        _make_subject(db_session, project.id, email="secret@corp.com")

        exporter = CSVExporter(db_session)
        job = exporter.run(project.id, output_dir=tmp_path)

        content = Path(job.file_path).read_text(encoding="utf-8")
        assert "secret@corp.com" not in content

    def test_no_raw_phone_in_file(self, db_session, tmp_path):
        project = _make_project(db_session)
        _make_subject(db_session, project.id, phone="+18005559999")

        exporter = CSVExporter(db_session)
        job = exporter.run(project.id, output_dir=tmp_path)

        content = Path(job.file_path).read_text(encoding="utf-8")
        assert "+18005559999" not in content

    def test_no_raw_address_in_file(self, db_session, tmp_path):
        project = _make_project(db_session)
        _make_subject(db_session, project.id)

        exporter = CSVExporter(db_session)
        job = exporter.run(project.id, output_dir=tmp_path)

        content = Path(job.file_path).read_text(encoding="utf-8")
        assert "123 Main St" not in content

    def test_multiple_subjects(self, db_session, tmp_path):
        project = _make_project(db_session)
        _make_subject(db_session, project.id, name="Alice")
        _make_subject(db_session, project.id, name="Bob")
        _make_subject(db_session, project.id, name="Charlie")

        exporter = CSVExporter(db_session)
        job = exporter.run(project.id, output_dir=tmp_path)

        assert job.row_count == 3

    def test_empty_project(self, db_session, tmp_path):
        project = _make_project(db_session)

        exporter = CSVExporter(db_session)
        job = exporter.run(project.id, output_dir=tmp_path)

        assert job.status == "completed"
        assert job.row_count == 0
        content = Path(job.file_path).read_text(encoding="utf-8")
        reader = csv.reader(io.StringIO(content))
        header = next(reader)
        assert header == DEFAULT_EXPORT_FIELDS
        remaining = list(reader)
        assert remaining == []

    def test_with_protocol_config_fields(self, db_session, tmp_path):
        project = _make_project(db_session)
        pc = _make_protocol_config(
            db_session,
            project.id,
            config_json={"export_fields": ["canonical_name", "review_status"]},
        )
        _make_subject(db_session, project.id)

        exporter = CSVExporter(db_session)
        job = exporter.run(
            project.id,
            output_dir=tmp_path,
            protocol_config_id=pc.id,
        )

        content = Path(job.file_path).read_text(encoding="utf-8")
        reader = csv.reader(io.StringIO(content))
        header = next(reader)
        assert header == ["canonical_name", "review_status"]

    def test_confidence_threshold_filter(self, db_session, tmp_path):
        project = _make_project(db_session)
        _make_subject(db_session, project.id, name="High", merge_confidence=0.95)
        _make_subject(db_session, project.id, name="Low", merge_confidence=0.30)

        exporter = CSVExporter(db_session)
        job = exporter.run(
            project.id,
            output_dir=tmp_path,
            filters={"confidence_threshold": 0.80},
        )

        assert job.row_count == 1

    def test_review_status_filter(self, db_session, tmp_path):
        project = _make_project(db_session)
        _make_subject(db_session, project.id, name="Approved", status="APPROVED")
        _make_subject(db_session, project.id, name="Pending", status="AI_PENDING")

        exporter = CSVExporter(db_session)
        job = exporter.run(
            project.id,
            output_dir=tmp_path,
            filters={"review_status": "APPROVED"},
        )

        assert job.row_count == 1

    def test_entity_types_filter(self, db_session, tmp_path):
        project = _make_project(db_session)
        _make_subject(db_session, project.id, name="SSN Person", pii_types=["US_SSN"])
        _make_subject(db_session, project.id, name="Email Person", pii_types=["EMAIL_ADDRESS"])

        exporter = CSVExporter(db_session)
        job = exporter.run(
            project.id,
            output_dir=tmp_path,
            filters={"entity_types": ["US_SSN"]},
        )

        assert job.row_count == 1

    def test_filters_stored_in_job(self, db_session, tmp_path):
        project = _make_project(db_session)
        filters = {"confidence_threshold": 0.80}

        exporter = CSVExporter(db_session)
        job = exporter.run(project.id, output_dir=tmp_path, filters=filters)

        assert job.filters_json == filters

    def test_export_job_persisted(self, db_session, tmp_path):
        project = _make_project(db_session)
        _make_subject(db_session, project.id)

        exporter = CSVExporter(db_session)
        job = exporter.run(project.id, output_dir=tmp_path)

        # Re-query from DB
        from sqlalchemy import select
        found = db_session.execute(
            select(ExportJob).where(ExportJob.id == job.id)
        ).scalar_one_or_none()
        assert found is not None
        assert found.status == "completed"
        assert found.row_count == 1


# ===========================================================================
# API endpoints
# ===========================================================================


class TestExportAPI:
    def test_create_export(self, db_session, client):
        # Create project + subject via DB
        project = _make_project(db_session)
        _make_subject(db_session, project.id)

        resp = client.post(
            f"/projects/{project.id}/exports",
            json={},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "completed"
        assert data["export_type"] == "csv"
        assert data["row_count"] == 1
        assert data["project_id"] == str(project.id)

    def test_create_export_project_not_found(self, client):
        resp = client.post(
            f"/projects/{uuid4()}/exports",
            json={},
        )
        assert resp.status_code == 404

    def test_list_exports(self, db_session, client):
        project = _make_project(db_session)
        _make_subject(db_session, project.id)

        # Create two exports
        client.post(f"/projects/{project.id}/exports", json={})
        client.post(f"/projects/{project.id}/exports", json={})

        resp = client.get(f"/projects/{project.id}/exports")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_list_exports_empty(self, db_session, client):
        project = _make_project(db_session)
        resp = client.get(f"/projects/{project.id}/exports")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_exports_project_not_found(self, client):
        resp = client.get(f"/projects/{uuid4()}/exports")
        assert resp.status_code == 404

    def test_get_export(self, db_session, client):
        project = _make_project(db_session)
        _make_subject(db_session, project.id)

        create_resp = client.post(f"/projects/{project.id}/exports", json={})
        export_id = create_resp.json()["id"]

        resp = client.get(f"/projects/{project.id}/exports/{export_id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == export_id
        assert resp.json()["status"] == "completed"

    def test_get_export_not_found(self, db_session, client):
        project = _make_project(db_session)
        resp = client.get(f"/projects/{project.id}/exports/{uuid4()}")
        assert resp.status_code == 404

    def test_download_export(self, db_session, client):
        project = _make_project(db_session)
        _make_subject(db_session, project.id, name="Download Test")

        create_resp = client.post(f"/projects/{project.id}/exports", json={})
        export_id = create_resp.json()["id"]

        resp = client.get(f"/projects/{project.id}/exports/{export_id}/download")
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]
        assert "canonical_name" in resp.text
        assert "Download Test" in resp.text

    def test_download_no_raw_email(self, db_session, client):
        project = _make_project(db_session)
        _make_subject(db_session, project.id, email="confidential@corp.com")

        create_resp = client.post(f"/projects/{project.id}/exports", json={})
        export_id = create_resp.json()["id"]

        resp = client.get(f"/projects/{project.id}/exports/{export_id}/download")
        assert resp.status_code == 200
        assert "confidential@corp.com" not in resp.text

    def test_download_not_found(self, db_session, client):
        project = _make_project(db_session)
        resp = client.get(f"/projects/{project.id}/exports/{uuid4()}/download")
        assert resp.status_code == 404

    def test_create_with_protocol_config(self, db_session, client):
        project = _make_project(db_session)
        pc = _make_protocol_config(
            db_session,
            project.id,
            config_json={"export_fields": ["canonical_name", "review_status"]},
        )
        _make_subject(db_session, project.id)

        resp = client.post(
            f"/projects/{project.id}/exports",
            json={"protocol_config_id": str(pc.id)},
        )
        assert resp.status_code == 200
        assert resp.json()["protocol_config_id"] == str(pc.id)

        # Download and verify columns
        export_id = resp.json()["id"]
        dl = client.get(f"/projects/{project.id}/exports/{export_id}/download")
        reader = csv.reader(io.StringIO(dl.text))
        header = next(reader)
        assert header == ["canonical_name", "review_status"]

    def test_create_with_filters(self, db_session, client):
        project = _make_project(db_session)
        _make_subject(db_session, project.id, name="High", merge_confidence=0.95)
        _make_subject(db_session, project.id, name="Low", merge_confidence=0.30)

        resp = client.post(
            f"/projects/{project.id}/exports",
            json={"filters": {"confidence_threshold": 0.80}},
        )
        assert resp.status_code == 200
        assert resp.json()["row_count"] == 1

    def test_response_shape(self, db_session, client):
        project = _make_project(db_session)
        _make_subject(db_session, project.id)

        resp = client.post(f"/projects/{project.id}/exports", json={})
        data = resp.json()

        assert "id" in data
        assert "project_id" in data
        assert "export_type" in data
        assert "status" in data
        assert "file_path" in data
        assert "row_count" in data
        assert "filters_json" in data
        assert "created_at" in data
        assert "completed_at" in data
