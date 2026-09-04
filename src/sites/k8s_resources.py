"""Builders for every Kubernetes resource owned by Sites.

Also owns cluster topology constants and tenant resource-quota normalization. Builders are
pure functions: custom-resource specs go in and Kubernetes dictionaries come out, without
implicit environment or cluster reads.
"""
from __future__ import annotations

import hashlib
import ipaddress
import re
from typing import Any

from sites import exposure as exposure_backend
from os import getenv
from sites.naming import cr_name_for, namespace_for_tenant
from sites import tracing
from sites.validation import (
    DEFAULT_MEMORY_LIMIT,
    DEFAULT_RUN_AS_USER,
    STATIC_RUN_AS_USER,
    STATIC_IMAGE,
    STATIC_SITE_ROOT,
    ValidationError,
    dns_label,
    normalize_deploy_payload,
)


# 🔴 The cluster's Pod CIDR is configuration, and it has no default.
#
# It used to be the literal "10.201.0.0/16" here, restated in sites/topology.py
# and in three NetworkPolicy rules in the chart -- five copies of a guess about
# somebody else's cluster.  Every rule that uses it is written as
# {"cidr": "0.0.0.0/0", "except": [pod CIDR]}, so when the value is wrong the
# exception selects nothing and the rule degrades to "allow everyone".  Nothing
# reports that: the NetworkPolicy still applies and `kubectl get netpol` looks
# normal.  The Infra reference clusters run 10.205/10.208/10.250 and stock
# kubeadm, Calico, Flannel, GKE and EKS each use something else again, so the
# old literal was wrong nearly everywhere.
#
# A default here would preserve exactly that defect as "the behaviour when
# unconfigured", so there is none: an unset value raises, and verify_pod_network
# below refuses to start a process whose own address contradicts the declaration.
POD_CIDR_ENV = "SITES_CLUSTER_POD_CIDR"
# Injected by the chart from the downward API (status.podIP). This process's own
# address is the only evidence available in-cluster that the declaration is true.
POD_IP_ENV = "SITES_POD_IP"


class ClusterNetworkError(RuntimeError):
    """The Pod network is unconfigured, unverifiable, or contradicted by reality."""


def cluster_pod_cidr() -> str:
    """The declared Pod CIDR, normalized. Raises rather than guessing."""
    raw = (getenv(POD_CIDR_ENV) or "").strip()
    if not raw:
        raise ClusterNetworkError(
            f"{POD_CIDR_ENV} is not set. It is the Pod network of the cluster this "
            "release is installed on, and it has no default because a wrong value "
            "silently disables cross-tenant NetworkPolicy isolation. Find it with "
            "`kubectl cluster-info dump | grep -m1 cluster-cidr` and set "
            "clusterNetwork.podCIDR in the Helm values."
        )
    if "/" not in raw:
        # ipaddress accepts a bare address and silently makes it a /32, so a
        # dropped prefix would become a "Pod CIDR" containing exactly one host.
        # The startup check would catch it, but only after it had been accepted
        # as configuration, and the error would point at the wrong thing.
        raise ClusterNetworkError(
            f"{POD_CIDR_ENV}={raw!r} has no prefix length. It must be a network "
            "in CIDR notation, for example 10.244.0.0/16."
        )
    try:
        network = ipaddress.ip_network(raw, strict=True)
    except ValueError as exc:
        raise ClusterNetworkError(
            f"{POD_CIDR_ENV}={raw!r} is not a valid network in CIDR notation: {exc}"
        ) from exc
    return str(network)


def cluster_service_cidr() -> str:
    """Service network of the same cluster.

    Still a constant: unlike the Pod CIDR, every rule that names it also names
    10.0.0.0/8, which is a superset of every conventional Service range, so a
    mismatch here narrows nothing. Kept separate rather than folded in so that
    is a stated reason and not an oversight.
    """
    return "10.221.0.0/16"


# Kept under the old name so the two are still visibly one decision.
CLUSTER_SERVICE_CIDR = cluster_service_cidr()


def workload_egress_except_cidrs() -> list[str]:
    """Networks excluded from workload Internet egress.

    Besides the cluster CIDRs, these exclude RFC1918 and link-local
    infrastructure ranges so tenants cannot reach the node-local registry,
    kubelet, API server, or cloud metadata endpoints directly.
    """
    return [
        cluster_pod_cidr(),
        cluster_service_cidr(),
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
    ]


def verify_pod_network() -> str:
    """Check the declared Pod CIDR against this process's own address.

    Three outcomes, and they are deliberately three rather than two.  "The
    declaration is wrong" and "the declaration cannot be checked" are different
    facts and must not collapse into one another -- least of all into a pass.
    Every one of them refuses to start, and each says which it was:

    * the CIDR is unset or unparseable -> configuration error (from
      :func:`cluster_pod_cidr`);
    * this Pod's IP is unknown or unparseable -> *unverifiable*, which is not
      the same as verified.  The downward API entry is missing from the
      Deployment, so nothing here can tell a correct declaration from a wrong
      one, and a guard that cannot tell must not answer "fine";
    * the IP is outside the declared network -> contradicted, and both values
      go into the message because either one of them could be the wrong one.

    Returns the verified CIDR so a caller can log what it accepted.
    """
    declared = cluster_pod_cidr()
    network = ipaddress.ip_network(declared, strict=True)
    raw_ip = (getenv(POD_IP_ENV) or "").strip()
    if not raw_ip:
        raise ClusterNetworkError(
            f"cannot verify {POD_CIDR_ENV}={declared}: {POD_IP_ENV} is not set, so "
            "this process does not know its own address and cannot tell a correct "
            "declaration from a wrong one. The chart injects it from the downward "
            "API (fieldRef: status.podIP); a Deployment edited by hand needs that "
            "entry too."
        )
    try:
        address = ipaddress.ip_address(raw_ip)
    except ValueError as exc:
        raise ClusterNetworkError(
            f"cannot verify {POD_CIDR_ENV}={declared}: {POD_IP_ENV}={raw_ip!r} is "
            f"not a valid IP address ({exc}), so the check could not be performed."
        ) from exc
    if address not in network:
        raise ClusterNetworkError(
            f"this Pod's address {address} is outside {POD_CIDR_ENV}={declared}. "
            "One of the two is wrong. Every tenant-isolating NetworkPolicy is "
            "written as 'allow 0.0.0.0/0 except the Pod CIDR', so a Pod CIDR that "
            "matches no Pod excludes nothing and the rules allow everyone -- "
            "without failing, and without appearing wrong in `kubectl get netpol`. "
            "Refusing to start instead."
        )
    return declared


