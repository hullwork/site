"""Backend abstraction for exposing sites outside the cluster.

This module centralizes public accessibility: port allocation, Service shape, routing
resources, NetworkPolicy ingress sources, and the URL returned to callers. It does not own
the workload itself; Deployments and ConfigMaps remain in the resource builders.

The layer exists because “port is identity” was previously duplicated across admission,
operator URL construction, resource builders, and local topology. Divergence produced
deployments that looked healthy and passed control-plane verification while the user could
not open the returned URL. Runtime constants now live here and contract tests pin the
deploy-side expression to them.

🔴 The NodePort backend has a hard platform limit: its pool contains only eight ports.
That bounds total public sites and is not a sizing knob. The gateway backend does not share
that constraint.
"""
from __future__ import annotations

import hashlib
from typing import Any

from os import getenv

# --- Parameters for the NodePort backend (sole source of truth) ---
# Each public service occupies one NodePort. The cluster side must map this section to
# Host, see PORT_FORWARDS in topology.py at the root of the repository.
# The size of the pool is the upper limit of the public route of the entire platform (there is no such upper limit under the Gateway backend), and a single tenant
# How many it can occupy is determined by its own quota. The control plane API occupies 30081 by itself, and the pool must skip it.
# Otherwise the first public deployment will push the control plane away.
NODE_PORT_MIN = int(getenv("SITES_NODE_PORT_MIN", "30080") or "30080")
NODE_PORT_MAX = int(getenv("SITES_NODE_PORT_MAX", "30088") or "30088")
NODE_PORT_EXCLUDED = frozenset(
    int(value)
    for value in (getenv("SITES_NODE_PORT_EXCLUDED", "30081") or "").split(",")
    if value.strip()
)
NODE_PORT_RANGE = tuple(
    port
    for port in range(NODE_PORT_MIN, NODE_PORT_MAX + 1)
    if port not in NODE_PORT_EXCLUDED
)


def bounded_public_route_default(configured: int) -> int:
    """Clamp a default quota to the selected backend's physical capacity."""
    capacity = backend().capacity
    return min(configured, capacity) if capacity is not None else configured
# Public NodePort URLs use host_port = HOST_PORT_BASE + nodePort - NODE_PORT_MIN.
# topology.py and its contract tests pin the same mapping, preventing a deployment
# from becoming ready at a host URL that cannot actually be opened.
PUBLIC_URL_HOST = (
    getenv("SITES_PUBLIC_URL_HOST", "http://127.0.0.1") or "http://127.0.0.1"
)
HOST_PORT_BASE = int(getenv("SITES_HOST_PORT_BASE", "18090") or "18090")

# --- Gateway backend parameters ---
# sslip.io resolves <any>.<IP>.sslip.io to the IP, so the local environment can provide each
# A resolvable host name for the site, and the number of sites is unlimited. The offline environment needs to be replaced with self-built wildcard DNS——
# This is just a suffix, not a code dependency.
DOMAIN_SUFFIX = getenv("SITES_DOMAIN_SUFFIX", "127.0.0.1.sslip.io") or "127.0.0.1.sslip.io"
# Host port for the shared gateway. The reference kubeadm cluster exposes it through one
# extra port mapping rather than a directly routable node address.
GATEWAY_HOST_PORT = int(getenv("SITES_GATEWAY_HOST_PORT", "18090") or "18090")
GATEWAY_NODE_PORT = int(getenv("SITES_GATEWAY_NODE_PORT", "30080") or "30080")
GATEWAY_SCHEME = getenv("SITES_GATEWAY_SCHEME", "http") or "http"
# 🔴 There are two different Namespaces here, confusing them will cause NetworkPolicy to release the wrong object:
#
#   GATEWAY_NAMESPACE The ns where the Gateway **object** is located. parentRef of HTTPRoute
#                          Point to it.
#   GATEWAY_DATA_PLANE_NS The **data plane Pod** pulled up by Envoy Gateway for this Gateway
#                          ns where it is located. Envoy Gateway builds them on its own by default
#                          envoy-gateway-system, not the ns of the Gateway object.
#
# What actually reaches the site is the data plane Pod, so NetworkPolicy must allow it according to the latter. If written according to the former
# The policy syntax is completely legal and the apply is successful, but the site still receives rejected traffic - the symptoms are all
# 502/Timeout, and the troubleshooting will first check the status of the Gateway (everything is normal there).
GATEWAY_NAME = getenv("SITES_GATEWAY_NAME", "sites-gateway") or "sites-gateway"
GATEWAY_NAMESPACE = getenv("SITES_GATEWAY_NAMESPACE", "sites-gateway") or "sites-gateway"
# Waker for hibernating sites. It is the same Namespace as the control plane, while HTTPRoute is in the tenant Namespace -
# The backendRef across Namespaces requires a ReferenceGrant on the side of the referenced party, which is determined by the operator each round
# Maintenance (see operator._reconcile_activator_grant).
# DNS label limit 63 = service name budget + hyphens + 16-bit digest.
_HOST_NAME_BUDGET = 63 - 1 - 16

