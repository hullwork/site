"""MCP tool surface for Sites, independent of how the JSON-RPC messages arrive.

:class:`Server` is the whole tool surface and knows nothing about transport. Two
transports carry it, and both go through this one class so the twelve tools cannot
become two different contracts:

* **Streamable HTTP** at ``POST /mcp`` on the control-plane API (``sites/api_mcp.py``).
  This is what an external agent host uses: it is a tenant on the network, not a process
  that has to hold a copy of this source tree.
* **stdio** via ``sites mcp`` (:func:`serve_stdio`), a local development and CLI
  convenience. It is not a second authorization path - it is an ordinary ``sites.client``
  caller presenting an ordinary credential to the same ``/v1/*`` API, so the server-side
  rules that decide tenancy are the same ones, reached the same way.

Tool definitions are generated from control-plane capabilities. Write tools enforce the
deployment-intent boundary and quota preflight, but the control plane remains the atomic
admission authority. On the stdio transport stdout is reserved for JSON-RPC; all logs go
to stderr.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from sites.client import (
    ACTING_SUBJECT_HEADER as _ACTING_SUBJECT_HEADER,
    ACTING_SUBJECT_RE as _ACTING_SUBJECT_RE,
    Client,
    SitesError,
)
from sites.validation import (
    DEPLOY_FIELDS,
    STATIC_IMAGE,
    STATIC_RUN_AS_USER,
    ValidationError,
    normalize_artifact,
)

# Echo the client's protocol version when possible. This server uses only the stable
# initialize/tools/list/tools/call methods.
FALLBACK_PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "sites", "version": "0.1.0"}

# Reserved argument names are injected by trusted runtime integration. Model-supplied
# values with these names must be stripped before the arguments reach this server.
CALLER_USER_ARGUMENT = "_agent_user_id"
DEPLOYMENT_AUTHORIZATION_ARGUMENT = "_agent_deployment_authorization"

# A tools/call message does not contain the original user prompt. Write tools therefore
# require both a server-issued authorization context and a verbatim intent excerpt
# before they can perform a side effect.
DEPLOYMENT_INTENT_ARGUMENT = "deploymentIntent"


def _caller_subject(subject_id: str) -> str:
    """The pseudonym to send as ``X-Acting-Subject`` for one caller.

    🔴 **The value arrives already derived, and is forwarded unchanged.** The deriving side
    is the runtime that knows the real account, and its salt never leaves that deployment;
    this server only knows a pseudonym. Deriving again here would hash a pseudonym into a
    different pseudonym - deterministic, isolated, and *wrong*: the subject the upstream
    means and the subject this control plane records would be two different tenants, and
    nothing would report an error.

    Anything that is not a well-formed pseudonym fails closed. It is never mapped, hashed,
    or dropped: mapping invents an identity, and dropping silently files one caller's work
    under whatever tenant the credential defaults to. Both answer 2xx while being wrong,
    which is the failure nobody finds.

    The old pass-through danger is gone with the old derivation. That one hashed the
    subject *unkeyed*, so anyone who knew a victim's account could compute their identifier
    offline and hand it back; a keyed pseudonym cannot be produced without the upstream's
    salt, and the runtime strips model-supplied values before injecting this one.
    """
    candidate = subject_id.strip()
    if not candidate:
        return ""
    if not _ACTING_SUBJECT_RE.fullmatch(candidate):
        raise SitesError(
            "the caller identity is not an acting-subject pseudonym; the calling "
            "runtime must derive it (32 lowercase hexadecimal characters) rather "
            "than pass an account identifier",
            code="sites_invalid_caller_identity",
        )
    return candidate

def _deployment_intent_schema() -> dict[str, Any]:
    return {
        "type": "string",
        "minLength": 2,
        "maxLength": 200,
        "description": (
            "A verbatim 2-200 character excerpt of the user's explicit request to "
            "deploy, host, publish, or return a public/live URL. Copy it byte-for-byte, "
            "including punctuation; do not rewrite, translate, supplement, or add a "
            "final punctuation mark. Preview-only, download-only, and file-generation "
            "requests have no valid value; do not call a Sites write tool."
        ),
    }


def _require_deployment_intent(arguments: dict[str, Any]) -> str:
    raw = arguments.get(DEPLOYMENT_INTENT_ARGUMENT)
    intent = str(raw or "").strip()
    trusted = arguments.get(DEPLOYMENT_AUTHORIZATION_ARGUMENT)
    trusted_run_id = trusted.get("runId") if isinstance(trusted, dict) else None
    trusted_nonce = trusted.get("nonce") if isinstance(trusted, dict) else None
    try:
        trusted_expiry = float(trusted.get("expiresAt", 0))
    except (TypeError, ValueError, AttributeError):
        trusted_expiry = 0
    trusted_context = bool(
        isinstance(trusted, dict)
        and trusted.get("version") == 1
        and isinstance(trusted_run_id, str)
        and 1 <= len(trusted_run_id) <= 128
        and isinstance(trusted_nonce, str)
        and len(trusted_nonce) >= 24
        and trusted_expiry > time.time()
    )
    if not trusted_context:
        raise ValidationError(
            "deployment_authorization_required: This run has no server-issued "
            "deployment authorization, so the model-provided deploymentIntent text "
            "cannot authorize a write. Do not retry Sites. Use a non-deployment "
            "mechanism for preview-only, download-only, or file-generation work."
        )
    if not 2 <= len(intent) <= 200:
        raise ValidationError(
            "deployment_intent_required: Quote a 2-200 character excerpt of "
            "the user's explicit deployment request. Do not call a write tool "
            "for preview-only or file-generation work."
        )
    return intent


def _require_standalone_exposure_authorization(
    arguments: dict[str, Any],
) -> None:
    """Prevent an Agent from turning a URL deployment into internal-only.

    The reserved authorization is issued from the original user prompt and is
    injected after model-provided reserved fields are stripped.  A model-side
    capacity guess or an in-run fallback therefore cannot broaden it.
    """

    if str(arguments.get("exposure", "public")).strip() != "internal":
        return
    trusted = arguments.get(DEPLOYMENT_AUTHORIZATION_ARGUMENT)
    if not isinstance(trusted, dict) or trusted.get("allowInternal") is not True:
        version = trusted.get("version") if isinstance(trusted, dict) else None
        allow_internal = (
            trusted.get("allowInternal") if isinstance(trusted, dict) else None
        )
        raise ValidationError(
            "internal_exposure_authorization_required: The user's original request does not clearly require "
            "cluster-internal, intranet, private, or internal-only deployment. Public deployment is not allowed. "
            "Do not downgrade to exposure=internal: Accessible URLs will not be returned in that mode. "
            "Keep exposure=public; if public deployment fails, report the original error and ask the user to "
            "clarify or propose on-premises deployment."
            f" authorizationPolicyVersion={version!r}; "
            f"allowInternal={allow_internal!r}"
        )


def _authorization_evidence(arguments: dict[str, Any]) -> dict[str, Any]:
    """Return only the non-secret part of the server-issued decision."""

    trusted = arguments.get(DEPLOYMENT_AUTHORIZATION_ARGUMENT)
    if not isinstance(trusted, dict):
        return {"policyVersion": None, "allowInternal": False}
    return {
        "policyVersion": trusted.get("version"),
        "allowInternal": trusted.get("allowInternal") is True,
    }


def _with_authorization_evidence(
    payload: dict[str, Any], arguments: dict[str, Any]
) -> dict[str, Any]:
    return {
        **payload,
        "deploymentAuthorization": _authorization_evidence(arguments),
    }


def _preflight_deployment_quota(
    client: Client,
    *,
    name: str,
    exposure: str,
) -> None:
    """Block only a definitely-new deployment when visible quota is full.

    These GETs reduce predictable failed writes; the control plane remains the
    atomic source of truth for races and any ambiguous response shape.
    """

    identity = client.whoami()
    listing = client.list_deployments()
    deployments = listing.get("deployments")
    if not isinstance(deployments, list):
        return
    active = [
        item
        for item in deployments
        if isinstance(item, dict) and not item.get("deletionRequestedAt")
    ]
    if any(
        str(item.get("serviceName") or item.get("name") or "") == name
        for item in active
    ):
        return

    max_deployments = identity.get("maxDeployments")
    if isinstance(max_deployments, int) and len(active) >= max_deployments:
        raise SitesError(
            "deployment quota is already full; blocked before mutation. "
            "Reuse an existing service name for an in-place update.",
            status=429,
            code="quota_preflight_blocked",
            retryable=False,
        )

    if exposure != "public":
        return
    max_public_routes = identity.get("maxPublicRoutes")
    visible_public = sum(1 for item in active if item.get("url"))
    if isinstance(max_public_routes, int) and visible_public >= max_public_routes:
        raise SitesError(
            "public route quota is already full; blocked before mutation. "
            "Reuse an existing service name for an in-place update; do not "
            "delete an unrelated deployment or silently downgrade exposure.",
            status=429,
            code="quota_preflight_blocked",
            retryable=False,
        )


def _limits_sentence(capabilities: dict[str, Any]) -> str:
    """Render capability limits as one unambiguous sentence for tool metadata."""
    limits = capabilities.get("limits") or {}
    modes = capabilities.get("deploymentModes") or {}
    features = capabilities.get("features") or {}
    parts: list[str] = []
    if (modes.get("staticInline") or {}).get("enabled"):
        parts.append(
            f"static inline deploys accept at most "
            f"{limits.get('maxInlineArtifactFiles', '?')} flat UTF-8 text files and "
            f"{limits.get('maxInlineArtifactBytes', '?')} total bytes, and require index.html"
        )
    if (modes.get("dockerfileSource") or {}).get("enabled"):
        parts.append(
            f"source builds accept at most {limits.get('maxSourceFiles', '?')} "
            "UTF-8 text files and require a root Dockerfile"
        )
    public_routes = limits.get("publicRoutes", "?")
    if public_routes is None:
        parts.append("there is no configured public-route capacity limit")
    else:
        parts.append(f"public-route capacity is {public_routes}")
    if not features.get("customDomains"):
        parts.append("Custom domain names are not supported")
    if not features.get("requestSecrets"):
        parts.append("secret values are not accepted; reference existing Secrets only")
    if features.get("serverSideVerification"):
        parts.append(
            "the control plane records its own in-cluster health request in "
            "status.verification; check the returned public URL separately from the "
            "user's network path"
        )
    return "; ".join(parts) + "."


def _tool_definitions(capabilities: dict[str, Any] | None) -> list[dict]:
    boundary = (
        _limits_sentence(capabilities)
        if capabilities
        else "The control plane is currently unreachable. Boundaries unknown: call the capabilities tool first. "
        "Do not assume any limits."
    )
    return [
        {
            "name": "capabilities",
            "description": "Read the live capability, quota, and feature contract. Treat this response as the authority boundary before deploying.",
            "annotations": {
                "readOnlyHint": True,
                "idempotentHint": True,
                "openWorldHint": True,
            },
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "scaffolds",
            "description": (
                "Read the live scaffold support matrix and its executable evidence. "
                "contractCheckSuccessRate covers only local validation checks; never "
                "present it as an Agent end-to-end build or deployment success rate."
            ),
            "annotations": {
                "readOnlyHint": True,
                "idempotentHint": True,
                "openWorldHint": False,
            },
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "list",
            "description": "List deployments visible to the current identity. Identity is the (merchantId, userId) pair; the same userId can exist under different merchants.",
            "annotations": {"readOnlyHint": True, "idempotentHint": True},
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "status",
            "description": "Read one deployment's live status and control-plane verification evidence (HTTP status and body digest). Verification probes the in-cluster service; request the returned public URL separately from the user's network path.",
            "annotations": {
                "readOnlyHint": True,
                "idempotentHint": True,
                "openWorldHint": True,
            },
            "inputSchema": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
        {
            "name": "deploy_static",
            "description": "Deploy inline flat static files only when the user explicitly asks for deployment, hosting, publishing, or a public/live URL. Do not use this tool to preview, download, or generate files. Read the files first and include index.html. The same name will be updated in place instead of allocating another route. Public URLs are not reachable from the sandbox; do not curl them there. Verify with the status tool and require ready=true plus verification.ok=true. "
            + boundary,
            "annotations": {"destructiveHint": True, "openWorldHint": True},
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Service name"},
                    "files": {
                        "type": "object",
                        "description": (
                            "Map of flat UTF-8 file name to text content; index.html is required"
                        ),
                        "additionalProperties": {"type": "string"},
                    },
                    "exposure": {
                        "type": "string",
                        "enum": ["public", "internal"],
                        "default": "public",
                        "description": (
                            "Public by default. Internal exposure does not return accessible URLs. Use internal only when the user's original request explicitly requires cluster-internal or private deployment. Do not downgrade yourself based on capacity judgment."
                        ),
                    },
                    DEPLOYMENT_INTENT_ARGUMENT: _deployment_intent_schema(),
                },
                "required": ["name", "files", DEPLOYMENT_INTENT_ARGUMENT],
            },
        },
        {
            "name": "deploy_static_versioned",
            "description": (
                "Upload a UTF-8 static file tree to private object storage, create an "
                "immutable content-addressed site version, and deploy that exact "
                "version with the fixed static runtime. Use this for site iteration "
                "and rollback-safe publishing; verify with the status and "
                "versions tools. "
            ) + boundary,
            "annotations": {"destructiveHint": True, "openWorldHint": True},
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Service name"},
                    "files": {
                        "type": "object",
                        "description": (
                            "Map of normalized UTF-8 relative path to text content; "
                            "nested directories are allowed and root index.html is required"
                        ),
                        "additionalProperties": {"type": "string"},
                    },
                    "contentSha256": {
                        "type": "string",
                        "pattern": "^[a-f0-9]{64}$",
                        "description": (
                            "Optional expected canonical file-set digest; the control "
                            "plane computes it and rejects a mismatch"
                        ),
                    },
                    "metadata": {"type": "object"},
                    "exposure": {
                        "type": "string",
                        "enum": ["public", "internal"],
                        "default": "public",
                    },
                    DEPLOYMENT_INTENT_ARGUMENT: _deployment_intent_schema(),
                },
                "required": ["name", "files", DEPLOYMENT_INTENT_ARGUMENT],
            },
        },
        {
            "name": "deploy_image",
            "description": "Deploy an existing container image only when the user explicitly asks for deployment, hosting, publishing, or a public/live URL. The cluster must be able to pull the image. The same name will be updated in place instead of allocating another route. Public URLs are not reachable from the sandbox; do not curl them there. Verify with the status tool. "
            + boundary,
            "annotations": {"destructiveHint": True, "openWorldHint": True},
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "image": {"type": "string"},
                    "port": {"type": "integer", "default": 8080},
                    "healthPath": {"type": "string", "default": "/"},
                    "livenessPath": {
                        "type": "string",
                        "description": "Optional; defaults to healthPath",
                    },
                    "exposure": {
                        "type": "string",
                        "enum": ["public", "internal"],
                        "default": "public",
                        "description": (
                            "Public by default. Internal exposure does not return accessible URLs. Use internal only when the user's original request explicitly requires cluster-internal or private deployment. Do not downgrade yourself based on capacity judgment."
                        ),
                    },
                    "env": {
                        "type": "array",
                        "description": (
                            "Non-sensitive plaintext values, or secretKeyRef references to existing Secrets; secret contents are never submitted"
                        ),
                        "items": {"type": "object"},
                    },
                    # These two properties were once left outside the schema: the model was invisible, but the dispatch
                    # Whitelist them for forwarding - they will be forwarded silently. The key set of the schema is the same as
                    # DEPLOY_FIELDS consistency is nailed by test_interface.
                    "secretMounts": {
                        "type": "array",
                        "description": (
                            "Read-only mounts for existing Secrets: {secretName, mountPath, optional items}. Only Secret references are submitted."
                        ),
                        "items": {"type": "object"},
                    },
                    "runAsUser": {
                        "type": "integer",
                        "default": 10001,
                        "description": (
                            "Container UID from 1-65535; the restricted namespace rejects UID 0"
                        ),
                    },
                    "scaleToZero": {
                        "type": "boolean",
                        "default": True,
                        "description": (
                            "Scale an idle public site to zero replicas and wake it on the next request. A cold start can take several seconds."
                        ),
                    },
                    "memoryLimit": {
                        "type": "string",
                        "default": "512Mi",
                        "description": (
                            "Container memory limit from 128Mi through 2Gi, for example 1Gi."
                        ),
                    },
                    "siteVersion": {
                        "type": "integer",
                        "minimum": 1,
                        "description": (
                            "Immutable dynamic-site version created before deployment. "
                            "The image must match that version exactly."
                        ),
                    },
                    DEPLOYMENT_INTENT_ARGUMENT: _deployment_intent_schema(),
                },
                "required": ["name", "image", DEPLOYMENT_INTENT_ARGUMENT],
            },
        },
        {
            "name": "whoami",
            "description": "Read the current (merchantId, userId) identity and quotas. Check this before interpreting a 429 response; the list tool shows current usage, never another tenant's resources.",
            "annotations": {"readOnlyHint": True, "idempotentHint": True},
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "deploy_dynamic",
            "description": (
                "Create an immutable dynamic-site version, provision its isolated "
                "PostgreSQL schema and roles, then deploy the exact digest-pinned "
                "image. The live version is promoted only after readiness and "
                "server-side verification; a failed update rolls back to the last "
                "promoted image. "
            ) + boundary,
            "annotations": {"destructiveHint": True, "openWorldHint": True},
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "image": {"type": "string", "description": "Image pinned with @sha256:<64 hex>"},
                    "contentSha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
                    "port": {"type": "integer", "default": 8080},
                    "healthPath": {"type": "string", "default": "/"},
                    "livenessPath": {"type": "string"},
                    "exposure": {"type": "string", "enum": ["public", "internal"], "default": "public"},
                    "env": {"type": "array", "items": {"type": "object"}},
                    "secretMounts": {"type": "array", "items": {"type": "object"}},
                    "runAsUser": {"type": "integer"},
                    "scaleToZero": {"type": "boolean"},
                    "memoryLimit": {"type": "string"},
                    "metadata": {"type": "object"},
                    "changeMode": {
                        "type": "string",
                        "enum": ["incremental", "rebuild-compatible"],
                    },
                    "databaseStrategy": {"type": "string", "enum": ["shared"]},
                    "databaseCompatibility": {
                        "type": "string", "enum": ["backward-compatible"]
                    },
                    "schemaChange": {
                        "type": "string",
                        "enum": ["none", "additive", "compatible", "destructive"],
                        "description": (
                            "Classify database impact independently from code refactoring. "
                            "destructive is reported but rejected from automatic deployment."
                        ),
                    },
                    "migrationStrategy": {
                        "type": "string", "enum": ["none", "expand-contract", "manual-cutover"]
                    },
                    "migrationSha256": {
                        "type": "string", "pattern": "^[a-f0-9]{64}$"
                    },
                    "migrationSql": {
                        "type": "string",
                        "description": (
                            "Exact UTF-8 migration artifact whose SHA-256 equals "
                            "migrationSha256. Required for additive/compatible changes; "
                            "only bounded idempotent additive PostgreSQL DDL is accepted."
                        ),
                    },
                    "decisionRationale": {"type": "string", "maxLength": 1000},
                    DEPLOYMENT_INTENT_ARGUMENT: _deployment_intent_schema(),
                },
                "required": [
                    "name", "image", "contentSha256", "changeMode",
                    "databaseStrategy", "databaseCompatibility",
                    "schemaChange", "migrationStrategy", "decisionRationale",
                    DEPLOYMENT_INTENT_ARGUMENT
                ],
            },
        },
        {
            "name": "query_database",
            "description": (
                "Execute one bounded read-only PostgreSQL SELECT against the named "
                "dynamic site. NL2SQL reasoning belongs in the agent skill; "
                "this tool deterministically validates the SQL AST, uses the site's "
                "reader role and read-only transaction, and caps timeout and rows."
            ),
            "annotations": {"readOnlyHint": True, "idempotentHint": True},
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Dynamic site name"},
                    "query": {"type": "string", "maxLength": 20000},
                    "rowLimit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 1000,
                        "default": 100,
                    },
                    "timeoutSeconds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10,
                        "default": 5,
                    },
                },
                "required": ["name", "query"],
            },
        },
        {
            "name": "versions",
            "description": (
                "List immutable versions, the currently promoted version, and the "
                "version presently deployed in Kubernetes. If deployedVersion differs "
                "from currentVersion, an update or rollback has not completed verified "
                "promotion yet. Use this before and after every site iteration."
            ),
            "annotations": {"readOnlyHint": True, "idempotentHint": True},
            "inputSchema": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
        {
            "name": "deploy_bundle",
            "description": "Atomically submit multiple interdependent components only when the user explicitly asks for deployment, hosting, publishing, or a public/live URL. Configure cross-component discovery with Kubernetes Service names in each component's env; the control plane does not prescribe the application topology. "
            + boundary,
            "annotations": {"destructiveHint": True, "openWorldHint": True},
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "bundle name"},
                    "components": {
                        "type": "array",
                        "description": (
                            "Each item uses the deploy_image properties. Internal components do not consume public-route quota."
                        ),
                        "items": {"type": "object"},
                    },
                    DEPLOYMENT_INTENT_ARGUMENT: _deployment_intent_schema(),
                },
                "required": ["name", "components", DEPLOYMENT_INTENT_ARGUMENT],
            },
        },
        {
            "name": "bundle_status",
            "description": "Read the overall phase and status of each component of a bundle.",
            "annotations": {"readOnlyHint": True, "idempotentHint": True},
            "inputSchema": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
        {
            "name": "delete",
            "description": "Delete one deployment in two stages. Without force=true, inspect the returned creation time and content fingerprint and confirm it is not an asset from a concurrent session. Only call again with force=true after that confirmation; deletion is irreversible.",
            "annotations": {
                "destructiveHint": True,
                "idempotentHint": True,
                "openWorldHint": True,
            },
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "force": {"type": "boolean"},
                },
                "required": ["name"],
            },
        },
        {
            "name": "source_deploy",
            "description": "Submit a bounded UTF-8 source tree with a root Dockerfile only when the user explicitly asks for deployment, hosting, publishing, or a public/live URL. Read the files first. Set buildOnly=true when the resulting digest will be passed to deploy_dynamic; use a unique build name per version. A Building response means the source was accepted; poll the build_status tool until Running, ready=true, and verification.ok=true. "
            + boundary,
            "annotations": {"destructiveHint": True, "openWorldHint": True},
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Service name"},
                    "files": {
                        "type": "object",
                        "description": "Map of UTF-8 relative file path to content; the root must contain a non-empty Dockerfile",
                        "additionalProperties": {"type": "string"},
                    },
                    "port": {"type": "integer", "default": 8080},
                    "healthPath": {"type": "string", "default": "/healthz"},
                    "buildOnly": {
                        "type": "boolean",
                        "default": False,
                        "description": "Build and verify an immutable image without creating a temporary SiteDeployment.",
                    },
                    DEPLOYMENT_INTENT_ARGUMENT: _deployment_intent_schema(),
                },
                "required": ["name", "files", DEPLOYMENT_INTENT_ARGUMENT],
            },
        },
        {
            "name": "build_status",
            "description": "Read one source build and its resulting deployment status. Success requires ready=true and verification.ok=true; Running alone is not acceptance.",
            "annotations": {
                "readOnlyHint": True,
                "idempotentHint": True,
                "openWorldHint": True,
            },
            "inputSchema": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
        {
            "name": "source_delete",
            "description": "Delete a source build and recycle its Job, SiteDeployment, source package, and Registry manifest.",
            "annotations": {
                "destructiveHint": True,
                "idempotentHint": True,
                "openWorldHint": True,
            },
            "inputSchema": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    ]


class Server:
    def __init__(
        self,
        client_factory: Callable[[], Client] | None = None,
        user_client_factory: Callable[[str], Client] | None = None,
        *,
        subject_from_arguments: bool = True,
        merchant_id: str = "",
    ):
        self._client_factory = client_factory or Client.from_env
        # Builds the client for a caller's own subject. None means from_env(subject=...):
        # the same credential as the service identity, only acting for someone.
        self._user_client_factory = user_client_factory
        # Whether a tool call may name the acting subject in its arguments
        # (CALLER_USER_ARGUMENT). True on stdio, where a trusted in-process runtime
        # injects the value after stripping any model-supplied one. False over the
        # network, where the subject travels with the credential as X-Acting-Subject and
        # a subject in the request body is a caller-declared identity - see
        # sites/api_mcp.py.
        self._subject_from_arguments = subject_from_arguments
        # Fallback for _with_merchant when no capabilities response has been cached.
        # The HTTP transport passes the merchant its authentication resolved, which is
        # decided by the credential and is therefore the authoritative answer.
        self._merchant_id = merchant_id
        self._client: Client | None = None
        self._capabilities: dict[str, Any] | None = None

    def _get_client(self) -> Client:
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def _client_for(self, subject: str) -> Client:
        """Pick the client for this call: service identity or the caller's.

        subject is empty → cached default identity (automation/call without account context,
        behaviour unchanged); non-empty → per-call client, deployment and query all fall
        under that subject. No caching of per-call clients: the set of subjects is open, so a
        cache is unbounded object growth.
        """
        if not subject:
            return self._get_client()
        if self._user_client_factory is not None:
            return self._user_client_factory(subject)
        return Client.from_env(subject=subject)

    def _describe(self) -> list[dict]:
        if self._capabilities is None:
            try:
                self._capabilities = self._get_client().capabilities()
            except (SitesError, OSError):
                # The temporary unreachability of the control plane should not prevent the entire server from being started: the tools are exposed as usual,
                # The description clearly states that the boundaries are unknown. Catches only business errors (SitesError has combined HTTP with
                # Network stack errors are included) and the OSError that covers everything - here was
                # The bare equivalent of (SitesError, Exception) except, which separates control plane unreachability and local
                # The programming errors of the process are swallowed up into "boundary unknowns", which no one can see from now on.
                self._capabilities = None
        return _tool_definitions(self._capabilities)

    def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            raise ValidationError("tool arguments must be a JSON object")
        arguments = dict(arguments)
        caller_user = ""
        raw = arguments.pop(CALLER_USER_ARGUMENT, "")
        caller_user = str(raw).strip() if raw else ""
        if caller_user and not self._subject_from_arguments:
            # 🔴 Refused, never dropped. Dropping it would run the call as whoever the
            # credential is and answer 2xx, so an agent host that meant to act for one
            # of its users would file that user's site under a different tenant and see
            # nothing wrong. On this transport the subject is carried by
            # X-Acting-Subject, where the control plane checks it against the key's
            # may_act_as_subjects grant.
            raise ValidationError(
                f"{CALLER_USER_ARGUMENT} is not accepted over this transport; the "
                "acting subject is carried by the credential, in the "
                f"{_ACTING_SUBJECT_HEADER} request header"
            )
        client = self._client_for(_caller_subject(caller_user))
        return self._with_merchant(self._dispatch(name, arguments, client))

    def _with_merchant(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Make sure the agent always sees which merchant it acted as.

        The control plane itself will bring merchantId in the response; here it is only used in capabilities when it is missing.
        Make up that copy, and **only make up but not overwrite** - the identity in the cache should not have the opportunity to change what the server just said
        That, otherwise the agent will keep reading to the previous merchant after changing the voucher.
        """
        if "merchantId" in payload:
            return payload
        merchant_id = (
            self._capabilities or {}
        ).get("merchantId") or self._merchant_id
        if not merchant_id:
            return payload
        return {**payload, "merchantId": merchant_id}

    def _dispatch(
        self,
        name: str,
        arguments: dict[str, Any],
        client: Client,
    ) -> dict[str, Any]:
        if name == "capabilities":
            self._capabilities = client.capabilities()
            return self._capabilities
        if name == "scaffolds":
            return client.scaffolds()
        if name == "list":
            return client.list_deployments()
        if name == "status":
            return client.get_deployment(str(arguments["name"]))
        if name == "delete":
            service_name = str(arguments["name"])
            if not arguments.get("force"):
                # 2026-08-20 Session accident: The model was deleted due to quota error and was directed to "Delete one and then deploy"
                # The concurrent session just deployed the product successfully and failed to rebuild itself - a net loss. Force before deletion
                # One round of summary confirmation: Creation time and content fingerprints are displayed, "Is it an activity of concurrent sessions?"
                # The judgment "product" has a basis, and is no longer a name in the list.
                target = client.get_deployment(service_name)
                spec = target.get("spec") or {}
                artifact = spec.get("artifact") or {}
                return {
                    "confirmation_required": True,
                    "name": target.get("name", service_name),
                    "createdAt": target.get("createdAt") or target.get("created_at"),
                    "image": spec.get("image"),
                    "bodyBytes": artifact.get("bodyBytes"),
                    "bodySha256": str(artifact.get("bodySha256", ""))[:16],
                    "hint": (
                        "After confirming that this is not an intentional deployment of concurrent sessions or users, use force=true "
                        "Recall to perform the delete. When the quota is insufficient, priority will be given to deploying in-place updates with the same name."
                        "(No new routes are added)."
                    ),
                }
            return client.delete_deployment(service_name)
        if name == "whoami":
            return client.whoami()
        if name == "query_database":
            return client.query_site(
                str(arguments["name"]),
                str(arguments["query"]),
                row_limit=int(arguments.get("rowLimit", 100)),
                timeout_seconds=int(arguments.get("timeoutSeconds", 5)),
            )
        if name == "deploy_dynamic":
            _require_deployment_intent(arguments)
            _require_standalone_exposure_authorization(arguments)
            service_name = str(arguments["name"])
            exposure = str(arguments.get("exposure", "public"))
            _preflight_deployment_quota(
                client, name=service_name, exposure=exposure
            )
            version = client.create_site_version(
                service_name,
                {
                    "siteType": "dynamic",
                    "contentSha256": str(arguments["contentSha256"]),
                    "image": str(arguments["image"]),
                    "metadata": arguments.get("metadata") or {},
                    "changeMode": str(arguments["changeMode"]),
                    "databaseStrategy": str(arguments["databaseStrategy"]),
                    "databaseCompatibility": str(arguments["databaseCompatibility"]),
                    "schemaChange": str(arguments["schemaChange"]),
                    "migrationStrategy": str(arguments["migrationStrategy"]),
                    "migrationSha256": str(arguments.get("migrationSha256") or ""),
                    "migrationSql": arguments.get("migrationSql"),
                    "decisionRationale": str(arguments["decisionRationale"]),
                },
            )
            payload = {
                key: arguments[key]
                for key in DEPLOY_FIELDS
                if key in arguments and key != "siteVersion"
            }
            payload["siteVersion"] = int(version["version"])
            deployed = client.deploy(payload)
            return _with_authorization_evidence(
                {"version": version, "deployment": deployed}, arguments
            )
        if name == "versions":
            return client.list_site_versions(str(arguments["name"]))
        if name == "deploy_bundle":
            _require_deployment_intent(arguments)
            return client.submit_bundle(
                {
                    "name": str(arguments["name"]),
                    "components": arguments["components"],
                }
            )
        if name == "bundle_status":
            return client.get_bundle(str(arguments["name"]))
        if name == "deploy_static":
            _require_deployment_intent(arguments)
            _require_standalone_exposure_authorization(arguments)
            files = arguments["files"]
            if not isinstance(files, dict) or not files:
                raise ValidationError(
                    "files must be non-empty filename→content mapping"
                )
            if "index.html" not in files:
                raise ValidationError(
                    "There is no index.html in files, and the static site cannot provide entry."
                )
            artifact = normalize_artifact({"files": files})
            assert artifact is not None
            service_name = str(arguments["name"])
            exposure = str(arguments.get("exposure", "public"))
            _preflight_deployment_quota(client, name=service_name, exposure=exposure)
            return _with_authorization_evidence(
                client.deploy(
                    {
                        "name": service_name,
                        "image": STATIC_IMAGE,
                        "port": 8080,
                        "healthPath": "/",
                        "exposure": exposure,
                        "artifact": {"files": artifact["files"]},
                    }
                ),
                arguments,
            )
        if name == "deploy_static_versioned":
            _require_deployment_intent(arguments)
            _require_standalone_exposure_authorization(arguments)
            files = arguments["files"]
            if not isinstance(files, dict) or not files:
                raise ValidationError(
                    "files must be non-empty filename→content mapping"
                )
            if "index.html" not in files:
                raise ValidationError(
                    "There is no index.html in files, and the static site cannot provide entry."
                )
            service_name = str(arguments["name"])
            exposure = str(arguments.get("exposure", "public"))
            _preflight_deployment_quota(client, name=service_name, exposure=exposure)
            version = client.create_static_site_version(
                service_name,
                files,
                content_sha256=(
                    str(arguments["contentSha256"])
                    if arguments.get("contentSha256")
                    else None
                ),
                metadata=arguments.get("metadata") or {},
            )
            deployed = client.deploy(
                {
                    "name": service_name,
                    "image": STATIC_IMAGE,
                    "port": 8080,
                    "healthPath": "/",
                    "exposure": exposure,
                    "runAsUser": STATIC_RUN_AS_USER,
                    "siteVersion": int(version["version"]),
                }
            )
            return _with_authorization_evidence(
                {"version": version, "deployment": deployed}, arguments
            )
        if name == "source_deploy":
            _require_deployment_intent(arguments)
            files = arguments["files"]
            if not isinstance(files, dict) or not files:
                raise ValidationError(
                    "files must be non-empty filename→content mapping"
                )
            if not str(files.get("Dockerfile", "")).strip():
                raise ValidationError("files root must contain a non-empty Dockerfile")
            service_name = str(arguments["name"])
            # buildOnly produces an immutable registry digest, not a route or
            # SiteDeployment. Deployment quota is checked when that digest is
            # promoted by deploy_dynamic.
            if not bool(arguments.get("buildOnly", False)):
                _preflight_deployment_quota(
                    client, name=service_name, exposure="public"
                )
            return _with_authorization_evidence(
                client.create_build(
                    {
                        "name": service_name,
                        "port": arguments.get("port", 8080),
                        "healthPath": arguments.get("healthPath", "/healthz"),
                        "buildOnly": bool(arguments.get("buildOnly", False)),
                        "files": files,
                    }
                ),
                arguments,
            )
        if name == "build_status":
            return client.get_build(str(arguments["name"]))
        if name == "source_delete":
            return client.delete_build(str(arguments["name"]))
        if name == "deploy_image":
            _require_deployment_intent(arguments)
            _require_standalone_exposure_authorization(arguments)
            # The forwarding whitelist is derived from DEPLOY_FIELDS: Here I have handwritten a key list, and
            # The inputSchema drifts individually, and there are parameters such as "the schema is not declared, but the hand plug is forwarded".
            payload = {key: arguments[key] for key in DEPLOY_FIELDS if key in arguments}
            _preflight_deployment_quota(
                client,
                name=str(arguments["name"]),
                exposure=str(arguments.get("exposure", "public")),
            )
            return _with_authorization_evidence(client.deploy(payload), arguments)
        raise ValidationError(f"unknown tool: {name}")

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        message_id = message.get("id")
        if method == "initialize":
            params = (
                message.get("params") if isinstance(message.get("params"), dict) else {}
            )
            return _result(
                message_id,
                {
                    "protocolVersion": params.get("protocolVersion")
                    or FALLBACK_PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": SERVER_INFO,
                },
            )
        if method == "tools/list":
            return _result(message_id, {"tools": self._describe()})
        if method == "tools/call":
            params = (
                message.get("params") if isinstance(message.get("params"), dict) else {}
            )
            name = str(params.get("name", ""))
            arguments = params.get("arguments") or {}
            try:
                payload = self._call(name, arguments)
            except Exception as exc:
                # MCP convention: If tool execution fails, isError will be returned instead of JSON-RPC error.
                # This way the model can see the cause of the failure and correct it on its own.
                return _result(
                    message_id,
                    {
                        "content": [{"type": "text", "text": _error_text(exc)}],
                        "isError": True,
                    },
                )
            return _result(
                message_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(payload, ensure_ascii=False, indent=2),
                        }
                    ],
                    "structuredContent": payload,
                },
            )
        if message_id is None:
            # Notifications (such as notifications/initialized) have no id and do not require a response.
            return None
        return {
            "jsonrpc": "2.0",
            "id": message_id,
            "error": {"code": -32601, "message": f"unknown method: {method}"},
        }


def _error_text(exc: Exception) -> str:
    if isinstance(exc, SitesError):
        return json.dumps(
            {
                "error": str(exc),
                "code": exc.code,
                "status": exc.status,
                "retryable": exc.retryable,
            },
            ensure_ascii=False,
        )
    if isinstance(exc, KeyError):
        return json.dumps(
            {"error": f"missing required argument: {exc.args[0]}"},
            ensure_ascii=False,
        )
    return json.dumps({"error": str(exc)}, ensure_ascii=False)


def _result(message_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": payload}


def serve_stdio(
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    server: Server | None = None,
) -> None:
    source = stdin or sys.stdin
    sink = stdout or sys.stdout
    active = server or Server()
    for line in source:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue
        response = active.handle(message)
        if response is None:
            continue
        sink.write(json.dumps(response, ensure_ascii=False) + "\n")
        sink.flush()


if __name__ == "__main__":
    serve_stdio()
