"""Polling operator for SiteDeployment and SiteBuild resources.

The operator reconciles desired custom resources into Kubernetes workloads, derives
lifecycle phase from observed state, runs bounded workload verification, and exposes
process metrics. Converged resources are reasserted at a lower drift-resync rate.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from sites import exposure, shutdown, telemetry, tracing
from sites.builds import (
    build_job_name,
    build_job_resource,
    build_metadata_digest,
    build_metadata_subpath,
    delete_registry_manifest,
    immutable_image,
    job_complete,
    job_failure,
    prepare_build_metadata,
    remove_build_metadata,
    remove_source,
    site_build_response,
)
from os import getenv
from sites.k8s_resources import (
    STATIC_ARTIFACT_CONTROL_SECRET,
    ClusterNetworkError,
    artifact_configmap_name,
    artifact_configmap_resource,
    autoscaling_resources,
    deployment_resource,
    namespace_resource,
    network_policy_resources,
    resource_quota_resource,
    route_resources,
    scaled_object_path,
    service_resource,
    site_database_secret_resource,
    site_deployment_resource,
    static_artifact_secret_name,
    static_artifact_secret_resource,
    verify_pod_network,
)
from sites.kube import ApiError, KubeClient
from sites.naming import namespace_for_tenant

# The single source of truth for values is in sites/exposure.py.
CONTROL_NAMESPACE = exposure.CONTROL_NAMESPACE
COLLECTION_PATH = (
    f"/apis/sites.local/v1alpha1/namespaces/{CONTROL_NAMESPACE}/sitedeployments"
)
BUILD_COLLECTION_PATH = (
    f"/apis/sites.local/v1alpha1/namespaces/{CONTROL_NAMESPACE}/sitebuilds"
)
JOB_COLLECTION_PATH = f"/apis/batch/v1/namespaces/{CONTROL_NAMESPACE}/jobs"
ACTIVATOR_GRANT_NAME = "sites-activator-from-tenants"


def _activator_grant_resource(namespaces: list[str]) -> dict[str, Any]:
    """Allow HTTPRoutes in these Namespaces to reference the activator's Service.

    Pin the service name in `to`: not writing name is equivalent to releasing references to all Services in this Namespace.
    The control plane ns also has sites-api and registry.
    """
    return {
        "apiVersion": "gateway.networking.k8s.io/v1beta1",
        "kind": "ReferenceGrant",
        "metadata": {
            "name": ACTIVATOR_GRANT_NAME,
            "namespace": CONTROL_NAMESPACE,
            "labels": {"app.kubernetes.io/managed-by": "sites-operator"},
        },
        "spec": {
            "from": [
                {
                    "group": "gateway.networking.k8s.io",
                    "kind": "HTTPRoute",
                    "namespace": namespace,
                }
                for namespace in namespaces
            ],
            "to": [
                {
                    "group": "",
                    "kind": "Service",
                    "name": exposure.ACTIVATOR_SERVICE,
                }
            ],
        },
    }
FINALIZER = "sites.local/local-cleanup"
BUILD_FINALIZER = "sites.local/source-build-cleanup"
# Deleting a Job without this leaves its Pod behind without an owner: the Pod
# keeps burning its 1 CPU / 1Gi limit, keeps pushing to the local registry, and
# loses the Job's ttlSecondsAfterFinished along with the Job object.
BACKGROUND_DELETE = {
    "apiVersion": "meta.k8s.io/v1",
    "kind": "DeleteOptions",
    "propagationPolicy": "Background",
}


def public_url_for_spec(spec: dict[str, Any]) -> str | None:
    """Calculate the external URL of the site based on the currently exposed backend.

    The only source of truth for URL formulas and port pools is in sites/exposure.py(PUBLIC_URL_HOST /
    HOST_PORT_BASE / NODE_PORT_MIN and NodePort offset formulas live there, by
    test_sites.PortMappingContractTests pin the kubeadm host-port mapping);
    This module no longer retains the second definition. Public_url_for used to calculate URL based on bare nodePort
    Deleted, the equivalent writing method is exposure.NodePortExposure().public_url({"nodePort": p}).
    """
    if str(spec.get("exposure", "public")) != "public":
        return None
    return exposure.backend().public_url(spec)


RECONCILE_INTERVAL = float(
    getenv("SITES_RECONCILE_INTERVAL", "2") or "2"
)
# A floodgate for forensic detection. The control plane makes this request by itself, so the timeout should be short and the reading should have an upper limit:
# It runs in the reconcile loop, and a dead workload should not hold back the convergence of other CRs.
VERIFY_TIMEOUT_SECONDS = float(
    getenv("SITES_VERIFY_TIMEOUT", "5") or "5"
)
VERIFY_MAX_BYTES = 64 * 1024
VERIFY_CHUNK_BYTES = 8 * 1024
# Back-off between verification probes of a site whose last probe failed. Without it a
# Ready-but-unreachable site is re-probed every sweep, each probe holding the serial
# loop for up to VERIFY_TIMEOUT_SECONDS; sixty such sites push one sweep past the
# /healthz stall threshold and the liveness probe kills a perfectly healthy operator.
VERIFY_RETRY_SECONDS = float(
    getenv("SITES_VERIFY_RETRY_SECONDS", "30") or "30"
)
DEPLOY_TIMEOUT_SECONDS = int(
    getenv("SITES_DEPLOY_TIMEOUT_SECONDS", "120") or "120"
)
# The operator's metrics/health endpoint. operator had no HTTP interface at all before, so it
# "Still alive but failing every round of reconcile" and "running well" are exactly the same outside the cluster -
# The Deployment has no probes, and the kubelet will not restart it.
METRICS_PORT = int(getenv("SITES_OPERATOR_METRICS_PORT", "9090") or "9090")
# How often the converged CR reiterates the desired state. See Operator._should_apply: This is the "self-healing delay"
# Explicit knob with "apiserver write amplification". Set 0 to fall back to the old behavior of applying every round.
DRIFT_RESYNC_SECONDS = float(
    getenv("SITES_DRIFT_RESYNC_SECONDS", "60") or "60"
)

METRICS = telemetry.Registry()
TRACE_EXPORT = METRICS.counter(
    "sites_tracing_export_total",
    "OTLP spans by bounded-export outcome.",
    ("outcome",),
)
for _outcome in ("queued", "exported", "dropped_queue_full", "dropped_export_failure"):
    TRACE_EXPORT.ensure(_outcome)
# There are three basic questions about reconcile: how many rounds were run, how long was each round, and how many failures occurred.
RECONCILE_TOTAL = METRICS.counter(
    "sites_operator_reconcile_total",
    "Reconcile attempts by resource kind and outcome.",
    ("kind", "outcome"),
)
RECONCILE_SECONDS = METRICS.histogram(
    "sites_operator_reconcile_seconds",
    "Wall time of one full reconcile sweep.",
    telemetry.DEFAULT_LATENCY_BUCKETS,
)
SWEEP_TOTAL = METRICS.counter(
    "sites_operator_sweep_total",
    "Full reconcile sweeps by outcome.",
    ("outcome",),
)
# Number of observed CRs. When it suddenly drops to 0, it's usually not because "all users have been deleted", but because the list has been hit.
# The wrong namespace or RBAC was narrowed - in that case the reconcile process was error-free.
OBSERVED_RESOURCES = METRICS.gauge(
    "sites_operator_observed_resources",
    "Custom resources seen in the most recent sweep, by kind.",
    ("kind",),
)
LAST_SWEEP_TIMESTAMP = METRICS.gauge(
    "sites_operator_last_sweep_timestamp_seconds",
    "Unix time of the last completed sweep; staleness is age(now, this).",
)
# Advances once per custom resource handled, so a sweep that is merely slow (many
# sites, many probe timeouts) still shows movement while a wedged loop does not.
LAST_PROGRESS_TIMESTAMP = METRICS.gauge(
    "sites_operator_last_progress_timestamp_seconds",
    "Unix time the operator last finished handling one custom resource.",
)
# Known tag combinations are first registered as 0, otherwise "never failed" and "pointer not connected" are the same on the crawler end.
for _kind in ("sitebuild", "sitedeployment"):
    for _outcome in ("success", "failure", "status_write_failure"):
        RECONCILE_TOTAL.ensure(_kind, _outcome)
for _outcome in ("success", "failure"):
    SWEEP_TOTAL.ensure(_outcome)


APPLY_SKIPPED = METRICS.counter(
    "sites_operator_apply_skipped_total",
    "Workload applies skipped because the resource was already settled.",
)
APPLY_SKIPPED.ensure()


PHASE_TRANSITIONS = METRICS.counter(
    "sites_operator_phase_transitions_total",
    "Observed status phase transitions by resource kind and target phase.",
    ("kind", "phase"),
)


def _log_phase_transition(
    kind: str, name: str, previous: Any, phase: str, message: str
) -> None:
    """Remember a state transition that actually occurred.

    The call point is after the "return without change" of the two _patch_*_status - transfer is a sparse event,
    And reconcile runs every 2 seconds. Putting it before judgment will make the same steady state produce tens of thousands of items every day
    Exactly the same logs, which both conceal the true events and make the log costs out of control.

    name is entered in the log but **not in the metric label**: Troubleshooting must be able to locate the specific site, and the metric
    Splitting tags by site can lead to both base bounding and tenant list leakage.
    """
    PHASE_TRANSITIONS.inc(kind, phase)
    telemetry.log(
        "phase_transition",
        level="warn" if phase == "Failed" else "info",
        kind=kind,
        name=name,
        previous_phase=previous or "<none>",
        phase=phase,
        message=message[:256],
    )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Forensic requests never follow redirects.

    What forensics needs to answer is "what does this site return by itself?" Follow Location and enter status.
    The response hash becomes the response hash of another target - and the control plane is in the registry's NetworkPolicy
    In the release list, the tenant returns a Location pointing to the registry, and the control plane can open it by itself.
    registry, and then write other people's repo list into the status as evidence of this site.
    Returning None causes 3xx to pass off as an HTTPError as is, so it is recorded as a forensic failure like 4xx/5xx;
    And this is before urllib makes the second request, and the followed address will not be hit at all.
    """

    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


