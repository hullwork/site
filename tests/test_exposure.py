"""Exposure-backend regressions for NodePort and gateway modes.

These tests cover quiet failure modes from the exposure split:

1. Gateway NetworkPolicy must not copy NodePort's PodCIDR exception—gateway Pods are in
   that CIDR and a copied exception exposes or blocks every site incorrectly.
2. Skipping port allocation is not equivalent to skipping quota; both checks live in the
   same admission function.
3. “Unlimited” must be represented as None, never by inventing a large capacity number.
4. Internal-site behavior must be identical under both backends because it never uses the
   public route.
"""
from __future__ import annotations

import os
import pathlib
import unittest
from contextlib import contextmanager

from tests import chart

from sites import exposure
from sites.k8s_resources import (
    cluster_pod_cidr,
    network_policy_resources,
    route_resources,
    service_resource,
)
from sites.validation import normalize_deploy_payload
from os import getenv
from sites.operator import public_url_for_spec
from tests.test_tenancy import ADMIN_TOKEN

DEFAULT_MERCHANT_ID = getenv("SITES_DEFAULT_MERCHANT_ID", "local") or "local"


@contextmanager
def using_backend(name: str):
    """Temporarily switch the exposed backend.

    Exposure.backend() rereads env every time it is called just to make this possible - file it to the module level
    It will let "the test of the first import determine which backend all subsequent tests run on". When there is a problem with that kind of coupling
    Does not point to env at all.
    """
    previous = os.environ.get("SITES_EXPOSURE_BACKEND")
    os.environ["SITES_EXPOSURE_BACKEND"] = name
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("SITES_EXPOSURE_BACKEND", None)
        else:
            os.environ["SITES_EXPOSURE_BACKEND"] = previous


def _spec(*, merchant=DEFAULT_MERCHANT_ID, user="local", **overrides):
    payload = {
        "name": "demo",
        "image": "example.invalid/demo:v1",
        "port": 8080,
        "healthPath": "/healthz",
        # Stated rather than omitted: these cases are about the resident form,
        # and should not change meaning if the admission default ever moves.
        "scaleToZero": False,
        **overrides,
    }
    spec = normalize_deploy_payload(payload, merchant, user)
    spec.setdefault("nodePort", 30080)
    return spec


class BackendSelectionTest(unittest.TestCase):
    def test_default_is_nodeport(self) -> None:
        """The default must be the existing behavior, and switching is an explicit action."""
        previous = os.environ.pop("SITES_EXPOSURE_BACKEND", None)
        try:
            self.assertEqual(exposure.backend().name, "nodeport")
        finally:
            if previous is not None:
                os.environ["SITES_EXPOSURE_BACKEND"] = previous

    def test_unknown_backend_fails_loudly(self) -> None:
        """If you type a wrong name, you must report an error, and you cannot silently fall back to the default backend.

        The consequences of silent rollback are: it is thought that the Gateway is switched, but the NodePort is actually still being sent, while the URL,
        Service mode, NetworkPolicy will consistently "look right" until the port pool is exhausted.
        """
        with using_backend("gatway"):  # misspelled on purpose
            with self.assertRaises(ValueError):
                exposure.backend()

    def test_backend_is_reread_on_every_call(self) -> None:
        with using_backend("gateway"):
            self.assertEqual(exposure.backend().name, "gateway")
        with using_backend("nodeport"):
            self.assertEqual(exposure.backend().name, "nodeport")


class CapacityTest(unittest.TestCase):
    def test_nodeport_capacity_is_the_pool_size(self) -> None:
        with using_backend("nodeport"):
            self.assertEqual(
                exposure.backend().capacity, len(exposure.NODE_PORT_RANGE)
            )

    def test_nodeport_pool_is_small_enough_to_matter(self) -> None:
        """This upper limit is not a tuning parameter, but a hard cap for the total number of public sites, which is worth nailing."""
        self.assertLessEqual(
            len(exposure.NODE_PORT_RANGE),
            16,
            "The pool is very small; make sure topology.PORT_FORWARDS is added before changing the size.",
        )

    def test_gateway_capacity_is_unbounded_and_expressed_as_none(self) -> None:
        """Uncapped must be None.

        Implementing to a very large number would give a false capacity promise when someone reads it in the future, and
        `capacity is not None` This type of judgment will silently enter the "capacity" branch.
        """
        with using_backend("gateway"):
            self.assertIsNone(exposure.backend().capacity)

    def test_gateway_does_not_allocate_ports(self) -> None:
        with using_backend("gateway"):
            self.assertFalse(exposure.backend().allocates_ports)
        with using_backend("nodeport"):
            self.assertTrue(exposure.backend().allocates_ports)


