"""Activator for dormant sites.

Following the Knative split of responsibilities, the activator handles 0→1 while the
external scaler (KEDA) handles N→0. Scale-down can be a periodic decision based on
metrics; wake-up cannot, because a request is already waiting and must be accepted now.

A cold start follows this path:

visitor → Gateway ─(HTTPRoute backendRef)→ activator
                                            │ 1. resolve the site from Host
                                            │ 2. scale replicas 0→1 exactly once
                                            │ 3. wait for an available replica
                                            └ 4. forward the original request and response

🔴 The activator is permanently on the data path; it is not inserted only at zero
replicas. Knative lets its autoscaler remove the activator from the path, which requires a
controller able to rewrite routes in real time. This implementation instead accepts one
extra hop so HTTPRoute does not have to change during scale-down or scale-up; scale-down
is performed by KEDA, so the operator does not know when it happens. Every request to an
active site passes through this Python process. That is acceptable at local-preview scale,
but higher-volume deployments should move to an activator present only at zero replicas.

🔴 WebSocket and HTTP Upgrade are unsupported. The handler deliberately returns 501
rather than pretending to forward: after an upgrade the connection is a bidirectional byte
stream that the request/response model of ``http.client`` cannot carry. A hard-forwarded
upgrade can establish a connection while passing no useful bytes, which is harder to debug
than an explicit refusal.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterable
from urllib.parse import parse_qs

from sites import exposure as exposure_backend
from sites import telemetry
from sites import tracing
from os import getenv
from sites.kube import ApiError, KubeClient
from sites.shutdown import install_stop_handler


# The single source of truth for values is in sites/exposure.py.
CONTROL_NAMESPACE = exposure_backend.CONTROL_NAMESPACE
COLLECTION_PATH = (
    f"/apis/sites.local/v1alpha1/namespaces/{CONTROL_NAMESPACE}/sitedeployments"
)
LISTEN_PORT = int(getenv("SITES_ACTIVATOR_PORT", "8090") or "8090")
# There is a single port for the operations plane. The forwarding surface uses **every** HTTP path to forward by Host, and the metrics are
# There is no place for health checks - picking a path as the operation and maintenance port is equivalent to announcing that "the path with this name will never be used".
# The site cannot be reached", and the site is the tenant's own application, and we do not have the right to retain any path to it.
ADMIN_PORT = int(getenv("SITES_ACTIVATOR_ADMIN_PORT", "9090") or "9090")
# Observation window to judge "Is this site idle?" Must >= KEDA’s polling interval (default 30s), otherwise
# There is a high probability that KEDA will read 0 in the gap between two requests and shrink the site being visited.
IDLE_WINDOW_SECONDS = float(
    getenv("SITES_ACTIVATOR_IDLE_WINDOW", "60") or "60"
)
# The routing table counts as stale when it has not been refreshed successfully for this
# long. Stale alone is not fatal: a stale but non-empty table still forwards correctly for
# every site that existed at the last refresh, so /healthz only reports 503 when there is
# nothing to forward with (never refreshed, or stale *and* empty). See AdminHandler.
ROUTE_STALE_SECONDS = float(
    getenv("SITES_ACTIVATOR_ROUTE_STALE", "60") or "60"
)
# Minimum spacing between forced refreshes (lookup misses). Without it every unknown Host
# under a wildcard domain is one LIST against the apiserver, so a scanner walking random
# subdomains turns into apiserver load. The first miss after start still refreshes at once
# (nothing has been attempted yet); a miss within this window of a fresh attempt is treated
# as a real miss - the table was just fetched, the site is genuinely not there.
FORCE_REFRESH_MIN_SECONDS = float(
    getenv("SITES_ACTIVATOR_FORCE_REFRESH_MIN_SECONDS", "1.0") or "1.0"
)
# Upper bound on requests handled at the same time. Each in-flight request may hold up to
# MAX_REQUEST_BYTES of buffered body plus a forwarding thread; without a cap the memory
# limit is reached by whoever sends enough concurrent uploads (128Mi was OOM-killed on
# 2026-08-19). Beyond the cap the activator answers 503 + Retry-After instead of dying.
MAX_INFLIGHT = int(getenv("SITES_ACTIVATOR_MAX_INFLIGHT", "64") or "64")
# How long should you wait for a cold start? 🔴 It only constrains the **wakeup** section, and does not constrain the response after wakeup - SSE,
# Large file downloads and long polling should not be truncated by this value: holdTimeout covers the entire
# When the link is connected, the slow response is cut off in the middle, and the symptom is "the download is gone halfway" without pointing to any timeout configuration).
WAKE_TIMEOUT_SECONDS = float(getenv("SITES_ACTIVATOR_WAKE_TIMEOUT", "30") or "30")
READY_POLL_SECONDS = float(getenv("SITES_ACTIVATOR_POLL_INTERVAL", "0.25") or "0.25")
ROUTE_REFRESH_SECONDS = float(getenv("SITES_ACTIVATOR_ROUTE_REFRESH", "5") or "5")
UPSTREAM_CONNECT_TIMEOUT = float(
    getenv("SITES_ACTIVATOR_UPSTREAM_TIMEOUT", "10") or "10"
)
# Endpoint propagation window: availableReplicas changed to 1 to Cilium to program the endpoint into the forwarding path
# There is a sub-second to second-level lag between them. When the connection is established, the ClusterIP connection is RST (connection refused).
# The wake-up path must pass through this window (ensure_awake waits for availableReplicas, but cannot wait for endpoint),
# Therefore, bounded retry is required if connection establishment fails; RST is returned immediately, and the actual time consumption of each retry is ≈ the backoff interval.
UPSTREAM_CONNECT_ATTEMPTS = int(
    getenv("SITES_ACTIVATOR_CONNECT_ATTEMPTS", "6") or "6"
)
UPSTREAM_CONNECT_RETRY_DELAY = float(
    getenv("SITES_ACTIVATOR_CONNECT_RETRY_DELAY", "0.4") or "0.4"
)
MAX_REQUEST_BYTES = int(
    getenv(
        "SITES_ACTIVATOR_MAX_REQUEST_BYTES",
        str(4 * 1024 * 1024),
    )
    or (4 * 1024 * 1024)
)


METRICS = telemetry.Registry()
WAKE_TOTAL = METRICS.counter(
    "sites_activator_wakes_total", "Number of cold starts", ("outcome",)
)
WAKE_SECONDS = METRICS.histogram(
    "sites_activator_wake_seconds", "Cold start time", buckets=(0.5, 1, 2, 5, 10, 30)
)
REQUEST_TOTAL = METRICS.counter(
    "sites_activator_requests_total", "Number of requests forwarded", ("outcome",)
)
TRACE_EXPORT = METRICS.counter(
    "sites_tracing_export_total",
    "OTLP spans by bounded-export outcome.",
    ("outcome",),
)
for _outcome in ("queued", "exported", "dropped_queue_full", "dropped_export_failure"):
    TRACE_EXPORT.ensure(_outcome)
# Counted separately from WAKE_TOTAL: a failed availability check is not a cold start,
# it is the apiserver being unreachable while a request is waiting.
AVAILABILITY_CHECK_FAILED = METRICS.counter(
    "sites_activator_availability_check_failures_total",
    "Availability checks that failed because the apiserver did not answer; "
    "the request was forwarded without knowing whether the site is awake",
)
for _outcome in (
    "success", "timeout", "error", "unknown_host", "upgrade_refused",
    "request_too_large", "bad_request", "overloaded",
):
    WAKE_TOTAL.ensure(_outcome)
    REQUEST_TOTAL.ensure(_outcome)
AVAILABILITY_CHECK_FAILED.ensure()
# The readiness judgement (/healthz) is not scraped by Prometheus; these two gauges carry
# the same facts so an alert can fire on a table that stopped refreshing while the
# process itself keeps answering. Both are refreshed right before /metrics renders.
ROUTE_AGE_SECONDS = METRICS.gauge(
    "sites_activator_route_age_seconds",
    "Seconds since the route table was last refreshed successfully; "
    "-1 while it has never been refreshed",
)
ROUTE_COUNT = METRICS.gauge(
    "sites_activator_route_count",
    "Number of scale-to-zero sites in the current route table",
)
# A never-refreshed table has age +inf, which the text exposition format cannot carry
# usefully into an `age > N` alert; -1 keeps the sample present and distinguishable.
NEVER_REFRESHED_AGE = -1.0


class RequestBodyError(ValueError):
    """A request body that the bounded proxy cannot safely buffer."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class TrafficWindow:
    """The number of requests for each site in the recent period is used by the external scaler to determine whether it is idle.

    Bucket by second instead of storing the timestamp of each request: the former is a constant-level memory (window seconds) under high-traffic sites.
    The latter will increase linearly with QPS, and this process must be permanent.
    """

    def __init__(self, window: float = IDLE_WINDOW_SECONDS):
        self._window = window
        self._lock = threading.Lock()
        self._buckets: dict[str, dict[int, int]] = {}

    def record(self, host: str) -> None:
        now = int(time.time())
        with self._lock:
            buckets = self._buckets.setdefault(host, {})
            buckets[now] = buckets.get(now, 0) + 1

    def count(self, host: str) -> int:
        cutoff = int(time.time()) - int(self._window)
        with self._lock:
            buckets = self._buckets.get(host)
            if buckets is None:
                return 0
            for second in [s for s in buckets if s < cutoff]:
                del buckets[second]
            if not buckets:
                # If it is empty, discard the entire host: otherwise the table will grow unbounded if the site is created and deleted.
                del self._buckets[host]
                return 0
            return sum(buckets.values())


