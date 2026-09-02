"""Contract tests for admin console login and for the session as a credential.

Every case here talks to the endpoint or the identity resolver directly. That is the whole
point: the console is a consequence of these answers, so a test that went through it could
only ever confirm that the two agree with each other.
"""
from __future__ import annotations

import json
import unittest
from unittest import mock

from sites import console_session
from sites import identity
from sites.api import (
    Handler,
    _load_console_session_key,
    _local_login_enabled,
    _require_admin_login_path,
)
from sites.validation import DEFAULT_MERCHANT_ID

from tests.test_sites import _FakeTenantStore, _merchant_row


SERVICE_TOKEN = "s" * 32
SESSION_KEY = "k" * 32


class _Headers(dict):
    def get(self, key, default=""):
        return super().get(key, default)


class _Wire:
    """Captures a raw (non-_json) response: status line, headers and body."""

    def __init__(self) -> None:
        self.status = 0
        self.headers: list[tuple[str, str]] = []
        self.body = b""

    def cookies(self) -> dict[str, str]:
        jar = {}
        for name, value in self.headers:
            if name == "Set-Cookie":
                key, _, rest = value.partition("=")
                jar[key] = rest.split(";", 1)[0]
        return jar


def _handler(
    headers: dict,
    *,
    local_login_enabled: bool = True,
    oidc_config=None,
    tenants: dict | None = None,
    merchants: list[dict] | None = None,
    body: dict | None = None,
) -> tuple[Handler, list[tuple[int, dict]], _Wire]:
    handler = object.__new__(Handler)
    handler.headers = _Headers(headers)
    handler.command = "POST"
    handler.path = "/v1/auth/local"
    handler.service_token = SERVICE_TOKEN
    handler.session_key = SESSION_KEY
    handler.local_login_enabled = local_login_enabled
    handler.oidc_config = oidc_config
    handler.store = _FakeTenantStore(tenants or {}, merchants=merchants)
    responses: list[tuple[int, dict]] = []
    wire = _Wire()
    handler._json = lambda status, payload: responses.append((status, payload))
    handler._read_body = lambda *a, **k: dict(body or {})
    handler.address_string = lambda: "203.0.113.9"

    def send_response(status, message=None):
        wire.status = status

    handler.send_response = send_response
    handler.send_header = lambda name, value: wire.headers.append((name, value))
    handler.end_headers = lambda: None

    class _File:
        def write(self, data):
            wire.body += data

    handler.wfile = _File()
    return handler, responses, wire


class StartupContractTests(unittest.TestCase):
    """Missing required secrets stop the process; they never downgrade it silently."""

    def test_no_session_key_refuses_to_start(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "SITES_CONSOLE_SESSION_KEY_FILE"):
            _load_console_session_key("")

    def test_an_unreadable_or_short_session_key_refuses_to_start(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "cannot be read"):
            _load_console_session_key("/nonexistent/console-session-key")
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "key"
            path.write_text("short")
            with self.assertRaisesRegex(RuntimeError, "at least 32 characters"):
                _load_console_session_key(str(path))
            path.write_text(f"  {SESSION_KEY}\n")
            self.assertEqual(_load_console_session_key(str(path)), SESSION_KEY)

    def test_local_login_defaults_follow_the_provider(self) -> None:
        with mock.patch.dict("os.environ", {"SITES_LOCAL_LOGIN_ENABLED": ""}, clear=False):
            # No identity provider: on, or nobody could ever reach the console.
            self.assertTrue(_local_login_enabled(False))
            # With one: off, so the platform token stops being a daily credential.
            self.assertFalse(_local_login_enabled(True))

    def test_an_explicit_value_wins_over_the_default(self) -> None:
        with mock.patch.dict(
            "os.environ", {"SITES_LOCAL_LOGIN_ENABLED": "true"}, clear=False
        ):
            self.assertTrue(_local_login_enabled(True))
        with mock.patch.dict(
            "os.environ", {"SITES_LOCAL_LOGIN_ENABLED": "false"}, clear=False
        ):
            self.assertFalse(_local_login_enabled(True))

    def test_disabling_both_login_paths_refuses_to_start(self) -> None:
        # Refusing beats booting a console nobody can enter and finding out during an
        # incident, which is the one moment the break-glass door is needed.
        with mock.patch.dict(
            "os.environ", {"SITES_LOCAL_LOGIN_ENABLED": "false"}, clear=False
        ):
            with self.assertRaisesRegex(RuntimeError, "nobody can reach"):
                _local_login_enabled(False)

    def test_a_malformed_value_refuses_to_start(self) -> None:
        with mock.patch.dict(
            "os.environ", {"SITES_LOCAL_LOGIN_ENABLED": "maybe"}, clear=False
        ):
            with self.assertRaises(RuntimeError):
                _local_login_enabled(False)

    def test_oidc_without_a_complete_admin_mapping_cannot_replace_local_admin(self) -> None:
        base = {
            "SITES_OIDC_ADMIN_CLAIM": "groups",
            "SITES_OIDC_ADMIN_VALUE": "platform-admin",
        }
        for missing in base:
            environment = {**base, missing: ""}
            with self.subTest(missing=missing), mock.patch.dict(
                "os.environ", environment, clear=False
            ):
                with self.assertRaisesRegex(RuntimeError, missing):
                    _require_admin_login_path(
                        oidc_configured=True, local_login=False
                    )

    def test_complete_oidc_admin_mapping_can_replace_local_admin(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {
                "SITES_OIDC_ADMIN_CLAIM": "groups",
                "SITES_OIDC_ADMIN_VALUE": "platform-admin",
            },
            clear=False,
        ):
            _require_admin_login_path(oidc_configured=True, local_login=False)

    def test_break_glass_admin_allows_merchant_only_oidc(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"SITES_OIDC_ADMIN_CLAIM": "", "SITES_OIDC_ADMIN_VALUE": ""},
            clear=False,
        ):
            _require_admin_login_path(oidc_configured=True, local_login=True)