_VERIFY_OPENER = urllib.request.build_opener(_NoRedirect)


def _read_bounded(stream: Any, deadline: float) -> bytes:
    """Read until the byte limit is reached or the wall clock expires, whichever comes first.

    The timeout of urlopen is the upper limit of each socket operation, not the upper limit of the entire call: one per
    For a tenant that spits out one byte per second, every recv is within the timeout, but the detection can drag on for several hours.
    While run_once is serial, it delays the convergence of all CRs. Therefore, both the byte and time gates are
    There must be.

    Must use read1: read(n) will read until n bytes are reached or the peer is closed. Those in the middle
    The "no timeout every time" recv is all in this call, and the wall clock has no chance to be checked——
    A slow tenant that emits one byte at a time can otherwise keep this function active
    long past every individual socket timeout.
    """
    read1 = getattr(stream, "read1", None) or stream.read
    chunks: list[bytes] = []
    remaining = VERIFY_MAX_BYTES
    while remaining > 0:
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"verification exceeded {VERIFY_TIMEOUT_SECONDS:g}s"
            )
        chunk = read1(min(remaining, VERIFY_CHUNK_BYTES))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _parse_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_transient_error(exc: BaseException) -> bool:
    """Is this exception a transient fault that "will probably heal itself if you try again?"

    Before writing the exception into CR status, you must first ask this question: SiteBuild's Failed in CRD is
    Termination state, the caller (api._FINISHED_BUILD_PHASES) abandons the entire build accordingly; once apiserver
    The next round after jittering for two seconds will be good. Writing it as Failed is equivalent to letting the caller give up a session.
    A successful build that is indistinguishable from a true failure afterwards.

    The criterion is against the real throw point of sites/kube.py:
    - Transport layer failures (URLError, TimeoutError when reading body) are all in kube.py
    Convert to **naked** RuntimeError thrown. ApiError is also a subclass of RuntimeError, so
    ApiError must be diverted first - in the end it will still be RuntimeError, in this reconcile
    On the path, apiserver cannot be connected (including local infrastructure such as registry).
    - ApiError is only considered instantaneous when server failure and explicit current limiting (5xx/408/429); 4xx is verification/permission/
    The result of quota retries will not change, falling on the permanent side, writing Failed is exactly its semantics.
    - Business verification errors such as ValidationError (ValueError subclasses) and code bugs (KeyError and the like)
    Also falls on the permanent side: hiding them in "recoverable" phases only makes real faults invisible.
    """
    if isinstance(exc, ApiError):
        return exc.status >= 500 or exc.status in (408, 429)
    if isinstance(exc, (TimeoutError, urllib.error.URLError)):
        return True
    return isinstance(exc, RuntimeError)


def _rollout_stall_reason(
    kube: Any,
    deployment: dict[str, Any],
    namespace: str,
    service_name: str,
) -> str:
    """The direct reason for the Deployment to be stuck is to enter the Failed message; if it cannot be obtained, an empty string will be returned.

    Take two layers: Deployment Available condition message (controller perspective), and the first
    Waiting/terminated reason for non-Running Pod (container perspective, OOMKilled only here).
    Failure to allow incidental information - reason returns the bare timeout copy when it cannot be obtained, and diagnostic information should not be allowed to be obtained
    Exceptions cover up the state that was originally intended to be written.
    """
    reasons: list[str] = []
    try:
        for condition in (deployment.get("status") or {}).get("conditions") or []:
            if (
                str(condition.get("type")) == "Available"
                and str(condition.get("status")) != "True"
                and (message := str(condition.get("message") or "").strip())
            ):
                reasons.append(message)
                break
        selector = urllib.parse.quote(
            ",".join(
                f"{key}={value}"
                for key, value in {
                    "app.kubernetes.io/name": service_name,
                    "app.kubernetes.io/managed-by": "sites-operator",
                }.items()
            )
        )
        pods = kube.get(
            f"/api/v1/namespaces/{namespace}/pods?labelSelector={selector}"
        )
        for pod in (pods or {}).get("items") or []:
            for container in (pod.get("status") or {}).get(
                "containerStatuses"
            ) or []:
                state = container.get("state") or {}
                waiting = str(
                    (state.get("waiting") or {}).get("reason") or ""
                ).strip()
                terminated = str(
                    (container.get("lastState") or {})
                    .get("terminated", {})
                    .get("reason")
                    or ""
                ).strip()
                if waiting or terminated:
                    # waiting is more real-time (CrashLoopBackOff stops at waiting),
                    # OOMKilled in lastState.terminated is the accident record of the last life.
                    reasons.append(
                        f"pod {pod['metadata']['name']}: "
                        + (waiting or terminated)
                    )
                    break
            if len(reasons) >= 2:
                break
    except Exception:  # noqa: BLE001 - diagnostic accessory, failure does not change the main process
        pass
    if not reasons:
        return ""
    return f" ({'; '.join(reasons[:2])[:400]})"


def _desired_replicas(deployment: dict[str, Any]) -> int:
    """The **current** expected number of replicas for this Deployment in the cluster.

    Read the object returned by the cluster instead of the desired one we calculated: the site where scaleToZero is turned on,
    .spec.replicas are managed by KEDA, and the operator is just an observer.
    """
    return int((deployment.get("spec") or {}).get("replicas", 1))