# When forwarding one by one, the past header cannot be included. Those listed in Connection are hop-by-hop, and forwarding them as they are will make the downstream
# Think this is end-to-end semantics; Transfer-Encoding/Content-Length is recalculated by ourselves.
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


class Route:
    """All information about a site as seen by the activator."""

    __slots__ = ("host", "namespace", "service", "port")

    def __init__(self, host: str, namespace: str, service: str, port: int):
        self.host = host
        self.namespace = namespace
        self.service = service
        self.port = port

    @property
    def deployment_path(self) -> str:
        return f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{self.service}"

    @property
    def scale_path(self) -> str:
        """Scale sub-resources are used for expansion, not the Deployment itself.

        🔴 Permissions vary greatly: patch Deployment means that you can change the image, env, and securityContext——
        The activator is on the data path and directly faces public network requests. If it is compromised, it is equivalent to reducing the workload of all tenants.
        Hand it over. The scale sub-resource can only change the replica count.
        To read the status, you still need to get Deployment (availableReplicas is not in the status of scale).
        """
        return f"{self.deployment_path}/scale"

    @property
    def upstream(self) -> str:
        return f"{self.service}.{self.namespace}.svc:{self.port}"

    def __repr__(self) -> str:                          # pragma: no cover - for debugging
        return f"Route({self.host} → {self.upstream})"


