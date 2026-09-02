"""External response projections for CRs, database rows, and merchant input.

This pure-function layer shapes API responses and performs only projection-level checks.
It does not perform HTTP or cluster I/O. Contract reasons for coupled fields are documented
by individual serializers.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from sites.builds import (
    SOURCE_BACKEND,
    SOURCE_MAX_FILES,
    SOURCE_MAX_TOTAL_BYTES,
    SOURCE_REQUEST_MAX_BYTES,
)
from sites.k8s_resources import MAX_BUNDLE_COMPONENTS
from sites.migrations import MAX_MIGRATION_BYTES, MAX_MIGRATION_STATEMENTS
from sites.validation import (
    SITES_REQUEST_MAX_BYTES,
    DEFAULT_MEMORY_LIMIT,
    INLINE_ARTIFACT_MAX_FILES,
    INLINE_ARTIFACT_MAX_TOTAL_BYTES,
    ValidationError,
    dns_label,
)
from sites import exposure


# The merchant name displayed on the management console is entirely entered by the administrator. Length capped and control characters blocked: it will be
# When written into the log, it will also be rendered by the frontend. The frontend renders it as plain text (Contract §6). Here it is only guaranteed to be one line.
# Bounded text.
_DISPLAY_NAME_MAX = 64


def display_name(value: Any) -> str:
    name = str(value).strip()
    if not 1 <= len(name) <= _DISPLAY_NAME_MAX:
        raise ValidationError(
            f"displayName must be 1-{_DISPLAY_NAME_MAX} characters"
        )
    if any(character < " " or character == "\x7f" for character in name):
        raise ValidationError("displayName must not contain control characters")
    return name


def parse_iso(value: Any) -> dt.datetime | None:
    """Parse an operator-written timestamp; None when absent or unparseable.

    The input comes from objects in the cluster and is not written by this process - if it cannot be parsed, just assume that there is no such signal and continue.
    Throwing it out will cause a broken status to drag the entire health page to 500.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.timezone.utc)


def capabilities_response(
    merchant_id: str, user_id: str, *, may_act_as_subjects: bool = False
) -> dict[str, Any]:
    """Return the bounded deployment contract consumed by any client.

    Both segments of the identity are returned to the caller: the same user_id is two
    tenants under two merchants, and a caller does not always know which merchant the
    credential it was handed belongs to.

    ``mayActAsSubjects`` is the caller's own grant, reported back to it. The control plane
    knows what it issued; it cannot know whether the holder has the salt to make use of it,
    so this is the one half of that question it can answer honestly. Without it a client
    that believes it may act for its users only finds out from a 403 on the first real
    call - after it has been deployed, by which time the operator who could grant it has
    moved on.
    """
    return {
        "apiVersion": "sites.local/v1alpha1",
        "merchantId": merchant_id,
        "userId": user_id,
        "mayActAsSubjects": may_act_as_subjects,
        "runtime": "kubernetes",
        "metadataDatabase": "postgresql",
        "artifactStorage": f"inline-configmap-and-source-{SOURCE_BACKEND}",
        "deploymentModes": {
            "staticInline": {
                "enabled": True,
                "flatTextFilesOnly": True,
                "requiresIndexHtml": True,
            },
            "staticVersioned": {
                "enabled": True,
                "privateObjectStorage": True,
                "immutableVersions": True,
                "verifiedPromotion": True,
            },
            "existingImage": {"enabled": True, "buildsSource": False},
            "dockerfileSource": {
                "enabled": True,
                "textFilesOnly": True,
                "rootDockerfile": True,
                "builder": "buildkit-rootless",
                "isolation": "rootless-no-process-sandbox",
            },
            "bundle": {
                "enabled": True,
                "maxComponents": MAX_BUNDLE_COMPONENTS,
                # The components all come from requests, and the control plane does not have any built-in stack.
                "componentsFromRequest": True,
            },
        },
        "limits": {
            "maxRequestBytes": SITES_REQUEST_MAX_BYTES,
            "maxInlineArtifactBytes": INLINE_ARTIFACT_MAX_TOTAL_BYTES,
            "maxInlineArtifactFiles": INLINE_ARTIFACT_MAX_FILES,
            "maxSourceRequestBytes": SOURCE_REQUEST_MAX_BYTES,
            "maxSourceArtifactBytes": SOURCE_MAX_TOTAL_BYTES,
            "maxSourceFiles": SOURCE_MAX_FILES,
            "maxMigrationBytes": MAX_MIGRATION_BYTES,
            "maxMigrationStatements": MAX_MIGRATION_STATEMENTS,
            # The size of the pool; see GET /v1/tenants/self for the upper limit for a single tenant.
            # There is no pool under the Gateway backend (Host is derived from serviceName), and null is returned instead.
            # A made-up number that allows the caller to distinguish between "the upper limit is N" and "there is no upper limit".
            "publicRoutes": exposure.backend().capacity,
        },
        "features": {
            # After the deployment is ready, the control plane requests a healthy path and hashes the HTTP status and response.
            # Write status.verification. The agent's self-statement is not evidence.
            "serverSideVerification": True,
            "secretRefs": True,
            "sourceBuild": True,
            "registry": True,
            "immutableSiteVersions": True,
            "readOnlySql": True,
            "managedData": True,
            "dynamicSiteDatabase": True,
            "verifiedVersionPromotion": True,
            "automaticVersionRollback": True,
            "scaffoldCatalog": True,
            "controlledSchemaMigrations": True,
            "versionedStaticArtifacts": True,
            "requestSecrets": False,
            "customDomains": False,
            "deploymentHistory": False,
        },
    }