def _deployment_ready(deployment: dict[str, Any], replicas: int | None = None) -> bool:
    metadata = deployment.get("metadata") or {}
    status = deployment.get("status") or {}
    generation = int(metadata.get("generation", 0))
    if replicas is None:
        replicas = _desired_replicas(deployment)
    # 🔴 The expected number of replicas must read the actual value in the cluster, and cannot be hard-coded to 1. scaleToZero turned on
    # After the site shrinks to 0, pressing 1 will pin it to Not Ready forever; and the CR of Not Ready will be reset every round.
    # Re-apply (reconcile's frequency reduction only takes effect on the converged ones), so every time the scaler shrinks,
    # operator increases the write frequency by 30 times.
    #
    # The following general criteria are naturally established for 0 (all three counts are 0 = dormant and achieved), no special criteria are needed -
    # I initially wrote a branch with `if replicas == 0`, and the rollback experiment proved that even if I delete it, there will be no use cases.
    # If it doesn't change to red, it means the code is not holding anything.
    return (
        int(status.get("observedGeneration", 0)) >= generation
        and int(status.get("updatedReplicas", 0)) == replicas
        and int(status.get("availableReplicas", 0)) == replicas
        and int(status.get("unavailableReplicas", 0)) == 0
    )


class Operator:
    def __init__(self, kube: KubeClient, stop: threading.Event | None = None):
        self.kube = kube
        # Set by the SIGTERM handler (or a test); run_forever exits at its next check
        # instead of sleeping through terminationGracePeriodSeconds into a SIGKILL.
        self._stop = stop if stop is not None else threading.Event()
        # CR name → the monotonic moment when the desired state was last reiterated. It was just a cache used for frequency reduction, so it was lost.
        # It can be applied for one more round at most; therefore, it is not persisted. After restarting, each CR will be applied once more.
        self._applied_at: dict[str, float] = {}

    def _patch_status(
        self, cr: dict[str, Any], phase: str, message: str, **extra: Any
    ) -> None:
        name = cr["metadata"]["name"]
        current = cr.get("status") or {}
        desired = {
            "phase": phase,
            "message": message[:512],
            **extra,
        }
        if all(current.get(key) == value for key, value in desired.items()):
            return
        _log_phase_transition("sitedeployment", name, current.get("phase"), phase, message)
        self.kube.patch(
            f"{COLLECTION_PATH}/{name}/status",
            {"status": desired},
        )

    @staticmethod
    def _probe(
        request: urllib.request.Request, deadline: float
    ) -> tuple[int, bytes]:
        """Issue a forensic request and return (status code, capped response body).

        Use the self-built opener instead of urllib.request.urlopen: the default opener will follow the redirect,
        Up to ten hops, each hop has its own socket timeout.
        """
        try:
            with _VERIFY_OPENER.open(
                request, timeout=VERIFY_TIMEOUT_SECONDS
            ) as response:
                return int(response.status), _read_bounded(response, deadline)
        except urllib.error.HTTPError as exc:
            # 4xx/5xx and 3xx followed by rejection are all part of forensics: note the status code and response body,
            # Instead of treating it as an unanswered request.
            return int(exc.code), _read_bounded(exc, deadline)

    def _verify_workload(
        self, spec: dict[str, Any], namespace: str
    ) -> dict[str, Any]:
        """Really request the workload once and write the results into the state as evidence.

        Responsibility: Initiated and recorded by the control plane itself; conclusions provided by the agent or caller are not accepted.
        The strength of the evidence only goes so far: it can say "something at this address is pressing 2xx to return content",
        The content cannot be said to be correct - the response body is originally generated by the tenant itself. Therefore, the criteria are only narrowed to two items
        What it can't forge: the status code must be 2xx (3xx is considered passed once, so let healthPath
        If you always return 304, you can get ok=true plus a hash of an empty response body), and do not follow the redirect.
        (See _NoRedirect - what is recorded after the past is the response of others).
        Constraints: Only GET healthy paths, only read the first 64 KiB, the entire detection has a 5-second wall clock;
        The detection results do not affect the phase. Readiness from the Kubernetes perspective and "it is really responding correctly" are two different things.
        thing, merging makes one of them never visible.
        """
        service = spec["serviceName"]
        url = (
            f"http://{service}.{namespace}.svc:{int(spec['port'])}"
            f"{spec['healthPath']}"
        )
        evidence: dict[str, Any] = {
            "checkedAt": _now(),
            "revision": str(spec.get("revision", "1")),
            "url": url,
        }
        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                "User-Agent": "sites-operator/verification",
                **tracing.outbound_headers(),
            },
        )
        # The wall clock starts before initiation, and the connection, first byte, and entire read are all included in this budget.
        deadline = time.monotonic() + VERIFY_TIMEOUT_SECONDS
        try:
            status, body = self._probe(request, deadline)
        except Exception as exc:
            evidence.update(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
            )
            return evidence

        evidence.update(
            {
                # Only 2xx counts as a pass. 3xx must be the one who has not been followed here. What it means is
                # "This address points people elsewhere", not "This site returns content".
                "ok": 200 <= status < 300,
                # JSON merge-patch treats null as deletion, so a successful
                # retry removes an earlier transport error instead of leaving
                # contradictory ok=true/error=... evidence behind.
                "error": None,
                "httpStatus": status,
                "bodyBytes": len(body),
                "bodySha256": hashlib.sha256(body).hexdigest(),
            }
        )
        return evidence

    def _verification_for(
        self,
        cr: dict[str, Any],
        spec: dict[str, Any],
        namespace: str,
        *,
        probe: bool = True,
    ) -> dict[str, Any]:
        """Reuse a passing check for the same revision, otherwise probe again.

        Reconcile is a cycle, and doing it every round will continue to increase traffic for the workload. The one that passed
        revision reuse; failed ones will be retried in the next round - the service may just not be fully up yet.

        probe=False is used for sites with zero replicas: there are no instances to probe at that time, and you will only get the
        A piece of evidence that is bound to fail. The evidence that has been passed is retained as usual (it proves that "this revision
        "Run it and pass"), leave blank if you have never passed - empty evidence collection and "detection failure" are two different things and should not be confused.
        """
        existing = (cr.get("status") or {}).get("verification") or {}
        same_revision = existing.get("revision") == str(spec.get("revision", "1"))
        if existing.get("ok") and same_revision:
            return existing
        if not probe:
            return existing if same_revision else {}
        # A failed probe is retried, but not every sweep: keep the failed evidence
        # for VERIFY_RETRY_SECONDS so one unreachable site costs one probe timeout
        # per retry window rather than one per sweep. Evidence without a parseable
        # checkedAt (or for another revision) gets probed as before.
        checked_at = _parse_time(existing.get("checkedAt")) if same_revision else None
        if checked_at is not None:
            age = (dt.datetime.now(dt.timezone.utc) - checked_at).total_seconds()
            if 0 <= age < VERIFY_RETRY_SECONDS:
                return existing
        evidence = self._verify_workload(spec, namespace)
        if evidence.get("ok") is True:
            # Keep the counter explicit so downstream rollback logic can
            # distinguish a recovered probe from legacy/missing evidence.
            evidence["consecutiveFailures"] = 0
            return evidence
        previous_failures = 0
        if same_revision:
            try:
                previous_failures = max(
                    0, int(existing.get("consecutiveFailures") or 0)
                )
            except (TypeError, ValueError):
                previous_failures = 0
        evidence["consecutiveFailures"] = previous_failures + 1
        return evidence

    def _ensure_finalizer(self, cr: dict[str, Any]) -> None:
        metadata = cr["metadata"]
        finalizers = list(metadata.get("finalizers") or [])
        if FINALIZER in finalizers:
            return
        finalizers.append(FINALIZER)
        self.kube.patch(
            f"{COLLECTION_PATH}/{metadata['name']}",
            {"metadata": {"finalizers": finalizers}},
        )

    def _patch_build_status(
        self, build: dict[str, Any], phase: str, message: str, **extra: Any
    ) -> None:
        name = build["metadata"]["name"]
        current = build.get("status") or {}
        desired = {"phase": phase, "message": message[:512], **extra}
        if all(current.get(key) == value for key, value in desired.items()):
            return
        _log_phase_transition("sitebuild", name, current.get("phase"), phase, message)
        self.kube.patch(
            f"{BUILD_COLLECTION_PATH}/{name}/status",
            {"status": desired},
        )

    def _ensure_build_finalizer(self, build: dict[str, Any]) -> None:
        metadata = build["metadata"]
        finalizers = list(metadata.get("finalizers") or [])
        if BUILD_FINALIZER in finalizers:
            return
        finalizers.append(BUILD_FINALIZER)
        self.kube.patch(
            f"{BUILD_COLLECTION_PATH}/{metadata['name']}",
            {"metadata": {"finalizers": finalizers}},
        )

    def _cleanup_build(self, build: dict[str, Any]) -> None:
        metadata = build["metadata"]
        spec = build.get("spec") or {}
        status = build.get("status") or {}
        for path in (
            f"{JOB_COLLECTION_PATH}/{build_job_name(build)}",
            f"{COLLECTION_PATH}/{metadata['name']}",
        ):
            try:
                self.kube.delete(path, BACKGROUND_DELETE)
            except ApiError as exc:
                if exc.status != 404:
                    raise
        source_path = str(spec.get("sourcePath") or "")
        if source_path:
            remove_source(
                source_path,
                backend=str(spec.get("sourceStorage") or "pvc"),
            )
        remove_build_metadata(build_metadata_subpath(build))
        repository = str(spec.get("repository") or "")
        digest = str(status.get("imageDigest") or "")
        # A build-only digest is the immutable artifact referenced by a later
        # SiteVersion. Deleting the SiteBuild must not make that version
        # undeployable; version-aware registry GC owns its eventual removal.
        if repository and digest and not bool(spec.get("buildOnly")):
            delete_registry_manifest(repository, digest)
        remaining = [
            item
            for item in (metadata.get("finalizers") or [])
            if item != BUILD_FINALIZER
        ]
        self.kube.patch(
            f"{BUILD_COLLECTION_PATH}/{metadata['name']}",
            {"metadata": {"finalizers": remaining}},
        )

    def _ensure_build_job(
        self, build: dict[str, Any], job_path: str
    ) -> dict[str, Any]:
        """Build the Job built this time; if the one with the same name already exists, retrieve it.

        The job name is a function of (SiteBuild, artifactSha256), so "the same name already exists" must mean
        Jobs built at the same time. Both paths are available: both copies read 404 when the control plane is rolled, and each build
        Once, the second one will get 409; or the newly deleted build will be relaunched immediately with the same source code, but the old Job is still there
        Background terminated. 409 on both paths means "it's already there", it's not that the build failed this time -
        Naked create will cause 409 to appear in run_once and be written as Failed, and Failed will be in CRD
        Here is the termination status, whereby the caller gives up a construction that actually succeeded two seconds later.

        You cannot use create_or_patch instead: 409 on that path is followed by patch, and Job's
        Spec.template and selector will be read-only after they are built. Put the entire body patch back or 422,
        Or modify a running Job. What we want here is "read back", not "write over".
        """
        try:
            return self.kube.create(
                JOB_COLLECTION_PATH,
                build_job_resource(build, namespace=CONTROL_NAMESPACE),
            )
        except ApiError as exc:
            if exc.status != 409:
                raise
        try:
            return self.kube.get(job_path)
        except ApiError as exc:
            if exc.status != 404:
                raise
        # 409 followed by 404: The Job with the same name disappeared between the two calls. Can’t read Job this round
        # status, the empty object is neither completed nor failed, so this time the reconcile falls on Building,
        # Rebuilding in the next round - better than recording a race state as a terminated state.
        return {}

    @tracing.traced_resource("sites.operator.build")
    def reconcile_build(self, build: dict[str, Any]) -> None:
        metadata = build["metadata"]
        if metadata.get("deletionTimestamp"):
            self._cleanup_build(build)
            return
        self._ensure_build_finalizer(build)
        spec = build["spec"]
        status = build.get("status") or {}
        node_port: int | None = None
        if exposure.backend().allocates_ports:
            try:
                node_port = int(spec["nodePort"])
            except (KeyError, TypeError, ValueError):
                node_port = 0
            if not 30000 <= node_port <= 32767:
                self._patch_build_status(
                    build,
                    "Failed",
                    "SiteBuild has no valid persisted nodePort; delete and resubmit it",
                    ready=False,
                )
                return
        generation = int(metadata.get("generation", 1))
        common = {
            "observedGeneration": generation,
            "startedAt": (
                status.get("startedAt")
                if int(status.get("observedGeneration", 0)) == generation
                else None
            )
            or _now(),
            "jobName": build_job_name(build),
            "artifactSha256": spec["artifactSha256"],
        }

        image_digest = ""
        if int(status.get("observedGeneration", 0)) == generation:
            image_digest = str(status.get("imageDigest") or "")
        metadata_subpath = build_metadata_subpath(build)
        if not image_digest:
            job_path = f"{JOB_COLLECTION_PATH}/{build_job_name(build)}"
            try:
                job = self.kube.get(job_path)
            except ApiError as exc:
                if exc.status != 404:
                    raise
                # The Job reports its digest onto the build PVC, so the drop
                # point has to exist and be writable before the Pod starts.
                prepare_build_metadata(metadata_subpath)
                job = self._ensure_build_job(build, job_path)
            failure = job_failure(job)
            if failure:
                remove_source(
                    str(spec["sourcePath"]),
                    backend=str(spec.get("sourceStorage") or "pvc"),
                )
                remove_build_metadata(metadata_subpath)
                self._patch_build_status(
                    build, "Failed", failure, ready=False, **common
                )
                return
            if not job_complete(job):
                self._patch_build_status(
                    build,
                    "Building",
                    "Waiting for the bounded BuildKit Job",
                    ready=False,
                    **common,
                )
                return
            # Kept until the SiteBuild is deleted: this is the only record of
            # what the build produced until imageDigest reaches the status
            # subresource, and the reconcile in between can be interrupted.
            image_digest = build_metadata_digest(metadata_subpath)

        image = immutable_image(str(spec["repository"]), image_digest)
        if bool(spec.get("buildOnly")):
            remove_source(
                str(spec["sourcePath"]),
                backend=str(spec.get("sourceStorage") or "pvc"),
            )
            self._patch_build_status(
                build,
                "Running",
                "Immutable image build completed",
                ready=True,
                imageDigest=image_digest,
                image=image,
                verification={
                    "ok": True,
                    "kind": "registry-digest",
                    "revision": str(spec["revision"]),
                    "imageDigest": image_digest,
                },
                **common,
            )
            return
        desired = site_deployment_resource(
            {
                "name": spec["serviceName"],
                "image": image,
                "port": spec["port"],
                "healthPath": spec["healthPath"],
            },
            str(spec["merchantID"]),
            str(spec["userID"]),
            namespace=CONTROL_NAMESPACE,
        )
        desired["spec"]["revision"] = str(spec["revision"])
        if node_port is not None:
            desired["spec"]["nodePort"] = node_port
        deploy_path = f"{COLLECTION_PATH}/{metadata['name']}"
        self.kube.create_or_patch(COLLECTION_PATH, deploy_path, desired)
        remove_source(
            str(spec["sourcePath"]),
            backend=str(spec.get("sourceStorage") or "pvc"),
        )
        site_deployment = self.kube.get(deploy_path)
        deploy_status = site_deployment.get("status") or {}
        deploy_phase = str(deploy_status.get("phase") or "Deploying")
        build_phase = deploy_phase if deploy_phase in {"Running", "Failed"} else "Deploying"
        response = site_build_response(build)
        self._patch_build_status(
            build,
            build_phase,
            str(deploy_status.get("message") or "Image built; waiting for deployment"),
            ready=build_phase == "Running" and bool(deploy_status.get("ready")),
            imageDigest=image_digest,
            image=image,
            url=deploy_status.get("url") or response.get("url"),
            verification=deploy_status.get("verification"),
            **common,
        )

    def _apply_autoscaling(
        self,
        spec: dict[str, Any],
        namespace: str,
        *,
        enabled: bool,
    ) -> None:
        """Hand replica ownership to KEDA only after the revision is verified."""
        if enabled and spec.get("scaleToZero"):
            for scaled in autoscaling_resources(spec, namespace):
                api_version = str(scaled["apiVersion"])
                plural = str(scaled["kind"]).lower() + "s"
                collection = f"/apis/{api_version}/namespaces/{namespace}/{plural}"
                try:
                    self.kube.create_or_patch(
                        collection,
                        f"{collection}/{scaled['metadata']['name']}",
                        scaled,
                    )
                except ApiError as exc:
                    if exc.status != 404:
                        raise
                    telemetry.log(
                        "keda_crd_absent",
                        level="warning",
                        service=str(spec.get("serviceName") or ""),
                    )
            return

        # This also removes a ScaledObject from the previous revision. Without
        # it KEDA can scale the old Deployment to zero while the replacement is
        # still rolling, preventing the new revision from ever being probed.
        try:
            self.kube.delete(scaled_object_path(spec, namespace))
        except ApiError as exc:
            if exc.status != 404:
                raise

    def _apply_workload(
        self,
        spec: dict[str, Any],
        namespace: str,
        *,
        enable_autoscaling: bool | None = None,
        force_replicas: int | None = None,
    ) -> None:
        namespace_body = namespace_resource(spec["merchantID"], spec["userID"])
        self.kube.create_or_patch(
            "/api/v1/namespaces",
            f"/api/v1/namespaces/{namespace}",
            namespace_body,
        )
        # The gate to the total amount of resources is established together with the Namespace: the quota for the number of deployments cannot stop the volume, and this layer
        # Forced by Kubernetes, there is no way around the API.
        quota = resource_quota_resource(spec)
        self.kube.create_or_patch(
            f"/api/v1/namespaces/{namespace}/resourcequotas",
            f"/api/v1/namespaces/{namespace}/resourcequotas/"
            f"{quota['metadata']['name']}",
            quota,
        )

        service_name = spec["serviceName"]
        # The exposed service allocates a NodePort from the port pool. Apply Service first, bypassing the API directly
        # The built CR will fail before leaving the Deployment or artifact ConfigMap; normal
        # The caller's API has been rejected synchronously once.
        service = service_resource(spec, namespace)
        self.kube.create_or_patch(
            f"/api/v1/namespaces/{namespace}/services",
            f"/api/v1/namespaces/{namespace}/services/{service_name}",
            service,
        )

        # Route object (HTTPRoute under Gateway backend). NodePort backend returns empty list -
        # The port itself is the route and has no additional objects. Place after Service: HTTPRoute's
        # backendRef points to Service. Building in reverse order will cause the gateway to first see a link pointing to a non-existent backend.
        # Route and mark it as ResolvedRefs=False, it will wait for the next round of reconcile to heal itself.
        for route in route_resources(spec, namespace):
            api_version = str(route["apiVersion"])
            plural = str(route["kind"]).lower() + "s"
            collection = f"/apis/{api_version}/namespaces/{namespace}/{plural}"
            self.kube.create_or_patch(
                collection,
                f"{collection}/{route['metadata']['name']}",
                route,
            )
        # The content volume for direct source code delivery must be in place first: if the Deployment is built first, the Pod will be stuck.
        # ContainerCreating(configmap not found), it will not heal itself until the next round of reconcile.
        artifact_cm = artifact_configmap_resource(spec, namespace)
        if artifact_cm is not None:
            self.kube.create_or_patch(
                f"/api/v1/namespaces/{namespace}/configmaps",
                (
                    f"/api/v1/namespaces/{namespace}/configmaps/"
                    f"{artifact_cm['metadata']['name']}"
                ),
                artifact_cm,
            )

        database = spec.get("database") or {}
        control_secret_name = str(database.get("controlSecretName") or "")
        if control_secret_name:
            control_secret = self.kube.get(
                f"/api/v1/namespaces/{CONTROL_NAMESPACE}/secrets/"
                f"{control_secret_name}"
            )
            runtime_secret = site_database_secret_resource(
                spec, namespace, control_secret
            )
            secret_collection = f"/api/v1/namespaces/{namespace}/secrets"
            self.kube.create_or_patch(
                secret_collection,
                f"{secret_collection}/{runtime_secret['metadata']['name']}",
                runtime_secret,
            )

        static_artifact = spec.get("staticArtifact") or {}
        if static_artifact:
            control_secret_name = str(
                static_artifact.get("controlSecretName")
                or STATIC_ARTIFACT_CONTROL_SECRET
            )
            control_secret = self.kube.get(
                f"/api/v1/namespaces/{CONTROL_NAMESPACE}/secrets/"
                f"{control_secret_name}"
            )
            runtime_secret = static_artifact_secret_resource(
                spec, namespace, control_secret
            )
            secret_collection = f"/api/v1/namespaces/{namespace}/secrets"
            self.kube.create_or_patch(
                secret_collection,
                f"{secret_collection}/{runtime_secret['metadata']['name']}",
                runtime_secret,
            )

        deployment = deployment_resource(spec, namespace)
        if force_replicas is not None:
            deployment["spec"]["replicas"] = force_replicas
        self.kube.create_or_patch(
            f"/apis/apps/v1/namespaces/{namespace}/deployments",
            f"/apis/apps/v1/namespaces/{namespace}/deployments/{service_name}",
            deployment,
        )
        for policy in network_policy_resources(spec, namespace):
            policy_name = policy["metadata"]["name"]
            self.kube.create_or_patch(
                f"/apis/networking.k8s.io/v1/namespaces/{namespace}/networkpolicies",
                (
                    f"/apis/networking.k8s.io/v1/namespaces/{namespace}"
                    f"/networkpolicies/{policy_name}"
                ),
                policy,
            )
        self._apply_autoscaling(
            spec,
            namespace,
            enabled=(
                bool(spec.get("scaleToZero"))
                if enable_autoscaling is None
                else enable_autoscaling
            ),
        )

    def _cleanup(self, cr: dict[str, Any]) -> None:
        spec = cr.get("spec") or {}
        service_name = spec.get("serviceName")
        merchant_id = spec.get("merchantID")
        user_id = spec.get("userID")
        # If one of the three sections is missing, it will not be cleared: if the merchantID is missing, you can only guess one merchant, and if you guess wrong, you will go to another merchant.
        # Delete the Deployment with the same name in the tenant Namespace. Would rather leave orphan resources and wait for manual processing.
        if service_name and merchant_id and user_id:
            namespace = namespace_for_tenant(merchant_id, user_id)
            # The name of NetworkPolicy is asked from the generator, not written by hand here. Two separate lists maintained
            # Sooner or later, there will be a fork, and the performance of the fork is "deleting CR and leaving a strategy that no one knows":
            # It selects the Pod by app.kubernetes.io/name, so the service with the same name will be quietly added when it is rebuilt.
            # A copy of the rules from a previous generation. This will automatically follow when adding a third strategy.
            policy_names = [
                str((policy.get("metadata") or {}).get("name", ""))
                for policy in network_policy_resources(spec, namespace)
            ]
            # The names of routing and scaling policies are also requested from the generator. HTTPRoute was not on this list before -
            # Deleting a site will leave a route pointing to the disappeared Service. It not only occupies that host,
            # It will be marked as ResolvedRefs=False by the gateway. When the entire tenant is deleted, it depends on the namespace.
            # The cleaning process is over, but deleting a single site will not.
            managed = [*route_resources(spec, namespace)]
            paths = [
                f"/apis/apps/v1/namespaces/{namespace}/deployments/{service_name}",
                f"/api/v1/namespaces/{namespace}/services/{service_name}",
                *(
                    f"/apis/{item['apiVersion']}/namespaces/{namespace}/"
                    f"{str(item['kind']).lower()}s/{item['metadata']['name']}"
                    for item in managed
                ),
                # Always listed, not derived from autoscaling_resources: a site
                # deleted after scaleToZero was switched off still owns the
                # ScaledObject created while it was on. 404 is filtered below.
                scaled_object_path(spec, namespace),
                *(
                    f"/apis/networking.k8s.io/v1/namespaces/{namespace}"
                    f"/networkpolicies/{name}"
                    for name in policy_names
                    if name
                ),
                (
                    f"/api/v1/namespaces/{namespace}/configmaps/"
                    f"{artifact_configmap_name(service_name)}"
                ),
                *(
                    [
                        f"/api/v1/namespaces/{namespace}/secrets/"
                        f"{spec['database']['secretName']}"
                    ]
                    if (spec.get("database") or {}).get("secretName")
                    else []
                ),
                *(
                    [
                        f"/api/v1/namespaces/{namespace}/secrets/"
                        f"{static_artifact_secret_name(spec)}"
                    ]
                    if spec.get("staticArtifact")
                    else []
                ),
            ]
            for path in paths:
                try:
                    self.kube.delete(path)
                except ApiError as exc:
                    if exc.status != 404:
                        raise
        # Drop only our own finalizer: clearing the list would also release
        # finalizers other controllers put on this object.
        metadata = cr["metadata"]
        remaining = [
            item for item in (metadata.get("finalizers") or []) if item != FINALIZER
        ]
        self.kube.patch(
            f"{COLLECTION_PATH}/{metadata['name']}",
            {"metadata": {"finalizers": remaining}},
        )

    def _should_apply(self, cr: dict[str, Any], generation: int) -> bool:
        """Do you want to re-write the desired state this round?

        Problem: `_apply_workload` hits create_or_patch 7 times per CR per round. Default
        A 2 second cycle, 100 sites is ~450 apiserver calls per second - and the vast majority of them
        It is to write the exact same object again.

        🔴 But **can't skip it** because of this: the operator's self-healing ability comes from "unconditionally changing the desired state in each round"
        Write it back". When someone manually deletes the Deployment of a site, they rely on rebuilding it in the next round.
        Skipping it completely is equivalent to exchanging self-healing for performance.

        So it is **frequency reduction, not skipping**: only convergence (spec unchanged + last round of Running + ready)
        The CR is downscaled, and still reiterated every DRIFT_RESYNC_SECONDS. Not ready, failed,
        The newly changed CR of spec will be applied every round - those are the ones that need to converge quickly.
        The self-heal delay therefore changes from "at most 2 seconds" to "at most DRIFT_RESYNC_SECONDS", which is explicit
        The cost is written here instead of asking people to infer it from the QPS diagram.
        """
        name = str(cr["metadata"].get("name") or "")
        if not name:
            return True
        status = cr.get("status") or {}
        settled = (
            int(status.get("observedGeneration", 0)) == generation
            and status.get("phase") == "Running"
            and status.get("ready") is True
        )
        now = time.monotonic()
        if not settled:
            # The CR that has not converged must be applied every round, and its timing is cleared at the same time - otherwise after it converges
            # A very old timestamp will be used, and the application will be applied again immediately in the first round.
            self._applied_at.pop(name, None)
            return True
        last = self._applied_at.get(name)
        if last is not None and (now - last) < DRIFT_RESYNC_SECONDS:
            APPLY_SKIPPED.inc()
            return False
        self._applied_at[name] = now
        return True

    def _forget_applied(self, observed_names: set[str]) -> None:
        """Clear the timer for a CR that no longer exists.

        If it is not clear, this dict will grow unbounded as sites are built and deleted - a single entry is small, but
        The operator is a resident process, and "small × unbounded" is still a leak.
        """
        for stale in set(self._applied_at) - observed_names:
            self._applied_at.pop(stale, None)

    @tracing.traced_resource("sites.operator.deploy")
    def reconcile(self, cr: dict[str, Any]) -> None:
        metadata = cr["metadata"]
        if metadata.get("deletionTimestamp"):
            self._cleanup(cr)
            return

        self._ensure_finalizer(cr)
        spec = cr["spec"]
        namespace = namespace_for_tenant(spec["merchantID"], spec["userID"])
        generation = int(metadata.get("generation", 1))
        status = cr.get("status") or {}
        if int(status.get("observedGeneration", 0)) != generation:
            started_at = _now()
        else:
            started_at = status.get("startedAt") or _now()
        started = _parse_time(started_at)
        if started is None:
            # An unparseable startedAt used to leave elapsed at 0 forever, so
            # the CR could never reach the deploy timeout. Restart the clock
            # and write the repaired value back through common_status below.
            started = dt.datetime.now(dt.timezone.utc)
            started_at = started.isoformat()

        verification_status = status.get("verification") or {}
        revision = str(spec.get("revision") or "")
        revision_verified = bool(
            verification_status.get("ok") is True
            and str(verification_status.get("revision") or "") == revision
        )
        promotion_pending = bool(spec.get("scaleToZero") and not revision_verified)
        if self._should_apply(cr, generation):
            self._apply_workload(
                spec,
                namespace,
                enable_autoscaling=False if promotion_pending else None,
                force_replicas=1 if promotion_pending else None,
            )
        deployment_path = (
            f"/apis/apps/v1/namespaces/{namespace}/deployments/"
            f"{spec['serviceName']}"
        )
        deployment = self.kube.get(deployment_path)
        # 🔴 The external scaler changes the number of replicas = the starting point of a new round of convergence, and resets the 120s window.
        # Neither KEDA's N→0 nor activator's 0→1 is "deployment failure retry", but after scaling
        # In the transition window, unavailableReplicas>0, _deployment_ready is False; and
        # startedAt uses the old value in CR (the deployment may have been a few hours ago), and elapsed must have exceeded
        # DEPLOY_TIMEOUT_SECONDS - so CR is marked as Failed every time it is woken up/shrunk.
        # After the copy is ready, the next round returns to Running. Failed is external fault semantics and cannot be
        # Normal sleep/wake flow triggers.
        desired_replicas = _desired_replicas(deployment)
        replicas_changed = (
            (observed_replicas := status.get("observedReplicas")) is not None
            and int(observed_replicas) != desired_replicas
        )
        ready_now = _deployment_ready(deployment)
        # Same treatment for a site that was ready last round and is not now with
        # an unchanged spec (eviction, OOM kill, node drain): startedAt still dates
        # from the original rollout, so elapsed is already past DEPLOY_TIMEOUT and
        # the first not-ready sweep would write Failed. Restart the clock so the
        # workload gets the full window to come back before it is called failed;
        # status.ready flips to False in this sweep, so the reset happens once.
        was_ready = (
            int(status.get("observedGeneration", 0)) == generation
            and status.get("ready") is True
        )
        if replicas_changed or (was_ready and not ready_now):
            started_at = _now()
            started = _parse_time(started_at)
        common_status = {
            "observedGeneration": generation,
            "startedAt": started_at,
            # Used for the next round of comparison; without it, the above detection will always see None.
            "observedReplicas": desired_replicas,
            "image": spec["image"],
            "serviceName": spec["serviceName"],
            "namespace": namespace,
            "port": int(spec["port"]),
            "url": public_url_for_spec(spec),
        }
        if ready_now:
            dormant = _desired_replicas(deployment) == 0
            verification = self._verification_for(
                cr, spec, namespace, probe=not dormant
            )
            if artifact := spec.get("artifact"):
                # The other half of the chain of evidence: the hash of the submission is put together with the hash of the actual response,
                # Only then can we make it clear that "the one that runs is the one that was submitted."
                common_status["artifactSha256"] = str(artifact.get("sha256", ""))
            self._patch_status(
                cr,
                "Running",
                # Hibernation is still Running: phase says "whether the desired state has been achieved", not
                # "Is there any instance now". The activator of the entry layer is responsible for whether it can be accessed, so it
                # Mixing into phase will make "scale down successful" and "deployment failed" become the same value.
                "Scaled to zero by an external autoscaler"
                if dormant
                else "Deployment rollout completed",
                ready=True,
                verification=verification,
                **common_status,
            )
            if (
                promotion_pending
                and isinstance(verification, dict)
                and verification.get("ok") is True
                and str(verification.get("revision") or "") == revision
            ):
                # Status evidence is persisted before KEDA can scale the
                # workload. Reapply the original spec to restore activator
                # routing and create the ScaledObject for the verified revision.
                self._apply_workload(
                    spec,
                    namespace,
                    enable_autoscaling=True,
                )
            return

        elapsed = (dt.datetime.now(dt.timezone.utc) - started).total_seconds()
        if elapsed >= DEPLOY_TIMEOUT_SECONDS:
            self._patch_status(
                cr,
                "Failed",
                # The reason in the attachment separates three types of investigation directions: OOMKilled (the upper limit is not enough),
                # CrashLoopBackOff (application itself), ImagePullBackOff (image/network),
                # The naked "not ready within 120s" reads exactly the same.
                (
                    f"Deployment was not ready within "
                    f"{DEPLOY_TIMEOUT_SECONDS}s"
                    + _rollout_stall_reason(
                        self.kube, deployment, namespace, spec["serviceName"]
                    )
                ),
                ready=False,
                **common_status,
            )
        else:
            self._patch_status(
                cr,
                "Deploying",
                "Waiting for Deployment rollout",
                ready=False,
                **common_status,
            )

    def _write_reconcile_failure(
        self,
        kind: str,
        item: dict[str, Any],
        exc: Exception,
        status_writer: Any,
    ) -> None:
        """Write a reconcile failure back to the CR state; transient errors do not write the final state.

        The semantics of Failed for both types of CR are "retrying will not change the outcome": SiteBuild's Failed is
        Termination state, the caller abandons the entire build accordingly; SiteDeployment's Failed is an external failure
        Semantics. As for transport layer RuntimeError and apiserver's 503/429 failures, the next round
        (default 2 seconds later) will self-heal - write them as Failed, and the caller will give up after a jitter.
        A build that would have succeeded is indistinguishable from a true failure. So first classify: Transient error press
        kind retreats to a recoverable notation, leaving the scene in the message for troubleshooting.
        """
        if not _is_transient_error(exc):
            status_writer(item, "Failed", str(exc), ready=False)
            return
        message = f"Transient error at {_now()}: {exc}"
        if kind == "sitebuild":
            # Building is a recoverable phase: the caller continues to wait, and the next round of reconcile will reconverge.
            # (Including briefly falling back from Running - re-walking the steps when handing over to deployment).
            status_writer(item, "Building", message, ready=False)
            return
        # SiteDeployment does not perform phase upgrade: the previous phase seen when using list is used, and the first read fails.
        # Cannot change the fact "what state it was in the last round"; new CR without historical phase falls back to
        # Deploying - Reconciliation has not finished running. It is "deploying" in itself, not a failure. Don't write
        # ready: merge-patch omitting means "no change", don't let a jitter make the last observation ready
        # The status is flushed away together. message has a timestamp of _now(), which is both testable and guaranteed
        # The "no change short circuit" of _patch_status will not swallow this record.
        previous = str((item.get("status") or {}).get("phase") or "Deploying")
        status_writer(item, previous, message)

    def _reconcile_collection(
        self,
        kind: str,
        path: str,
        handler: Any,
        status_writer: Any,
    ) -> None:
        """Run a round of reconciliation for a class CR.

        The two types of resources were previously two pieces of code that were repeated verbatim. The only difference was the two callbacks of kind/path/——
        The focus should be on every failed path. Making a copy means that you must remember to change two places when adding metrics later.
        """
        collection = self.kube.get(path)
        items = collection.get("items") or []
        OBSERVED_RESOURCES.set(len(items), kind)
        if kind == "sitedeployment":
            self._forget_applied(
                {
                    str((item.get("metadata") or {}).get("name") or "")
                    for item in items
                }
            )
        for item in items:
            name = (item.get("metadata") or {}).get("name", "unknown")
            try:
                handler(item)
            except Exception as exc:
                RECONCILE_TOTAL.inc(kind, "failure")
                telemetry.log_exception(
                    "reconcile_failed", exc, kind=kind, name=name
                )
                try:
                    self._write_reconcile_failure(
                        kind, item, exc, status_writer
                    )
                except Exception as status_exc:
                    # The fact that the status is not written back is more difficult to detect than the failure of reconcile itself: CR will stop at the old
                    # In terms of status, what you see on the platform side is "still being deployed" instead of "failed".
                    RECONCILE_TOTAL.inc(kind, "status_write_failure")
                    telemetry.log_exception(
                        "reconcile_status_write_failed",
                        status_exc,
                        kind=kind,
                        name=name,
                    )
            else:
                RECONCILE_TOTAL.inc(kind, "success")
            # Heartbeat for /healthz: one item done, whatever the outcome.
            LAST_PROGRESS_TIMESTAMP.set(time.time())

    def _reconcile_activator_grant(self) -> None:
        """Maintain cross-namespace reference permissions for activators.

        The HTTPRoute of the STZ site is in the tenant Namespace, and the backendRef refers to the namespace in the control plane.
        Activator Service. The Gateway API requires such cross-namespace references to be on the referenced side
        There is a ReferenceGrant, otherwise the route is marked as ResolvedRefs=False and the site is always 503,
        The configuration of both sides is correct when viewed individually.

        🔴 Why it must be rewritten by the operator every round, rather than written into a static list: ReferenceGrant
        `from[].namespace` only accepts specific names, does not support wildcards or selectors, and tenants
        Namespace is dynamically created with the first deployment. "All tenants ns" cannot be written in the list.

        When there is no STZ site, delete this grant instead of leaving an empty one: `from` must have at least one item,
        An empty list will be rejected by apiserver, and the error will be repeated every 2 seconds, mixed with the real failure.
        together.
        """
        if not exposure.backend().supports_scale_to_zero:
            return
        try:
            payload = self.kube.get(COLLECTION_PATH) or {}
        except (ApiError, RuntimeError) as exc:
            telemetry.log_exception("activator_grant_list_failed", exc)
            return

        namespaces = sorted(
            {
                str((item.get("status") or {}).get("namespace") or "")
                for item in (payload.get("items") or [])
                if (item.get("spec") or {}).get("scaleToZero")
                and str((item.get("spec") or {}).get("exposure", "public")) == "public"
            }
            - {""}
        )
        collection = (
            "/apis/gateway.networking.k8s.io/v1beta1/namespaces/"
            f"{CONTROL_NAMESPACE}/referencegrants"
        )
        path = f"{collection}/{ACTIVATOR_GRANT_NAME}"
        try:
            if not namespaces:
                self.kube.delete(path)
                return
            self.kube.create_or_patch(
                collection, path, _activator_grant_resource(namespaces)
            )
        except ApiError as exc:
            # 404 has two meanings here: ReferenceGrant CRD is not installed (Gateway is not deployed)
            # API), or the object to be deleted does not exist. Neither is a fault - the former explains the
            # The deployment does not use the L7 entry at all.
            if exc.status == 404:
                return
            telemetry.log_exception("activator_grant_failed", exc)
        except RuntimeError as exc:
            telemetry.log_exception("activator_grant_failed", exc)

    def run_once(self) -> None:
        # One trace per sweep. The operator has no inbound request to inherit from, so the
        # unit that is worth correlating is the sweep itself: without this, its log lines
        # carry no trace_id at all and cannot be lined up with the apiserver calls and
        # verification requests they caused.
        token = tracing.bind(tracing.new_trace_id())
        try:
            self._reconcile_collection(
                "sitebuild",
                BUILD_COLLECTION_PATH,
                self.reconcile_build,
                self._patch_build_status,
            )
            self._reconcile_collection(
                "sitedeployment",
                COLLECTION_PATH,
                self.reconcile,
                self._patch_status,
            )
        finally:
            tracing.release(token)
        self._reconcile_activator_grant()

    def run_forever(self) -> None:
        telemetry.log(
            "operator_started",
            namespace=CONTROL_NAMESPACE,
            reconcile_interval_seconds=RECONCILE_INTERVAL,
        )
        while not self._stop.is_set():
            with telemetry.Timer() as timer:
                try:
                    self.run_once()
                except Exception as exc:
                    # The entire round fails (usually the list cannot be opened/RBAC is narrowed), and a single CR fails
                    # are different faults: under the former, all CRs stop converging, but an error log
                    # Will not appear in the per-CR layer.
                    SWEEP_TOTAL.inc("failure")
                    telemetry.log_exception("sweep_failed", exc)
                else:
                    SWEEP_TOTAL.inc("success")
                    LAST_SWEEP_TIMESTAMP.set(time.time())
            RECONCILE_SECONDS.observe(timer.seconds)
            # wait() instead of sleep(): a SIGTERM during the pause returns at once.
            self._stop.wait(RECONCILE_INTERVAL)
        telemetry.log("operator_stopped", namespace=CONTROL_NAMESPACE)