# The Namespace where CoreDNS is located. Release to 53 of the entire kube-system rather than to the exact
# k8s-app=kube-dns This Pod label: The label will change on different distributions. If you guess it wrong, it means application
# Even the domain name cannot be resolved, and the fake load in the single test will be all green if the package is not sent.
DNS_NAMESPACE = "kube-system"
# The Namespace where the control plane is located and the Pod label of the operator. NetworkPolicy for the workload
# These two values must be used to allow forensic detection on the control plane. The single source of truth for values ​​is in sites/exposure.py.
CONTROL_NAMESPACE = exposure_backend.CONTROL_NAMESPACE
CONTROL_PLANE_PROBE_NAME = "sites-operator"
DATABASE_ENV_KEYS = (
    "PGHOST",
    "PGPORT",
    "PGDATABASE",
    "PGUSER",
    "PGPASSWORD",
    "PGSSLMODE",
    "SITES_DATABASE_SCHEMA",
)
STATIC_ARTIFACT_AUTH_MOUNT = "/var/run/sites-oss"
STATIC_ARTIFACT_DOWNLOADER_IMAGE = getenv(
    "SITES_STATIC_ARTIFACT_DOWNLOADER_IMAGE",
    "ghcr.io/hullwork/site-control:v0.1.0",
) or "ghcr.io/hullwork/site-control:v0.1.0"
STATIC_ARTIFACT_CONTROL_SECRET = getenv(
    "SITES_STATIC_ARTIFACT_CONTROL_SECRET", "sites-oss-auth"
) or "sites-oss-auth"


def _configured_static_artifact_egress_cidrs() -> tuple[str, ...]:
    raw = getenv("SITES_STATIC_ARTIFACT_EGRESS_CIDRS", "") or ""
    values: list[str] = []
    for item in raw.split(","):
        value = item.strip()
        if not value:
            continue
        try:
            network = ipaddress.ip_network(value, strict=True)
        except ValueError as exc:
            raise RuntimeError(
                "SITES_STATIC_ARTIFACT_EGRESS_CIDRS must contain exact CIDRs"
            ) from exc
        if network.version != 4:
            raise RuntimeError(
                "SITES_STATIC_ARTIFACT_EGRESS_CIDRS currently supports IPv4 only"
            )
        values.append(str(network))
    return tuple(dict.fromkeys(values))


STATIC_ARTIFACT_EGRESS_CIDRS = _configured_static_artifact_egress_cidrs()


# KEDA's telescoping beats. The polling interval must be <= the activator’s observation window (default 60s), otherwise KEDA will
# Missing entire segments of traffic between polls.
KEDA_POLLING_SECONDS = int(getenv("SITES_KEDA_POLLING_SECONDS", "30") or "30")
KEDA_COOLDOWN_SECONDS = int(getenv("SITES_KEDA_COOLDOWN_SECONDS", "300") or "300")
# 🔴 ScaledObject that has never been active is not protected by cooldownPeriod, see autoscaling_resources.
KEDA_INITIAL_COOLDOWN_SECONDS = int(
    getenv("SITES_KEDA_INITIAL_COOLDOWN_SECONDS", "1800") or "1800"
)


def site_deployment_resource(
    payload: dict[str, Any],
    merchant_id: str,
    user_id: str,
    *,
    namespace: str = "sites-local",
) -> dict[str, Any]:
    spec = normalize_deploy_payload(payload, merchant_id, user_id)
    name = cr_name_for(spec["merchantID"], spec["userID"], spec["serviceName"])
    return {
        "apiVersion": "sites.local/v1alpha1",
        "kind": "SiteDeployment",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "annotations": ({
                "sites.local/traceparent": carrier
            } if (carrier := tracing.traceparent_for_current()) else {}),
            "labels": {
                "app.kubernetes.io/managed-by": "sites-api",
                "sites.local/service": spec["serviceName"],
            },
        },
        "spec": spec,
    }


MAX_BUNDLE_COMPONENTS = 8


def bundle_resources(
    bundle_name: str,
    components: Any,
    merchant_id: str,
    user_id: str,
    *,
    namespace: str = "sites-local",
) -> list[dict[str, Any]]:
    """Turn a set of components submitted by the caller into a SiteDeployment tagged with the same bundle.

    The control face does not have any presets about the contents of the bundle: how components discover each other (Service DNS),
    Whoever depends on whom is written by the caller in his own env. Here we are only responsible for verification, deduplication and labeling, okay
    Allow them to be queried together and recycled together.
    """
    label = dns_label(bundle_name)
    if not isinstance(components, list) or not components:
        raise ValidationError("bundle components must be a non-empty list")
    if len(components) > MAX_BUNDLE_COMPONENTS:
        raise ValidationError(
            f"a bundle may contain at most {MAX_BUNDLE_COMPONENTS} components"
        )

    resources = []
    seen: set[str] = set()
    for component in components:
        if not isinstance(component, dict):
            raise ValidationError("bundle component must be a JSON object")
        resource = site_deployment_resource(
            component, merchant_id, user_id, namespace=namespace
        )
        service_name = resource["spec"]["serviceName"]
        if service_name in seen:
            raise ValidationError(
                f"bundle declares component {service_name!r} twice"
            )
        seen.add(service_name)
        resource["metadata"]["labels"]["sites.local/bundle"] = label
        resource["spec"]["bundleName"] = label
        resources.append(resource)
    return resources


