"""Sites HTTP API: authenticate requests, create resources, and return asynchronously.

The handler composes endpoint mixins, applies the shared exception mappings from
``api_errors``, and serves the admin console. Deployment reconciliation happens in the
operator; API responses reflect accepted intent and observed control-plane state.
"""
from __future__ import annotations

import hmac
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from os import getenv
from sites.kube import KubeClient
from sites.storage import Store, StorageError, store_from_env
from sites import shutdown
from sites import telemetry
from sites import identity
from sites import admission
from sites import oidc
from sites import tracing
from sites.api_errors import ErrorResponseTable as _ErrorResponseTable
from sites.serializers import capabilities_response as _capabilities_response
from sites.scaffolds import scaffold_catalog as _scaffold_catalog

# This module is the combination root, and downstream (sync, etc.) uses `from sites import api` to back reference metrics.
# When `python -m sites.api` is executed as __main__, there is no "sites.api" in sys.modules,
# That back reference will pull up the entire file and cause a circular import - the container command is exactly
# -m form (2026-08-22 three-piece CrashLoop record). Put __main__ when starting with -m
# The alias is sites.api, and you can get the one being initialized by back reference (Python>=3.7
# from-import (a fallback for partially initialized modules).
if __name__ == "__main__":
    sys.modules.setdefault("sites.api", sys.modules[__name__])

from sites.http_kit import CONSOLE_PREFIX, HTTPKitMixin
from sites import grafana_proxy

_DEPLOYMENT_PATH = re.compile(r"^/v1/deployments/([^/?]+)$")
_BUILD_PATH = re.compile(r"^/v1/builds/([^/?]+)$")
_BUNDLE_PATH = re.compile(r"^/v1/bundles/([^/?]+)$")
_TENANT_PATH = re.compile(r"^/v1/tenants/([^/?]+)$")
_TENANT_TOKEN_PATH = re.compile(r"^/v1/tenants/([^/?]+)/token$")
_MERCHANT_PATH = re.compile(r"^/v1/merchants/([^/?]+)$")
_MERCHANT_KEY_PATH = re.compile(r"^/v1/merchants/([^/?]+)/key$")
_SITE_QUERY_PATH = re.compile(r"^/v1/sites/([^/?]+)/query$")
_SITE_VERSIONS_PATH = re.compile(r"^/v1/sites/([^/?]+)/versions$")
_SITE_PROMOTE_PATH = re.compile(r"^/v1/sites/([^/?]+)/promote$")

_HTTP_STARTED_AT = time.time()

METRICS = telemetry.Registry()
HTTP_UPTIME = METRICS.gauge(
    "sites_api_process_uptime_seconds",
    "Process HTTP uptime (registry-backed twin of sites_api_uptime_seconds).",
)
# Routing template instead of original path: `/v1/tenants/{id}/token` The id in this type of path is the tenant ID,
# Entering the tag directly not only reveals the tenant list but also makes the base unlimited.
HTTP_REQUESTS = METRICS.counter(
    "sites_api_requests_total",
    "HTTP requests by method, route template and status class.",
    ("method", "route", "status"),
)
HTTP_SECONDS = METRICS.histogram(
    "sites_api_request_seconds",
    "HTTP request handling time by method and route template.",
    telemetry.DEFAULT_LATENCY_BUCKETS,
    ("method", "route"),
)
# There is a single authentication result: 401 flood is the first signal of credential leakage or client mismatch, mixed in
# status="4xx" cannot be seen.
AUTH_TOTAL = METRICS.counter(
    "sites_api_auth_total",
    "Authentication outcomes.",
    ("outcome",),
)
HANDLER_ERRORS = METRICS.counter(
    "sites_api_handler_errors_total",
    "Unexpected request-handler exceptions by outcome.",
    ("outcome",),
)
TRACE_EXPORT = METRICS.counter(
    "sites_tracing_export_total",
    "OTLP spans by bounded-export outcome.",
    ("outcome",),
)
for _outcome in ("queued", "exported", "dropped_queue_full", "dropped_export_failure"):
    TRACE_EXPORT.ensure(_outcome)
# Snapshot synchronization is a background thread that swallows its own failures (see DatabaseSynchronizer), so
# /readyz cannot be detected - stagnation can only be detected by these two metrics.
SYNC_TOTAL = METRICS.counter(
    "sites_api_snapshot_sync_total",
    "Database snapshot sync attempts by outcome.",
    ("outcome",),
)
SYNC_AGE = METRICS.gauge(
    "sites_api_snapshot_age_seconds",
    "Age of the most recent successful snapshot sync.",
)
for _outcome in ("success", "failure"):
    SYNC_TOTAL.ensure(_outcome)
