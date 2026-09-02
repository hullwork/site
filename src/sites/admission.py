"""Admission control: quota accounting, port allocation, and refusal exceptions.

This is a stateless domain separated from ``api.py``. Inputs are Kubernetes collections
and tenant identity; outputs are counts, a port assignment, or an exception. Kubernetes
custom resources—not the database snapshot—remain the authoritative source; see each
accounting function for the reason.
"""
from __future__ import annotations

import contextlib
import threading
from typing import Any, Iterator

from sites.validation import ValidationError
from os import getenv
from sites import exposure
from sites.kube import KubeClient


# The single source of truth for values is in sites/exposure.py.
# The port pool and URL formula of the public route are uniformly defined in sites/exposure.py (the only source of truth),
# Exposure.NODE_PORT_* will be referenced here and in the test, and the second name will not be transferred.
CONTROL_NAMESPACE = exposure.CONTROL_NAMESPACE
COLLECTION_PATH = (
    f"/apis/sites.local/v1alpha1/namespaces/{CONTROL_NAMESPACE}/sitedeployments"
)
BUILD_COLLECTION_PATH = (
    f"/apis/sites.local/v1alpha1/namespaces/{CONTROL_NAMESPACE}/sitebuilds"
)
# How many SiteBuilds may be unfinished at once. Each accepted build turns into
# one BuildKit Job holding 1 CPU / 1Gi on the single local node, so without a
# gate here a loop over POST /v1/builds evicts the control plane itself. Kept in
# step with the ResourceQuota in charts/site/templates/07-build-plane.yaml.
MAX_ACTIVE_BUILDS = int(getenv("SITES_MAX_ACTIVE_BUILDS", "3") or "3")
# Phases a build can no longer consume a Job or the sources PVC in.
_FINISHED_BUILD_PHASES = {"Running", "Failed"}
# Upper bound on waiting for the single mutation lock, in seconds. Every write path
# and the snapshot sync thread share that lock, and the critical sections contain
# Kubernetes round trips and a database transaction. When one holder stalls (a
# database that accepted the connection but never answers, an apiserver that hangs)
# an unbounded acquire turns one stuck request into every write request piling up
# behind it, with no self-healing once the client has given up. 10s stays under the
# client's 15s request timeout so the caller sees a retryable 503 instead of its own
# timeout.
MUTATION_LOCK_TIMEOUT = float(getenv("SITES_MUTATION_LOCK_TIMEOUT", "10") or "10")


class PublicRouteConflict(RuntimeError):
    """Raised before mutation when no public route can be granted."""


class QuotaExceeded(RuntimeError):
    """Raised before mutation when a tenant is at its limit."""


class MerchantQuotaExceeded(QuotaExceeded):
    """Raised when the merchant-level ceiling is hit, above any tenant's own.

    The purpose of dividing it into two types is to make it clear in the rejection which level is reached: the tenant layer is reached, and the caller deletes himself.
    One deployment can continue; if the merchant level is at the top, there is no point in deleting one's own - or other tenants under this merchant's name will be vacated.
    position, or the administrator raises the quota. Both mean completely different next steps for the caller.
    """


class ServiceNameConflict(RuntimeError):
    """Raised when the other deployment API already owns this service name.

    An SiteBuild and the SiteDeployment it produces share one CR name, so the two
    endpoints can silently take over each other's object: a build would adopt an
    existing deployment on its next reconcile, and deleting the deployment would
    only make the build's reconcile loop recreate it two seconds later.
    """


class BuildNameExists(RuntimeError):
    """Raised when a live SiteBuild already occupies this service name.

    Separate from ServiceNameConflict: the next step for the caller is different (delete build and go to /v1/builds,
    Delete deployments and go to /v1/deployments). Used to reuse PublicRouteConflict just to mess with it
    409, the code received by the caller is public_route_capacity, but the error text says another
    One thing - the agent can only guess.
    """


class BuildCapacityExceeded(RuntimeError):
    """Raised when too many builds are already unfinished."""


class ControlPlaneBusy(RuntimeError):
    """Raised when the mutation lock cannot be taken within its timeout.

    Nothing about the request itself was rejected: the control plane is serializing
    writes behind a holder that has not returned. Mapped to 503 so the caller retries
    later, unlike the 4xx refusals above which retry identically.
    """