# A single source of truth for the Namespace where the control plane resides. Historically, api/operator/common read the same
# One env and the default value were copied four times - changing the default value requires synchronization everywhere, and the performance of the drift is NetworkPolicy
# The wrong namespace is released (the routing endpoints are all normal, but packets are lost, which is extremely difficult to check). module that requires this value
# Always take it from here. `or` defense: fall back to the default value when explicitly set to an empty string, instead of spelling out an empty Namespace
# CR path.
CONTROL_NAMESPACE = (
    getenv("SITES_CONTROL_NAMESPACE", "sites-local") or "sites-local"
)
ACTIVATOR_NAMESPACE = CONTROL_NAMESPACE
ACTIVATOR_SERVICE = (
    getenv("SITES_ACTIVATOR_SERVICE", "sites-activator") or "sites-activator"
)
ACTIVATOR_PORT = int(getenv("SITES_ACTIVATOR_PORT", "8090") or "8090")
# The port of the operations plane (health + scaling metrics). Separate from the forwarding plane: every path in the forwarding plane belongs to the tenant
# Application, leaving a name like /healthz is to declare that the path will never reach the site.
ACTIVATOR_ADMIN_PORT = int(
    getenv("SITES_ACTIVATOR_ADMIN_PORT", "9090") or "9090"
)
# Request timeout for STZ routing. The backend is activator. Cold start must first wait for 0→1 (activator's
# WAKE_TIMEOUT defaults to 30s), while Envoy's default request timeout is shorter - the wake-up is still in progress,
# The gateway returns 504 first. Here we get the wake-up window plus the forwarding margin; with activator.WAKE_TIMEOUT_SECONDS
# The homology relies on the convention of reading the same env prefix in two places, without import(activator→exposure)
# Depending on the direction reversal, a loop will form).
STZ_ROUTE_TIMEOUT = (
    getenv("SITES_STZ_ROUTE_TIMEOUT", "45s") or "45s"
)
GATEWAY_DATA_PLANE_NS = (
    getenv("SITES_GATEWAY_DATA_PLANE_NAMESPACE", "envoy-gateway-system")
    or "envoy-gateway-system"
)
# The attribution label assigned by Envoy Gateway to the data plane Pod. Use owning-gateway-name instead
# app.kubernetes.io/name: The latter has the same data plane for all Gateways in the same ns.
# Allowing it to pass is equivalent to putting **any** Gateway traffic into the tenant site.
GATEWAY_POD_LABEL_KEY = (
    getenv(
        "SITES_GATEWAY_POD_LABEL_KEY",
        "gateway.envoyproxy.io/owning-gateway-name",
    )
    or "gateway.envoyproxy.io/owning-gateway-name"
)
GATEWAY_POD_LABEL_VALUE = (
    getenv("SITES_GATEWAY_POD_LABEL_VALUE", "") or GATEWAY_NAME
)


def tenant_digest(*parts: str) -> str:
    """Stable digest of several normalized identity segments, 16 hex characters.

    lives here instead of common.py because the dependency direction is common → exposure (common asks
    backend allocates_ports), which in turn will cause a loop. common.namespace_for_tenant normalization
    After calling it, the two places share the same derivation - writing one in each place will inevitably drift.

    The number of segments is given according to purpose: Namespace only needs to distinguish tenants (merchant, user), and host names also need to be distinguished.
    Site (merchant, user, service), because the service name in the host name may be truncated by the maximum length.

    🔴 Input must use
    """
    return hashlib.sha256(
        "\0".join(parts).encode("utf-8")
    ).hexdigest()[:16]