class LocalLoginTests(unittest.TestCase):
    def test_the_login_methods_are_published_for_the_login_page(self) -> None:
        handler, responses, _ = _handler({}, local_login_enabled=True)
        handler._login_methods()
        self.assertEqual(responses[-1], (200, {"oidc": False, "localLogin": True}))

    def test_a_correct_token_starts_a_session(self) -> None:
        handler, responses, wire = _handler({}, body={"token": SERVICE_TOKEN})
        with mock.patch("sites.api_auth.telemetry.log") as log:
            handler._local_login()
        self.assertEqual(wire.status, 200)
        jar = wire.cookies()
        self.assertIn(console_session.COOKIE, jar)
        self.assertIn(console_session.CSRF_COOKIE, jar)
        self.assertEqual(responses, [])
        # Break-glass logins are audited with the source address (contract §1 decision B):
        # n8n's owner bypass lived only in the code, so operators never knew it was there
        # while anyone reading the source did.
        event, kwargs = log.call_args.args[0], log.call_args.kwargs
        self.assertEqual(event, "console_login")
        self.assertEqual(kwargs["outcome"], "allow")
        self.assertEqual(kwargs["method"], "local")
        self.assertEqual(kwargs["peer"], "203.0.113.9")
        self.assertTrue(kwargs["admin"])

    def test_the_session_cookie_is_httponly_and_the_csrf_cookie_is_not(self) -> None:
        handler, _, wire = _handler({}, body={"token": SERVICE_TOKEN})
        handler._local_login()
        values = {
            name.split("=", 1)[0]: name
            for header, name in wire.headers
            if header == "Set-Cookie"
        }
        self.assertIn("HttpOnly", values[console_session.COOKIE])
        # The console has to read this one to echo it back in a header.
        self.assertNotIn("HttpOnly", values[console_session.CSRF_COOKIE])

    def test_a_wrong_token_is_refused_and_audited(self) -> None:
        handler, responses, wire = _handler({}, body={"token": "wrong"})
        with mock.patch("sites.api_auth.telemetry.log") as log:
            handler._local_login()
        self.assertEqual(responses[-1][0], 401)
        self.assertEqual(wire.cookies(), {})
        self.assertEqual(log.call_args.kwargs["outcome"], "deny")

    def test_disabled_local_login_refuses_the_endpoint_itself(self) -> None:
        """🔴 Contract §5.2: bypass the console entirely and call the endpoint.

        Gitea keeps "hide the sign-in form" and "disable BASIC" as two settings precisely
        because operators who did only the first left the door open. The correct token is
        used here, so the refusal cannot be about the credential.
        """
        handler, responses, wire = _handler(
            {}, local_login_enabled=False, body={"token": SERVICE_TOKEN}
        )
        handler._local_login()
        self.assertEqual(responses[-1][0], 403)
        self.assertIn("disabled", responses[-1][1]["error"])
        self.assertEqual(wire.cookies(), {})

    def test_disabled_local_login_also_publishes_itself(self) -> None:
        handler, responses, _ = _handler({}, local_login_enabled=False)
        handler._login_methods()
        self.assertFalse(responses[-1][1]["localLogin"])

    def test_without_a_provider_the_oidc_endpoints_are_absent(self) -> None:
        handler, responses, _ = _handler({})
        handler._begin_oidc_login()
        self.assertEqual(responses[-1][0], 404)
        handler._complete_oidc_login({})
        self.assertEqual(responses[-1][0], 404)


