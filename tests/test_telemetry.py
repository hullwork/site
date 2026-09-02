"""Telemetry and control-plane instrumentation regressions.

These cases protect invariants rather than chase coverage: logs never corrupt stdio MCP
frames, legacy metric contracts remain stable, metric label cardinality is bounded, and
operator health reports reconciliation progress rather than merely an open port.
"""
from __future__ import annotations

import io
import json
import re
import threading
import time
import unittest
import urllib.request
from contextlib import redirect_stdout, redirect_stderr

from sites import telemetry
from sites.api import (
    METRICS as API_METRICS,
    _render_metrics,
    _route_template,
    _status_class,
)
from sites import operator as operator_module


class TelemetryPrimitiveTest(unittest.TestCase):
    def test_log_never_writes_stdout(self) -> None:
        """stdout belongs to the JSON-RPC channel of MCP, and not a single byte can be borrowed.

        sites.mcp.serve_stdio uses stdout to transmit protocol frames. Historically, print was written as stdout,
        It's just because mcp.py happened to have zero logs that it didn't explode - after switching to a unified log facility, any mcp
        If the imported module logs a log, it will destroy the protocol, and the symptom is that the client fails to parse and does not point to it at all.
        Log this direction.
        """
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            telemetry.log("probe_event", detail="x")
        self.assertEqual(out.getvalue(), "", "Log leakage to stdout breaks the MCP stdio protocol")
        self.assertIn("probe_event", err.getvalue())

    def test_log_resolves_stderr_at_call_time(self) -> None:
        """sys.stderr must not be cached to the module level.

        The resident process uses safe_stdout.install() to replace sys.stderr with a non-blocking substitute.
        (Drop logs instead of freezing threads when the log pipe is full). Caching a reference silently bypasses that layer of protection -
        The log is output as usual, and is only exposed as "thread collective stuck" when the pipe is really full, and at that time it is basically
        No one will doubt the logging module. If redirect_stderr can take effect, it means that it is evaluated when calling.
        """
        sentinel = io.StringIO()
        with redirect_stderr(sentinel):
            telemetry.log("late_bound_probe")
        self.assertIn("late_bound_probe", sentinel.getvalue())

    def test_json_format_emits_one_parseable_object_per_line(self) -> None:
        err = io.StringIO()
        original = telemetry._LOG_FORMAT
        telemetry._LOG_FORMAT = "json"
        try:
            with redirect_stderr(err):
                telemetry.log("evt", name="site-a", count=3)
        finally:
            telemetry._LOG_FORMAT = original
        record = json.loads(err.getvalue().strip())
        self.assertEqual(record["event"], "evt")
        self.assertEqual(record["name"], "site-a")
        self.assertEqual(record["count"], 3)
        self.assertIn("ts", record)
        self.assertIn("service", record)

    def test_log_exception_splits_type_from_message(self) -> None:
        """Types must be separated into fields to be aggregated: if the message contains high cardinality content, aggregation by message will fragment it."""
        err = io.StringIO()
        original = telemetry._LOG_FORMAT
        telemetry._LOG_FORMAT = "json"
        try:
            with redirect_stderr(err):
                telemetry.log_exception("boom", ValueError("site-77 exploded"))
        finally:
            telemetry._LOG_FORMAT = original
        record = json.loads(err.getvalue().strip())
        self.assertEqual(record["error_type"], "ValueError")
        self.assertEqual(record["error"], "site-77 exploded")

    def test_level_filter_drops_below_minimum(self) -> None:
        err = io.StringIO()
        original = telemetry._MIN_LEVEL
        telemetry._MIN_LEVEL = "warn"
        try:
            with redirect_stderr(err):
                telemetry.log("chatty", level="info")
                telemetry.log("important", level="error")
        finally:
            telemetry._MIN_LEVEL = original
        self.assertNotIn("chatty", err.getvalue())
        self.assertIn("important", err.getvalue())

    def test_counter_ensure_makes_zero_observable(self) -> None:
        """Without ensure, "never failed" and "metric not connected" are the same on the crawler side."""
        registry = telemetry.Registry()
        counter = registry.counter("t_total", "help", ("outcome",))
        counter.ensure("failure")
        counter.inc("success")
        rendered = registry.render()
        self.assertIn('t_total{outcome="failure"} 0', rendered)
        self.assertIn('t_total{outcome="success"} 1', rendered)

    def test_histogram_buckets_are_cumulative(self) -> None:
        registry = telemetry.Registry()
        hist = registry.histogram("h_seconds", "help", (0.1, 1.0))
        hist.observe(0.05)
        hist.observe(0.5)
        hist.observe(9.0)
        rendered = registry.render()
        self.assertIn('h_seconds_bucket{le="0.1"} 1', rendered)
        self.assertIn('h_seconds_bucket{le="1"} 2', rendered)
        self.assertIn('h_seconds_bucket{le="+Inf"} 3', rendered)
        self.assertIn("h_seconds_count 3", rendered)

    def test_label_values_are_escaped(self) -> None:
        registry = telemetry.Registry()
        counter = registry.counter("e_total", "help", ("name",))
        counter.inc('has"quote')
        self.assertIn(r'e_total{name="has\"quote"} 1', registry.render())

    def test_metric_without_samples_still_declares_type(self) -> None:
        """The crawler uses this to distinguish between "no data" and "this version does not have this metric"."""
        registry = telemetry.Registry()
        registry.counter("empty_total", "help")
        rendered = registry.render()
        self.assertIn("# TYPE empty_total counter", rendered)

    def test_label_arity_mismatch_is_rejected(self) -> None:
        registry = telemetry.Registry()
        counter = registry.counter("a_total", "help", ("x",))
        with self.assertRaises(ValueError):
            counter.inc()