def routes_from_items(items: Iterable[dict[str, Any]]) -> dict[str, Route]:
    """Organize the SiteDeployment list into host → Route.

    Only public sites with scaleToZero enabled are closed: the number of unopened site replicas is maintained by the operator and will never
    is 0, putting them in will only cover up the problem if the routing is mismatched - then it should be 404, not "easy to use"
    forward".
    """
    table: dict[str, Route] = {}
    for item in items:
        spec = item.get("spec") or {}
        if not spec.get("scaleToZero"):
            continue
        if str(spec.get("exposure", "public")) != "public":
            continue
        host = exposure_backend.backend().host_for(spec)
        if not host:
            continue
        namespace = str((item.get("status") or {}).get("namespace") or "")
        service = str(spec.get("serviceName") or "")
        if not namespace or not service:
            continue
        table[host] = Route(host, namespace, service, int(spec.get("port", 80)))
    return table


class RouteTable:
    """The routing table is refreshed periodically.

    If the refresh fails, the previous copy is retained instead of cleared: apiserver shakes all sites and turns them into 404, which is better than holding
    Forwarding an old list of a few seconds is much worse - the worst case scenario for an old list is to be forwarded to a site that has just been deleted, and there will be
    Return 404 yourself.
    """

    def __init__(
        self,
        kube: KubeClient,
        ttl: float = ROUTE_REFRESH_SECONDS,
        force_min_interval: float = FORCE_REFRESH_MIN_SECONDS,
    ):
        self._kube = kube
        self._ttl = ttl
        self._force_min_interval = force_min_interval
        self._lock = threading.Lock()
        self._routes: dict[str, Route] = {}
        self._fetched_at = 0.0
        # Last *attempt*, successful or not. Throttling forced refreshes on this rather
        # than on _fetched_at matters when the apiserver is down: every miss would
        # otherwise retry the LIST immediately because nothing ever succeeded.
        self._attempted_at = 0.0

    def refresh(self, *, force: bool = False) -> None:
        now = time.monotonic()
        with self._lock:
            if not force and now - self._fetched_at < self._ttl:
                return
            if force and now - self._attempted_at < self._force_min_interval:
                return
            self._attempted_at = now
        try:
            payload = self._kube.get(COLLECTION_PATH) or {}
        except (ApiError, RuntimeError) as exc:
            telemetry.log_exception("activator_route_refresh_failed", exc)
            return
        routes = routes_from_items(payload.get("items") or [])
        with self._lock:
            self._routes = routes
            self._fetched_at = time.monotonic()

    def start_background_refresh(self) -> threading.Thread:
        """Periodic refresh. **Cannot be saved, nor can it be triggered solely by traffic. **

        Refresh also has only two trigger points: when it is started, and when lookup misses (= there is traffic).
        And the scale-to-zero site happens to have no traffic when it is idle - the table will stop at the startup time,
        After ROUTE_STALE_SECONDS, /healthz reported 503 and, back when liveness also hit /healthz, the process was
        killed and the restart cycle crashed again. KEDA pulls metrics every 30 seconds, ScaledObject READY=False,
        half of the scale is reduced and the entire link is broken.
        (2026-08-19 Real cluster test: activator restarted 7 times, FailedGetExternalMetric ×10).
        Liveness now hits /livez (process alive only) and a stale but non-empty table stays Ready, so the
        failure mode is narrower - but a table that stops refreshing still misses every newly created site.

        KEDA pulling /scale-metrics cannot replace it: that path does not follow lookup, admin handler
        It doesn't trigger a refresh either.

        Use your own ttl instead of module constants: the test should be able to adjust the period to a smaller value, and "how long is considered stale" has nothing to do with
        "How often to brush" must be decided by the same object.
        """

        def loop() -> None:
            while True:
                time.sleep(self._ttl)
                try:
                    self.refresh()
                except Exception as exc:  # noqa: BLE001 - the loop must outlive one bad round
                    # 🔴 refresh() handles ApiError and RuntimeError; anything
                    # else used to end this thread for the life of the process.
                    # Measured: one TypeError out of kube.get and the refresher
                    # is gone, the table freezes at whatever it last held, and
                    # /healthz keeps answering 200 because a stale non-empty
                    # table is deliberately still Ready (see above). Every site
                    # created after that moment is invisible to this activator
                    # and there is no signal anywhere that says so -- the
                    # traceback went to stderr, which in the resident process is
                    # a safe_stdout proxy that drops segments under pressure.
                    telemetry.log_exception("activator_route_refresh_thread_error", exc)

        thread = threading.Thread(target=loop, name="route-refresher", daemon=True)
        thread.start()
        return thread

    def seconds_since_refresh(self) -> float:
        """How long has passed since the last **successful** refresh. This value will not be refreshed on failure."""
        with self._lock:
            if self._fetched_at == 0.0:
                return float("inf")
            return time.monotonic() - self._fetched_at

    def route_count(self) -> int:
        with self._lock:
            return len(self._routes)

    def lookup(self, host: str) -> Route | None:
        # The Host header may contain a port, or may contain a case difference.
        name = host.split(":", 1)[0].strip().lower()
        with self._lock:
            route = self._routes.get(name)
        if route is not None:
            return route
        # If you miss it, force a refresh: When the newly created site is visited for the first time, it is not in the table yet, and that is
        # The most typical moment of a cold start.
        self.refresh(force=True)
        with self._lock:
            return self._routes.get(name)