class SessionAsCredentialTests(unittest.TestCase):
    """A session cookie is a credential, so it gets a credential's treatment."""

    def _session_headers(self, claims: dict, *, csrf: bool = True) -> dict:
        token, csrf_value = console_session.issue(claims, SESSION_KEY)
        headers = {"Cookie": f"{console_session.COOKIE}={token}"}
        if csrf:
            headers[console_session.CSRF_HEADER] = csrf_value
        return headers

    def test_an_admin_session_authenticates_without_any_token(self) -> None:
        handler, responses, _ = _handler(
            self._session_headers({"sub": "local-admin", "adm": True})
        )
        self.assertTrue(handler._is_admin())
        self.assertEqual(
            handler._authenticate(),
            (DEFAULT_MERCHANT_ID, identity.DEFAULT_USER_ID),
        )

    def test_a_forged_session_is_refused(self) -> None:
        token, _ = console_session.issue({"sub": "x", "adm": True}, "another-key" * 4)
        handler, responses, _ = _handler(
            {"Cookie": f"{console_session.COOKIE}={token}"}
        )
        self.assertFalse(handler._is_admin())
        self.assertIsNone(handler._authenticate())
        self.assertEqual(responses[-1][0], 401)

    def test_an_unsafe_request_without_the_csrf_header_is_not_admin(self) -> None:
        handler, _, _ = _handler(
            self._session_headers({"sub": "local-admin", "adm": True}, csrf=False)
        )
        handler.command = "DELETE"
        self.assertFalse(handler._is_admin())

    def test_a_tenant_session_is_re_read_from_the_database_every_request(self) -> None:
        """🔴 Contract §4: unbinding or disabling must take effect at once.

        The identity is signed into the cookie, but it is not *believed* from the cookie.
        Trusting the signed copy leaves a disabled tenant working for the rest of the
        cookie's lifetime, which is the failure that looks like nothing happened.
        """
        claims = {"sub": "alice", "mid": "acme", "uid": "oidc-alice", "adm": False}
        store_args = {
            "tenants": {("acme", "oidc-alice"): "site_alice"},
            "merchants": [_merchant_row(), _merchant_row("acme")],
        }
        handler, responses, _ = _handler(self._session_headers(claims), **store_args)
        self.assertEqual(handler._authenticate(), ("acme", "oidc-alice"))

        handler, responses, _ = _handler(self._session_headers(claims), **store_args)
        handler.store.tenant("acme", "oidc-alice")["disabled_at"] = "2026-08-29"
        self.assertIsNone(handler._authenticate())
        self.assertEqual(responses[-1][0], 403)

        handler, responses, _ = _handler(
            self._session_headers(claims),
            tenants={("acme", "oidc-alice"): "site_alice"},
            merchants=[_merchant_row(), _merchant_row("acme", disabled_at="2026-08-29")],
        )
        self.assertIsNone(handler._authenticate())
        self.assertEqual(responses[-1][0], 403)

    def test_a_session_for_a_tenant_that_is_gone_is_refused(self) -> None:
        # No just-in-time creation from a cookie: the row has to be there.
        handler, responses, _ = _handler(
            self._session_headers(
                {"sub": "alice", "mid": "acme", "uid": "oidc-ghost", "adm": False}
            ),
            merchants=[_merchant_row(), _merchant_row("acme")],
        )
        self.assertIsNone(handler._authenticate())
        self.assertEqual(responses[-1][0], 403)
        self.assertEqual(handler.store.created, [])

    def test_a_session_may_not_act_for_a_subject(self) -> None:
        handler, responses, _ = _handler(
            {
                **self._session_headers({"sub": "local-admin", "adm": True}),
                "X-Acting-Subject": "0" * 32,
            }
        )
        self.assertIsNone(handler._authenticate())
        self.assertEqual(responses[-1][0], 403)

    def test_an_expired_session_is_refused(self) -> None:
        token, _ = console_session.issue({"sub": "a", "adm": True}, SESSION_KEY, now=1000)
        headers = _Headers({"Cookie": f"{console_session.COOKIE}={token}"})
        self.assertIsNone(
            console_session.verify(
                headers, SESSION_KEY, now=1000 + console_session.SESSION_TTL_SECONDS + 1
            )
        )
        self.assertIsNotNone(console_session.verify(headers, SESSION_KEY, now=1001))

    def test_no_signing_key_means_no_sessions_at_all(self) -> None:
        # Fail closed if the wiring is ever missing: an empty key must not verify
        # everything, which is what a naive HMAC comparison against "" would do.
        token, _ = console_session.issue({"sub": "a", "adm": True}, SESSION_KEY)
        headers = _Headers({"Cookie": f"{console_session.COOKIE}={token}"})
        self.assertIsNone(console_session.verify(headers, ""))

    def test_a_malformed_cookie_is_not_an_error(self) -> None:
        self.assertIsNone(console_session.verify(_Headers({"Cookie": "broken"}), SESSION_KEY))
        self.assertFalse(console_session.has_session(_Headers({"Cookie": "broken"})))