def artifact_configmap_name(service_name: str) -> str:
    return f"{service_name}-artifact"


def artifact_configmap_resource(
    spec: dict[str, Any],
    namespace: str,
) -> dict[str, Any] | None:
    """Configure spec.artifact as a ConfigMap in the workload namespace.

    The name is derived from serviceName and is managed exclusively by the operator, so it can be deleted when SiteDeployment is deleted.
    Accurate recycling, objects with the same name created by users will not be accidentally deleted.
    """
    artifact = spec.get("artifact")
    if not artifact:
        return None
    service_name = spec["serviceName"]
    labels = workload_labels(service_name)
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": artifact_configmap_name(service_name),
            "namespace": namespace,
            "labels": dict(labels, **{"sites.local/artifact": artifact["sha256"][:63]}),
        },
        "data": dict(artifact["files"]),
    }


def workload_labels(service_name: str) -> dict[str, str]:
    return {
        "app.kubernetes.io/name": service_name,
        "app.kubernetes.io/managed-by": "sites-operator",
        "sites.local/service": service_name,
    }


def static_artifact_secret_name(spec: dict[str, Any]) -> str:
    """Stable tenant-local name for the runtime's minimal OSS credential copy."""
    identity = "/".join(
        str(spec.get(key) or "")
        for key in ("merchantID", "userID", "serviceName")
    )
    return f"site-oss-{hashlib.sha256(identity.encode()).hexdigest()[:24]}"


def static_artifact_secret_resource(
    spec: dict[str, Any], namespace: str, control_secret: dict[str, Any]
) -> dict[str, Any]:
    """Copy only the two downloader credential keys into a tenant namespace."""
    source = control_secret.get("data") or {}
    if not isinstance(source, dict):
        raise ValidationError("static artifact OSS Secret is malformed")
    try:
        data = {
            "access-key-id": str(source["access-key-id"]),
            "access-key-secret": str(source["access-key-secret"]),
        }
    except KeyError as exc:
        raise ValidationError("static artifact OSS Secret is incomplete") from exc
    if any(not value for value in data.values()):
        raise ValidationError("static artifact OSS Secret is incomplete")
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": static_artifact_secret_name(spec),
            "namespace": namespace,
            "labels": {"sites.local/managed": "static-artifact-runtime"},
        },
        "type": "Opaque",
        "data": data,
    }


# The total resources of a single tenant Namespace. The quota for the number of deployments cannot stop the volume: three deployments can each be required
# One core and a half G. This layer is enforced by Kubernetes itself and is more reliable than computing on the API side - it manages
# The actually created Pod cannot be bypassed.
TENANT_CPU_LIMIT = getenv("SITES_TENANT_CPU_LIMIT", "4")
TENANT_MEMORY_LIMIT = getenv("SITES_TENANT_MEMORY_LIMIT", "4Gi")
TENANT_POD_LIMIT = getenv("SITES_TENANT_POD_LIMIT", "16")


def default_tenant_quota() -> dict[str, str]:
    """The upper limit of resources when not configured according to merchants. The three envs are deployment-level default values, not per-merchant values."""
    return {
        "cpu": str(TENANT_CPU_LIMIT),
        "memory": str(TENANT_MEMORY_LIMIT),
        "pods": str(TENANT_POD_LIMIT),
    }


def normalize_tenant_quota(value: Any) -> dict[str, str]:
    """Converge the externally provided quota specifications into a dictionary with three complete sections, and use default values to fill in the missing parts.

    The value itself is not verified here (dimensions such as `4`/`500m`/`2Gi` are left to Kubernetes for judgment):
    Only the shape is guaranteed here. The consequence of writing the wrong dimension is that ResourceQuota is rejected by apiserver. That error
    It will appear in the reconcile log instead of being eaten quietly.
    """
    quota = default_tenant_quota()
    if isinstance(value, dict):
        for key in quota:
            if key in value and str(value[key]).strip():
                quota[key] = str(value[key]).strip()
    return quota


def resource_quota_resource(spec: dict[str, Any]) -> dict[str, Any]:
    """Cap what one tenant's Namespace can consume in total.

    🔴 The quota value is taken from **CR spec**, not from the environment variables of the process. The reason is that operator is not connected
    Database (the entire file references Store), and the value classified by merchant is only known by sites-api -
    Let it bring the value when building CR, and the operator only consumes CR, so this dependency boundary does not need to be broken.

    🔴 This brings a constraint: **The tenantQuota of all CRs under the same tenant name must be consistent**.
    ResourceQuota is a namespace-level copy, and each site reconcile will be written once here;
    When the values are inconsistent, the two sites will overwrite the same object back and forth. The phenomenon is that the quota jumps between the two numbers. Change quota
    The sites-api must propagate the new value to all existing CRs for the merchant (see _propagate_* of the api).
    """
    quota = normalize_tenant_quota(spec.get("tenantQuota"))
    return {
        "apiVersion": "v1",
        "kind": "ResourceQuota",
        "metadata": {
            "name": "sites-tenant-quota",
            "namespace": namespace_for_tenant(spec["merchantID"], spec["userID"]),
            "labels": {"sites.local/managed": "true"},
        },
        "spec": {
            "hard": {
                "limits.cpu": quota["cpu"],
                "limits.memory": quota["memory"],
                "pods": quota["pods"],
            }
        },
    }


