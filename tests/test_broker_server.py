"""End-to-end tests: a real Broker gateway on an ephemeral TCP port, driven with
real HTTP requests. Covers contract sections 3.1-3.5 as seen from outside the
process -- everything test_broker_{db,tokens,secretbox,config_store,identity,
auth}.py already covers at the unit level, this exercises through the socket,
the router's auth-mode resolution, and the JSON/cookie/SSE wire format.
"""
from __future__ import annotations

import json
import os
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler
from typing import Any

from sites.broker import config_store, identity, mailer as broker_mailer, server
from sites.broker.auth import AuthService
from sites.broker.http import HttpError, Response, Router, Services, StreamResponse

from tests import broker_fixtures as fx

_VALID_CONFIG = {
    "apiVersion": "hull/v1alpha1", "app": "demo",
    "auth": {"methods": ["email_otp"], "registration": "open"},
    "roles": ["member"], "default_role": "member",
    "plans": {"free": {}}, "default_plan": "free",
    "entities": {}, "permissions": {}, "agents": {}, "flows": {},
    "secrets": ["SUPER_SECRET_VALUE_MARKER"],
}
ADMIN_TOKEN = "x" * 40


class _ServerCase(unittest.TestCase):
    """Base class that stands up one real gateway per test method (cheap: an
    in-process ThreadingHTTPServer, not a subprocess) against the shared test
    database, with a fresh app_env_id so tests never see each other's rows.
    """

    def setUp(self) -> None:
        self.db = fx.database()
        self.keys = fx.keyring()
        self.secret_key = fx.secret_key()
        self.store = config_store.ConfigStore(self.db, self.secret_key, cache_seconds=0)
        self.mail = broker_mailer.InMemoryMailer()
        self.auth_service = AuthService(self.db, self.store, self.keys, self.mail,
                                         self.secret_key)
        self.reviewer = identity.FakeTokenReviewer()
        self.services = Services(
            db=self.db, config=self.store, keys=self.keys, clock=time.time,
            auth=self.auth_service, mailer=self.mail, reviewer=self.reviewer,
            admin_token=ADMIN_TOKEN, dev_tokens_enabled=True,
        )
        self.router = server.build_router(self.services)
        self.httpd = server.listen("127.0.0.1", 0, self.router, self.services)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.app_env_id = fx.app_env_id()
        self.addCleanup(self._stop)

    def _stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def call(self, method: str, path: str, body: Any = None,
             headers: dict[str, str] | None = None, raw_body: bytes | None = None,
             extra_content_length: int | None = None) -> tuple[int, dict, Any]:
        data = raw_body
        send_headers = dict(headers or {})
        if data is None and body is not None:
            data = json.dumps(body).encode("utf-8")
            send_headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers=send_headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as resp:
                raw = resp.read()
                return resp.status, dict(resp.headers), (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                payload = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                payload = raw.decode("utf-8", "replace")
            return exc.code, dict(exc.headers), payload

    def admin(self) -> dict:
        return {"Authorization": f"Bearer {ADMIN_TOKEN}"}

    def plan_and_apply(self, config: dict = _VALID_CONFIG) -> None:
        status, _, plan = self.call(
            "POST", f"/_hull/admin/v1/apps/{self.app_env_id}/config/plan",
            {"config": config}, self.admin(),
        )
        self.assertEqual(status, 200, plan)
        status, _, applied = self.call(
            "POST", f"/_hull/admin/v1/apps/{self.app_env_id}/config/apply",
            {"config": config, "planDigest": plan["digest"]}, self.admin(),
        )
        self.assertEqual(status, 200, applied)


class HealthTests(_ServerCase):
    def test_healthz(self) -> None:
        status, _, body = self.call("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok"})

    def test_readyz(self) -> None:
        status, _, body = self.call("GET", "/readyz")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ready")


