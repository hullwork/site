"""Activator regressions: routing table, single-flight wake-up, and forwarding."""

from __future__ import annotations

import http.client
import os
import pathlib
import signal
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sites import activator
from sites.validation import normalize_deploy_payload
from sites.kube import ApiError


@contextmanager
def using_backend(name: str):
    previous = os.environ.get("SITES_EXPOSURE_BACKEND")
    os.environ["SITES_EXPOSURE_BACKEND"] = name
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("SITES_EXPOSURE_BACKEND", None)
        else:
            os.environ["SITES_EXPOSURE_BACKEND"] = previous


def cr(name="web", merchant="acme", user="alice", *, stz=True, exposure="public",
       namespace="ns-a"):
    with using_backend("gateway"):
        spec = normalize_deploy_payload(
            {
                "name": name,
                "image": "example.invalid/x:v1",
                "port": 8080,
                "healthPath": "/",
                "exposure": exposure,
                # Passed explicitly rather than omitted: these cases are about
                # the activator's behaviour on each setting, so neither may ride
                # on whatever the admission default happens to be.
                "scaleToZero": stz,
            },
            merchant,
            user,
        )
    return {"spec": spec, "status": {"namespace": namespace}}


class StubKube:
    """Only implement the two methods used by activator and record write operations."""

    def __init__(self, *, available=0, items=None, fail_list=False):
        self.available = available
        self.items = items if items is not None else []
        self.fail_list = fail_list
        self.patches: list[tuple[str, dict]] = []
        self.gets = 0
        self.lists = 0
        self.lock = threading.Lock()

    def get(self, path: str):
        if path == activator.COLLECTION_PATH:
            with self.lock:
                self.lists += 1
            if self.fail_list:
                raise ApiError(503, "apiserver down")
            return {"items": self.items}
        with self.lock:
            self.gets += 1
        return {"status": {"availableReplicas": self.available}}

    def patch(self, path: str, body: dict):
        with self.lock:
            self.patches.append((path, body))
            # Simulated expansion takes effect: the replica is ready after the patch.
            self.available = int(body["spec"]["replicas"])
        return {}


class RouteTableTests(unittest.TestCase):
    def table(self, **kwargs):
        # force_min_interval=0: these cases refresh back to back on purpose.
        return activator.RouteTable(StubKube(**kwargs), ttl=0, force_min_interval=0)

    def test_only_opted_in_public_sites_are_routed(self) -> None:
        """Sites without scaleToZero enabled should not be included in the list.

        Their replica count is maintained by the operator and is never 0. Their inclusion will only cover up routing mismatches.
        Problem - it should be 404 at that time, not "can be forwarded easily".
        """
        with using_backend("gateway"):
            routes = activator.routes_from_items(
                [
                    cr(name="opted-in"),
                    cr(name="plain", stz=False),
                    cr(name="private", exposure="internal"),
                ]
            )
        self.assertEqual(
            sorted(r.service for r in routes.values()), ["opted-in"]
        )

    def test_host_matches_what_the_exposure_backend_publishes(self) -> None:
        """The key of the routing table must have the same origin as the hostname of HTTPRoute.

        If you think about it separately, the gateway presses A to divert traffic, and the activator presses B to look up the table. The result is that each request is 404.
        The configurations on both sides are correct when viewed individually.
        """
        from sites.k8s_resources import route_resources

        item = cr()
        with using_backend("gateway"):
            published = route_resources(item["spec"], "ns-a")[0]["spec"]["hostnames"][0]
            routes = activator.routes_from_items([item])
        self.assertIn(published, routes)

    def test_lookup_tolerates_port_and_case(self) -> None:
        with using_backend("gateway"):
            table = self.table(items=[cr()])
            table.refresh(force=True)
            host = next(iter(table._routes))
            self.assertIsNotNone(table.lookup(f"{host.upper()}:18090"))

    def test_refresh_failure_keeps_the_previous_table(self) -> None:
        """Apiserver shake cannot turn all sites into 404.

        The worst case scenario is that the old table will be transferred to a newly deleted site, where it will respond with 404; if it is cleared, the entire site will be instantly unavailable.
        """
        kube = StubKube(items=[cr()])
        with using_backend("gateway"):
            table = activator.RouteTable(kube, ttl=0, force_min_interval=0)
            table.refresh(force=True)
            self.assertEqual(len(table._routes), 1)
            kube.fail_list = True
            table.refresh(force=True)
        self.assertEqual(kube.lists, 2, "The second refresh must really have been attempted")
        self.assertEqual(len(table._routes), 1, "The table was cleared when the refresh failed")