for _outcome in ("success", "unauthenticated"):
    AUTH_TOTAL.ensure(_outcome)
for _outcome in ("response_written", "connection_lost"):
    HANDLER_ERRORS.ensure(_outcome)
# Depends on availability. 1/0 instead of boolean: Prometheus only has numeric values.
DEPENDENCY_UP = METRICS.gauge(
    "sites_api_dependency_up",
    "Whether a downstream dependency answered on its most recent use.",
    ("dependency",),
)

# Assembly: The implementation and rationale of DependencyHealth/DatabaseSynchronizer are commented in
# sites/sync.py. You must import all metrics after the registration is completed - sync through the module
# Attribute access here is SYNC_*/DEPENDENCY_UP/KUBERNETES_HEALTH, and the import order
# It is the registration order of metrics in /metrics (the row order of the existing capture output will not be rearranged).
from sites.sync import DependencyHealth, DatabaseSynchronizer  # noqa: E402

KUBERNETES_HEALTH = DependencyHealth("kubernetes")

# Path → Route Template. The order makes sense: /v1/tenants/{id}/token must come first
# /v1/tenants/{id}, otherwise the former will be eaten by the latter's regularity first, and the metrics of the two routes will be merged.
_ROUTE_TEMPLATES: tuple[tuple[re.Pattern[str], str], ...] = (
    (_TENANT_TOKEN_PATH, "/v1/tenants/{id}/token"),
    (_TENANT_PATH, "/v1/tenants/{id}"),
    (_MERCHANT_KEY_PATH, "/v1/merchants/{id}/key"),
    (_MERCHANT_PATH, "/v1/merchants/{id}"),
    (_DEPLOYMENT_PATH, "/v1/deployments/{id}"),
    (_BUILD_PATH, "/v1/builds/{id}"),
    (_BUNDLE_PATH, "/v1/bundles/{id}"),
    (_SITE_VERSIONS_PATH, "/v1/sites/{id}/versions"),
    (_SITE_PROMOTE_PATH, "/v1/sites/{id}/promote"),
    (_SITE_QUERY_PATH, "/v1/sites/{id}/query"),
)


def _status_class(status: int) -> str:
    """Buckets by status category. Separating tags by status code causes the base to grow with the error codes being scanned."""
    if status <= 0:
        return "unknown"
    return f"{status // 100}xx"


def _route_template(path: str) -> str:
    """Normalize request paths into bounded routing templates.

    If there is no match, `other` will be returned instead of the original path: the base of the metric label must have an upper bound, otherwise
    A crawler that scans the path randomly can explode the time series on the crawler side (and those paths will still be deleted as they are).
    stored in the monitoring system).
    """
    clean = path.split("?", 1)[0]
    for pattern, template in _ROUTE_TEMPLATES:
        if pattern.match(clean):
            return template
    if clean in ("/healthz", "/readyz", "/metrics", "/v1/deployments", "/v1/builds",
                 "/v1/bundles", "/v1/tenants", "/v1/merchants", "/v1/capabilities",
                 "/v1/scaffolds",
                 "/mcp",
                 "/v1/auth/methods", "/v1/auth/login",
                 "/v1/auth/callback", "/v1/auth/local", "/v1/auth/logout",
                 # Four static routes on the admin console: If it falls into "other", the admin traffic will be
                 # sites_api_requests_total is completely invisible - and it is exactly the troubleshooting
                 # That curve is the first thing to look at when it comes to console problems.
                 "/v1/admin/deployments", "/v1/admin/health",
                 "/v1/admin/builds", "/v1/admin/images",
                 "/v1/admin/metrics/cluster", "/v1/admin/metrics/application"):
        return clean
    if clean.startswith("/console"):
        return "/console"
    # The embedded-panel proxy fans out over Grafana's own asset tree. One
    # label, not an enumeration: the alternative is either an unbounded label
    # or a route table that has to track a third party's bundle layout.
    if clean.startswith("/grafana/"):
        return "/grafana/*"
    return "other"


def _render_metrics() -> str:
    """Return the small, dependency-free control-plane scrape contract.

    Up/uptime are historical contracts (test_sites has assertions) and remain unchanged verbatim; the rest are left unchanged
    telemetry registry. Request metrics are only labeled by method/routing template/status code - never
    With tenant or site identification, otherwise /metrics will become an authentication-free tenant list leakage surface.
    """
    uptime = max(0.0, time.time() - _HTTP_STARTED_AT)
    HTTP_UPTIME.set(uptime)
    return "\n".join(
        (
            "# HELP sites_api_up Whether the Sites HTTP API is running.",
            "# TYPE sites_api_up gauge",
            "sites_api_up 1",
            "# HELP sites_api_uptime_seconds Process HTTP uptime.",
            "# TYPE sites_api_uptime_seconds gauge",
            f"sites_api_uptime_seconds {uptime:.3f}",
            METRICS.render(),
        )
    )


