"""The embedded panel proxy must not become a Grafana API key with a UI.

Almost every case here is negative, and that ratio is the point: this feature
adds a route that attaches a Grafana service-account token to outbound
requests. A proxy that merely renders a panel has already failed if it also
forwards the console session cookie, answers a non-administrator, or passes
``/api/datasources/proxy/`` - each of which turns one dashboard into the whole
Grafana API.

The control plane runs in-process over real HTTP against a stub Grafana, so the
identity check under test is the real one. No database is involved: the panel
route never reaches the store, and asserting that is part of the point.
"""
from __future__ import annotations

import json
import os
import re
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sites import grafana_proxy
from sites.api import Handler

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = ROOT / "observability" / "dashboards" / "sites-control-plane.json"

ADMIN_TOKEN = "a" * 32
SESSION_KEY = "k" * 32
SA_TOKEN = "grafana-service-account-token"
DATASOURCE = "prom-uid"


def _query_body(uid: str = DATASOURCE) -> bytes:
    """A body the datasource check accepts: one query naming the declared datasource."""
    return json.dumps(
        {"queries": [{"refId": "A", "datasource": {"uid": uid, "type": "prometheus"}}]}
    ).encode("utf-8")

_SEEN: list[dict] = []
_SEEN_LOCK = threading.Lock()


# Address shape, not the word: `grafana: GrafanaCapability` is a legitimate
# TypeScript annotation in the component this guards, and the first version of
# the guard matched it.
HOST_PORT = re.compile(r"[A-Za-z0-9.-]*grafana[A-Za-z0-9.-]*:\d{2,5}")


class _GrafanaStub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:
        return

    def _answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        with _SEEN_LOCK:
            _SEEN.append({
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
            })
        payload = b"<html>panel</html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        # Grafana really does set a session cookie here. It must not land on the
        # console's origin.
        self.send_header("Set-Cookie", "grafana_session=abc; Path=/")
        self.send_header("X-Grafana-Internal", "leaked")
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _answer
    do_POST = _answer


class _StoreShouldNotBeUsed:
    """Any attribute access is a failure.

    The panel route has no business touching the control-plane database, and a
    stub that quietly answers would hide it if one day it did.
    """

    def __getattr__(self, name: str):
        raise AssertionError(f"the panel route touched the store: {name}")