class ServiceShapeTest(unittest.TestCase):
    def test_nodeport_backend_keeps_existing_shape(self) -> None:
        with using_backend("nodeport"):
            service = service_resource(_spec(), "ulocal-local")
        self.assertEqual(service["spec"]["type"], "NodePort")
        self.assertEqual(service["spec"]["externalTrafficPolicy"], "Local")
        self.assertEqual(service["spec"]["ports"][0]["nodePort"], 30080)

    def test_gateway_backend_uses_clusterip_and_drops_the_port(self) -> None:
        """The site no longer occupies the host port, and all external traffic enters through the gateway."""
        with using_backend("gateway"):
            service = service_resource(_spec(), "ulocal-local")
        self.assertEqual(service["spec"]["type"], "ClusterIP")
        self.assertNotIn("externalTrafficPolicy", service["spec"])
        self.assertNotIn("nodePort", service["spec"]["ports"][0])

    def test_internal_exposure_is_identical_across_backends(self) -> None:
        """The internal site does not pass through the entrance, and must be word-for-word identical under both backends."""
        spec = _spec(exposure="internal")
        with using_backend("nodeport"):
            a = service_resource(spec, "ulocal-local")
        with using_backend("gateway"):
            b = service_resource(spec, "ulocal-local")
        self.assertEqual(a, b)
        self.assertEqual(a["spec"]["type"], "ClusterIP")