def namespace_resource(merchant_id: str, user_id: str) -> dict[str, Any]:
    name = namespace_for_tenant(merchant_id, user_id)
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": name,
            "labels": {
                "sites.local/managed": "true",
                # Mark both identity segments: The admin console filters by merchant. Namespace has only one entrance.
                # The Namespace name itself has been broken up by the abstract, and the merchant cannot be identified.
                "sites.local/merchant": dns_label(merchant_id),
                "sites.local/user": dns_label(user_id),
                "pod-security.kubernetes.io/enforce": "restricted",
                "pod-security.kubernetes.io/enforce-version": "latest",
            },
        },
    }


SCALED_OBJECT_API_VERSION = "keda.sh/v1alpha1"
SCALED_OBJECT_KIND = "ScaledObject"


def scaled_object_name(spec: dict[str, Any]) -> str:
    """Name of the ScaledObject for this site, whether or not one should exist."""
    return str(spec["serviceName"])


def scaled_object_path(spec: dict[str, Any], namespace: str) -> str:
    """API path of the ScaledObject that autoscaling_resources emits for this site.

    Unlike autoscaling_resources this does not look at scaleToZero: the operator needs
    the path to delete the object after the flag is turned off, and _cleanup needs it
    regardless of what the final spec says. Deriving it from the same name/kind/version
    as the generator keeps "what we create" and "what we delete" from drifting apart.
    """
    return (
        f"/apis/{SCALED_OBJECT_API_VERSION}/namespaces/{namespace}/"
        f"{SCALED_OBJECT_KIND.lower()}s/{scaled_object_name(spec)}"
    )


def autoscaling_resources(
    spec: dict[str, Any], namespace: str
) -> list[dict[str, Any]]:
    """Sites with scaleToZero turned on will let KEDA manage the replica count.

    🔴 Must be KEDA and cannot be HPA: HPA directly performs ScalingDisabled on the target with replicas=0,
    Even minReplicas will not make it restart - and there is no problem in the status. It has been tested by the upstream side.
    (ArgoCD shows Synced/Healthy, but the application cannot display it).

    🔴 initialCooldownPeriod must be given explicitly. KEDA's cooldownPeriod is only active if
    It will be timed after it has been turned into inactive"; a new ScaledObject that has never been active will be counted for the first time
    When polling** finds that the metric is 0, it will be reset to zero immediately - a site that has just been deployed and has not yet been visited by anyone will disappear in seconds.
    The phenomenon is "the deployment was successful but cannot be opened".

    The metric comes from the activator: it is on the data path and is the only one that knows "has anyone visited this site recently?"
    place. The query is by host rather than by service name, because the service name is only unique within the tenant.
    """
    if not spec.get("scaleToZero"):
        return []
    host = exposure_backend.backend().host_for(spec)
    if not host:
        return []
    service_name = scaled_object_name(spec)
    metrics_url = (
        f"http://{exposure_backend.ACTIVATOR_SERVICE}."
        f"{exposure_backend.ACTIVATOR_NAMESPACE}.svc:"
        f"{exposure_backend.ACTIVATOR_ADMIN_PORT}/scale-metrics?host={host}"
    )
    return [
        {
            "apiVersion": SCALED_OBJECT_API_VERSION,
            "kind": SCALED_OBJECT_KIND,
            "metadata": {
                "name": service_name,
                "namespace": namespace,
                "labels": {
                    "app.kubernetes.io/name": service_name,
                    "app.kubernetes.io/managed-by": "sites-operator",
                },
            },
            "spec": {
                "scaleTargetRef": {"name": service_name},
                "minReplicaCount": 0,
                # The single-copy assumption has not changed: this layer only does 0↔1 and does not do horizontal expansion.
                "maxReplicaCount": 1,
                "pollingInterval": KEDA_POLLING_SECONDS,
                "cooldownPeriod": KEDA_COOLDOWN_SECONDS,
                "initialCooldownPeriod": KEDA_INITIAL_COOLDOWN_SECONDS,
                "triggers": [
                    {
                        "type": "metrics-api",
                        "metadata": {
                            "url": metrics_url,
                            "valueLocation": "value",
                            "targetValue": "1",
                            # 0 before shrinking to zero: Any request in the window is considered alive.
                            "activationTargetValue": "0",
                        },
                    }
                ],
            },
        }
    ]


