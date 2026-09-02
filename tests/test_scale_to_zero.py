"""First scale-to-zero boundary: ownership of ``.spec.replicas``.

These tests do not implement complete scale-to-zero; there is no activator or KEDA. They
prove that operator convergence hands replica ownership to an external scaler instead of
fighting it.

Without this boundary, operator ``create_or_patch`` repeatedly restates ``replicas: 1``,
KEDA scales back to zero, and the conflict accelerates because a just-scaled Deployment is
briefly not ready and therefore remains on the faster reconcile path.
"""

from __future__ import annotations

import datetime as dt
import os
import pathlib
import unittest
import urllib.error
from contextlib import contextmanager
from unittest.mock import patch

from tests import chart

from sites.k8s_resources import deployment_resource
from sites.validation import ValidationError, normalize_deploy_payload
from sites.kube import ApiError
from sites.operator import (
    BUILD_COLLECTION_PATH,
    BUILD_FINALIZER,
    COLLECTION_PATH,
    FINALIZER,
    JOB_COLLECTION_PATH,
    Operator,
    _deployment_ready,
    _desired_replicas,
    _is_transient_error,
)


DEFAULT_MERCHANT_ID = "local"


@contextmanager
def using_backend(name: str):
    """Temporarily switch the exposed backend (same as test_exposure)."""
    previous = os.environ.get("SITES_EXPOSURE_BACKEND")
    os.environ["SITES_EXPOSURE_BACKEND"] = name
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("SITES_EXPOSURE_BACKEND", None)
        else:
            os.environ["SITES_EXPOSURE_BACKEND"] = previous


def payload(**extra):
    return {
        "name": "demo",
        "image": "example.invalid/demo:v1",
        "port": 8080,
        "healthPath": "/healthz",
        **extra,
    }


class AdmissionTests(unittest.TestCase):
    def test_accepted_only_where_it_can_actually_work(self) -> None:
        with using_backend("gateway"):
            spec = normalize_deploy_payload(
                payload(scaleToZero=True), DEFAULT_MERCHANT_ID, "local"
            )
        self.assertIs(spec["scaleToZero"], True)

    def test_rejected_on_nodeport_instead_of_being_ignored(self) -> None:
        """It must be **rejected** under NodePort and cannot be silently ignored.

        The consequence of silent ignoring is that the caller thinks that the site will shrink when it is idle, but in fact it is always running at full capacity, and the bill
        Neither the police nor the supervisor will mention this matter. The same lesson has been learned once with the nodePort field.
        """
        with using_backend("nodeport"):
            with self.assertRaises(ValidationError) as caught:
                normalize_deploy_payload(
                    payload(scaleToZero=True), DEFAULT_MERCHANT_ID, "local"
                )
        self.assertIn("gateway", str(caught.exception))

    def test_public_on_gateway_stays_off_unless_asked(self) -> None:
        """Never derived from the backend: not asking for it does not opt in.

        Deriving it meant a caller that said nothing got scale-to-zero
        whenever it happened to be public on the L7 backend, and every
        request to such a site then goes through the single-replica
        activator -- one more hop that can be down, plus a cold start on the
        first request after idle. This is the one case the derivation
        actually changed, so it is the one that has to be pinned.
        """
        with using_backend("gateway"):
            spec = normalize_deploy_payload(payload(), DEFAULT_MERCHANT_ID, "local")
        self.assertNotIn("scaleToZero", spec)

    def test_explicit_true_still_opts_in(self) -> None:
        """The constant default must not have narrowed the accepted values."""
        with using_backend("gateway"):
            spec = normalize_deploy_payload(
                payload(scaleToZero=True), DEFAULT_MERCHANT_ID, "local"
            )
        self.assertIs(spec["scaleToZero"], True)

    def test_explicit_false_wins_over_default(self) -> None:
        with using_backend("gateway"):
            spec = normalize_deploy_payload(
                payload(scaleToZero=False), DEFAULT_MERCHANT_ID, "local"
            )
        self.assertNotIn("scaleToZero", spec)

    def test_internal_stays_off_by_default(self) -> None:
        """Internal has no entrance that the activator can reach. If it is reduced to 0, no one will wake it up."""
        with using_backend("gateway"):
            spec = normalize_deploy_payload(
                payload(exposure="internal"), DEFAULT_MERCHANT_ID, "local"
            )
        self.assertNotIn("scaleToZero", spec)

    def test_nodeport_defaults_off_instead_of_rejecting(self) -> None:
        """The nodeport backend is off by default: The default value cannot push unexpressed callers into the rejection path."""
        with using_backend("nodeport"):
            spec = normalize_deploy_payload(payload(), DEFAULT_MERCHANT_ID, "local")
        self.assertNotIn("scaleToZero", spec)

    def test_non_boolean_is_rejected(self) -> None:
        with using_backend("gateway"):
            for bad in ("true", 1, None):
                with self.subTest(value=bad):
                    with self.assertRaises(ValidationError):
                        normalize_deploy_payload(
                            payload(scaleToZero=bad), DEFAULT_MERCHANT_ID, "local"
                        )


class OwnershipTests(unittest.TestCase):
    """The transfer of ownership itself. These two items are the main points of this change."""

    def spec_for(self, **extra):
        with using_backend("gateway"):
            return normalize_deploy_payload(
                # Explicitly off: The public site is opened by default. This category tests the ownership when it is not open.
                payload(**{**{'scaleToZero': False}, **extra}), DEFAULT_MERCHANT_ID, "local"
            )

    def test_operator_still_owns_replicas_by_default(self) -> None:
        deployment = deployment_resource(self.spec_for(), "ulocal-local")
        self.assertEqual(deployment["spec"]["replicas"], 1, "The behavior of sites without the switch remains unchanged")

    def test_operator_stops_writing_replicas_once_opted_in(self) -> None:
        """After opening, there cannot be replicas in desired.

        kube.patch uses merge-patch+json, omitting it means "no change" - this is exactly what the field is
        KEDA'S WAY. It is wrong to write `replicas: 0`: that is the operator actively shrinking,
        Instead of giving up ownership, KEDA will still be pushed back in the next round when it wants to pull up.
        """
        deployment = deployment_resource(
            self.spec_for(scaleToZero=True), "ulocal-local"
        )
        self.assertNotIn(
            "replicas",
            deployment["spec"],
            "When scaleToZero is turned on, replicas cannot be written, otherwise they will overwrite each other with the external scaler.",
        )
        # None of the remaining fields should be omitted - only replicas should be omitted.
        for key in ("selector", "strategy", "template"):
            self.assertIn(key, deployment["spec"])