class RouteResourceTest(unittest.TestCase):
    def test_nodeport_backend_emits_no_route_object(self) -> None:
        with using_backend("nodeport"):
            self.assertEqual(route_resources(_spec(), "ulocal-local"), [])

    def test_gateway_backend_emits_one_httproute_bound_to_the_host(self) -> None:
        with using_backend("gateway"):
            routes = route_resources(_spec(), "ulocal-local")
        self.assertEqual(len(routes), 1)
        route = routes[0]
        self.assertEqual(route["kind"], "HTTPRoute")
        self.assertTrue(route["apiVersion"].startswith("gateway.networking.k8s.io/"))
        self.assertEqual(route["metadata"]["namespace"], "ulocal-local")
        host = route["spec"]["hostnames"][0]
        self.assertTrue(
            host.endswith(exposure.DOMAIN_SUFFIX),
            f"{host} Not under the configured domain name suffix",
        )
        backend_ref = route["spec"]["rules"][0]["backendRefs"][0]
        self.assertEqual(backend_ref["port"], 8080)

    def test_httproute_parent_is_cross_namespace(self) -> None:
        """The site is in the tenant ns and the Gateway is in its own ns. The parentRef must contain namespace.

        When the namespace is omitted, HTTPRoute will go to this ns to find the Gateway with the same name. If it cannot find it, it will stop.
        Accepted=False, and the site performance is just "cannot be accessed", and there will be no error reporting here.
        """
        with using_backend("gateway"):
            route = route_resources(_spec(), "ulocal-local")[0]
        parent = route["spec"]["parentRefs"][0]
        self.assertEqual(parent["namespace"], exposure.GATEWAY_NAMESPACE)
        self.assertEqual(parent["name"], exposure.GATEWAY_NAME)

    def test_internal_site_never_gets_a_public_route(self) -> None:
        spec = _spec(exposure="internal")
        for name in ("nodeport", "gateway"):
            with using_backend(name):
                self.assertEqual(
                    route_resources(spec, "ulocal-local"), [], f"backend={name}"
                )

    def test_stz_route_carries_a_wake_sized_request_timeout(self) -> None:
        """STZ routing requests must accommodate a cold start (0 → 1 and then forward). Envoy defaults to ~15s.
        Wake-up in progress is intercepted with 504; resident routing is not relaxed - it should fail quickly."""
        with using_backend("gateway"):
            stz = route_resources(
                _spec(scaleToZero=True), "ulocal-local"
            )[0]
            plain = route_resources(_spec(), "ulocal-local")[0]
        self.assertEqual(
            stz["spec"]["rules"][0]["timeouts"]["request"],
            exposure.STZ_ROUTE_TIMEOUT,
        )
        self.assertNotIn("timeouts", plain["spec"]["rules"][0])

    def test_hosts_are_distinct_per_service(self) -> None:
        with using_backend("gateway"):
            a = route_resources(_spec(name="alpha"), "ulocal-local")[0]
            b = route_resources(_spec(name="beta"), "ulocal-local")[0]
        self.assertNotEqual(a["spec"]["hostnames"], b["spec"]["hostnames"])

    def test_hosts_are_distinct_across_tenants_sharing_a_service_name(self) -> None:
        """🔴 Sites with the same name across tenants must obtain different hosts.

        serviceName is unique only within the tenant (namespace is the isolation boundary). Two HTTPRoutes
        When the same hostname is declared in different namespaces and hung on the same Gateway,
        The Gateway API will pick a winner - visitors from one tenant get the site content of another tenant,
        The deployment on both sides shows success, the URL is "normal", and no error is reported on either side.

        Simply dividing by serviceName (the one above) cannot prove this: the two tenants use the same **
        Same serviceName.
        """
        with using_backend("gateway"):
            a = route_resources(
                _spec(name="web", merchant="acme", user="alice"), "ns-a"
            )[0]
            b = route_resources(
                _spec(name="web", merchant="other", user="bob"), "ns-b"
            )[0]
        self.assertNotEqual(
            a["spec"]["hostnames"],
            b["spec"]["hostnames"],
            "The sites with the same name of two tenants hit the same host, which is a cross-tenant traffic leak.",
        )

    def test_same_tenant_and_service_is_stable(self) -> None:
        """The same (merchant, tenant, service) gets the same host every time - otherwise the URL will drift."""
        with using_backend("gateway"):
            first = route_resources(
                _spec(name="web", merchant="acme", user="alice"), "ns-a"
            )[0]
            second = route_resources(
                _spec(name="web", merchant="acme", user="alice"), "ns-a"
            )[0]
        self.assertEqual(first["spec"]["hostnames"], second["spec"]["hostnames"])

    def test_host_is_a_single_label_under_the_domain_suffix(self) -> None:
        """🔴 The host name must be the **first-level** subdomain under the domain name suffix.

        The listener wildcard of the Gateway API only matches a single tag (`*` exclusive to the first tag, specification
        Plain text): `*.<suffix>` matches `a.<suffix>`, but **does not match** `a.b.<suffix>`. put the tenant
        To paraphrase the summary, every route falls outside the listener's matching range - all sites are routed
        No, but the configurations of HTTPRoute and Gateway are correct when viewed individually.

        The same holds true for real domain names: `*.*.apps.example.com` This two-level wildcard is used by most DNS service providers
        Not supported.
        """
        from sites import exposure

        with using_backend("gateway"):
            host = route_resources(
                _spec(name="web", merchant="acme", user="alice"), "ns-a"
            )[0]["spec"]["hostnames"][0]
        self.assertTrue(host.endswith("." + exposure.DOMAIN_SUFFIX), host)
        label = host[: -(len(exposure.DOMAIN_SUFFIX) + 1)]
        self.assertNotIn(".", label, f"Hostnames have multiple levels above the suffix:{host}")
        self.assertLessEqual(len(label), 63, "DNS label limit")

    def test_long_service_names_stay_within_one_label_and_stay_distinct(self) -> None:
        """Truncation does not break injecting: the digest input carries the full service name."""
        from sites import exposure

        long_a, long_b = "a" * 50 + "xxx", "a" * 50 + "yyy"
        with using_backend("gateway"):
            host_a = route_resources(_spec(name=long_a), "ns-a")[0]["spec"]["hostnames"][0]
            host_b = route_resources(_spec(name=long_b), "ns-a")[0]["spec"]["hostnames"][0]
        for host in (host_a, host_b):
            label = host[: -(len(exposure.DOMAIN_SUFFIX) + 1)]
            self.assertLessEqual(len(label), 63, host)
            self.assertNotIn(".", label, host)
        self.assertNotEqual(host_a, host_b, "The first 46 characters of the same long name hit the same host")

