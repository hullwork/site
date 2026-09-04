from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import chart

from sites import monitoring
from sites.naming import namespace_for_tenant


class MonitoringTests(unittest.TestCase):
    def test_cluster_query_catalog_is_fixed_and_returns_summary(self) -> None:
        with patch.object(
            monitoring, "_query_range", return_value=[{"timestamp": 100, "value": 2.5}]
        ) as query:
            response = monitoring.cluster_metrics("1h")
        self.assertTrue(response["source"]["available"])
        self.assertFalse(response["source"]["trafficAvailable"])
        self.assertIn("gateway", response["source"]["trafficReason"])
        self.assertEqual(response["summary"]["cpu"], 2.5)
        self.assertEqual(len(response["series"]), 4)
        self.assertEqual(query.call_count, 4)

    def test_application_queries_are_scoped_to_derived_namespace_and_service(self) -> None:
        captured: list[str] = []

        def fake_query(query: str, *_args: int):
            captured.append(query)
            return []

        with patch.object(monitoring, "_query_range", side_effect=fake_query):
            response = monitoring.application_metrics("local", "alice", "shop", "6h")
        self.assertEqual(
            response["identity"],
            {"merchantId": "local", "userId": "alice", "serviceName": "shop"},
        )
        self.assertTrue(all(namespace_for_tenant("local", "alice") in query for query in captured))
        self.assertTrue(all("shop" in query for query in captured))

    def test_backend_failure_is_a_structured_unavailable_response(self) -> None:
        with patch.object(
            monitoring, "_query_range", side_effect=monitoring.MonitoringError("secret URL")
        ):
            response = monitoring.cluster_metrics("24h")
        self.assertFalse(response["source"]["available"])
        self.assertEqual(response["source"]["error"], "metrics backend unavailable")
        self.assertFalse(response["source"]["trafficAvailable"])
        self.assertEqual(response["series"], [])
        self.assertNotIn("secret", str(response))

    def test_gateway_reports_traffic_metrics_as_available(self) -> None:
        with (
            patch.dict("os.environ", {"SITES_EXPOSURE_BACKEND": "gateway"}),
            patch.object(monitoring, "_query_range", return_value=[]),
        ):
            response = monitoring.cluster_metrics("1h")
        self.assertTrue(response["source"]["trafficAvailable"])
        self.assertIsNone(response["source"]["trafficReason"])
        self.assertEqual(len(response["series"]), 7)

    def test_invalid_range_and_identity_are_rejected_or_sanitized(self) -> None:
        with self.assertRaisesRegex(ValueError, "range"):
            monitoring.cluster_metrics("7d")
        captured: list[str] = []
        with patch.object(monitoring, "_query_range", side_effect=lambda query, *_: captured.append(query) or []):
            response = monitoring.application_metrics("local", "alice", 'shop"} or vector(1)', "1h")
        self.assertEqual(response["identity"]["serviceName"], "shop-or-vector-1")
        self.assertFalse(any('"} or vector' in query for query in captured))