class ReadinessTests(unittest.TestCase):
    """The readiness criterion must be calculated based on the **actual** expected number of replicas in the cluster."""

    def deployment(self, *, spec_replicas=1, available=1, updated=1,
                   unavailable=0, generation=3, observed=3):
        return {
            "metadata": {"generation": generation},
            "spec": {"replicas": spec_replicas},
            "status": {
                "observedGeneration": observed,
                "updatedReplicas": updated,
                "availableReplicas": available,
                "unavailableReplicas": unavailable,
            },
        }

    def test_reads_the_live_spec_not_a_hardcoded_one(self) -> None:
        self.assertEqual(_desired_replicas(self.deployment(spec_replicas=0)), 0)
        self.assertEqual(_desired_replicas({}), 1, "If it cannot be read, it will be treated as 1, which is consistent with the default of K8s.")

    def test_running_site_unchanged(self) -> None:
        self.assertTrue(_deployment_ready(self.deployment()))

    def test_dormant_site_counts_as_ready(self) -> None:
        """0 The replica achieved the desired state, not a failure.

        The cost of being judged as not ready is not only the ugly state: unconverged CR must be re-applied every round.
        (Frequency reduction only takes effect for converged ones), so every time the scaler shrinks, the operator writes the frequency
        30 times better.
        """
        self.assertTrue(
            _deployment_ready(self.deployment(spec_replicas=0, available=0, updated=0))
        )

    def test_still_draining_is_not_ready(self) -> None:
        """Expect 0 but there are still instances running = scaling is in progress, not the final state."""
        self.assertFalse(
            _deployment_ready(self.deployment(spec_replicas=0, available=1, updated=0))
        )

    def test_stale_observed_generation_is_never_ready(self) -> None:
        """This is also true for copy 0: kube-controller has not seen the latest spec."""
        for replicas in (0, 1):
            with self.subTest(replicas=replicas):
                self.assertFalse(
                    _deployment_ready(
                        self.deployment(
                            spec_replicas=replicas,
                            available=replicas,
                            updated=replicas,
                            generation=4,
                            observed=3,
                        )
                    )
                )

    def test_partial_rollout_is_not_ready(self) -> None:
        self.assertFalse(
            _deployment_ready(self.deployment(spec_replicas=2, available=1, updated=1))
        )


class VerificationTests(unittest.TestCase):
    """There is no active detection in the 0 copy - there is no instance to detect, and you will only get a piece of evidence that will inevitably fail."""

    class _Operator:
        """Only borrow this method of Operator and do not construct the entire object."""

        probed = False

        def _verify_workload(self, spec, namespace):
            self.probed = True
            return {"ok": False, "error": "probed"}

        _verification_for = None       # Really implemented by the following binding

    def setUp(self) -> None:
        from sites.operator import Operator

        self.op = self._Operator()
        self._Operator._verification_for = Operator._verification_for

    def test_dormant_site_is_not_probed(self) -> None:
        cr = {"status": {"verification": {"ok": True, "revision": "1"}}}
        got = self.op._verification_for(cr, {"revision": "1"}, "ns", probe=False)
        self.assertFalse(self.op.probed, "0 replicas should not initiate probes")
        self.assertTrue(got["ok"], "The evidence that has been passed must be kept.")

    def test_dormant_site_without_prior_evidence_stays_empty(self) -> None:
        """Reduced to 0 for never passing: Leave blank instead of recording a failure.

        Null forensics and "probe failure" are two different things - the latter can make people think the site is broken.
        """
        got = self.op._verification_for({}, {"revision": "1"}, "ns", probe=False)
        self.assertFalse(self.op.probed)
        self.assertEqual(got, {})

    def test_dormant_new_revision_does_not_reuse_old_evidence(self) -> None:
        cr = {"status": {"verification": {"ok": True, "revision": "1"}}}
        got = self.op._verification_for(cr, {"revision": "2"}, "ns", probe=False)
        self.assertFalse(self.op.probed)
        self.assertEqual(got, {})

    def test_active_site_still_probes(self) -> None:
        got = self.op._verification_for({}, {"revision": "1"}, "ns")
        self.assertTrue(self.op.probed, "Forensics for active sites cannot be skipped by this change")
        self.assertFalse(got["ok"])
        self.assertEqual(1, got["consecutiveFailures"])

    def test_failed_probe_counter_advances_only_for_the_same_revision(self) -> None:
        previous = self._failed(None)
        previous["status"]["verification"]["consecutiveFailures"] = 1
        got = self.op._verification_for(previous, {"revision": "1"}, "ns")
        self.assertEqual(2, got["consecutiveFailures"])

        self.op.probed = False
        got = self.op._verification_for(previous, {"revision": "2"}, "ns")
        self.assertEqual(1, got["consecutiveFailures"])

    def test_successful_retry_resets_the_failure_counter(self) -> None:
        previous = self._failed(None)
        previous["status"]["verification"]["consecutiveFailures"] = 1
        self.op._verify_workload = lambda _spec, _namespace: {
            "ok": True, "revision": "1",
        }
        got = self.op._verification_for(previous, {"revision": "1"}, "ns")
        self.assertEqual(0, got["consecutiveFailures"])

    def _failed(self, age_seconds: float | None, revision: str = "1") -> dict:
        evidence = {"ok": False, "revision": revision, "error": "old"}
        if age_seconds is not None:
            checked = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
                seconds=age_seconds
            )
            evidence["checkedAt"] = checked.isoformat()
        return {"status": {"verification": evidence}}

    def test_failed_probe_is_not_retried_inside_the_backoff(self) -> None:
        """Ready-but-unreachable sites used to cost one probe timeout per sweep."""
        got = self.op._verification_for(self._failed(1.0), {"revision": "1"}, "ns")
        self.assertFalse(self.op.probed, "must reuse the failed evidence inside the window")
        self.assertEqual(got["error"], "old")

    def test_failed_probe_is_retried_after_the_backoff(self) -> None:
        from sites.operator import VERIFY_RETRY_SECONDS

        self.op._verification_for(
            self._failed(VERIFY_RETRY_SECONDS + 1), {"revision": "1"}, "ns"
        )
        self.assertTrue(self.op.probed, "the back-off must expire")

    def test_failed_probe_without_checked_at_is_retried(self) -> None:
        self.op._verification_for(self._failed(None), {"revision": "1"}, "ns")
        self.assertTrue(self.op.probed, "no checkedAt means no back-off to honour")

    def test_failed_probe_of_another_revision_is_retried(self) -> None:
        self.op._verification_for(
            self._failed(1.0, revision="1"), {"revision": "2"}, "ns"
        )
        self.assertTrue(self.op.probed, "a new revision is new evidence, not a retry")


class VerificationBackoffReconcileTests(unittest.TestCase):
    """End to end through reconcile: two sweeps, one probe."""

    def _ready_deployment(self) -> dict:
        return {
            "metadata": {"name": "demo", "generation": 1},
            "spec": {"replicas": 1},
            "status": {
                "observedGeneration": 1,
                "updatedReplicas": 1,
                "availableReplicas": 1,
                "unavailableReplicas": 0,
            },
        }

    def _cr(self, status: dict | None = None) -> dict:
        with using_backend("gateway"):
            spec = normalize_deploy_payload(payload(), DEFAULT_MERCHANT_ID, "local")
        return {
            "metadata": {
                "name": "local-local-demo-abcdef0123456789",
                "generation": 1,
                "finalizers": [FINALIZER],
            },
            "spec": spec,
            "status": status or {},
        }

    def test_two_sweeps_probe_once_then_retry_after_backoff(self) -> None:
        from sites import operator as operator_module

        probes: list[str] = []

        def failing_probe(self, spec, namespace):
            probes.append(namespace)
            return {
                "checkedAt": operator_module._now(),
                "revision": str(spec.get("revision", "1")),
                "ok": False,
                "error": "URLError: unreachable",
            }

        kube = _FakeDeployKube(self._ready_deployment())
        with (
            using_backend("gateway"),
            patch.object(Operator, "_verify_workload", failing_probe),
        ):
            op = Operator(kube)
            op.reconcile(self._cr())
            self.assertEqual(len(probes), 1, "first sweep must probe")
            # Feed the written status back, as the apiserver would on the next list.
            op.reconcile(self._cr(status=kube.status_patches[-1]))
            self.assertEqual(len(probes), 1, "second sweep inside the back-off must not probe")
            aged = dict(kube.status_patches[-1])
            stale = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
                seconds=operator_module.VERIFY_RETRY_SECONDS + 1
            )
            aged["verification"] = {
                **aged["verification"], "checkedAt": stale.isoformat()
            }
            op.reconcile(self._cr(status=aged))
            self.assertEqual(len(probes), 2, "must probe again once the back-off expired")