class ApiMetricContractTest(unittest.TestCase):
    def test_legacy_scrape_contract_is_preserved_verbatim(self) -> None:
        """up/uptime are existing contracts, extending /metrics must not change their form."""
        payload = _render_metrics()
        self.assertIn("# TYPE sites_api_up gauge", payload)
        self.assertIn("sites_api_up 1", payload)
        self.assertIn("sites_api_uptime_seconds ", payload)

    def test_registry_metrics_are_appended(self) -> None:
        payload = _render_metrics()
        self.assertIn("sites_api_requests_total", payload)
        self.assertIn("sites_api_auth_total", payload)
        self.assertIn("sites_api_snapshot_sync_total", payload)

    def test_rendered_payload_is_prometheus_parseable(self) -> None:
        """Each non-comment line must be a `name[{label}] value`."""
        sample = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*(\{.*\})? -?[0-9.eE+]+$")
        for line in _render_metrics().splitlines():
            if not line or line.startswith("#"):
                continue
            self.assertRegex(line, sample, f"Not a valid Prometheus sample line: {line!r}")

    def test_route_template_bounds_cardinality(self) -> None:
        """Unknown paths must converge to other, otherwise the crawler can overwhelm the time series."""
        self.assertEqual(_route_template("/v1/deployments/site-a"), "/v1/deployments/{id}")
        self.assertEqual(_route_template("/v1/deployments"), "/v1/deployments")
        self.assertEqual(_route_template("/wp-admin.php"), "other")
        self.assertEqual(_route_template("/random/" + "x" * 200), "other")

    def test_tenant_token_route_stays_separate_from_tenant_route(self) -> None:
        """"Token issuance" must be an independent time series, otherwise an independent alarm cannot be generated.

        The current two routes are naturally mutually exclusive by regularity (`[^/?]+` does not span `/`, and `$` anchors the tail), so
        The order of _ROUTE_TEMPLATES is actually insensitive - the mutual exclusivity itself is directly asserted below,
        Instead of asserting "sequential pairs". Placing guards in order is ineffective: I have actually tested reversing the order.
        The two use cases are still all green. What really makes them merge is when someone relaxes the regular expression to `.+` or removes it
        `$`, so you can catch it by observing regular behavior.
        """
        self.assertEqual(
            _route_template("/v1/tenants/acme/token"), "/v1/tenants/{id}/token"
        )
        self.assertEqual(_route_template("/v1/tenants/acme"), "/v1/tenants/{id}")
        # Mutual exclusivity itself: a single-tenant regex must not eat any URL with a subpath.
        from sites.api import _TENANT_PATH

        self.assertIsNone(
            _TENANT_PATH.match("/v1/tenants/acme/token"),
            "After _TENANT_PATH is relaxed enough to match sub-paths, the two routes will be merged into one time series.",
        )

    def test_route_template_strips_query_string(self) -> None:
        self.assertEqual(
            _route_template("/v1/deployments?limit=10"), "/v1/deployments"
        )

    def test_no_metric_label_carries_tenant_identity(self) -> None:
        """/metrics does not require authentication. If the tenant/site name appears in the label, the list is leaked."""
        # Each item in the whitelist must be a fixed-base enumeration. New tags must be explicitly reviewed here:
        # The value of this guard is that it will block every new tag and force people to answer "Is its value set bounded?"
        # Will it bring the tenant/site identification?" The dependency currently only has "kubernetes".
        allowed = {
            "method", "route", "status", "outcome", "kind", "phase", "le",
            "dependency",
        }
        for metric in API_METRICS._metrics:
            self.assertTrue(
                set(metric.labels) <= allowed,
                f"{metric.name} tags {metric.labels} Out of whitelist {allowed}",
            )

    def test_status_class_buckets_by_hundred(self) -> None:
        self.assertEqual(_status_class(200), "2xx")
        self.assertEqual(_status_class(404), "4xx")
        self.assertEqual(_status_class(503), "5xx")
        self.assertEqual(_status_class(0), "unknown")