@contextlib.contextmanager
def acquire_mutation_lock(
    lock: threading.Lock, timeout: float
) -> Iterator[None]:
    """``with`` replacement for the bare mutation lock that gives up after ``timeout``.

    ``with lock:`` waits forever. This raises ControlPlaneBusy instead, so a stalled
    holder bounds the damage to the requests issued while it stalls, and the handler
    threads themselves are released for graceful shutdown to join.
    """
    if not lock.acquire(timeout=timeout):
        raise ControlPlaneBusy(
            "control plane busy, retry later"
        )
    try:
        yield
    finally:
        lock.release()


def reject_over_capacity(max_public_routes: int) -> None:
    """A tenant's public routing quota must not exceed the total capacity of the exposed backend.

    Gateway backend capacity is None (no upper limit) - no verification is performed at this time, instead of taking a certain
    Default value as upper limit: implement "no upper limit" as a large number that someone will read in the future
    The numbers give a false capacity promise.
    """
    capacity = exposure.backend().capacity
    if capacity is not None and max_public_routes > capacity:
        raise ValidationError(
            f"maxPublicRoutes cannot exceed the pool size ({capacity})"
        )


def active_build_count(items: list[dict[str, Any]]) -> int:
    """Count builds that may still own a BuildKit Job.

    Counted across all owners on purpose: the node, the sources PVC and the Job
    slots are one shared resource here, not a per-user one. Builds already
    marked for deletion still count, because the operator only removes their Job
    on its next pass — otherwise a create/delete loop could stack up Jobs two
    seconds apart while every individual request looked idle.
    """
    return sum(
        1
        for item in items
        if str((item.get("status") or {}).get("phase") or "Pending")
        not in _FINISHED_BUILD_PHASES
    )


def active_public_ports(
    items: list[dict[str, Any]],
    exclude_names: set[str],
) -> dict[int, str]:
    """Map every NodePort currently spoken for to the service holding it.

    The authoritative source is Kubernetes rather than the database snapshot: Service is the object that actually holds the NodePort,
    Allocating ports against a potentially lagging snapshot will bump up against actual occupancy on the operator side.
    """
    taken: dict[int, str] = {}
    for item in items:
        metadata = item.get("metadata") or {}
        spec = item.get("spec") or {}
        name = str(metadata.get("name", ""))
        if (
            not name
            or name in exclude_names
            or metadata.get("deletionTimestamp")
            or spec.get("exposure", "public") != "public"
        ):
            continue
        try:
            port = int(spec.get("nodePort", 0))
        except (TypeError, ValueError):
            continue
        if port:
            taken[port] = str(spec.get("serviceName") or name)
    return taken


def owned_by(spec: dict[str, Any], merchant_id: str, user_id: str) -> bool:
    """The identity is two parts (merchant, user). Just compare the userID to calculate the tenants of the two merchants with the same name.
    One - quotas crowd out each other, and in both directions."""
    return (
        spec.get("merchantID") == merchant_id
        and spec.get("userID") == user_id
    )


def _live(item: dict[str, Any], exclude_names: set[str]) -> bool:
    metadata = item.get("metadata") or {}
    return (
        str(metadata.get("name", "")) not in exclude_names
        and not metadata.get("deletionTimestamp")
    )


def tenant_public_count(
    items: list[dict[str, Any]],
    merchant_id: str,
    user_id: str,
    exclude_names: set[str],
) -> int:
    return sum(
        1
        for item in items
        if _live(item, exclude_names)
        and owned_by(item.get("spec") or {}, merchant_id, user_id)
        and (item.get("spec") or {}).get("exposure", "public") == "public"
    )


def tenant_deployment_count(
    items: list[dict[str, Any]],
    merchant_id: str,
    user_id: str,
    exclude_names: set[str],
) -> int:
    return sum(
        1
        for item in items
        if _live(item, exclude_names)
        and owned_by(item.get("spec") or {}, merchant_id, user_id)
    )