class RoutingAndBodyTests(_ServerCase):
    def test_unknown_route_is_404(self) -> None:
        status, _, body = self.call("GET", "/_hull/nope")
        self.assertEqual(status, 404)
        self.assertEqual(body["code"], "route_not_found")

    def test_wrong_method_is_405(self) -> None:
        status, _, body = self.call("GET", "/_hull/v1/app-token")
        self.assertEqual(status, 405)
        self.assertEqual(body["code"], "method_not_allowed")

    def test_malformed_json_is_400(self) -> None:
        status, _, body = self.call(
            "POST", "/_hull/v1/app-token", raw_body=b"{not json",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "invalid_json")

    def test_body_over_the_limit_is_413(self) -> None:
        import os
        os.environ["HULL_MAX_BODY_BYTES"] = "16"
        try:
            status, _, body = self.call(
                "POST", "/_hull/v1/app-token",
                {"serviceAccountToken": "x" * 100},
            )
        finally:
            del os.environ["HULL_MAX_BODY_BYTES"]
        self.assertEqual(status, 413)
        self.assertEqual(body["code"], "body_too_large")


class AdminAuthTests(_ServerCase):
    def test_missing_admin_token_is_401(self) -> None:
        status, _, body = self.call("GET", f"/_hull/admin/v1/apps/{self.app_env_id}/config")
        self.assertEqual(status, 401)
        self.assertEqual(body["code"], "invalid_admin_token")

    def test_wrong_admin_token_is_401(self) -> None:
        status, _, body = self.call(
            "GET", f"/_hull/admin/v1/apps/{self.app_env_id}/config",
            headers={"Authorization": "Bearer wrong-token-wrong-token-wrong"},
        )
        self.assertEqual(status, 401)
        self.assertEqual(body["code"], "invalid_admin_token")

    def test_bad_app_env_id_is_400(self) -> None:
        status, _, body = self.call(
            "GET", "/_hull/admin/v1/apps/not-hex/config", headers=self.admin()
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "invalid_app_env_id")


class ConcurrentApplyOverHttpTests(_ServerCase):
    def test_one_of_two_concurrent_applies_gets_409(self) -> None:
        status, _, plan = self.call(
            "POST", f"/_hull/admin/v1/apps/{self.app_env_id}/config/plan",
            {"config": _VALID_CONFIG}, self.admin(),
        )
        self.assertEqual(status, 200)
        results = []
        lock = threading.Lock()

        def attempt() -> None:
            status, _, body = self.call(
                "POST", f"/_hull/admin/v1/apps/{self.app_env_id}/config/apply",
                {"config": _VALID_CONFIG, "planDigest": plan["digest"]}, self.admin(),
            )
            with lock:
                results.append(status)

        threads = [threading.Thread(target=attempt) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count(200), 1, results)
        self.assertEqual(results.count(409), 3, results)


class SecretsNeverReturnedTests(_ServerCase):
    def test_secret_value_never_appears_in_any_admin_response(self) -> None:
        self.plan_and_apply()
        marker = "sk-do-not-leak-this-value-ever"
        status, _, _ = self.call(
            "PUT", f"/_hull/admin/v1/apps/{self.app_env_id}/secrets/SUPER_SECRET_VALUE_MARKER",
            {"value": marker}, self.admin(),
        )
        self.assertEqual(status, 204)

        responses = [
            self.call("GET", f"/_hull/admin/v1/apps/{self.app_env_id}/config", headers=self.admin()),
            self.call("PUT", f"/_hull/admin/v1/apps/{self.app_env_id}/settings",
                       {"hosts": [f"{self.app_env_id[:8]}-secret-scan.test"]}, self.admin()),
            self.call("GET", f"/_hull/admin/v1/apps/{self.app_env_id}/mail", headers=self.admin()),
        ]
        for status, _, body in responses:
            with self.subTest(status=status):
                self.assertNotIn(marker, json.dumps(body))