class NetworkPolicyTest(unittest.TestCase):
    """🔴 The most hidden aspect of renovation."""

    def _public_sources(self, backend_name: str):
        with using_backend(backend_name):
            policies = network_policy_resources(_spec(), "ulocal-local")
        return policies[1]["spec"]["ingress"][0]["from"]

    def test_nodeport_backend_excludes_the_pod_cidr(self) -> None:
        sources = self._public_sources("nodeport")
        blocks = [item for item in sources if "ipBlock" in item]
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["ipBlock"]["except"], [cluster_pod_cidr()])

    def test_gateway_backend_admits_the_gateway_pod(self) -> None:
        sources = self._public_sources("gateway")
        selectors = [
            item
            for item in sources
            if item.get("namespaceSelector", {})
            .get("matchLabels", {})
            .get("kubernetes.io/metadata.name")
            == exposure.GATEWAY_DATA_PLANE_NS
        ]
        self.assertEqual(len(selectors), 1, "There is no Namespace where the gateway data plane is located.")
        self.assertEqual(
            selectors[0]["podSelector"]["matchLabels"][
                exposure.GATEWAY_POD_LABEL_KEY
            ],
            exposure.GATEWAY_POD_LABEL_VALUE,
        )

    def test_gateway_backend_must_not_keep_the_pod_cidr_exclusion(self) -> None:
        """Core assertion: The gateway Pod is in the Pod network segment, leaving except will block it.

        The symptom is that all sites receive 502, and the troubleshooting direction will point to the gateway configuration instead of NetworkPolicy——
        The purpose of this assertion is to make "copy NodePort's rule" red on the spot.
        """
        sources = self._public_sources("gateway")
        offending = [
            item
            for item in sources
            if cluster_pod_cidr() in (item.get("ipBlock", {}).get("except") or [])
        ]
        self.assertEqual(
            offending,
            [],
            "The Gateway backend is still excluding Pod network segments, and the Gateway Pod will be blocked by its own policy.",
        )

    def test_policy_targets_the_data_plane_namespace_not_the_gateway_object(self) -> None:
        """🔴The data plane Pod of Envoy Gateway is not in the ns of the Gateway object.

        If released according to the ns where the Gateway object is located, the policy syntax is legal and the apply is successful, but the actual
        The data plane Pod of the site is still rejected - the symptoms are all 502, and the troubleshooting will first go to the Gateway
        status (everything is fine there). This only has discriminating power when the two ns names are different, so by the way
        Assert that they are indeed different.
        """
        self.assertNotEqual(
            exposure.GATEWAY_DATA_PLANE_NS,
            exposure.GATEWAY_NAMESPACE,
            "Two ns with the same name will make this guard lose its ability to identify",
        )
        namespaces = [
            item.get("namespaceSelector", {})
            .get("matchLabels", {})
            .get("kubernetes.io/metadata.name")
            for item in self._public_sources("gateway")
            if "namespaceSelector" in item
        ]
        self.assertIn(exposure.GATEWAY_DATA_PLANE_NS, namespaces)
        self.assertNotIn(exposure.GATEWAY_NAMESPACE, namespaces)

    def test_gateway_pod_selector_is_owner_scoped(self) -> None:
        """The label must be able to distinguish which Gateway's data plane it is.

        If you use a general label such as app.kubernetes.io/name, it is the same as **any** Gateway under ns
        All traffic will be put into the tenant site.
        """
        sources = self._public_sources("gateway")
        selector = next(item for item in sources if "podSelector" in item and "namespaceSelector" in item)
        key = next(iter(selector["podSelector"]["matchLabels"]))
        self.assertIn("owning-gateway", key, f"{key} Not a label distinguished by Gateway attribution")

    def test_own_namespace_stays_reachable_in_both_backends(self) -> None:
        for name in ("nodeport", "gateway"):
            sources = self._public_sources(name)
            self.assertIn({"podSelector": {}}, sources, f"backend={name}")


class HostPortMappingTest(unittest.TestCase):
    """The host under the Gateway backend only needs **one** portForward - but it must exist.

    The corresponding assertions for the NodePort backend are in test_sites.PortMappingContractTests (each
    The ports must be mapped). The two are two back-end forms of the same type of contract: the symptoms of drifting are exactly the same——
    The deployment is successful, the control plane forensics also passes (using the address within the cluster), and only the user can retrieve the returned URL.
    Only to find that it couldn't be opened.
    """

    def _mappings(self) -> dict[int, int]:
        from sites import topology

        forwards = topology.PORT_FORWARDS
        return {int(e["guest"]): int(e["host"]) for e in forwards}

    def test_gateway_node_port_is_mapped_to_the_host(self) -> None:
        mappings = self._mappings()
        self.assertIn(
            exposure.GATEWAY_NODE_PORT,
            mappings,
            "The gateway's NodePort does not host portForward, and all site URLs cannot be opened.",
        )

    def test_gateway_host_port_matches_the_url_we_hand_out(self) -> None:
        """The port returned to the user must be the host-side port of portForward.

        If these two numbers are maintained separately, the URL will point to a port that no one is listening to.
        """
        mappings = self._mappings()
        self.assertEqual(
            mappings[exposure.GATEWAY_NODE_PORT],
            exposure.GATEWAY_HOST_PORT,
            "The host port used by public_url_for does not match the lima portForward",
        )