def merchant_deployment_count(
    items: list[dict[str, Any]],
    merchant_id: str,
    exclude_names: set[str],
) -> int:
    """The total number of active deployments for all tenants under the merchant's name.

    Same caliber as the tenant layer: counts Kubernetes CRs instead of database rows, and excludes deleted tombstones.
    The database snapshot will temporarily lag behind the cluster before the operator converges, and using it to determine quotas will allow over-deployment.
    """
    return sum(
        1
        for item in items
        if _live(item, exclude_names)
        and (item.get("spec") or {}).get("merchantID") == merchant_id
    )


def held_builds(
    items: list[dict[str, Any]], exclude_names: set[str]
) -> list[dict[str, Any]]:
    """Return builds that can still create or recreate a public SiteDeployment."""
    return [
        item
        for item in items
        if str((item.get("metadata") or {}).get("name", ""))
        not in exclude_names
        and not (item.get("metadata") or {}).get("deletionTimestamp")
        and str((item.get("status") or {}).get("phase") or "Pending")
        != "Failed"
    ]


def list_items(kube: KubeClient, collection_path: str) -> list[dict[str, Any]]:
    collection = kube.get(collection_path)
    items = collection.get("items") or []
    if not isinstance(items, list):
        raise RuntimeError("Kubernetes collection is invalid")
    return items