class AppConfigTests(_ServerCase):
    def test_app_config_has_a_cache_control_header_and_no_settings_or_secrets(self) -> None:
        self.plan_and_apply()
        self.reviewer.add("sa-tok", f"system:serviceaccount:ns-{self.app_env_id[:12]}:sa-a")
        self.call("PUT", f"/_hull/admin/v1/apps/{self.app_env_id}/settings",
                   {"workloads": [{"namespace": f"ns-{self.app_env_id[:12]}", "serviceAccount": "sa-a"}]},
                   self.admin())
        status, _, token_body = self.call("POST", "/_hull/v1/app-token",
                                           {"serviceAccountToken": "sa-tok"})
        self.assertEqual(status, 200)
        status, headers, body = self.call(
            "GET", "/_hull/v1/app-config",
            headers={"Authorization": f"Bearer {token_body['token']}"},
        )
        self.assertEqual(status, 200)
        self.assertIn("max-age=", headers.get("Cache-Control", ""))
        self.assertNotIn("secrets", body)
        self.assertNotIn("hosts", body)  # settings must not leak through this route


class HostResolutionTests(_ServerCase):
    """Only the sign-in routes resolve the app from the Host header; everything else
    needs the app token, so browser code on the app domain cannot go around the app
    backend (contract section 3.4: all runtime routes require an app token)."""

    def test_unknown_host_is_404_on_a_sign_in_route(self) -> None:
        status, _, body = self.call("POST", "/_hull/auth/email/start", {"email": "a@example.test"},
                                    {"Host": "nobody-owns-this.test"})
        self.assertEqual(status, 404)
        self.assertEqual(body["code"], "unknown_app")

    def test_known_host_resolves_the_app_on_a_sign_in_route(self) -> None:
        self.plan_and_apply()
        self.call("PUT", f"/_hull/admin/v1/apps/{self.app_env_id}/settings",
                   {"hosts": [f"{self.app_env_id[:8]}-known-host.test"]}, self.admin())
        status, _, body = self.call("POST", "/_hull/auth/email/start", {"email": "a@example.test"},
                                    {"Host": f"{self.app_env_id[:8]}-known-host.test"})
        self.assertEqual(status, 202)
        self.assertEqual(len(self.mail.sent), 1)

    def test_a_known_host_is_not_an_app_token(self) -> None:
        self.plan_and_apply()
        self.call("PUT", f"/_hull/admin/v1/apps/{self.app_env_id}/settings",
                   {"hosts": [f"{self.app_env_id[:8]}-known-host.test"]}, self.admin())
        for method, path, body in (("GET", "/_hull/v1/app-config", None),
                                   ("POST", "/_hull/v1/flows/any/run", {"input": {}}),
                                   ("GET", "/_hull/v1/usage/me", None)):
            status, _, resp = self.call(method, path, body, {"Host": f"{self.app_env_id[:8]}-known-host.test"})
            self.assertEqual(status, 401, path)
            self.assertEqual(resp["code"], "app_token_required", path)


class HostOwnershipTests(_ServerCase):
    def test_another_app_cannot_take_over_a_host_or_workload(self) -> None:
        other = fx.app_env_id()
        for app in (self.app_env_id, other):
            self.call("POST", f"/_hull/admin/v1/apps/{app}/config/plan", {"config": _VALID_CONFIG}, self.admin())
        status, _, _ = self.call("PUT", f"/_hull/admin/v1/apps/{self.app_env_id}/settings",
                                 {"hosts": [f"{self.app_env_id[:8]}-owned.test"],
                                  "workloads": [{"namespace": f"ns-own-{self.app_env_id[:8]}", "serviceAccount": "sa-own"}]},
                                 self.admin())
        self.assertEqual(status, 200)
        status, _, body = self.call("PUT", f"/_hull/admin/v1/apps/{other}/settings",
                                    {"hosts": [f"{self.app_env_id[:8]}-owned.test"]}, self.admin())
        self.assertEqual((status, body["code"]), (409, "host_taken"))
        status, _, body = self.call("PUT", f"/_hull/admin/v1/apps/{other}/settings",
                                    {"workloads": [{"namespace": f"ns-own-{self.app_env_id[:8]}", "serviceAccount": "sa-own"}]},
                                    self.admin())
        self.assertEqual((status, body["code"]), (409, "workload_taken"))
        self.assertEqual(self.store.by_host(f"{self.app_env_id[:8]}-owned.test"), self.app_env_id)
        # the owner can still rewrite its own settings, and freeing the host lets another claim it
        self.assertEqual(self.call("PUT", f"/_hull/admin/v1/apps/{self.app_env_id}/settings",
                                   {"hosts": []}, self.admin())[0], 200)
        self.assertEqual(self.call("PUT", f"/_hull/admin/v1/apps/{other}/settings",
                                   {"hosts": [f"{self.app_env_id[:8]}-owned.test"]}, self.admin())[0], 200)