from sites.api_tenants import TenantsMixin  # noqa: E402
from sites.api_merchants import MerchantsMixin  # noqa: E402
from sites.api_admin import AdminMixin  # noqa: E402
from sites.api_builds import BuildsMixin  # noqa: E402
from sites.api_bundles import BundlesMixin  # noqa: E402
from sites.api_deployments import DeploymentsMixin  # noqa: E402
from sites.api_sites import SitesMixin  # noqa: E402
from sites.api_auth import AuthMixin  # noqa: E402
from sites import api_mcp as mcp_route  # noqa: E402
from sites.api_mcp import McpMixin  # noqa: E402


class Handler(
    HTTPKitMixin,
    TenantsMixin,
    MerchantsMixin,
    AdminMixin,
    BuildsMixin,
    BundlesMixin,
    DeploymentsMixin,
    SitesMixin,
    AuthMixin,
    McpMixin,
    BaseHTTPRequestHandler,
):
    # Avoid disclosing the Python/runtime version in every response header.
    server_version = "sites-api"
    sys_version = ""

    def version_string(self) -> str:
        return self.server_version

    # The upper limit of any socket read and write on a single connection. BaseHTTPRequestHandler is not set by default
    # (timeout of socketserver is None), so Content-Length is declared but body is not sent.
    # Without even finishing sending the request line, a thread can be permanently occupied - and this happens in _authenticate
    # **Before**, no credentials were required, and ThreadingHTTPServer had no upper limit on the number of connections.
    # 30 seconds is generous enough for request bodies up to 64KiB.
    timeout = 30

    kube: KubeClient
    store: Store
    service_token: str
    # Signing key for admin console sessions. Its own secret, never the service token
    # (see sites/console_session.py). serve() refuses to start without it.
    session_key: str = ""
    # Whether the service token may still be exchanged for a console session and accepted
    # as a credential. The default matches the documented rule for a deployment with no
    # identity provider; serve() computes it from configuration on every real start.
    local_login_enabled: bool = True
    # sites.oidc.Config, or None when this deployment has no identity provider.
    oidc_config: Any = None
    mutation_lock: threading.Lock
    # Seconds a write path waits for mutation_lock before answering 503
    # control_plane_busy (see admission.acquire_mutation_lock). A class attribute
    # so tests can shrink it without touching the environment.
    mutation_lock_timeout: float = admission.MUTATION_LOCK_TIMEOUT
    # Set by serve(). The deployment collection is served from the database
    # snapshot, so clients need to know how old that snapshot is: the sync
    # thread swallows its own failures and /readyz only pings the database,
    # which used to make an indefinitely frozen list indistinguishable from a
    # quiet one. None when no synchronizer is wired (unit tests).
    synchronizer: DatabaseSynchronizer | None = None
    site_databases: Any
    static_artifacts: Any

    def log_message(self, fmt: str, *args: Any) -> None:
        # Access log debugging: one line for each request will drown out the real failure events, and the request volume itself
        # Hosted by sites_api_requests_total. To access the log item by item, set SITES_LOG_LEVEL=debug.
        telemetry.log(
            "http_access",
            level="debug",
            peer=self.address_string(),
            message=fmt % args,
        )

    def send_response(self, code: int, message: str | None = None) -> None:
        # The status code only appears here once. Let the metric be taken from here instead of having each do_* branch
        # Report it yourself - missing a branch in the latter is a silent blind spot.
        self._observed_status = code
        super().send_response(code, message)

    def handle_one_request(self) -> None:
        """Wrap the entire request cycle to collect latency and status codes.

        Put it in this layer instead of four do_* methods: malformed requests, timeouts, and anything in routing
        Paths returned before distribution (such as 414/400) will also be counted, and those are the troubleshooting
        The only useful sample is when "the gateway says 5xx but the application logs have nothing".
        """
        self._observed_status = 0
        started = time.monotonic()
        # A local id is bound before the request line has even been read, so the lines
        # written by the malformed-request and timeout paths carry one too. parse_request
        # replaces it with the caller's once the headers exist - which is the earliest
        # moment they do exist, since super().handle_one_request() is what parses them.
        self._trace_header_sent = False
        self._trace_id = tracing.new_trace_id()
        self._server_span = None
        trace_token = tracing.bind(self._trace_id)
        try:
            super().handle_one_request()
        except Exception as exc:
            outcome = (
                "connection_lost"
                if self._observed_status
                else "response_written"
            )
            HANDLER_ERRORS.inc(outcome)
            telemetry.log_exception("api_handler_failed", exc)
            if not self._observed_status:
                try:
                    self._json(500, {"error": "internal server error"})
                except (BrokenPipeError, ConnectionResetError, OSError):
                    HANDLER_ERRORS.inc("connection_lost")
        finally:
            raw_path = getattr(self, "path", "") or ""
            method = getattr(self, "command", "") or "UNKNOWN"
            if self._observed_status:
                route = _route_template(raw_path)
                elapsed = time.monotonic() - started
                HTTP_REQUESTS.inc(method, route, _status_class(self._observed_status))
                HTTP_SECONDS.observe(elapsed, method, route)
            server_span = getattr(self, "_server_span", None)
            if server_span is not None:
                server_span.set_attribute("http.request.method", method)
                server_span.set_attribute("http.route", _route_template(raw_path))
                server_span.set_attribute("http.response.status_code", self._observed_status)
                if self._observed_status >= 500:
                    server_span.set_error(f"HTTP {self._observed_status}")
                server_span.__exit__(None, None, None)
            tracing.release(trace_token)

    def parse_request(self) -> bool:
        """Adopt the caller's trace context as soon as the headers are readable.

        Returning False means the base class already answered with an error, and that
        answer keeps the locally generated id - there is nothing to inherit from a request
        that could not be parsed.
        """
        parsed = super().parse_request()
        if parsed:
            self._trace_id, flags = tracing.inbound_context(self.headers)
            tracing.bind(self._trace_id, flags)
            self._server_span = tracing.span(
                "sites.api.request",
                kind=2,
                trace_id=self._trace_id,
                parent_span_id=tracing.inbound_parent_span_id(self.headers),
                flags=flags,
            )
            self._server_span.__enter__()
        return parsed

    def end_headers(self) -> None:
        """Echo the trace id on every response, whatever wrote it.

        Hooked here rather than in the response helpers because this is the one call every
        path goes through - including ``send_error`` and the static console - so no branch
        can be added later that quietly answers without it. That is the whole value of the
        header: the id in the caller's hand is the id in our logs, for every response.
        """
        trace_id = getattr(self, "_trace_id", "")
        if trace_id and not getattr(self, "_trace_header_sent", False):
            self._trace_header_sent = True
            self.send_header(tracing.REQUEST_ID_HEADER, trace_id)
        super().end_headers()

    def _respond_with_error(
        self, exc: BaseException, table: _ErrorResponseTable
    ) -> bool:
        """Write exceptions as HTTP reject responses in an ordered mapping table (see the comments at the table definition).

        isinstance is the first hit in list order, equivalent to the first matching clause in the except ladder.
        Returning False indicates that the type is not in the table - the caller must re-raise as is, keeping
        Old behavior of "unlisted exceptions continue to propagate upward".
        """
        for exc_type, status, code, fixed_error in table:
            if isinstance(exc, exc_type):
                payload: dict[str, str] = {
                    "error": (
                        fixed_error if fixed_error is not None else str(exc)
                    )
                }
                if code is not None:
                    payload["code"] = code
                self._json(status, payload)
                return True
        return False

    def _readyz(self) -> None:
        """Readiness probe.

        🔴 **Only the database can make it red. ** The semantics of readiness is "should traffic be sent here?"
        It's not "whether everything is normal" - the criterion is "can removing the traffic improve the situation":

        The database is unavailable → turns red. Authentication, quotas, and snapshots are all removed and nothing can be done.
        K8s is blocked → **not red**. The read path (column deployment/query status) is served by the database snapshot,
        Available as usual; and this is a single-copy deployment, removing traffic is equivalent to including read-only
        Turning it off, strictly worse, will not allow the apiserver to recover one second earlier.
        Snapshot stale → **not red**. The data is old but serviceable, and removing traffic will not allow synchronization threads
        Start running again.

        The latter two do not affect the status code, but must be present in the response body and metrics - they were completely invisible before:
        The synchronization thread swallows its own failures, and /readyz only pings the database, so the "frozen list"
        Looks exactly like "Quiet List".
        """
        checks: dict[str, Any] = {}
        try:
            self.store.ping()
        except StorageError as exc:
            # 🔴 /readyz answers before authentication, so this body is readable
            # by anything that can reach the port. The driver text names the
            # database host and port (psycopg: `connection to server at
            # "10.0.0.5", port 5432 failed`), and truncating to 200 characters
            # does not help - the address is in the first eighty. The type stays
            # whole, the message is host-redacted, and the unredacted text goes
            # to the log where reading it already needs cluster access.
            telemetry.log_exception("readyz_database_unavailable", exc)
            checks["database"] = {
                "ok": False,
                "error_type": type(exc).__name__,
                "error": telemetry.redact_endpoints(
                    f"{type(exc).__name__}: {exc}"
                ),
            }
            self._json(503, {"status": "not ready", "checks": checks})
            return
        checks["database"] = {"ok": True, "backend": self.store.backend}
        checks["kubernetes"] = KUBERNETES_HEALTH.snapshot()

        age = (
            self.synchronizer.snapshot_age_seconds()
            if self.synchronizer is not None
            else None
        )
        checks["snapshot"] = (
            {"observed": False}
            if age is None
            else {"observed": True, "ageSeconds": round(age, 3)}
        )
        # Report the actual backend instead of a hard-coded name so health and
        # troubleshooting evidence stays truthful.
        self._json(
            200,
            {
                "status": "ready",
                "database": self.store.backend,
                "checks": checks,
            },
        )

    # --- Identity Shell: Annotations for decisions and reasons are in sites/identity.py, which only does two things -
    # Write Refusal as the response and remember AUTH_TOTAL (success or failure does not remember the path, see identity.authenticate).
    def _may_act_as_subjects(self, merchant_id: str) -> bool:
        """Whether the credential's merchant carries the impersonation grant.

        Read rather than remembered: the grant can be withdrawn at any time, and a client
        that keeps asking gets the current answer.
        """
        try:
            record = self.store.merchant(merchant_id)
        except StorageError:
            # Capabilities is a description, not an authorization decision. Reporting the
            # conservative answer beats failing a request that is otherwise fine.
            return False
        return bool(record and record.get("may_act_as_subjects"))

    def _unsafe_method(self) -> bool:
        return getattr(self, "command", "GET") in {"POST", "PATCH", "PUT", "DELETE"}

    def _is_admin(self) -> bool:
        return identity.is_admin(
            self.headers,
            self.service_token,
            session_key=self.session_key,
            local_login_enabled=self.local_login_enabled,
            unsafe=self._unsafe_method(),
        )

    def _authenticate(self) -> tuple[str, str] | None:
        """Resolve the (merchant, tenant) this request acts as, or write the refusal.

        The four mutually exclusive paths are annotated with all decisions in sites/identity.authenticate.
        None indicates that a rejection response has been written, and the caller returns directly.
        """
        outcome = identity.authenticate(
            self.headers,
            self.store,
            self.service_token,
            session_key=self.session_key,
            local_login_enabled=self.local_login_enabled,
            unsafe=self._unsafe_method(),
        )
        raw_path = getattr(self, "path", "") or ""
        method = getattr(self, "command", "") or "UNKNOWN"
        if isinstance(outcome, identity.Refusal):
            AUTH_TOTAL.inc("unauthenticated")
            identity.audit_acting_call(self.headers, method, raw_path, "deny")
            self._json(outcome.status, outcome.payload)
            return None
        AUTH_TOTAL.inc("success")
        identity.audit_acting_call(self.headers, method, raw_path, "allow")
        return outcome

    def _require_admin(self) -> bool:
        if self._is_admin():
            return True
        self._json(403, {"error": "this endpoint requires the admin token"})
        return False

    def _tenant_quota(
        self, merchant_id: str, user_id: str
    ) -> dict[str, int] | None:
        """One tenant’s quota + merchant-level quota. None indicates that a rejection response has been written;
        See sites/identity.tenant_quota for semantics and JIT construction reasons."""
        outcome = identity.tenant_quota(self.store, merchant_id, user_id)
        if isinstance(outcome, identity.Refusal):
            self._json(outcome.status, outcome.payload)
            return None
        return outcome

    def _admit_and_assign_ports(
        self,
        merchant_id: str,
        user_id: str,
        desired_resources: list[dict[str, Any]],
        quota: dict[str, int],
    ) -> None:
        """The pure function domain is in sites/admission.py; here only the method shell is reserved for endpoints and tests to call."""
        admission.admit_and_assign_ports(
            self.kube, merchant_id, user_id, desired_resources, quota
        )

    def do_GET(self) -> None:  # noqa: N802
        path, query = self._route()
        # The embedded observability panel. Everything about it - who may see
        # it, which upstream paths exist, what headers cross - is in
        # sites/grafana_proxy.py; only the dispatch lives here.
        if path.startswith(grafana_proxy.ROUTE_PREFIX):
            grafana_proxy.serve(self, path)
            return
        if path == "/healthz":
            self._json(200, {"status": "ok"})
            return
        if path == mcp_route.ROUTE:
            self._mcp_not_allowed()
            return
        if path == "/metrics":
            self._text(
                200,
                _render_metrics(),
                "text/plain; version=0.0.4; charset=utf-8",
            )
            return
        if path == "/readyz":
            self._readyz()
            return
        if path == "/console":
            # If one slash is missing, 404 is a pure pitfall: all console resources are relative paths, no
            # In this jump, the first visit will get index.html, but all assets will be parsed incorrectly.
            self.send_response(301)
            self.send_header("Location", CONSOLE_PREFIX)
            self._common_security_headers()
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path.startswith(CONSOLE_PREFIX):
            self._serve_console(path)
            return
        if path == "/v1/auth/methods":
            self._login_methods()
            return
        if path == "/v1/auth/login":
            self._begin_oidc_login()
            return
        if path == "/v1/auth/callback":
            self._complete_oidc_login(query)
            return
        if path == "/v1/capabilities":
            identity = self._authenticate()
            if identity is None:
                return
            self._json(
                200,
                _capabilities_response(
                    *identity, may_act_as_subjects=self._may_act_as_subjects(identity[0])
                ),
            )
            return
        if path == "/v1/scaffolds":
            identity = self._authenticate()
            if identity is None:
                return
            self._json(200, _scaffold_catalog())
            return
        if path == "/v1/tenants":
            self._list_tenants(query)
            return
        if path == "/v1/tenants/self":
            self._describe_self()
            return
        if path == "/v1/merchants":
            self._list_merchants()
            return
        merchant_match = _MERCHANT_PATH.fullmatch(path)
        if merchant_match:
            self._describe_merchant(merchant_match.group(1))
            return
        if path == "/v1/admin/deployments":
            self._admin_deployments(query)
            return
        if path == "/v1/admin/health":
            self._admin_health()
            return
        if path == "/v1/admin/builds":
            self._admin_builds(query)
            return
        if path == "/v1/admin/images":
            self._admin_images()
            return
        if path == "/v1/admin/metrics/cluster":
            self._admin_cluster_metrics(query)
            return
        if path == "/v1/admin/metrics/application":
            self._admin_application_metrics(query)
            return
        if path == "/v1/deployments":
            self._list_deployments()
            return
        build_match = _BUILD_PATH.fullmatch(path)
        if build_match:
            self._get_build(build_match.group(1))
            return
        bundle_match = _BUNDLE_PATH.fullmatch(path)
        if bundle_match:
            self._get_bundle(bundle_match.group(1))
            return
        site_versions_match = _SITE_VERSIONS_PATH.fullmatch(path)
        if site_versions_match:
            self._list_site_versions(site_versions_match.group(1))
            return

        match = _DEPLOYMENT_PATH.fullmatch(path)
        if not match:
            self._json(404, {"error": "not found"})
            return
        self._get_deployment(match.group(1))

    def do_POST(self) -> None:  # noqa: N802
        path, query = self._route()
        if path.startswith(grafana_proxy.ROUTE_PREFIX):
            grafana_proxy.serve(self, path)
            return
        if path == mcp_route.ROUTE:
            self._serve_mcp()
            return
        if path == "/v1/auth/local":
            self._local_login()
            return
        if path == "/v1/auth/logout":
            self._logout()
            return
        if path == "/v1/tenants":
            self._create_tenant()
            return
        tenant_token_match = _TENANT_TOKEN_PATH.fullmatch(path)
        if tenant_token_match:
            self._rotate_tenant_token(tenant_token_match.group(1), query)
            return
        if path == "/v1/merchants":
            self._create_merchant()
            return
        merchant_key_match = _MERCHANT_KEY_PATH.fullmatch(path)
        if merchant_key_match:
            self._rotate_merchant_key(merchant_key_match.group(1))
            return
        if path == "/v1/builds":
            self._post_build()
            return
        if path == "/v1/bundles":
            self._post_bundle()
            return
        site_query_match = _SITE_QUERY_PATH.fullmatch(path)
        if site_query_match:
            self._query_dynamic_site(site_query_match.group(1))
            return
        site_versions_match = _SITE_VERSIONS_PATH.fullmatch(path)
        if site_versions_match:
            self._create_site_version(site_versions_match.group(1))
            return
        site_promote_match = _SITE_PROMOTE_PATH.fullmatch(path)
        if site_promote_match:
            self._promote_site_version(site_promote_match.group(1))
            return
        if path != "/v1/deployments":
            self._json(404, {"error": "not found"})
            return
        self._post_deployment()

    def do_PATCH(self) -> None:  # noqa: N802
        path, query = self._route()
        tenant_match = _TENANT_PATH.fullmatch(path)
        if tenant_match:
            self._patch_tenant(tenant_match.group(1), query)
            return
        merchant_match = _MERCHANT_PATH.fullmatch(path)
        if merchant_match:
            self._patch_merchant(merchant_match.group(1))
            return
        self._json(404, {"error": "not found"})

    def do_DELETE(self) -> None:  # noqa: N802
        path, query = self._route()
        if path == mcp_route.ROUTE:
            self._mcp_not_allowed()
            return
        tenant_match = _TENANT_PATH.fullmatch(path)
        if tenant_match:
            self._disable_tenant(tenant_match.group(1), query)
            return
        merchant_match = _MERCHANT_PATH.fullmatch(path)
        if merchant_match:
            self._disable_merchant(merchant_match.group(1))
            return
        bundle_match = _BUNDLE_PATH.fullmatch(path)
        if bundle_match:
            self._delete_bundle(bundle_match.group(1))
            return
        build_match = _BUILD_PATH.fullmatch(path)
        if build_match:
            self._delete_build(build_match.group(1))
            return
        match = _DEPLOYMENT_PATH.fullmatch(path)
        if not match:
            self._json(404, {"error": "not found"})
            return
        self._delete_deployment(match.group(1))