def deployment_resource(spec: dict[str, Any], namespace: str) -> dict[str, Any]:
    service_name = spec["serviceName"]
    port = int(spec["port"])
    static_artifact = spec.get("staticArtifact") or {}
    inline_artifact = spec.get("artifact") or {}
    if static_artifact and inline_artifact:
        raise ValidationError("artifact and staticArtifact are mutually exclusive")
    if static_artifact:
        source_path = str(static_artifact.get("sourcePath") or "")
        artifact_sha256 = str(static_artifact.get("sha256") or "")
        if not re.fullmatch(r"[a-f0-9]{64}", artifact_sha256):
            raise ValidationError("staticArtifact.sha256 must be a SHA-256 digest")
        if not source_path or source_path.rsplit("/", 1)[-1] != artifact_sha256:
            raise ValidationError(
                "staticArtifact.sourcePath must end with its SHA-256 digest"
            )
    static_content = bool(static_artifact or inline_artifact)
    image = STATIC_IMAGE if static_content else str(spec["image"])
    # The local stack image tag is fixed (such as example-app:sites-local) but the content changes with each build, so it must
    # Always is based on the registry in the cluster - IfNotPresent, the old cache of the node with the same name will win and redeploy
    # Silent failure (tested on 2026-08-19).
    #
    # But the digest reference (…@sha256:…) is excluded: it is content-addressed, and the same reference always points to
    # For the same image layer, Always cannot get anything new, it just adds a registry every time the Pod is started.
    # Round trip. The build product takes exactly this path (sites/test_builds.py asserts image with @<digest>).
    #
    # ⚠️ The rest are Always, including third-party public images provided directly by the caller (used by sites_static_deploy
    # nginxinc/nginx-unprivileged:1.29-alpine)——That kind of image will hit the upstream every time the Pod starts.
    # In registry, offline environment or Docker Hub anonymous current limit, it will become ImagePullBackOff. To cure it, you have to press
    # "Whether the image points to the registry in the cluster" is a separate matter. Then the registry configuration needs to be passed into this layer, which is another matter.
    pull_policy = "IfNotPresent" if "@sha256:" in image else "Always"

    # 🔴 **Ownership** of the replica count. After turning on scaleToZero, .spec.replicas belongs to the external scaler
    # (KEDA) management, the operator can no longer write it back every round - otherwise it will be "KEDA shrinks to 0 → next round
    # is restated into a 1 → shrink again loop, and will accelerate itself: Deployment after the number of replicas is changed back
    # If it is not ready for a short period of time, the frequency reduction rules of reconcile will be applied in each round that has not converged, and the conflict frequency will be from
    # 60 seconds becomes 2 seconds.
    #
    # The way to hand over ownership is to **not write this field**: kube.patch uses
    # merge-patch+json, omitting it means "no change", apiserver will also fill in the default value of 1 when it is first created.
    # The behavior of sites without the switch is literally unchanged.
    replicas_field: dict[str, Any] = (
        {} if spec.get("scaleToZero") else {"replicas": 1}
    )
    labels = workload_labels(service_name)
    pod_labels = dict(labels)
    if spec.get("bundleName"):
        bundle_label = dns_label(str(spec["bundleName"]))
        # Deployment metadata must also be queryable by bundle for cleaning, inspection, and capacity statistics.
        # The selector remains stable to avoid triggering immutable errors when updating existing Deployments.
        labels["sites.local/bundle"] = bundle_label
        pod_labels["sites.local/bundle"] = bundle_label
    # componentRole only distinguishes between "general workload" and "direct static site" and no longer carries any application identity:
    # What UID, what environment variables, and which Secret are required by the application all come from the spec submitted by the caller.
    # In the early version, a specific application was hard-coded according to the three roles of backend/gateway/web.
    # Component assembly, those components must be deleted first and then resubmitted according to the new contract.
    role = "static" if static_content else str(spec.get("componentRole") or "app")
    if role not in {"app", "static"}:
        raise ValidationError(
            f"unsupported component role: {role}; resubmit the component with "
            "its own env and secretMounts"
        )
    run_as_user = int(
        spec.get(
            "runAsUser",
            STATIC_RUN_AS_USER if role == "static" else DEFAULT_RUN_AS_USER,
        )
    )
    liveness_path = spec.get("livenessPath") or spec["healthPath"]
    # Only two general assumptions remain: a read-only root filesystem requires a writable HOME, and that most
    # Recognize PORT at runtime. In addition, what the application needs is left to the caller himself in spec.env.
    # When using the same name, the caller shall prevail - the control plane does not configure any specific application.
    env: list[dict[str, Any]] = [
        {"name": "HOME", "value": "/data"},
        {"name": "PORT", "value": str(port)},
    ]
    volume_mounts: list[dict[str, Any]] = [
        {"name": "home", "mountPath": "/data"},
        {"name": "tmp", "mountPath": "/tmp"},
    ]
    volumes: list[dict[str, Any]] = [
        {"name": "home", "emptyDir": {"sizeLimit": "1Gi"}},
        {"name": "tmp", "emptyDir": {"sizeLimit": "256Mi"}},
    ]
    init_containers: list[dict[str, Any]] = []

    declared_env = [dict(item) for item in (spec.get("env") or [])]
    declared_names = {str(item.get("name", "")) for item in declared_env}
    env = [item for item in env if item["name"] not in declared_names]
    env.extend(declared_env)
    database = spec.get("database") or {}
    database_secret = str(database.get("secretName") or "")
    if database_secret:
        # This label is intentionally Pod-only. The control-plane PostgreSQL
        # ingress policy uses it together with the managed-tenant Namespace
        # label, so an arbitrary workload in the same tenant cannot reach the
        # database merely by knowing its Service address.
        pod_labels["sites.local/database-access"] = "true"
        env.extend(
            {
                "name": name,
                "valueFrom": {
                    "secretKeyRef": {"name": database_secret, "key": name}
                },
            }
            for name in DATABASE_ENV_KEYS
        )

    for index, mount in enumerate(spec.get("secretMounts") or []):
        volume_name = f"secret-{index}"
        volume_mounts.append(
            {
                "name": volume_name,
                "mountPath": mount["mountPath"],
                "readOnly": True,
            }
        )
        secret_volume: dict[str, Any] = {
            "secretName": mount["secretName"],
            "defaultMode": 0o440,
        }
        if mount.get("items"):
            secret_volume["items"] = [dict(item) for item in mount["items"]]
        volumes.append({"name": volume_name, "secret": secret_volume})

    if spec.get("artifact"):
        # Direct source code delivery: The site content is hung into the site root of the runtime image as a read-only ConfigMap.
        # This is independent of the role branch above - the direct investment uses the general static runtime, and the caller
        # The assembly of components submitted by yourself has nothing to do with it.
        volume_mounts.append(
            {
                "name": "artifact",
                "mountPath": STATIC_SITE_ROOT,
                "readOnly": True,
            }
        )
        volumes.append(
            {
                "name": "artifact",
                "configMap": {
                    "name": artifact_configmap_name(spec["serviceName"]),
                    "defaultMode": 0o444,
                },
            }
        )

    if static_artifact:
        # The control image fetches and verifies the immutable object before nginx
        # starts. Credentials are scoped to this init container; nginx receives
        # only the resulting read-only files.
        volume_mounts.append(
            {
                "name": "static-artifact",
                "mountPath": STATIC_SITE_ROOT,
                "readOnly": True,
            }
        )
        volumes.extend(
            [
                {
                    "name": "static-artifact",
                    "emptyDir": {"sizeLimit": "64Mi"},
                },
                {
                    "name": "static-artifact-oss-auth",
                    "secret": {
                        "secretName": static_artifact_secret_name(spec),
                        "items": [
                            {
                                "key": "access-key-id",
                                "path": "access-key-id",
                            },
                            {
                                "key": "access-key-secret",
                                "path": "access-key-secret",
                            },
                        ],
                    },
                },
            ]
        )
        init_containers.append(
            {
                "name": "fetch-static-artifact",
                "image": STATIC_ARTIFACT_DOWNLOADER_IMAGE,
                "imagePullPolicy": "Always",
                "command": ["python3", "-m", "sites.static_artifacts"],
                "args": [
                    "--source-path",
                    str(static_artifact["sourcePath"]),
                    "--destination",
                    STATIC_SITE_ROOT,
                ],
                "env": [
                    {
                        "name": "PYTHONDONTWRITEBYTECODE",
                        "value": "1",
                    },
                    {
                        "name": "SITES_OSS_ENDPOINT",
                        "value": getenv("SITES_OSS_ENDPOINT", "") or "",
                    },
                    {
                        "name": "SITES_OSS_BUCKET",
                        "value": getenv("SITES_OSS_BUCKET", "") or "",
                    },
                    {
                        "name": "SITES_OSS_PREFIX",
                        "value": getenv("SITES_OSS_PREFIX", "") or "",
                    },
                    {
                        "name": "SITES_OSS_REGION",
                        "value": getenv("SITES_OSS_REGION", "") or "",
                    },
                    {
                        "name": "SITES_OSS_ADDRESSING_STYLE",
                        "value": getenv("SITES_OSS_ADDRESSING_STYLE", "virtual")
                        or "virtual",
                    },
                    {
                        "name": "SITES_OSS_SIGNATURE_VERSION",
                        "value": getenv("SITES_OSS_SIGNATURE_VERSION", "s3")
                        or "s3",
                    },
                    {
                        "name": "SITES_OSS_ACCESS_KEY_ID_FILE",
                        "value": f"{STATIC_ARTIFACT_AUTH_MOUNT}/access-key-id",
                    },
                    {
                        "name": "SITES_OSS_ACCESS_KEY_SECRET_FILE",
                        "value": f"{STATIC_ARTIFACT_AUTH_MOUNT}/access-key-secret",
                    },
                ],
                "securityContext": {
                    "runAsUser": 65532,
                    "runAsGroup": 65532,
                    "allowPrivilegeEscalation": False,
                    "readOnlyRootFilesystem": True,
                    "capabilities": {"drop": ["ALL"]},
                    "seccompProfile": {"type": "RuntimeDefault"},
                },
                "resources": {
                    "requests": {"cpu": "10m", "memory": "32Mi"},
                    "limits": {"cpu": "250m", "memory": "128Mi"},
                },
                "volumeMounts": [
                    {
                        "name": "static-artifact",
                        "mountPath": STATIC_SITE_ROOT,
                    },
                    {
                        "name": "static-artifact-oss-auth",
                        "mountPath": STATIC_ARTIFACT_AUTH_MOUNT,
                        "readOnly": True,
                    },
                ],
            }
        )

    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": service_name,
            "namespace": namespace,
            "labels": labels,
        },
        "spec": {
            **replicas_field,
            "selector": {"matchLabels": workload_labels(service_name)},
            "strategy": {
                "type": "RollingUpdate",
                "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1},
            },
            "template": {
                "metadata": {
                    # The bundle label is only added to the Pod and does not include selector: Deployment.
                    # The selector is immutable, and changing it will cause existing workload patches to fail.
                    # NetworkPolicy's ingress.from relies on this Pod label.
                    "labels": pod_labels,
                    "annotations": {
                        "sites.local/revision": str(spec.get("revision", "1")),
                        **(
                            {
                                "sites.local/static-artifact-sha256": str(
                                    static_artifact["sha256"]
                                )
                            }
                            if static_artifact
                            else {}
                        ),
                    },
                },
                "spec": {
                    "automountServiceAccountToken": False,
                    "enableServiceLinks": False,
                    "terminationGracePeriodSeconds": 10,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": run_as_user,
                        "runAsGroup": run_as_user,
                        "fsGroup": run_as_user,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    **(
                        {"initContainers": init_containers}
                        if init_containers
                        else {}
                    ),
                    "containers": [
                        {
                            "name": service_name,
                            "image": image,
                            "imagePullPolicy": pull_policy,
                            "ports": [{"name": "http", "containerPort": port}],
                            "env": env,
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "resources": {
                                # Actual measured tenant container RSS <30Mi, 128Mi is falsely high by 4 times;
                                # requests is the resident scheduling cost of each tenant, directly
                                # Determine how many tenants a single worker can accommodate
                                "requests": {"cpu": "100m", "memory": "64Mi"},
                                "limits": {
                                    "cpu": "1",
                                    # memoryLimit covers only the upper limit; requests
                                    # Do not follow - the schedule is based on requests, and the follow-up will
                                    # "How far you can run" becomes "how far you must occupy".
                                    "memory": str(
                                        spec.get("memoryLimit")
                                        or DEFAULT_MEMORY_LIMIT
                                    ),
                                },
                            },
                            "readinessProbe": {
                                "httpGet": {
                                    "path": spec["healthPath"],
                                    "port": "http",
                                },
                                "initialDelaySeconds": 2,
                                "periodSeconds": 2,
                                "timeoutSeconds": 2,
                                "failureThreshold": 30,
                            },
                            "livenessProbe": {
                                "httpGet": {
                                    "path": liveness_path,
                                    "port": "http",
                                },
                                "initialDelaySeconds": 10,
                                "periodSeconds": 10,
                                "timeoutSeconds": 2,
                                "failureThreshold": 3,
                            },
                            "volumeMounts": volume_mounts,
                        }
                    ],
                    "volumes": volumes,
                },
            },
        },
    }