class OidcLandingTests(unittest.TestCase):
    """Where a verified login is allowed to land. Merchants are never created here."""

    def _login(self, **overrides):
        from sites.oidc import LoginIdentity

        fields = {
            "subject": "alice-subject",
            "email": "alice@example.test",
            "admin": False,
            "merchant_id": "acme",
            "user_id": "oidc-alice",
        }
        fields.update(overrides)
        return LoginIdentity(**fields)

    def _config(self, *, signups: bool = False, domains: tuple[str, ...] = ()):
        from sites.oidc import Config

        return Config(
            issuer="https://idp.example.test",
            client_id="site-console",
            client_secret="",
            audience="site",
            redirect_url="https://sites.example.test/v1/auth/callback",
            scopes="openid email",
            admin_claim="groups",
            admin_value="platform-admin",
            merchant_claim="org",
            merchant_map={"acme-corp": "acme"},
            signups_enabled=signups,
            email_domains=domains,
        )

    def _handler(self, *, signups=False, domains=(), **store):
        handler, responses, wire = _handler(
            {}, oidc_config=self._config(signups=signups, domains=domains), **store
        )
        return handler, responses

    def test_an_admin_login_lands_on_the_pinned_identity(self) -> None:
        handler, _ = self._handler()
        self.assertEqual(
            handler._resolve_login_tenant(self._login(admin=True)),
            (DEFAULT_MERCHANT_ID, identity.DEFAULT_USER_ID),
        )

    def test_an_existing_tenant_is_reused_without_creating_anything(self) -> None:
        handler, _ = self._handler(
            tenants={("acme", "oidc-alice"): "site_alice"},
            merchants=[_merchant_row(), _merchant_row("acme")],
        )
        self.assertEqual(
            handler._resolve_login_tenant(self._login()), ("acme", "oidc-alice")
        )
        self.assertEqual(handler.store.created, [])

    def test_a_first_time_user_is_refused_while_signups_are_closed(self) -> None:
        handler, responses = self._handler(
            merchants=[_merchant_row(), _merchant_row("acme")]
        )
        with mock.patch("sites.api_auth.telemetry.log") as log:
            self.assertIsNone(handler._resolve_login_tenant(self._login()))
        self.assertEqual(responses[-1][0], 403)
        self.assertEqual(handler.store.created, [])
        self.assertIn(
            "console_login_signup_refused",
            [call.args[0] for call in log.call_args_list],
        )

    def test_signups_create_a_tenant_only_inside_the_allowed_domains(self) -> None:
        handler, _ = self._handler(
            signups=True,
            domains=("example.test",),
            merchants=[_merchant_row(), _merchant_row("acme")],
        )
        self.assertEqual(
            handler._resolve_login_tenant(self._login()), ("acme", "oidc-alice")
        )
        self.assertEqual(handler.store.created, [("acme", "oidc-alice")])

        handler, responses = self._handler(
            signups=True,
            domains=("example.test",),
            merchants=[_merchant_row(), _merchant_row("acme")],
        )
        self.assertIsNone(
            handler._resolve_login_tenant(self._login(email="mallory@elsewhere.test"))
        )
        self.assertEqual(responses[-1][0], 403)
        self.assertEqual(handler.store.created, [])

    def test_a_mapped_merchant_that_does_not_exist_is_refused_and_logged(self) -> None:
        """🔴 Contract §4: no merchant is created, and the reason is written down.

        Silently landing the user somewhere else produces "my permissions vanished", which
        nobody traces back to a login. The mapping table names merchants that must already
        exist; a stale entry is an operator error and has to read like one.
        """
        handler, responses = self._handler(
            signups=True, domains=("example.test",), merchants=[_merchant_row()]
        )
        with mock.patch("sites.api_auth.telemetry.log") as log:
            self.assertIsNone(handler._resolve_login_tenant(self._login()))
        self.assertEqual(responses[-1][0], 403)
        self.assertEqual(handler.store.created, [])
        self.assertIn(
            "console_login_merchant_unavailable",
            [call.args[0] for call in log.call_args_list],
        )

    def test_a_disabled_merchant_is_refused(self) -> None:
        handler, responses = self._handler(
            signups=True,
            domains=("example.test",),
            merchants=[
                _merchant_row(),
                _merchant_row("acme", disabled_at="2026-08-29"),
            ],
        )
        self.assertIsNone(handler._resolve_login_tenant(self._login()))
        self.assertEqual(responses[-1][0], 403)
        self.assertEqual(handler.store.created, [])

    def test_a_disabled_tenant_cannot_be_revived_by_logging_in(self) -> None:
        handler, responses = self._handler(
            signups=True,
            domains=("example.test",),
            tenants={("acme", "oidc-alice"): "site_alice"},
            merchants=[_merchant_row(), _merchant_row("acme")],
        )
        handler.store.tenant("acme", "oidc-alice")["disabled_at"] = "2026-08-29"
        self.assertIsNone(handler._resolve_login_tenant(self._login()))
        self.assertEqual(responses[-1][0], 403)