def _wait_for_database(timeout: float = 120.0) -> Store:
    store = store_from_env()
    telemetry.log("storage_backend_selected", backend=store.backend)
    deadline = time.monotonic() + timeout
    while True:
        try:
            store.migrate()
            store.ping()
            return store
        except StorageError as exc:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"{store.backend} did not become ready in time"
                ) from exc
            time.sleep(2)


def _initial_sync(synchronizer: DatabaseSynchronizer) -> None:
    """Take the first snapshot before serving, but never refuse to start over it.

    The sync thread's run() loop already tolerates a failing round and retries every
    interval. Letting the same failure escape here made any restart of sites-api
    during an apiserver outage a CrashLoop: the Pod needed Kubernetes to be healthy
    at the exact moment it started, which is the one moment nothing guarantees.
    KUBERNETES_HEALTH still records the failure through sync_once, so /v1/admin/health
    reports it while the loop keeps retrying.
    """
    try:
        synchronizer.sync_once()
    except Exception as exc:  # noqa: BLE001 - startup must not depend on this round
        telemetry.log_exception("startup_snapshot_sync_failed", exc)


class _ApiServer(ThreadingHTTPServer):
    # ThreadingHTTPServer defaults to daemon request threads, which server_close()
    # does not join: SIGTERM would then return from serve_forever and exit the
    # process with requests half-written. Non-daemon threads are joined by
    # server_close(); the wait is bounded because every critical section a request
    # can block in now gives up after Handler.mutation_lock_timeout.
    daemon_threads = False