class ReadinessSemanticsTest(unittest.TestCase):
    """/readyz should only turn red if "removing traffic would improve the situation".

    This set of use cases focuses on the judgment itself, not the implementation: K8s or snapshot lag is also included in readiness.
    When the apiserver is flapping, a single-copy service with a completely healthy read path will be completely removed.
    """

    class _Store:
        backend = "postgresql"

        def __init__(self, fail: bool = False) -> None:
            self.fail = fail

        def ping(self) -> None:
            if self.fail:
                from sites.storage import StorageError

                raise StorageError("db down")

    class _Sync:
        def __init__(self, age: float | None) -> None:
            self.age = age

        def snapshot_age_seconds(self) -> float | None:
            return self.age

    def _probe(self, store, synchronizer=None) -> tuple[int, dict]:
        from sites.api import Handler

        responses: list = []
        handler = object.__new__(Handler)
        handler.path = "/readyz"
        handler.store = store
        handler.synchronizer = synchronizer
        handler._json = lambda status, payload: responses.append((status, payload))
        handler._readyz()
        return responses[-1]

    def setUp(self) -> None:
        from sites.api import KUBERNETES_HEALTH

        self.health = KUBERNETES_HEALTH

    def test_database_failure_sheds_traffic(self) -> None:
        status, body = self._probe(self._Store(fail=True))
        self.assertEqual(status, 503)
        self.assertFalse(body["checks"]["database"]["ok"])

    def test_kubernetes_failure_does_not_shed_traffic(self) -> None:
        """core assertion.

        The read path is served by the database snapshot and is available as usual; single copy download traffic is equivalent to turning off read-only.
        And it will not let the apiserver recover one second earlier.
        """
        self.health.record_failure(RuntimeError("apiserver unreachable"))
        try:
            status, body = self._probe(self._Store())
        finally:
            self.health.record_success()
        self.assertEqual(status, 200, "K8s unreachability should not make readiness red")
        self.assertFalse(body["checks"]["kubernetes"]["ok"])
        self.assertIn("apiserver unreachable", body["checks"]["kubernetes"]["error"])

    def test_kubernetes_state_is_visible_even_when_healthy(self) -> None:
        self.health.record_success()
        _status, body = self._probe(self._Store())
        self.assertTrue(body["checks"]["kubernetes"]["ok"])
        self.assertTrue(body["checks"]["kubernetes"]["observed"])

    def test_never_used_dependency_is_not_reported_as_broken(self) -> None:
        """This is the state between startup and the first round of synchronization.

        Reporting ok:false will make the newly started process appear to be faulty.
        """
        from sites.api import DependencyHealth

        fresh = DependencyHealth("probe-only")
        snapshot = fresh.snapshot()
        self.assertFalse(snapshot["observed"])
        self.assertNotIn("error", snapshot)

    def test_stale_snapshot_is_reported_but_stays_ready(self) -> None:
        """Frozen lists were previously completely indistinguishable from quiet lists."""
        self.health.record_success()
        status, body = self._probe(self._Store(), self._Sync(age=3600.0))
        self.assertEqual(status, 200)
        self.assertEqual(body["checks"]["snapshot"]["ageSeconds"], 3600.0)

    def test_snapshot_without_a_synchronizer_is_unobserved_not_zero(self) -> None:
        """When the synchronizer is not connected, observed:false is reported instead of age=0 (which is equivalent to lying about just synchronizing)."""
        self.health.record_success()
        _status, body = self._probe(self._Store(), None)
        self.assertFalse(body["checks"]["snapshot"]["observed"])
        self.assertNotIn("ageSeconds", body["checks"]["snapshot"])

    def test_backend_name_is_the_real_one(self) -> None:
        _status, body = self._probe(self._Store())
        self.assertEqual(body["database"], "postgresql")


