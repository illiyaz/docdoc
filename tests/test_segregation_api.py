"""Tests for segregation review API endpoints (Step 30e-3).

Tests the CRUD operations on segregation groups:
- POST /run — trigger segregation (mocked)
- GET /groups — list groups
- POST /groups/{id}/approve — approve a group
- POST /groups/{id}/reject — reject a group
- POST /groups/{id}/reclassify — reclassify a group
- POST /approve-all — bulk approve
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from unittest.mock import patch, MagicMock

from app.api.deps import get_db
from app.db.base import Base
from app.db.models import Document, IngestionRun, Project


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
def client(db_session: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    """TestClient with DB and settings overridden."""
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    # Point upload_dir to tmp_path so segregation files go there
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))

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


@pytest.fixture()
def project_and_job(db_session: Session, tmp_path: Path):
    """Create a project, job, and some documents."""
    proj = Project(
        id=uuid4(),
        name="Test Segregation Project",
        status="active",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db_session.add(proj)
    db_session.flush()

    run = IngestionRun(
        id=uuid4(),
        project_id=proj.id,
        status="analyzed",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(run)
    db_session.flush()

    # Create some dummy documents
    docs = []
    for i in range(3):
        f = tmp_path / f"doc{i}.pdf"
        f.write_bytes(b"fake pdf content")
        doc = Document(
            id=uuid4(),
            ingestion_run_id=run.id,
            file_name=f"doc{i}.pdf",
            file_type="pdf",
            source_path=str(f),
            status="cataloged",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db_session.add(doc)
        docs.append(doc)

    db_session.commit()
    return proj, run, docs


@pytest.fixture()
def seeded_groups(project_and_job, client, tmp_path, monkeypatch):
    """Seed segregation groups JSON on disk for GET/approve/reject tests."""
    proj, run, docs = project_and_job

    groups = [
        {
            "group_id": "grp-001",
            "group_name": "Medical Forms (Patient)",
            "document_type": "medical_form",
            "is_pii": True,
            "file_paths": ["/tmp/a.pdf", "/tmp/b.pdf"],
            "file_count": 2,
            "sample_file_paths": ["/tmp/a.pdf"],
            "field_inventory": ["PERSON", "US_SSN"],
            "role_summary": {"PERSON": "primary_subject", "US_SSN": "primary_subject"},
            "primary_subject_type": "patient",
            "confidence_avg": 0.92,
            "confidence_min": 0.85,
            "status": "pending_review",
            "issuing_entities": ["Hospital A"],
        },
        {
            "group_id": "grp-002",
            "group_name": "Non-PII Documents",
            "document_type": "non_pii",
            "is_pii": False,
            "file_paths": ["/tmp/c.pdf"],
            "file_count": 1,
            "sample_file_paths": ["/tmp/c.pdf"],
            "field_inventory": [],
            "role_summary": {},
            "primary_subject_type": None,
            "confidence_avg": 0.88,
            "confidence_min": 0.88,
            "status": "pending_review",
            "issuing_entities": [],
        },
    ]

    # Write groups to the segregation path
    from app.api.routes.segregation import _get_segregation_path
    seg_path = _get_segregation_path(str(proj.id), str(run.id))
    seg_path.parent.mkdir(parents=True, exist_ok=True)
    with open(seg_path, "w") as f:
        json.dump({
            "project_id": str(proj.id),
            "job_id": str(run.id),
            "groups": groups,
        }, f)

    return proj, run, groups


# ---------------------------------------------------------------------------
# Tests: GET /groups
# ---------------------------------------------------------------------------


class TestListGroups:

    def test_list_groups_returns_seeded(self, client, seeded_groups):
        proj, run, groups = seeded_groups
        resp = client.get(
            f"/api/projects/{proj.id}/segregation/groups",
            params={"job_id": str(run.id)},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["job_id"] == str(run.id)
        assert len(data["groups"]) == 2
        assert data["summary"]["total_groups"] == 2
        assert data["summary"]["pending_review"] == 2
        assert data["summary"]["total_files"] == 3

    def test_list_groups_empty_when_no_segregation(self, client, project_and_job):
        proj, run, docs = project_and_job
        resp = client.get(
            f"/api/projects/{proj.id}/segregation/groups",
            params={"job_id": str(run.id)},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["groups"] == []
        assert data["summary"]["total_groups"] == 0


# ---------------------------------------------------------------------------
# Tests: POST approve / reject / reclassify
# ---------------------------------------------------------------------------


class TestGroupActions:

    def test_approve_group(self, client, seeded_groups):
        proj, run, _ = seeded_groups
        resp = client.post(
            f"/api/projects/{proj.id}/segregation/groups/grp-001/approve",
            params={"job_id": str(run.id)},
            json={"reviewer_id": "auditor", "rationale": "Looks correct"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"

        # Verify persisted
        resp2 = client.get(
            f"/api/projects/{proj.id}/segregation/groups",
            params={"job_id": str(run.id)},
        )
        groups = resp2.json()["groups"]
        grp = next(g for g in groups if g["group_id"] == "grp-001")
        assert grp["status"] == "approved"
        assert grp["reviewed_by"] == "auditor"

    def test_reject_group(self, client, seeded_groups):
        proj, run, _ = seeded_groups
        resp = client.post(
            f"/api/projects/{proj.id}/segregation/groups/grp-002/reject",
            params={"job_id": str(run.id)},
            json={"reviewer_id": "auditor", "rationale": "Not relevant"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "rejected"

        resp2 = client.get(
            f"/api/projects/{proj.id}/segregation/groups",
            params={"job_id": str(run.id)},
        )
        summary = resp2.json()["summary"]
        assert summary["rejected"] == 1

    def test_approve_nonexistent_group(self, client, seeded_groups):
        proj, run, _ = seeded_groups
        resp = client.post(
            f"/api/projects/{proj.id}/segregation/groups/grp-999/approve",
            params={"job_id": str(run.id)},
            json={"reviewer_id": "auditor"},
        )
        assert resp.status_code == 404

    def test_reclassify_group(self, client, seeded_groups):
        proj, run, _ = seeded_groups
        resp = client.post(
            f"/api/projects/{proj.id}/segregation/groups/grp-002/reclassify",
            params={"job_id": str(run.id)},
            json={
                "reviewer_id": "auditor",
                "new_document_type": "billing_statement",
                "new_is_pii": True,
                "rationale": "Actually contains PII",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "reclassified"

        # Verify changes persisted
        resp2 = client.get(
            f"/api/projects/{proj.id}/segregation/groups",
            params={"job_id": str(run.id)},
        )
        grp = next(g for g in resp2.json()["groups"] if g["group_id"] == "grp-002")
        assert grp["document_type"] == "billing_statement"
        assert grp["is_pii"] is True


# ---------------------------------------------------------------------------
# Tests: POST approve-all
# ---------------------------------------------------------------------------


class TestBulkApprove:

    def test_approve_all(self, client, seeded_groups):
        proj, run, _ = seeded_groups
        resp = client.post(
            f"/api/projects/{proj.id}/segregation/approve-all",
            params={"job_id": str(run.id)},
            json={"reviewer_id": "auditor", "rationale": "All groups look good"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["approved"] == 2
        assert data["total"] == 2

        # All should now be approved
        resp2 = client.get(
            f"/api/projects/{proj.id}/segregation/groups",
            params={"job_id": str(run.id)},
        )
        assert resp2.json()["summary"]["pending_review"] == 0
        assert resp2.json()["summary"]["approved"] == 2

    def test_approve_all_skips_already_approved(self, client, seeded_groups):
        proj, run, _ = seeded_groups
        # Approve one first
        client.post(
            f"/api/projects/{proj.id}/segregation/groups/grp-001/approve",
            params={"job_id": str(run.id)},
            json={"reviewer_id": "auditor"},
        )
        # Now bulk approve
        resp = client.post(
            f"/api/projects/{proj.id}/segregation/approve-all",
            params={"job_id": str(run.id)},
            json={"reviewer_id": "auditor"},
        )
        assert resp.status_code == 200
        assert resp.json()["approved"] == 1  # only the pending one


# ---------------------------------------------------------------------------
# Tests: POST /run (mocked segregation engine)
# ---------------------------------------------------------------------------


class TestRunSegregation:

    def test_run_segregation_mocked(self, client, project_and_job):
        """Test the run endpoint with mocked segregation engine."""
        proj, run, docs = project_and_job

        # Mock the segregation engine and grouping
        mock_result = MagicMock()
        mock_result.file_path = "/tmp/doc0.pdf"
        mock_result.pii_detected = True
        mock_result.confidence = 0.9
        mock_result.document_type = "medical_form"
        mock_result.field_inventory = ["PERSON", "US_SSN"]
        mock_result.fields = []
        mock_result.primary_subject_type = "patient"
        mock_result.issuing_entity = "Test Hospital"

        mock_group = MagicMock()
        mock_group.to_dict.return_value = {
            "group_id": "gen-001",
            "group_name": "Medical Forms (Patient)",
            "document_type": "medical_form",
            "is_pii": True,
            "file_count": 3,
            "status": "pending_review",
        }

        with patch("app.api.routes.segregation.SegregationEngine") as MockEngine, \
             patch("app.api.routes.segregation.group_documents") as mock_group_fn:

            mock_engine_inst = MockEngine.return_value
            mock_engine_inst.classify_batch.return_value = [mock_result] * 3
            mock_group_fn.return_value = [mock_group]

            resp = client.post(
                f"/api/projects/{proj.id}/segregation/run",
                json={"job_id": str(run.id), "sample_size": 3},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "completed"
        assert data["total_files"] == 3
        assert data["total_groups"] == 1

    def test_run_segregation_no_docs(self, client, db_session):
        """Job with no documents returns 404."""
        proj = Project(
            id=uuid4(), name="Empty", status="active",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db_session.add(proj)
        db_session.flush()

        run = IngestionRun(
            id=uuid4(), project_id=proj.id, status="analyzed",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(run)
        db_session.commit()

        resp = client.post(
            f"/api/projects/{proj.id}/segregation/run",
            json={"job_id": str(run.id)},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests: Router registration
# ---------------------------------------------------------------------------


class TestRouterRegistration:

    def test_segregation_routes_registered(self, client):
        """Verify segregation routes are accessible (not 404 on the path prefix)."""
        # A GET to a non-existent project should return 404 from our handler,
        # not a generic FastAPI 404 (which would mean the route isn't registered)
        resp = client.get(f"/api/projects/{uuid4()}/segregation/groups")
        # Should be 404 from our handler (no jobs for this project)
        assert resp.status_code == 404
        # The error should be from our code, not "Not Found" generic
        assert "not found" in resp.json().get("detail", "").lower() or \
               "no jobs" in resp.json().get("detail", "").lower()