class RoutingTests(unittest.TestCase):
    """The STZ site's route must point to the activator, not its own Service, which may have zero replicas."""

    def route(self, **extra):
        from sites.k8s_resources import route_resources

        with using_backend("gateway"):
            spec = normalize_deploy_payload(
                # Same as above: Explicitly turn off the resident form, STZ use case to pass True
                payload(**{**{'scaleToZero': False}, **extra}), DEFAULT_MERCHANT_ID, "local"
            )
            return route_resources(spec, "ulocal-local")[0]

    def test_plain_site_points_at_its_own_service(self) -> None:
        ref = self.route()["spec"]["rules"][0]["backendRefs"][0]
        self.assertEqual(ref["name"], "demo")
        self.assertNotIn("namespace", ref, "The same reference as ns should not include namespace")

    def test_dormant_site_points_at_the_activator(self) -> None:
        """When directly referring to the site's own Service, zero replicas = the gateway gets one without endpoint
        The backend returns 503, and no link will wake it up."""
        from sites import exposure

        ref = self.route(scaleToZero=True)["spec"]["rules"][0]["backendRefs"][0]
        self.assertEqual(ref["name"], exposure.ACTIVATOR_SERVICE)
        self.assertEqual(ref["port"], exposure.ACTIVATOR_PORT)

    def test_activator_ref_carries_its_namespace(self) -> None:
        """🔴 Cross-namespace references must write namespace.

        If you don’t write it, Gateway will find a Service called sites-activator in **tenant ns**.
        If it cannot be found, mark the route as ResolvedRefs=False - the site is always 503, and the backendRef line
        Looks completely normal.
        """
        from sites import exposure

        ref = self.route(scaleToZero=True)["spec"]["rules"][0]["backendRefs"][0]
        self.assertEqual(ref["namespace"], exposure.ACTIVATOR_NAMESPACE)


class ReferenceGrantTests(unittest.TestCase):
    """Permissions referenced across namespaces, rewritten by the operator each round based on the actual set of tenants."""

    def operator(self, items):
        from sites.kube import ApiError
        from sites.operator import Operator

        class StubKube:
            def __init__(self):
                self.writes: list[tuple[str, dict]] = []
                self.deletes: list[str] = []
                self.crd_missing = False

            def get(self, path):
                if path.endswith("/sitedeployments"):
                    return {"items": items}
                raise ApiError(404, "not found")

            def create_or_patch(self, collection, path, body):
                if self.crd_missing:
                    raise ApiError(404, "the server could not find the requested resource")
                self.writes.append((path, body))
                return body

            def delete(self, path, body=None):
                if self.crd_missing:
                    raise ApiError(404, "not found")
                self.deletes.append(path)
                return {}

            def patch(self, path, body):
                return body

        kube = StubKube()
        return Operator(kube), kube

    def item(self, *, stz=True, namespace="ns-a", exposure_kind="public"):
        with using_backend("gateway"):
            spec = normalize_deploy_payload(
                # stz explicitly passes boolean: omitting it is equal to on under the new default
                payload(exposure=exposure_kind, scaleToZero=stz),
                DEFAULT_MERCHANT_ID,
                "local",
            )
        return {"spec": spec, "status": {"namespace": namespace}}

    def test_grant_lists_every_tenant_namespace_that_opted_in(self) -> None:
        op, kube = self.operator(
            [
                self.item(namespace="ns-a"),
                self.item(namespace="ns-b"),
                self.item(namespace="ns-plain", stz=False),
            ]
        )
        with using_backend("gateway"):
            op._reconcile_activator_grant()
        self.assertEqual(len(kube.writes), 1)
        grant = kube.writes[0][1]
        froms = sorted(entry["namespace"] for entry in grant["spec"]["from"])
        self.assertEqual(froms, ["ns-a", "ns-b"], "Tenants who have not turned on the switch should not be included in the permission list")

    def test_grant_pins_the_service_name(self) -> None:
        """`to` Not writing name is equivalent to releasing the reference to the Service of the entire control plane ns ——
        There are also sites-api and registry."""
        from sites import exposure

        op, kube = self.operator([self.item()])
        with using_backend("gateway"):
            op._reconcile_activator_grant()
        to = kube.writes[0][1]["spec"]["to"][0]
        self.assertEqual(to["name"], exposure.ACTIVATOR_SERVICE)
        self.assertEqual(to["kind"], "Service")

    def test_grant_is_removed_when_nobody_opts_in(self) -> None:
        """Delete it when there is no STZ site, instead of leaving an empty one: `from` must have at least one item,
        Empty lists are rejected by apiserver and that error repeats every 2 seconds."""
        op, kube = self.operator([self.item(stz=False)])
        with using_backend("gateway"):
            op._reconcile_activator_grant()
        self.assertEqual(kube.writes, [])
        self.assertEqual(len(kube.deletes), 1)

    def test_nothing_happens_on_a_backend_without_l7(self) -> None:
        """There is no HTTPRoute under the NodePort topology, and there are no cross-ns references to allow."""
        op, kube = self.operator([self.item()])
        with using_backend("nodeport"):
            op._reconcile_activator_grant()
        self.assertEqual((kube.writes, kube.deletes), ([], []))

    def test_missing_crd_is_not_an_error(self) -> None:
        """When the Gateway API is not installed, the ReferenceGrant type does not exist at all.

        That means this deployment does not use the L7 entrance, and it is not a fault - if it is not silent, one message will be refreshed every 2 seconds.
        """
        op, kube = self.operator([self.item()])
        kube.crd_missing = True
        with using_backend("gateway"):
            op._reconcile_activator_grant()      # Don't throw
        self.assertEqual(kube.writes, [])