class PrometheusManifestContractTest(unittest.TestCase):
    """The rendered 11-monitoring.yaml must agree with the metrics the code registers.

    An alert rule that names a metric the code never emits does not fail: it
    evaluates to an empty vector forever and the platform is "monitored" with
    zero coverage. Nothing in Prometheus, kubectl or the manifests notices, so
    the only place the spelling can be checked is here, against the
    `METRICS.counter/gauge/histogram("...")` registrations in src/sites/*.py.

    Same shape as test_exposure.GatewayManifestContractTest: two spellings of
    one fact, held together by a test because no runtime step joins them.
    """

    _ROOT = Path(__file__).resolve().parent.parent
    _MANIFEST = "11-monitoring.yaml"
    _ACTIVATOR_MANIFEST = "09-activator.yaml"
    _CONFIGMAP = "sites-prometheus-config"
    _EXPECTED_ALERTS = {
        "SitesControlPlaneTargetDown",
        "SitesGatewayDown",
        "SitesApiDependencyDown",
        "SitesApiSnapshotStale",
        "SitesOperatorSweepStale",
        "SitesTraceExportDrops",
        "SitesActivatorWakeFailures",
        "SitesActivatorUnhealthy",
        "SitesActivatorRouteTableStale",
    }
    # Labels an annotation may interpolate. Every one is a fixed enumeration
    # (registered at import time or added by the scraper); anything else would
    # be a path for tenant or site identifiers to leak into alert text.
    _ANNOTATION_LABELS = {"dependency", "outcome", "job", "instance", "kind"}
    _METRIC_NAME = re.compile(r"\bsites_[a-z0-9_]+")
    _REGISTRATION = re.compile(
        r"METRICS\.(?:counter|gauge|histogram)\(\s*\"(sites_[a-z0-9_]+)\""
    )

    def setUp(self) -> None:
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML is only in the dev dependency; CI is installed, but native bare python may not.")

    def _docs(self, template: str) -> list[dict]:
        return chart.documents(template)

    def _doc(self, template: str, kind: str, name: str) -> dict:
        return next(
            d for d in self._docs(template)
            if d["kind"] == kind and d["metadata"]["name"] == name
        )

    def _configmap_data(self) -> dict[str, str]:
        return self._doc(self._MANIFEST, "ConfigMap", self._CONFIGMAP)["data"]

    def _prometheus_config(self) -> dict:
        import yaml

        return yaml.safe_load(self._configmap_data()["prometheus.yml"])

    def _config_mount_path(self) -> str:
        """Where the ConfigMap lands in the container; also asserts it is a whole-directory mount."""
        deployment = self._doc(self._MANIFEST, "Deployment", "sites-prometheus")
        spec = deployment["spec"]["template"]["spec"]
        volume = next(
            v for v in spec["volumes"]
            if v.get("configMap", {}).get("name") == self._CONFIGMAP
        )
        (container,) = spec["containers"]
        mount = next(m for m in container["volumeMounts"] if m["name"] == volume["name"])
        # A subPath mount projects exactly one key; every other key in the
        # ConfigMap (the rules file) would silently not exist in the container.
        self.assertNotIn("subPath", mount)
        config_arg = next(a for a in container["args"] if a.startswith("--config.file="))
        self.assertEqual(
            str(Path(config_arg.split("=", 1)[1]).parent), mount["mountPath"],
            "--config.file must live in the ConfigMap mount, or rule paths drift",
        )
        return mount["mountPath"]

    def _alert_rules(self) -> list[dict]:
        import yaml

        data = self._configmap_data()
        rules: list[dict] = []
        for rule_file in self._prometheus_config().get("rule_files", []):
            body = yaml.safe_load(data[Path(rule_file).name])
            for group in body["groups"]:
                rules.extend(r for r in group["rules"] if "alert" in r)
        return rules

    def _registered_metric_names(self) -> set[str]:
        names: set[str] = set()
        for source in sorted((self._ROOT / "src" / "sites").glob("*.py")):
            names.update(self._REGISTRATION.findall(source.read_text()))
        # Histograms are exposed as the _bucket/_sum/_count trio, never under
        # the bare name, so rules may legitimately reference those.
        for name in list(names):
            names.update({f"{name}_bucket", f"{name}_sum", f"{name}_count"})
        # Self-check: an empty scan would let any expression through.
        self.assertIn("sites_api_dependency_up", names)
        return names

    def test_rule_files_are_declared_and_mounted_next_to_the_config(self) -> None:
        config = self._prometheus_config()
        data = self._configmap_data()
        mount_path = self._config_mount_path()
        self.assertTrue(config.get("rule_files"), "prometheus.yml declares no rule_files: zero alerting")
        for rule_file in config["rule_files"]:
            path = Path(rule_file)
            self.assertIn(path.name, data, f"{rule_file} is not a key of ConfigMap {self._CONFIGMAP}")
            if path.is_absolute():
                self.assertEqual(str(path.parent), mount_path)

    def test_alert_rules_parse_with_expr_for_severity_and_annotations(self) -> None:
        rules = self._alert_rules()
        self.assertEqual({r["alert"] for r in rules}, self._EXPECTED_ALERTS)
        for rule in rules:
            with self.subTest(alert=rule["alert"]):
                self.assertTrue(str(rule.get("expr", "")).strip())
                self.assertIn("for", rule)
                self.assertIn(rule["labels"]["severity"], {"critical", "warning"})
                self.assertTrue(rule["annotations"]["summary"])
                self.assertTrue(rule["annotations"]["description"])
                for text in rule["annotations"].values():
                    used = set(re.findall(r"\$labels\.([a-z_]+)", text))
                    self.assertLessEqual(used, self._ANNOTATION_LABELS, text)

    def test_alert_expressions_only_use_metric_names_the_code_registers(self) -> None:
        registered = self._registered_metric_names()
        seen: set[str] = set()
        for rule in self._alert_rules():
            # `up`-only rules (target down) legitimately read no sites_* series.
            used = set(self._METRIC_NAME.findall(rule["expr"]))
            seen |= used
            with self.subTest(alert=rule["alert"]):
                self.assertLessEqual(
                    used, registered,
                    f"unregistered metric names in expr: {sorted(used - registered)}",
                )
        # Self-check: a regex that matches nothing would pass every rule.
        self.assertTrue(seen)

    def test_scrape_jobs_cover_the_targets_the_rules_name(self) -> None:
        config = self._prometheus_config()
        jobs = {j["job_name"]: j for j in config["scrape_configs"]}
        # The `up{job=~...}` regex in SitesControlPlaneTargetDown is only as
        # good as the jobs that exist: a job missing from scrape_configs has no
        # `up` series at all and can never match `== 0`.
        for job in ("sites-api-local", "sites-operator-local", "sites-activator-local"):
            self.assertIn(job, jobs)
        target_down = next(r for r in self._alert_rules() if r["alert"] == "SitesControlPlaneTargetDown")
        job_regex = re.search(r'job=~"([^"]+)"', target_down["expr"]).group(1)
        for job in ("sites-api-local", "sites-operator-local", "sites-activator-local"):
            self.assertRegex(job, f"^(?:{job_regex})$")

    def test_gateway_alert_has_a_scrape_job_that_can_produce_its_up_series(self) -> None:
        """SitesGatewayDown reads `up` for the envoy job; without the job there is no series.

        This is the alert whose absence let a readiness-only gateway death run
        for 15 hours: the control plane stayed green throughout. It used to live
        in a consumer's monitoring stack; moving it in here makes this
        repository the only place the expression and the scrape job can be
        checked against each other.
        """
        jobs = {j["job_name"] for j in self._prometheus_config()["scrape_configs"]}
        rule = next(r for r in self._alert_rules() if r["alert"] == "SitesGatewayDown")
        job_regex = re.search(r'job=~"([^"]+)"', rule["expr"]).group(1)
        matching = [job for job in jobs if re.fullmatch(job_regex, job)]
        self.assertTrue(
            matching,
            f"no scrape_config job matches {job_regex!r}; the rule can never fire",
        )

    def test_importable_rule_artifact_matches_the_bundled_configmap(self) -> None:
        """observability/alerts/sites-rules.yaml is generated; drift means an operator imports rules we do not run.

        The bundled Prometheus loads the ConfigMap, an external one imports the
        PrometheusRule. Nothing at runtime joins the two, so editing the
        ConfigMap and forgetting `observability/scripts/render-rules.py` would
        ship an operator a stale rule set that still looks authoritative.
        """
        import yaml

        artifact = self._ROOT / "observability" / "alerts" / "sites-rules.yaml"
        self.assertTrue(artifact.exists(), f"{artifact} is missing")
        rule = yaml.safe_load(artifact.read_text())
        self.assertEqual(rule["kind"], "PrometheusRule")
        bundled = yaml.safe_load(self._configmap_data()["sites-alerts.yml"])["groups"]
        self.assertEqual(
            rule["spec"]["groups"], bundled,
            "run: python3 observability/scripts/render-rules.py",
        )

    def test_activator_scrape_target_matches_the_admin_service_port(self) -> None:
        jobs = {j["job_name"]: j for j in self._prometheus_config()["scrape_configs"]}
        (target,) = jobs["sites-activator-local"]["static_configs"][0]["targets"]
        host, _, port = target.rpartition(":")
        service = self._doc(self._ACTIVATOR_MANIFEST, "Service", "sites-activator")
        admin = next(p for p in service["spec"]["ports"] if p["name"] == "admin")
        self.assertEqual(
            host, f"{service['metadata']['name']}.{service['metadata']['namespace']}.svc"
        )
        self.assertEqual(int(port), admin["port"])