class EnvoyProxyPatchTest(unittest.TestCase):
    """The shape of the Service patch in EnvoyProxy.

    The first test of the real cluster (2026-08-19) stepped on: strategic merge pair list should be aligned according to the merge key.
    Service's ports use `port` as key. When only name+nodePort is given, apiserver reports
    "does not contain declared merge key: port", **EnvoyProxy's infra creates the entire segment
    Failure**, Gateway is stuck at Programmed=False / AddressNotAssigned - and the error is reported at
    In the log of the Envoy Gateway controller, it is not in the output of our apply.
    """

    # The data-plane Service shape is a chart value, so it has to be selected
    # here.  The retired manifests/ copy hardcoded NodePort 30080 and these
    # assertions read that copy; the chart defaults to LoadBalancer and renders
    # no nodePort at all.  Two copies of one fact that had already drifted, and
    # the checksum gate between them compared each copy against its own recorded
    # hash, so it could never have reported the disagreement.
    NODE_PORT_SHAPE = (
        "--set-string", "gateway.serviceType=NodePort",
        "--set", f"gateway.httpNodePort={exposure.GATEWAY_NODE_PORT}",
    )

    def ports(self):
        for doc in chart.documents("08-gateway.yaml", *self.NODE_PORT_SHAPE):
            if doc.get("kind") == "EnvoyProxy":
                return (
                    doc["spec"]["provider"]["kubernetes"]["envoyService"]["patch"]
                    ["value"]["spec"]["ports"]
                )
        raise AssertionError("08-gateway.yaml renders no EnvoyProxy")

    def test_every_port_entry_carries_the_merge_key(self) -> None:
        entries = self.ports()
        self.assertTrue(entries, "There is not a single port in the patch")
        for entry in entries:
            self.assertIn(
                "port",
                entry,
                f"Without strategic-merge key, apiserver will reject the entire patch:{entry}",
            )

    def test_the_node_port_reaches_the_patch_the_url_formula_reads(self) -> None:
        """The value must survive the template's ``if``, not just be accepted.

        ``gateway.httpNodePort`` is rendered only when ``serviceType`` is
        NodePort.  Get that condition wrong and the chart accepts the port,
        reports nothing, and emits a patch with no nodePort in it -- the
        gateway then answers on an arbitrary port while public_url_for keeps
        handing out GATEWAY_NODE_PORT.
        """
        node_ports = {entry.get("nodePort") for entry in self.ports()}
        self.assertIn(exposure.GATEWAY_NODE_PORT, node_ports)


