"""``/v1/auth/*`` endpoints: how a browser gets an admin console session.

Two ways in, both ending in the same signed cookie (``sites.console_session``):

* OIDC Authorization Code + PKCE against whatever provider the deployment runs;
* the local break-glass login, which trades the service token for a session.

🔴 ``SITES_LOCAL_LOGIN_ENABLED`` disables the second **authentication path**, not the form in
front of it. Gitea splits "hide the form" from "disable BASIC" precisely because operators
who only did the first kept an unauthenticated door open. The negative test for this lives in
test_console_auth.py and calls the endpoint directly, with no console involved.
"""
from __future__ import annotations

import json
from http.cookies import CookieError, SimpleCookie
from typing import Any

from sites import console_session
from sites import identity
from sites import oidc
from sites import telemetry
from sites.http_kit import CONSOLE_PREFIX
from sites.storage import StorageError
from sites.validation import DEFAULT_MERCHANT_ID, ValidationError


class AuthMixin:
    """Console login endpoints. Everything here is reachable without a credential."""

    def _login_methods(self) -> None:
        """What this deployment accepts. The login page cannot be rendered without it.

        Publishing the enabled methods discloses nothing an unauthenticated visitor could not
        learn by trying them; hiding it only produces a login page that guesses.
        """
        self._json(
            200,
            {
                "oidc": self.oidc_config is not None,
                "localLogin": bool(self.local_login_enabled),
            },
        )

    def _begin_oidc_login(self) -> None:
        if self.oidc_config is None:
            self._json(404, {"error": "no identity provider is configured"})
            return
        try:
            url, flow = oidc.begin(self.oidc_config, self.session_key)
        except (oidc.OidcError, oidc.ConfigError) as exc:
            telemetry.log("console_login_unavailable", level="warn", message=str(exc))
            self._json(503, {"error": "the identity provider is unavailable"})
            return
        self.send_response(302)
        self.send_header("Location", url)
        self._common_security_headers()
        self.send_header(
            "Set-Cookie", oidc.flow_cookie(flow, secure=console_session.COOKIE_SECURE)
        )
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _complete_oidc_login(self, query: dict[str, list[str]]) -> None:
        if self.oidc_config is None:
            self._json(404, {"error": "no identity provider is configured"})
            return
        config = self.oidc_config
        flow_value = self._cookie(oidc.FLOW_COOKIE)
        try:
            claims = oidc.complete(config, query, flow_value, self.session_key)
            login = oidc.login_identity(config, claims)
        except oidc.OidcError as exc:
            self._audit_login("oidc", "", "deny", reason=str(exc))
            self._json(403, {"error": str(exc)})
            return
        resolved = self._resolve_login_tenant(login)
        if resolved is None:
            return
        merchant_id, user_id = resolved
        session, csrf = console_session.issue(
            {
                "sub": login.subject,
                "email": login.email,
                "mid": merchant_id,
                "uid": user_id,
                "adm": login.admin,
            },
            self.session_key,
        )
        self._audit_login("oidc", login.subject, "allow", admin=login.admin)
        self._start_session(session, csrf)

    def _resolve_login_tenant(
        self, login: "oidc.LoginIdentity"
    ) -> tuple[str, str] | None:
        """Land a login on an existing merchant and tenant. None means a refusal was written.

        Merchants are never created here (contract §4); tenants may be, and only when signups
        are open and the address is inside the configured domains.
        """
        if login.admin:
            return DEFAULT_MERCHANT_ID, identity.DEFAULT_USER_ID
        merchant = identity.active_merchant(self.store, login.merchant_id)
        if isinstance(merchant, identity.Refusal):
            # The mapping names a merchant that is gone or disabled. Say so in the log: the
            # user only sees "refused", and without this line the cause is invisible.
            telemetry.log(
                "console_login_merchant_unavailable",
                level="warn",
                merchant_id=login.merchant_id,
                subject=login.subject,
            )
            self._audit_login("oidc", login.subject, "deny", reason="merchant unavailable")
            self._json(merchant.status, merchant.payload)
            return None
        try:
            record = self.store.tenant(login.merchant_id, login.user_id)
        except StorageError:
            self._json(503, {"error": "database unavailable"})
            return None
        if record is not None and record.get("disabled_at") is not None:
            self._audit_login("oidc", login.subject, "deny", reason="tenant disabled")
            self._json(403, {"error": "tenant is disabled"})
            return None
        if record is None:
            if not oidc.signup_allowed(self.oidc_config, login.email):
                telemetry.log(
                    "console_login_signup_refused",
                    level="warn",
                    merchant_id=login.merchant_id,
                    subject=login.subject,
                )
                self._audit_login("oidc", login.subject, "deny", reason="signups closed")
                self._json(
                    403,
                    {"error": "this account has no tenant and signups are closed"},
                )
                return None
            registered = identity.register_tenant(
                self.store, merchant, login.user_id
            )
            if isinstance(registered, identity.Refusal):
                self._json(registered.status, registered.payload)
                return None
        return login.merchant_id, login.user_id

    def _local_login(self) -> None:
        """Break-glass: exchange the service token for an admin session.

        Every success is audited with the source address (contract §1 decision B). n8n's owner
        bypass existed only in code, so operators did not know the escape hatch was there and
        attackers who read the source did.
        """
        if not self.local_login_enabled:
            self._audit_login("local", "", "deny", reason="local login disabled")
            self._json(403, {"error": "local login is disabled"})
            return
        try:
            payload = self._read_body()
            token = str(payload.get("token") or "")
        except ValidationError as exc:
            self._json(400, {"error": str(exc)})
            return
        if not identity.admin_token_matches(token, self.service_token):
            self._audit_login("local", "", "deny", reason="token mismatch")
            self._json(401, {"error": "invalid service token"})
            return
        session, csrf = console_session.issue(
            {
                "sub": "local-admin",
                "mid": DEFAULT_MERCHANT_ID,
                "uid": identity.DEFAULT_USER_ID,
                "adm": True,
            },
            self.session_key,
        )
        self._audit_login("local", "local-admin", "allow", admin=True)
        self._start_session(session, csrf, status=200, body={"admin": True})

    def _logout(self) -> None:
        if console_session.has_session(self.headers) and not console_session.verify(
            self.headers, self.session_key, unsafe=True
        ):
            self._json(403, {"error": "invalid console session or CSRF token"})
            return
        self.send_response(204)
        self._common_security_headers()
        for value in console_session.clear_cookie_values():
            self.send_header("Set-Cookie", value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # --- shared shell --------------------------------------------------

    def _start_session(
        self,
        session: str,
        csrf: str,
        *,
        status: int = 303,
        body: dict[str, Any] | None = None,
    ) -> None:
        encoded = b""
        if body is not None:
            encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self._common_security_headers()
        if status == 303:
            self.send_header("Location", CONSOLE_PREFIX)
        if body is not None:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        for value in console_session.set_cookie_values(session, csrf):
            self.send_header("Set-Cookie", value)
        # The flow cookie has done its job; leaving it would let a stale verifier be
        # replayed against a second callback.
        self.send_header(
            "Set-Cookie", oidc.clear_flow_cookie(secure=console_session.COOKIE_SECURE)
        )
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        if encoded:
            try:
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _cookie(self, name: str) -> str:
        jar = SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", "") or "")
        except (CookieError, ValueError):
            return ""
        return jar[name].value if name in jar else ""

    def _audit_login(
        self,
        method: str,
        subject: str,
        outcome: str,
        *,
        admin: bool = False,
        reason: str = "",
    ) -> None:
        telemetry.log(
            "console_login",
            level="info" if outcome == "allow" else "warn",
            method=method,
            subject=subject,
            admin=admin,
            outcome=outcome,
            reason=reason,
            peer=self.address_string(),
        )