class AutoscalingTests(unittest.TestCase):
    """Scaling strategy handed over to KEDA."""

    def scaled(self, **extra):
        from sites.k8s_resources import autoscaling_resources

        with using_backend("gateway"):
            spec = normalize_deploy_payload(
                # Same as OwnershipTests.spec_for: off by default, explicitly pass True for open use cases
                payload(**{**{'scaleToZero': False}, **extra}), DEFAULT_MERCHANT_ID, "local"
            )
            return autoscaling_resources(spec, "ns-a")

    def test_plain_site_gets_no_scaled_object(self) -> None:
        self.assertEqual(self.scaled(), [], "The number of replicas of sites where the switch is not turned on still belongs to the operator.")

    def test_scales_between_zero_and_one(self) -> None:
        spec = self.scaled(scaleToZero=True)[0]["spec"]
        self.assertEqual(spec["minReplicaCount"], 0, "If the scale is less than 0, there is no scale-to-zero.")
        self.assertEqual(spec["maxReplicaCount"], 1, "The single-copy assumption has not changed")

    def test_initial_cooldown_is_set(self) -> None:
        """🔴 The symptom of this missing item is "deployed successfully but cannot be opened".

        KEDA's cooldownPeriod only counts after "it was once active and then turned inactive";
        A new ScaledObject that has never been active will be polled as soon as the index is 0 for the first time.
        Reset to zero - a site that has just been deployed and no one has had time to access it will disappear in seconds.
        """
        spec = self.scaled(scaleToZero=True)[0]["spec"]
        self.assertGreater(
            spec.get("initialCooldownPeriod", 0),
            0,
            "If initialCooldownPeriod is not set, the new site will be shortened on the first poll.",
        )

    def test_metric_is_asked_per_host_on_the_admin_port(self) -> None:
        """Ask by host instead of service name: the service name is unique within the tenant."""
        from sites import exposure

        trigger = self.scaled(scaleToZero=True)[0]["spec"]["triggers"][0]
        url = trigger["metadata"]["url"]
        self.assertEqual(trigger["type"], "metrics-api")
        self.assertIn(f":{exposure.ACTIVATOR_ADMIN_PORT}/scale-metrics", url)
        self.assertIn("host=", url)
        self.assertEqual(trigger["metadata"]["activationTargetValue"], "0")

    def test_polling_interval_fits_inside_the_observation_window(self) -> None:
        """🔴 Cross-module beat contract.

        KEDA asks "how many recent requests" every pollingInterval, and the activator only remembers
        IDLE_WINDOW_SECONDS so long. When the polling interval is larger than the window, the traffic between two polls will
        Falling outside the window - the site being visited will be read as 0 and then shrunk, while the configurations on both sides are useless when viewed individually.
        Reasonable.
        """
        from sites import activator
        from sites.k8s_resources import KEDA_POLLING_SECONDS

        self.assertLessEqual(
            KEDA_POLLING_SECONDS,
            activator.IDLE_WINDOW_SECONDS,
            "The polling interval exceeds the activator's observation window, and the active site will be mistakenly shortened.",
        )


class CleanupTests(unittest.TestCase):
    """When the site is deleted, everything built by the operator will also be deleted."""

    def test_routes_and_scaled_objects_are_cleaned_up(self) -> None:
        """HTTPRoute was not in the cleanup list before: deleting a site alone would leave a link that has disappeared.
        Service's route not only occupies that host, but will also be marked as ResolvedRefs=False by the gateway.
        When the entire tenant is deleted, it will be covered by namespace cleaning, but not when a single site is deleted.
        """
        source = (
            pathlib.Path(__file__).resolve().parent.parent
            / "src" / "sites" / "operator.py"
        ).read_text(encoding="utf-8")
        cleanup = source[source.index("def _cleanup(self"):]
        cleanup = cleanup[: cleanup.index("def _should_apply")]
        # The ScaledObject path must come from scaled_object_path, not from
        # autoscaling_resources: the latter is empty once scaleToZero is off.
        for generator in ("route_resources", "scaled_object_path"):
            self.assertIn(
                generator,
                cleanup,
                f"_cleanup is not pressed {generator} Delete the objects it created",
            )


class _RecordingKube:
    """Absorbs writes, records deletes; delete raises the scripted ApiError if any."""

    def __init__(self, delete_error: ApiError | None = None) -> None:
        self.deleted: list[str] = []
        self.written: list[str] = []
        self.delete_error = delete_error

    def get(self, path: str) -> dict:
        raise AssertionError(f"unexpected get: {path}")

    def create_or_patch(self, collection: str, path: str, body: dict) -> dict:
        self.written.append(path)
        return body

    def patch(self, path: str, body: dict) -> dict:
        return body

    def delete(self, path: str, body: dict | None = None) -> dict:
        self.deleted.append(path)
        if self.delete_error is not None:
            raise self.delete_error
        return {}


class OrphanScaledObjectTests(unittest.TestCase):
    """scaleToZero true→false must remove the ScaledObject KEDA is still driving.

    autoscaling_resources returns [] once the flag is off, so the apply loop never
    touches the old object; KEDA keeps scaling to 0 while the operator reasserts
    replicas=1 on every drift resync and the site oscillates forever.
    """

    def _spec(self, **extra):
        with using_backend("gateway"):
            return normalize_deploy_payload(
                payload(**{"scaleToZero": False, **extra}), DEFAULT_MERCHANT_ID, "local"
            )

    def _path(self, spec) -> str:
        from sites.k8s_resources import scaled_object_path

        return scaled_object_path(spec, "ns-a")

    def test_path_helper_matches_what_the_generator_creates(self) -> None:
        """One name formula: the path we delete is the path we would have created."""
        from sites.k8s_resources import autoscaling_resources

        spec = self._spec(scaleToZero=True)
        with using_backend("gateway"):
            scaled = autoscaling_resources(spec, "ns-a")[0]
        created = (
            f"/apis/{scaled['apiVersion']}/namespaces/ns-a/"
            f"{scaled['kind'].lower()}s/{scaled['metadata']['name']}"
        )
        self.assertEqual(self._path(spec), created)

    def test_apply_deletes_scaled_object_when_flag_is_off(self) -> None:
        spec = self._spec(scaleToZero=False)
        kube = _RecordingKube()
        with using_backend("gateway"):
            Operator(kube)._apply_workload(spec, "ns-a")
        self.assertIn(self._path(spec), kube.deleted)
        self.assertNotIn(self._path(spec), kube.written)

    def test_apply_keeps_scaled_object_when_flag_is_on(self) -> None:
        spec = self._spec(scaleToZero=True)
        kube = _RecordingKube()
        with using_backend("gateway"):
            Operator(kube)._apply_workload(spec, "ns-a")
        self.assertIn(self._path(spec), kube.written)
        self.assertNotIn(self._path(spec), kube.deleted)

    def test_apply_tolerates_missing_scaled_object(self) -> None:
        """404 covers both "never had one" and "KEDA CRD not installed"."""
        spec = self._spec(scaleToZero=False)
        kube = _RecordingKube(delete_error=ApiError(404, "not found"))
        with using_backend("gateway"):
            Operator(kube)._apply_workload(spec, "ns-a")
        self.assertIn(self._path(spec), kube.deleted)

    def test_apply_surfaces_other_delete_errors(self) -> None:
        spec = self._spec(scaleToZero=False)
        kube = _RecordingKube(delete_error=ApiError(403, "forbidden"))
        with using_backend("gateway"), self.assertRaises(ApiError):
            Operator(kube)._apply_workload(spec, "ns-a")

    def test_cleanup_deletes_scaled_object_even_when_flag_is_off(self) -> None:
        """A site deleted after the flag was switched off still owns the object."""
        from sites.naming import namespace_for_tenant

        spec = self._spec(scaleToZero=False)
        namespace = namespace_for_tenant(spec["merchantID"], spec["userID"])
        cr = {
            "metadata": {
                "name": "local-local-demo-abcdef0123456789",
                "deletionTimestamp": "2026-08-27T00:00:00Z",
                "finalizers": [FINALIZER],
            },
            "spec": spec,
        }
        kube = _RecordingKube()
        with using_backend("gateway"):
            Operator(kube)._cleanup(cr)
        from sites.k8s_resources import scaled_object_path

        self.assertIn(scaled_object_path(spec, namespace), kube.deleted)


