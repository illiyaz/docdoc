"""Tests for the FastAPI skeleton.

Covers:
- GET /health — 200, correct shape
- Stub routes (jobs, review) — all return 501 with correct body
- PIIFilterMiddleware — blocks SSN in JSON response body (500)
- PIIFilterMiddleware — passes clean JSON through unchanged
- PIIFilterMiddleware — passes non-JSON responses through unchanged
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Fresh TestClient against the real app with SQLite env override."""
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")

    from app.core.settings import get_settings

    get_settings.cache_clear()

    from app.api.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c

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
        # Return a raw SSN — middleware must block this
        return {"value": "123-45-6789"}

    @test_app.get("/text")
    def text_response():
        # Non-JSON content type — middleware must pass through unchanged
        return PlainTextResponse("raw text 123-45-6789")

    with TestClient(test_app, raise_server_exceptions=False) as c:
        yield c


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Jobs stub routes
# ---------------------------------------------------------------------------


class TestJobsStubs:
    def test_create_job_returns_501(self, client: TestClient) -> None:
        resp = client.post("/jobs")
        assert resp.status_code == 501

    def test_create_job_body(self, client: TestClient) -> None:
        resp = client.post("/jobs")
        assert resp.json() == {"detail": "not yet implemented"}

    def test_get_job_returns_501(self, client: TestClient) -> None:
        resp = client.get("/jobs/some-uuid")
        assert resp.status_code == 501

    def test_get_job_body(self, client: TestClient) -> None:
        resp = client.get("/jobs/some-uuid")
        assert resp.json() == {"detail": "not yet implemented"}

    def test_get_job_results_returns_501(self, client: TestClient) -> None:
        resp = client.get("/jobs/some-uuid/results")
        assert resp.status_code == 501

    def test_get_job_results_body(self, client: TestClient) -> None:
        resp = client.get("/jobs/some-uuid/results")
        assert resp.json() == {"detail": "not yet implemented"}


# ---------------------------------------------------------------------------
# Review stub routes
# ---------------------------------------------------------------------------


class TestReviewStubs:
    def test_review_queue_returns_501(self, client: TestClient) -> None:
        resp = client.get("/review/queue")
        assert resp.status_code == 501

    def test_review_queue_body(self, client: TestClient) -> None:
        resp = client.get("/review/queue")
        assert resp.json() == {"detail": "not yet implemented"}

    def test_approve_record_returns_501(self, client: TestClient) -> None:
        resp = client.post("/review/some-id/approve")
        assert resp.status_code == 501

    def test_approve_record_body(self, client: TestClient) -> None:
        resp = client.post("/review/some-id/approve")
        assert resp.json() == {"detail": "not yet implemented"}

    def test_reject_record_returns_501(self, client: TestClient) -> None:
        resp = client.post("/review/some-id/reject")
        assert resp.status_code == 501

    def test_reject_record_body(self, client: TestClient) -> None:
        resp = client.post("/review/some-id/reject")
        assert resp.json() == {"detail": "not yet implemented"}


# ---------------------------------------------------------------------------
# PIIFilterMiddleware
# ---------------------------------------------------------------------------


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
        # Plain text body must be returned verbatim
        assert "123-45-6789" in resp.text

    def test_blocked_response_does_not_contain_pii(
        self, pii_client: TestClient
    ) -> None:
        resp = pii_client.get("/ssn")
        # The 500 response body must NOT contain the raw SSN
        assert "123-45-6789" not in resp.text
