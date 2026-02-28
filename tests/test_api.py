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
    NotificationList,
    NotificationSubject,
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

    def test_streaming_run_emits_all_stages(self, client: TestClient) -> None:
        """POST /jobs/run returns SSE events for each pipeline stage."""
        from unittest.mock import patch, MagicMock

        # Upload a file first
        up_resp = client.post(
            "/jobs/upload",
            files=[("files", ("data.csv", b"name\nJohn", "text/csv"))],
        )
        upload_id = up_resp.json()["upload_id"]

        with (
            patch("app.api.routes.jobs.FilesystemConnector"),
            patch("app.api.routes.jobs.DiscoveryTask") as mock_disc,
            patch("app.api.routes.jobs.EntityResolver") as mock_er,
            patch("app.api.routes.jobs.Deduplicator") as mock_dedup,
            patch("app.api.routes.jobs.build_notification_list") as mock_nl,
        ):
            mock_disc.return_value.run.return_value = [{"source_path": "/tmp/data.csv"}]
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
                    "/jobs/run",
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