class Waker:
    """Pull the site up from zero replicas, and only issue one expansion request for concurrent requests."""

    def __init__(self, kube: KubeClient, timeout: float = WAKE_TIMEOUT_SECONDS):
        self._kube = kube
        self._timeout = timeout
        self._lock = threading.Lock()
        self._inflight: dict[str, threading.Event] = {}

    def _available(self, route: Route) -> int:
        deployment = self._kube.get(route.deployment_path) or {}
        return int((deployment.get("status") or {}).get("availableReplicas", 0))

    def _is_awake(self, route: Route) -> bool | None:
        """availableReplicas > 0, or None when the apiserver could not answer.

        🔴 Must not raise. This runs on the request path outside the wake try/except; an
        ApiError escaping _handle makes socketserver drop the connection without a
        response, so an apiserver outage turned every request - including those to sites
        that were already awake and forwardable - into an empty reply. Unknown is
        reported as None so the caller can choose to try forwarding anyway.
        """
        try:
            return self._available(route) > 0
        except Exception as exc:
            # Deliberately every exception, not the (ApiError, RuntimeError) it
            # used to be. KubeClient normalises to those two, but a promise of
            # "must not raise" that depends on a caller keeping its side of a
            # contract is not a promise -- and the cost of one escaping is a
            # dropped connection on a site that is up and serving. Unknown is
            # already a first-class answer here, so widening the catch changes
            # nothing else: the caller decides whether to forward anyway.
            AVAILABILITY_CHECK_FAILED.inc()
            telemetry.log_exception(
                "activator_available_check_failed", exc, host=route.host
            )
            return None

    def _scale_up(self, route: Route) -> None:
        self._kube.patch(route.scale_path, {"spec": {"replicas": 1}})

    def ensure_awake(self, route: Route) -> bool:
        """Make sure a copy of the site is available. No write operations occur while already awake.

        Returns True when the site is awake **or when that cannot be determined**: with the
        apiserver unreachable the only useful thing left is to attempt the forward - a
        running site answers, a dormant one fails to connect and takes the existing 502 path.
        """
        awake = self._is_awake(route)
        if awake is None or awake:
            return True

        # Solo flight: At the moment of cold start, dozens of requests often come at the same time (browsers pull resources concurrently). Send each one once
        # Patches are not just wasteful - they push the same Deployment's generation dozens of times.
        # And the operator is judging readiness according to observedGeneration.
        with self._lock:
            waiter = self._inflight.get(route.host)
            leader = waiter is None
            if leader:
                waiter = threading.Event()
                self._inflight[route.host] = waiter
        assert waiter is not None

        if not leader:
            # When the follower waits for the leader to wake up, the patch will not be sent again. When you wait, you should review it yourself:
            # The leader may have timed out.
            waiter.wait(self._timeout)
            return self._is_awake(route) is not False

        started = time.monotonic()
        try:
            self._scale_up(route)
            deadline = started + self._timeout
            while time.monotonic() < deadline:
                if self._available(route) > 0:
                    WAKE_TOTAL.inc("success")
                    WAKE_SECONDS.observe(time.monotonic() - started)
                    return True
                time.sleep(READY_POLL_SECONDS)
            WAKE_TOTAL.inc("timeout")
            telemetry.log(
                "activator_wake_timeout",
                level="warning",
                host=route.host,
                waited_seconds=round(time.monotonic() - started, 3),
            )
            return False
        except (ApiError, RuntimeError) as exc:
            WAKE_TOTAL.inc("error")
            telemetry.log_exception("activator_wake_failed", exc, host=route.host)
            return False
        finally:
            with self._lock:
                self._inflight.pop(route.host, None)
            waiter.set()