def admit_and_assign_ports(
    kube: KubeClient,
    merchant_id: str,
    user_id: str,
    desired_resources: list[dict[str, Any]],
    quota: dict[str, int],
) -> None:
    """Check this tenant's quota and hand each public component a NodePort.

    Override spec.nodePort of desired_resources in place. The port looks for the first one in order from the pool.
    Idle names that have been occupied by this submission are excluded first - redeploying the same service should not be
    The port occupied by the previous version is blocked.

    Under the Gateway backend, only the port allocation section is skipped (see the demarcation note below): Host consists of
    serviceName is derived, not a scarce resource that needs to be allocated, so there is neither a pool nor a port
    conflict. **Three-tier quotas are still all in effect** - they block "how many openings can this tenant/merchant have?"
    "Site" and "how many ports does the platform have in total" are two different things. The disappearance of the latter does not mean that the former should also disappear.
    """
    replacing = {
        str((resource.get("metadata") or {}).get("name", ""))
        for resource in desired_resources
    }
    replacing.discard("")
    items = list_items(kube, COLLECTION_PATH)
    # The unfinished build does not yet have a SiteDeployment, but it will definitely produce one, and it must be public
    # (Source code build does not have internal form). Just counting SiteDeployment will make two builds with different names
    # Through this, we have to pay the complete construction price. The second one is when building the Service, it hits the port and has been blocked.
    # occupied. The construction of Failed cannot go to that step and does not occupy space.
    builds = list_items(kube, BUILD_COLLECTION_PATH)
    held = held_builds(builds, replacing)
    deployment_names = {
        str((item.get("metadata") or {}).get("name", ""))
        for item in items
        if not (item.get("metadata") or {}).get("deletionTimestamp")
    }
    # A Running build and its SiteDeployment are one logical deployment. Builds
    # without a SiteDeployment are admitted capacity that must count now, not
    # only after the operator materializes the deployment.
    unmaterialized_builds = [
        build
        for build in held
        if str((build.get("metadata") or {}).get("name", ""))
        not in deployment_names
    ]

    # The merchant layer precedes the tenant layer: a tenant is still within its own quota, but the total number of merchants it belongs to has reached the limit.
    # Sometimes, reporting "You still have room for quota" will lead the caller to the next step that is completely invalid.
    merchant_total = merchant_deployment_count(
        items, merchant_id, replacing
    ) + sum(
        1
        for build in unmaterialized_builds
        if (build.get("spec") or {}).get("merchantID") == merchant_id
    )
    if (
        merchant_total + len(desired_resources)
        > quota["merchant_max_deployments"]
    ):
        raise MerchantQuotaExceeded(
            f"merchant '{merchant_id}' may hold at most "
            f"{quota['merchant_max_deployments']} deployments across all "
            f"its tenants; it currently has {merchant_total}"
        )

    existing_total = tenant_deployment_count(
        items, merchant_id, user_id, replacing
    ) + sum(
        1
        for build in unmaterialized_builds
        if owned_by(build.get("spec") or {}, merchant_id, user_id)
    )
    if existing_total + len(desired_resources) > quota["max_deployments"]:
        raise QuotaExceeded(
            f"tenant '{user_id}' may hold at most "
            f"{quota['max_deployments']} deployments; it currently has "
            f"{existing_total}"
        )

    public_resources = [
        resource
        for resource in desired_resources
        if (resource.get("spec") or {}).get("exposure", "public") == "public"
    ]
    if not public_resources:
        return

    existing_public = tenant_public_count(
        items, merchant_id, user_id, replacing
    ) + sum(
        1
        for build in unmaterialized_builds
        if owned_by(build.get("spec") or {}, merchant_id, user_id)
    )
    if existing_public + len(public_resources) > quota["max_public_routes"]:
        raise QuotaExceeded(
            f"tenant '{user_id}' may hold at most "
            f"{quota['max_public_routes']} public routes; it currently has "
            f"{existing_public}. Reuse an existing service name to update it "
            "in place (same name replaces, no extra route), or deploy with "
            "exposure=internal, or delete a deployment you own after "
            "confirming it is not from a concurrent session"
        )
        # 🔴 2026-08-20 Conversation accident: The old copy only suggested "Delete one" - the model was deleted accordingly
        # The concurrent session has just successfully deployed the product (it cannot identify the ownership from the list), and then exhausted its turn,
        # Net effect = lost. The deployment with the same name is the replacing path of admission (excluding new routes),
        # It is a better solution, and it must be mentioned first when reporting an error.

    # ---- Demarcation: The above is the quota (all backends must run), the following is the port allocation (NodePort only) ----
    # The Gateway backend returns here. The key is to put it after the quota rather than at the beginning of the function: Three-tier quota
    # The check (total number of merchants/total number of tenants/number of public routes) and port allocation are written in the same function and ranked
    # Previously, returning from the beginning of the function would skip them all together - that's not "removing the port cap",
    # is "remove all quotas", and the API returns 200 as usual.
    if not exposure.backend().allocates_ports:
        return

    taken = active_public_ports(items, replacing)
    invalid_reservations: list[str] = []
    port_claims: dict[int, str] = {}
    for item in items:
        metadata = item.get("metadata") or {}
        spec = item.get("spec") or {}
        name = str(metadata.get("name") or "")
        if (
            not name
            or name in replacing
            or metadata.get("deletionTimestamp")
            or spec.get("exposure", "public") != "public"
        ):
            continue
        try:
            port = int(spec.get("nodePort", 0))
        except (TypeError, ValueError):
            continue
        if port:
            port_claims[port] = name
    conflicting_reservations: list[str] = []
    for build in held:
        metadata = build.get("metadata") or {}
        spec = build.get("spec") or {}
        name = str(metadata.get("name") or "unknown")
        try:
            port = int(spec["nodePort"])
        except (KeyError, TypeError, ValueError):
            invalid_reservations.append(name)
            continue
        if not 30000 <= port <= 32767:
            invalid_reservations.append(name)
            continue
        previous = port_claims.get(port)
        if previous is not None and previous != name:
            conflicting_reservations.append(
                f"{previous} and {name} both claim {port}"
            )
            continue
        port_claims[port] = name
        taken.setdefault(port, str(spec.get("serviceName") or name))
    if invalid_reservations:
        raise PublicRouteConflict(
            "existing source builds have no valid persisted public port "
            "reservation; delete and resubmit them: "
            + ", ".join(sorted(invalid_reservations))
        )
    if conflicting_reservations:
        raise PublicRouteConflict(
            "existing public port reservations conflict: "
            + "; ".join(sorted(conflicting_reservations))
        )
    available = [
        port for port in exposure.NODE_PORT_RANGE if port not in taken
    ]
    if len(available) < len(public_resources):
        holders = sorted(set(taken.values()))
        holder = ", ".join(holders) or "another tenant"
        raise PublicRouteConflict(
            f"no free public port in {exposure.NODE_PORT_RANGE[0]}-"
            f"{exposure.NODE_PORT_RANGE[-1]}; currently held by: {holder}"
        )
    for resource, port in zip(public_resources, available, strict=False):
        resource["spec"]["nodePort"] = port