class _MetricsHandler(BaseHTTPRequestHandler):
    """The read-only operation and maintenance side of the operator: `/metrics` and `/healthz`.

    Constraints: This page is read-only and non-authenticated, so **no CR content or tenant identification shall be exposed** ——
    Metric labels only have fixed-base enumerations like kind/outcome. Labels by site name will be
    Causes two things: leaks the tenant list to anyone who can hit the port, and explodes the base.
    """

    server_version = "sites-operator"
    sys_version = ""

    def version_string(self) -> str:
        return self.server_version

    def log_message(self, fmt: str, *args: Any) -> None:
        # BaseHTTPRequestHandler writes each request to stderr by default. The crawler comes every few seconds,
        # That would drown out the real reconcile log.
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/metrics":
            self._respond(200, METRICS.render(), "text/plain; version=0.0.4")
            return
        if self.path == "/healthz":
            stale_after = _health_stale_after()
            now = time.time()
            sweep_age = _age(now, _last_sweep_value())
            progress_age = _age(now, _last_progress_value())
            # Just returning 200 only proves that this thread is still responding, and it is independent of the reconcile loop.
            # daemon thread - it still responds when reconcile is stuck. The criterion is
            # "is the loop making progress", not "did a whole sweep finish recently":
            # a sweep over many sites with probe timeouts can legitimately outlast
            # stale_after, and killing the operator for that only restarts the same
            # slow sweep from scratch. A wedged loop advances neither clock.
            ages = [age for age in (sweep_age, progress_age) if age is not None]
            age = min(ages) if ages else None
            healthy = age is not None and age <= stale_after
            body = json.dumps(
                {
                    "ok": healthy,
                    "lastSweepAgeSeconds": _rounded(sweep_age),
                    "lastProgressAgeSeconds": _rounded(progress_age),
                    "staleAfterSeconds": round(stale_after, 3),
                }
            )
            self._respond(200 if healthy else 503, body, "application/json")
            return
        self._respond(404, "not found\n", "text/plain")

    def _respond(self, status: int, payload: str, content_type: str) -> None:
        encoded = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError):
            pass