def _listen(host: str, port: int) -> ThreadingHTTPServer:
    """Bind the API socket and wire SIGTERM/SIGINT to a graceful stop.

    Split from serve() so the wiring is testable: serve() itself blocks forever.
    Must run on the main thread (signal.signal refuses any other).
    """
    server = _ApiServer((host, port), Handler)
    shutdown.install_stop_handler(server.shutdown)
    return server


def _load_console_session_key(path: str) -> str:
    """Read the console session signing key, or refuse to start.

    There is no generated fallback: a per-process random key would sign sessions that every
    restart silently invalidates and that a second replica cannot verify, and the operator
    would only find out from users. Missing required secrets stop the process, they do not
    downgrade it.
    """
    if not path:
        raise RuntimeError(
            "SITES_CONSOLE_SESSION_KEY_FILE is required; it signs admin console "
            "sessions and must not be the service token"
        )
    try:
        key = Path(path).read_text().strip()
    except OSError as exc:
        raise RuntimeError(
            f"Sites console session key cannot be read: {path}"
        ) from exc
    if len(key) < 32:
        raise RuntimeError(
            "Sites console session key must contain at least 32 characters"
        )
    return key


def _local_login_enabled(oidc_configured: bool) -> bool:
    """Decide whether the local (break-glass) login path exists at all.

        configured OIDC      -> off by default, may be turned on for break-glass
        no OIDC              -> on by default, or nobody could ever log in
        both explicitly off  -> refuse to start, and say why

    🔴 The flag disables the authentication path itself. The console's login form is a
    consequence of this value, never the other way round.
    """
    raw = (getenv("SITES_LOCAL_LOGIN_ENABLED", "") or "").strip().lower()
    if not raw:
        return not oidc_configured
    if raw in {"true", "1", "yes", "on"}:
        return True
    if raw not in {"false", "0", "no", "off"}:
        raise RuntimeError("SITES_LOCAL_LOGIN_ENABLED must be true or false")
    if not oidc_configured:
        raise RuntimeError(
            "SITES_LOCAL_LOGIN_ENABLED=false requires SITES_OIDC_ISSUER; with both "
            "disabled nobody can reach the admin console"
        )
    return False


