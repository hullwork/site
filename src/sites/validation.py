"""Sites input-contract validation: exceptions, normalizers, and gate constants.

Owns ValidationError, normalize functions, identity validation, DNS labels, and deploy
contract constants such as DEPLOY_FIELDS and STATIC_IMAGE. Identity helpers remain here
because validation calls them directly while naming raises ValidationError; moving them to
naming would create a circular import. Naming therefore depends one-way on this module.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from sites import exposure as exposure_backend


class ValidationError(ValueError):
    """A user-provided Sites deployment is invalid."""


# The accepted user id range must equal the dns_label range. Namespace and CR
# names are derived through dns_label, which folds dots, underscores and case
# into hyphens; accepting a wider range would make that derivation lossy and
# let two distinct users share one namespace.
_USER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
# The merchant ID is narrower than the user ID: it is the first segment in the Namespace/CR name prefix, and the two
# The name is only 63 characters in total. The upper limit is guaranteed to be unique by the abstract, not by the number of characters, so here you only need to leave
# A prefix that is recognizable to the human eye.
_MERCHANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
_SITE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
# Identity of the single-merchant era and of the break-glass admin token: every record
# from before multi-merchant belongs to it. It is not a fallback for a request that failed
# to name a merchant - no request may name one.
DEFAULT_MERCHANT_ID = "local"
_IMAGE_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")
_IMAGE_DIGEST_RE = re.compile(
    r"^[A-Za-z0-9]+(?:[+._-][A-Za-z0-9]+)*:[A-Fa-f0-9]{32,}$"
)
_REGISTRY_RE = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+"
    r"|localhost"
    r"|\[[0-9A-Fa-f:.]+\])"
    r"(?::[0-9]{1,5})?$"
)
_REPOSITORY_COMPONENT_RE = re.compile(
    r"^[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*$"
)


def _valid_image_reference(value: str) -> bool:
    """Validate the Docker image grammar needed by Kubernetes admission."""
    if not 1 <= len(value) <= 255:
        return False
    before_digest, separator, digest = value.rpartition("@")
    if separator:
        if before_digest.count("@") or not _IMAGE_DIGEST_RE.fullmatch(digest):
            return False
    else:
        before_digest = value

    slash = before_digest.find("/")
    repository = before_digest
    registry = ""
    if slash > 0:
        candidate = before_digest[:slash]
        if (
            ":" in candidate
            or candidate == "localhost"
            or "." in candidate
            or candidate.startswith("[")
        ):
            registry = candidate
            repository = before_digest[slash + 1 :]
    if not repository:
        return False

    colon = repository.rfind(":")
    if colon >= 0:
        tag = repository[colon + 1 :]
        repository = repository[:colon]
        if not tag or not _IMAGE_TAG_RE.fullmatch(tag):
            return False
    if not repository:
        return False
    if registry and not _REGISTRY_RE.fullmatch(registry):
        return False
    if registry and not registry.startswith("["):
        port_text = registry.rsplit(":", 1)[-1] if ":" in registry else ""
        if port_text and not 1 <= int(port_text) <= 65535:
            return False
    return all(
        _REPOSITORY_COMPONENT_RE.fullmatch(part)
        for part in repository.split("/")
    )


def valid_image_reference(value: str) -> bool:
    """Return whether *value* is an accepted Kubernetes image reference."""
    return _valid_image_reference(value)


# Volume gate for direct source code injection. Content goes into HTTP JSON, PostgreSQL spec, SiteDeployment CR and
# ConfigMap;must be kept small until the object storage artifact comes online. HTTP body fixed 64KiB, text content
# Tighten again to 60KiB, leaving room for field names, file names, and JSON escaping. The caller must still serialize according to the final
# Bytes do a check because a lot of quotes/backslashes may exceed this 4KiB margin.
SITES_REQUEST_MAX_BYTES = 64 * 1024
INLINE_ARTIFACT_MAX_TOTAL_BYTES = 60 * 1024
INLINE_ARTIFACT_MAX_FILES = 64
# Relative paths, directory separation and .. are not accepted - the ConfigMap key itself does not allow "/".
_ARTIFACT_PATH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")


def normalize_artifact(payload: Any) -> dict[str, Any] | None:
    """Verify the source code direct input content and return the artifact spec with content addressing sha256.

    Constraints: Only tiled text files are accepted. The key of ConfigMap cannot contain "/", and subdirectories require additional
    Drop arrangement is not currently supported - rather than quietly discarding the subdirectory, it is better to reject it directly and make it clear.
    """
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise ValidationError("artifact must be a JSON object")
    files = payload.get("files")
    if not isinstance(files, dict) or not files:
        raise ValidationError("artifact.files must be a non-empty object")
    if len(files) > INLINE_ARTIFACT_MAX_FILES:
        raise ValidationError(
            "artifact.files must contain at most "
            f"{INLINE_ARTIFACT_MAX_FILES} files"
        )

    total = 0
    normalized: dict[str, str] = {}
    for name, content in sorted(files.items()):
        if not isinstance(name, str) or not _ARTIFACT_PATH_RE.fullmatch(name):
            raise ValidationError(
                f"artifact file name is not a flat relative path: {name!r}"
            )
        if not isinstance(content, str):
            raise ValidationError(f"artifact file content must be text: {name!r}")
        total += len(content.encode("utf-8"))
        normalized[name] = content
    if total > INLINE_ARTIFACT_MAX_TOTAL_BYTES:
        raise ValidationError(
            f"artifact exceeds {INLINE_ARTIFACT_MAX_TOTAL_BYTES} bytes "
            f"(got {total}); build an image instead of inlining sources"
        )

    digest = hashlib.sha256()
    for name in sorted(normalized):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(normalized[name].encode("utf-8"))
        digest.update(b"\0")
    return {"files": normalized, "sha256": digest.hexdigest()}


# Single source of truth for deploy input parameters: normalize_deploy_payload accepts which keys and MCP
# inputSchema and forwarding whitelist, CLI deploy flags, are all derived from this list
# (test_interface.DeployContractTests pins the three sides together). The previous three places were copied by hand,
# The forms that drift out are "the schema is not declared, but the parameters are forwarded" and "a switch with the entrance
# Another entrance can never be given". The artifact is deliberately not in the list: it is a static direct cast mode selector
# (deploy_static only, also overrides componentRole/port/runAsUser), never
# Forwarded as a normal argument to deploy.
DEPLOY_FIELDS = (
    "name",
    "image",
    "port",
    "healthPath",
    "livenessPath",
    "exposure",
    "env",
    "secretMounts",
    "runAsUser",
    "scaleToZero",
    "memoryLimit",
    "siteVersion",
)


# The application's own configuration is submitted by the caller, and the control plane does not know any specific application. These are from that contract
# gate.
#
# env has two forms, the difference is the safety boundary rather than the writing preference:
#   {"name": ..., "value": ...} plain text, will enter CR, database and GET responses,
#                                     Only non-sensitive configurations can be placed
#   {"name": ..., "secretKeyRef": {"name": ..., "key": ...}}
#                                     Reference an existing Secret in the caller's Namespace
# The control plane therefore never receives the key contents (capabilities.features.requestSecrets remains
# false), but allows reference to operation and maintenance preset Secrets (secretRefs is true).
MAX_ENV_VARS = 32
MAX_ENV_VALUE_BYTES = 4096
MAX_SECRET_MOUNTS = 4
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_SECRET_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$")
# The mount point in the container is already occupied. Allowing the caller to hang into these locations will only overwrite the writable HOME,
# /tmp or direct site root, the deployment is successful but the application behaves inexplicably.
_RESERVED_MOUNT_PATHS = frozenset({"/data", "/tmp"})
# Secrets the control plane writes into the tenant's own Namespace. Their names are
# pure functions of merchant/user/site, so a tenant can compute them; without this
# denylist a tenant could mount `site-oss-<digest>` - the platform-wide object store
# credentials, one bucket and one key pair for every tenant - and read or overwrite
# every other tenant's published artifact and build context.
# AI-LOCK: adding a `site-<something>-` naming scheme for a platform-managed Secret
# means adding its prefix here in the same change.
_PLATFORM_SECRET_PREFIXES = ("site-oss-", "site-db-")


def _reject_platform_secret(secret_name: str, field: str) -> None:
    for prefix in _PLATFORM_SECRET_PREFIXES:
        if secret_name.startswith(prefix):
            raise ValidationError(
                f"{field} may not reference the control-plane managed Secret "
                f"{secret_name!r}; names starting with {prefix!r} belong to the "
                "platform"
            )
# Root cannot run: Namespace is restricted PodSecurity, runAsNonRoot is also hardcoded.
_MIN_RUN_AS_USER = 1
_MAX_RUN_AS_USER = 65535
DEFAULT_RUN_AS_USER = 10001


# The static runtime of direct investment is nginx-unprivileged, and its uid is fixed and not selected by the caller.
STATIC_RUN_AS_USER = 101
# Directly deploy the fixed runtime image of the static site, canonical only has this one: MCP's deploy_static
# It is used by default with CLI's deploy-static, and only this part is changed during the upgrade (CLI's --image can still be overridden).
# I once wrote one version each for MCP and CLI, but the static site deployed by CLI was one minor older than MCP.
STATIC_IMAGE = (
    "nginxinc/nginx-unprivileged:1.29-alpine@sha256:"
    "0c79d56aee561a1d81c63f00eee5fb5fe29279560cdc55e91425133104c7fbe6"
)
# The memory limit for the site container. Default 512Mi, overridden via memoryLimit (see site_deployment_resource).
# Only open the memory but not the CPU: If the memory is not enough, "such applications cannot be deployed" (n8n exceeds 512Mi after initialization),
# When the CPU upper limit is relaxed, only one site will occupy the full abuse surface of the node, and there is no corresponding benefit.
DEFAULT_MEMORY_LIMIT = "512Mi"
MIN_MEMORY_LIMIT_BYTES = 128 * 1024**2
MAX_MEMORY_LIMIT_BYTES = 2 * 1024**3
_MEMORY_QUANTITY_RE = re.compile(r"^(\d+(?:\.\d+)?)(Ki|Mi|Gi)$")
_MEMORY_UNIT_BYTES = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3}


def _memory_limit_bytes(value: str) -> int:
    """Convert quantities such as 512Mi/1Gi into bytes; if illegal, a ValidationError will occur."""
    match = _MEMORY_QUANTITY_RE.fullmatch(value.strip())
    if match is None:
        raise ValidationError(
            "memoryLimit must be in the form of 512Mi or 1Gi (only integer multiples of Ki/Mi/Gi are accepted)"
        )
    return int(float(match.group(1)) * _MEMORY_UNIT_BYTES[match.group(2)])


# The tenant's default quota. These two items are required under multi-tenancy: without them, one tenant can fill up the cluster.
DEFAULT_MAX_DEPLOYMENTS = 10
# 2026-08-20 1→10: Aligned with maxDeployments. The original value 1 is the configuration root cause of mutual deletion accident——
# Deploying one site in two concurrent sessions will hit the quota. If an error is reported, the model will be directed to "delete one" and then trigger the attribution.
# Misjudgment. In gateway exposure mode, public routes are distributed by Host and there are no physical bottlenecks such as port pools.
# (see comments in charts/site/templates/08-gateway.yaml), 1 has no technical basis; NodePort mode
# There is still a pool gate (reject_over_capacity that blocks the pool according to the size of the pool).
DEFAULT_MAX_PUBLIC_ROUTES = 10
# Default quota for merchant level. The merchant-level max_deployments is the sum of all tenants under its name, so
# It must be greater than the DEFAULT_MAX_DEPLOYMENTS of a single tenant, otherwise one tenant will cover the entire merchant.
DEFAULT_MAX_TENANTS = 100
DEFAULT_MERCHANT_MAX_DEPLOYMENTS = 100
# Lifetime of a merchant API key. A credential that never expires is one that survives every
# laptop it was ever pasted on; rotation is the only thing that ends it, and nothing forces
# rotation. 90 days is long enough not to be a daily chore and short enough that a forgotten
# key stops working.
DEFAULT_MERCHANT_KEY_TTL_SECONDS = 90 * 24 * 60 * 60


def dns_label(value: str, *, max_length: int = 63) -> str:
    """Convert a value to a stable DNS label, adding a hash when truncated."""
    normalized = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    normalized = re.sub(r"-+", "-", normalized)
    if not normalized:
        raise ValidationError("name must contain at least one letter or number")
    if len(normalized) <= max_length:
        return normalized
    # 64 bits of digest: a shorter suffix lets someone pick a long name whose
    # truncated prefix and digest both match another name's.
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    prefix = normalized[: max_length - len(digest) - 1].rstrip("-")
    return f"{prefix}-{digest}"


def normalize_user_id(value: str) -> str:
    value = value.strip()
    if not _USER_ID_RE.fullmatch(value):
        raise ValidationError(
            "userId must be 1-63 lowercase letters, numbers or hyphens "
            "and start with a letter or number"
        )
    return value


def normalize_merchant_id(value: str) -> str:
    value = value.strip()
    if not _MERCHANT_ID_RE.fullmatch(value):
        raise ValidationError(
            "merchantId must be 1-31 lowercase letters, numbers or hyphens "
            "and start with a letter or number"
        )
    return value


def normalize_site_name(value: str) -> str:
    value = value.strip()
    if not _SITE_NAME_RE.fullmatch(value):
        raise ValidationError(
            "site name must be 1-63 lowercase letters, numbers or hyphens "
            "and start with a letter or number"
        )
    return value


def _probe_path(value: Any, field: str) -> str:
    path = str(value).strip()
    if (
        not path.startswith("/")
        or len(path) > 128
        or any(ch.isspace() for ch in path)
    ):
        raise ValidationError(
            f"{field} must be a whitespace-free path starting with /"
        )
    return path


def normalize_env(payload: Any) -> list[dict[str, Any]]:
    """Verify the component environment variables and return a list that can be put directly into container.env.

    Plain text value is only used for non-sensitive configuration; the key must be referenced in the namespace by secretKeyRef
    Existing Secret - The control plane does not receive the secret content, see the contract description at the top of the file.
    """
    if payload is None:
        return []
    if not isinstance(payload, list):
        raise ValidationError("env must be a list")
    if len(payload) > MAX_ENV_VARS:
        raise ValidationError(f"env must contain at most {MAX_ENV_VARS} entries")

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in payload:
        if not isinstance(entry, dict):
            raise ValidationError("env entry must be a JSON object")
        name = str(entry.get("name", ""))
        if not _ENV_NAME_RE.fullmatch(name):
            raise ValidationError(f"env name is not a valid identifier: {name!r}")
        if name in seen:
            raise ValidationError(f"env name is declared twice: {name!r}")
        seen.add(name)

        has_value = "value" in entry
        secret_ref = entry.get("secretKeyRef")
        if has_value == (secret_ref is not None):
            raise ValidationError(
                f"env {name!r} must set exactly one of value or secretKeyRef"
            )
        if has_value:
            value = entry["value"]
            if not isinstance(value, str):
                raise ValidationError(f"env {name!r} value must be a string")
            if len(value.encode("utf-8")) > MAX_ENV_VALUE_BYTES:
                raise ValidationError(
                    f"env {name!r} value exceeds {MAX_ENV_VALUE_BYTES} bytes"
                )
            normalized.append({"name": name, "value": value})
            continue

        if not isinstance(secret_ref, dict):
            raise ValidationError(f"env {name!r} secretKeyRef must be an object")
        secret_name = dns_label(str(secret_ref.get("name", "")))
        _reject_platform_secret(secret_name, f"env {name!r} secretKeyRef.name")
        secret_key = str(secret_ref.get("key", ""))
        if not _SECRET_KEY_RE.fullmatch(secret_key):
            raise ValidationError(
                f"env {name!r} secretKeyRef.key is not a valid Secret key"
            )
        normalized.append(
            {
                "name": name,
                "valueFrom": {
                    "secretKeyRef": {"name": secret_name, "key": secret_key}
                },
            }
        )
    return normalized


def normalize_secret_mounts(payload: Any) -> list[dict[str, Any]]:
    """Verify read-only Secret mounts. What is mounted is a reference, and the content still does not go through the control plane."""
    if payload is None:
        return []
    if not isinstance(payload, list):
        raise ValidationError("secretMounts must be a list")
    if len(payload) > MAX_SECRET_MOUNTS:
        raise ValidationError(
            f"secretMounts must contain at most {MAX_SECRET_MOUNTS} entries"
        )

    normalized: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for entry in payload:
        if not isinstance(entry, dict):
            raise ValidationError("secretMounts entry must be a JSON object")
        secret_name = dns_label(str(entry.get("secretName", "")))
        _reject_platform_secret(secret_name, "secretMounts secretName")
        mount_path = str(entry.get("mountPath", "")).strip()
        if (
            not mount_path.startswith("/")
            or len(mount_path) > 256
            or any(ch.isspace() for ch in mount_path)
            or ".." in mount_path
        ):
            raise ValidationError(
                "secretMounts mountPath must be an absolute path without .."
            )
        normalized_path = mount_path.rstrip("/") or "/"
        if normalized_path in _RESERVED_MOUNT_PATHS or normalized_path == "/":
            raise ValidationError(
                f"secretMounts mountPath is reserved: {mount_path}"
            )
        if normalized_path == STATIC_SITE_ROOT:
            raise ValidationError(
                "secretMounts mountPath collides with the static site root"
            )
        if normalized_path in seen_paths:
            raise ValidationError(
                f"secretMounts mountPath is declared twice: {mount_path}"
            )
        seen_paths.add(normalized_path)

        items = entry.get("items")
        normalized_items: list[dict[str, str]] = []
        if items is not None:
            if not isinstance(items, list) or not items:
                raise ValidationError("secretMounts items must be a non-empty list")
            for item in items:
                if not isinstance(item, dict):
                    raise ValidationError("secretMounts item must be an object")
                key = str(item.get("key", ""))
                path = str(item.get("path", key))
                if not _SECRET_KEY_RE.fullmatch(key):
                    raise ValidationError(
                        f"secretMounts item key is invalid: {key!r}"
                    )
                if not _ARTIFACT_PATH_RE.fullmatch(path):
                    raise ValidationError(
                        f"secretMounts item path is not a flat name: {path!r}"
                    )
                normalized_items.append({"key": key, "path": path})

        mount: dict[str, Any] = {
            "secretName": secret_name,
            "mountPath": normalized_path,
        }
        if normalized_items:
            mount["items"] = normalized_items
        normalized.append(mount)
    return normalized


def normalize_deploy_payload(
    payload: dict[str, Any], merchant_id: str, user_id: str
) -> dict[str, Any]:
    """Validate the intentionally small SiteDeployment input contract.

    The identity is two parts (merchant, user), both parts go into spec: operator only gets CR, derived
    Namespace There is no other place to ask who the merchant is.
    """
    if not isinstance(payload, dict):
        raise ValidationError("request body must be a JSON object")

    # Which keys are accepted is determined by the DEPLOY_FIELDS single source: unregistered keys are discarded here, field by field below
    # The verification reads the filtered dict - if the new parameter is forgotten to be registered, it will not be read at the source.
    # There will be no half-parameters like "the control plane is known but a certain entrance is invisible". artifact is the only
    # Exception (direct cast mode selector), taken before filtering.
    artifact_payload = payload.get("artifact")
    # Refuse what is not registered rather than dropping it. The filter below
    # keeps its own job (nothing unregistered can be read further down), but on
    # its own it also swallowed the caller's mistake: `scaleToZeroo: true`
    # deployed a site with scale-to-zero off and answered 200, and the response
    # says nothing about a key it never looked at. Every sibling normalizer in
    # this API -- bundles, merchants, tenants, quotas, source builds -- already
    # refuses unknown keys, and README/docs/AUTH.md state the rule as "refused,
    # not ignored". `artifact` is the mode selector, taken above, so it is
    # accepted here too.
    unexpected = sorted(set(payload) - set(DEPLOY_FIELDS) - {"artifact"})
    if unexpected:
        raise ValidationError(
            "unknown deployment field(s): " + ", ".join(unexpected)
        )
    payload = {k: v for k, v in payload.items() if k in DEPLOY_FIELDS}

    # The name submitted by the user must be the deployment name: silent transliteration (uppercase→lowercase/underscore→hyphen) will make
    # MY_CARD and my-card are folded into the same deployment (the latter overrides the former), and the conversion is not stated in the response
    # Occurred - user could not find their service by submission name. Normalization (dns_label) is left only to the control plane
    # Internally derived (cr_name / namespace etc.), do not fall on user input.
    raw_name = str(payload.get("name", ""))
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", raw_name):
        raise ValidationError(
            "service name must be 1-63 lowercase letters, digits or hyphens "
            "and start with a letter or digit"
        )
    service_name = raw_name
    image = str(payload.get("image", "")).strip()
    if not _valid_image_reference(image):
        raise ValidationError("image must be a valid non-empty container image reference")

    try:
        port = int(payload.get("port", 8086))
    except (TypeError, ValueError) as exc:
        raise ValidationError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValidationError("port must be between 1 and 65535")

    health_path = _probe_path(payload.get("healthPath", "/"), "healthPath")
    # Liveness and readiness must be separated: the readiness path usually checks downstream dependencies, using it as a liveness probe will make
    # Rely on kubelet to repeatedly restart the Pod as soon as it is shaken, instead of just removing it from the Service. Default
    # The same, the components that need to be distinguished are given by yourself.
    liveness_path = _probe_path(
        payload.get("livenessPath", health_path), "livenessPath"
    )

    exposure = str(payload.get("exposure", "public")).strip()
    if exposure not in {"public", "internal"}:
        raise ValidationError("exposure must be either public or internal")

    try:
        run_as_user = int(payload.get("runAsUser", DEFAULT_RUN_AS_USER))
    except (TypeError, ValueError) as exc:
        raise ValidationError("runAsUser must be an integer") from exc
    if not _MIN_RUN_AS_USER <= run_as_user <= _MAX_RUN_AS_USER:
        raise ValidationError(
            f"runAsUser must be between {_MIN_RUN_AS_USER} and "
            f"{_MAX_RUN_AS_USER}; the workload Namespace enforces runAsNonRoot"
        )

    # Opt-in only. Deriving the default from the backend meant a caller that
    # said nothing got scale-to-zero whenever it happened to be public on the
    # L7 backend, and every one of those requests then travels through the
    # single-replica activator: one more hop that can be down, plus cold-start
    # latency on the first request after idle. That is not a default to hand
    # someone who never asked for it.
    #
    # The derivation existed so that silent callers on a backend that cannot do
    # scale-to-zero would not be pushed into the reject path below. False is
    # accepted by every backend, so that concern is met by the constant.
    scale_to_zero = payload.get("scaleToZero", False)
    if not isinstance(scale_to_zero, bool):
        raise ValidationError("scaleToZero must be a boolean")

    # The upper limit of memory must be adjustable: Node applications such as n8n require a heap exceeding 512Mi for initialization, which is hard-coded
    # Equivalent to "This type of application is prohibited from deployment." The scope is fixed at the top of the common module and is not configured - it is
    # The boundaries of multi-tenant fairness, not deployment preferences.
    memory_limit = str(payload.get("memoryLimit", DEFAULT_MEMORY_LIMIT)).strip()
    memory_limit_bytes = _memory_limit_bytes(memory_limit)
    if not MIN_MEMORY_LIMIT_BYTES <= memory_limit_bytes <= MAX_MEMORY_LIMIT_BYTES:
        raise ValidationError(
            f"memoryLimit must be between 128Mi and 2Gi, received {memory_limit}"
        )

    normalized_merchant = normalize_merchant_id(merchant_id)
    normalized_user = normalize_user_id(user_id)
    spec: dict[str, Any] = {
        "merchantID": normalized_merchant,
        "userID": normalized_user,
        "serviceName": service_name,
        "image": image,
        "port": port,
        "healthPath": health_path,
        "livenessPath": liveness_path,
        "replicas": 1,
        "componentRole": "app",
        "exposure": exposure,
        "runAsUser": run_as_user,
        "env": normalize_env(payload.get("env")),
        "secretMounts": normalize_secret_mounts(payload.get("secretMounts")),
        "bundleName": "",
    }
    if "siteVersion" in payload:
        try:
            site_version = int(payload["siteVersion"])
        except (TypeError, ValueError) as exc:
            raise ValidationError("siteVersion must be a positive integer") from exc
        if site_version < 1:
            raise ValidationError("siteVersion must be a positive integer")
        spec["siteVersion"] = site_version
    # nodePort is part of this spec only under the NodePort backend. The placeholder value is determined by the admission stage
    # Allocation logic coverage (sites/api.py), placeholders are given here to make the spec shape complete before allocation.
    #
    # 🔴 The Gateway backend must **not have this field**, instead of having an ignored default value:
    # Each CR stores the same 30080. Once someone switches the backend back to nodeport, all sites will be
    # claims 30080 occupied - and _active_public_ports will count them all as real occupied, so
    # The port pool was "exhausted" instantly, but the error reported pointed to the quota rather than this switch.
    if exposure_backend.backend().allocates_ports:
        spec["nodePort"] = 30080

    # scale-to-zero is opt-in, and is only accepted in exposed backends where it is structurally feasible.
    #
    # 🔴 Must be rejected when the backend does not support it, and cannot be silently ignored: The consequence of silently ignoring is that the caller thinks that the site will
    # It shrinks when idle and is actually running at full capacity, but neither the bill nor the monitoring will say anything about it. same category
    # Lesson learned above with nodePort - having an ignored value is worse than not having it at all.
    if scale_to_zero:
        if not exposure_backend.backend().supports_scale_to_zero:
            raise ValidationError(
                "scaleToZero requires L7 gateway (SITES_EXPOSURE_BACKEND=gateway): "
                "NodePort directly DNATs external traffic to the site Pod. After shrinking to 0, there will be no"
                "The link can receive the request and trigger the wake-up"
            )
        # Only write spec when true, the same as nodePort: the shape of the existing CR is not affected,
        # On the reading side, spec.get("scaleToZero") is used to judge.
        spec["scaleToZero"] = True

    # Same as scaleToZero, "not written by default": the shape of the existing CR is not affected, and is used uniformly on the reading side.
    # spec.get("memoryLimit") or DEFAULT_MEMORY_LIMIT.
    if memory_limit != DEFAULT_MEMORY_LIMIT:
        spec["memoryLimit"] = memory_limit

    artifact = normalize_artifact(artifact_payload)
    if artifact is not None:
        spec["artifact"] = artifact
        # There is only one operating form of direct investment: read-only static site. Let the caller select role/port/uid only
        # Create a silent 404 of "the content is hung but the server reads it elsewhere".
        spec["componentRole"] = "static"
        spec["port"] = int(payload.get("port", 8080))
        spec["runAsUser"] = STATIC_RUN_AS_USER
    return spec


# When the static site is running, the site root is hung here. When changing the runtime image, it must be synchronized, otherwise the content will be hung.
# But the web server reads elsewhere, which shows that the deployment is successful but 404.
STATIC_SITE_ROOT = "/usr/share/nginx/html"