class _EmbedTestCase(unittest.TestCase):
    PANEL = "/grafana/d-solo/sites-control-plane/panel"

    @classmethod
    def setUpClass(cls) -> None:
        Handler.kube = None
        Handler.store = _StoreShouldNotBeUsed()
        Handler.service_token = ADMIN_TOKEN
        Handler.session_key = SESSION_KEY
        Handler.local_login_enabled = True
        Handler.oidc_config = None
        Handler.mutation_lock = threading.Lock()
        Handler.synchronizer = None
        cls.grafana = ThreadingHTTPServer(("127.0.0.1", 0), _GrafanaStub)
        cls.grafana_thread = threading.Thread(
            target=cls.grafana.serve_forever, daemon=True
        )
        cls.grafana_thread.start()
        cls.grafana_url = f"http://127.0.0.1:{cls.grafana.server_address[1]}"
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.grafana.shutdown()
        cls.grafana.server_close()
        cls.grafana_thread.join(timeout=2)

    def setUp(self) -> None:
        with _SEEN_LOCK:
            _SEEN.clear()
        self._old = {
            name: os.environ.get(name)
            for name in (
                "SITES_GRAFANA_URL", "SITES_GRAFANA_TOKEN",
                "SITES_GRAFANA_TOKEN_FILE", "SITES_GRAFANA_ORG_ID",
                "SITES_GRAFANA_DASHBOARD_UID", "SITES_GRAFANA_DATASOURCE_UID",
            )
        }
        for name in self._old:
            os.environ.pop(name, None)

    def tearDown(self) -> None:
        for name, value in self._old.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def configure_grafana(self) -> None:
        os.environ["SITES_GRAFANA_URL"] = self.grafana_url
        os.environ["SITES_GRAFANA_TOKEN"] = SA_TOKEN
        os.environ["SITES_GRAFANA_DATASOURCE_UID"] = DATASOURCE

    def seen(self) -> list[dict]:
        with _SEEN_LOCK:
            return list(_SEEN)

    def call(
        self,
        method: str,
        path: str,
        *,
        admin: bool = True,
        headers: dict | None = None,
        body: bytes | None = None,
    ) -> tuple[int, dict, bytes]:
        sent = {"Connection": "close"}
        if admin:
            sent["X-Sites-Service-Token"] = ADMIN_TOKEN
        # The console session cookie always rides along: proving it is dropped
        # is the point of the credential-isolation assertions.
        sent["Cookie"] = "sites_console_session=console-value"
        sent.update(headers or {})
        request = urllib.request.Request(
            self.url + path, data=body, method=method, headers=sent
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()


class AccessControlTests(_EmbedTestCase):
    def test_a_caller_without_the_admin_credential_is_refused(self) -> None:
        """Signed out and signed-in-but-not-admin get the same 403.

        The console's own admin gate does not distinguish them either
        (``_require_admin`` answers 403 for both), and the panel follows it
        rather than inventing a second convention.
        """
        self.configure_grafana()
        self.assertEqual(self.call("GET", self.PANEL, admin=False)[0], 403)
        self.assertEqual(
            self.call(
                "GET", self.PANEL, admin=False,
                headers={"X-Sites-Service-Token": "b" * 32},
            )[0],
            403,
        )
        self.assertEqual(self.seen(), [], "a refused request reached Grafana")

    def test_an_administrator_reaches_the_panel(self) -> None:
        """Mutation anchor: without it, deleting the proxy would pass the refusals."""
        self.configure_grafana()
        status, _, payload = self.call("GET", self.PANEL)
        self.assertEqual(status, 200, payload)
        self.assertIn(b"panel", payload)
        self.assertEqual(len(self.seen()), 1)

    def test_authorisation_is_checked_before_configuration(self) -> None:
        """Otherwise 403-vs-404 is a free configuration oracle."""
        without = self.call("GET", self.PANEL, admin=False)[0]
        self.configure_grafana()
        configured = self.call("GET", self.PANEL, admin=False)[0]
        self.assertEqual((without, configured), (403, 403))


class PathAllowlistTests(_EmbedTestCase):
    # Every one of these is a real Grafana endpoint. The first is the reason the
    # allowlist exists: with a service-account token attached it is "run any
    # query against any datasource this Grafana can see".
    FORBIDDEN = (
        ("GET", "/grafana/api/datasources/proxy/1/api/v1/query?query=up"),
        ("GET", "/grafana/api/datasources"),
        ("GET", "/grafana/api/admin/settings"),
        ("GET", "/grafana/api/admin/users"),
        ("GET", "/grafana/api/auth/keys"),
        ("GET", "/grafana/api/orgs"),
        ("GET", "/grafana/api/users"),
        ("GET", "/grafana/d/sites-control-plane/panel"),
        ("GET", "/grafana/login"),
        ("GET", "/grafana/"),
        ("POST", "/grafana/api/dashboards/db"),
        ("GET", "/grafana/../api/admin/settings"),
        ("GET", "/grafana//evil.example/"),
    )

    def test_every_non_panel_path_is_refused(self) -> None:
        self.configure_grafana()
        for method, path in self.FORBIDDEN:
            with self.subTest(path=path):
                status, _, _ = self.call(
                    method, path,
                    headers={"Sec-Fetch-Site": "same-origin"},
                    body=b"{}" if method == "POST" else None,
                )
                self.assertEqual(status, 403)
        self.assertEqual(
            self.seen(), [], "a non-allowlisted path was forwarded upstream"
        )

    def test_the_paths_a_panel_needs_are_allowed(self) -> None:
        """Mutation anchor: an allowlist that refuses everything also passes the test above."""
        self.configure_grafana()
        allowed = (
            ("GET", "/grafana/d-solo/sites-control-plane/panel?panelId=10"),
            ("GET", "/grafana/public/build/runtime.js"),
            ("GET", "/grafana/api/frontend/settings"),
            ("GET", "/grafana/api/dashboards/uid/sites-control-plane"),
            ("POST", "/grafana/api/ds/query"),
        )
        for method, path in allowed:
            with self.subTest(path=path):
                status, _, payload = self.call(
                    method, path,
                    headers={"Sec-Fetch-Site": "same-origin"},
                    body=_query_body() if method == "POST" else None,
                )
                self.assertEqual(status, 200, payload)
        self.assertEqual(len(self.seen()), len(allowed))

    def test_the_one_write_shaped_route_requires_same_origin(self) -> None:
        """POST /api/ds/query stands in for the CSRF token it cannot carry.

        Grafana's own front-end issues it and cannot read the console's
        double-submit cookie, so fetch metadata replaces that check. Without
        this the single non-GET entry would have no CSRF defence at all.
        """
        self.configure_grafana()
        status, _, _ = self.call(
            "POST", "/grafana/api/ds/query",
            headers={"Sec-Fetch-Site": "cross-site"}, body=_query_body(),
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.seen(), [])

    def test_a_request_with_neither_fetch_metadata_nor_origin_is_refused(self) -> None:
        """🔴 The fail-open this check exists to avoid.

        Both headers are "meaningful only when present", so "check
        Sec-Fetch-Site, else check Origin, else allow" admits anything that omits
        both. Refusing that case turns away every page-driven cross-origin
        request, which is the class CSRF is about, and costs nothing: a browser
        always sends Origin on a same-origin POST. It does not stop a caller that
        composes its own request - that one can send Origin too - but such a
        caller already holds the session cookie and is a session-theft problem,
        not a CSRF one.
        """
        self.configure_grafana()
        status, _, _ = self.call("POST", "/grafana/api/ds/query", body=_query_body())
        self.assertEqual(status, 403)
        self.assertEqual(self.seen(), [])

    def test_an_origin_header_alone_is_enough(self) -> None:
        """Mutation anchor: a check that refuses everything also passes the two above."""
        self.configure_grafana()
        status, _, payload = self.call(
            "POST", "/grafana/api/ds/query",
            headers={"Origin": self.url}, body=_query_body(),
        )
        self.assertEqual(status, 200, payload)


class DatasourceScopeTests(_EmbedTestCase):
    """The allowlist bounds the URL; only the body bounds /api/ds/query.

    That endpoint dispatches on a datasource uid inside the request, so it is
    read-only *of the datasource it names*. An operator whose Grafana also has a
    SQL datasource attached would otherwise have handed every console
    administrator a same-origin channel for arbitrary SQL - and a folder-scoped
    Viewer does not stop it, because OSS Grafana does not scope datasource
    permissions by folder.
    """

    PATH = "/grafana/api/ds/query"

    def post(self, body: bytes) -> int:
        return self.call(
            "POST", self.PATH,
            headers={"Sec-Fetch-Site": "same-origin"}, body=body,
        )[0]

    def test_the_declared_datasource_is_forwarded(self) -> None:
        """Mutation anchor for every refusal below."""
        self.configure_grafana()
        self.assertEqual(self.post(_query_body()), 200)
        self.assertEqual(len(self.seen()), 1)

    def test_another_datasource_is_refused(self) -> None:
        self.configure_grafana()
        self.assertEqual(self.post(_query_body("some-mysql-datasource")), 403)
        self.assertEqual(self.seen(), [])

    def test_one_out_of_bounds_query_refuses_the_whole_request(self) -> None:
        """Forwarding the rest would still have run the one that mattered."""
        self.configure_grafana()
        body = json.dumps({
            "queries": [
                {"refId": "A", "datasource": {"uid": DATASOURCE}},
                {"refId": "B", "datasource": {"uid": "some-mysql-datasource"}},
            ]
        }).encode("utf-8")
        self.assertEqual(self.post(body), 403)
        self.assertEqual(self.seen(), [])

    def test_every_uncertain_body_is_refused(self) -> None:
        """Unparseable, empty, unnamed and legacy-addressed bodies all fail closed.

        A query with no datasource is the subtle one: Grafana falls back to its
        default datasource, which is whatever the operator made default - not
        something this proxy has vetted.
        """
        self.configure_grafana()
        cases = {
            "not json": b"<html>",
            "not an object": b"[]",
            "no queries": b"{}",
            "empty queries": b'{"queries": []}',
            "no datasource": b'{"queries": [{"refId": "A"}]}',
            "legacy numeric id": b'{"queries": [{"refId": "A", "datasourceId": 1}]}',
            "uid is not a string": b'{"queries": [{"datasource": {"uid": 1}}]}',
        }
        for name, body in cases.items():
            with self.subTest(case=name):
                self.assertEqual(self.post(body), 403)
        self.assertEqual(self.seen(), [])


class CredentialIsolationTests(_EmbedTestCase):
    def test_the_console_session_cookie_never_reaches_grafana(self) -> None:
        self.configure_grafana()
        self.call("GET", self.PANEL)
        (seen,) = self.seen()
        self.assertNotIn("cookie", seen["headers"])
        self.assertNotIn(
            "x-sites-service-token", seen["headers"],
            "the console's own admin credential must not cross either",
        )

    def test_only_the_service_account_token_goes_upstream(self) -> None:
        self.configure_grafana()
        self.call("GET", self.PANEL)
        (seen,) = self.seen()
        self.assertEqual(seen["headers"]["authorization"], f"Bearer {SA_TOKEN}")
        self.assertNotIn(ADMIN_TOKEN, seen["headers"]["authorization"])

    def test_grafana_set_cookie_never_reaches_the_browser(self) -> None:
        self.configure_grafana()
        _, headers, _ = self.call("GET", self.PANEL)
        lowered = {name.lower() for name in headers}
        self.assertNotIn("set-cookie", lowered)
        self.assertNotIn(
            "x-grafana-internal", lowered,
            "response headers are an allowlist; unknown upstream headers stay upstream",
        )


class ResponsePolicyTests(_EmbedTestCase):
    def test_console_assets_keep_their_own_framing_policy(self) -> None:
        """Acceptance criterion: adding the panel must not relax the console CSP."""
        from sites.http_kit import CONSOLE_CSP

        self.assertIn("frame-ancestors 'none'", CONSOLE_CSP)
        self.assertIn("default-src 'self'", CONSOLE_CSP)
        # frame-src is what a cross-origin iframe would have needed. Its absence
        # is the whole point of proxying on this origin.
        self.assertNotIn("frame-src", CONSOLE_CSP)

    def test_the_panel_response_may_be_framed_by_this_origin_only(self) -> None:
        self.configure_grafana()
        _, headers, _ = self.call("GET", self.PANEL)
        policy = headers["Content-Security-Policy"]
        self.assertIn("frame-ancestors 'self'", policy)
        self.assertNotIn("frame-ancestors 'none'", policy)
        self.assertEqual(headers["X-Frame-Options"], "SAMEORIGIN")
        self.assertIn("object-src 'none'", policy)
        self.assertIn("form-action 'none'", policy)
        self.assertIn("base-uri 'none'", policy)


class UnconfiguredDeploymentTests(_EmbedTestCase):
    def test_the_route_does_not_exist_without_grafana(self) -> None:
        self.assertEqual(self.call("GET", self.PANEL)[0], 404)

    def test_half_configured_counts_as_unconfigured(self) -> None:
        """A base URL with no token would fill the iframe with 401s: worse than an absent tab."""
        os.environ["SITES_GRAFANA_URL"] = self.grafana_url
        self.assertFalse(grafana_proxy.load_config().enabled)
        self.assertEqual(self.call("GET", self.PANEL)[0], 404)
        self.assertEqual(self.seen(), [])

    def test_an_unnamed_datasource_counts_as_unconfigured(self) -> None:
        """Without it the proxy cannot bound what /api/ds/query may reach.

        Refusing to enable is the honest outcome: the alternative is a panel
        that renders while forwarding queries nobody scoped.
        """
        os.environ["SITES_GRAFANA_URL"] = self.grafana_url
        os.environ["SITES_GRAFANA_TOKEN"] = SA_TOKEN
        self.assertFalse(grafana_proxy.load_config().enabled)
        self.assertEqual(self.call("GET", self.PANEL)[0], 404)
        self.assertEqual(self.seen(), [])


class PanelCatalogTests(unittest.TestCase):
    """The catalog and the dashboard are two spellings of one fact.

    A panel id that no longer exists renders as an empty rectangle: Grafana
    answers 200 with nothing in it, so no status code, log line or metric says
    anything is wrong. This is the only place the two can be compared.
    """

    def test_every_offered_panel_exists_in_the_shipped_dashboard(self) -> None:
        dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
        self.assertEqual(dashboard["uid"], grafana_proxy.DEFAULT_DASHBOARD_UID)
        available = {panel["id"]: panel["title"] for panel in dashboard["panels"]}
        # Self-check: an empty dashboard would let every catalog entry through.
        self.assertTrue(available)
        for panel_id, title in grafana_proxy.PANELS:
            with self.subTest(panel=panel_id):
                self.assertIn(panel_id, available)
                self.assertEqual(available[panel_id], title)

    def test_the_dashboard_uses_exactly_one_datasource_variable(self) -> None:
        """``allowed_datasource_uids`` returns one uid because the dashboard needs one.

        If a future panel pins its own datasource, that assumption stops holding
        and the proxy would start refusing a query the panel legitimately makes -
        or, worse, someone would widen the allowlist to match. Catch it here.
        """
        dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
        variables = [
            item for item in dashboard["templating"]["list"]
            if item.get("type") == "datasource"
        ]
        self.assertEqual(len(variables), 1)
        for panel in dashboard["panels"]:
            with self.subTest(panel=panel["title"]):
                source = panel.get("datasource") or {}
                # The variable reference, never a pinned uid.
                self.assertEqual(source.get("uid"), "${datasource}")

    def test_the_query_string_is_built_only_from_console_controlled_values(self) -> None:
        config = grafana_proxy.Config(
            base_url="http://grafana.example",
            token="t",
            dashboard_uid="sites-control-plane",
            org_id=1,
            datasource_uid="",
        )
        self.assertEqual(
            grafana_proxy.panel_query(
                config, panel_id=10, from_spec="now-6h", to_spec="now", theme="light"
            ),
            "orgId=1&panelId=10&from=now-6h&to=now&theme=light",
        )

    def test_the_metric_label_for_the_proxy_is_bounded(self) -> None:
        """Grafana's asset tree must not become the label set of sites_api_requests_total."""
        from sites.api import _route_template

        self.assertEqual(_route_template("/grafana/public/build/a.js"), "/grafana/*")
        self.assertEqual(
            _route_template("/grafana/d-solo/x/y?panelId=1"), "/grafana/*"
        )


class ConsoleIntegrationTests(unittest.TestCase):
    """The console builds the panel URL; the proxy decides which URLs exist.

    Nothing at runtime joins them. If the allowlist regex is tightened or the
    route prefix renamed, the console keeps rendering iframes and every one of
    them answers 403 - a grid of empty rectangles with a status code nobody is
    looking at. Compared here because this is the only place both spellings are
    visible at once.
    """

    VIEW = ROOT / "console" / "src" / "components" / "GrafanaPanels.tsx"

    def setUp(self) -> None:
        self.source = self.VIEW.read_text(encoding="utf-8")

    def test_the_route_prefix_the_console_falls_back_to_is_the_one_served(self) -> None:
        self.assertIn(
            f'grafana.route ?? "{grafana_proxy.ROUTE_PREFIX}"', self.source
        )

    def test_the_url_the_console_builds_is_on_the_allowlist(self) -> None:
        # The console renders `${route}d-solo/${uid}?...`; strip the prefix the
        # same way the proxy does and ask the allowlist directly.
        self.assertIn("d-solo/${encodeURIComponent(uid)}", self.source)
        built = f"{grafana_proxy.ROUTE_PREFIX}d-solo/sites-control-plane"
        upstream = grafana_proxy.upstream_path(built)
        self.assertIsNotNone(upstream)
        self.assertTrue(grafana_proxy.is_allowed("GET", upstream))

    def test_the_console_never_names_a_grafana_address(self) -> None:
        """Same-origin is the whole design; an address here would undo it.

        The frame src must start from the proxy route, not from anything the
        browser could resolve on its own - the moment a Grafana host appears in
        this file, the CSP and the service-account token both stop mattering.

        🔴 The assertion is about address *shape*, not about the word "grafana"
        appearing. The first version of this guard searched for the bare
        substring ``grafana:`` and was promptly hit by this component's own
        TypeScript annotation, ``grafana: GrafanaCapability`` - a guard that
        fires on the legitimate spelling of the thing it guards. Two shapes are
        checked instead: an absolute URL, and a bare ``host:port``.
        ``test_a_real_address_would_be_caught`` proves the tightening did not
        turn a false positive into a blind spot, which is the failure mode a
        regex fix runs into next and which looks exactly like passing.
        """
        for marker in ("http://", "https://", "//grafana", "VITE_GRAFANA"):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, self.source)
        self.assertIsNone(
            HOST_PORT.search(self.source),
            "a bare host:port would resolve without going through the proxy",
        )
        self.assertIn("src={`${route}d-solo/", self.source)

    def test_a_real_address_would_be_caught(self) -> None:
        """Self-check on the regex above: from over-matching to never matching is one character."""
        for sample in (
            'src={`http://grafana.example/d-solo/`}',
            'const host = "grafana:3000";',
            'const host = "grafana.internal:3000";',
        ):
            with self.subTest(sample=sample):
                self.assertTrue(
                    sample.startswith("src={`http")
                    or HOST_PORT.search(sample) is not None
                )
        # ...and the legitimate spelling that broke the first version does not.
        self.assertIsNone(HOST_PORT.search("  grafana: GrafanaCapability;"))

    def test_the_panel_frame_is_sandboxed_to_what_a_chart_needs(self) -> None:
        self.assertIn('sandbox="allow-scripts allow-same-origin"', self.source)
        for token in ("allow-top-navigation", "allow-popups", "allow-forms"):
            with self.subTest(token=token):
                self.assertNotIn(token, self.source)