class RealClusterRegressionTests(unittest.TestCase):
    """The first test of the true cluster (2026-08-19) exposed and repaired the few items that were not guarded.

    What they have in common is that it is naturally not true in a unit testing environment, so none of them were red before.
    """

    def spec(self, **extra):
        with using_backend("gateway"):
            return normalize_deploy_payload(
                # Same as above: This category compares to the permanent form
                payload(**{**{'scaleToZero': False}, **extra}), DEFAULT_MERCHANT_ID, "local"
            )

    def crd(self, name):
        for doc in chart.documents("00-platform.yaml"):
            if doc.get("kind") == "CustomResourceDefinition":
                if doc["metadata"]["name"].startswith(name):
                    return doc
        raise AssertionError(f"CRD not found {name}")

    def test_gateway_specs_satisfy_the_crd_required_list(self) -> None:
        """🔴 The spec generated by the gateway backend must pass the required requirement of CRD.

        On the real cluster, the performance is "Creation is always 422": CR under the Gateway backend deliberately does not have nodePort.
        (To prevent the port pool from being instantly "exhausted" by the placeholder value when switching back to nodeport), the schema also requires it.
        The single test only adjusts normalize_deploy_payload and never sends the results to apiserver for verification.
        Therefore, both sides are self-consistent and sewn in the middle.
        """
        schema = self.crd("sitedeployments")["spec"]["versions"][0]["schema"]
        required = set(schema["openAPIV3Schema"]["properties"]["spec"]["required"])
        for kwargs in ({}, {"scaleToZero": True}, {"exposure": "internal"}):
            with self.subTest(**kwargs):
                missing = required - set(self.spec(**kwargs))
                self.assertEqual(missing, set(), f"CR is missing required fields:{missing}")

    def test_nodeport_backend_still_provides_what_it_requires(self) -> None:
        """Reverse: nodePort must be present under the NodePort backend, and its required field is guaranteed by the admission layer."""
        with using_backend("nodeport"):
            spec = normalize_deploy_payload(payload(), DEFAULT_MERCHANT_ID, "local")
        self.assertIn("nodePort", spec)

    def _ingress_sources(self, spec):
        from sites.k8s_resources import network_policy_resources

        sources = []
        for policy in network_policy_resources(spec, "ns-a"):
            for rule in policy["spec"].get("ingress") or []:
                sources.extend(rule.get("from") or [])
        return sources

    def _mentions_activator(self, sources) -> bool:
        from sites import exposure

        for source in sources:
            pod = (source.get("podSelector") or {}).get("matchLabels") or {}
            if pod.get("app.kubernetes.io/name") == exposure.ACTIVATOR_SERVICE:
                return True
        return False

    def test_dormant_sites_let_the_activator_in(self) -> None:
        """🔴 After the route points to the activator, its forwarding to the site must also pass the inbound policy.

        Cilium silently loses packets without this one: the activator log is TimeoutError, site 502,
        HTTPRoute and endpoint are all normal. In the single test, both processes are on the local machine, and the strategy layer is not at all
        Not involved, therefore undetectable - this assertion is its only guard.
        """
        self.assertTrue(
            self._mentions_activator(self._ingress_sources(self.spec(scaleToZero=True))),
            "Sites with scaleToZero turned on do not release activators, and forwarding packets will be silently lost.",
        )

    def test_plain_sites_do_not_widen_their_ingress(self) -> None:
        """The traffic of the site without STZ does not pass through the activator, and the extra openings are just the attack surface——
        Activator is the only control plane component that directly faces public network requests."""
        self.assertFalse(
            self._mentions_activator(self._ingress_sources(self.spec())),
            "Sites that do not have scaleToZero enabled should not release the activator",
        )


class ActivatorIngressTests(unittest.TestCase):
    """Incoming convergence of activator.

    🔴 The most important thing is the operation and maintenance port: /scale-metrics. Answer by host. Ask each question one by one to get a complete
    A list of tenant sites, which was previously open to any Pod in the cluster - including applications deployed by the tenant itself.
    """

    def policy(self):
        for doc in chart.documents("09-activator.yaml"):
            if doc.get("kind") == "NetworkPolicy":
                return doc
        raise AssertionError("09-Activator.yaml There is no NetworkPolicy")

    def rule_for(self, port: int):
        for rule in self.policy()["spec"]["ingress"]:
            if any(entry.get("port") == port for entry in rule.get("ports", [])):
                return rule
        raise AssertionError(f"Not targeted {port} Port inbound rules")

    def test_it_selects_the_activator(self) -> None:
        spec = self.policy()["spec"]
        self.assertEqual(
            spec["podSelector"]["matchLabels"]["app.kubernetes.io/name"],
            "sites-activator",
        )
        self.assertEqual(spec["policyTypes"], ["Ingress"])

    def test_the_admin_port_is_not_open_to_the_whole_cluster(self) -> None:
        """9090 There cannot be a "Release All Pods" source.

        Empty podSelector / empty namespaceSelector are equal to releasing the entire cluster or the entire
        Namespace - that's what this strategy is going to turn off.
        """
        for entry in self.rule_for(9090)["from"]:
            if "namespaceSelector" in entry:
                self.assertTrue(
                    entry["namespaceSelector"].get("matchLabels"),
                    "Empty namespaceSelector = allow all Namespaces",
                )
            if "podSelector" in entry:
                self.assertTrue(
                    entry["podSelector"].get("matchLabels"),
                    "Empty podSelector = Release all Pods in this Namespace",
                )

    def test_the_admin_port_still_lets_the_kubelet_in(self) -> None:
        """The probe hits /healthz at 9090, and the traffic originating from the host falls outside the Pod network segment.

        The consequence of blocking it is that the Pod will always be NotReady, and the activator itself will be fine - this kind of
        The "policy is written tightly to lock yourself out" form, which has been used once during the renovation of the entrance floor.
        """
        blocks = [
            entry["ipBlock"]
            for entry in self.rule_for(9090)["from"]
            if "ipBlock" in entry
        ]
        self.assertTrue(blocks, "If the node source is not released, the kubelet probe will be blocked.")
        self.assertTrue(
            any(block.get("except") for block in blocks),
            "0.0.0.0/0 without except is equivalent to including the Pod network segment.",
        )

    def test_the_forwarding_port_only_admits_the_shared_gateway(self) -> None:
        """🔴 The data plane Namespace must be released and recognized by owning-gateway.

        Envoy Gateway builds the data plane Pod in envoy-gateway-system, and the Gateway object is in
        sites-gateway - According to the latter, the policy syntax is legal and the apply is successful, but the traffic is still rejected.
        The label cannot be used as app.kubernetes.io/name: that will destroy **any** Gateway data plane.
        Put it in and build another Gateway in the same cluster to bypass this strategy.
        """
        sources = self.rule_for(8090)["from"]
        self.assertEqual(len(sources), 1, "There should only be one source for forwarding")
        source = sources[0]
        self.assertEqual(
            source["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"],
            "envoy-gateway-system",
            "What is released is the ns where the Gateway object is located, not the ns where the data plane is located.",
        )
        self.assertIn(
            "gateway.envoyproxy.io/owning-gateway-name",
            source["podSelector"]["matchLabels"],
            "Release by app name will include the data plane of any Gateway.",
        )