def deployment_response(obj: dict[str, Any]) -> dict[str, Any]:
    metadata = obj.get("metadata") or {}
    spec = obj.get("spec") or {}
    status = obj.get("status") or {}
    service_name = spec.get("serviceName", "")
    return {
        "name": metadata.get("name"),
        # The identity must be in two parts. user_id is only unique within the merchant. If only merchantId is returned to the caller
        # The primary key of this row cannot be reconstructed - the list interface has been fixed long ago (deployment_record_response),
        # The details interface has been missing, and the tool description of mcp.py is telling the model that "only one report is not enough for positioning".
        "merchantId": spec.get("merchantID"),
        "userId": spec.get("userID"),
        "serviceName": service_name,
        "phase": status.get("phase", "Pending"),
        "ready": bool(
            status.get("ready", status.get("phase") == "Running")
        ),
        "message": status.get("message", ""),
        "url": status.get("url"),
        "revision": spec.get("revision"),
        "siteVersion": spec.get("siteVersion"),
        "staticArtifact": spec.get("staticArtifact"),
        "exposure": spec.get("exposure", "public"),
        # Echo opt-in status: Without echoing, the caller has no way to confirm that this switch is in effect.
        # The consequences of this (the number of replicas is no longer maintained by the control plane) are invisible.
        "scaleToZero": bool(spec.get("scaleToZero")),
        "observedReplicas": _observed_replicas(status.get("observedReplicas")),
        "runtimeState": _runtime_state(
            bool(spec.get("scaleToZero")),
            status.get("observedReplicas"),
            phase=str(status.get("phase", "Pending")),
            ready=bool(
                status.get("ready", status.get("phase") == "Running")
            ),
        ),
        # The same type of echo: the default value is also returned, so the caller does not have to guess what it was when it was not written.
        "memoryLimit": str(spec.get("memoryLimit") or DEFAULT_MEMORY_LIMIT),
        # Evidence detected by the control plane itself. The list interface reads the database snapshot, and there is no status in it.
        # Full text, so this item only appears in the details interface - evidence collection should also be returned to the authoritative source.
        "verification": status.get("verification"),
        "artifactSha256": status.get("artifactSha256"),
        "status_url": f"/v1/deployments/{dns_label(service_name)}"
        if service_name
        else None,
    }


def positive_int(value: Any, field: str, *, minimum: int = 1) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} must be an integer") from exc
    if number < minimum:
        raise ValidationError(f"{field} must be at least {minimum}")
    return number


def iso_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    return str(isoformat()) if callable(isoformat) else str(value)