LOADING_PAGE = (
    "<!doctype html><meta charset=utf-8>"
    "<meta http-equiv=refresh content=2>"
    "<title>Starting</title>"
    "<p>The site is starting and will refresh automatically later. </p>"
).encode("utf-8")


def retrace_headers(
    headers: dict[str, str], trace_id: str, flags: str
) -> dict[str, str]:
    """Replace the caller's trace context with this hop's, in place.

    Same trace, new span: this hop is a hop. Forwarding the caller's ``traceparent``
    verbatim would make the site's work look like it happened in the caller's span, and
    the time spent waking the site - the reason anyone reads this trace at all - would
    belong to nobody.

    🔴 The old name is removed **case-insensitively**. Header names are case-insensitive
    on the wire, but this dict was built from the exact spelling the caller used: a
    ``Traceparent`` survives a lowercase ``pop`` and is then forwarded *alongside* the one
    added here, leaving the upstream to pick between two contexts. Written as a named
    function so that behaviour can be tested without a socket - it is invisible from the
    outside until the day two contexts disagree.
    """
    for name in [
        key for key in headers if key.lower() == tracing.TRACEPARENT_HEADER
    ]:
        headers.pop(name, None)
    headers.update(tracing.outbound_headers(trace_id, flags))
    return headers


def wants_html(headers: Any) -> bool:
    """Determine whether this is a browser navigation.

    Returning a page that will automatically refresh when the navigation request times out is better than letting the browser wait until the end: what the user sees is
    "Starting" instead of a white screen that circles until it times out. Non-navigation requests (XHR/Images/API) do not have this
    problem, they should get a clear status code.
    """
    accept = (headers.get("Accept") or "").lower()
    return "text/html" in accept


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # The same reason as sites-api: if not set, just declare Content-Length without sending body.
    # Permanently occupy a thread.
    timeout = 30

    routes: RouteTable
    waker: Waker
    traffic: TrafficWindow
    # Shared by every handler instance: one process-wide budget of concurrent requests.
    inflight = threading.BoundedSemaphore(MAX_INFLIGHT)

    def log_message(self, *args: Any) -> None:      # noqa: D401 - silent default log
        """By default, each request is sent to stderr; the log is sent to telemetry."""

    def send_response(self, code: int, message: str | None = None) -> None:
        self._observed_status = code
        super().send_response(code, message)

    def parse_request(self) -> bool:
        """Adopt the caller's trace context, so a wake-up joins the trace that caused it.

        The activator sits between the gateway and a site that is not running yet. Without
        this, the one hop where a request visibly stalls - waiting for a cold start - is
        the one hop missing from the trace.
        """
        parsed = super().parse_request()
        if parsed:
            self._trace_id, self._trace_flags = tracing.inbound_context(self.headers)
            tracing.bind(self._trace_id, self._trace_flags)
            self._server_span = tracing.span(
                "sites.activator.request",
                kind=2,
                trace_id=self._trace_id,
                parent_span_id=tracing.inbound_parent_span_id(self.headers),
                flags=self._trace_flags,
            )
            self._server_span.__enter__()
        return parsed

    def handle_one_request(self) -> None:
        self._trace_id = tracing.new_trace_id()
        self._trace_flags = tracing.current_flags()
        self._server_span = None
        self._observed_status = 0
        token = tracing.bind(self._trace_id, self._trace_flags)
        try:
            super().handle_one_request()
        finally:
            server_span = getattr(self, "_server_span", None)
            if server_span is not None:
                server_span.set_attribute("http.request.method", getattr(self, "command", "UNKNOWN"))
                server_span.set_attribute("http.route", "activator-forward")
                server_span.set_attribute("http.response.status_code", self._observed_status)
                if self._observed_status >= 500:
                    server_span.set_error(f"HTTP {self._observed_status}")
                server_span.__exit__(None, None, None)
            tracing.release(token)

    def _request_body(self) -> bytes | None:
        """Read a bounded, length-delimited request body before waking a site."""
        if (self.headers.get("Transfer-Encoding") or "").strip():
            raise RequestBodyError(
                411,
                "Content-Length is required; chunked request bodies are not proxied",
            )
        raw_lengths = self.headers.get_all("Content-Length", [])
        if len(raw_lengths) > 1:
            raise RequestBodyError(400, "invalid Content-Length")
        raw_length = raw_lengths[0] if raw_lengths else "0"
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise RequestBodyError(400, "invalid Content-Length") from exc
        if length < 0:
            raise RequestBodyError(400, "invalid Content-Length")
        if length > MAX_REQUEST_BYTES:
            raise RequestBodyError(
                413,
                f"request body exceeds {MAX_REQUEST_BYTES} bytes",
            )
        if length == 0:
            return None
        body = self.rfile.read(length)
        if len(body) != length:
            raise RequestBodyError(400, "incomplete request body")
        return body

    def _reply(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        retry_after: int | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if retry_after is not None:
            self.send_header("Retry-After", str(retry_after))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _handle(self) -> None:
        # The budget must wrap the whole request - reading the body is where the memory
        # goes, and the forward is where the thread is held - so acquire before either.
        # Non-blocking: queueing over-budget requests would only hide the overload until
        # the gateway's own timeout fires, with the memory already spent.
        if not self.inflight.acquire(blocking=False):
            REQUEST_TOTAL.inc("overloaded")
            self.close_connection = True
            self._reply(
                503, b"activator busy", "text/plain; charset=utf-8", retry_after=1
            )
            return
        try:
            self._serve_request()
        finally:
            self.inflight.release()

    def _serve_request(self) -> None:
        if (self.headers.get("Upgrade") or "").strip():
            REQUEST_TOTAL.inc("upgrade_refused")
            self._reply(
                501,
                b"activator does not proxy protocol upgrades",
                "text/plain; charset=utf-8",
            )
            return

        route = self.routes.lookup(self.headers.get("Host") or "")
        if route is None:
            REQUEST_TOTAL.inc("unknown_host")
            self._reply(404, b"unknown site", "text/plain; charset=utf-8")
            return

        try:
            body = self._request_body()
        except RequestBodyError as exc:
            outcome = "request_too_large" if exc.status == 413 else "bad_request"
            REQUEST_TOTAL.inc(outcome)
            self.close_connection = True
            self._reply(exc.status, exc.message.encode(), "text/plain; charset=utf-8")
            return

        # Note before waking up: The request at the moment of cold start is the strongest "this site should not continue to sleep"
        # Evidence, if recorded after wake-up, none of the wake-up timeout requests will enter the window, but the scaler will
        # Seeing an metric that remains at 0, retract the site that was just woken up.
        self.traffic.record(route.host)
        with tracing.span("sites.activator.wake", attributes={"sites.operation": "activate"}) as wake_span:
            if not self.waker.ensure_awake(route):
                wake_span.set_attribute("sites.wake.success", False)
                REQUEST_TOTAL.inc("timeout")
                if wants_html(self.headers):
                    self._reply(503, LOADING_PAGE, "text/html; charset=utf-8")
                else:
                    self._reply(
                        503, b"site is starting", "text/plain; charset=utf-8"
                    )
                return
            wake_span.set_attribute("sites.wake.success", True)

        try:
            self._forward(route, body)
        except OSError as exc:
            REQUEST_TOTAL.inc("error")
            telemetry.log_exception("activator_forward_failed", exc, host=route.host)
            self._reply(502, b"upstream unavailable", "text/plain; charset=utf-8")

    def _forward(self, route: Route, body: bytes | None = None) -> None:
        with tracing.span(
            "sites.activator.forward",
            kind=3,
            attributes={"server.address": route.upstream},
        ):
            return self._forward_traced(route, body)

    def _forward_traced(self, route: Route, body: bytes | None = None) -> None:
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in HOP_BY_HOP
        }
        retrace_headers(
            headers,
            getattr(self, "_trace_id", ""),
            getattr(self, "_trace_flags", ""),
        )
        # Retry only connection establishment. Once a request has started to be sent,
        # even a ConnectionError cannot prove that the upstream did not receive and
        # process part or all of a non-idempotent request; replaying it could duplicate
        # an order or write.
        last_error: ConnectionError | None = None
        for attempt in range(UPSTREAM_CONNECT_ATTEMPTS):
            conn = http.client.HTTPConnection(
                route.upstream, timeout=UPSTREAM_CONNECT_TIMEOUT
            )
            try:
                try:
                    conn.connect()
                except ConnectionError as exc:
                    last_error = exc
                    if attempt + 1 < UPSTREAM_CONNECT_ATTEMPTS:
                        time.sleep(UPSTREAM_CONNECT_RETRY_DELAY)
                    continue
                try:
                    conn.request(
                        self.command, self.path, body=body, headers=headers
                    )
                except ConnectionError as exc:
                    last_error = exc
                    break
                # 🔴 After getting the response, remove the timeout: the above value is used for establishing connections and sending requests.
                # Continuing to push on the response body will truncate SSE and large files.
                conn.sock.settimeout(None)
                upstream = conn.getresponse()
                self.send_response(upstream.status)
                for key, value in upstream.getheaders():
                    if key.lower() in HOP_BY_HOP:
                        continue
                    self.send_header(key, value)
                # http.client decodes an upstream chunked body before exposing
                # it here.  If that response had no Content-Length, forwarding
                # neither its Transfer-Encoding nor an explicit close leaves
                # the downstream HTTP/1.1 response without an end delimiter.
                # Closing is also the only bounded-memory framing available for
                # streams whose size is not known in advance.
                self.send_header("Connection", "close")
                self.close_connection = True
                self.end_headers()
                while chunk := upstream.read(65536):
                    self.wfile.write(chunk)
                REQUEST_TOTAL.inc("success")
                return
            finally:
                conn.close()
        assert last_error is not None
        raise last_error

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_PATCH = _handle
    do_DELETE = _handle
    do_HEAD = _handle
    do_OPTIONS = _handle


