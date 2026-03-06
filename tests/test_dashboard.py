"""Tests for Step 16: Dashboard summary endpoint.

Verifies:
- Empty state: no data → all counts zero, empty lists
- With projects: correct counts
- Pending reviews: appear in needs_attention
- Running job: appears in running_jobs with progress
- Recent activity: job completion, ordering, limit
- Active projects: ordered by last_activity DESC
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
from app.db.models import (
    Document,
    DocumentAnalysisReview,
    ExportJob,
    IngestionRun,
    Project,
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


def _make_doc(db: Session, run: IngestionRun, *, file_name: str = "test.pdf", status: str = "discovered", **kw) -> Document:
    defaults = {
        "id": uuid4(),
        "ingestion_run_id": run.id,
        "file_name": file_name,
        "file_type": "pdf",
        "source_path": f"/tmp/{file_name}",
        "sha256": uuid4().hex,
        "status": status,
    }
    defaults.update(kw)
    doc = Document(**defaults)
    db.add(doc)
    db.flush()
    return doc


def _make_review(db: Session, doc: Document, run: IngestionRun, *, status: str = "pending_review", **kw) -> DocumentAnalysisReview:
    defaults = {
        "id": uuid4(),
        "document_id": doc.id,
        "ingestion_run_id": run.id,
        "status": status,
    }
    defaults.update(kw)
    review = DocumentAnalysisReview(**defaults)
    db.add(review)
    db.flush()
    return review


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
# Tests
# ---------------------------------------------------------------------------

class TestDashboardEmpty:
    """Empty database returns zeros and empty lists."""

    def test_empty_state(self, client: TestClient) -> None:
        resp = client.get("/dashboard/summary")
        assert resp.status_code == 200
        data = resp.json()

        assert data["stats"]["active_projects"] == 0
        assert data["stats"]["pending_reviews"] == 0
        assert data["stats"]["jobs_this_week"] == 0
        assert data["stats"]["documents_processed"] == 0
        assert data["needs_attention"] == []
        assert data["running_jobs"] == []
        assert data["active_projects"] == []
        assert data["recent_activity"] == []


class TestDashboardStats:
    """Stats section counts correctly."""

    def test_with_projects(self, db_session: Session, client: TestClient) -> None:
        p1 = _make_project(db_session, name="Alpha")
        p2 = _make_project(db_session, name="Beta")
        _make_project(db_session, name="Archived", status="archived")

        run = _make_run(db_session, p1)
        _make_doc(db_session, run, status="processed")
        _make_doc(db_session, run, file_name="b.pdf", status="processed")

        resp = client.get("/dashboard/summary")
        data = resp.json()

        assert data["stats"]["active_projects"] == 2
        assert data["stats"]["documents_processed"] == 2
        assert data["stats"]["jobs_this_week"] >= 1


class TestNeedsAttention:
    """Pending reviews appear in needs_attention."""

    def test_pending_reviews_in_needs_attention(self, db_session: Session, client: TestClient) -> None:
        proj = _make_project(db_session, name="Review Me")
        run = _make_run(db_session, proj, status="analyzed")
        doc = _make_doc(db_session, run)
        _make_review(db_session, doc, run, status="pending_review")

        resp = client.get("/dashboard/summary")
        data = resp.json()

        assert data["stats"]["pending_reviews"] == 1
        assert len(data["needs_attention"]) == 1
        assert data["needs_attention"][0]["project_name"] == "Review Me"
        assert data["needs_attention"][0]["pending_count"] == 1


class TestRunningJobs:
    """Running jobs appear with progress."""

    def test_running_job_with_progress(self, db_session: Session, client: TestClient) -> None:
        proj = _make_project(db_session, name="Active Job")
        run = _make_run(db_session, proj, status="running", started_at=datetime.now(timezone.utc))
        _make_doc(db_session, run, status="processed")
        _make_doc(db_session, run, file_name="b.pdf", status="discovered")

        resp = client.get("/dashboard/summary")
        data = resp.json()

        assert len(data["running_jobs"]) == 1
        job = data["running_jobs"][0]
        assert job["job_id"] == str(run.id)
        assert job["project_name"] == "Active Job"
        assert job["document_count"] == 2
        assert job["progress_pct"] == 50.0


class TestRecentActivity:
    """Recent activity feed tests."""

    def test_job_completion_appears(self, db_session: Session, client: TestClient) -> None:
        proj = _make_project(db_session, name="Done")
        _make_run(
            db_session, proj, status="completed",
            completed_at=datetime.now(timezone.utc),
        )

        resp = client.get("/dashboard/summary")
        data = resp.json()

        job_events = [e for e in data["recent_activity"] if e["type"] == "job_completed"]
        assert len(job_events) >= 1
        assert job_events[0]["project_name"] == "Done"

    def test_ordered_by_timestamp_desc(self, db_session: Session, client: TestClient) -> None:
        proj = _make_project(db_session, name="Ordered")
        now = datetime.now(timezone.utc)

        _make_run(db_session, proj, status="completed", completed_at=now - timedelta(hours=2))
        _make_run(db_session, proj, status="completed", completed_at=now - timedelta(hours=1))

        resp = client.get("/dashboard/summary")
        data = resp.json()

        timestamps = [e["timestamp"] for e in data["recent_activity"] if e["timestamp"]]
        # Should be in descending order
        assert timestamps == sorted(timestamps, reverse=True)

    def test_activity_limit_20(self, db_session: Session, client: TestClient) -> None:
        """Maximum 20 activity items returned."""
        proj = _make_project(db_session, name="Many")
        now = datetime.now(timezone.utc)

        for i in range(25):
            _make_run(
                db_session, proj, status="completed",
                completed_at=now - timedelta(minutes=i),
            )

        resp = client.get("/dashboard/summary")
        data = resp.json()

        assert len(data["recent_activity"]) <= 20


class TestActiveProjects:
    """Active projects with stats, ordered by last activity."""

    def test_ordered_by_last_activity_desc(self, db_session: Session, client: TestClient) -> None:
        now = datetime.now(timezone.utc)

        p1 = _make_project(db_session, name="Old")
        r1 = _make_run(db_session, p1, status="completed")
        # Force an older updated_at by direct SQL-like approach
        r1.updated_at = now - timedelta(days=5)
        db_session.flush()

        p2 = _make_project(db_session, name="Recent")
        r2 = _make_run(db_session, p2, status="completed")
        r2.updated_at = now
        db_session.flush()

        resp = client.get("/dashboard/summary")
        data = resp.json()

        names = [p["name"] for p in data["active_projects"]]
        assert names.index("Recent") < names.index("Old")

    def test_includes_doc_count(self, db_session: Session, client: TestClient) -> None:
        proj = _make_project(db_session, name="WithDocs")
        run = _make_run(db_session, proj)
        _make_doc(db_session, run, file_name="a.pdf")
        _make_doc(db_session, run, file_name="b.pdf")

        resp = client.get("/dashboard/summary")
        data = resp.json()

        match = [p for p in data["active_projects"] if p["name"] == "WithDocs"]
        assert len(match) == 1
        assert match[0]["document_count"] == 2

    def test_includes_pending_reviews(self, db_session: Session, client: TestClient) -> None:
        proj = _make_project(db_session, name="Pending")
        run = _make_run(db_session, proj, status="analyzed")
        doc = _make_doc(db_session, run)
        _make_review(db_session, doc, run, status="pending_review")

        resp = client.get("/dashboard/summary")
        data = resp.json()

        match = [p for p in data["active_projects"] if p["name"] == "Pending"]
        assert len(match) == 1
        assert match[0]["pending_reviews"] == 1