def _observed_replicas(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _runtime_state(
    scale_to_zero: bool,
    observed_replicas: Any,
    *,
    phase: str,
    ready: bool,
) -> str:
    """Classify replica presence without changing lifecycle phase semantics."""
    replicas = _observed_replicas(observed_replicas)
    if phase != "Running" or not ready:
        return "Unknown"
    if not scale_to_zero:
        return "Active"
    if replicas == 0:
        return "Dormant"
    if replicas is not None and replicas > 0:
        return "Active"
    return "Unknown"


def deployment_record_response(record: dict[str, Any]) -> dict[str, Any]:
    service_name = str(record.get("service_name", ""))
    phase = str(record.get("phase", "Pending"))
    scale_to_zero = bool(record.get("scale_to_zero", False))
    observed_replicas = _observed_replicas(record.get("observed_replicas"))
    spec = record.get("spec") or {}
    return {
        "name": record.get("cr_name"),
        "merchantId": record.get("merchant_id"),
        # userId and merchantId must appear in pairs: user_id is only unique within the merchant, single
        # merchantId cannot locate a specific row. The admin console overview is missing it, what the administrator sees is
        # A bunch of sites but I don’t know who they belong to - and the tenants with the same name under two merchants are exactly what this transformation needs to deal with
        # The scene is exactly when the distinction is most needed. There is no harm in bringing it from the tenant perspective (it is its own id).
        "userId": record.get("user_id"),
        "serviceName": service_name,
        "image": record.get("image"),
        "port": record.get("port"),
        "healthPath": record.get("health_path"),
        "revision": record.get("revision"),
        "siteVersion": spec.get("siteVersion"),
        "staticArtifact": spec.get("staticArtifact"),
        "exposure": record.get("exposure", "public"),
        "scaleToZero": scale_to_zero,
        "observedReplicas": observed_replicas,
        "runtimeState": _runtime_state(
            scale_to_zero,
            observed_replicas,
            phase=phase,
            ready=phase == "Running",
        ),
        "phase": phase,
        "ready": phase == "Running",
        "message": record.get("message", ""),
        "url": record.get("url"),
        "createdAt": iso_timestamp(record.get("created_at")),
        "updatedAt": iso_timestamp(record.get("updated_at")),
        "deletionRequestedAt": iso_timestamp(
            record.get("deletion_requested_at")
        ),
        "status_url": f"/v1/deployments/{dns_label(service_name)}"
        if service_name
        else None,
    }


def bundle_response(
    bundle_name: str,
    objects: list[dict[str, Any]],
    *,
    force_pending: bool = False,
) -> dict[str, Any]:
    components = []
    for obj in sorted(
        objects,
        key=lambda item: str((item.get("spec") or {}).get("serviceName", "")),
    ):
        metadata = obj.get("metadata") or {}
        spec = obj.get("spec") or {}
        status = obj.get("status") or {}
        generation = int(metadata.get("generation", 0))
        observed = int(status.get("observedGeneration", 0))
        phase = "Pending" if force_pending else status.get("phase", "Pending")
        components.append(
            {
                "name": spec.get("serviceName"),
                "role": spec.get("componentRole"),
                "exposure": spec.get("exposure"),
                "phase": phase,
                "ready": (
                    not force_pending
                    and phase == "Running"
                    and generation > 0
                    and observed == generation
                ),
                "url": status.get("url"),
            }
        )
    # The expected number of components is the currently found group: the control plane does not preset any bundle composition.
    # POST creates all components atomically, so what is found is all; a few are deleted in the middle.
    # The Deleting branch below will be hit first.
    if any(item["phase"] == "Failed" for item in components):
        phase = "Failed"
    elif components and all(item["ready"] for item in components):
        phase = "Running"
    elif any(item["phase"] == "Deleting" for item in components):
        phase = "Deleting"
    else:
        phase = "Pending" if force_pending else "Deploying"
    public = next(
        (item for item in components if item["exposure"] == "public"), None
    )
    return {
        "name": bundle_name,
        # The components of the bundle belong to the same (merchant, user), just pick the first one; the empty list is upstream
        # Already blocked by 404.
        "merchantId": next(
            (
                (obj.get("spec") or {}).get("merchantID")
                for obj in objects
            ),
            None,
        ),
        "userId": next(
            ((obj.get("spec") or {}).get("userID") for obj in objects),
            None,
        ),
        "phase": phase,
        "url": public.get("url") if public else None,
        "status_url": f"/v1/bundles/{dns_label(bundle_name)}",
        "components": components,
    }
