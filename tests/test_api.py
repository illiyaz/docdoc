"""Tests for the FastAPI routes.

Covers:
- GET /health — liveness check
- POST /jobs — pipeline execution (mocked internals)
- GET /jobs/{job_id} — job status lookup
- GET /jobs/{job_id}/results — masked NotificationSubjects
- GET /review/queues — counts per queue type
- GET /review/queues/{queue_type} — PENDING tasks for queue
- POST /review/tasks/{task_id}/assign — assign reviewer
- POST /review/tasks/{task_id}/complete — complete + workflow transition
- GET /audit/{subject_id}/history — audit events (no rationale)
- PIIFilterMiddleware — blocks raw PII in JSON responses
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db
from app.db.base import Base
from app.db.models import (
    AuditEvent,
    IngestionRun,
    NotificationList,
    NotificationSubject,
    Project,
    ReviewTask,
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


@pytest.fixture()
def pii_client() -> TestClient:
    """Minimal test app that exercises PIIFilterMiddleware in isolation."""
    from app.api.middleware.pii_filter import PIIFilterMiddleware

    test_app = FastAPI()
    test_app.add_middleware(PIIFilterMiddleware)

    @test_app.get("/clean")
    def clean_response():
        return {"message": "Hello, world!"}

    @test_app.get("/ssn")
    def ssn_response():
        return {"value": "123-45-6789"}

    @test_app.get("/text")
    def text_response():
        return PlainTextResponse("raw text 123-45-6789")

    with TestClient(test_app, raise_server_exceptions=False) as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_subject(
    db_session: Session,
    *,
    name: str = "Jane Doe",
    email: str | None = "jane@example.com",
    phone: str | None = "+12025551234",
    status: str = "AI_PENDING",
    notification_required: bool = True,
) -> NotificationSubject:
    ns = NotificationSubject(
        subject_id=uuid4(),
        canonical_name=name,
        canonical_email=email,
        canonical_phone=phone,
        canonical_address={"street": "123 Main St", "city": "DC", "state": "DC", "zip": "20001"},
        pii_types_found=["US_SSN"],
        notification_required=notification_required,
        review_status=status,
    )
    db_session.add(ns)
    db_session.flush()
    return ns


def _make_notification_list(
    db_session: Session,
    job_id: str,
    subject_ids: list[str],
) -> NotificationList:
    nl = NotificationList(
        notification_list_id=uuid4(),
        job_id=job_id,
        protocol_id="hipaa_breach_rule",
        subject_ids=subject_ids,
        status="PENDING",
    )
    db_session.add(nl)
    db_session.flush()
    return nl


def _make_review_task(
    db_session: Session,
    subject_id,
    queue_type: str = "low_confidence",
    role: str = "REVIEWER",
) -> ReviewTask:
    task = ReviewTask(
        review_task_id=uuid4(),
        queue_type=queue_type,
        subject_id=subject_id,
        status="PENDING",
        required_role=role,
    )
    db_session.add(task)
    db_session.flush()
    return task


def _make_audit_event(
    db_session: Session,
    subject_id: str,
    event_type: str = "human_review",
    actor: str = "reviewer-1",
    rationale: str = "Confirmed correct",
) -> AuditEvent:
    ev = AuditEvent(
        audit_event_id=uuid4(),
        event_type=event_type,
        actor=actor,
        subject_id=subject_id,
        rationale=rationale,
        immutable=True,
        timestamp=datetime.now(timezone.utc),
    )
    db_session.add(ev)
    db_session.flush()
    return ev


# ===========================================================================
# Health endpoint
# ===========================================================================


class TestHealth:
    def test_health_returns_200(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_health_body_has_status_ok(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert resp.json()["status"] == "ok"

    def test_health_body_has_version(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert "version" in resp.json()

    def test_health_content_type_is_json(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert "application/json" in resp.headers["content-type"]


# ===========================================================================
# GET /jobs/{job_id}
# ===========================================================================


class TestGetJob:
    def test_found(self, db_session: Session, client: TestClient) -> None:
        _make_notification_list(db_session, "job-1", ["sid-1"])
        resp = client.get("/jobs/job-1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["job_id"] == "job-1"
        assert data["protocol_id"] == "hipaa_breach_rule"
        assert data["status"] == "PENDING"
        assert data["subject_count"] == 1

    def test_not_found(self, client: TestClient) -> None:
        resp = client.get("/jobs/nonexistent")
        assert resp.status_code == 404


# ===========================================================================
# GET /jobs/{job_id}/results
# ===========================================================================


class TestGetJobResults:
    def test_returns_masked_email(self, db_session: Session, client: TestClient) -> None:
        subj = _make_subject(db_session, email="jane@example.com")
        _make_notification_list(db_session, "job-mask", [str(subj.subject_id)])
        resp = client.get("/jobs/job-mask/results")
        assert resp.status_code == 200
        results = resp.json()
        assert len(results) == 1
        assert results[0]["canonical_email"] == "***@***.***"

    def test_returns_masked_phone(self, db_session: Session, client: TestClient) -> None:
        subj = _make_subject(db_session, phone="+12025551234")
        _make_notification_list(db_session, "job-mask2", [str(subj.subject_id)])
        resp = client.get("/jobs/job-mask2/results")
        results = resp.json()
        assert results[0]["canonical_phone"] == "***-***-1234"

    def test_null_email_stays_null(self, db_session: Session, client: TestClient) -> None:
        subj = _make_subject(db_session, email=None)
        _make_notification_list(db_session, "job-null", [str(subj.subject_id)])
        resp = client.get("/jobs/job-null/results")
        results = resp.json()
        assert results[0]["canonical_email"] is None

    def test_null_phone_stays_null(self, db_session: Session, client: TestClient) -> None:
        subj = _make_subject(db_session, phone=None)
        _make_notification_list(db_session, "job-null2", [str(subj.subject_id)])
        resp = client.get("/jobs/job-null2/results")
        results = resp.json()
        assert results[0]["canonical_phone"] is None

    def test_no_raw_email_in_response(self, db_session: Session, client: TestClient) -> None:
        subj = _make_subject(db_session, email="jane@example.com")
        _make_notification_list(db_session, "job-safe", [str(subj.subject_id)])
        resp = client.get("/jobs/job-safe/results")
        assert "jane@example.com" not in resp.text

    def test_no_raw_phone_in_response(self, db_session: Session, client: TestClient) -> None:
        subj = _make_subject(db_session, phone="+12025551234")
        _make_notification_list(db_session, "job-safe2", [str(subj.subject_id)])
        resp = client.get("/jobs/job-safe2/results")
        assert "+12025551234" not in resp.text

    def test_address_not_in_response(self, db_session: Session, client: TestClient) -> None:
        subj = _make_subject(db_session)
        _make_notification_list(db_session, "job-addr", [str(subj.subject_id)])
        resp = client.get("/jobs/job-addr/results")
        assert "123 Main St" not in resp.text

    def test_not_found(self, client: TestClient) -> None:
        resp = client.get("/jobs/nonexistent/results")
        assert resp.status_code == 404

    def test_response_shape(self, db_session: Session, client: TestClient) -> None:
        subj = _make_subject(db_session)
        _make_notification_list(db_session, "job-shape", [str(subj.subject_id)])
        resp = client.get("/jobs/job-shape/results")
        item = resp.json()[0]
        assert "subject_id" in item
        assert "canonical_name" in item
        assert "pii_types_found" in item
        assert "notification_required" in item
        assert "review_status" in item


# ===========================================================================
# GET /review/queues
# ===========================================================================


class TestReviewQueues:
    def test_empty_queues(self, client: TestClient) -> None:
        resp = client.get("/review/queues")
        assert resp.status_code == 200
        data = resp.json()
        assert data["low_confidence"] == 0
        assert data["escalation"] == 0
        assert data["qc_sampling"] == 0
        assert data["rra_review"] == 0

    def test_with_tasks(self, db_session: Session, client: TestClient) -> None:
        subj = _make_subject(db_session)
        _make_review_task(db_session, subj.subject_id, "low_confidence")
        _make_review_task(db_session, subj.subject_id, "escalation", role="LEGAL_REVIEWER")
        resp = client.get("/review/queues")
        data = resp.json()
        assert data["low_confidence"] == 1
        assert data["escalation"] == 1


# ===========================================================================
# GET /review/queues/{queue_type}
# ===========================================================================


class TestReviewQueueList:
    def test_valid_queue_type(self, db_session: Session, client: TestClient) -> None:
        subj = _make_subject(db_session)
        task = _make_review_task(db_session, subj.subject_id, "low_confidence")
        resp = client.get("/review/queues/low_confidence")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert items[0]["review_task_id"] == str(task.review_task_id)

    def test_invalid_queue_type(self, client: TestClient) -> None:
        resp = client.get("/review/queues/invalid_queue")
        assert resp.status_code == 400


# ===========================================================================
# POST /review/tasks/{task_id}/assign
# ===========================================================================


class TestReviewAssign:
    def test_valid_assignment(self, db_session: Session, client: TestClient) -> None:
        subj = _make_subject(db_session)
        task = _make_review_task(db_session, subj.subject_id, "low_confidence")
        resp = client.post(
            f"/review/tasks/{task.review_task_id}/assign",
            json={"reviewer_id": "rev-1", "role": "REVIEWER"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "IN_PROGRESS"
        assert resp.json()["assigned_to"] == "rev-1"

    def test_wrong_role(self, db_session: Session, client: TestClient) -> None:
        subj = _make_subject(db_session)
        task = _make_review_task(db_session, subj.subject_id, "low_confidence")
        resp = client.post(
            f"/review/tasks/{task.review_task_id}/assign",
            json={"reviewer_id": "rev-1", "role": "QC_SAMPLER"},
        )
        assert resp.status_code == 400

    def test_not_found(self, client: TestClient) -> None:
        resp = client.post(
            f"/review/tasks/{uuid4()}/assign",
            json={"reviewer_id": "rev-1", "role": "REVIEWER"},
        )
        assert resp.status_code == 404


# ===========================================================================
# POST /review/tasks/{task_id}/complete
# ===========================================================================


class TestReviewComplete:
    def test_approved_transitions_subject(self, db_session: Session, client: TestClient) -> None:
        subj = _make_subject(db_session, status="HUMAN_REVIEW")
        task = _make_review_task(db_session, subj.subject_id, "low_confidence")
        # assign first
        task.status = "IN_PROGRESS"
        task.assigned_to = "rev-1"
        db_session.flush()

        resp = client.post(
            f"/review/tasks/{task.review_task_id}/complete",
            json={
                "reviewer_id": "rev-1",
                "role": "REVIEWER",
                "decision": "approved",
                "rationale": "All correct",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "COMPLETED"
        assert data["subject_review_status"] == "APPROVED"

    def test_rejected(self, db_session: Session, client: TestClient) -> None:
        subj = _make_subject(db_session, status="HUMAN_REVIEW")
        task = _make_review_task(db_session, subj.subject_id, "low_confidence")
        task.status = "IN_PROGRESS"
        task.assigned_to = "rev-1"
        db_session.flush()

        resp = client.post(
            f"/review/tasks/{task.review_task_id}/complete",
            json={
                "reviewer_id": "rev-1",
                "role": "REVIEWER",
                "decision": "rejected",
                "rationale": "False positive",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["subject_review_status"] == "REJECTED"

    def test_escalated(self, db_session: Session, client: TestClient) -> None:
        subj = _make_subject(db_session, status="HUMAN_REVIEW")
        task = _make_review_task(db_session, subj.subject_id, "low_confidence")
        task.status = "IN_PROGRESS"
        task.assigned_to = "rev-1"
        db_session.flush()

        resp = client.post(
            f"/review/tasks/{task.review_task_id}/complete",
            json={
                "reviewer_id": "rev-1",
                "role": "REVIEWER",
                "decision": "escalated",
                "rationale": "Needs legal review",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["subject_review_status"] == "LEGAL_REVIEW"

    def test_not_found(self, client: TestClient) -> None:
        resp = client.post(
            f"/review/tasks/{uuid4()}/complete",
            json={
                "reviewer_id": "rev-1",
                "role": "REVIEWER",
                "decision": "approved",
                "rationale": "ok",
            },
        )
        assert resp.status_code == 404

    def test_empty_rationale_returns_400(self, db_session: Session, client: TestClient) -> None:
        subj = _make_subject(db_session, status="HUMAN_REVIEW")
        task = _make_review_task(db_session, subj.subject_id, "low_confidence")
        task.status = "IN_PROGRESS"
        task.assigned_to = "rev-1"
        db_session.flush()

        resp = client.post(
            f"/review/tasks/{task.review_task_id}/complete",
            json={
                "reviewer_id": "rev-1",
                "role": "REVIEWER",
                "decision": "approved",
                "rationale": "",
            },
        )
        assert resp.status_code == 400


# ===========================================================================
# GET /audit/{subject_id}/history
# ===========================================================================


class TestAuditHistory:
    def test_returns_events_in_order(self, db_session: Session, client: TestClient) -> None:
        sid = str(uuid4())
        _make_audit_event(db_session, sid, "human_review", "system", "triage")
        _make_audit_event(db_session, sid, "approval", "rev-1", "confirmed")
        resp = client.get(f"/audit/{sid}/history")
        assert resp.status_code == 200
        events = resp.json()
        assert len(events) == 2
        assert events[0]["event_type"] == "human_review"
        assert events[1]["event_type"] == "approval"

    def test_rationale_not_in_response(self, db_session: Session, client: TestClient) -> None:
        sid = str(uuid4())
        _make_audit_event(db_session, sid, "human_review", "system", "SECRET RATIONALE")
        resp = client.get(f"/audit/{sid}/history")
        events = resp.json()
        # rationale field must not appear in the response
        assert "rationale" not in events[0]
        assert "SECRET RATIONALE" not in resp.text

    def test_response_shape(self, db_session: Session, client: TestClient) -> None:
        sid = str(uuid4())
        _make_audit_event(db_session, sid, "approval", "legal-1", "ok")
        resp = client.get(f"/audit/{sid}/history")
        ev = resp.json()[0]
        assert "event_type" in ev
        assert "actor" in ev
        assert "decision" in ev
        assert "timestamp" in ev
        assert "regulatory_basis" in ev

    def test_not_found(self, client: TestClient) -> None:
        resp = client.get(f"/audit/{uuid4()}/history")
        assert resp.status_code == 404

    def test_actor_matches(self, db_session: Session, client: TestClient) -> None:
        sid = str(uuid4())
        _make_audit_event(db_session, sid, "human_review", "auto-triage", "test")
        resp = client.get(f"/audit/{sid}/history")
        assert resp.json()[0]["actor"] == "auto-triage"


# ===========================================================================
# PIIFilterMiddleware
# ===========================================================================


class TestPIIFilterMiddleware:
    def test_clean_json_passes_through(self, pii_client: TestClient) -> None:
        resp = pii_client.get("/clean")
        assert resp.status_code == 200
        assert resp.json() == {"message": "Hello, world!"}

    def test_ssn_in_json_is_blocked(self, pii_client: TestClient) -> None:
        resp = pii_client.get("/ssn")
        assert resp.status_code == 500

    def test_blocked_response_body(self, pii_client: TestClient) -> None:
        resp = pii_client.get("/ssn")
        assert "blocked" in resp.json()["detail"].lower()

    def test_non_json_passes_through(self, pii_client: TestClient) -> None:
        resp = pii_client.get("/text")
        assert resp.status_code == 200
        assert "123-45-6789" in resp.text

    def test_blocked_response_does_not_contain_pii(
        self, pii_client: TestClient
    ) -> None:
        resp = pii_client.get("/ssn")
        assert "123-45-6789" not in resp.text


# ===========================================================================
# POST /diagnostic/file
# ===========================================================================


class TestDiagnosticFile:
    def test_unsupported_file_type_returns_400(self, client: TestClient) -> None:
        resp = client.post(
            "/diagnostic/file",
            files={"file": ("test.xyz", b"hello", "application/octet-stream")},
            data={"protocol_id": "hipaa_breach_rule"},
        )
        assert resp.status_code == 400
        assert "Unsupported file type" in resp.json()["detail"]

    def test_unknown_protocol_returns_400(self, client: TestClient) -> None:
        resp = client.post(
            "/diagnostic/file",
            files={"file": ("test.csv", b"name,ssn\nJohn,123-45-6789", "text/csv")},
            data={"protocol_id": "nonexistent_protocol"},
        )
        assert resp.status_code == 400
        assert "Protocol not found" in resp.json()["detail"]

    def test_valid_csv_upload_returns_correct_shape(
        self, db_session: Session, client: TestClient,
    ) -> None:
        """Upload a small CSV and verify the response structure.

        Both get_reader and PresidioEngine are mocked so the test is
        fully self-contained and does not depend on optional reader deps.
        """
        from unittest.mock import patch
        from app.readers.base import ExtractedBlock

        fake_blocks = [
            ExtractedBlock(
                text="John Doe,123-45-6789",
                page_or_sheet=1,
                source_path="/tmp/test.csv",
                file_type="csv",
            ),
            ExtractedBlock(
                text="Jane Doe,987-65-4321",
                page_or_sheet=1,
                source_path="/tmp/test.csv",
                file_type="csv",
            ),
        ]

        class _FakeReader:
            def read(self):
                return fake_blocks

        class _FakeEngine:
            def analyze(self, blocks):  # type: ignore[override]
                results = []
                for b in blocks:
                    if not b.text.strip():
                        continue
                    results.append(type("Det", (), {
                        "block": b,
                        "entity_type": "US_SSN",
                        "start": 9,
                        "end": min(20, len(b.text)),
                        "score": 0.95,
                        "pattern_used": r"\d{3}-\d{2}-\d{4}",
                        "extraction_layer": "layer_1_pattern",
                    })())
                return results

        import app.api.routes.diagnostic as diag_mod

        orig_engine = diag_mod._create_presidio_engine
        diag_mod._create_presidio_engine = lambda: _FakeEngine()  # type: ignore[assignment]
        try:
            with patch("app.api.routes.diagnostic.get_reader", return_value=_FakeReader()):
                csv_content = b"name,ssn\nJohn Doe,123-45-6789\nJane Doe,987-65-4321\n"
                resp = client.post(
                    "/diagnostic/file",
                    files={"file": ("test.csv", csv_content, "text/csv")},
                    data={"protocol_id": "hipaa_breach_rule"},
                )
        finally:
            diag_mod._create_presidio_engine = orig_engine  # type: ignore[assignment]

        assert resp.status_code == 200
        data = resp.json()

        # Top-level shape
        assert data["file_name"] == "test.csv"
        assert data["file_type"] == "csv"
        assert "total_pages" in data
        assert "pages" in data
        assert "summary" in data

        # Summary shape
        summary = data["summary"]
        assert "total_pii_hits" in summary
        assert "by_entity_type" in summary
        assert "layer_distribution" in summary
        assert "low_confidence_hits" in summary
        assert "pages_skipped_by_onset" in summary
        assert "ocr_pages" in summary

        # Page shape — mock guarantees 1 page with 2 hits
        assert data["total_pages"] == 1
        page = data["pages"][0]
        assert "page_number" in page
        assert "page_type" in page
        assert "blocks_extracted" in page
        assert "pii_hits" in page

        # PII hit shape
        assert summary["total_pii_hits"] == 2
        hit = page["pii_hits"][0]
        assert hit["entity_type"] == "US_SSN"
        assert "masked_value" in hit
        assert hit["confidence"] == 0.95
        assert hit["extraction_layer"] == "layer_1_pattern"
        # No raw SSN in response
        assert "123-45-6789" not in resp.text
        assert "987-65-4321" not in resp.text


# ===========================================================================
# POST /jobs/upload  +  two-step job submission
# ===========================================================================


class TestUploadFiles:
    def test_upload_single_csv(self, client: TestClient, tmp_path) -> None:
        resp = client.post(
            "/jobs/upload",
            files=[("files", ("data.csv", b"name,ssn\nJohn,123-45-6789", "text/csv"))],
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "upload_id" in data
        assert data["file_count"] == 1
        assert data["files"][0]["name"] == "data.csv"

    def test_upload_multiple_files(self, client: TestClient) -> None:
        resp = client.post(
            "/jobs/upload",
            files=[
                ("files", ("a.csv", b"col1\nval1", "text/csv")),
                ("files", ("b.pdf", b"%PDF-fake", "application/pdf")),
            ],
        )
        assert resp.status_code == 200
        assert resp.json()["file_count"] == 2

    def test_upload_skips_unsupported(self, client: TestClient) -> None:
        resp = client.post(
            "/jobs/upload",
            files=[
                ("files", (".DS_Store", b"junk", "application/octet-stream")),
                ("files", ("notes.txt", b"hello", "text/plain")),
                ("files", ("data.csv", b"col\nval", "text/csv")),
            ],
        )
        assert resp.status_code == 200
        assert resp.json()["file_count"] == 1

    def test_upload_all_unsupported_returns_400(self, client: TestClient) -> None:
        resp = client.post(
            "/jobs/upload",
            files=[
                ("files", (".DS_Store", b"junk", "application/octet-stream")),
                ("files", ("readme.txt", b"hello", "text/plain")),
            ],
        )
        assert resp.status_code == 400
        assert "No supported files" in resp.json()["detail"]

    def test_create_job_with_upload_id(self, client: TestClient, monkeypatch) -> None:
        """Two-step flow: upload files, then submit job with upload_id."""
        # Step 1: upload
        up_resp = client.post(
            "/jobs/upload",
            files=[("files", ("data.csv", b"name\nJohn", "text/csv"))],
        )
        assert up_resp.status_code == 200
        upload_id = up_resp.json()["upload_id"]

        # Step 2: mock pipeline internals and submit with upload_id
        from unittest.mock import patch, MagicMock

        mock_connector = MagicMock()
        mock_discovery = MagicMock()
        mock_discovery.run.return_value = []

        with (
            patch("app.api.routes.jobs.FilesystemConnector", return_value=mock_connector),
            patch("app.api.routes.jobs.DiscoveryTask", return_value=mock_discovery),
            patch("app.api.routes.jobs.EntityResolver") as mock_er,
            patch("app.api.routes.jobs.Deduplicator") as mock_dedup,
            patch("app.api.routes.jobs.build_notification_list") as mock_nl,
        ):
            mock_er.return_value.resolve.return_value = []
            mock_dedup.return_value.build_subjects.return_value = []
            mock_nl.return_value = MagicMock()

            resp = client.post(
                "/jobs",
                json={"protocol_id": "hipaa_breach_rule", "upload_id": upload_id},
            )

        assert resp.status_code == 200
        assert resp.json()["status"] == "COMPLETE"

    def test_create_job_both_fields_returns_422(self, client: TestClient) -> None:
        resp = client.post(
            "/jobs",
            json={
                "protocol_id": "hipaa_breach_rule",
                "source_directory": "/some/path",
                "upload_id": "some-id",
            },
        )
        assert resp.status_code == 422

    def test_create_job_neither_field_returns_422(self, client: TestClient) -> None:
        resp = client.post(
            "/jobs",
            json={"protocol_id": "hipaa_breach_rule"},
        )
        assert resp.status_code == 422

    def test_expired_upload_returns_404(self, client: TestClient) -> None:
        resp = client.post(
            "/jobs",
            json={
                "protocol_id": "hipaa_breach_rule",
                "upload_id": "nonexistent-upload-id",
            },
        )
        assert resp.status_code == 404
        assert "not found or expired" in resp.json()["detail"]

    def test_streaming_run_emits_all_stages(self, db_session: Session, client: TestClient) -> None:
        """POST /jobs/run/stream returns SSE events for each pipeline stage."""
        from unittest.mock import patch, MagicMock

        # Upload a file first
        up_resp = client.post(
            "/jobs/upload",
            files=[("files", ("data.csv", b"name\nJohn", "text/csv"))],
        )
        upload_id = up_resp.json()["upload_id"]

        # Patch the session factory so the streaming endpoint uses the test DB
        _test_factory = sessionmaker(bind=db_session.get_bind())

        with (
            patch("app.api.routes.jobs.FilesystemConnector"),
            patch("app.api.routes.jobs.DiscoveryTask") as mock_disc,
            patch("app.api.routes.jobs.EntityResolver") as mock_er,
            patch("app.api.routes.jobs.Deduplicator") as mock_dedup,
            patch("app.api.routes.jobs.build_notification_list") as mock_nl,
            patch("app.api.deps._get_session_factory", return_value=_test_factory),
        ):
            mock_disc.return_value.run.return_value = [{"source_path": "/tmp/data.csv", "file_name": "data.csv"}]
            mock_er.return_value.resolve.return_value = []
            mock_dedup.return_value.build_subjects.return_value = []
            mock_nl.return_value = MagicMock()

            # Mock the reader + engine for the detection stage
            from app.readers.base import ExtractedBlock

            class _FakeReader:
                def read(self):
                    return [ExtractedBlock(text="John", page_or_sheet=1, source_path="/tmp/data.csv", file_type="csv")]

            class _FakeEngine:
                def analyze(self, blocks):
                    return []

            with (
                patch("app.readers.registry.get_reader", return_value=_FakeReader()),
                patch("app.pii.presidio_engine.PresidioEngine", return_value=_FakeEngine()),
            ):
                resp = client.post(
                    "/jobs/run/stream",
                    json={"protocol_id": "hipaa_breach_rule", "upload_id": upload_id},
                )

        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]

        # Parse SSE events
        import json
        events = []
        for line in resp.text.split("\n"):
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))

        # Should have events for all stages
        stages_seen = [e["stage"] for e in events]
        assert "discovery" in stages_seen
        assert "cataloging" in stages_seen
        assert "detection" in stages_seen
        assert "resolution" in stages_seen
        assert "deduplication" in stages_seen
        assert "notification" in stages_seen
        assert "complete" in stages_seen

        # Final event should have the result
        complete_event = [e for e in events if e["stage"] == "complete"][0]
        assert complete_event["result"]["status"] == "COMPLETE"

    def test_upload_cleanup_after_job(self, client: TestClient, monkeypatch) -> None:
        """Upload directory is removed after the job completes."""
        import os

        up_resp = client.post(
            "/jobs/upload",
            files=[("files", ("data.csv", b"name\nJohn", "text/csv"))],
        )
        upload_id = up_resp.json()["upload_id"]
        upload_dir = up_resp.json()["directory"]
        assert os.path.isdir(upload_dir)

        from unittest.mock import patch, MagicMock

        with (
            patch("app.api.routes.jobs.FilesystemConnector"),
            patch("app.api.routes.jobs.DiscoveryTask") as mock_disc,
            patch("app.api.routes.jobs.EntityResolver") as mock_er,
            patch("app.api.routes.jobs.Deduplicator") as mock_dedup,
            patch("app.api.routes.jobs.build_notification_list") as mock_nl,
        ):
            mock_disc.return_value.run.return_value = []
            mock_er.return_value.resolve.return_value = []
            mock_dedup.return_value.build_subjects.return_value = []
            mock_nl.return_value = MagicMock()

            client.post(
                "/jobs",
                json={"protocol_id": "hipaa_breach_rule", "upload_id": upload_id},
            )

        assert not os.path.isdir(upload_dir)


# ===========================================================================
# Projects CRUD
# ===========================================================================


class TestProjectsCRUD:
    def test_create_project(self, client: TestClient) -> None:
        resp = client.post("/projects", json={"name": "Test Breach"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Test Breach"
        assert data["status"] == "active"
        assert "id" in data

    def test_list_projects(self, client: TestClient) -> None:
        client.post("/projects", json={"name": "P1"})
        client.post("/projects", json={"name": "P2"})
        resp = client.get("/projects")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_get_project(self, client: TestClient) -> None:
        cr = client.post("/projects", json={"name": "Detail"}).json()
        resp = client.get(f"/projects/{cr['id']}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "Detail"
        assert "protocols" in resp.json()

    def test_get_project_not_found(self, client: TestClient) -> None:
        resp = client.get(f"/projects/{uuid4()}")
        assert resp.status_code == 404

    def test_update_project(self, client: TestClient) -> None:
        cr = client.post("/projects", json={"name": "Old"}).json()
        resp = client.patch(f"/projects/{cr['id']}", json={"name": "New", "status": "archived"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "New"
        assert resp.json()["status"] == "archived"

    def test_update_project_invalid_status(self, client: TestClient) -> None:
        cr = client.post("/projects", json={"name": "P"}).json()
        resp = client.patch(f"/projects/{cr['id']}", json={"status": "invalid"})
        assert resp.status_code == 400

    def test_catalog_summary_empty(self, client: TestClient) -> None:
        cr = client.post("/projects", json={"name": "Empty"}).json()
        resp = client.get(f"/projects/{cr['id']}/catalog-summary")
        assert resp.status_code == 200
        assert resp.json()["total_documents"] == 0

    def test_density_empty(self, client: TestClient) -> None:
        cr = client.post("/projects", json={"name": "Empty"}).json()
        resp = client.get(f"/projects/{cr['id']}/density")
        assert resp.status_code == 200
        assert resp.json()["project_summary"] is None


# ===========================================================================
# ProtocolConfig CRUD
# ===========================================================================


class TestProtocolConfigCRUD:
    def test_create_protocol_config(self, client: TestClient) -> None:
        pr = client.post("/projects", json={"name": "P"}).json()
        resp = client.post(
            f"/projects/{pr['id']}/protocols",
            json={
                "name": "HIPAA Custom",
                "base_protocol_id": "hipaa_breach_rule",
                "config_json": {"target_entity_types": ["US_SSN"], "sampling_rate": 0.1},
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "HIPAA Custom"
        assert data["status"] == "draft"
        assert data["config_json"]["sampling_rate"] == 0.1

    def test_list_protocol_configs(self, client: TestClient) -> None:
        pr = client.post("/projects", json={"name": "P"}).json()
        client.post(f"/projects/{pr['id']}/protocols", json={"name": "A", "config_json": {}})
        client.post(f"/projects/{pr['id']}/protocols", json={"name": "B", "config_json": {}})
        resp = client.get(f"/projects/{pr['id']}/protocols")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_get_protocol_config(self, client: TestClient) -> None:
        pr = client.post("/projects", json={"name": "P"}).json()
        pc = client.post(
            f"/projects/{pr['id']}/protocols",
            json={"name": "Test", "config_json": {"key": "val"}},
        ).json()
        resp = client.get(f"/projects/{pr['id']}/protocols/{pc['id']}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "Test"

    def test_update_protocol_config(self, client: TestClient) -> None:
        pr = client.post("/projects", json={"name": "P"}).json()
        pc = client.post(
            f"/projects/{pr['id']}/protocols",
            json={"name": "Old", "config_json": {}},
        ).json()
        resp = client.patch(
            f"/projects/{pr['id']}/protocols/{pc['id']}",
            json={"name": "New", "status": "active"},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "New"
        assert resp.json()["status"] == "active"

    def test_update_locked_returns_409(self, client: TestClient) -> None:
        pr = client.post("/projects", json={"name": "P"}).json()
        pc = client.post(
            f"/projects/{pr['id']}/protocols",
            json={"name": "Lock", "config_json": {}},
        ).json()
        # Lock it
        client.patch(f"/projects/{pr['id']}/protocols/{pc['id']}", json={"status": "locked"})
        # Try to edit
        resp = client.patch(
            f"/projects/{pr['id']}/protocols/{pc['id']}",
            json={"name": "Fail"},
        )
        assert resp.status_code == 409

    def test_not_found(self, client: TestClient) -> None:
        pr = client.post("/projects", json={"name": "P"}).json()
        resp = client.get(f"/projects/{pr['id']}/protocols/{uuid4()}")
        assert resp.status_code == 404


# ===========================================================================
# GET /protocols/base
# ===========================================================================


class TestBaseProtocols:
    def test_returns_all_base_protocols(self, client: TestClient) -> None:
        resp = client.get("/protocols/base")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 6  # at least the original 6 built-in protocols
        ids = [p["protocol_id"] for p in data]
        # Verify original protocols are present
        assert "hipaa_breach_rule" in ids
        assert "gdpr_article_33" in ids
        assert "ccpa" in ids
        assert "hitech" in ids
        assert "ferpa" in ids
        assert "state_breach_generic" in ids

    def test_new_protocols_included(self, client: TestClient) -> None:
        resp = client.get("/protocols/base")
        data = resp.json()
        ids = [p["protocol_id"] for p in data]
        assert "bipa" in ids
        assert "dpdpa" in ids

    def test_response_shape(self, client: TestClient) -> None:
        resp = client.get("/protocols/base")
        data = resp.json()
        for p in data:
            assert "protocol_id" in p
            assert "name" in p
            assert "jurisdiction" in p
            assert "regulatory_framework" in p
            assert "notification_deadline_days" in p


# ---------------------------------------------------------------------------
# Step 8b helpers
# ---------------------------------------------------------------------------


def _make_project(db_session: Session, name: str = "Test Project") -> Project:
    project = Project(name=name)
    db_session.add(project)
    db_session.flush()
    return project


def _make_ingestion_run(
    db_session: Session,
    *,
    project_id=None,
    status: str = "pending",
    source_path: str = "/data/test",
    started_at=None,
    completed_at=None,
    metrics: dict | None = None,
    error_summary: str | None = None,
) -> IngestionRun:
    run = IngestionRun(
        id=uuid4(),
        project_id=project_id,
        source_path=source_path,
        config_hash="abc123",
        code_version="0.1.0",
        initiated_by="test",
        status=status,
        started_at=started_at,
        completed_at=completed_at,
        metrics=metrics,
        error_summary=error_summary,
    )
    db_session.add(run)
    db_session.flush()
    return run


# ===========================================================================
# GET /projects/{id}/jobs (Step 8b)
# ===========================================================================


class TestProjectJobs:
    def test_list_jobs_for_project(self, db_session: Session, client: TestClient) -> None:
        project = _make_project(db_session)
        run = _make_ingestion_run(db_session, project_id=project.id, status="completed")
        resp = client.get(f"/projects/{project.id}/jobs")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["id"] == str(run.id)
        assert data[0]["status"] == "completed"
        assert data[0]["project_id"] == str(project.id)

    def test_empty_project_returns_empty_list(self, db_session: Session, client: TestClient) -> None:
        project = _make_project(db_session)
        resp = client.get(f"/projects/{project.id}/jobs")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_project_not_found(self, client: TestClient) -> None:
        resp = client.get(f"/projects/{uuid4()}/jobs")
        assert resp.status_code == 404

    def test_only_returns_project_jobs(self, db_session: Session, client: TestClient) -> None:
        """Jobs for other projects or unlinked jobs should not appear."""
        p1 = _make_project(db_session, name="P1")
        p2 = _make_project(db_session, name="P2")
        _make_ingestion_run(db_session, project_id=p1.id)
        _make_ingestion_run(db_session, project_id=p2.id)
        _make_ingestion_run(db_session)  # unlinked

        resp = client.get(f"/projects/{p1.id}/jobs")
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_response_shape(self, db_session: Session, client: TestClient) -> None:
        project = _make_project(db_session)
        _make_ingestion_run(
            db_session,
            project_id=project.id,
            started_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            completed_at=datetime(2025, 1, 1, 0, 5, 0, tzinfo=timezone.utc),
        )
        resp = client.get(f"/projects/{project.id}/jobs")
        job = resp.json()[0]
        assert "id" in job
        assert "project_id" in job
        assert "status" in job
        assert "source_path" in job
        assert "started_at" in job
        assert "completed_at" in job
        assert "created_at" in job
        assert "document_count" in job
        assert "duration_seconds" in job
        assert job["duration_seconds"] == 300.0

    def test_duration_none_when_not_completed(self, db_session: Session, client: TestClient) -> None:
        project = _make_project(db_session)
        _make_ingestion_run(db_session, project_id=project.id, status="running")
        resp = client.get(f"/projects/{project.id}/jobs")
        assert resp.json()[0]["duration_seconds"] is None


# ===========================================================================
# GET /jobs/{job_id}/status (Step 8b)
# ===========================================================================


class TestJobStatus:
    def test_returns_status(self, db_session: Session, client: TestClient) -> None:
        run = _make_ingestion_run(db_session, status="running")
        resp = client.get(f"/jobs/{run.id}/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == str(run.id)
        assert data["status"] == "running"

    def test_not_found(self, client: TestClient) -> None:
        resp = client.get(f"/jobs/{uuid4()}/status")
        assert resp.status_code == 404

    def test_has_8_stages(self, db_session: Session, client: TestClient) -> None:
        run = _make_ingestion_run(db_session)
        resp = client.get(f"/jobs/{run.id}/status")
        data = resp.json()
        assert len(data["stages"]) == 8
        stage_names = [s["name"] for s in data["stages"]]
        assert "Discovery" in stage_names
        assert "Cataloging" in stage_names
        assert "PII Detection" in stage_names
        assert "PII Extraction" in stage_names
        assert "Normalization" in stage_names
        assert "Entity Resolution" in stage_names
        assert "Quality Assurance" in stage_names
        assert "Notification" in stage_names

    def test_completed_run_all_stages_completed(self, db_session: Session, client: TestClient) -> None:
        run = _make_ingestion_run(db_session, status="completed")
        resp = client.get(f"/jobs/{run.id}/status")
        data = resp.json()
        assert data["progress_pct"] == 100.0
        for stage in data["stages"]:
            assert stage["status"] == "completed"

    def test_pending_run_no_progress(self, db_session: Session, client: TestClient) -> None:
        run = _make_ingestion_run(db_session, status="pending")
        resp = client.get(f"/jobs/{run.id}/status")
        data = resp.json()
        assert data["progress_pct"] == 0.0
        assert data["current_stage"] == "Discovery"

    def test_with_metrics_stages(self, db_session: Session, client: TestClient) -> None:
        """When metrics.stages is populated, those statuses are used."""
        metrics = {
            "stages": {
                "Discovery": {"status": "completed"},
                "Cataloging": {"status": "completed"},
                "PII Detection": {"status": "running"},
            }
        }
        run = _make_ingestion_run(db_session, status="running", metrics=metrics)
        resp = client.get(f"/jobs/{run.id}/status")
        data = resp.json()
        assert data["current_stage"] == "PII Detection"
        assert data["progress_pct"] == 25.0  # 2/8 completed

    def test_response_shape(self, db_session: Session, client: TestClient) -> None:
        run = _make_ingestion_run(db_session)
        resp = client.get(f"/jobs/{run.id}/status")
        data = resp.json()
        assert "id" in data
        assert "status" in data
        assert "project_id" in data
        assert "current_stage" in data
        assert "progress_pct" in data
        assert "stages" in data
        assert "started_at" in data
        assert "completed_at" in data
        assert "created_at" in data
        assert "error_summary" in data

    def test_stage_shape(self, db_session: Session, client: TestClient) -> None:
        run = _make_ingestion_run(db_session)
        resp = client.get(f"/jobs/{run.id}/status")
        stage = resp.json()["stages"][0]
        assert "name" in stage
        assert "status" in stage
        assert "started_at" in stage
        assert "completed_at" in stage
        assert "error_count" in stage

    def test_failed_run_stages(self, db_session: Session, client: TestClient) -> None:
        run = _make_ingestion_run(db_session, status="failed", error_summary="Out of memory")
        resp = client.get(f"/jobs/{run.id}/status")
        data = resp.json()
        assert data["error_summary"] == "Out of memory"
        for stage in data["stages"]:
            assert stage["status"] == "failed"


# ===========================================================================
# POST /jobs/run (Step 8b — returns job_id for polling)
# ===========================================================================


class TestRunJobPolling:
    def test_returns_job_id(self, db_session: Session, client: TestClient) -> None:
        resp = client.post(
            "/jobs/run",
            json={
                "protocol_id": "hipaa_breach_rule",
                "source_directory": "/data/test",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "job_id" in data
        assert data["status"] == "pending"

    def test_with_project_id(self, db_session: Session, client: TestClient) -> None:
        project = _make_project(db_session)
        resp = client.post(
            "/jobs/run",
            json={
                "protocol_id": "hipaa_breach_rule",
                "source_directory": "/data/test",
                "project_id": str(project.id),
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["project_id"] == str(project.id)

    def test_with_protocol_config_id(self, db_session: Session, client: TestClient) -> None:
        resp = client.post(
            "/jobs/run",
            json={
                "protocol_id": "hipaa_breach_rule",
                "source_directory": "/data/test",
                "protocol_config_id": str(uuid4()),
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["protocol_config_id"] is not None

    def test_invalid_protocol_returns_400(self, client: TestClient) -> None:
        resp = client.post(
            "/jobs/run",
            json={
                "protocol_id": "nonexistent",
                "source_directory": "/data/test",
            },
        )
        assert resp.status_code == 400

    def test_job_is_pollable_via_status(self, db_session: Session, client: TestClient) -> None:
        """After POST /jobs/run, the job should be queryable via GET /jobs/{id}/status."""
        run_resp = client.post(
            "/jobs/run",
            json={
                "protocol_id": "hipaa_breach_rule",
                "source_directory": "/data/test",
            },
        )
        job_id = run_resp.json()["job_id"]
        status_resp = client.get(f"/jobs/{job_id}/status")
        assert status_resp.status_code == 200
        assert status_resp.json()["status"] == "pending"


# ===========================================================================
# GET /jobs/recent (Step 8b)
# ===========================================================================


class TestRecentJobs:
    def test_returns_recent_jobs(self, db_session: Session, client: TestClient) -> None:
        _make_ingestion_run(db_session)
        _make_ingestion_run(db_session)
        resp = client.get("/jobs/recent")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_empty_returns_empty_list(self, client: TestClient) -> None:
        resp = client.get("/jobs/recent")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_unlinked_only(self, db_session: Session, client: TestClient) -> None:
        project = _make_project(db_session)
        _make_ingestion_run(db_session, project_id=project.id)  # linked
        _make_ingestion_run(db_session)  # unlinked
        _make_ingestion_run(db_session)  # unlinked

        resp = client.get("/jobs/recent?unlinked=true")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        for job in data:
            assert job["project_id"] is None

    def test_all_jobs_when_unlinked_false(self, db_session: Session, client: TestClient) -> None:
        project = _make_project(db_session)
        _make_ingestion_run(db_session, project_id=project.id)
        _make_ingestion_run(db_session)
        resp = client.get("/jobs/recent?unlinked=false")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_limit_parameter(self, db_session: Session, client: TestClient) -> None:
        for _ in range(5):
            _make_ingestion_run(db_session)
        resp = client.get("/jobs/recent?limit=3")
        assert resp.status_code == 200
        assert len(resp.json()) == 3

    def test_response_shape(self, db_session: Session, client: TestClient) -> None:
        _make_ingestion_run(db_session)
        resp = client.get("/jobs/recent")
        job = resp.json()[0]
        assert "id" in job
        assert "project_id" in job
        assert "status" in job
        assert "source_path" in job
        assert "created_at" in job
        assert "document_count" in job


# ===========================================================================
# PATCH /jobs/{job_id} (Step 8b — link job to project)
# ===========================================================================


class TestPatchJob:
    def test_link_job_to_project(self, db_session: Session, client: TestClient) -> None:
        project = _make_project(db_session)
        run = _make_ingestion_run(db_session)
        resp = client.patch(
            f"/jobs/{run.id}",
            json={"project_id": str(project.id)},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["project_id"] == str(project.id)

    def test_job_not_found(self, client: TestClient) -> None:
        resp = client.patch(
            f"/jobs/{uuid4()}",
            json={"project_id": str(uuid4())},
        )
        assert resp.status_code == 404

    def test_project_not_found(self, db_session: Session, client: TestClient) -> None:
        run = _make_ingestion_run(db_session)
        resp = client.patch(
            f"/jobs/{run.id}",
            json={"project_id": str(uuid4())},
        )
        assert resp.status_code == 404
        assert "Project" in resp.json()["detail"]

    def test_already_linked_same_project_is_idempotent(self, db_session: Session, client: TestClient) -> None:
        project = _make_project(db_session)
        run = _make_ingestion_run(db_session, project_id=project.id)
        resp = client.patch(
            f"/jobs/{run.id}",
            json={"project_id": str(project.id)},
        )
        assert resp.status_code == 200
        assert resp.json()["project_id"] == str(project.id)

    def test_already_linked_different_project_returns_409(self, db_session: Session, client: TestClient) -> None:
        p1 = _make_project(db_session, name="P1")
        p2 = _make_project(db_session, name="P2")
        run = _make_ingestion_run(db_session, project_id=p1.id)
        resp = client.patch(
            f"/jobs/{run.id}",
            json={"project_id": str(p2.id)},
        )
        assert resp.status_code == 409
        assert "already linked" in resp.json()["detail"]

    def test_linked_job_appears_in_project_jobs(self, db_session: Session, client: TestClient) -> None:
        project = _make_project(db_session)
        run = _make_ingestion_run(db_session)

        # Initially no jobs for the project
        resp1 = client.get(f"/projects/{project.id}/jobs")
        assert len(resp1.json()) == 0

        # Link the job
        client.patch(f"/jobs/{run.id}", json={"project_id": str(project.id)})

        # Now the job should appear
        resp2 = client.get(f"/projects/{project.id}/jobs")
        assert len(resp2.json()) == 1
        assert resp2.json()[0]["id"] == str(run.id)


# ===========================================================================
# PII filter allowlist — catalog-summary not blocked by UUID patterns
# ===========================================================================


class TestPIIFilterAllowlist:
    """Verify that project/protocol endpoints are not blocked by the PII
    filter middleware when pii_masking_enabled is True.

    UUIDs in JSON responses (project_id, etc.) previously matched the
    credit-card regex, causing false 500 responses.
    """

    def test_catalog_summary_not_blocked(
        self, db_session: Session, client: TestClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from app.core.settings import get_settings
        get_settings.cache_clear()
        monkeypatch.setenv("PII_MASKING_ENABLED", "true")
        get_settings.cache_clear()

        pr = client.post("/projects", json={"name": "PII Test"})
        assert pr.status_code == 200
        pid = pr.json()["id"]

        resp = client.get(f"/projects/{pid}/catalog-summary")
        assert resp.status_code == 200
        assert resp.json()["total_documents"] == 0
        get_settings.cache_clear()

    def test_project_detail_not_blocked(
        self, db_session: Session, client: TestClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from app.core.settings import get_settings
        get_settings.cache_clear()
        monkeypatch.setenv("PII_MASKING_ENABLED", "true")
        get_settings.cache_clear()

        pr = client.post("/projects", json={"name": "PII Detail"})
        assert pr.status_code == 200
        pid = pr.json()["id"]

        resp = client.get(f"/projects/{pid}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "PII Detail"
        get_settings.cache_clear()

    def test_density_not_blocked(
        self, db_session: Session, client: TestClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from app.core.settings import get_settings
        get_settings.cache_clear()
        monkeypatch.setenv("PII_MASKING_ENABLED", "true")
        get_settings.cache_clear()

        pr = client.post("/projects", json={"name": "PII Density"})
        assert pr.status_code == 200
        pid = pr.json()["id"]

        resp = client.get(f"/projects/{pid}/density")
        assert resp.status_code == 200
        get_settings.cache_clear()