class ReadinessDisclosureTests(unittest.TestCase):
    """/readyz answers before authentication, so its body is public.

    The database host is diagnosis for an operator and topology for everyone
    else. The class of failure stays in the response - that is what readiness is
    reporting - and the address moves to the log, where reading it already needs
    cluster access. Asserted over real HTTP because the thing under test is what
    an unauthenticated stranger receives.
    """

    SECRET_HOST = "sites-postgres.sites-local.svc.cluster.local"

    class _FailingStore:
        backend = "postgresql"

        def __init__(self, host: str) -> None:
            self._host = host

        def ping(self) -> None:
            from sites.storage import StorageError

            raise StorageError(
                f'connection to server at "{self._host}", port 5432 failed: '
                "Connection refused"
            )

    @classmethod
    def setUpClass(cls) -> None:
        import threading
        from http.server import ThreadingHTTPServer

        from sites.api import Handler

        Handler.kube = None
        Handler.store = cls._FailingStore(cls.SECRET_HOST)
        Handler.service_token = "a" * 32
        Handler.session_key = "k" * 32
        Handler.local_login_enabled = True
        Handler.oidc_config = None
        Handler.mutation_lock = threading.Lock()
        Handler.synchronizer = None
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}/readyz"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def _readyz(self):
        import json
        import urllib.error
        import urllib.request

        try:
            with urllib.request.urlopen(self.url, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_the_probe_actually_reached_the_database_failure_branch(self) -> None:
        """Self-check: a 200 would make every assertion below vacuous."""
        status, _ = self._readyz()
        self.assertEqual(status, 503)

    def test_the_database_host_is_not_in_the_response(self) -> None:
        _, body = self._readyz()
        self.assertNotIn(self.SECRET_HOST, json.dumps(body))
        self.assertIn("<redacted>", body["checks"]["database"]["error"])

    def test_the_class_of_failure_survives_redaction(self) -> None:
        """Readiness has to stay diagnosable; redacting must not cost the reason."""
        _, body = self._readyz()
        database = body["checks"]["database"]
        self.assertFalse(database["ok"])
        self.assertEqual(database["error_type"], "StorageError")
        self.assertIn("Connection refused", database["error"])


class RedactionTests(unittest.TestCase):
    def test_addresses_are_removed_and_sentences_are_kept(self) -> None:
        from sites import telemetry

        self.assertNotIn("10.0.0.5", telemetry.redact_endpoints(
            'connection to server at "10.0.0.5", port 5432 failed'
        ))
        self.assertNotIn("pg.internal.example", telemetry.redact_endpoints(
            "failed to resolve host 'pg.internal.example'"
        ))
        self.assertNotIn("http://minio:9000", telemetry.redact_endpoints(
            "cannot reach http://minio:9000/probe"
        ))
        # A message with no address shape is untouched: over-redaction is safe,
        # but there is no reason to mangle a plain sentence.
        self.assertEqual(
            telemetry.redact_endpoints("database unreachable"),
            "database unreachable",
        )

    def test_long_messages_are_truncated(self) -> None:
        """A DSN or a SQL statement should not be walkable out one probe at a time."""
        from sites import telemetry

        redacted = telemetry.redact_endpoints("x" * 500)
        self.assertLess(len(redacted), 500)
        self.assertTrue(redacted.endswith("..."))


if __name__ == "__main__":
    unittest.main()