def _require_admin_login_path(*, oidc_configured: bool, local_login: bool) -> None:
    """Refuse a first deployment that no platform administrator can enter.

    Merchant mappings only create merchant-scoped sessions. They cannot create
    merchants or repair authentication policy, so they are not a substitute for
    the first management-plane identity when the service-token login is off.
    Both sides of the OIDC admin mapping are explicit: silently defaulting the
    claim name makes a typo look like a valid but permanently locked console.
    """
    if local_login or not oidc_configured:
        return
    admin_claim = (getenv("SITES_OIDC_ADMIN_CLAIM", "") or "").strip()
    admin_value = (getenv("SITES_OIDC_ADMIN_VALUE", "") or "").strip()
    if not admin_claim or not admin_value:
        raise RuntimeError(
            "SITES_LOCAL_LOGIN_ENABLED=false requires both "
            "SITES_OIDC_ADMIN_CLAIM and SITES_OIDC_ADMIN_VALUE; merchant-only "
            "OIDC identities cannot bootstrap or administer the platform"
        )


def serve() -> None:
    tracing.configure("sites-api", lambda outcome, amount: TRACE_EXPORT.inc(outcome, amount=amount))
    token_file = Path(
        getenv("SITES_TOKEN_FILE", "/var/run/sites/token")
    )
    service_token = token_file.read_text().strip()
    if len(service_token) < 32:
        raise RuntimeError(
            "Sites service token must contain at least 32 characters"
        )

    session_key = _load_console_session_key(
        getenv("SITES_CONSOLE_SESSION_KEY_FILE", "") or ""
    )
    if hmac.compare_digest(session_key, service_token):
        raise RuntimeError(
            "Sites console session key must differ from the service token"
        )
    mcp_endpoint_enabled = mcp_route.endpoint_enabled_from_env()
    oidc_config = oidc.config_from_env()
    local_login = _local_login_enabled(oidc_config is not None)
    _require_admin_login_path(
        oidc_configured=oidc_config is not None,
        local_login=local_login,
    )
    telemetry.log(
        "console_login_modes",
        oidc=oidc_config is not None,
        local_login=local_login,
    )
    telemetry.log("mcp_endpoint", enabled=mcp_endpoint_enabled)

    store = _wait_for_database()
    kube = KubeClient()
    from sites.site_database import (
        DynamicSiteDatabaseService,
        SiteDatabaseSecretStore,
        site_data_config_from_env,
    )

    site_databases = DynamicSiteDatabaseService(
        site_data_config_from_env(), SiteDatabaseSecretStore(kube)
    )
    mutation_lock = threading.Lock()
    synchronizer = DatabaseSynchronizer(kube, store, mutation_lock)
    _initial_sync(synchronizer)

    Handler.kube = kube
    Handler.store = store
    Handler.service_token = service_token
    Handler.session_key = session_key
    Handler.local_login_enabled = local_login
    Handler.oidc_config = oidc_config
    Handler.mcp_endpoint_enabled = mcp_endpoint_enabled
    Handler.mutation_lock = mutation_lock
    Handler.site_databases = site_databases
    # Versioned static object storage is optional and initialized lazily by the
    # first static upload, so an OSS outage cannot stop dynamic sites API startup.
    Handler.static_artifacts = None
    Handler.synchronizer = synchronizer
    threading.Thread(
        target=synchronizer.run,
        name="sites-database-sync",
        daemon=True,
    ).start()
    host = getenv("SITES_API_HOST", "0.0.0.0") or "0.0.0.0"
    port = int(getenv("SITES_HTTP_PORT", "8080") or "8080")
    server = _listen(host, port)
    telemetry.log(
        "api_listening", host=host, port=port, backend=store.backend
    )
    # Returns once the stop handler calls server.shutdown(); server_close() then
    # joins the in-flight request threads (see _ApiServer) before the process exits.
    try:
        server.serve_forever()
    finally:
        server.server_close()
        store.close()
        tracing.shutdown()


if __name__ == "__main__":
    # Same as sites.operator: When the log consumer is shut down, the HTTP thread cannot be locked on print.
    from sites import safe_stdout

    safe_stdout.install()
    telemetry.configure("sites-api")
    serve()