def site_database_secret_resource(
    spec: dict[str, Any], namespace: str, control_secret: dict[str, Any]
) -> dict[str, Any]:
    """Copy only runtime connection fields into the tenant namespace."""
    database = spec.get("database") or {}
    name = str(database.get("secretName") or "")
    source = control_secret.get("data") or {}
    mapping = {
        "PGHOST": "runtime-host",
        "PGPORT": "runtime-port",
        "PGDATABASE": "runtime-database",
        "PGUSER": "runtime-user",
        "PGPASSWORD": "runtime-password",
        "PGSSLMODE": "runtime-sslmode",
        "SITES_DATABASE_SCHEMA": "runtime-schema",
    }
    if not name or not isinstance(source, dict):
        raise ValidationError("dynamic site database Secret is malformed")
    try:
        data = {target: str(source[key]) for target, key in mapping.items()}
    except KeyError as exc:
        raise ValidationError("dynamic site database Secret is incomplete") from exc
    if any(not value for value in data.values()):
        raise ValidationError("dynamic site database Secret is incomplete")
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"sites.local/managed": "site-database-runtime"},
        },
        "type": "Opaque",
        "data": data,
    }


def service_resource(spec: dict[str, Any], namespace: str) -> dict[str, Any]:
    service_name = spec["serviceName"]
    labels = workload_labels(service_name)
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": service_name,
            "namespace": namespace,
            "labels": labels,
        },
        "spec": {
            "type": "ClusterIP",
            "selector": labels,
            "ports": [
                {
                    "name": "http",
                    "port": int(spec["port"]),
                    "targetPort": "http",
                }
            ],
        },
    }
    if spec.get("exposure", "public") == "public":
        # The Service form of the public site is determined by the exposed backend (NodePort port is the identity/
        # Gateway returns ClusterIP and is distributed by gateway by Host), see sites/exposure.py.
        overrides = dict(exposure_backend.backend().service_overrides(spec))
        node_port = overrides.pop("nodePort", None)
        service["spec"].update(overrides)
        if node_port is not None:
            service["spec"]["ports"][0]["nodePort"] = int(node_port)
    return service


