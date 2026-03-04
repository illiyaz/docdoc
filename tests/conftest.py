import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _ensure_masking_enabled_for_tests(monkeypatch: pytest.MonkeyPatch):
    """Tests always run with masking ON (safe default).

    The .env file may have PII_MASKING_ENABLED=false for local dev,
    but tests that verify masking behavior need it enabled.
    """
    monkeypatch.setenv("PII_MASKING_ENABLED", "true")
    from app.core.settings import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")

    from app.core.settings import get_settings

    get_settings.cache_clear()

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client

    get_settings.cache_clear()
    os.environ.pop("DATABASE_URL", None)
