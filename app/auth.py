"""Session-cookie auth: verify bcrypt hashes from env, sign cookies."""

from __future__ import annotations

import logging
from typing import Annotated

import bcrypt
from fastapi import Depends, HTTPException, Request, Response, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, Field

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

COOKIE_NAME_DEFAULT = "po_ui_session"
# Fixed hash so unknown-username checks still do bcrypt work (timing).
_DUMMY_HASH = bcrypt.hashpw(b"not-a-real-password", bcrypt.gensalt(rounds=12))


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


def parse_app_users(raw: str) -> dict[str, str]:
    """Parse APP_USERS=user1:hash1,user2:hash2 into {username: bcrypt_hash}."""
    users: dict[str, str] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            logger.warning("Skipping malformed APP_USERS entry (missing ':')")
            continue
        username, hash_value = part.split(":", 1)
        username = username.strip()
        hash_value = hash_value.strip()
        if username and hash_value:
            users[username] = hash_value
    return users


def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(
        settings.session_secret,
        salt="po-classifier-ui-session",
    )


def create_session_token(username: str, settings: Settings) -> str:
    return _serializer(settings).dumps({"u": username})


def read_session_username(token: str, settings: Settings) -> str | None:
    max_age = int(settings.session_max_age_hours * 3600)
    try:
        data = _serializer(settings).loads(token, max_age=max_age)
    except SignatureExpired:
        return None
    except BadSignature:
        return None
    username = data.get("u") if isinstance(data, dict) else None
    if not isinstance(username, str) or not username:
        return None
    users = parse_app_users(settings.app_users)
    if username not in users:
        return None
    return username


def verify_password(plain: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(
            plain.encode("utf-8"),
            password_hash.encode("utf-8"),
        )
    except (ValueError, TypeError):
        return False


def authenticate_user(username: str, password: str, settings: Settings) -> str | None:
    users = parse_app_users(settings.app_users)
    if not users:
        return None
    stored = users.get(username)
    if not stored:
        bcrypt.checkpw(password.encode("utf-8"), _DUMMY_HASH)
        return None
    if not verify_password(password, stored):
        return None
    return username


def cookie_kwargs(settings: Settings) -> dict:
    same_site = (settings.cookie_samesite or "lax").strip().lower()
    if same_site not in {"lax", "strict", "none"}:
        same_site = "lax"
    secure = settings.cookie_secure
    if same_site == "none":
        # Browsers require Secure when SameSite=None.
        secure = True
    return {
        "key": settings.session_cookie_name or COOKIE_NAME_DEFAULT,
        "httponly": True,
        "secure": secure,
        "samesite": same_site,
        "max_age": int(settings.session_max_age_hours * 3600),
        "path": "/",
    }


def set_session_cookie(response: Response, username: str, settings: Settings) -> None:
    token = create_session_token(username, settings)
    kwargs = cookie_kwargs(settings)
    response.set_cookie(value=token, **kwargs)


def clear_session_cookie(response: Response, settings: Settings) -> None:
    kwargs = cookie_kwargs(settings)
    # delete_cookie must match path/samesite/secure for browsers to clear it.
    response.delete_cookie(
        key=kwargs["key"],
        path=kwargs["path"],
        secure=kwargs["secure"],
        httponly=kwargs["httponly"],
        samesite=kwargs["samesite"],
    )


def require_auth_configured(settings: Settings) -> None:
    if not (settings.session_secret or "").strip():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth is not configured (SESSION_SECRET missing).",
        )
    if not parse_app_users(settings.app_users):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth is not configured (APP_USERS missing). Add username:bcrypt_hash.",
        )


def get_current_username(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> str:
    require_auth_configured(settings)
    token = request.cookies.get(settings.session_cookie_name or COOKIE_NAME_DEFAULT)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    username = read_session_username(token, settings)
    if not username:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return username


CurrentUser = Annotated[str, Depends(get_current_username)]