class ForceRefreshThrottleTests(unittest.TestCase):
    """A lookup miss forces a refresh; misses must not be an unmetered LIST each.

    Under a wildcard domain every unknown Host reaches the activator, so without spacing
    a scanner walking random subdomains is apiserver load at request rate.
    """

    def table(self, interval):
        kube = StubKube(items=[])
        # ttl large: the background/ttl path must not be what refreshes here.
        return activator.RouteTable(kube, ttl=3600, force_min_interval=interval), kube

    def test_consecutive_misses_trigger_one_list(self) -> None:
        table, kube = self.table(interval=1.0)
        with using_backend("gateway"):
            self.assertIsNone(table.lookup("a.unknown.example"))
            self.assertIsNone(table.lookup("b.unknown.example"))
        self.assertEqual(kube.lists, 1, f"Two misses cost {kube.lists} LISTs")

    def test_a_miss_after_the_interval_refreshes_again(self) -> None:
        """The throttle is spacing, not a switch: a newly created site still gets picked up."""
        table, kube = self.table(interval=0.05)
        with using_backend("gateway"):
            table.lookup("a.unknown.example")
            time.sleep(0.1)
            kube.items = [cr(name="late")]
            found = table.lookup(next(iter(activator.routes_from_items(kube.items))))
        self.assertEqual(kube.lists, 2)
        self.assertIsNotNone(found)

    def test_failed_attempts_count_toward_the_spacing(self) -> None:
        """With the apiserver down nothing ever succeeds; throttling on the last *success*
        would retry the LIST on every single miss - the exact moment the apiserver needs
        it least."""
        table, kube = self.table(interval=1.0)
        kube.fail_list = True
        with using_backend("gateway"):
            table.lookup("a.unknown.example")
            table.lookup("b.unknown.example")
        self.assertEqual(kube.lists, 1)


class BackgroundRefreshTests(unittest.TestCase):
    """🔴 Under an idle scale-to-zero site, the routing table must be kept fresh by itself.

    The consequences of real cluster testing (2026-08-19): refresh originally only consisted of "start" and "lookup miss"
    Triggered, and there happens to be no traffic when the STZ site is idle - the table is stuck at the start time, ROUTE_STALE_SECONDS
    After that, /healthz executes the death sentence and liveness kills the process. The restart cycle hits KEDA's index pull every 30 seconds.
    ScaledObject READY=False, half of the scale is reduced and the entire link is broken (the activator is restarted 7 times,
    FailedGetExternalMetric ×10).

    This set of use cases deliberately does not adjust the lookup even once: that is the definition of idle, and it is also the only one in the original version that has not been adjusted.
    Overridden input.
    """

    def table(self, ttl):
        kube = StubKube(items=[])
        with using_backend("gateway"):
            return activator.RouteTable(kube, ttl=ttl), kube

    def test_table_stays_fresh_with_zero_traffic(self) -> None:
        table, kube = self.table(ttl=0.05)
        table.refresh(force=True)
        table.start_background_refresh()
        time.sleep(0.4)
        self.assertLess(
            table.seconds_since_refresh(),
            0.2,
            "Routing tables become stale under zero traffic - healthz will sentence itself to death",
        )

    def test_it_actually_refetches_rather_than_touching_a_timestamp(self) -> None:
        """What you brush must be real data. If you only update the timestamp, healthz will be green, but the table is actually old.
        ——That’s worse than a death sentence: the site is still forwarded after being deleted, and the new site will always get 404."""
        # 🔴 The entire section must be in the gateway backend: the refresh thread runs in the background, and exiting using_backend
        # What it reads is the default backend, host_for returns None, and the table is always empty - that is for test scaffolding
        # The problem is not the behavior being tested.
        with using_backend("gateway"):
            table, kube = self.table(ttl=0.05)
            kube.items = []
            table.refresh(force=True)
            table.start_background_refresh()
            time.sleep(0.3)
            kube.items = [cr(name="late-arrival")]
            time.sleep(0.3)
            routes = list(table._routes.values())
        self.assertTrue(
            any(route.service == "late-arrival" for route in routes),
            "Sites built later were not picked up by periodic refreshes",
        )

    def test_one_unexpected_error_does_not_end_the_refresher_for_good(self) -> None:
        """🔴 refresh() handles ApiError and RuntimeError; anything else killed the thread.

        Measured before the fix: one TypeError out of ``kube.get`` and the
        thread is gone for the life of the process, ``kube.get`` is never
        called again, and the table freezes at whatever it last held.  Nothing
        reports it -- a stale but non-empty table is deliberately still Ready
        on /healthz (see the class docstring), and the traceback goes to
        stderr, which in the resident process is a ``safe_stdout`` proxy that
        drops segments under pressure.  Every site created after that instant
        is invisible to this activator.
        """

        class OnceBroken:
            def __init__(self) -> None:
                self.calls = 0

            def get(self, path: str):
                self.calls += 1
                if self.calls == 1:
                    # Deliberately neither ApiError nor RuntimeError.
                    raise TypeError("apiserver returned something unexpected")
                return {"items": [cr(name="after-the-error")]}

        with using_backend("gateway"):
            kube = OnceBroken()
            table = activator.RouteTable(kube, ttl=0.05)
            thread = table.start_background_refresh()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and kube.calls < 2:
                time.sleep(0.02)
            routes = list(table._routes.values())
        # Red if the thread dies: calls stops at 1 and is_alive() is False.
        self.assertTrue(thread.is_alive(), "the refresher thread did not survive one bad round")
        self.assertGreaterEqual(kube.calls, 2, "the refresher never ran again after the error")
        # Red if the loop survives but stops actually refetching -- surviving
        # without recovering would satisfy the two assertions above alone.
        self.assertTrue(
            any(route.service == "after-the-error" for route in routes),
            "the refresher survived but never picked the table back up",
        )

    def test_the_refresh_period_leaves_room_before_the_health_deadline(self) -> None:
        """Cross-constant contract: The refresh period must be much smaller than the death threshold.

        When the two are close to each other, occasionally a refresh fails (apiserver shakes, refresh will retain the old table and
        return) is enough to cause healthz to die and liveness to restart the process - and that is an otherwise harmless
        Jitter.
        """
        self.assertLess(
            activator.ROUTE_REFRESH_SECONDS * 3,
            activator.ROUTE_STALE_SECONDS,
            "The refresh cycle is too close to the death threshold, and a failed refresh will trigger a restart.",
        )


