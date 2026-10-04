"""Shared fixtures. Sets dummy env before app imports; Azure is never called."""

from __future__ import annotations

import os

import bcrypt
import pytest

TEST_USERNAME = "testuser"
TEST_PASSWORD = "testpass"
# rounds=4 keeps unit tests fast; production hashes use gensalt() default.
TEST_PASSWORD_HASH = bcrypt.hashpw(
    TEST_PASSWORD.encode("utf-8"),
    bcrypt.gensalt(rounds=4),
).decode("utf-8")

# Set before any app import so get_settings() / main.settings pick these up.
os.environ["APP_USERS"] = f"{TEST_USERNAME}:{TEST_PASSWORD_HASH}"
os.environ["SESSION_SECRET"] = "test-session-secret-not-for-production"
os.environ["AZURE_CLASSIFY_URL"] = "https://example.test/api/classify-po"
os.environ["AZURE_FUNCTION_KEY"] = ""
os.environ["COOKIE_SAMESITE"] = "lax"
os.environ["COOKIE_SECURE"] = "false"
os.environ["CORS_ORIGINS"] = "http://localhost:5173"
os.environ["BATCH_SIZE"] = "5"
os.environ["MAX_EXCEL_PO_ROWS"] = "50"


@pytest.fixture
def settings(tmp_path, monkeypatch):
    """Fresh Settings pointing at an isolated JOBS_DIR."""
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    monkeypatch.setenv("JOBS_DIR", str(jobs_dir))
    monkeypatch.setenv("APP_USERS", f"{TEST_USERNAME}:{TEST_PASSWORD_HASH}")
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret-not-for-production")
    monkeypatch.setenv("AZURE_CLASSIFY_URL", "https://example.test/api/classify-po")

    from app.config import get_settings

    get_settings.cache_clear()
    s = get_settings()

    import app.main as main

    main.settings = s
    yield s
    get_settings.cache_clear()


@pytest.fixture
def client(settings):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth_client(client):
    """TestClient already logged in as TEST_USERNAME."""
    res = client.post(
        "/api/login",
        json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
    )
    assert res.status_code == 200, res.text
    return client