class ExposureBackend:
    """Exposure policy for public sites. Internal sites do not pass here (they are always ClusterIP)."""

    name = "base"
    # The maximum number of public sites. None = No upper limit.
    capacity: int | None = None
    # Whether each exposed component needs to be assigned a scarce port upon admission.
    allocates_ports = False
    # Can the public site be reduced to zero replicas? The criterion is not "willingness", but whether it can be done structurally:
    # After shrinking to 0, something must catch the request and trigger the wake-up, and NodePort directs external traffic
    # DNAT to the site Pod. Without the Pod, no link can hold the connection. Only L7
    # Only at the entrance (the gateway is routed by Host) is there a place to insert the activator.
    supports_scale_to_zero = False

    def service_overrides(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Fields to set additionally on Service.spec."""
        raise NotImplementedError

    def route_resources(
        self, spec: dict[str, Any], namespace: str
    ) -> list[dict[str, Any]]:
        """Route objects required in addition to Service (such as HTTPRoute)."""
        return []

    def public_ingress_from(self, cluster_pod_cidr: str) -> list[dict[str, Any]]:
        """Exposes the inbound source for the site's NetworkPolicy."""
        raise NotImplementedError

    def public_url(self, spec: dict[str, Any]) -> str | None:
        raise NotImplementedError

    def host_for(self, spec: dict[str, Any]) -> str | None:
        """The external hostname of the site; this concept is not available in NodePort mode."""
        return None


class NodePortExposure(ExposureBackend):
    """Existing behavior: The port is the identity, and the host port is calculated by the offset formula."""

    name = "nodeport"
    capacity = len(NODE_PORT_RANGE)
    allocates_ports = True

    def service_overrides(self, spec: dict[str, Any]) -> dict[str, Any]:
        # externalTrafficPolicy: Local - the workload only falls on w1, which hosts NodePort;
        # Changing to Cluster will cause the CP side to respond but there will be no local endpoint, and the host access will time out.
        return {
            "type": "NodePort",
            "externalTrafficPolicy": "Local",
            "nodePort": int(spec["nodePort"]),
        }

    def public_ingress_from(self, cluster_pod_cidr: str) -> list[dict[str, Any]]:
        # External clients and kubelet probes carry addresses outside the cluster and cannot be expressed using podSelector;
        # Exclude the Pod network segment to block other tenants' Namespaces.
        return [{"ipBlock": {"cidr": "0.0.0.0/0", "except": [cluster_pod_cidr]}}]

    def public_url(self, spec: dict[str, Any]) -> str | None:
        try:
            port = int(spec.get("nodePort"))
        except (TypeError, ValueError):
            return None
        if port <= 0:
            return None
        return f"{PUBLIC_URL_HOST}:{HOST_PORT_BASE + port - NODE_PORT_MIN}"


class GatewayExposure(ExposureBackend):
    """Gateway API backend: Host is the identity, and there is no upper limit on the number of sites.

    The operator builds an HTTPRoute for each public application on the shared Gateway.
    Scale-to-zero keeps the same route shape and switches backendRef from the site Service
    to the activator.
    """

    name = "gateway"
    capacity = None  # Host is derived from serviceName, not a scarce resource
    allocates_ports = False
    # The route has passed through a layer of gateway, and backendRef is changed to activator to take over the wakeup - routing form
    # It doesn't move itself. ⚠️ What is said here is "structurally allowed", which does not mean that the activator has been deployed: it is still
    # No, so sites that are really reduced to 0 when the switch is turned on are **really inaccessible** and will not wake up automatically.
    supports_scale_to_zero = True

    def service_overrides(self, spec: dict[str, Any]) -> dict[str, Any]:
        # Public sites also fall back to ClusterIP: external traffic enters through the gateway, and the site itself no longer occupies the host port.
        return {"type": "ClusterIP"}

    def host_for(self, spec: dict[str, Any]) -> str | None:
        """The site's external hostname. **Tenant summary required. **

        🔴 Using only serviceName to derive is a cross-tenant traffic leak: serviceName is only within the tenant
        The only one (namespace is the isolation boundary), merchant A’s alice and merchant B’s bob both build one
        When calling a web site, two HTTPRoutes are in different namespaces but declare the same one.
        hostname, hung on the same Gateway. Gateway API will choose one to win based on the creation time.
        So visitors from one tenant get the site content from another tenant - and both deployments display
        Success and URL are all "normal", and no error will be reported on either side.

        🔴 The summary must be spelled into the **same** DNS tag and cannot be added to another paragraph. Gateway API's
        The listener wildcard "matches only a single tag" (`*` exclusive of the first tag, canonical plain text),
        `*.<suffix>` matches `a.<suffix>` but **does not match** `a.b.<suffix>` - if split into two paragraphs
        Each route falls outside the matching range of listener, and all sites cannot be routed. Under real domain name
        Worse: `*.*.apps.example.com` This two-level wildcard is not supported by most DNS service providers.

        Length: Tag limit 63 = serviceName truncated to 46 + hyphens + 16-bit summary. Truncation does not destroy
        Injective, because the digest input carries the **complete** service name - two long names with the same first 46 digits
        You will get different summaries.
        """
        service_name = str(spec.get("serviceName") or "")
        if not service_name:
            return None
        digest = tenant_digest(
            str(spec.get("merchantID") or ""),
            str(spec.get("userID") or ""),
            service_name,
        )
        label = f"{service_name[:_HOST_NAME_BUDGET].rstrip('-')}-{digest}"
        return f"{label}.{DOMAIN_SUFFIX}"

    def route_resources(
        self, spec: dict[str, Any], namespace: str
    ) -> list[dict[str, Any]]:
        host = self.host_for(spec)
        if not host:
            return []
        service_name = str(spec["serviceName"])
        # For a site with scale-to-zero turned on, the route points to the activator instead of the site's own Service:
        # The site may have zero replicas. Directly referring to the past tense, the gateway will only get a backend without an endpoint.
        # Return to 503 - nothing will wake it up. activator Press Host to identify which site it is.
        # Expand the capacity, wait until it is ready, and then transfer the original request.
        #
        # 🔴 This is a cross-namespace reference (HTTPRoute in tenant ns, activator in control plane ns),
        # A ReferenceGrant is required on the side of the cited party. Without it, HTTPRoute will be marked as
        # ResolvedRefs=False, the site is always 503, and the configurations on both sides are correct when viewed individually.
        # ReferenceGrant is rewritten by the operator each round according to the Namespace collection of the current STZ site -
        # Its from.namespace does not support wildcards or selectors, and the tenant ns is dynamically built.
        if spec.get("scaleToZero"):
            backend_ref = {
                "name": ACTIVATOR_SERVICE,
                "namespace": ACTIVATOR_NAMESPACE,
                "port": ACTIVATOR_PORT,
            }
        else:
            backend_ref = {"name": service_name, "port": int(spec["port"])}
        return [
            {
                "apiVersion": "gateway.networking.k8s.io/v1",
                "kind": "HTTPRoute",
                "metadata": {
                    "name": service_name,
                    "namespace": namespace,
                    "labels": {
                        "app.kubernetes.io/name": service_name,
                        "app.kubernetes.io/managed-by": "sites-operator",
                    },
                },
                "spec": {
                    # Cross-namespace mounting: Gateway is in its own ns, and the site is in the tenant ns.
                    # This requires the Gateway's listener to declare allowedRoutes.namespaces
                    # from: Selector and match the tag of tenant ns - otherwise HTTPRoute will
                    # Rejected silently (notAllowedByListeners is written in status, and the site looks like
                    # Just "not accessible").
                    "parentRefs": [
                        {
                            "name": GATEWAY_NAME,
                            "namespace": GATEWAY_NAMESPACE,
                        }
                    ],
                    "hostnames": [host],
                    "rules": [
                        {
                            "matches": [{"path": {"type": "PathPrefix", "value": "/"}}],
                            "backendRefs": [backend_ref],
                            # Only STZ routes relax request timeouts: they have to accommodate a cold start,
                            # Resident routers should not learn to wait (what should be fast should fail quickly).
                            **(
                                {"timeouts": {"request": STZ_ROUTE_TIMEOUT}}
                                if spec.get("scaleToZero")
                                else {}
                            ),
                        }
                    ],
                },
            }
        ]

    def public_ingress_from(self, cluster_pod_cidr: str) -> list[dict[str, Any]]:
        """🔴 Only the gateway Pod is allowed, the NodePort except PodCIDR cannot be used.

        In NodePort mode, external traffic carries addresses outside the cluster, so that rule excludes the Pod network segment.
        Block out other tenants. After switching to L7, the traffic source becomes the gateway Pod - it is in the Pod network segment.
        Copying that rule will block the gateway altogether: all sites get 502, and the symptoms point to the gateway configuration.
        Does not point to NetworkPolicy.
        """
        return [
            {
                "namespaceSelector": {
                    "matchLabels": {
                        # Data plane ns, not ns where the Gateway object is located - see file header
                        # That note about two Namespaces.
                        "kubernetes.io/metadata.name": GATEWAY_DATA_PLANE_NS
                    }
                },
                "podSelector": {
                    "matchLabels": {GATEWAY_POD_LABEL_KEY: GATEWAY_POD_LABEL_VALUE}
                },
            }
        ]

    def public_url(self, spec: dict[str, Any]) -> str | None:
        host = self.host_for(spec)
        if not host:
            return None
        if GATEWAY_HOST_PORT in (80, 443):
            return f"{GATEWAY_SCHEME}://{host}"
        return f"{GATEWAY_SCHEME}://{host}:{GATEWAY_HOST_PORT}"


_BACKENDS: dict[str, type[ExposureBackend]] = {
    "nodeport": NodePortExposure,
    "gateway": GatewayExposure,
}


def _selected_name() -> str:
    return (getenv("SITES_EXPOSURE_BACKEND", "nodeport") or "nodeport").strip().lower()


def backend() -> ExposureBackend:
    """The exposed backend of the current process.

    Re-read env every time it is called instead of module-level file: The test must be able to switch backends in the same process,
    The fixed file will make "the first import test determine which backend all subsequent tests run on" ——
    That kind of coupling doesn't point to env at all when something goes wrong.
    """
    name = _selected_name()
    try:
        return _BACKENDS[name]()
    except KeyError:
        raise ValueError(
            f"unknown SITES_EXPOSURE_BACKEND {name!r}; "
            f"expected one of {sorted(_BACKENDS)}"
        ) from None
