#!/usr/bin/env python3
"""Real, isolated Kubernetes benchmark driver for site."""
from __future__ import annotations

import base64
import hashlib
import json
import platform
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from sites.validation import STATIC_IMAGE


ROOT = Path(__file__).resolve().parents[1]
IMAGE = re.compile(r"^(?P<repository>[^@]+)@(?P<digest>sha256:[0-9a-f]{64})$")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * fraction + 0.999999)))
    return round(ordered[rank], 3)


def chart_digest() -> str:
    digest = hashlib.sha256()
    for path in sorted((ROOT / "charts" / "site").rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(ROOT).as_posix().encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def tool_version(command: str, *args: str) -> str | None:
    try:
        completed = subprocess.run([command, *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (completed.stdout or completed.stderr).strip().splitlines()[0] if (completed.stdout or completed.stderr).strip() else None


@dataclass(frozen=True)
class ClusterConfig:
    context: str
    namespace: str
    trials: int
    control_image: str
    dynamic_image: str
    timeout_seconds: int = 600


def plan(config: ClusterConfig) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "profile": "cluster",
        "dryRun": True,
        "valid": False,
        "passed": False,
        "reason": "plan only; no benchmark evidence was collected",
        "context": config.context,
        "namespace": config.namespace,
        "trials": config.trials,
        "images": {
            "control": config.control_image,
            "dynamicFixture": config.dynamic_image,
            "staticRuntime": STATIC_IMAGE,
        },
        "lifecycle": [
            "verify explicit kubectl context and empty generated namespace",
            "run deterministic scaffold contract evaluator",
            "bootstrap existing Secrets and install the product Helm Chart",
            "run standalone rollout and /readyz smoke",
            "create isolated merchant and tenant",
            "for each trial: static publish, dynamic version publish, revision-match verification, version rollback/recovery, workload cleanup",
            "uninstall Helm release and delete only the generated namespace",
        ],
        "scoredStages": [
            "staticPublish", "dynamicPublish", "revisionMatch", "rollbackRecovery", "cleanup"
        ],
        "acceptance": "at least 60 valid trials; any failed, blocked, or not-run stage fails closed",
    }


class ClusterFailure(RuntimeError):
    pass


def decode_kubernetes_secret(encoded: str) -> str:
    """Decode a text Secret without leaking kubectl/file trailing newlines."""
    try:
        value = base64.b64decode(encoded, validate=True).decode("utf-8").strip()
    except (ValueError, UnicodeDecodeError) as exc:
        raise ClusterFailure("Kubernetes Secret is not valid base64 UTF-8 text") from exc
    if not value or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ClusterFailure("Kubernetes Secret contains an empty or unsafe token")
    return value


class ActualClusterDriver:
    def __init__(self, config: ClusterConfig) -> None:
        self.config = config
        self.events: list[dict[str, Any]] = []
        self.base_url = ""
        self.admin_token = ""
        self.tenant_token = ""
        self.forward: subprocess.Popen[bytes] | None = None
        self._redactions: set[str] = set()
        self.context_verified = False
        # Kept separate from context_verified on purpose. One flag answering both
        # "am I talking to the right cluster" and "may I destroy this namespace"
        # is what let a namespace the run explicitly refused to use be deleted.
        self.namespace_created_by_this_run = False
        self.runtime_environment: dict[str, Any] = {}

    def _start_port_forward(self, *, reason: str) -> None:
        if self.forward is not None:
            self.forward.terminate()
            try:
                self.forward.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.forward.kill()
                self.forward.wait(timeout=5)
        port = self._free_port()
        started = now()
        self.forward = subprocess.Popen(
            ["kubectl", "-n", self.config.namespace, "port-forward", "service/sites-api", f"{port}:8080"],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.base_url = f"http://127.0.0.1:{port}"
        for _ in range(60):
            if self.forward.poll() is not None:
                raise ClusterFailure("benchmark API port-forward exited before becoming ready")
            try:
                with urllib.request.urlopen(self.base_url + "/readyz", timeout=1) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.25)
        else:
            raise ClusterFailure("benchmark API port-forward did not become ready")
        self.events.append({
            "kind": "portForward", "startedAt": started, "finishedAt": now(),
            "localPort": port, "ready": True, "reason": reason,
        })

    def _clean(self, value: str) -> str:
        for secret in self._redactions:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value[-4000:]

    def command(self, argv: list[str], *, record_output: bool = True) -> subprocess.CompletedProcess[str]:
        started = time.monotonic()
        timestamp = now()
        completed = subprocess.run(
            argv, cwd=ROOT, capture_output=True, text=True, check=False,
            timeout=self.config.timeout_seconds,
        )
        event = {
            "kind": "command", "startedAt": timestamp, "finishedAt": now(),
            "durationMs": round((time.monotonic() - started) * 1000, 3),
            "argv": argv, "exitCode": completed.returncode,
        }
        if record_output:
            event.update(stdout=self._clean(completed.stdout), stderr=self._clean(completed.stderr))
        else:
            event.update(stdout="[REDACTED]", stderr=self._clean(completed.stderr))
        self.events.append(event)
        if completed.returncode:
            raise ClusterFailure(f"command failed ({completed.returncode}): {' '.join(argv)}")
        return completed

    def request(self, method: str, path: str, token: str, payload: dict[str, Any] | None = None, expected: tuple[int, ...] = (200,)) -> dict[str, Any]:
        data = json.dumps(payload).encode() if payload is not None else None
        for attempt in range(2):
            request = urllib.request.Request(
                self.base_url + path, data=data, method=method,
                headers={"X-Sites-Service-Token": token, "Content-Type": "application/json"},
            )
            started = time.monotonic()
            timestamp = now()
            status = 0
            body: dict[str, Any] = {}
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    status = response.status
                    body = json.loads(response.read().decode() or "{}")
            except urllib.error.HTTPError as exc:
                status = exc.code
                try:
                    body = json.loads(exc.read().decode() or "{}")
                except json.JSONDecodeError:
                    body = {"error": "non-JSON HTTP error"}
            except (urllib.error.URLError, OSError) as exc:
                self.events.append({
                    "kind": "httpTransportError", "startedAt": timestamp,
                    "finishedAt": now(), "method": method, "path": path,
                    "errorType": type(exc).__name__, "attempt": attempt + 1,
                })
                if attempt == 0 and self.context_verified:
                    self._start_port_forward(reason="transport-recovery")
                    continue
                raise ClusterFailure(
                    f"{method} {path} transport failed after recovery"
                ) from exc
            break
        sanitized = {key: value for key, value in body.items() if key not in {"token", "apiKey"}}
        request_payload = (
            {key: value for key, value in payload.items() if key not in {"token", "apiKey"}}
            if payload is not None else None
        )
        self.events.append({
            "kind": "http", "startedAt": timestamp, "finishedAt": now(),
            "durationMs": round((time.monotonic() - started) * 1000, 3),
            "method": method, "path": path, "status": status,
            "request": request_payload, "response": sanitized,
        })
        if status not in expected:
            raise ClusterFailure(f"{method} {path} returned {status}: {sanitized}")
        return body

    def _free_port(self) -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def prepare(self) -> dict[str, Any]:
        current = self.command(["kubectl", "config", "current-context"]).stdout.strip()
        if current != self.config.context:
            raise ClusterFailure(f"current context {current!r} does not match explicit context {self.config.context!r}")
        namespace = self.command(
            ["kubectl", "get", "namespace", self.config.namespace, "--ignore-not-found", "-o", "name"]
        ).stdout.strip()
        if namespace:
            raise ClusterFailure(f"benchmark namespace already exists: {self.config.namespace}")
        self.context_verified = True

        contract = self.command([
            sys.executable, str(ROOT / "scripts" / "evaluate-scaffolds.py")
        ])
        contract_result = json.loads(contract.stdout)
        if contract_result["summary"]["failed"] != 0 or contract_result["summary"]["localCheckSuccessRate"] != 1.0:
            raise ClusterFailure("scaffold contract evaluator did not pass at 100%")

        control = IMAGE.fullmatch(self.config.control_image)
        assert control is not None
        # Set before the install, not after: a partially installed namespace is
        # still one this run brought into existence, and cleanup must reclaim it.
        # Everything the namespace could have contained beforehand was ruled out
        # by the existence check above.
        self.namespace_created_by_this_run = True
        self.command([
            str(ROOT / "scripts" / "standalone.sh"), "install",
            "--namespace", self.config.namespace,
            "--set", f"images.control.repository={control.group('repository')}",
            "--set", f"images.control.digest={control.group('digest')}",
        ])
        self.command([
            str(ROOT / "scripts" / "standalone.sh"), "smoke", "--namespace", self.config.namespace
        ])
        kubernetes = self.command(["kubectl", "version", "-o", "json"])
        postgres_image = self.command([
            "kubectl", "-n", self.config.namespace, "get", "statefulset", "sites-postgres",
            "-o", "jsonpath={.spec.template.spec.containers[0].image}",
        ]).stdout.strip()
        self.runtime_environment = {
            "kubernetesVersion": json.loads(kubernetes.stdout),
            "postgresImage": postgres_image,
        }

        encoded = self.command([
            "kubectl", "-n", self.config.namespace, "get", "secret", "sites-api-token",
            "-o", "jsonpath={.data.token}",
        ], record_output=False).stdout.strip()
        self.admin_token = decode_kubernetes_secret(encoded)
        self._redactions.add(self.admin_token)

        self._start_port_forward(reason="initial")

        merchant = self.request("POST", "/v1/merchants", self.admin_token, {
            "merchantId": "benchmark", "displayName": "Benchmark",
            "maxTenants": 1, "maxDeployments": 10,
        }, (201,))
        tenant = self.request("POST", "/v1/tenants", self.admin_token, {
            "merchantId": "benchmark", "userId": "runner",
            "maxDeployments": 10, "maxPublicRoutes": 0,
        }, (201,))
        self._redactions.add(str(merchant["apiKey"]))
        self.tenant_token = str(tenant["token"])
        self._redactions.add(self.tenant_token)
        return {"scaffoldContract": contract_result["summary"]}

    def _wait_ready(self, name: str, expected_version: int | None = None) -> dict[str, Any]:
        deadline = time.monotonic() + self.config.timeout_seconds
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = self.request("GET", f"/v1/deployments/{name}", self.tenant_token)
            verification = last.get("verification") or {}
            if (
                last.get("ready") is True
                and verification.get("ok") is True
                and verification.get("revision") == last.get("revision")
                and (expected_version is None or last.get("siteVersion") == expected_version)
            ):
                return last
            if last.get("phase") == "Failed":
                raise ClusterFailure(f"deployment {name} failed: {last.get('message')}")
            time.sleep(1)
        raise ClusterFailure(f"deployment {name} did not reach revision-matched readiness: {last}")

    def _wait_version_pointers(self, name: str, expected_version: int) -> dict[str, Any]:
        deadline = time.monotonic() + self.config.timeout_seconds
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = self.request("GET", f"/v1/sites/{name}/versions", self.tenant_token)
            if (
                last.get("currentVersion") == expected_version
                and last.get("deployedVersion") == expected_version
            ):
                return last
            time.sleep(1)
        raise ClusterFailure(
            f"version pointers did not converge to v{expected_version}: {last}"
        )

    def static_publish(self, trial: int) -> dict[str, Any]:
        name = f"static-{trial}"
        accepted = self.request("POST", "/v1/deployments", self.tenant_token, {
            "name": name, "image": "ignored-for-static", "port": 8080,
            "healthPath": "/", "exposure": "internal",
            "artifact": {"files": {"index.html": f"<h1>trial {trial}</h1>"}},
        }, (202,))
        ready = self._wait_ready(name)
        return {"name": name, "acceptedRevision": accepted["revision"], "observed": ready}

    def dynamic_publish(self, trial: int) -> dict[str, Any]:
        name = f"dynamic-{trial}"
        content_v1 = hashlib.sha256(f"dynamic-{trial}-v1".encode()).hexdigest()
        version = self.request("POST", f"/v1/sites/{name}/versions", self.tenant_token, {
            "siteType": "dynamic", "contentSha256": content_v1,
            "image": self.config.dynamic_image, "schemaChange": "none",
            "decisionRationale": "fixed cluster benchmark fixture",
        }, (201,))
        accepted = self.request("POST", "/v1/deployments", self.tenant_token, {
            "name": name, "image": self.config.dynamic_image, "port": 8080,
            "healthPath": "/", "exposure": "internal", "siteVersion": version["version"],
        }, (202,))
        ready = self._wait_ready(name, int(version["version"]))
        return {"name": name, "version": version, "acceptedRevision": accepted["revision"], "observed": ready}

    def rollback_recovery(self, trial: int, dynamic: dict[str, Any]) -> dict[str, Any]:
        name = dynamic["name"]
        v1 = int(dynamic["version"]["version"])
        content_v2 = hashlib.sha256(f"dynamic-{trial}-v2".encode()).hexdigest()
        version2 = self.request("POST", f"/v1/sites/{name}/versions", self.tenant_token, {
            "siteType": "dynamic", "contentSha256": content_v2,
            "image": self.config.dynamic_image, "schemaChange": "none",
            "decisionRationale": "fixed cluster benchmark recovery fixture",
        }, (201,))
        v2 = int(version2["version"])
        self.request("POST", "/v1/deployments", self.tenant_token, {
            "name": name, "image": self.config.dynamic_image, "port": 8080,
            "healthPath": "/", "exposure": "internal", "siteVersion": v2,
        }, (202,))
        observed_v2 = self._wait_ready(name, v2)
        pointers_v2 = self._wait_version_pointers(name, v2)
        # A rollback is an explicit control-plane decision, not a failed forward
        # rollout. Move the durable pointer first so the automatic failed-update
        # recovery cannot legitimately restore v2 while v1 is converging. The
        # mutation fence shared by this endpoint and snapshot sync keeps a stale
        # Kubernetes snapshot from overwriting the decision.
        promoted = self.request(
            "POST", f"/v1/sites/{name}/promote", self.tenant_token,
            {"version": v1},
        )
        accepted = self.request("POST", "/v1/deployments", self.tenant_token, {
            "name": name, "image": self.config.dynamic_image, "port": 8080,
            "healthPath": "/", "exposure": "internal", "siteVersion": v1,
        }, (202,))
        recovered = self._wait_ready(name, v1)
        versions = self._wait_version_pointers(name, v1)
        return {
            "v2Observed": observed_v2, "v2Pointers": pointers_v2,
            "rollbackPromotion": promoted,
            "rollbackRevision": accepted["revision"], "recovered": recovered,
            "versions": versions,
        }

    def cleanup_trial(self, names: list[str]) -> dict[str, Any]:
        for name in names:
            self.request("DELETE", f"/v1/deployments/{name}", self.tenant_token, expected=(202,))
        deadline = time.monotonic() + self.config.timeout_seconds
        remaining = set(names)
        while remaining and time.monotonic() < deadline:
            for name in list(remaining):
                try:
                    self.request("GET", f"/v1/deployments/{name}", self.tenant_token, expected=(200, 404))
                except ClusterFailure:
                    raise
                if self.events[-1]["status"] == 404:
                    remaining.remove(name)
            if remaining:
                time.sleep(1)
        if remaining:
            raise ClusterFailure(f"deployments not cleaned up: {sorted(remaining)}")
        return {"deleted": names}

    def _delete_benchmark_persistent_data(self) -> list[str]:
        """Stop PVC consumers, then reclaim data while the provisioner is alive."""
        payload = json.loads(self.command([
            "kubectl", "-n", self.config.namespace, "get", "pvc", "-o", "json",
        ]).stdout or "{}")
        volumes = sorted({
            str((item.get("spec") or {}).get("volumeName") or "")
            for item in payload.get("items") or []
        } - {""})
        if not (payload.get("items") or []):
            return []
        # A PVC with pvc-protection cannot finish deleting while a chart Pod is
        # still mounting it.  Stop every benchmark workload and wait for its
        # Pods to disappear before asking the still-running local provisioner
        # to reclaim the bound PVs.  The later Helm uninstall remains
        # responsible for deleting the now-scaled workload objects.
        self.command([
            "kubectl", "-n", self.config.namespace, "scale",
            "deployment,statefulset", "--all", "--replicas=0",
        ])
        self.command([
            "kubectl", "-n", self.config.namespace, "delete", "pod", "--all",
            "--wait=true", f"--timeout={self.config.timeout_seconds}s",
        ])
        self.command([
            "kubectl", "-n", self.config.namespace, "delete", "pvc", "--all",
            "--wait=true", f"--timeout={self.config.timeout_seconds}s",
        ])
        deadline = time.monotonic() + self.config.timeout_seconds
        remaining = set(volumes)
        while remaining and time.monotonic() < deadline:
            for volume in list(remaining):
                found = self.command([
                    "kubectl", "get", "pv", volume, "--ignore-not-found", "-o", "name",
                ]).stdout.strip()
                if not found:
                    remaining.remove(volume)
            if remaining:
                time.sleep(1)
        if remaining:
            raise ClusterFailure(
                f"persistent volumes were not reclaimed before provisioner removal: {sorted(remaining)}"
            )
        return volumes

    def finish(self) -> dict[str, Any]:
        if self.forward is not None:
            self.forward.terminate()
            try:
                self.forward.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.forward.kill()
        if not self.context_verified:
            return {"namespaceDeleted": False, "reason": "context was never verified"}
        # The only licence to scale workloads to zero, delete every Pod and PVC,
        # wait out PV reclamation and drop the namespace. "The namespace exists"
        # is not that licence: a mistyped --namespace naming a live namespace
        # makes prepare() raise "already exists", which reads like a refusal,
        # and cleanup would then destroy exactly what the run refused to touch.
        if not self.namespace_created_by_this_run:
            return {
                "namespaceDeleted": False,
                "reason": "namespace was not created by this run",
            }
        existing = self.command([
            "kubectl", "get", "namespace", self.config.namespace, "--ignore-not-found", "-o", "name"
        ]).stdout.strip()
        if not existing:
            return {"namespaceDeleted": False, "reason": "namespace was never created"}
        release = self.command([
            "helm", "status", "site", "--namespace", self.config.namespace
        ]) if subprocess.run(
            ["helm", "status", "site", "--namespace", self.config.namespace],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0 else None
        deleted_volumes = self._delete_benchmark_persistent_data()
        if release is not None:
            self.command([
                str(ROOT / "scripts" / "standalone.sh"), "uninstall", "--namespace", self.config.namespace
            ])
        self.command(["kubectl", "delete", "namespace", self.config.namespace, "--wait=true", f"--timeout={self.config.timeout_seconds}s"])
        return {
            "namespaceDeleted": True,
            "persistentVolumesDeleted": deleted_volumes,
        }


def _stage(name: str, operation: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    started = time.monotonic()
    timestamp = now()
    try:
        evidence = operation()
    except Exception as exc:  # fail-closed evidence boundary
        return {"stage": name, "status": "failed", "startedAt": timestamp, "finishedAt": now(), "durationMs": round((time.monotonic() - started) * 1000, 3), "error": str(exc)}
    return {"stage": name, "status": "passed", "startedAt": timestamp, "finishedAt": now(), "durationMs": round((time.monotonic() - started) * 1000, 3), "evidence": evidence}


def run(config: ClusterConfig, driver: Any | None = None) -> dict[str, Any]:
    if config.trials < 60:
        raise ValueError("cluster benchmark requires at least 60 trials")
    for label, value in (("control", config.control_image), ("dynamic", config.dynamic_image)):
        if not IMAGE.fullmatch(value):
            raise ValueError(f"{label} image must be pinned by sha256 digest")
    if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", config.namespace) or len(config.namespace) > 63:
        raise ValueError("benchmark namespace must be a Kubernetes DNS label")

    actual = driver or ActualClusterDriver(config)
    started = now()
    setup = _stage("cleanNamespaceInstall", actual.prepare)
    trials: list[dict[str, Any]] = []
    if setup["status"] == "passed":
        for number in range(1, config.trials + 1):
            trial_started = now()
            static = _stage("staticPublish", lambda n=number: actual.static_publish(n))
            dynamic = (
                _stage("dynamicPublish", lambda n=number: actual.dynamic_publish(n))
                if static["status"] == "passed" else {"stage": "dynamicPublish", "status": "not-run", "reason": "prior scored stage failed"}
            )
            if dynamic["status"] == "passed":
                observed = dynamic["evidence"]["observed"]
                revision = _stage("revisionMatch", lambda o=observed: (
                    {"revision": o["revision"], "verificationRevision": o["verification"]["revision"]}
                    if o.get("verification", {}).get("ok") is True and o.get("revision") == o.get("verification", {}).get("revision")
                    else (_ for _ in ()).throw(ClusterFailure("verification revision mismatch"))
                ))
            else:
                revision = {"stage": "revisionMatch", "status": "not-run", "reason": "dynamic publish did not pass"}
            rollback = (
                _stage("rollbackRecovery", lambda n=number, d=dynamic["evidence"]: actual.rollback_recovery(n, d))
                if revision["status"] == "passed" else {"stage": "rollbackRecovery", "status": "not-run", "reason": "revision match did not pass"}
            )
            names = [f"static-{number}", f"dynamic-{number}"]
            cleanup = _stage("cleanup", lambda names=names: actual.cleanup_trial(names))
            stages = [static, dynamic, revision, rollback, cleanup]
            trials.append({
                "trial": number, "startedAt": trial_started, "finishedAt": now(),
                "valid": all(stage["status"] == "passed" for stage in stages), "stages": stages,
            })
            if not trials[-1]["valid"]:
                break
    while len(trials) < config.trials:
        number = len(trials) + 1
        reason = "setup failed" if setup["status"] != "passed" else "run aborted after an earlier invalid trial"
        stages = [
            {"stage": name, "status": "not-run", "reason": reason}
            for name in ("staticPublish", "dynamicPublish", "revisionMatch", "rollbackRecovery", "cleanup")
        ]
        trials.append({"trial": number, "startedAt": now(), "finishedAt": now(), "valid": False, "stages": stages})
    final_cleanup = _stage("clusterCleanup", actual.finish)
    statuses = [stage["status"] for trial in trials for stage in trial["stages"]]
    counts = {name: statuses.count(name) for name in ("passed", "failed", "blocked", "not-run")}
    valid_trials = sum(trial["valid"] for trial in trials)
    static_ms = [next(s for s in trial["stages"] if s["stage"] == "staticPublish")["durationMs"] for trial in trials if trial["valid"]]
    dynamic_ms = [next(s for s in trial["stages"] if s["stage"] == "dynamicPublish")["durationMs"] for trial in trials if trial["valid"]]
    rollback_ms = [next(s for s in trial["stages"] if s["stage"] == "rollbackRecovery")["durationMs"] for trial in trials if trial["valid"]]
    threshold_path = ROOT / "evaluation" / "thresholds.v1.json"
    thresholds = json.loads(threshold_path.read_text(encoding="utf-8"))["profiles"]["cluster"]
    total = len(statuses)
    success_rate = round(counts["passed"] / total, 4) if total else 0.0
    revision_passed = sum(
        next(stage for stage in trial["stages"] if stage["stage"] == "revisionMatch")["status"] == "passed"
        for trial in trials
    )
    cleanup_passed = sum(
        next(stage for stage in trial["stages"] if stage["stage"] == "cleanup")["status"] == "passed"
        for trial in trials
    )
    revision_rate = revision_passed / len(trials) if trials else 0.0
    cleanup_rate = cleanup_passed / len(trials) if trials else 0.0
    static_p95 = percentile(static_ms, .95)
    dynamic_p95 = percentile(dynamic_ms, .95)
    evidence_valid = (
        setup["status"] == "passed" and final_cleanup["status"] == "passed"
        and len(trials) == config.trials and all(trial["valid"] for trial in trials)
    )
    passed = (
        evidence_valid and valid_trials >= thresholds["minimumValidTrials"] and valid_trials == config.trials
        and counts["failed"] <= thresholds["maximumFailed"]
        and counts["blocked"] <= thresholds["maximumBlocked"]
        and counts["not-run"] <= thresholds["maximumNotRun"]
        and revision_rate >= thresholds["requiredRevisionMatchedVerificationRate"]
        and cleanup_rate >= thresholds["requiredCleanupSuccessRate"]
        and static_p95 is not None and static_p95 <= thresholds["maximumStaticDeployP95Seconds"] * 1000
        and dynamic_p95 is not None and dynamic_p95 <= thresholds["maximumDynamicDeployP95Seconds"] * 1000
    )
    return {
        "schemaVersion": 1, "benchmarkVersion": "1.0.0", "runId": str(uuid.uuid4()),
        "profile": "cluster", "startedAt": started, "finishedAt": now(),
        "environment": {
            "gitCommit": subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip(),
            "gitDirty": bool(subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, check=True).stdout),
            "python": sys.version.splitlines()[0], "platform": platform.platform(),
            "kubernetesContext": config.context, "namespace": config.namespace,
            "chartSourceDigest": chart_digest(),
            "images": {"control": config.control_image, "dynamicFixture": config.dynamic_image, "staticRuntime": STATIC_IMAGE},
            "cluster": getattr(actual, "runtime_environment", {}),
            "tools": {
                "helm": tool_version("helm", "version", "--short"),
                "kubectl": tool_version("kubectl", "version", "--client"),
            },
        },
        "thresholds": thresholds,
        "setup": setup, "trials": trials, "cleanup": final_cleanup,
        "rawEvents": actual.events,
        "summary": {
            "requestedTrials": config.trials, "completedTrials": len(trials),
            "validTrials": valid_trials, "passed": counts["passed"],
            "failed": counts["failed"], "blocked": counts["blocked"],
            "notRun": counts["not-run"], "successRate": success_rate,
            "revisionMatchedVerificationRate": revision_rate,
            "cleanupSuccessRate": cleanup_rate,
        },
        "scoredStages": [stage for trial in trials for stage in trial["stages"]],
        "unscoredStages": [],
        "latencyMs": {
            "staticPublish": {"p50": percentile(static_ms, .50), "p95": static_p95},
            "dynamicPublish": {"p50": percentile(dynamic_ms, .50), "p95": dynamic_p95},
            "rollbackRecovery": {"p50": percentile(rollback_ms, .50), "p95": percentile(rollback_ms, .95)},
        },
        "valid": evidence_valid, "passed": passed,
    }