class WakerTests(unittest.TestCase):
    def route(self):
        return activator.Route("web.d.example", "ns-a", "web", 8080)

    def test_awake_site_costs_no_write(self) -> None:
        kube = StubKube(available=1)
        self.assertTrue(activator.Waker(kube).ensure_awake(self.route()))
        self.assertEqual(kube.patches, [], "Sites that are already awake should not generate any write operations")

    def test_scales_up_from_zero(self) -> None:
        kube = StubKube(available=0)
        self.assertTrue(activator.Waker(kube).ensure_awake(self.route()))
        self.assertEqual(len(kube.patches), 1)
        path, body = kube.patches[0]
        self.assertEqual(body, {"spec": {"replicas": 1}})
        self.assertTrue(
            path.endswith("/scale"),
            "Expansion must use the scale sub-resource - the patch Deployment ontology means that the image can be changed."
            "The activator faces public network requests directly.",
        )

    def test_concurrent_requests_scale_up_only_once(self) -> None:
        """🔴 Flying solo. At the moment of cold start, the browser will pull dozens of resources concurrently.

        Patching once per request is not just wasteful: they patch the same Deployment's generation
        Pushed dozens of times, and the operator is ready according to observedGeneration.
        """
        class SlowKube(StubKube):
            def patch(self, path, body):
                time.sleep(0.05)          # Make the concurrency window really exist
                return super().patch(path, body)

        kube = SlowKube(available=0)
        waker = activator.Waker(kube)
        route = self.route()
        results: list[bool] = []
        lock = threading.Lock()

        def hit():
            ok = waker.ensure_awake(route)
            with lock:
                results.append(ok)

        threads = [threading.Thread(target=hit) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(len(kube.patches), 1, f"Sent {len(kube.patches)} secondary expansion")
        self.assertTrue(all(results), "There are requests but the site is not up yet")

    def test_timeout_returns_false_without_raising(self) -> None:
        class NeverReady(StubKube):
            def patch(self, path, body):
                self.patches.append((path, body))   # Do not change available
                return {}

        kube = NeverReady(available=0)
        waker = activator.Waker(kube, timeout=0.2)
        self.assertFalse(waker.ensure_awake(self.route()))

    def test_scale_failure_is_reported_not_raised(self) -> None:
        class Rejecting(StubKube):
            def patch(self, path, body):
                raise ApiError(403, "forbidden")

        waker = activator.Waker(Rejecting(available=0), timeout=1)
        self.assertFalse(waker.ensure_awake(self.route()))

    def test_an_unanswerable_availability_check_is_unknown_not_an_exception(self) -> None:
        """🔴 The pre-wake check ran outside every try: an unreachable apiserver escaped
        _handle as RuntimeError, socketserver dropped the connection, and sites that were
        awake and forwardable answered with nothing. Unknown must mean "try forwarding"."""
        class Unreachable(StubKube):
            def get(self, path):
                raise RuntimeError("apiserver unreachable")

        kube = Unreachable(available=0)
        before = activator.AVAILABILITY_CHECK_FAILED._values.get((), 0.0)
        self.assertTrue(activator.Waker(kube).ensure_awake(self.route()))
        self.assertEqual(kube.patches, [], "Do not scale on a guess; nothing is known")
        self.assertEqual(
            activator.AVAILABILITY_CHECK_FAILED._values.get((), 0.0), before + 1
        )

    def test_a_follower_whose_recheck_fails_also_tries_forwarding(self) -> None:
        """The follower re-checks after the leader; that call is the second escape point."""
        class DiesDuringWake(StubKube):
            def __init__(self):
                super().__init__(available=0)
                self.deployment_gets = 0

            def get(self, path):
                if path == activator.COLLECTION_PATH:
                    return super().get(path)
                with self.lock:
                    self.deployment_gets += 1
                    n = self.deployment_gets
                if n <= 2:            # leader's and follower's pre-checks: dormant
                    return {"status": {"availableReplicas": 0}}
                raise ApiError(503, "apiserver down")   # leader's poll, follower's re-check

            def patch(self, path, body):
                time.sleep(0.1)       # keep the follower waiting on the leader
                return super().patch(path, body)

        kube = DiesDuringWake()
        waker = activator.Waker(kube, timeout=1)
        route = self.route()
        results: list[bool] = []
        leader = threading.Thread(target=lambda: results.append(waker.ensure_awake(route)))
        leader.start()
        time.sleep(0.03)
        follower_result = waker.ensure_awake(route)
        leader.join(5)
        self.assertGreaterEqual(kube.deployment_gets, 4, "The follower path (re-check) was not taken")
        self.assertTrue(follower_result, "Follower must not raise or report dormant on unknown")


class _Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen: list[tuple[str, str, bytes]] = []

    def log_message(self, *args):
        pass

    # /block holds the response until the test opens the gate: how a test creates a
    # request that is genuinely in flight.
    gate = threading.Event()

    def _reply(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        type(self).seen.append((self.command, self.path, body))
        if self.path == "/block":
            type(self).gate.wait(10)
        payload = b"upstream-ok:" + body
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        if self.path != "/close-delimited":
            self.send_header("Content-Length", str(len(payload)))
        else:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.send_header("X-Upstream", "yes")
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _reply
    do_POST = _reply


class ForwardingTests(unittest.TestCase):
    """End-to-end: really create an activator and an upstream, and use a real socket."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
        cls.upstream_port = cls.upstream.server_address[1]
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.upstream.shutdown()
        cls.upstream.server_close()

    def setUp(self) -> None:
        _Upstream.seen = []
        _Upstream.gate = threading.Event()
        self.addCleanup(_Upstream.gate.set)
        # The semaphore is a class attribute shared by every test; restore it.
        self.addCleanup(
            setattr, activator.Handler, "inflight", activator.Handler.inflight
        )
        host = "web.digest.example"

        # Route.upstream spells svc DNS, which refers to loopback in the test. Use duck typing instead of inheritance:
        # Route has __slots__, subclassing just to change one property is not worth it.
        class LocalRoute:
            deployment_path = "/apis/apps/v1/namespaces/ns-a/deployments/web"
            scale_path = deployment_path + "/scale"

            def __init__(self, host: str, port: int):
                self.host = host
                self.upstream = f"127.0.0.1:{port}"

        route = LocalRoute(host, self.upstream_port)

        class FixedTable:
            def lookup(self, value):
                return route if value.split(":")[0] == host else None

        class AlwaysAwake:
            def ensure_awake(self, _route):
                return True

        activator.Handler.routes = FixedTable()
        activator.Handler.waker = AlwaysAwake()
        activator.Handler.traffic = activator.TrafficWindow()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), activator.Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.host = host

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def request(self, method="GET", path="/", body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers={"Host": self.host, **(headers or {})})
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()

    def test_get_is_proxied_with_response_headers(self) -> None:
        status, headers, body = self.request(path="/hello")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"upstream-ok:")
        self.assertEqual(headers.get("X-Upstream"), "yes", "The upstream response header needs to be brought back")
        self.assertEqual(_Upstream.seen[0][1], "/hello", "The path must be forwarded as is")

    def test_request_body_is_forwarded(self) -> None:
        status, _, body = self.request(
            "POST", "/submit", body=b"payload",
            headers={"Content-Length": "7"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, b"upstream-ok:payload")
        self.assertEqual(_Upstream.seen[0][2], b"payload")

    def test_an_oversized_request_body_is_rejected_before_waking(self) -> None:
        status, headers, body = self.request(
            "POST", "/upload", body=b"x",
            headers={"Content-Length": str(activator.MAX_REQUEST_BYTES + 1)},
        )
        self.assertEqual(status, 413)
        self.assertIn(b"request body exceeds", body)
        self.assertEqual(headers.get("Connection"), "close")
        self.assertEqual(_Upstream.seen, [])

    def test_an_invalid_content_length_is_rejected(self) -> None:
        status, _, body = self.request(
            "POST", "/submit", body=b"x",
            headers={"Content-Length": "not-a-number"},
        )
        self.assertEqual(status, 400)
        self.assertIn(b"invalid Content-Length", body)
        self.assertEqual(_Upstream.seen, [])

    def test_chunked_request_bodies_are_rejected_explicitly(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest("POST", "/stream", skip_host=True)
            conn.putheader("Host", self.host)
            conn.putheader("Transfer-Encoding", "chunked")
            conn.endheaders()
            response = conn.getresponse()
            body = response.read()
            self.assertEqual(response.status, 411)
            self.assertIn(b"Content-Length is required", body)
        finally:
            conn.close()

    def test_close_delimited_upstream_terminates_the_downstream_response(self) -> None:
        status, headers, body = self.request(path="/close-delimited")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"upstream-ok:")
        self.assertEqual(headers.get("Connection"), "close")

    def test_unknown_host_is_404(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("GET", "/", headers={"Host": "nobody.example"})
            self.assertEqual(conn.getresponse().status, 404)
        finally:
            conn.close()

    def test_protocol_upgrade_is_refused_not_half_proxied(self) -> None:
        """WebSocket must be explicitly rejected.

        The result of pretending to forward is that the connection is established successfully and not a single byte of data is passed - much harder to check than 501.
        """
        status, _, _ = self.request(headers={"Upgrade": "websocket", "Connection": "Upgrade"})
        self.assertEqual(status, 501)

    def test_cold_start_timeout_serves_a_refreshing_page_to_browsers(self) -> None:
        class NeverAwake:
            def ensure_awake(self, _route):
                return False

        activator.Handler.waker = NeverAwake()
        status, headers, body = self.request(headers={"Accept": "text/html"})
        self.assertEqual(status, 503)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn(b"http-equiv=refresh", body, "The navigation request should get the page that will refresh automatically.")

    def test_cold_start_timeout_is_plain_for_non_navigation(self) -> None:
        """XHR/images/API shouldn't get a piece of HTML - they want a status code."""
        class NeverAwake:
            def ensure_awake(self, _route):
                return False

        activator.Handler.waker = NeverAwake()
        status, headers, body = self.request(headers={"Accept": "application/json"})
        self.assertEqual(status, 503)
        self.assertNotIn("text/html", headers.get("Content-Type", ""))

    class _ApiserverDown:
        """Every Kubernetes call fails; only the real Waker's error handling is under test.

        The failure type is a parameter because it used to be `RuntimeError`
        alone -- the one type the Waker's guard already caught -- so this
        fixture agreed with the guard by construction and the P0 below could
        not tell a working guard from a broken one. KubeClient normalises to
        RuntimeError now (see test_kube_error_normalisation), and this keeps
        the Waker honest if that ever slips.
        """

        def __init__(self, error=None):
            self._error = error or RuntimeError("apiserver unreachable")

        def get(self, path):
            raise self._error

        def patch(self, path, body):
            raise self._error

    def test_apiserver_outage_still_forwards_to_an_awake_site(self) -> None:
        """🔴 P0: with the apiserver down, a request to a running STZ site must get the
        site's response, not a dropped connection (RemoteDisconnected)."""
        import http.client as _http
        import ssl as _ssl

        for name, error in (
            ("runtime", RuntimeError("apiserver unreachable")),
            ("api", activator.ApiError(503, "unavailable")),
            ("reset", ConnectionResetError("reset by peer")),
            ("short read", _http.IncompleteRead(b"ab", 100)),
            ("tls", _ssl.SSLError("record layer failure")),
            ("non-json", ValueError("Expecting value: line 1 column 1")),
        ):
            with self.subTest(failure=name):
                activator.Handler.waker = activator.Waker(
                    self._ApiserverDown(error)
                )
                status, _, body = self.request(path="/still-up")
                self.assertEqual(status, 200)
                self.assertEqual(body, b"upstream-ok:")

    def test_apiserver_outage_with_a_dormant_site_is_502_not_an_empty_reply(self) -> None:
        """Both down: the answer is a real 502 (the existing forward-failure path), which
        the gateway can render, rather than a half-open connection."""
        class Dormant:
            host = self.host
            upstream = "127.0.0.1:1"        # nothing listens on port 1
            deployment_path = "/apis/apps/v1/namespaces/ns-a/deployments/web"
            scale_path = deployment_path + "/scale"

        dormant = Dormant()

        class Table:
            def lookup(self, _value):
                return dormant

        activator.Handler.routes = Table()
        activator.Handler.waker = activator.Waker(self._ApiserverDown())
        self.addCleanup(
            setattr, activator, "UPSTREAM_CONNECT_RETRY_DELAY",
            activator.UPSTREAM_CONNECT_RETRY_DELAY,
        )
        activator.UPSTREAM_CONNECT_RETRY_DELAY = 0
        status, _, body = self.request(path="/asleep")
        self.assertEqual(status, 502)
        self.assertEqual(body, b"upstream unavailable")

    def test_requests_over_the_inflight_budget_get_503_not_a_thread(self) -> None:
        """Each in-flight request may hold a 4MiB body and a thread; the budget is what
        stands between a burst of uploads and the memory limit (OOM on 2026-08-19)."""
        activator.Handler.inflight = threading.BoundedSemaphore(1)
        first: list = []
        t = threading.Thread(target=lambda: first.append(self.request(path="/block")))
        t.start()
        deadline = time.monotonic() + 5
        while not any(p == "/block" for _, p, _ in _Upstream.seen):
            self.assertLess(time.monotonic(), deadline, "first request never reached upstream")
            time.sleep(0.01)
        status, headers, body = self.request(path="/second")
        self.assertEqual(status, 503)
        self.assertEqual(body, b"activator busy")
        self.assertEqual(headers.get("Retry-After"), "1")
        self.assertEqual(headers.get("Connection"), "close")
        self.assertNotIn("/second", [p for _, p, _ in _Upstream.seen], "Over budget must not reach upstream")
        _Upstream.gate.set()
        t.join(5)
        self.assertEqual(first[0][0], 200, "The in-flight request must finish normally")
        # Budget released: the next request goes through again.
        status, _, _ = self.request(path="/third")
        self.assertEqual(status, 200)


class ConnectRetryTests(unittest.TestCase):
    """The first forward after waking up will hit the endpoint propagation window.

    ensure_awake is waiting for availableReplicas, and it changes to 1 to Cilium to program the endpoint
    There is sub-second to second-level lag between forwarding paths - during that time the connection to ClusterIP is RSTed.
    The first test of the real cluster (2026-08-19) was stepped on. In the single test, the upstream has been monitoring, so naturally it cannot be measured.
    """

    def setUp(self) -> None:
        self.attempts = 0
        self.original = activator.http.client.HTTPConnection
        self.addCleanup(
            setattr, activator.http.client, "HTTPConnection", self.original
        )

    def handler(self):
        handler = activator.Handler.__new__(activator.Handler)
        handler.headers = {}
        handler.command = "POST"          # Non-idempotent: replaying it has consequences
        handler.path = "/orders"
        return handler

    def route(self):
        return type("R", (), {"upstream": "127.0.0.1:1", "host": "x"})()

    def install(self, *, fail_on: str):
        """Fail during connection, request send, or response processing."""
        outer = self

        class Flaky:
            # The sock is provided by the following class attribute: _forward on the success path will call settimeout.
            def __init__(self, *args, **kwargs):
                pass

            def connect(self):
                outer.attempts += 1
                if fail_on == "connect":
                    raise ConnectionRefusedError(111, "Connection refused")

            def request(self, *args, **kwargs):
                if fail_on == "request":
                    raise ConnectionResetError(104, "connection reset during send")

            def getresponse(self):
                raise ConnectionResetError(104, "Connection reset by peer")

            def close(self):
                pass

        # sock.settimeout will be called on the success path.
        Flaky.sock = type("S", (), {"settimeout": lambda self, _v: None})()
        activator.http.client.HTTPConnection = Flaky

    def test_connect_failures_are_retried_up_to_a_bound(self) -> None:
        self.install(fail_on="connect")
        with self.assertRaises(ConnectionRefusedError):
            self.handler()._forward(self.route())
        self.assertEqual(
            self.attempts,
            activator.UPSTREAM_CONNECT_ATTEMPTS,
            "If the connection fails, try again until the budget is exhausted, and cannot exceed it.",
        )
        self.assertGreater(activator.UPSTREAM_CONNECT_ATTEMPTS, 1, "Budget for at least 1 retry")

    def test_a_request_send_failure_is_never_retried(self) -> None:
        self.install(fail_on="request")
        with self.assertRaises(ConnectionResetError):
            self.handler()._forward(self.route())
        self.assertEqual(self.attempts, 1)

    def test_a_request_that_reached_the_upstream_is_never_replayed(self) -> None:
        """🔴 Only retry during the connection establishment phase.

        Once the request is sent, the upstream may have processed it (POST is deliberately used here); subsequent failures will be replayed
        Just repeat the order. This assertion pins the retry boundary to "before the response starts".
        """
        self.install(fail_on="response")
        with self.assertRaises(ConnectionResetError):
            self.handler()._forward(self.route())
        self.assertEqual(self.attempts, 1)


class TrafficWindowTests(unittest.TestCase):
    def test_counts_within_the_window(self) -> None:
        window = activator.TrafficWindow(window=60)
        for _ in range(3):
            window.record("a.example")
        self.assertEqual(window.count("a.example"), 3)
        self.assertEqual(window.count("b.example"), 0, "Sites that have not been visited are 0")

    def test_old_buckets_fall_out(self) -> None:
        window = activator.TrafficWindow(window=0)
        window.record("a.example")
        time.sleep(1.1)
        self.assertEqual(window.count("a.example"), 0, "Requests outside the window should no longer be counted")

    def test_idle_hosts_are_forgotten(self) -> None:
        """Sites that are created and deleted cannot allow this table to grow unbounded - this is a long-term process."""
        window = activator.TrafficWindow(window=0)
        window.record("gone.example")
        time.sleep(1.1)
        window.count("gone.example")
        self.assertNotIn("gone.example", window._buckets)


class AdminFaceTests(unittest.TestCase):
    """Operation and maintenance: health criteria and scaling metrics."""

    class _Routes:
        def __init__(self, age, count):
            self.age = age
            self.count = count

        def seconds_since_refresh(self):
            return self.age

        def route_count(self):
            return self.count

    def serve(self, age=1.0, traffic=None, count=3):
        activator.AdminHandler.routes = self._Routes(age, count)
        activator.AdminHandler.traffic = traffic or activator.TrafficWindow()
        server = ThreadingHTTPServer(("127.0.0.1", 0), activator.AdminHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        # unittest runs cleanups in reverse order: stop serving before closing.
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return server.server_address[1]

    def get(self, port, path):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            import json as _json

            body = resp.read()
            return resp.status, (_json.loads(body) if body.startswith(b"{") else body)
        finally:
            conn.close()

    def test_healthz_is_green_when_routes_are_fresh(self) -> None:
        status, body = self.get(self.serve(age=1.0), "/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertFalse(body["stale"])
        self.assertEqual(body["routeCount"], 3)

    def test_healthz_is_red_before_the_first_successful_refresh(self) -> None:
        """A fresh process with no table forwards nothing; it must not receive traffic."""
        status, body = self.get(self.serve(age=float("inf"), count=0), "/healthz")
        self.assertEqual(status, 503)
        self.assertFalse(body["ok"])
        self.assertIsNone(body["routeAgeSeconds"])

    def test_a_stale_but_populated_table_stays_ready(self) -> None:
        """🔴 P0: readiness on staleness pulled the endpoint exactly when the apiserver
        was down - the stale table was the only copy that could still forward."""
        status, body = self.get(
            self.serve(age=activator.ROUTE_STALE_SECONDS + 10, count=3), "/healthz"
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["stale"], "Staleness must still be visible in the body")

    def test_a_stale_and_empty_table_is_not_ready(self) -> None:
        """Nothing to forward with: every request would be 404 while the process is fine."""
        status, body = self.get(
            self.serve(age=activator.ROUTE_STALE_SECONDS + 10, count=0), "/healthz"
        )
        self.assertEqual(status, 503)
        self.assertFalse(body["ok"])

    def test_livez_ignores_the_route_table(self) -> None:
        """Liveness must not be bound to the apiserver: restarting the process over a stale
        table discards the only good copy and comes back with none (_fetched_at = 0)."""
        status, body = self.get(self.serve(age=float("inf"), count=0), "/livez")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_scale_metrics_reports_per_site_traffic(self) -> None:
        window = activator.TrafficWindow(window=60)
        window.record("web.d.example")
        window.record("web.d.example")
        port = self.serve(traffic=window)
        status, body = self.get(port, "/scale-metrics?host=web.d.example")
        self.assertEqual(status, 200)
        self.assertEqual(body["value"], 2)

    def test_scale_metrics_reports_zero_for_an_idle_site(self) -> None:
        """0 is the basis for the scaler to scale down - if you ask a site with no traffic, it must return 0 cleanly.
        Instead of 404 or error."""
        status, body = self.get(self.serve(), "/scale-metrics?host=idle.example")
        self.assertEqual(status, 200)
        self.assertEqual(body["value"], 0)

    def test_scale_metrics_requires_a_host(self) -> None:
        status, _ = self.get(self.serve(), "/scale-metrics")
        self.assertEqual(status, 400)

    def test_prometheus_metrics_carry_no_site_identity(self) -> None:
        """This endpoint is not authenticated, and labeling by site is both a tenant list leak and a base explosion."""
        window = activator.TrafficWindow(window=60)
        window.record("secret-tenant-site.example")
        status, body = self.get(self.serve(traffic=window), "/metrics")
        self.assertEqual(status, 200)
        self.assertNotIn(b"secret-tenant-site", body)
        self.assertIn(b"sites_activator_requests_total", body)

    def test_prometheus_metrics_expose_route_table_age_and_size(self) -> None:
        """/healthz is not scraped; the alert on a table that stopped refreshing reads these."""
        status, body = self.get(self.serve(age=42.5, count=7), "/metrics")
        self.assertEqual(status, 200)
        self.assertIn(b"sites_activator_route_age_seconds 42.5", body)
        self.assertIn(b"sites_activator_route_count 7", body)
        # A table that has never been refreshed must still emit a sample, not vanish.
        status, body = self.get(self.serve(age=float("inf"), count=0), "/metrics")
        self.assertEqual(status, 200)
        self.assertIn(b"sites_activator_route_age_seconds -1", body)


class ServerLifecycleTests(unittest.TestCase):
    """SIGTERM as PID 1 used to be dropped; every rollout waited out the grace period."""

    def setUp(self) -> None:
        self._previous = {
            signum: signal.getsignal(signum)
            for signum in (signal.SIGTERM, signal.SIGINT)
        }

    def tearDown(self) -> None:
        for signum, previous in self._previous.items():
            signal.signal(signum, previous)

    def test_sigterm_stops_both_listeners(self) -> None:
        main_server, admin = activator.build_servers("127.0.0.1", 0, 0)
        returned = {"main": threading.Event(), "admin": threading.Event()}

        def run(server, key):
            server.serve_forever(poll_interval=0.05)
            returned[key].set()

        threading.Thread(target=run, args=(main_server, "main"), daemon=True).start()
        threading.Thread(target=run, args=(admin, "admin"), daemon=True).start()
        time.sleep(0.1)                    # both loops must be inside serve_forever
        os.kill(os.getpid(), signal.SIGTERM)
        self.assertTrue(returned["main"].wait(3.0), "forwarding serve_forever must return")
        self.assertTrue(returned["admin"].wait(3.0), "admin serve_forever must return")
        main_server.server_close()
        admin.server_close()

    def test_close_waits_for_in_flight_forwards(self) -> None:
        """A terminating Pod finishes the response it is sending instead of cutting it."""
        self.assertFalse(activator.ForwardingServer.daemon_threads)
        _Upstream.gate = threading.Event()
        self.addCleanup(_Upstream.gate.set)
        up = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
        threading.Thread(target=up.serve_forever, daemon=True).start()
        self.addCleanup(up.server_close)
        self.addCleanup(up.shutdown)

        class Route:
            host = "web.digest.example"
            upstream = f"127.0.0.1:{up.server_address[1]}"

        route = Route()
        activator.Handler.routes = type("T", (), {"lookup": lambda self, _v: route})()
        activator.Handler.waker = type("W", (), {"ensure_awake": lambda self, _r: True})()
        activator.Handler.traffic = activator.TrafficWindow()
        server = activator.ForwardingServer(("127.0.0.1", 0), activator.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()

        result: list = []

        def client():
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            conn.request("GET", "/block", headers={"Host": route.host})
            resp = conn.getresponse()
            result.append((resp.status, resp.read()))
            conn.close()

        caller = threading.Thread(target=client, daemon=True)
        caller.start()
        deadline = time.monotonic() + 5
        while not any(p == "/block" for _, p, _ in _Upstream.seen):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        server.shutdown()
        closed = threading.Event()
        threading.Thread(
            target=lambda: (server.server_close(), closed.set()), daemon=True
        ).start()
        time.sleep(0.2)
        self.assertFalse(closed.is_set(), "server_close returned while a forward was in flight")
        _Upstream.gate.set()
        self.assertTrue(closed.wait(5.0))
        # server_close() returning means the handler thread was joined; it says nothing
        # about this test's own client thread having read the response and recorded it.
        # Without this join the assertion below races the client and fails as an empty
        # list - intermittently, and more often when the process is busy.
        caller.join(5.0)
        self.assertFalse(caller.is_alive(), "the client thread never finished")
        self.assertEqual(result, [(200, b"upstream-ok:")])


class TrafficRecordingTests(unittest.TestCase):
    """The forwarding plane must record traffic before waking up."""

    def test_cold_start_request_is_counted_even_when_wake_times_out(self) -> None:
        """If recorded after waking up, none of the wake-up timeout requests will enter the window, and the scaler will see a
        The metric that continues to be 0 retracts the site that was just woken up."""
        source = (
            pathlib.Path(activator.__file__).read_text(encoding="utf-8")
        )
        record_at = source.index("self.traffic.record(route.host)")
        wake_at = source.index("if not self.waker.ensure_awake(route):")
        self.assertLess(record_at, wake_at, "Traffic must be logged before waking up")


if __name__ == "__main__":
    unittest.main()