class ClientAddressTests(unittest.TestCase):
    """Browser traffic arrives through the Envoy data plane; per-IP sign-in limits
    must see the visitor, not the proxy, and must not trust a header the visitor wrote."""

    def setUp(self) -> None:
        import ipaddress

        self.proxies = (ipaddress.ip_network("10.0.0.0/8"),)

    def test_the_rightmost_untrusted_hop_is_the_client(self) -> None:
        self.assertEqual(server.client_ip("10.1.2.3", "203.0.113.9", self.proxies), "203.0.113.9")
        self.assertEqual(server.client_ip("10.1.2.3", "198.51.100.7, 203.0.113.9, 10.4.4.4",
                                          self.proxies), "203.0.113.9")

    def test_a_forwarded_header_from_an_untrusted_peer_is_ignored(self) -> None:
        self.assertEqual(server.client_ip("198.51.100.1", "203.0.113.9", self.proxies), "198.51.100.1")

    def test_nothing_is_trusted_by_default(self) -> None:
        self.assertEqual(server.client_ip("10.1.2.3", "203.0.113.9", ()), "10.1.2.3")

    def test_garbage_in_the_header_falls_back_to_the_peer(self) -> None:
        self.assertEqual(server.client_ip("10.1.2.3", "not-an-ip", self.proxies), "10.1.2.3")

    def test_the_setting_is_parsed_and_refuses_bad_entries(self) -> None:
        from unittest import mock

        with mock.patch.dict(os.environ, {"HULL_TRUSTED_PROXY_CIDRS": "10.0.0.0/8, fd00::/8"}):
            self.assertEqual(len(server.trusted_proxy_networks()), 2)
        with mock.patch.dict(os.environ, {"HULL_TRUSTED_PROXY_CIDRS": "10.0.0.0/33"}):
            with self.assertRaises(RuntimeError):
                server.trusted_proxy_networks()


class ForwardedClientThroughTheServerTests(_ServerCase):
    def setUp(self) -> None:
        from unittest import mock

        patcher = mock.patch.dict(os.environ, {"HULL_TRUSTED_PROXY_CIDRS": "127.0.0.0/8"})
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

        def echo_ip(request):
            return Response(200, {"ip": request.client_ip})

        self.router.add("GET", "/_hull/test/ip", echo_ip, auth="none")

    def test_the_handler_sees_the_forwarded_client(self) -> None:
        status, _, body = self.call("GET", "/_hull/test/ip", headers={"X-Forwarded-For": "203.0.113.9"})
        self.assertEqual(status, 200)
        self.assertEqual(body["ip"], "203.0.113.9")