# The threshold for determining "sweep stall". Two simultaneous constraints, whichever is larger:
#
#   Number of rounds - Follow RECONCILE_INTERVAL scaling, the threshold will automatically keep up when the interval is increased;
#   Absolute seconds - when the value calculated from the number of rounds is too small.
#
# Simply counting the number of rounds will cause serious misjudgment: a sweep needs to traverse N CRs, and each one takes up to
# VERIFY_TIMEOUT_SECONDS(5s) For forensic detection, it may take 50s for ten sites——
# And 5 rounds x 2s is only 10s. If it is installed as livenessProbe, kubelet will be under normal load.
# Periodically killing the operator and restarting it will not make that round of reconciliation faster, it will only make it start over again.
# So the site can never converge. This is a classic example of "barrier budget being used as a failure detector".
#
# Even the larger threshold cannot cover every sweep: N unreachable Ready sites cost
# N x VERIFY_TIMEOUT_SECONDS per sweep before the probe back-off kicks in, so the
# sweep clock alone would still flag a healthy operator once N grows. /healthz
# therefore also reads the progress heartbeat (LAST_PROGRESS_TIMESTAMP, advanced per
# custom resource) and is green if either clock is within the threshold. The
# threshold thus bounds "time without finishing one item", which is what a real
# deadlock looks like, not "time without finishing one full sweep".
_HEALTH_STALE_SWEEPS = float(getenv("SITES_OPERATOR_HEALTH_STALE_SWEEPS", "5") or "5")
_HEALTH_STALE_FLOOR_SECONDS = float(
    getenv("SITES_OPERATOR_HEALTH_STALE_SECONDS", "300") or "300"
)