class AdminHandler(BaseHTTPRequestHandler):
    """Operation and maintenance: health, scaling metrics, Prometheus metrics.

    🔴 Separate ports from the forwarding plane, not separate paths. Each path on the forwarding plane belongs to the tenant's application.
    Retaining names like /healthz is tantamount to declaring that "the path with this name will never reach the site."

    🔴 `/scale-metrics` has a site identifier (the scaler must be asked per site), so this port is **required**
    Use NetworkPolicy to converge to the Namespace where the scaler is located - it is also a list of tenant sites.
    Prometheus's `/metrics` is the opposite: the labels there are only fixed-base enumerations.
    """

    server_version = "sites-activator"
    sys_version = ""

    def version_string(self) -> str:
        return self.server_version

    routes: RouteTable
    traffic: TrafficWindow

    def log_message(self, *args: Any) -> None:
        """The crawler comes every few seconds, and the default implementation will flood the real logs."""

    def _healthz(self) -> None:
        """Readiness: can this process forward anything right now?

        🔴 Not "was the table refreshed recently". A stale table is the *only* copy of the
        routes this process has when the apiserver is unreachable; it still forwards
        correctly for every site that existed at the last refresh, and it keeps KEDA's
        metric pulls answered. Reporting 503 on staleness pulled the endpoint at the exact
        moment it was most needed. The three 503 cases are the ones with nothing to
        forward with: never refreshed (fresh restart, apiserver still down), or stale
        *and* empty. Liveness is a separate path (/livez) on purpose - killing the process
        over staleness also kills that last good copy.
        """
        age = self.routes.seconds_since_refresh()
        count = self.routes.route_count()
        never = age == float("inf")
        stale = age > ROUTE_STALE_SECONDS
        healthy = not never and (not stale or count > 0)
        self._json(
            200 if healthy else 503,
            {
                "ok": healthy,
                "stale": stale,
                "routeAgeSeconds": None if never else round(age, 3),
                # A number only: this port is scraped by Prometheus and must not carry
                # tenant identity.
                "routeCount": count,
            },
        )

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:                       # noqa: N802 - BaseHTTPRequestHandler
        path, _, raw_query = self.path.partition("?")
        if path == "/livez":
            # Liveness: the process answers HTTP. Nothing about the apiserver belongs
            # here - see _healthz for why restarting over a stale table makes it worse.
            self._json(200, {"ok": True})
            return
        if path == "/healthz":
            self._healthz()
            return
        if path == "/scale-metrics":
            host = parse_qs(raw_query).get("host", [""])[0]
            if not host:
                self._json(400, {"error": "host is required"})
                return
            # The scaler asks "how many requests have there been during the last period" by site. 0 = Can be reduced.
            self._json(200, {"host": host, "value": self.traffic.count(host)})
            return
        if path == "/metrics":
            age = self.routes.seconds_since_refresh()
            ROUTE_AGE_SECONDS.set(
                NEVER_REFRESHED_AGE if age == float("inf") else age
            )
            ROUTE_COUNT.set(self.routes.route_count())
            body = METRICS.render().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._json(404, {"error": "not found"})