class UserTokenAuthTests(_ServerCase):
    """X-Hull-User verification (contract section 3.1): app-config itself uses
    auth="app" and never looks at X-Hull-User, so these register a throwaway
    "app+optional_user" route -- the auth mode D's real runtime routes use --
    to exercise the check the router itself is responsible for.
    """

    def setUp(self) -> None:
        super().setUp()

        def echo_user(request):
            return Response(200, {"userId": request.user.id if request.user else None})

        self.router.add("GET", "/_hull/test/whoami", echo_user, auth="app+optional_user")

    def _sign_in(self) -> tuple[str, str]:
        self.plan_and_apply()
        self.call("PUT", f"/_hull/admin/v1/apps/{self.app_env_id}/settings",
                   {"hosts": [f"{self.app_env_id[:8]}-user-auth.test"]}, self.admin())
        self.call("POST", "/_hull/auth/email/start", {"email": "carol@example.com"},
                   {"Host": f"{self.app_env_id[:8]}-user-auth.test"})
        import re
        code = re.search(r"code is (\d{6})", self.mail.sent[-1]["body"]).group(1)
        status, headers, body = self.call(
            "POST", "/_hull/auth/email/verify", {"email": "carol@example.com", "code": code},
            {"Host": f"{self.app_env_id[:8]}-user-auth.test"},
        )
        self.assertEqual(status, 200, body)
        set_cookie = headers["Set-Cookie"]
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("Secure", set_cookie)
        self.assertIn("SameSite=Lax", set_cookie)
        self.assertIn("Path=/", set_cookie)
        self.assertIn("__Host-hull_session=", set_cookie)
        return body["token"], set_cookie.split(";")[0]

    def test_x_hull_user_with_matching_audience_is_accepted(self) -> None:
        user_token, _ = self._sign_in()
        self.reviewer.add("sa-tok-2", f"system:serviceaccount:ns-b-{self.app_env_id[:8]}:sa-b")
        self.call("PUT", f"/_hull/admin/v1/apps/{self.app_env_id}/settings",
                   {"workloads": [{"namespace": f"ns-b-{self.app_env_id[:8]}", "serviceAccount": "sa-b"}]},
                   self.admin())
        _, _, app_token_body = self.call("POST", "/_hull/v1/app-token",
                                          {"serviceAccountToken": "sa-tok-2"})
        app_token = app_token_body["token"]
        status, _, body = self.call(
            "GET", "/_hull/test/whoami",
            headers={"Authorization": f"Bearer {app_token}", "X-Hull-User": user_token},
        )
        self.assertEqual(status, 200, body)
        self.assertIsNotNone(body["userId"])

    def test_x_hull_user_with_mismatched_audience_is_401(self) -> None:
        # A user token minted for a *different* app must not be accepted here.
        other_app = fx.app_env_id()
        from sites.broker import tokens as broker_tokens
        foreign_token, _ = broker_tokens.issue_user_token(
            self.keys, app_env_id=other_app, user_id="u-foreign", session_id="s",
            org=None, role="member", plan="free", env="prod",
        )
        self.plan_and_apply()
        self.reviewer.add("sa-tok-3", f"system:serviceaccount:ns-c-{self.app_env_id[:8]}:sa-c")
        self.call("PUT", f"/_hull/admin/v1/apps/{self.app_env_id}/settings",
                   {"workloads": [{"namespace": f"ns-c-{self.app_env_id[:8]}", "serviceAccount": "sa-c"}]},
                   self.admin())
        _, _, app_token_body = self.call("POST", "/_hull/v1/app-token",
                                          {"serviceAccountToken": "sa-tok-3"})
        status, _, body = self.call(
            "GET", "/_hull/test/whoami",
            headers={"Authorization": f"Bearer {app_token_body['token']}",
                     "X-Hull-User": foreign_token},
        )
        self.assertEqual(status, 401)
        self.assertEqual(body["code"], "invalid_user_token")