class ExposureConfigMapTest(unittest.TestCase):
    """The configuration of the L7 entry must have only one source and be actually read by three processes.

    Operator presses SITES_DOMAIN_SUFFIX to spell the hostname of HTTPRoute, activator presses the same
    The value is calculated as the key of the routing table, and the api uses it to spell back the URL to the caller. When the three places are in their proper place, any one of them will drift away.
    The performance is "the site cannot be accessed", and the three configurations are reasonable when viewed individually.
    """

    KEYS = {
        "SITES_EXPOSURE_BACKEND",
        "SITES_DOMAIN_SUFFIX",
        "SITES_GATEWAY_SCHEME",
        "SITES_GATEWAY_HOST_PORT",
        "SITES_REGISTRY_PULL_HOST",
    }
    def docs(self, name):
        return chart.documents(name)

    def config_map(self):
        for doc in self.docs("08-gateway.yaml"):
            if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "sites-exposure":
                return doc
        raise AssertionError("There is no sites-exposure ConfigMap in 08-gateway.yaml")

    def deployments(self):
        found = {}
        for name in ("09-activator.yaml", "10-control-plane.yaml"):
            for doc in self.docs(name):
                if doc["kind"] == "Deployment":
                    found[doc["metadata"]["name"]] = doc
        return found

    def test_config_map_carries_every_knob(self) -> None:
        self.assertEqual(set(self.config_map()["data"]) & self.KEYS, self.KEYS)

    def test_every_consumer_actually_mounts_it(self) -> None:
        """🔴 Without envFrom, everything written in ConfigMap will be dead.

        There is a configuration in the list that looks completely correct, but the running state takes all default values ​​this state
        Can't see any problem from kubectl get configmap.
        """
        for name, deploy in self.deployments().items():
            container = deploy["spec"]["template"]["spec"]["containers"][0]
            sources = {
                ref["configMapRef"]["name"]
                for ref in container.get("envFrom", [])
                if "configMapRef" in ref
            }
            self.assertIn("sites-exposure", sources, f"{name} Not linked sites-exposure")

    def test_nobody_shadows_the_shared_values(self) -> None:
        """Explicit env has higher priority than envFrom: if you write one with the same name, ConfigMap will be silently overwritten."""
        for name, deploy in self.deployments().items():
            container = deploy["spec"]["template"]["spec"]["containers"][0]
            shadowed = {e["name"] for e in container.get("env", [])} & self.KEYS
            self.assertEqual(
                shadowed, set(), f"{name} Covered shared configuration with explicit env:{shadowed}"
            )

    def test_config_map_agrees_with_the_gateway_listener(self) -> None:
        """The domain name suffix has three expressions: the default value of exposure.py, this ConfigMap, and the listener's
        Wildcard hostname. Correcting one thing and missing two will result in 404 for all sites."""
        from sites import exposure

        suffix = self.config_map()["data"]["SITES_DOMAIN_SUFFIX"]
        gateway = [d for d in self.docs("08-gateway.yaml") if d["kind"] == "Gateway"][0]
        self.assertEqual(gateway["spec"]["listeners"][0]["hostname"], f"*.{suffix}")
        self.assertEqual(
            suffix,
            exposure.DOMAIN_SUFFIX,
            "The default suffixes of ConfigMap and exposure.py do not match - both should be changed at the same time",
        )

    def test_gateway_uses_a_distinct_workload_registry_name(self) -> None:
        self.assertEqual(
            self.config_map()["data"]["SITES_REGISTRY_PULL_HOST"],
            "site-registry.convee.local:5000",
        )


class GatewayManifestContractTest(unittest.TestCase):
    """The constants in charts/site/templates/08-gateway.yaml and exposure.py must be consistent.

    These are two expressions of the same fact: the code is spelled GATEWAY_NAME/GATEWAY_NAMESPACE
    parentRef, press DOMAIN_SUFFIX to spell hostname, and the gateway side must really call that name, really
    Connect those Hosts on that listener. The symptom is that HTTPRoute stops at Accepted=False
    Or no listener is matched - the site side only shows "not accessible" and does not point here.

    The same type of guard as test_sites.PortMappingContractTests, but with a different backend.
    """

    def setUp(self) -> None:
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML is only in the dev dependency; CI is installed, but native bare python may not.")

    def _docs(self) -> list[dict]:
        return chart.documents("08-gateway.yaml")

    def _doc(self, kind: str) -> dict:
        return next(d for d in self._docs() if d["kind"] == kind)

    def test_gateway_identity_matches_the_parent_ref_code_emits(self) -> None:
        gateway = self._doc("Gateway")
        self.assertEqual(gateway["metadata"]["name"], exposure.GATEWAY_NAME)
        self.assertEqual(
            gateway["metadata"]["namespace"], exposure.GATEWAY_NAMESPACE
        )

    def test_listener_hostname_covers_the_domain_suffix(self) -> None:
        listener = self._doc("Gateway")["spec"]["listeners"][0]
        self.assertEqual(listener["hostname"], f"*.{exposure.DOMAIN_SUFFIX}")

    def test_listener_admits_routes_from_tenant_namespaces(self) -> None:
        """Must be a Selector: HTTPRoute is built in tenant ns, and Gateway is in its own ns.

        Leaving the default Same will cause every tenant route to be silently rejected.
        """
        listener = self._doc("Gateway")["spec"]["listeners"][0]
        namespaces = listener["allowedRoutes"]["namespaces"]
        self.assertEqual(namespaces["from"], "Selector")
        self.assertEqual(
            namespaces["selector"]["matchLabels"].get("sites.local/managed"),
            "true",
            "The selector must match the common.namespace_resource label for tenant ns",
        )

    NODE_PORT_SHAPE = (
        "--set-string", "gateway.serviceType=NodePort",
        "--set", f"gateway.httpNodePort={exposure.GATEWAY_NODE_PORT}",
    )

    def test_data_plane_node_port_matches_the_url_we_hand_out(self) -> None:
        proxy = next(
            doc for doc in chart.documents("08-gateway.yaml", *self.NODE_PORT_SHAPE)
            if doc["kind"] == "EnvoyProxy"
        )
        ports = proxy["spec"]["provider"]["kubernetes"]["envoyService"]["patch"][
            "value"
        ]["spec"]["ports"]
        self.assertEqual(ports[0]["nodePort"], exposure.GATEWAY_NODE_PORT)

    def test_the_data_plane_service_type_is_the_operator_choice(self) -> None:
        """A cluster without a LoadBalancer controller leaves the Service Pending.

        The retired manifests/ copy hardcoded NodePort and this asserted that.
        The chart makes it ``gateway.serviceType``, so the fact to hold is that
        the choice reaches the EnvoyProxy rather than being dropped on the way:
        a template that ignored the value would leave every install on the
        LoadBalancer default and look identical in ``helm lint``.
        """
        for selected in ("NodePort", "ClusterIP", "LoadBalancer"):
            with self.subTest(serviceType=selected):
                proxy = next(
                    doc for doc in chart.documents(
                        "08-gateway.yaml",
                        "--set-string", f"gateway.serviceType={selected}",
                    )
                    if doc["kind"] == "EnvoyProxy"
                )
                service = proxy["spec"]["provider"]["kubernetes"]["envoyService"]
                self.assertEqual(service["type"], selected)


