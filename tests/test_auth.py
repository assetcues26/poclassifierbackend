"""Unit tests for session auth helpers (no HTTP)."""

from __future__ import annotations

from app.auth import (
    authenticate_user,
    cookie_kwargs,
    create_session_token,
    parse_app_users,
    read_session_username,
    verify_password,
)
from tests.conftest import TEST_PASSWORD, TEST_PASSWORD_HASH, TEST_USERNAME


def test_parse_app_users():
    users = parse_app_users(f"alice:$2b$12$abc,bob:$2b$12$def")
    assert users == {"alice": "$2b$12$abc", "bob": "$2b$12$def"}


def test_parse_app_users_skips_malformed():
    users = parse_app_users("nocolon,ok:hash")
    assert users == {"ok": "hash"}


def test_verify_password_roundtrip():
    assert verify_password(TEST_PASSWORD, TEST_PASSWORD_HASH) is True
    assert verify_password("wrong", TEST_PASSWORD_HASH) is False


def test_authenticate_user(settings):
    assert authenticate_user(TEST_USERNAME, TEST_PASSWORD, settings) == TEST_USERNAME
    assert authenticate_user(TEST_USERNAME, "wrong", settings) is None
    assert authenticate_user("nobody", TEST_PASSWORD, settings) is None


def test_session_token_roundtrip(settings):
    token = create_session_token(TEST_USERNAME, settings)
    assert read_session_username(token, settings) == TEST_USERNAME
    assert read_session_username("not-a-valid-token", settings) is None


def test_cookie_kwargs_local(settings):
    kwargs = cookie_kwargs(settings)
    assert kwargs["httponly"] is True
    assert kwargs["samesite"] == "lax"
    assert kwargs["secure"] is False


def test_cookie_kwargs_none_forces_secure(settings, monkeypatch):
    monkeypatch.setattr(settings, "cookie_samesite", "none")
    monkeypatch.setattr(settings, "cookie_secure", False)
    kwargs = cookie_kwargs(settings)
    assert kwargs["samesite"] == "none"
    assert kwargs["secure"] is True