class ActivatorRbacTests(unittest.TestCase):
    """Activator is the only control plane component that directly faces public network requests, and the write permission must only reach scale."""

    def rules(self):
        for doc in chart.documents("09-activator.yaml"):
            if doc.get("kind") == "ClusterRole":
                return doc["rules"]
        raise AssertionError("There is no ClusterRole in 09-activator.yaml")

    def test_can_scale_but_cannot_rewrite_workloads(self) -> None:
        by_resource = {
            resource: set(rule["verbs"])
            for rule in self.rules()
            for resource in rule["resources"]
        }
        self.assertEqual(by_resource.get("deployments/scale"), {"patch"})
        self.assertEqual(
            by_resource.get("deployments"),
            {"get"},
            "Giving write permission to deployments is equivalent to allowing the activator to change the image and securityContext",
        )


if __name__ == "__main__":
    unittest.main()


class MemoryLimitTests(unittest.TestCase):
    """memoryLimit: 512Mi hard cap on egress, with admission bounds."""

    def spec_for(self, **extra):
        with using_backend("gateway"):
            return normalize_deploy_payload(
                payload(**extra), DEFAULT_MERCHANT_ID, "local"
            )

    def test_out_of_range_or_malformed_is_rejected(self) -> None:
        for bad in ("100Mi", "3Gi", "abc", "512", "1Ti"):
            with self.subTest(value=bad):
                with self.assertRaises(ValidationError):
                    self.spec_for(memoryLimit=bad)

    def test_absent_by_default_keeps_spec_shape(self) -> None:
        self.assertNotIn("memoryLimit", self.spec_for())

    def test_workload_limit_follows_but_requests_do_not(self) -> None:
        """Requests do not follow limit: the schedule is based on requests, and the following will change "how far it can run"
        It becomes "how big it must be"."""
        with using_backend("gateway"):
            deployment = deployment_resource(
                self.spec_for(memoryLimit="2Gi"), "ulocal-local"
            )
        resources = deployment["spec"]["template"]["spec"]["containers"][0][
            "resources"
        ]
        self.assertEqual(resources["limits"]["memory"], "2Gi")
        # 64Mi: measured RSS of tenant container <30Mi, 128Mi is 4 times higher and direct
        # Converted into one less tenant to schedule (2026-08-22 sites-w1 requests 83% incident)
        self.assertEqual(resources["requests"]["memory"], "64Mi")

    def test_default_limit_unchanged_for_existing_specs(self) -> None:
        with using_backend("gateway"):
            deployment = deployment_resource(self.spec_for(), "ulocal-local")
        resources = deployment["spec"]["template"]["spec"]["containers"][0][
            "resources"
        ]
        self.assertEqual(resources["limits"]["memory"], "512Mi")


class _FakeDeployKube:
    """Minimal stub for reconcile(SiteDeployment): deployment returns by get,
    All write operations are absorbed, and the status patch is recorded for assertion."""

    def __init__(self, deployment: dict, pods: dict | None = None) -> None:
        self.deployment = deployment
        self.pods = pods or {"items": []}
        self.status_patches: list[dict] = []
        self.operations: list[tuple[str, str, dict | None]] = []

    def get(self, path: str) -> dict:
        if "/deployments/" in path:
            return self.deployment
        if "/pods?" in path:
            return self.pods
        raise AssertionError(f"unexpected get: {path}")

    def create(self, path: str, body: dict) -> dict:
        return body

    def create_or_patch(
        self, collection_path: str, resource_path: str, body: dict
    ) -> dict:
        self.operations.append(("write", resource_path, body))
        return body

    def patch(self, path: str, body: dict) -> dict:
        if path.endswith("/status"):
            self.status_patches.append(body["status"])
            self.operations.append(("status", path, body))
        return body

    def delete(self, path: str, body: dict | None = None) -> dict:
        self.operations.append(("delete", path, body))
        return {}


def _transitioning_deployment(replicas: int, generation: int = 5) -> dict:
    """spec has reached the new number of replicas and status has not converged in the transition window."""
    return {
        "metadata": {"name": "demo", "generation": generation},
        "spec": {"replicas": replicas},
        "status": {
            "observedGeneration": generation,
            "updatedReplicas": 0,
            "availableReplicas": 0,
            "unavailableReplicas": 1,
        },
    }