class SingleReplicaInvariantTest(unittest.TestCase):
    """The single-copy assumption of the control plane must be machine-verifiable and cannot just live in comments.

    Correctness depends on it: the only mutexes for NodePort allocation, tenant quota checks, CR occupancy checks are in-process
    `threading.Lock`. When two processes coexist, the three checks fail at the same time, and the symptom is occasional repeated allocation/
    Over-issued, **not an error**.

    See docs/PRODUCTION.md "Number of Control Plane Copies" for decisions and costs. To change to multiple replicas, you must first change the 8
    The critical section is replaced with cross-copy mutual exclusion - not changing the number here.
    """

    def setUp(self) -> None:
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML is only in the dev dependency")

    def _deployments(self) -> dict[str, dict]:
        docs = chart.documents("10-control-plane.yaml")
        return {
            d["metadata"]["name"]: d
            for d in docs
            if d["kind"] == "Deployment"
        }

    def test_control_plane_deployments_are_single_replica(self) -> None:
        for name, doc in self._deployments().items():
            self.assertEqual(
                doc["spec"].get("replicas"),
                1,
                f"{name} Not a single copy; the three checks for in-process lock protection will fail at the same time",
            )

    def test_control_plane_deployments_use_recreate(self) -> None:
        """RollingUpdate is maxSurge=1 for replicas=1 - two processes coexist during the rolling update.

        This is more hidden than "multiple replicas": the replica count is written as 1, but there are indeed two processes each holding one during the few seconds when the version is released.
        Irrelevant locks.
        """
        for name, doc in self._deployments().items():
            self.assertEqual(
                doc["spec"].get("strategy", {}).get("type"),
                "Recreate",
                f"{name} RollingUpdate is used; during the rolling update, the old and new processes coexist and the lock becomes invalid.",
            )

    def test_both_control_plane_components_are_covered(self) -> None:
        """The scanning surface of the guard itself also needs to be reconciled: missing a component equals no guard."""
        self.assertEqual(
            set(self._deployments()), {"sites-api", "sites-operator"}
        )


class PublicUrlTest(unittest.TestCase):
    def test_nodeport_url_is_host_plus_mapped_port(self) -> None:
        with using_backend("nodeport"):
            url = public_url_for_spec(_spec())
        self.assertEqual(
            url, f"{exposure.PUBLIC_URL_HOST}:{exposure.HOST_PORT_BASE}"
        )

    def test_gateway_url_is_a_hostname_not_a_port_offset(self) -> None:
        with using_backend("gateway"):
            url = public_url_for_spec(_spec())
        self.assertIn(exposure.DOMAIN_SUFFIX, url)
        self.assertTrue(url.startswith(exposure.GATEWAY_SCHEME + "://"))

    def test_internal_site_has_no_public_url_in_either_backend(self) -> None:
        spec = _spec(exposure="internal")
        for name in ("nodeport", "gateway"):
            with using_backend(name):
                self.assertIsNone(public_url_for_spec(spec), f"backend={name}")

    def test_gateway_url_matches_the_httproute_hostname(self) -> None:
        """The URL and the actual route must be the same host.

        If these two points are counted separately, the situation before the transformation will be repeated: the deployment is successful and the control plane forensics is also passed.
        (Using the address within the cluster), only when the user tries to open the returned URL does he find that it cannot be opened.
        """
        with using_backend("gateway"):
            spec = _spec()
            url = public_url_for_spec(spec)
            host = route_resources(spec, "ulocal-local")[0]["spec"]["hostnames"][0]
        self.assertIn(host, url)




