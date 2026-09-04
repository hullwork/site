"""Contracts for reproducible, fail-closed benchmark evidence."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "scripts" / "run-benchmark.py"
sys.path.insert(0, str(ROOT / "scripts"))


def load_runner():
    spec = importlib.util.spec_from_file_location("site_benchmark_runner", RUNNER)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load benchmark runner")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BenchmarkContractTests(unittest.TestCase):
    def test_cluster_driver_uses_the_product_service_token_header(self) -> None:
        from cluster_benchmark import ActualClusterDriver, ClusterConfig

        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self): return b"{}"

        driver = ActualClusterDriver(ClusterConfig(
            context="isolated-kubeadm", namespace="sites-benchmark-test", trials=60,
            control_image="registry.example/control@sha256:" + "a" * 64,
            dynamic_image="registry.example/fixture@sha256:" + "b" * 64,
        ))
        driver.base_url = "http://127.0.0.1:8080"
        with mock.patch("cluster_benchmark.urllib.request.urlopen", return_value=Response()) as opened:
            driver.request("GET", "/readyz", "service-token")
        request = opened.call_args.args[0]
        self.assertEqual("service-token", request.get_header("X-sites-service-token"))
        self.assertIsNone(request.get_header("Authorization"))

    def test_cluster_driver_recovers_one_broken_port_forward(self) -> None:
        import urllib.error
        from cluster_benchmark import ActualClusterDriver, ClusterConfig

        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self): return b"{}"

        driver = ActualClusterDriver(ClusterConfig(
            context="isolated-kubeadm", namespace="sites-benchmark-test", trials=60,
            control_image="registry.example/control@sha256:" + "a" * 64,
            dynamic_image="registry.example/fixture@sha256:" + "b" * 64,
        ))
        driver.context_verified = True
        driver.base_url = "http://127.0.0.1:8080"
        with (
            mock.patch(
                "cluster_benchmark.urllib.request.urlopen",
                side_effect=[urllib.error.URLError("closed"), Response()],
            ),
            mock.patch.object(driver, "_start_port_forward") as restart,
        ):
            self.assertEqual({}, driver.request("GET", "/readyz", "service-token"))
        restart.assert_called_once_with(reason="transport-recovery")
        self.assertEqual("httpTransportError", driver.events[0]["kind"])

    def test_rollback_moves_durable_pointer_before_redeploying_old_version(self) -> None:
        from cluster_benchmark import ActualClusterDriver, ClusterConfig

        driver = ActualClusterDriver(ClusterConfig(
            context="isolated-kubeadm", namespace="sites-benchmark-test", trials=60,
            control_image="registry.example/control@sha256:" + "a" * 64,
            dynamic_image="registry.example/fixture@sha256:" + "b" * 64,
        ))
        driver.tenant_token = "tenant-token"
        paths: list[str] = []

        def request(method, path, _token, payload=None, expected=(200)):
            paths.append(path)
            if path.endswith("/versions") and method == "POST":
                return {"version": 2}
            if path.endswith("/promote") and method == "POST":
                self.assertEqual({"version": 1}, payload)
                return {"currentVersion": 1}
            if path == "/v1/deployments":
                return {"revision": "revision"}
            raise AssertionError((method, path, payload, expected))

        driver.request = request
        driver._wait_ready = lambda _name, version: {"siteVersion": version}
        driver._wait_version_pointers = lambda _name, version: {
            "currentVersion": version, "deployedVersion": version,
        }
        result = driver.rollback_recovery(1, {
            "name": "dynamic-1", "version": {"version": 1},
        })
        self.assertEqual(1, result["versions"]["currentVersion"])
        promote_index = paths.index("/v1/sites/dynamic-1/promote")
        rollback_deploy_index = len(paths) - 1
        self.assertLess(promote_index, rollback_deploy_index)

    def test_kubernetes_secret_token_trims_file_newline_and_rejects_controls(self) -> None:
        import base64
        from cluster_benchmark import ClusterFailure, decode_kubernetes_secret

        encoded = base64.b64encode(b"service-token\n").decode("ascii")
        self.assertEqual("service-token", decode_kubernetes_secret(encoded))
        with self.assertRaises(ClusterFailure):
            decode_kubernetes_secret(base64.b64encode(b"bad\ntoken").decode("ascii"))

    def test_cluster_finish_reclaims_pvs_before_removing_the_provisioner(self) -> None:
        from cluster_benchmark import ActualClusterDriver, ClusterConfig

        driver = ActualClusterDriver(ClusterConfig(
            context="isolated-kubeadm", namespace="sites-benchmark-test", trials=60,
            control_image="registry.example/control@sha256:" + "a" * 64,
            dynamic_image="registry.example/fixture@sha256:" + "b" * 64,
        ))
        driver.context_verified = True
        driver.namespace_created_by_this_run = True
        calls: list[list[str]] = []

        def command(argv, **_kwargs):
            calls.append(argv)
            stdout = ""
            if argv == [
                "kubectl", "get", "namespace", "sites-benchmark-test",
                "--ignore-not-found", "-o", "name",
            ]:
                stdout = "namespace/sites-benchmark-test\n"
            elif argv[-4:] == ["get", "pvc", "-o", "json"]:
                stdout = json.dumps({"items": [
                    {"spec": {"volumeName": "pv-postgres"}},
                    {"spec": {"volumeName": "pv-registry"}},
                ]})
            return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

        driver.command = command
        with mock.patch("cluster_benchmark.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0)
            result = driver.finish()

        workload_scale = next(index for index, argv in enumerate(calls) if "scale" in argv)
        pod_delete = next(index for index, argv in enumerate(calls) if "pod" in argv and "delete" in argv)
        pvc_delete = next(index for index, argv in enumerate(calls) if "pvc" in argv and "delete" in argv)
        uninstall = next(index for index, argv in enumerate(calls) if "standalone.sh" in " ".join(argv))
        self.assertLess(workload_scale, pod_delete)
        self.assertLess(pod_delete, pvc_delete)
        self.assertLess(pvc_delete, uninstall)
        self.assertEqual(["pv-postgres", "pv-registry"], result["persistentVolumesDeleted"])

    DESTRUCTIVE = ("delete", "scale", "uninstall")

    def _driver(self):
        from cluster_benchmark import ActualClusterDriver, ClusterConfig

        return ActualClusterDriver(ClusterConfig(
            context="isolated-kubeadm", namespace="sites-benchmark-test", trials=60,
            control_image="registry.example/control@sha256:" + "a" * 64,
            dynamic_image="registry.example/fixture@sha256:" + "b" * 64,
        ))

    def _recorder(self, calls, *, namespace_exists):
        def command(argv, **_kwargs):
            calls.append(argv)
            stdout = ""
            if argv == ["kubectl", "config", "current-context"]:
                stdout = "isolated-kubeadm\n"
            elif argv[:3] == ["kubectl", "get", "namespace"] and namespace_exists:
                stdout = "namespace/sites-benchmark-test\n"
            return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

        return command

    def _destructive(self, calls):
        return [argv for argv in calls if any(
            word in " ".join(argv) for word in self.DESTRUCTIVE
        )]

    def test_cluster_finish_spares_a_namespace_this_run_did_not_create(self) -> None:
        """The namespace exists, but nothing here brought it into existence.

        This is the shape that costs data: `finish` used to require only a
        verified context plus a namespace that exists now, and both hold for a
        namespace that merely belongs to somebody else.
        """
        from cluster_benchmark import ClusterFailure

        driver = self._driver()
        calls: list[list[str]] = []
        driver.command = self._recorder(calls, namespace_exists=True)

        # prepare() must refuse, and must not leave the run holding a licence to
        # destroy the namespace it just refused.
        with self.assertRaises(ClusterFailure) as refusal:
            driver.prepare()
        self.assertIn("already exists", str(refusal.exception))
        self.assertFalse(driver.namespace_created_by_this_run)

        with mock.patch("cluster_benchmark.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0)
            result = driver.finish()

        self.assertFalse(result["namespaceDeleted"])
        self.assertEqual([], self._destructive(calls))

    def test_cluster_finish_spares_the_namespace_even_with_a_verified_context(self) -> None:
        """Independent of statement order in prepare().

        The reordering that stops context_verified being set before the
        existence check is one guard; this pins the other, so moving that
        assignment back would not quietly reopen the hole.
        """
        driver = self._driver()
        driver.context_verified = True
        self.assertFalse(driver.namespace_created_by_this_run)
        calls: list[list[str]] = []
        driver.command = self._recorder(calls, namespace_exists=True)

        with mock.patch("cluster_benchmark.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0)
            result = driver.finish()

        self.assertFalse(result["namespaceDeleted"])
        self.assertEqual(
            "namespace was not created by this run", result["reason"]
        )
        self.assertEqual([], self._destructive(calls))

    def test_contract_profile_emits_environment_and_passes_frozen_thresholds(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(RUNNER), "--profile", "contract"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertTrue(result["valid"])
        self.assertTrue(result["passed"])
        self.assertEqual(1.0, result["summary"]["successRate"])
        self.assertEqual(0, result["summary"]["notRun"])
        self.assertRegex(result["environment"]["gitCommit"], r"^[0-9a-f]{40}$")
        self.assertIn("platform", result["environment"])
        self.assertGreater(len(result["unscoredStages"]), 0)

    def test_scored_missing_or_not_run_stage_fails_closed(self) -> None:
        runner = load_runner()
        summary = runner.summarize([
            {"stage": "required", "status": "not-run"},
        ])
        thresholds = json.loads(
            (ROOT / "evaluation" / "thresholds.v1.json").read_text()
        )["profiles"]["contract"]
        self.assertFalse(runner.accepted(summary, thresholds))

    def test_every_declared_threshold_is_read_by_the_code_that_scores_it(self) -> None:
        """A threshold nothing evaluates is worse than no threshold at all.

        thresholds.v1.json carried an `excellent` block of nine criteria that
        no code read: the README described it as predeclared and not to be
        relaxed, so a reader took it for a quality bar the project was held
        to, while the accept decision never consulted one of them. Six of the
        nine had no underlying measurement in the benchmark at all -- no
        attempt/retry model, no safety stage, no dormant-wake stage, no
        multi-run aggregation, no oracle -- so they could not have been
        evaluated even in principle.

        This pins the property rather than the deletion: every key under
        `profiles` must be named by the module that scores that profile.
        """
        document = json.loads(
            (ROOT / "evaluation" / "thresholds.v1.json").read_text()
        )
        self.assertEqual({"schemaVersion", "benchmarkVersion", "profiles"}, set(document))
        scorers = {
            "contract": (ROOT / "scripts" / "run-benchmark.py").read_text(encoding="utf-8"),
            "cluster": (ROOT / "scripts" / "cluster_benchmark.py").read_text(encoding="utf-8"),
        }
        self.assertEqual(set(scorers), set(document["profiles"]))
        for profile, thresholds in document["profiles"].items():
            for key in thresholds:
                with self.subTest(profile=profile, threshold=key):
                    self.assertIn(
                        f'"{key}"', scorers[profile],
                        f"{profile}.{key} is declared but never read; it gates nothing",
                    )

    def test_output_file_matches_required_result_schema_fields(self) -> None:
        schema = json.loads((ROOT / "evaluation" / "result.schema.json").read_text())
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.json"
            completed = subprocess.run(
                [sys.executable, str(RUNNER), "--profile", "contract", "--output", str(output)],
                cwd=ROOT,
                check=False,
                timeout=30,
            )
            self.assertEqual(0, completed.returncode)
            result = json.loads(output.read_text())
        self.assertEqual(set(schema["required"]) - set(result), set())
        self.assertEqual(schema["properties"]["schemaVersion"]["const"], result["schemaVersion"])

    def test_unimplemented_agent_profile_cannot_be_selected(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(RUNNER), "--profile", "agent"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("invalid choice", completed.stderr)

    def test_cluster_dry_run_is_a_non_evidence_plan_and_never_contacts_kubernetes(self) -> None:
        digest_a = "sha256:" + "a" * 64
        digest_b = "sha256:" + "b" * 64
        completed = subprocess.run([
            sys.executable, str(RUNNER), "--profile", "cluster", "--dry-run",
            "--context", "isolated-kubeadm", "--namespace", "sites-benchmark-test",
            "--trials", "60", "--control-image", f"registry.example/control@{digest_a}",
            "--dynamic-image", f"registry.example/fixture@{digest_b}",
        ], cwd=ROOT, check=False, capture_output=True, text=True)
        self.assertEqual(0, completed.returncode, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertTrue(result["dryRun"])
        self.assertFalse(result["valid"])
        self.assertFalse(result["passed"])
        self.assertEqual(60, result["trials"])
        self.assertIn("cleanup", " ".join(result["scoredStages"]).lower())

    def test_cluster_profile_accepts_sixty_real_driver_trials_and_records_raw_evidence(self) -> None:
        from cluster_benchmark import ClusterConfig, run

        class FakeDriver:
            def __init__(self) -> None:
                self.events = [{"kind": "fake-driver", "fixture": True}]
            def prepare(self): return {"cleanNamespace": True}
            def static_publish(self, trial): return {"name": f"static-{trial}"}
            def dynamic_publish(self, trial):
                revision = f"revision-{trial}"
                return {"name": f"dynamic-{trial}", "version": {"version": 1}, "observed": {"revision": revision, "verification": {"ok": True, "revision": revision}}}
            def rollback_recovery(self, trial, dynamic): return {"currentVersion": 1, "deployedVersion": 1}
            def cleanup_trial(self, names): return {"deleted": names}
            def finish(self): return {"namespaceDeleted": True}

        result = run(ClusterConfig(
            context="isolated-kubeadm", namespace="sites-benchmark-test", trials=60,
            control_image="registry.example/control@sha256:" + "a" * 64,
            dynamic_image="registry.example/fixture@sha256:" + "b" * 64,
        ), FakeDriver())
        self.assertTrue(result["valid"])
        self.assertTrue(result["passed"])
        self.assertEqual(60, result["summary"]["validTrials"])
        self.assertEqual(0, result["summary"]["notRun"])
        self.assertEqual(300, result["summary"]["passed"])
        self.assertEqual(1.0, result["summary"]["revisionMatchedVerificationRate"])
        self.assertEqual("fake-driver", result["rawEvents"][0]["kind"])

    def test_cluster_profile_fails_closed_after_driver_failure(self) -> None:
        from cluster_benchmark import ClusterConfig, ClusterFailure, run

        class FailingDriver:
            events = []
            def prepare(self): return {"cleanNamespace": True}
            def static_publish(self, trial): return {"name": f"static-{trial}"}
            def dynamic_publish(self, trial): raise ClusterFailure("fixture failed")
            def cleanup_trial(self, names): return {"deleted": names}
            def finish(self): return {"namespaceDeleted": True}

        result = run(ClusterConfig(
            context="isolated-kubeadm", namespace="sites-benchmark-test", trials=60,
            control_image="registry.example/control@sha256:" + "a" * 64,
            dynamic_image="registry.example/fixture@sha256:" + "b" * 64,
        ), FailingDriver())
        self.assertFalse(result["valid"])
        self.assertFalse(result["passed"])
        self.assertGreater(result["summary"]["failed"], 0)
        self.assertGreater(result["summary"]["notRun"], 0)


if __name__ == "__main__":
    unittest.main()