class OperatorHealthTest(unittest.TestCase):
    """The discriminating power of operator in operation and maintenance."""

    def setUp(self) -> None:
        operator_module.LAST_SWEEP_TIMESTAMP.set(0.0)
        operator_module.LAST_PROGRESS_TIMESTAMP.set(0.0)
        self.server = operator_module.serve_metrics(port=0)
        self.port = self.server.server_address[1]
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)

    def _get(self, path: str) -> tuple[int, str]:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}{path}", timeout=5
            ) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def test_healthz_is_red_when_no_sweep_has_completed(self) -> None:
        """Just returning 200 only proves that the daemon thread is responding, which has nothing to do with the life or death of reconcile."""
        status, body = self._get("/healthz")
        self.assertEqual(status, 503)
        self.assertFalse(json.loads(body)["ok"])

    def test_healthz_is_green_right_after_a_sweep(self) -> None:
        operator_module.LAST_SWEEP_TIMESTAMP.set(time.time())
        status, body = self._get("/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_healthz_goes_red_once_sweeps_go_stale(self) -> None:
        """This is the core: the HTTP thread still responds when reconcile is stuck, and the criterion must be the sweep clock."""
        stale_by = operator_module._health_stale_after() + 10
        operator_module.LAST_SWEEP_TIMESTAMP.set(time.time() - stale_by)
        status, body = self._get("/healthz")
        self.assertEqual(status, 503)
        payload = json.loads(body)
        self.assertFalse(payload["ok"])
        self.assertGreater(payload["lastSweepAgeSeconds"], payload["staleAfterSeconds"])

    def test_healthz_stays_green_while_a_slow_sweep_makes_progress(self) -> None:
        """A sweep that is slow but moving is not a deadlock.

        Sixty Ready-but-unreachable sites cost 60 x VERIFY_TIMEOUT per sweep, past the
        stall threshold; killing the operator for that only restarts the same sweep.
        The criterion is whether *one item* finished recently, not a whole sweep.
        """
        stale_by = operator_module._health_stale_after() + 10
        operator_module.LAST_SWEEP_TIMESTAMP.set(time.time() - stale_by)
        operator_module.LAST_PROGRESS_TIMESTAMP.set(time.time())
        status, body = self._get("/healthz")
        payload = json.loads(body)
        self.assertEqual(status, 200, body)
        self.assertTrue(payload["ok"])
        self.assertGreater(payload["lastSweepAgeSeconds"], payload["staleAfterSeconds"])
        self.assertLess(payload["lastProgressAgeSeconds"], payload["staleAfterSeconds"])

    def test_healthz_goes_red_when_neither_clock_moves(self) -> None:
        stale_by = operator_module._health_stale_after() + 10
        operator_module.LAST_SWEEP_TIMESTAMP.set(time.time() - stale_by)
        operator_module.LAST_PROGRESS_TIMESTAMP.set(time.time() - stale_by)
        status, body = self._get("/healthz")
        self.assertEqual(status, 503)
        self.assertFalse(json.loads(body)["ok"])

    def test_stale_threshold_survives_a_slow_sweep(self) -> None:
        """The threshold must be enough to complete a round of real sweep, otherwise liveness will periodically accidentally kill the operator.

        Worst round round = N CRs × forensic probes at most VERIFY_TIMEOUT_SECONDS each.
        Estimated at ten stations, that's 50s - and "5 rounds × 2s" is only 10s. This handle
        The difference between "barrier budget" and "failure detector" is fixed: restarting will not make a slow reconcile faster.
        """
        worst_case_sweep = operator_module.VERIFY_TIMEOUT_SECONDS * 10
        self.assertGreater(
            operator_module._health_stale_after(),
            worst_case_sweep,
            "The stagnation threshold is less than the time of a slow sweep. Hanging livenessProbe will restart itself.",
        )

    def test_metrics_endpoint_serves_registry(self) -> None:
        status, body = self._get("/metrics")
        self.assertEqual(status, 200)
        self.assertIn("sites_operator_reconcile_total", body)
        self.assertIn("sites_operator_sweep_total", body)

    def test_unknown_path_is_404(self) -> None:
        self.assertEqual(self._get("/")[0], 404)

    def test_operator_metric_labels_carry_no_tenant_identity(self) -> None:
        allowed = {"kind", "outcome", "phase", "le"}
        for metric in operator_module.METRICS._metrics:
            self.assertTrue(
                set(metric.labels) <= allowed,
                f"{metric.name} tags {metric.labels} Out of whitelist {allowed}",
            )


class OperatorReconcileAccountingTest(unittest.TestCase):
    """Every failed path must be counted, otherwise the failure rate metric is false."""

    class _Kube:
        def __init__(self, items: list[dict]) -> None:
            self.items = items

        def get(self, path: str) -> dict:
            if path == operator_module.BUILD_COLLECTION_PATH:
                return {"items": []}
            return {"items": self.items}

    def _counter_value(self, kind: str, outcome: str) -> float:
        for name, value in operator_module.RECONCILE_TOTAL.samples():
            if f'kind="{kind}"' in name and f'outcome="{outcome}"' in name:
                return value
        return 0.0

    def test_reconcile_failure_is_counted_and_status_written(self) -> None:
        item = {"metadata": {"name": "site-a"}}
        operator = operator_module.Operator(self._Kube([item]))
        written: list[tuple] = []
        before = self._counter_value("sitedeployment", "failure")

        def failing(_cr: dict) -> None:
            raise RuntimeError("boom")

        with redirect_stderr(io.StringIO()):
            operator._reconcile_collection(
                "sitedeployment",
                operator_module.COLLECTION_PATH,
                failing,
                lambda *a, **k: written.append((a, k)),
            )
        self.assertEqual(self._counter_value("sitedeployment", "failure"), before + 1)
        self.assertEqual(len(written), 1, "Failure must write back the Failed status")

    def test_status_write_failure_is_counted_separately(self) -> None:
        """The status that is not written back is more difficult to detect than the failure of reconcile: CR will stop at the old status."""
        item = {"metadata": {"name": "site-b"}}
        operator = operator_module.Operator(self._Kube([item]))
        before = self._counter_value("sitedeployment", "status_write_failure")

        def failing(_cr: dict) -> None:
            raise RuntimeError("boom")

        def failing_status(*_a, **_k) -> None:
            raise RuntimeError("status api down")

        with redirect_stderr(io.StringIO()):
            operator._reconcile_collection(
                "sitedeployment",
                operator_module.COLLECTION_PATH,
                failing,
                failing_status,
            )
        self.assertEqual(
            self._counter_value("sitedeployment", "status_write_failure"), before + 1
        )

    def _progress_value(self) -> float:
        for _name, value in operator_module.LAST_PROGRESS_TIMESTAMP.samples():
            return value
        return 0.0

    def test_progress_heartbeat_advances_after_each_item(self) -> None:
        """Success and failure both count as progress: /healthz asks "is it moving", not "is it winning"."""
        items = [{"metadata": {"name": "ok"}}, {"metadata": {"name": "bad"}}]
        operator = operator_module.Operator(self._Kube(items))
        seen: list[float] = []

        def handler(cr: dict) -> None:
            # Snapshot the gauge *before* this item is handled: for the second item
            # it must already carry the first item's timestamp.
            seen.append(self._progress_value())
            if cr["metadata"]["name"] == "bad":
                raise RuntimeError("boom")

        operator_module.LAST_PROGRESS_TIMESTAMP.set(0.0)
        before = time.time()
        with redirect_stderr(io.StringIO()):
            operator._reconcile_collection(
                "sitedeployment",
                operator_module.COLLECTION_PATH,
                handler,
                lambda *a, **k: None,
            )
        self.assertEqual(seen[0], 0.0, "nothing handled yet when the first item starts")
        self.assertGreaterEqual(seen[1], before, "first item must have advanced the heartbeat")
        self.assertGreaterEqual(
            self._progress_value(), seen[1], "a failing item must also advance it"
        )

    def test_observed_resource_gauge_tracks_list_size(self) -> None:
        """A sudden drop to 0 is usually caused by the wrong namespace being typed in the list, and there are zero error logs on that path."""
        items = [{"metadata": {"name": f"s{i}"}} for i in range(3)]
        operator = operator_module.Operator(self._Kube(items))
        operator._reconcile_collection(
            "sitedeployment",
            operator_module.COLLECTION_PATH,
            lambda _cr: None,
            lambda *a, **k: None,
        )
        observed = {
            name: value
            for name, value in operator_module.OBSERVED_RESOURCES.samples()
        }
        self.assertEqual(observed['sites_operator_observed_resources{kind="sitedeployment"}'], 3)


class OperatorStopTest(unittest.TestCase):
    """run_forever must return once the stop event is set, not sleep through it."""

    def test_run_forever_returns_after_stop_is_set(self) -> None:
        stop = threading.Event()
        operator = operator_module.Operator(object(), stop=stop)
        sweeps: list[int] = []

        def one_sweep() -> None:
            sweeps.append(1)
            stop.set()

        operator.run_once = one_sweep  # type: ignore[method-assign]
        original = operator_module.RECONCILE_INTERVAL
        # Long interval on purpose: only a stop-aware wait can return in time.
        operator_module.RECONCILE_INTERVAL = 30.0
        thread = threading.Thread(target=operator.run_forever, daemon=True)
        try:
            with redirect_stderr(io.StringIO()):
                thread.start()
                thread.join(timeout=3.0)
        finally:
            operator_module.RECONCILE_INTERVAL = original
        self.assertFalse(thread.is_alive(), "run_forever must exit once stop is set")
        self.assertEqual(sweeps, [1])

    def test_default_operator_owns_its_own_stop_event(self) -> None:
        operator = operator_module.Operator(object())
        self.assertIsInstance(operator._stop, threading.Event)
        self.assertFalse(operator._stop.is_set())


class DriftResyncTest(unittest.TestCase):
    """Converged CR downscaling reiterates the desired state - **downscaling, not skipping**.

    The operator's self-healing comes from "writing back the desired state in each round": someone manually deletes the Deployment of a certain site,
    It depends on the next round of reconstruction. Skipping it completely is equivalent to trading self-healing for performance, so the focus of this set of use cases is
    "Must be re-applied after the window" and "If it has not converged, apply every round".
    """

    def _operator(self):
        op = operator_module.Operator(object())
        return op

    def _cr(self, name: str, *, generation: int = 1, phase: str = "Running",
            ready: bool = True, observed: int | None = None) -> dict:
        return {
            "metadata": {"name": name, "generation": generation},
            "status": {
                "observedGeneration": generation if observed is None else observed,
                "phase": phase,
                "ready": ready,
            },
        }

    def test_settled_resource_is_applied_once_then_skipped(self) -> None:
        op = self._operator()
        cr = self._cr("site-a")
        self.assertTrue(op._should_apply(cr, 1), "You must apply when you see it for the first time")
        self.assertFalse(op._should_apply(cr, 1), "Apply should not be repeated within the window")

    def test_window_expiry_reapplies_so_self_healing_survives(self) -> None:
        """Core assertion: The desired state must be rewritten after the window has passed.

        If this article is red, it means "the operator will never be rebuilt after manually deleting the Deployment".
        """
        op = self._operator()
        cr = self._cr("site-a")
        self.assertTrue(op._should_apply(cr, 1))
        # Push the last apply moment out of the window
        op._applied_at["site-a"] -= operator_module.DRIFT_RESYNC_SECONDS + 1
        self.assertTrue(op._should_apply(cr, 1), "Must be reapplied after the window has expired")

    def test_unsettled_resources_are_applied_every_sweep(self) -> None:
        op = self._operator()
        for phase, ready in (("Deploying", False), ("Failed", False), ("Running", False)):
            cr = self._cr("s", phase=phase, ready=ready)
            self.assertTrue(op._should_apply(cr, 1), f"{phase}/{ready} Must apply every round")
            self.assertTrue(op._should_apply(cr, 1), f"{phase}/{ready} Also want the second round")

    def test_spec_change_applies_immediately(self) -> None:
        """If the generation changes, it means that the user has changed the spec and cannot wait for the window."""
        op = self._operator()
        self.assertTrue(op._should_apply(self._cr("site-a", generation=1), 1))
        self.assertFalse(op._should_apply(self._cr("site-a", generation=1), 1))
        # spec changed: observedGeneration lags behind generation
        changed = self._cr("site-a", generation=2, observed=1)
        self.assertTrue(op._should_apply(changed, 2), "spec changes must be applied immediately")

    def test_becoming_unsettled_clears_the_timer(self) -> None:
        """When convergence → loss of connection → convergence again, the old timestamp cannot be used.

        If you continue to use it, CR will immediately resume Running because "the last apply was a long time ago"
        apply again - that's not wrong, but conversely if the timestamp is very new it will be rebuilt when it really needs to be
        Skip. Clearing is the only way to write that does not depend on timing.
        """
        op = self._operator()
        self.assertTrue(op._should_apply(self._cr("site-a"), 1))
        self.assertIn("site-a", op._applied_at, "The timing should be recorded after convergence.")
        op._should_apply(self._cr("site-a", phase="Failed", ready=False), 1)
        self.assertNotIn("site-a", op._applied_at, "The timer must be cleared when convergence is not achieved")

    def test_deleted_resources_do_not_leak_timers(self) -> None:
        """The operator is a resident process, and "small × unbounded" is still a leak."""
        op = self._operator()
        for name in ("a", "b", "c"):
            op._should_apply(self._cr(name), 1)
        self.assertEqual(len(op._applied_at), 3)
        op._forget_applied({"a"})
        self.assertEqual(set(op._applied_at), {"a"})

    def test_skip_is_counted(self) -> None:
        before = next(
            (v for _n, v in operator_module.APPLY_SKIPPED.samples()), 0.0
        )
        op = self._operator()
        cr = self._cr("counted")
        op._should_apply(cr, 1)
        op._should_apply(cr, 1)
        after = next((v for _n, v in operator_module.APPLY_SKIPPED.samples()), 0.0)
        self.assertEqual(after, before + 1)

    def test_zero_window_restores_apply_every_sweep(self) -> None:
        """Leave a knob to roll back to old behavior: no need to roll back code when something goes wrong."""
        original = operator_module.DRIFT_RESYNC_SECONDS
        operator_module.DRIFT_RESYNC_SECONDS = 0.0
        try:
            op = self._operator()
            cr = self._cr("site-a")
            self.assertTrue(op._should_apply(cr, 1))
            self.assertTrue(op._should_apply(cr, 1), "When the window is 0, it must be applied every round")
        finally:
            operator_module.DRIFT_RESYNC_SECONDS = original


class PhaseTransitionLoggingTest(unittest.TestCase):
    def test_transition_logs_carry_both_ends(self) -> None:
        """If we only remember the new status, "it has always been Failed" and "it just became Failed" are inseparable."""
        err = io.StringIO()
        original = telemetry._LOG_FORMAT
        telemetry._LOG_FORMAT = "json"
        try:
            with redirect_stderr(err):
                operator_module._log_phase_transition(
                    "sitedeployment", "site-a", "Deploying", "Failed", "timed out"
                )
        finally:
            telemetry._LOG_FORMAT = original
        record = json.loads(err.getvalue().strip())
        self.assertEqual(record["previous_phase"], "Deploying")
        self.assertEqual(record["phase"], "Failed")
        self.assertEqual(record["level"], "warn", "Transfer to Failed and must be raised to warn")
        self.assertEqual(record["name"], "site-a")

    def test_first_observation_reports_no_previous_phase(self) -> None:
        err = io.StringIO()
        original = telemetry._LOG_FORMAT
        telemetry._LOG_FORMAT = "json"
        try:
            with redirect_stderr(err):
                operator_module._log_phase_transition(
                    "sitebuild", "b-1", None, "Building", "started"
                )
        finally:
            telemetry._LOG_FORMAT = original
        record = json.loads(err.getvalue().strip())
        self.assertEqual(record["previous_phase"], "<none>")


if __name__ == "__main__":
    unittest.main(verbosity=2)