class LogoutTests(unittest.TestCase):
    def test_logout_clears_both_cookies(self) -> None:
        token, csrf = console_session.issue({"sub": "a", "adm": True}, SESSION_KEY)
        handler, responses, wire = _handler(
            {
                "Cookie": f"{console_session.COOKIE}={token}",
                console_session.CSRF_HEADER: csrf,
            }
        )
        handler._logout()
        self.assertEqual(wire.status, 204)
        self.assertEqual(
            wire.cookies(), {console_session.COOKIE: "", console_session.CSRF_COOKIE: ""}
        )

    def test_logout_with_a_session_but_no_csrf_is_refused(self) -> None:
        token, _ = console_session.issue({"sub": "a", "adm": True}, SESSION_KEY)
        handler, responses, wire = _handler(
            {"Cookie": f"{console_session.COOKIE}={token}"}
        )
        handler._logout()
        self.assertEqual(responses[-1][0], 403)

    def test_logout_without_any_session_still_clears(self) -> None:
        handler, responses, wire = _handler({})
        handler._logout()
        self.assertEqual(wire.status, 204)


class ActingAuditTests(unittest.TestCase):
    def test_an_acting_call_is_audited_on_both_outcomes(self) -> None:
        """Contract §3.4: one line per impersonating call, with the outcome."""
        headers = _Headers(
            {"X-Sites-Service-Token": "sitem_key", "X-Acting-Subject": "a" * 32}
        )
        for outcome in ("allow", "deny"):
            with mock.patch("sites.identity.telemetry.log") as log:
                identity.audit_acting_call(headers, "POST", "/v1/deployments?x=1", outcome)
            kwargs = log.call_args.kwargs
            self.assertEqual(log.call_args.args[0], "auth_acting_call")
            self.assertEqual(kwargs["outcome"], outcome)
            self.assertEqual(kwargs["acting_as"], "a" * 32)
            self.assertEqual(kwargs["route"], "POST /v1/deployments")
            # The key is identified by digest prefix: the log store is read by more people
            # than the database is, and a plaintext prefix is part of the secret.
            self.assertNotIn("sitem_key", json.dumps(kwargs))

    def test_a_call_that_acts_for_nobody_is_not_audited(self) -> None:
        with mock.patch("sites.identity.telemetry.log") as log:
            identity.audit_acting_call(
                _Headers({"X-Sites-Service-Token": "site_x"}), "GET", "/v1/deployments", "allow"
            )
        log.assert_not_called()


if __name__ == "__main__":
    unittest.main()