def route_resources(spec: dict[str, Any], namespace: str) -> list[dict[str, Any]]:
    """Exposes the routing objects required by the site in addition to the Service.

    The NodePort backend returns an empty list (the port itself is the route); the Gateway backend returns an HTTPRoute.
    Internal sites never have external routes.
    """
    if str(spec.get("exposure", "public")) != "public":
        return []
    return exposure_backend.backend().route_resources(spec, namespace)


def network_policy_resources(
    spec: dict[str, Any], namespace: str
) -> list[dict[str, Any]]:
    service_name = spec["serviceName"]
    labels = workload_labels(service_name)
    bundle_name = str(spec.get("bundleName") or "")
    ingress_rule: dict[str, Any] = {
        "ports": [{"protocol": "TCP", "port": int(spec["port"])}]
    }
    # The control plane itself must be able to hit the workload: after the deployment is completed, the operator will actually request a healthy path.
    # Write the results to the status as forensics. Without this provision, the following exclusion of Pod network segments will cause the operator to
    # Block it all together - it's also in the Pod network segment. The opening is limited to the operator Pod tag,
    # And only in this rule that has limited the target port.
    control_plane_source = {
        "namespaceSelector": {
            "matchLabels": {"kubernetes.io/metadata.name": CONTROL_NAMESPACE}
        },
        "podSelector": {
            "matchLabels": {"app.kubernetes.io/name": CONTROL_PLANE_PROBE_NAME}
        },
    }
    if str(spec.get("exposure", "public")) == "internal":
        # The origin of internal components is expressed by bundle membership, not by component name - the control plane does not know
        # Who should visit whom. Interoperability within the same bundle, but unreachable outside the bundle (including other tenants); None
        # Bundle's independent internal services are only allowed in this Namespace.
        #
        # In earlier versions, the allowed sources were hard-coded by component name, which was narrower than this: at that time, backend only accepted
        # work-ui, gateway only accepts backend and work-ui. After changing to bundle granularity, the same
        # There are no longer orientation restrictions between components in a bundle.
        ingress_rule["from"] = [
            {
                "podSelector": (
                    {"matchLabels": {"sites.local/bundle": bundle_name}}
                    if bundle_name
                    else {}
                )
            },
            control_plane_source,
        ]
    else:
        # The incoming source of the public site is determined by the exposed backend, because "where the traffic comes from" is exactly the difference between these two backends.
        # The fundamental difference: NodePort is an address outside the cluster (can only be expressed using ipBlock, excluding the Pod network segment)
        # Block other tenants); Under Gateway is the gateway Pod - it is in the Pod network segment, copy it
        # The except for NodePort will block the gateway as well. Empty podSelector retains this Namespace
        # Reachable by itself, both backends are required.
        ingress_rule["from"] = [
            *exposure_backend.backend().public_ingress_from(cluster_pod_cidr()),
            {"podSelector": {}},
            control_plane_source,
        ]
        # The public site of scale-to-zero is opened. The data path is Gateway → activator → site:
        # The route's backendRef points to the activator(exposure.route_resources), which reports to the site
        # The forwarding initiated must also pass this inbound policy. Without this item, Cilium silently loses packets, symptoms
        # Yes activator logs TimeoutError, site 502, and HTTPRoute/endpoint all
        # Normal - In the single test, both processes are on the local machine, the policy layer does not take effect at all and cannot be measured.
        # It is only allowed when STZ is open: the traffic of unopened sites does not pass through the activator, and the extra openings are just
        # Unnecessary attack surface (activator is the only control plane component that directly faces public network requests).
        if spec.get("scaleToZero"):
            ingress_rule["from"].append(
                {
                    "namespaceSelector": {
                        "matchLabels": {
                            "kubernetes.io/metadata.name": (
                                exposure_backend.ACTIVATOR_NAMESPACE
                            )
                        }
                    },
                    "podSelector": {
                        "matchLabels": {
                            "app.kubernetes.io/name": (
                                exposure_backend.ACTIVATOR_SERVICE
                            )
                        }
                    },
                }
            )
    # Outbound. These two policies previously only declared Ingress, so the outbound direction is fully enabled - tenant application (= any
    # code) can directly connect to sites-postgres:5432, other tenant Namespace Pods, kubelet and
    # apiserver can also bypass the registry's own NetworkPolicy and call <nodeIP>:5000
    # Enumerate /v2/_catalog or delete other's manifest.
    #
    # The convergence direction is "block targets within the cluster and allow access outside the cluster", not "only allow whitelist ports": Tenant application
    # Accessing the Internet is a normal requirement (calling external APIs, pulling data), and it makes no sense to block the platform of the external network; while pressing
    # Putting only 80/443 in the build interface will block non-HTTP outbound such as externally hosted databases (5432) and SMTP.
    # The performance is that the application connection times out without any error reporting from the policy layer - the fake load in the single test does not send a single packet.
    # Such defects will be completely green. The reason why the build surface can limit the port is because buildkitd only uses HTTP to pull the base image.
    #
    # Three categories are allowed and the rest are rejected:
    #   1. 53 of kube-system - without it, the application cannot even resolve the domain name.
    #   2. Pods in this Namespace are not limited to ports. Between services of the same tenant, **also includes the same
    #      The components of the bundle discover each other**: the components of a bundle all fall into the same tenant
    #      In the namespace, they rely on Service DNS to call each other. If this bundle is blocked, it will immediately break.
    #      This does not amplify permissions - the target Pod's own inbound policy still only recognizes the source of the bundle.
    #   3. Outside the cluster, regardless of port, except workload_egress_except_cidrs().
    #
    # 🔴Three remaining issues are not resolved, so don’t treat them as resolved:
    #   - <nodeIP>:5000 is blocked by except 192.168.0.0/16, provided that the node address falls within
    #     RFC1918 (the reference kubeadm nodes use a private cluster network).
    #     **If the node is a bare metal with a public IP address, this except is not true and hostPort 5000 will be reset.
    #     Reachable** - the code cannot determine the node address, so it can only be recorded here. registry has no authentication this
    #     The root cause remains unchanged.
    #   - Item 2 matches by Pod IP. Most CNIs decide where to go after DNAT and therefore use ClusterIP
    #     The Service that accesses this ns will hit it; if a CNI is determined before DNAT, this type of traffic will
    #     It will fall into Article 3 and be blocked by the except of Service CIDR. The performance is one of the bundle components.
    #     Can't connect. The sites-builder strategy for building surfaces makes the same assumption.
    #   - ipBlock is IPv4 only. IPv6 target in dual-stack cluster does not match any rule = rejected
    #     (fail closed), not released.
    egress_rules: list[dict[str, Any]] = [
        {
            "to": [
                {
                    "namespaceSelector": {
                        "matchLabels": {
                            "kubernetes.io/metadata.name": DNS_NAMESPACE
                        }
                    }
                }
            ],
            "ports": [
                {"protocol": "UDP", "port": 53},
                {"protocol": "TCP", "port": 53},
            ],
        },
        {"to": [{"podSelector": {}}]},
        {
            "to": [
                {
                    "ipBlock": {
                        "cidr": "0.0.0.0/0",
                        "except": workload_egress_except_cidrs(),
                    }
                }
            ]
        },
    ]
    if spec.get("staticArtifact") and STATIC_ARTIFACT_EGRESS_CIDRS:
        # Private object-storage endpoints are normally inside RFC1918 space and
        # are excluded by the general Internet rule above. Operators may open
        # only the exact provider CIDRs needed by the credentialed downloader.
        egress_rules.append(
            {
                "to": [
                    {"ipBlock": {"cidr": cidr}}
                    for cidr in STATIC_ARTIFACT_EGRESS_CIDRS
                ],
                "ports": [{"protocol": "TCP", "port": 443}],
            }
        )
    if (spec.get("database") or {}).get("secretName"):
        egress_rules.append(
            {
                "to": [
                    {
                        "namespaceSelector": {
                            "matchLabels": {
                                "kubernetes.io/metadata.name": CONTROL_NAMESPACE
                            }
                        },
                        "podSelector": {
                            "matchLabels": {
                                "app.kubernetes.io/name": "sites-postgres"
                            }
                        },
                    }
                ],
                "ports": [{"protocol": "TCP", "port": 5432}],
            }
        )
    return [
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {
                "name": f"{service_name}-default-deny",
                "namespace": namespace,
                "labels": labels,
            },
            "spec": {
                "podSelector": {"matchLabels": labels},
                "policyTypes": ["Ingress", "Egress"],
            },
        },
        {
            # There is only http in the name, but this one also contains egress: both names are load-bearing.
            # —— operator._cleanup deletes the NetworkPolicy according to the hard-coded name and creates an outbound order.
            # The third strategy will leave an orphan after CR is deleted, and press app.kubernetes.io/name
            # Apply it to the next service with the same name. To change the name, the two paths of operator must be changed simultaneously.
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {
                "name": f"{service_name}-allow-http",
                "namespace": namespace,
                "labels": labels,
            },
            "spec": {
                "podSelector": {"matchLabels": labels},
                "policyTypes": ["Ingress", "Egress"],
                "ingress": [
                    ingress_rule
                ],
                "egress": egress_rules,
            },
        },
    ]