def _health_stale_after() -> float:
    return max(RECONCILE_INTERVAL * _HEALTH_STALE_SWEEPS, _HEALTH_STALE_FLOOR_SECONDS)


def _last_sweep_value() -> float:
    for _name, value in LAST_SWEEP_TIMESTAMP.samples():
        return value
    return 0.0


def _last_progress_value() -> float:
    for _name, value in LAST_PROGRESS_TIMESTAMP.samples():
        return value
    return 0.0


def _age(now: float, timestamp: float) -> float | None:
    """Seconds since a gauge timestamp; None while the gauge is still unset (0)."""
    return now - timestamp if timestamp else None


def _rounded(age: float | None) -> float | None:
    return round(age, 3) if age is not None else None


def serve_metrics(port: int = METRICS_PORT) -> ThreadingHTTPServer:
    """Start the background operation and maintenance interface and return to the server for test shutdown."""
    server = ThreadingHTTPServer(("", port), _MetricsHandler)
    server.daemon_threads = True
    thread = threading.Thread(
        target=server.serve_forever, name="sites-operator-metrics", daemon=True
    )
    thread.start()
    telemetry.log("metrics_listening", port=server.server_address[1])
    return server


if __name__ == "__main__":
    # When the log pipe is full, it is better to lose the log than freeze the reconcile thread (see safe_stdout module).
    # Only installed at the entrance of the resident process, not during import: sites.cli / sites.mcp is a one-time installation
    # or stdio protocol processes, replacing their streams has no benefit and an added layer of risk.
    from sites import safe_stdout

    safe_stdout.install()
    telemetry.configure("sites-operator")
    # Before any port is opened and before the first apiserver call: this process
    # writes every tenant-isolating NetworkPolicy, and each of them is "allow
    # 0.0.0.0/0 except the Pod CIDR". A Pod CIDR that matches no Pod excludes
    # nothing, the policies still apply, and `kubectl get netpol` still looks
    # right -- so the only place this can be turned into an observable signal is
    # here, as a refusal to start. See k8s_resources.verify_pod_network for why
    # "cannot check" refuses too rather than passing.
    try:
        verified_pod_cidr = verify_pod_network()
    except ClusterNetworkError as exc:
        telemetry.log("cluster_network_refused", level="error", error=str(exc))
        raise SystemExit(1) from exc
    telemetry.log("cluster_network_verified", pod_cidr=verified_pod_cidr)
    tracing.configure("sites-operator", lambda outcome, amount: TRACE_EXPORT.inc(outcome, amount=amount))
    metrics_server = serve_metrics()
    # PID 1 drops SIGTERM unless a handler is installed; the returned event ends the
    # reconcile loop and the callback stops the metrics listener.
    stop = shutdown.install_stop_handler(metrics_server.shutdown)
    Operator(KubeClient(), stop=stop).run_forever()
    metrics_server.server_close()
    tracing.shutdown()