class OperatorScaleEventTests(unittest.TestCase):
    """External scaling event resets 120s window - scaling transition is not a deployment failure."""

    def _reconcile(
        self,
        deployment: dict,
        observed_replicas: int,
        started_at: str,
        *,
        phase: str = "Running",
        ready: bool = True,
    ):
        from sites.operator import Operator
        from unittest.mock import patch

        with using_backend("gateway"):
            spec = normalize_deploy_payload(
                payload(scaleToZero=True), DEFAULT_MERCHANT_ID, "local"
            )
        cr = {
            "metadata": {
                "name": "local-local-demo-abcdef0123456789",
                "generation": 5,
                "uid": "demo-uid",
                "finalizers": ["sites.local/operator"],
            },
            "spec": spec,
            "status": {
                "phase": phase,
                "ready": ready,
                "observedGeneration": 5,
                "namespace": "ulocal-local",
                "startedAt": started_at,
                "observedReplicas": observed_replicas,
            },
        }
        kube = _FakeDeployKube(deployment)
        with (
            using_backend("gateway"),
            patch.object(Operator, "_verification_for", return_value=None),
        ):
            Operator(kube).reconcile(cr)
        return kube

    def test_scale_event_resets_window_instead_of_failing(self) -> None:
        """KEDA brings the transition window from 0 back to 1: the old startedAt must have exceeded 120s,
        But this is a scaling transfer, not a deployment failure."""
        kube = self._reconcile(
            _transitioning_deployment(replicas=1),
            observed_replicas=0,
            started_at="2026-08-19T09:00:00+00:00",
        )
        self.assertTrue(kube.status_patches)
        status = kube.status_patches[-1]
        self.assertNotEqual(status["phase"], "Failed")
        self.assertEqual(status["observedReplicas"], 1)
        self.assertNotEqual(
            status["startedAt"], "2026-08-19T09:00:00+00:00",
            "The scaling event must reset the window, otherwise the transition period elapsed will inevitably time out.",
        )

    def test_running_site_losing_readiness_gets_a_fresh_window(self) -> None:
        """Eviction / OOM kill on a converged site is not a failed rollout.

        startedAt still dates from the original rollout, so without a reset the very
        first not-ready sweep sees elapsed >= DEPLOY_TIMEOUT and writes Failed.
        """
        kube = self._reconcile(
            _transitioning_deployment(replicas=1),
            observed_replicas=1,
            started_at="2026-08-19T09:00:00+00:00",
        )
        status = kube.status_patches[-1]
        self.assertEqual(status["phase"], "Deploying")
        self.assertFalse(status["ready"])
        self.assertNotEqual(
            status["startedAt"], "2026-08-19T09:00:00+00:00",
            "losing readiness after Running must restart the deploy clock",
        )

    def test_genuine_stall_without_scale_event_still_fails(self) -> None:
        """120s semantic retention when there is no scaling event: a really stuck deployment will still fail.
        And the message comes with the reason for the stuck (here comes from the waiting reason of the pod).

        The reset above happens once (the sweep that first sees not-ready writes
        ready=False); a site still not ready after the window must still fail.
        """
        kube = self._reconcile(
            _transitioning_deployment(replicas=1),
            observed_replicas=1,
            started_at="2026-08-19T09:00:00+00:00",
            phase="Deploying",
            ready=False,
        )
        self.assertTrue(kube.status_patches)
        status = kube.status_patches[-1]
        self.assertEqual(status["phase"], "Failed")
        self.assertIn("not ready within", status["message"])

    def test_new_revision_is_verified_before_keda_is_enabled(self) -> None:
        with using_backend("gateway"):
            spec = normalize_deploy_payload(
                payload(scaleToZero=True), DEFAULT_MERCHANT_ID, "local"
            )
        spec["revision"] = "revision-6"
        cr = {
            "metadata": {
                "name": "local-local-demo-abcdef0123456789",
                "generation": 6,
                "uid": "demo-uid",
                "finalizers": ["sites.local/operator"],
            },
            "spec": spec,
            "status": {
                "phase": "Running",
                "ready": True,
                "observedGeneration": 5,
                "startedAt": "2026-08-28T09:00:00+00:00",
                "observedReplicas": 1,
                "verification": {
                    "ok": True,
                    "revision": "revision-5",
                },
            },
        }
        deployment = {
            "metadata": {"name": "demo", "generation": 6},
            "spec": {"replicas": 1},
            "status": {
                "observedGeneration": 6,
                "updatedReplicas": 1,
                "availableReplicas": 1,
                "unavailableReplicas": 0,
            },
        }
        kube = _FakeDeployKube(deployment)
        verification = {"ok": True, "revision": "revision-6", "httpStatus": 200}
        with (
            using_backend("gateway"),
            patch.object(Operator, "_verification_for", return_value=verification),
        ):
            Operator(kube).reconcile(cr)

        scaled_writes = [
            index
            for index, (action, path, _body) in enumerate(kube.operations)
            if action == "write" and "/scaledobjects/" in path
        ]
        status_writes = [
            index
            for index, (action, _path, _body) in enumerate(kube.operations)
            if action == "status"
        ]
        self.assertTrue(scaled_writes)
        self.assertTrue(status_writes)
        self.assertLess(status_writes[-1], scaled_writes[-1])

        deployment_writes = [
            body
            for action, path, body in kube.operations
            if action == "write" and "/deployments/" in path
        ]
        self.assertEqual(deployment_writes[0]["spec"]["replicas"], 1)
        self.assertNotIn("replicas", deployment_writes[-1]["spec"])


class TransientErrorClassificationTests(unittest.TestCase):
    """The classification assistant itself: the criterion must be against the real throw point of sites/kube.py."""

    def test_infrastructure_faults_are_transient(self) -> None:
        for exc in (
            # kube.py converts all URLError/naked TimeoutError into naked RuntimeError and throws them.
            RuntimeError("Kubernetes API unavailable: connection refused"),
            RuntimeError("Kubernetes API timed out after 10.0s"),
            TimeoutError("timed out"),
            urllib.error.URLError("connection reset"),
            ApiError(500, "etcd is unhealthy"),
            ApiError(503, "the server is currently unable to handle the request"),
            ApiError(408, "request timeout"),
            ApiError(429, "too many requests"),
        ):
            with self.subTest(error=repr(exc)):
                self.assertTrue(_is_transient_error(exc))

    def test_client_errors_stay_permanent(self) -> None:
        """🔴 ApiError is a subclass of RuntimeError: it must be diverted first when classifying, otherwise 403/404
        All are eaten up by the transport layer rules, and not a single permanent error is left."""
        for exc in (
            ApiError(403, "forbidden"),
            ApiError(404, "not found"),
            ApiError(409, "already exists"),
            ApiError(422, "spec is invalid"),
        ):
            with self.subTest(error=repr(exc)):
                self.assertFalse(_is_transient_error(exc))

    def test_business_errors_and_bugs_stay_permanent(self) -> None:
        for exc in (
            ValidationError("port must be between 1 and 65535"),
            KeyError("nodePort"),
            ValueError("not a runtime error"),
        ):
            with self.subTest(error=repr(exc)):
                self.assertFalse(_is_transient_error(exc))


class _FailureScript:
    """Press "Path Fragment + Count" to inject a one-time exception; it will automatically recover after use, just simulate shaking.

    times instead of Boolean: the test needs to run two rounds of reconcile to verify "normal convergence in the next round", the injection must
    Only consumed in the first round.
    """

    def __init__(self) -> None:
        self._entries: list[list] = []

    def on_get(self, needle: str, error: Exception, times: int = 1) -> None:
        self._entries.append([needle, times, error])

    def error_for(self, path: str) -> Exception | None:
        for entry in self._entries:
            if entry[1] and entry[0] in path:
                entry[1] -= 1
                return entry[2]
        return None


def _transient_build() -> dict:
    """SiteBuild that can directly enter reconcile_build, the nodePort is legal and the finalizer is in place."""
    return {
        "apiVersion": "sites.local/v1alpha1",
        "kind": "SiteBuild",
        "metadata": {
            "name": "local-local-dynamic-web-0123456789abcdef",
            "generation": 1,
            "finalizers": [BUILD_FINALIZER],
        },
        "spec": {
            "merchantID": "local",
            "userID": "local",
            "serviceName": "dynamic-web",
            "sourcePath": "sources/dynamic-web",
            "artifactSha256": "0123456789abcdef" * 4,
            "dockerfile": "Dockerfile",
            "repository": "local/local/dynamic-web",
            "tag": "sha256-aaaa",
            "port": 8080,
            "nodePort": 30082,
            "healthPath": "/healthz",
            "revision": "123",
        },
    }


