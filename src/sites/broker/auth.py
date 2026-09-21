"""End-user auth: email OTP, sessions, and the HTTP routes for contract section 3.2.

Every branch a caller can distinguish from the outside answers the same way
regardless of *why*: whether the email exists, registration is closed, or the
caller is rate-limited never changes the response shape or status code for
``email/start``. That is the point of the whole module, not an incidental
property, so read every early return in :func:`AuthService.start_email` as "do the
same amount of externally-visible work as every other branch".
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from os import getenv
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from sites.broker import db as broker_db
from sites.broker import tokens as broker_tokens
from sites.broker.config_store import AppRuntime, ConfigStore
from sites.broker.http import HttpError, Request, Response, Router, Services
from sites.broker.identity import EndUser
from sites.broker.mailer import Mailer

SESSION_COOKIE = "__Host-hull_session"
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AuthError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def otp_ttl_seconds() -> int:
    return int(getenv("HULL_OTP_TTL_SECONDS", "600") or "600")


def otp_max_attempts() -> int:
    # Not given a default in the contract; conservative and in line with the
    # agent login limiter's order of magnitude for a single credential's guesses.
    return int(getenv("HULL_OTP_MAX_ATTEMPTS", "5") or "5")


def _rate_limit(name: str) -> tuple[int, int]:
    # Agent server/auth.py:45-46 sets both limiter constants this mirrors:
    # _LOGIN_FAILURES_LIMIT = 10, _LOGIN_WINDOW_SECONDS = 300.
    limit = int(getenv(f"HULL_OTP_{name}_LIMIT", "10") or "10")
    window = int(getenv(f"HULL_OTP_{name}_WINDOW_SECONDS", "300") or "300")
    return limit, window


def session_ttl_seconds() -> int:
    return int(getenv("HULL_SESSION_TTL_SECONDS", "28800") or "28800")


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _valid_email(email: str) -> bool:
    return bool(email) and len(email) <= 254 and bool(_EMAIL_RE.match(email))


def _otp_key_from_secret(secret_key: bytes) -> bytes:
    """A process-random pepper would make one replica unable to verify a code
    another replica minted, which every OTP send/verify pair risks hitting behind
    a load balancer. HKDF-deriving a dedicated subkey from the same secret-box
    key keeps this to one provisioned secret while still giving OTP hashing a
    cryptographically independent key from the one that encrypts app secrets
    (NIST SP 800-57 §5.2's one-key-one-purpose, satisfied by derivation rather
    than by a second file the contract never asks operators to provision).
    """
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                info=b"hull-broker-otp-v1").derive(secret_key)


def _code_hmac(otp_key: bytes, app_env_id: str, email: str, code: str) -> str:
    mac = hmac.new(otp_key, f"{app_env_id}\0{email}\0{code}".encode("utf-8"), hashlib.sha256)
    return mac.hexdigest()


def _window_start(now: datetime, seconds: int) -> datetime:
    epoch = int(now.timestamp())
    floored = epoch - (epoch % seconds)
    return datetime.fromtimestamp(floored, tz=timezone.utc)


@dataclass(frozen=True)
class VerifyResult:
    user: dict
    token: str
    expires_at: int
    cookie_value: str


@dataclass(frozen=True)
class TokenResult:
    user: dict
    token: str
    expires_at: int


class AuthService:
    def __init__(self, db: broker_db.BrokerDatabase, config: ConfigStore,
                 keys: broker_tokens.KeyRing, mailer: Mailer, secret_key: bytes, *,
                 clock: Any = lambda: datetime.now(timezone.utc)):
        self._db = db
        self._config = config
        self._keys = keys
        self._mailer = mailer
        self._clock = clock
        self._otp_key = _otp_key_from_secret(secret_key)

    def _rate_limit_ok(self, cursor: Any, app_env_id: str, scope: str, key: str,
                        limit: int, window_seconds: int, now: datetime) -> bool:
        window_start = _window_start(now, window_seconds)
        return broker_db.rate_limit_increment(cursor, app_env_id, scope, key,
                                               window_start, limit)

    def _registration_mode(self, runtime: AppRuntime) -> str:
        return (runtime.config.get("auth") or {}).get("registration", "open")

    def _may_sign_in(self, runtime: AppRuntime, email: str, user_exists: bool) -> bool:
        if user_exists:
            return True
        mode = self._registration_mode(runtime)
        if mode == "open":
            return True
        if mode == "domain_allowlist":
            domain = email.rsplit("@", 1)[-1]
            allowed = {d.lower() for d in runtime.settings.get("allowed_email_domains") or []}
            return domain in allowed
        # "invite" has no invite-issuance mechanism in this slice, and "closed"
        # means exactly what it says: neither admits a user who has never signed
        # in before.
        return False

    def start_email(self, app_env_id: str, email: str, client_ip: str) -> None:
        """Never raises for caller-controlled reasons: the route always answers
        ``202 {"status":"sent"}``. See the module docstring for why.
        """
        now = self._clock()
        runtime = self._config.active(app_env_id)
        email = _normalize_email(email)
        if runtime is None or not _valid_email(email):
            return
        send_limit, send_window = _rate_limit("SEND")
        with self._db.transaction() as cursor:
            if not self._rate_limit_ok(cursor, app_env_id, "otp_send_email", email,
                                        send_limit, send_window, now):
                return
            if not self._rate_limit_ok(cursor, app_env_id, "otp_send_ip", client_ip,
                                        send_limit, send_window, now):
                return
            user = broker_db.find_user_by_email(cursor, app_env_id, email)
            if not self._may_sign_in(runtime, email, user is not None):
                return
            code = f"{secrets.randbelow(1_000_000):06d}"
            expires_at = now + timedelta(seconds=otp_ttl_seconds())
            code_hmac = _code_hmac(self._otp_key, app_env_id, email, code)
            broker_db.upsert_otp(cursor, app_env_id, email, code_hmac, expires_at)
        app_name = runtime.config.get("app", "this app")
        self._mailer.send(
            app_env_id, email, f"Your {app_name} sign-in code",
            f"Your sign-in code is {code}. It expires in {otp_ttl_seconds() // 60} minutes.",
        )

    def verify_email(self, app_env_id: str, email: str, code: str,
                      client_ip: str) -> VerifyResult:
        now = self._clock()
        runtime = self._config.active(app_env_id)
        email = _normalize_email(email)
        invalid = AuthError("invalid_code", "the code is incorrect or has expired")
        if runtime is None or not _valid_email(email) or not code:
            raise invalid

        # Every branch below has to *commit* its side effect (a rate-limit
        # bucket increment, an attempts increment) even when the branch's own
        # answer is "refused" -- raising from inside `with db.transaction()`
        # rolls the whole block back, silently discarding exactly the write a
        # refused attempt most needs to keep (that used to be a real bug here:
        # a wrong code never actually counted against otp_max_attempts because
        # the raise that reported it also undid it). So this only ever exits
        # the block normally, and decides whether to raise afterward.
        verify_limit, verify_window = _rate_limit("VERIFY")
        succeeded = False
        with self._db.transaction() as cursor:
            rate_limited = not self._rate_limit_ok(
                cursor, app_env_id, "otp_verify_email", email, verify_limit,
                verify_window, now,
            )
            rate_limited = rate_limited or not self._rate_limit_ok(
                cursor, app_env_id, "otp_verify_ip", client_ip, verify_limit,
                verify_window, now,
            )
            if not rate_limited:
                record = broker_db.get_otp(cursor, app_env_id, email)
                supplied = _code_hmac(self._otp_key, app_env_id, email, code)
                succeeded = (
                    record is not None
                    and record["consumed_at"] is None
                    and record["expires_at"] > now
                    and record["attempts"] < otp_max_attempts()
                    and hmac.compare_digest(record["code_hmac"], supplied)
                )
                if not succeeded and record is not None:
                    broker_db.increment_otp_attempts(cursor, app_env_id, email)
                if succeeded:
                    broker_db.consume_otp(cursor, app_env_id, email)
                    user = broker_db.find_user_by_email(cursor, app_env_id, email)
                    if user is None:
                        user_id = "u" + secrets.token_hex(16)
                        role = runtime.config.get("default_role", "")
                        plan = runtime.config.get("default_plan", "")
                        broker_db.create_user(cursor, app_env_id, user_id, email, None,
                                               role, plan)
                        user = {"id": user_id, "email": email, "org": None, "role": role,
                                "plan": plan}
                    session_token = secrets.token_urlsafe(32)
                    token_sha256 = hashlib.sha256(session_token.encode("ascii")).hexdigest()
                    session_expires = now + timedelta(seconds=session_ttl_seconds())
                    broker_db.create_session(cursor, app_env_id, token_sha256, user["id"],
                                              session_expires)
        if not succeeded:
            raise invalid
        user_token, expires_at = broker_tokens.issue_user_token(
            self._keys, app_env_id=app_env_id, user_id=user["id"], session_id=token_sha256,
            org=user["org"], role=user["role"], plan=user["plan"], env=_env_of(runtime),
        )
        return VerifyResult(user=user, token=user_token, expires_at=expires_at,
                             cookie_value=session_token)

    def token_from_session(self, app_env_id: str, session_token: str | None) -> TokenResult:
        no_session = AuthError("no_session", "there is no valid session for this request")
        if not session_token:
            raise no_session
        runtime = self._config.active(app_env_id)
        if runtime is None:
            raise no_session
        token_sha256 = hashlib.sha256(session_token.encode("ascii")).hexdigest()
        now = self._clock()
        with self._db.transaction() as cursor:
            record = broker_db.get_session(cursor, app_env_id, token_sha256)
            if (record is None or record["revoked_at"] is not None
                    or record["expires_at"] <= now):
                raise no_session
            user = broker_db.get_user(cursor, app_env_id, record["user_id"])
        if user is None:
            raise no_session
        user_token, expires_at = broker_tokens.issue_user_token(
            self._keys, app_env_id=app_env_id, user_id=user["id"], session_id=token_sha256,
            org=user["org"], role=user["role"], plan=user["plan"], env=_env_of(runtime),
        )
        return TokenResult(user=user, token=user_token, expires_at=expires_at)

    def logout(self, app_env_id: str, session_token: str | None) -> None:
        if not session_token:
            return
        token_sha256 = hashlib.sha256(session_token.encode("ascii")).hexdigest()
        with self._db.transaction() as cursor:
            broker_db.revoke_session(cursor, app_env_id, token_sha256)


def _env_of(runtime: AppRuntime) -> str:
    # Neither the compiled config (section 3.6) nor the settings example
    # (section 3.7) names which value goes here, and the appEnvId itself is an
    # opaque HMAC digest the Broker cannot invert. Settings gains an optional
    # "env" key for this slice, defaulting to "prod" -- see docs/BROKER.md and
    # the report to the lead for the open question this leaves for partition A/E.
    value = runtime.settings.get("env")
    return value if value in ("dev", "prod") else "prod"


def _user_body(user: dict) -> dict:
    return {"id": user["id"], "email": user["email"], "org": user["org"],
            "role": user["role"], "plan": user["plan"]}


def _cookie_header(value: str, max_age: int) -> str:
    return (
        f"{SESSION_COOKIE}={value}; HttpOnly; Secure; SameSite=Lax; Path=/; "
        f"Max-Age={max_age}"
    )


def _clear_cookie_header() -> str:
    return f"{SESSION_COOKIE}=; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=0"


def register(router: Router, services: Services) -> None:
    auth = services.auth  # type: ignore[attr-defined]

    def start(request: Request) -> Response:
        body = request.body if isinstance(request.body, dict) else {}
        auth.start_email(request.app.app_env_id, str(body.get("email") or ""),
                          request.client_ip)
        return Response(202, {"status": "sent"})

    def verify(request: Request) -> Response:
        body = request.body if isinstance(request.body, dict) else {}
        try:
            result = auth.verify_email(
                request.app.app_env_id, str(body.get("email") or ""),
                str(body.get("code") or ""), request.client_ip,
            )
        except AuthError as exc:
            raise HttpError(401, exc.code, exc.message) from exc
        return Response(
            200,
            {"user": _user_body(result.user), "token": result.token,
             "expiresAt": result.expires_at},
            {"Set-Cookie": _cookie_header(result.cookie_value, session_ttl_seconds())},
        )

    def token(request: Request) -> Response:
        try:
            result = auth.token_from_session(
                request.app.app_env_id, request.cookies.get(SESSION_COOKIE)
            )
        except AuthError as exc:
            raise HttpError(401, exc.code, exc.message) from exc
        return Response(
            200,
            {"user": _user_body(result.user), "token": result.token,
             "expiresAt": result.expires_at},
        )

    def logout(request: Request) -> Response:
        auth.logout(request.app.app_env_id, request.cookies.get(SESSION_COOKIE))
        return Response(204, headers={"Set-Cookie": _clear_cookie_header()})

    def jwks(request: Request) -> Response:
        return Response(200, services.keys.jwks())

    router.add("POST", "/_hull/auth/email/start", start, auth="app")
    router.add("POST", "/_hull/auth/email/verify", verify, auth="app")
    router.add("POST", "/_hull/auth/token", token, auth="app")
    router.add("POST", "/_hull/auth/logout", logout, auth="app")
    router.add("GET", "/_hull/.well-known/jwks.json", jwks, auth="none")
