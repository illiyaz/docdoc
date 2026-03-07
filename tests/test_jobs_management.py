"""Tests for Step 16b: Jobs tab backend — cancel, archive, filtering, pagination, filename.

Verifies:
- POST /jobs/{id}/cancel sets status to cancelled
- DELETE /jobs/{id} sets status to archived
- GET /projects/{id}/jobs with status filter returns correct subset
- GET /projects/{id}/jobs with pagination returns correct page
- Job response includes first_file_name
- Cancel only works on running/pending jobs
- Archive only works on completed/failed/cancelled/analyzed jobs
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db
from app.db.base import Base
from app.db.models import Document, IngestionRun, Project


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_RUN_DEFAULTS = dict(
    source_path="/tmp",
    config_hash="abc",
    code_version="0.1.0",
    initiated_by="test",
)


def _make_project(db: Session, *, name: str = "Test Project", **kw) -> Project:
    defaults = {"id": uuid4(), "name": name, "status": "active"}
    defaults.update(kw)
    proj = Project(**defaults)
    db.add(proj)
    db.flush()
    return proj


def _make_run(db: Session, project: Project | None = None, *, status: str = "completed", **kw) -> IngestionRun:
    defaults = {
        **_RUN_DEFAULTS,
        "id": uuid4(),
        "status": status,
        "project_id": project.id if project else None,
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


# ---------------------------------------------------------------------------
# Tests: Cancel
# ---------------------------------------------------------------------------

class TestCancelJob:
    """POST /jobs/{id}/cancel tests."""

    def test_cancel_running_job(self, db_session: Session, client: TestClient) -> None:
        run = _make_run(db_session, status="running", started_at=datetime.now(timezone.utc))
        resp = client.post(f"/jobs/{run.id}/cancel")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "cancelled"

    def test_cancel_pending_job(self, db_session: Session, client: TestClient) -> None:
        run = _make_run(db_session, status="pending")
        resp = client.post(f"/jobs/{run.id}/cancel")
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"

    def test_cancel_completed_job_fails(self, db_session: Session, client: TestClient) -> None:
        run = _make_run(db_session, status="completed")
        resp = client.post(f"/jobs/{run.id}/cancel")
        assert resp.status_code == 409

    def test_cancel_nonexistent_job(self, client: TestClient) -> None:
        resp = client.post(f"/jobs/{uuid4()}/cancel")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests: Archive
# ---------------------------------------------------------------------------

class TestArchiveJob:
    """DELETE /jobs/{id} tests."""

    def test_archive_completed_job(self, db_session: Session, client: TestClient) -> None:
        run = _make_run(db_session, status="completed")
        resp = client.delete(f"/jobs/{run.id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "archived"

    def test_archive_failed_job(self, db_session: Session, client: TestClient) -> None:
        run = _make_run(db_session, status="failed")
        resp = client.delete(f"/jobs/{run.id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "archived"

    def test_archive_cancelled_job(self, db_session: Session, client: TestClient) -> None:
        run = _make_run(db_session, status="cancelled")
        resp = client.delete(f"/jobs/{run.id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "archived"

    def test_archive_analyzed_job(self, db_session: Session, client: TestClient) -> None:
        run = _make_run(db_session, status="analyzed")
        resp = client.delete(f"/jobs/{run.id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "archived"

    def test_archive_running_job_fails(self, db_session: Session, client: TestClient) -> None:
        run = _make_run(db_session, status="running")
        resp = client.delete(f"/jobs/{run.id}")
        assert resp.status_code == 409

    def test_archive_nonexistent_job(self, client: TestClient) -> None:
        resp = client.delete(f"/jobs/{uuid4()}")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests: Filtering and Pagination
# ---------------------------------------------------------------------------

class TestJobsFiltering:
    """GET /projects/{id}/jobs with filters."""

    def test_status_filter(self, db_session: Session, client: TestClient) -> None:
        proj = _make_project(db_session)
        _make_run(db_session, proj, status="completed")
        _make_run(db_session, proj, status="completed")
        _make_run(db_session, proj, status="failed")

        resp = client.get(f"/projects/{proj.id}/jobs?status=completed")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert len(data["jobs"]) == 2
        assert all(j["status"] == "completed" for j in data["jobs"])

    def test_archived_excluded_by_default(self, db_session: Session, client: TestClient) -> None:
        proj = _make_project(db_session)
        _make_run(db_session, proj, status="completed")
        _make_run(db_session, proj, status="archived")

        resp = client.get(f"/projects/{proj.id}/jobs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["jobs"][0]["status"] == "completed"

    def test_archived_filter_shows_archived(self, db_session: Session, client: TestClient) -> None:
        proj = _make_project(db_session)
        _make_run(db_session, proj, status="completed")
        _make_run(db_session, proj, status="archived")

        resp = client.get(f"/projects/{proj.id}/jobs?status=archived")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["jobs"][0]["status"] == "archived"

    def test_pagination(self, db_session: Session, client: TestClient) -> None:
        proj = _make_project(db_session)
        now = datetime.now(timezone.utc)
        for i in range(15):
            _make_run(db_session, proj, status="completed")

        # Page 1
        resp = client.get(f"/projects/{proj.id}/jobs?per_page=5&page=1")
        data = resp.json()
        assert data["total"] == 15
        assert data["page"] == 1
        assert data["per_page"] == 5
        assert len(data["jobs"]) == 5

        # Page 3
        resp = client.get(f"/projects/{proj.id}/jobs?per_page=5&page=3")
        data = resp.json()
        assert len(data["jobs"]) == 5

        # Page 4 (last, partial)
        resp = client.get(f"/projects/{proj.id}/jobs?per_page=5&page=4")
        data = resp.json()
        assert data["total"] == 15

    def test_response_includes_pagination_fields(self, db_session: Session, client: TestClient) -> None:
        proj = _make_project(db_session)
        _make_run(db_session, proj, status="completed")

        resp = client.get(f"/projects/{proj.id}/jobs")
        data = resp.json()
        assert "jobs" in data
        assert "total" in data
        assert "page" in data
        assert "per_page" in data


# ---------------------------------------------------------------------------
# Tests: Response Fields
# ---------------------------------------------------------------------------

class TestJobResponseFields:
    """Job response includes new fields: first_file_name, pipeline_mode, analysis_completed_at."""

    def test_first_file_name(self, db_session: Session, client: TestClient) -> None:
        proj = _make_project(db_session)
        run = _make_run(db_session, proj, status="completed")
        _make_doc(db_session, run, file_name="report.pdf")

        resp = client.get(f"/projects/{proj.id}/jobs")
        data = resp.json()
        assert len(data["jobs"]) == 1
        assert data["jobs"][0]["first_file_name"] == "report.pdf"

    def test_no_documents_returns_null_filename(self, db_session: Session, client: TestClient) -> None:
        proj = _make_project(db_session)
        _make_run(db_session, proj, status="completed")

        resp = client.get(f"/projects/{proj.id}/jobs")
        data = resp.json()
        assert data["jobs"][0]["first_file_name"] is None

    def test_pipeline_mode_in_response(self, db_session: Session, client: TestClient) -> None:
        proj = _make_project(db_session)
        _make_run(db_session, proj, status="analyzed", pipeline_mode="two_phase")

        resp = client.get(f"/projects/{proj.id}/jobs")
        data = resp.json()
        assert data["jobs"][0]["pipeline_mode"] == "two_phase"

    def test_duration_uses_analysis_completed_at(self, db_session: Session, client: TestClient) -> None:
        proj = _make_project(db_session)
        now = datetime.now(timezone.utc)
        run = _make_run(
            db_session, proj,
            status="analyzed",
            pipeline_mode="two_phase",
            started_at=now - timedelta(seconds=120),
            analysis_completed_at=now,
        )

        resp = client.get(f"/projects/{proj.id}/jobs")
        data = resp.json()
        # Duration should be ~120 seconds (analysis_completed_at - started_at)
        assert data["jobs"][0]["duration_seconds"] is not None
        assert 119 <= data["jobs"][0]["duration_seconds"] <= 121

    def test_analysis_completed_at_in_response(self, db_session: Session, client: TestClient) -> None:
        proj = _make_project(db_session)
        now = datetime.now(timezone.utc)
        _make_run(db_session, proj, status="analyzed", analysis_completed_at=now)

        resp = client.get(f"/projects/{proj.id}/jobs")
        data = resp.json()
        assert data["jobs"][0]["analysis_completed_at"] is not None