class _FlakyBuildKube:
    """List version of FakeBuildKube(test_builds): supports _reconcile_collection
    Collect GET, and inject a fault according to the script - the only way to get the bottom of the branch is to go true _reconcile_collection
    was executed.

    Calls outside the contract always raise AssertionError, which is the same as _FakeDeployKube: the pile is silently absorbed
    If there is an unexpected call, the assertion green will just be "the assertion was not hit".
    """

    def __init__(self, build: dict) -> None:
        self.build = build
        self.job: dict | None = None
        self.patched: list[tuple[str, dict]] = []
        self.created: list[tuple[str, dict]] = []
        self.failures = _FailureScript()

    def get(self, path: str) -> dict:
        if (error := self.failures.error_for(path)) is not None:
            raise error
        if path == BUILD_COLLECTION_PATH:
            return {"items": [self.build]}
        if "/jobs/" in path:
            if self.job is None:
                raise ApiError(
                    404, f'jobs.batch "{path.rsplit("/", 1)[-1]}" not found'
                )
            return self.job
        if "/sitedeployments/" in path:
            return {
                "metadata": {"name": self.build["metadata"]["name"]},
                "status": {"phase": "Running", "ready": True},
            }
        raise AssertionError(f"unexpected get: {path}")

    def create(self, path: str, body: dict) -> dict:
        if path != JOB_COLLECTION_PATH:
            raise AssertionError(f"unexpected create: {path}")
        self.created.append((path, body))
        self.job = body
        return body

    def patch(self, path: str, body: dict) -> dict:
        self.patched.append((path, body))
        return body

    def create_or_patch(self, collection: str, path: str, body: dict) -> dict:
        raise AssertionError(f"unexpected create_or_patch: {path}")

    def delete(self, path: str, body: dict | None = None) -> dict:
        raise AssertionError(f"unexpected delete: {path}")


class TransientBuildFailureTests(unittest.TestCase):
    """An apiserver flutter cannot write the build as Failed - that is a termination state."""

    def _sweep(self, kube: _FlakyBuildKube, operator: Operator) -> None:
        with patch("sites.operator.prepare_build_metadata"):
            operator._reconcile_collection(
                "sitebuild",
                BUILD_COLLECTION_PATH,
                operator.reconcile_build,
                operator._patch_build_status,
            )

    @staticmethod
    def _statuses(kube: _FlakyBuildKube) -> list[dict]:
        return [
            body["status"]
            for path, body in kube.patched
            if path.endswith("/status")
        ]

    def test_transport_blip_keeps_the_build_recoverable(self) -> None:
        kube = _FlakyBuildKube(_transient_build())
        kube.failures.on_get(
            "/jobs/", RuntimeError("Kubernetes API unavailable: connection refused")
        )
        operator = Operator(kube)
        self._sweep(kube, operator)
        statuses = self._statuses(kube)
        self.assertTrue(statuses, "Transient errors must also be written back to the status, and the scene must be left in the message")
        self.assertEqual(statuses[-1]["phase"], "Building")
        self.assertIn("Kubernetes API unavailable", statuses[-1]["message"])
        self.assertNotIn(
            "Failed",
            [status["phase"] for status in statuses],
            "If the termination status is written after a jitter, the caller will give up on a construction that would have been successful after two seconds.",
        )
        # The next round of jitter has passed: 404 → Build Job → Return to the normal flow of Building.
        self._sweep(kube, operator)
        statuses = self._statuses(kube)
        self.assertEqual(statuses[-1]["phase"], "Building")
        self.assertEqual(
            statuses[-1]["message"], "Waiting for the bounded BuildKit Job"
        )

    def test_server_errors_are_equally_transient(self) -> None:
        kube = _FlakyBuildKube(_transient_build())
        kube.failures.on_get("/jobs/", ApiError(503, "apiserver is overloaded"))
        self._sweep(kube, Operator(kube))
        status = self._statuses(kube)[-1]
        self.assertEqual(status["phase"], "Building")
        self.assertIn("503", status["message"])

    def test_client_error_still_fails_the_build(self) -> None:
        kube = _FlakyBuildKube(_transient_build())
        kube.failures.on_get(
            "/jobs/", ApiError(403, 'jobs.batch "build-x" is forbidden')
        )
        self._sweep(kube, Operator(kube))
        status = self._statuses(kube)[-1]
        self.assertEqual(status["phase"], "Failed", "RBAC errors such as this will not change if you try again.")
        self.assertIn("403", status["message"])
        self.assertIs(status["ready"], False)


class _FlakyDeployKube(_FakeDeployKube):
    """_FakeDeployKube adds two things: a collection list, and conditionally injected one-time failures."""

    def __init__(self, deployment: dict, items: list[dict]) -> None:
        super().__init__(deployment)
        self.items = items
        self.failures = _FailureScript()

    def get(self, path: str) -> dict:
        if (error := self.failures.error_for(path)) is not None:
            raise error
        if path == COLLECTION_PATH:
            return {"items": self.items}
        return super().get(path)


def _ready_deployment() -> dict:
    return {
        "metadata": {"name": "demo", "generation": 5},
        "spec": {"replicas": 1},
        "status": {
            "observedGeneration": 5,
            "updatedReplicas": 1,
            "availableReplicas": 1,
            "unavailableReplicas": 0,
        },
    }


def _running_cr() -> dict:
    """Converged SiteDeployment: It must still appear healthy during transient errors."""
    spec = normalize_deploy_payload(payload(), DEFAULT_MERCHANT_ID, "local")
    return {
        "metadata": {
            "name": "local-local-demo-abcdef0123456789",
            "generation": 5,
            "uid": "demo-uid",
            "finalizers": [FINALIZER],
        },
        "spec": spec,
        "status": {
            "phase": "Running",
            "ready": True,
            "message": "Deployment rollout completed",
            "observedGeneration": 5,
            "namespace": "ulocal-local",
            "startedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
            "observedReplicas": 1,
        },
    }


class TransientDeploymentFailureTests(unittest.TestCase):
    """Same thing on the SiteDeployment side: Transient errors do not escalate the phase, and are distinguishable from true failures."""

    def _sweep(self, kube: _FlakyDeployKube, operator: Operator) -> None:
        with patch.object(Operator, "_verification_for", return_value=None):
            operator._reconcile_collection(
                "sitedeployment",
                COLLECTION_PATH,
                operator.reconcile,
                operator._patch_status,
            )

    def test_transport_blip_keeps_the_previous_phase(self) -> None:
        kube = _FlakyDeployKube(_ready_deployment(), [_running_cr()])
        kube.failures.on_get(
            "/deployments/", ApiError(503, "etcd is not available")
        )
        operator = Operator(kube)
        self._sweep(kube, operator)
        self.assertTrue(kube.status_patches)
        status = kube.status_patches[-1]
        self.assertEqual(status["phase"], "Running", "If a read fails, the phase will not be upgraded.")
        self.assertIn("503", status["message"])
        self.assertIn("etcd", status["message"], "The scene should be left in the message")
        self.assertIn("Transient", status["message"])
        self.assertNotIn(
            "ready", status, "If merge-patch is omitted, no changes will be made. The ready state should not be washed away by jitter."
        )
        # Next round of recovery: Normal convergence back to regular copywriting.
        self._sweep(kube, operator)
        status = kube.status_patches[-1]
        self.assertEqual(status["phase"], "Running")
        self.assertEqual(status["message"], "Deployment rollout completed")

    def test_client_error_still_fails_the_deployment(self) -> None:
        kube = _FlakyDeployKube(_ready_deployment(), [_running_cr()])
        kube.failures.on_get(
            "/deployments/",
            ApiError(403, 'deployments.apps "demo" is forbidden'),
        )
        self._sweep(kube, Operator(kube))
        status = kube.status_patches[-1]
        self.assertEqual(status["phase"], "Failed")
        self.assertIn("403", status["message"])
        self.assertIs(status["ready"], False)