class ForwardingServer(ThreadingHTTPServer):
    """Forwarding plane whose in-flight requests are joined on close.

    ThreadingHTTPServer defaults to daemon threads, which the interpreter abandons at
    exit: a SIGTERM mid-download would cut the response. With non-daemon threads
    ``server_close()`` blocks until every handler thread has returned, so a terminating
    Pod finishes what it was forwarding. terminationGracePeriodSeconds + SIGKILL remains
    the bound for a forward that never ends.
    """

    daemon_threads = False


def build_servers(
    bind: str = "0.0.0.0", port: int = LISTEN_PORT, admin_port: int = ADMIN_PORT
) -> tuple[ForwardingServer, ThreadingHTTPServer]:
    """Bind both listeners and wire SIGTERM/SIGINT to stop them.

    Kept apart from serve() so the signal wiring is testable: serve() is the process
    entry point and never returns. Must run on the main thread (signal.signal).
    """
    main_server = ForwardingServer((bind, port), Handler)
    admin = ThreadingHTTPServer((bind, admin_port), AdminHandler)
    install_stop_handler(main_server.shutdown, admin.shutdown)
    return main_server, admin


def serve() -> None:                                # pragma: no cover - process entry
    from sites import safe_stdout

    safe_stdout.install()
    telemetry.configure("sites-activator")
    tracing.configure("sites-activator", lambda outcome, amount: TRACE_EXPORT.inc(outcome, amount=amount))
    kube = KubeClient()
    routes = RouteTable(kube)
    traffic = TrafficWindow()
    Handler.routes = routes
    Handler.waker = Waker(kube)
    Handler.traffic = traffic
    AdminHandler.routes = routes
    AdminHandler.traffic = traffic
    routes.refresh(force=True)

    routes.start_background_refresh()
    main_server, admin = build_servers()
    threading.Thread(target=admin.serve_forever, daemon=True).start()
    telemetry.log("activator_started", port=LISTEN_PORT, admin_port=ADMIN_PORT)
    main_server.serve_forever()          # returns once the stop handler fires
    # Joins in-flight forwards (daemon_threads = False) before the process exits.
    main_server.server_close()
    tracing.shutdown()
    telemetry.log("activator_stopped")


if __name__ == "__main__":                          # pragma: no cover
    serve()