class TracePropagationTests(_EmbedTestCase):
    """The Grafana hop carries W3C trace context, and mints its own span for it.

    Correlation is the only thing that lets "an administrator opened a panel"
    and "Grafana took nine seconds" be recognised as one event later. There is
    no tracing SDK here and none is wanted; the header is a string.
    """

    INBOUND_TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"

    def upstream_traceparent(self, headers: dict | None = None) -> str:
        self.configure_grafana()
        self.call("GET", self.PANEL, headers=headers)
        (seen,) = self.seen()
        self.assertIn("traceparent", seen["headers"])
        return seen["headers"]["traceparent"]

    def test_a_request_with_no_trace_still_gets_one(self) -> None:
        sent = self.upstream_traceparent()
        parsed = grafana_proxy.parse_traceparent(sent)
        self.assertIsNotNone(parsed, sent)
        # We minted it, so we decide: sampled.
        self.assertEqual(parsed[1], "01")

    def test_an_inbound_trace_is_continued_under_a_new_span(self) -> None:
        inbound = f"00-{self.INBOUND_TRACE}-00f067aa0ba902b7-01"
        sent = self.upstream_traceparent({"traceparent": inbound})
        self.assertNotEqual(sent, inbound, "the caller's span was forwarded verbatim")
        trace, _flags = grafana_proxy.parse_traceparent(sent)
        self.assertEqual(trace, self.INBOUND_TRACE)
        self.assertNotIn("00f067aa0ba902b7", sent, "the caller's span id was reused")

    def test_an_inbound_sampling_decision_is_not_overturned(self) -> None:
        """🔴 `00` means "do not sample". Upgrading it would be a silent override.

        Nothing fails when a propagator flips this: the trace stays connected
        and the only symptom is a collector receiving a stretch of spans it was
        told to skip. That makes it exactly the kind of decision that has to be
        asserted rather than trusted.
        """
        inbound = f"00-{self.INBOUND_TRACE}-00f067aa0ba902b7-00"
        sent = self.upstream_traceparent({"traceparent": inbound})
        self.assertEqual(grafana_proxy.parse_traceparent(sent)[1], "00")

    def test_the_header_is_matched_without_regard_to_case(self) -> None:
        """The wire spelling is not a contract; matching case-insensitively is."""
        inbound = f"00-{self.INBOUND_TRACE}-00f067aa0ba902b7-01"
        sent = self.upstream_traceparent({"TraceParent": inbound})
        self.assertEqual(
            grafana_proxy.parse_traceparent(sent)[0], self.INBOUND_TRACE
        )

    def test_a_request_id_already_shaped_like_a_trace_is_adopted(self) -> None:
        sent = self.upstream_traceparent({"X-Request-Id": self.INBOUND_TRACE})
        self.assertEqual(
            grafana_proxy.parse_traceparent(sent)[0], self.INBOUND_TRACE
        )

    def test_a_malformed_inbound_trace_is_replaced_rather_than_relayed(self) -> None:
        """Relaying a broken header would poison the trace it lands in."""
        sent = self.upstream_traceparent({"traceparent": "00-not-a-trace-01"})
        parsed = grafana_proxy.parse_traceparent(sent)
        self.assertIsNotNone(parsed, sent)
        self.assertNotIn("not-a-trace", sent)


if __name__ == "__main__":
    unittest.main()