class GatewayQuotaEnforcementTests(unittest.TestCase):
    """🔴 The quotas for the lower three layers of the Gateway backend must **take effect** as usual.

    Port allocation and quota checking are written in the same function (_ensure_quota_and_ports), with quota first,
    Assigned later. When adding skip to Gateway, if you return from the beginning of the function, the three-tier quota will be
    Skip - while the API returns 202 as usual, the symptoms will wait until a tenant rolls out hundreds or thousands of sites,
    Or a merchant only becomes visible when the cluster is full. This set of use cases nails that dividing line.

    Directly reuse the service fixture of test_tenancy (real HTTP + PostgreSQL + FakeKube),
    Just replace the exposed backend with gateway.
    """

    @classmethod
    def setUpClass(cls) -> None:
        from tests.test_tenancy import TenancyTests

        cls._base = TenancyTests
        cls._base.setUpClass()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._base.tearDownClass()

    def setUp(self) -> None:
        self._ctx = using_backend("gateway")
        self._ctx.__enter__()
        self.addCleanup(self._ctx.__exit__, None, None, None)
        # Borrow helpers from base classes: they only depend on classmethod level server/url.
        self._helper = self._base(methodName="setUp")
        self._helper.url = self._base.url
        self._helper.setUp()

    def _new_tenant(self, name: str, **quota) -> str:
        return self._helper.new_tenant(name, **quota)

    def _deploy(self, token: str, name: str, **extra):
        return self._helper.deploy(token, name, **extra)

    def test_backend_really_is_gateway_in_the_server_process(self) -> None:
        """Fixture self-test: The server and the test process are the same, and the env switch must really affect it.

        Without this one, the following ones will pass even under the nodeport backend.
        The entire set of use cases degenerates into "testing the existing quota again."
        """
        self.assertEqual(exposure.backend().name, "gateway")
        status, body = self._helper.call("GET", "/v1/capabilities", ADMIN_TOKEN)
        self.assertEqual(status, 200, body)
        self.assertIsNone(
            body["limits"]["publicRoutes"],
            "The Gateway backend should return null capacity; returning a number indicates that the server is still in the nodeport backend",
        )

    def test_public_route_quota_still_applies(self) -> None:
        token = self._new_tenant("gw-route", maxDeployments=5, maxPublicRoutes=1)
        self.assertEqual(self._deploy(token, "site")[0], 202)
        status, body = self._deploy(token, "second-site")
        self.assertEqual(status, 429, body)
        self.assertEqual(body["code"], "quota_exceeded")

    def test_deployment_quota_still_applies(self) -> None:
        token = self._new_tenant("gw-deploy", maxDeployments=2, maxPublicRoutes=5)
        self.assertEqual(self._deploy(token, "one")[0], 202)
        self.assertEqual(self._deploy(token, "two")[0], 202)
        status, body = self._deploy(token, "three")
        self.assertEqual(status, 429, body)
        self.assertEqual(body["code"], "quota_exceeded")

    def test_quota_above_the_nodeport_pool_is_now_accepted(self) -> None:
        """The real benefit: Tenant quotas are no longer stuck with an 8-port pool."""
        oversized = len(exposure.NODE_PORT_RANGE) + 5
        token = self._new_tenant(
            "gw-big", maxDeployments=oversized, maxPublicRoutes=oversized
        )
        self.assertTrue(token)

    def test_deployments_get_no_node_port(self) -> None:
        token = self._new_tenant("gw-nport", maxDeployments=3, maxPublicRoutes=3)
        self.assertEqual(self._deploy(token, "alpha")[0], 202)
        from sites.api import Handler

        # _FakeKube.objects has a CR name as key (not a path), and only hosts SiteDeployment.
        crs = list(Handler.kube.objects.values())
        self.assertTrue(crs, "SiteDeployment not created")
        for cr in crs:
            self.assertNotIn(
                "nodePort",
                cr.get("spec", {}),
                "The Gateway backend should no longer allocate ports to the site",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