class SseWriterTests(_ServerCase):
    """StreamResponse -> SSE wire format, using a fake handler registered just
    for this test (D's real agent-run routes do not exist yet).
    """

    def setUp(self) -> None:
        super().setUp()

        def events():
            yield "run.started", {"runId": "r1"}
            yield "message.delta", {"text": "hi"}
            yield "run.finished", {"usage": {"credits": 1}}

        def handler(request):
            return StreamResponse(events=events())

        self.router.add("GET", "/_hull/test/stream", handler, auth="none")

    def test_sse_events_are_written_in_order(self) -> None:
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/_hull/test/stream")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "text/event-stream")
        body = resp.read().decode("utf-8")
        conn.close()
        self.assertIn("event: run.started\ndata: {\"runId\": \"r1\"}\n\n", body)
        self.assertIn("event: message.delta", body)
        self.assertIn("event: run.finished", body)
        # events arrive in the order the generator yielded them
        self.assertLess(body.index("run.started"), body.index("message.delta"))
        self.assertLess(body.index("message.delta"), body.index("run.finished"))


class RuntimeApiAbsenceTests(_ServerCase):
    """A build without the runtime module must still serve; simulate that build."""

    def setUp(self) -> None:
        import sys
        from unittest import mock

        patcher = mock.patch.dict(sys.modules, {"sites.broker.runtime_api": None})
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

    def test_gateway_starts_without_partition_d(self) -> None:
        # build_router() already ran in setUp without raising; this just pins
        # that a D-owned path is simply absent, not a 500.
        status, _, body = self.call("POST", "/_hull/v1/flows/summarize/run", {"input": {}})
        self.assertEqual(status, 404)
        self.assertEqual(body["code"], "route_not_found")

    def test_usage_is_501_without_partition_d(self) -> None:
        status, _, body = self.call(
            "GET", f"/_hull/admin/v1/apps/{self.app_env_id}/usage", headers=self.admin()
        )
        self.assertEqual(status, 501)
        self.assertEqual(body["code"], "not_available")


class DevTokensDisabledTests(_ServerCase):
    def setUp(self) -> None:
        super().setUp()
        # Rebuild services/router/server with dev tokens off, matching the
        # HULL_BROKER_DEV_TOKENS=1 gate in admin_api.py.
        self._stop()
        self.services.dev_tokens_enabled = False
        self.router = server.build_router(self.services)
        self.httpd = server.listen("127.0.0.1", 0, self.router, self.services)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def test_dev_app_token_is_not_available(self) -> None:
        status, _, body = self.call(
            "POST", f"/_hull/admin/v1/apps/{self.app_env_id}/dev-app-token", {},
            self.admin(),
        )
        self.assertEqual(status, 404)
        self.assertEqual(body["code"], "not_available")

    def test_mail_listing_is_not_available(self) -> None:
        status, _, body = self.call(
            "GET", f"/_hull/admin/v1/apps/{self.app_env_id}/mail", headers=self.admin()
        )
        self.assertEqual(status, 404)
        self.assertEqual(body["code"], "not_available")


class AdminMiscTests(_ServerCase):
    def test_invalid_config_returns_the_error_list(self) -> None:
        broken = {k: v for k, v in _VALID_CONFIG.items() if k != "default_role"}
        status, _, body = self.call(
            "POST", f"/_hull/admin/v1/apps/{self.app_env_id}/config/plan",
            {"config": broken}, self.admin(),
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "invalid_config")
        self.assertTrue(any("default_role" in e for e in body["errors"]))

    def test_put_user_plan_for_unknown_user_is_404(self) -> None:
        self.plan_and_apply()
        status, _, body = self.call(
            "PUT", f"/_hull/admin/v1/apps/{self.app_env_id}/users/no-such-user/plan",
            {"plan": "pro"}, self.admin(),
        )
        self.assertEqual(status, 404)
        self.assertEqual(body["code"], "unknown_user")

    def test_config_not_found_before_any_apply(self) -> None:
        status, _, body = self.call(
            "GET", f"/_hull/admin/v1/apps/{fx.app_env_id()}/config", headers=self.admin()
        )
        self.assertEqual(status, 404)
        self.assertEqual(body["code"], "no_active_config")


if __name__ == "__main__":
    unittest.main()
