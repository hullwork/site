"""Signed browser sessions for the Sites admin console.

One credential shape for both console login paths (OIDC callback and the break-glass local
login): an HMAC-signed cookie holding the identity, plus a readable CSRF cookie whose value
must be echoed in a header on every unsafe request.

The signing key is its own secret (``SITES_CONSOLE_SESSION_KEY_FILE``) and is deliberately
**not** the service token: the service token is a bearer credential that gets handed to
other parties, and NIST SP 800-57 §5.2 wants one key to serve one purpose. An earlier design
here signed its assertions with exactly the token it also handed out, so anyone holding that
token could mint an assertion this side would accept.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from http.cookies import CookieError, SimpleCookie
from typing import Any

from os import getenv


COOKIE = "sites_console_session"
CSRF_COOKIE = "sites_console_csrf"
CSRF_HEADER = "X-Sites-Console-CSRF"
SESSION_TTL_SECONDS = 8 * 60 * 60
AUDIENCE = "sites-console"
COOKIE_SECURE = (
    getenv("SITES_CONSOLE_SECURE_COOKIES", "false").strip().lower() == "true"
)
# Bound on what will even be parsed: a session cookie is a couple of hundred bytes, and a
# megabyte-long "cookie" should cost nothing to reject.
_MAX_COOKIE_BYTES = 4096


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sign(encoded: str, key: str) -> str:
    return _b64(
        hmac.new(key.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
    )


def issue(
    claims: dict[str, Any], key: str, *, now: int | None = None
) -> tuple[str, str]:
    """Mint a session for an already-authenticated identity. Returns (cookie value, csrf)."""
    if not key:
        raise ValueError("console sessions require a signing key")
    current = int(time.time() if now is None else now)
    csrf = secrets.token_urlsafe(24)
    payload = {
        "v": 1,
        "aud": AUDIENCE,
        "sub": str(claims.get("sub") or ""),
        "email": str(claims.get("email") or ""),
        "mid": str(claims.get("mid") or ""),
        "uid": str(claims.get("uid") or ""),
        "adm": bool(claims.get("adm")),
        "iat": current,
        "exp": current + SESSION_TTL_SECONDS,
        "csrf": csrf,
    }
    encoded = _b64(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    return f"{encoded}.{_sign(encoded, key)}", csrf


def verify(
    headers: Any, key: str, *, unsafe: bool = False, now: int | None = None
) -> dict[str, Any] | None:
    """The claims of a valid session, or None. Never raises on caller-controlled input.

    ``unsafe`` is the request method's own judgement: state-changing requests must also carry
    the CSRF value, because the cookie alone travels on cross-site requests.
    """
    if not key:
        return None
    token = _cookie_value(headers, COOKIE)
    if not token:
        return None
    try:
        encoded, supplied = token.split(".", 1)
        expected = _sign(encoded, key)
        if not hmac.compare_digest(supplied, expected):
            return None
        payload = json.loads(_unb64(encoded))
    except (ValueError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    current = int(time.time() if now is None else now)
    issued_at = _timestamp(payload, "iat")
    expires_at = _timestamp(payload, "exp")
    valid = (
        payload.get("v") == 1
        and payload.get("aud") == AUDIENCE
        and bool(payload.get("sub"))
        and issued_at is not None
        and expires_at is not None
        and issued_at <= current + 5
        and current <= expires_at
        and expires_at <= issued_at + SESSION_TTL_SECONDS
    )
    if not valid:
        return None
    if unsafe and not hmac.compare_digest(
        str(payload.get("csrf") or ""), headers.get(CSRF_HEADER, "") or ""
    ):
        return None
    return payload


def has_session(headers: Any) -> bool:
    """Whether a browser supplied the session-cookie name, valid or not."""
    return _cookie_value(headers, COOKIE) is not None


def set_cookie_values(session: str, csrf: str) -> list[str]:
    """Set-Cookie values for a fresh session.

    The session cookie is HttpOnly (script must not be able to read the credential); the CSRF
    cookie is deliberately readable, because the console has to copy it into a header.
    """
    secure = "; Secure" if COOKIE_SECURE else ""
    return [
        f"{COOKIE}={session}; Path=/; Max-Age={SESSION_TTL_SECONDS}; "
        f"HttpOnly; SameSite=Lax{secure}",
        f"{CSRF_COOKIE}={csrf}; Path=/; Max-Age={SESSION_TTL_SECONDS}; "
        f"SameSite=Lax{secure}",
    ]


def clear_cookie_values() -> list[str]:
    secure = "; Secure" if COOKIE_SECURE else ""
    return [
        f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax{secure}",
        f"{CSRF_COOKIE}=; Path=/; Max-Age=0; SameSite=Lax{secure}",
    ]


def _cookie_value(headers: Any, name: str) -> str | None:
    raw = headers.get("Cookie", "") or ""
    if not raw or len(raw) > _MAX_COOKIE_BYTES:
        return None
    cookies = SimpleCookie()
    try:
        cookies.load(raw)
    except (CookieError, ValueError):
        return None
    if name not in cookies:
        return None
    return cookies[name].value


def _timestamp(payload: dict[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value
