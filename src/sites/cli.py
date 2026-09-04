"""Command-line entry point for Sites.

The CLI and the MCP server started by ``sites mcp`` are the two agent-facing interfaces.
Any agent that can run a shell can use the CLI without configuration. Both share
``sites.client`` and its request/authentication logic.

stdout is always JSON; errors are JSON on stderr with a non-zero exit status. Machine
parseability takes precedence over decorative output.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from sites.client import SitesError, Client
from sites.builds import (
    SOURCE_MAX_FILE_BYTES,
    SOURCE_MAX_FILES,
    SOURCE_MAX_TOTAL_BYTES,
    normalize_source_payload,
)
from sites.validation import (
    DEFAULT_MERCHANT_ID,
    DEPLOY_FIELDS,
    INLINE_ARTIFACT_MAX_FILES,
    INLINE_ARTIFACT_MAX_TOTAL_BYTES,
    STATIC_IMAGE,
    ValidationError,
    normalize_artifact,
)
from sites.identity import DEFAULT_USER_ID


def collect_site(directory: Path) -> dict[str, str]:
    """Read a flat static site directory into an inline artifact payload.

    Constraints: Only tiled one-layer text files are collected, which must have index.html. Check these restrictions locally,
    Instead of sending the entire directory and waiting for the server to reject it - then the agent will get "the request is too large",
    I can't tell which file the problem is.
    """
    if not directory.is_dir():
        raise ValidationError(f"not a directory: {directory}")
    files: dict[str, str] = {}
    for entry in sorted(directory.iterdir()):
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            raise ValidationError(
                f"subdirectories are not supported, found: {entry.name}/"
            )
        try:
            files[entry.name] = entry.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationError(
                f"static inline deploys only accept UTF-8 text: {entry.name}"
            ) from exc
    if not files:
        raise ValidationError(f"no files found in {directory}")
    if "index.html" not in files:
        raise ValidationError("a static site must contain index.html")
    if len(files) > INLINE_ARTIFACT_MAX_FILES:
        raise ValidationError(
            f"{len(files)} files exceeds the limit of {INLINE_ARTIFACT_MAX_FILES}"
        )
    total = sum(len(content.encode("utf-8")) for content in files.values())
    if total > INLINE_ARTIFACT_MAX_TOTAL_BYTES:
        raise ValidationError(
            f"{total} bytes exceeds the inline limit of "
            f"{INLINE_ARTIFACT_MAX_TOTAL_BYTES}; build an image instead"
        )
    return files


def collect_source(directory: Path) -> dict[str, str]:
    """Read a bounded UTF-8 Dockerfile source tree into a flat file map."""
    if not directory.is_dir():
        raise ValidationError(f"not a directory: {directory}")
    denied = {".env", ".git", ".hg", ".svn", ".venv", "__pycache__", "node_modules", "vendor"}
    files: dict[str, str] = {}
    total = 0
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory)
        if any(part in denied or part.startswith(".env.") for part in relative.parts):
            raise ValidationError(
                f"source entry is not allowed: {relative.as_posix()}"
            )
        if path.is_symlink():
            raise ValidationError(f"source symlinks are not supported: {relative.as_posix()}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValidationError(
                f"source entry is not a regular file: {relative.as_posix()}"
            )
        if len(files) >= SOURCE_MAX_FILES:
            raise ValidationError(
                f"source exceeds the limit of {SOURCE_MAX_FILES} files"
            )
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationError(
                f"source files must be UTF-8 text: {relative.as_posix()}"
            ) from exc
        size = len(content.encode("utf-8"))
        if size > SOURCE_MAX_FILE_BYTES:
            raise ValidationError(
                f"source file exceeds {SOURCE_MAX_FILE_BYTES} bytes: "
                f"{relative.as_posix()}"
            )
        total += size
        if total > SOURCE_MAX_TOTAL_BYTES:
            raise ValidationError(
                f"source exceeds {SOURCE_MAX_TOTAL_BYTES} total UTF-8 bytes"
            )
        files[relative.as_posix()] = content
    return files


def _parse_pairs(values: list[str] | None, flag: str) -> list[tuple[str, str]]:
    pairs = []
    for raw in values or []:
        name, separator, value = raw.partition("=")
        if not separator or not name:
            raise ValidationError(f"{flag} expects NAME=VALUE, got {raw!r}")
        pairs.append((name, value))
    return pairs


def build_env(args: argparse.Namespace) -> list[dict[str, Any]]:
    env: list[dict[str, Any]] = [
        {"name": name, "value": value}
        for name, value in _parse_pairs(args.env, "--env")
    ]
    for name, reference in _parse_pairs(args.secret_env, "--secret-env"):
        secret_name, separator, key = reference.partition("/")
        if not separator or not secret_name or not key:
            raise ValidationError(
                f"--secret-env expects NAME=secretName/key, got {reference!r}"
            )
        env.append(
            {"name": name, "secretKeyRef": {"name": secret_name, "key": key}}
        )
    return env


def build_secret_mounts(args: argparse.Namespace) -> list[dict[str, Any]]:
    mounts = []
    for raw in args.secret_mount or []:
        secret_name, separator, mount_path = raw.partition(":")
        if not separator or not secret_name or not mount_path:
            raise ValidationError(
                f"--secret-mount expects secretName:/path, got {raw!r}"
            )
        mounts.append({"secretName": secret_name, "mountPath": mount_path})
    return mounts


def _deploy_payload(args: argparse.Namespace) -> dict[str, Any]:
    # The key set is derived from DEPLOY_FIELDS single source: the value of each flag is first put into a dict, and the output is just
    # Select keys in the list that are not None - if you slip and write the wrong key name here, it will disappear directly instead of being emitted.
    # A field that is not recognized by the control plane. Options not given remain None and are eliminated here, as is the case with PATCH
    # The same semantics: if the --memory-limit is not given as "set back to default 512Mi", it will be silent.
    # Covering the configuration plane that the caller did not mention at all.
    values: dict[str, Any] = {
        "name": args.name,
        "image": args.image,
        "port": args.port,
        "healthPath": args.health_path,
        "livenessPath": args.liveness_path or None,
        "exposure": args.exposure,
        "runAsUser": args.run_as_user,
        "env": build_env(args) or None,
        "secretMounts": build_secret_mounts(args) or None,
        "scaleToZero": args.scale_to_zero or None,
        "memoryLimit": args.memory_limit,
        "siteVersion": args.site_version,
    }
    # DEPLOY_FIELDS If a new key is added and no value is provided above, KeyError will explode on the spot - than
    # The silent drift of "flag never takes effect" is good.
    return {key: values[key] for key in DEPLOY_FIELDS if values[key] is not None}


def _local_payload(args: argparse.Namespace) -> dict[str, Any] | None:
    """Build and validate whatever can be checked without the network.

    Complete the purely local verification first before touching the credentials and network: otherwise a wrongly written --secret-env will be "not configured"
    token", the error the caller gets points to a completely unrelated place.
    """
    if args.command == "deploy":
        return _deploy_payload(args)
    if args.command == "deploy-static":
        artifact = normalize_artifact({"files": collect_site(Path(args.directory))})
        assert artifact is not None
        return {
            "name": args.name,
            "image": args.image,
            "port": args.port,
            "healthPath": args.health_path,
            "exposure": args.exposure,
            "artifact": {"files": artifact["files"]},
        }
    if args.command == "bundle" and args.bundle_command == "submit":
        try:
            return json.loads(Path(args.file).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(f"cannot read bundle file: {exc}") from exc
    if args.command == "build" and args.build_command == "submit":
        payload = {
            "name": args.name,
            "port": args.port,
            "healthPath": args.health_path,
            "files": collect_source(Path(args.directory)),
        }
        normalize_source_payload(payload, DEFAULT_MERCHANT_ID, DEFAULT_USER_ID)
        return payload
    return None


def _changed(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Collect the flags that were actually given.

    PATCH only contains the mentioned fields: Treating the ungiven options as "set as default" will make it easier to change the quota at once
    Another quota is also reset without the caller mentioning it at all.
    """
    payload = {name: value for name, value in pairs if value is not None}
    if not payload:
        raise ValidationError("nothing to update; pass at least one option")
    return payload


def _run_admin(args: argparse.Namespace, client: Client) -> dict[str, Any]:
    """Dispatch the platform administration surface.

    The management interface only appears in the CLI and console, and is deliberately not made into an MCP tool - giving agent administrator capabilities
    It is equivalent to incorporating ultra vires into the product, see sites/mcp.py for details.
    """
    if args.admin_command == "health":
        return client.admin_health()
    if args.admin_command == "deployments":
        return client.admin_deployments(
            merchant_id=args.merchant, phase=args.phase, limit=args.limit
        )
    if args.admin_command == "merchants":
        if args.merchants_command == "list":
            return client.list_merchants()
        if args.merchants_command == "create":
            payload: dict[str, Any] = {
                "merchantId": args.merchant_id,
                "displayName": args.display_name,
            }
            if args.max_tenants is not None:
                payload["maxTenants"] = args.max_tenants
            if args.max_deployments is not None:
                payload["maxDeployments"] = args.max_deployments
            return client.create_merchant(payload)
        if args.merchants_command == "show":
            return client.get_merchant(args.merchant_id)
        if args.merchants_command == "update":
            return client.update_merchant(
                args.merchant_id,
                _changed(
                    [
                        ("displayName", args.display_name),
                        ("maxTenants", args.max_tenants),
                        ("maxDeployments", args.max_deployments),
                    ]
                ),
            )
        if args.merchants_command == "rotate-key":
            return client.rotate_merchant_key(args.merchant_id)
        if args.merchants_command == "disable":
            return client.disable_merchant(args.merchant_id)
    if args.admin_command == "tenants":
        if args.tenants_command == "list":
            return client.list_tenants(merchant_id=args.merchant)
        if args.tenants_command == "create":
            payload = {"merchantId": args.merchant, "userId": args.name}
            if args.max_deployments is not None:
                payload["maxDeployments"] = args.max_deployments
            if args.max_public_routes is not None:
                payload["maxPublicRoutes"] = args.max_public_routes
            return client.create_tenant(payload)
        if args.tenants_command == "update":
            return client.update_tenant(
                args.name,
                _changed(
                    [
                        ("maxDeployments", args.max_deployments),
                        ("maxPublicRoutes", args.max_public_routes),
                    ]
                ),
                merchant_id=args.merchant,
            )
        if args.tenants_command == "rotate":
            return client.rotate_tenant_token(
                args.name, merchant_id=args.merchant
            )
        if args.tenants_command == "disable":
            return client.disable_tenant(args.name, merchant_id=args.merchant)
    raise ValidationError(f"unknown admin command: {args.admin_command}")


def _run(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "mcp":
        from sites.mcp import serve_stdio

        serve_stdio()
        return {}

    payload = _local_payload(args)
    client = Client.from_env(
        **({"base_url": args.url} if args.url else {}),
    )
    if payload is not None:
        if args.command == "build":
            return client.create_build(payload)
        if args.command == "bundle":
            return client.submit_bundle(payload)
        return client.deploy(payload)
    if args.command == "capabilities":
        return client.capabilities()
    if args.command == "list":
        return client.list_deployments()
    if args.command == "status":
        return client.get_deployment(args.name)
    if args.command == "delete":
        return client.delete_deployment(args.name)
    if args.command == "whoami":
        return client.whoami()
    if args.command == "admin":
        return _run_admin(args, client)
    if args.command == "bundle":
        if args.bundle_command == "status":
            return client.get_bundle(args.name)
        if args.bundle_command == "delete":
            return client.delete_bundle(args.name)
    if args.command == "build":
        if args.build_command == "status":
            return client.get_build(args.name)
        if args.build_command == "delete":
            return client.delete_build(args.name)
    raise ValidationError(f"unknown command: {args.command}")


def _add_deploy_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--name", required=True, help="service name")
    parser.add_argument("--image", required=True, help="container image")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--health-path", default="/")
    parser.add_argument(
        "--liveness-path",
        default="",
        help="defaults to --health-path; set it when the readiness path "
        "checks downstream dependencies",
    )
    parser.add_argument(
        "--exposure", choices=("public", "internal"), default="public"
    )
    parser.add_argument("--run-as-user", type=int, default=None)
    parser.add_argument(
        "--env", action="append", metavar="NAME=VALUE",
        help="plain environment variable; never put secrets here",
    )
    parser.add_argument(
        "--secret-env", action="append", metavar="NAME=secretName/key",
        help="environment variable read from an existing Secret",
    )
    parser.add_argument(
        "--secret-mount", action="append", metavar="secretName:/path",
        help="mount an existing Secret read-only",
    )
    # The MCP side of these two switches has long been exposed, and the CLI has not been connected: the same capability should not depend on the caller
    # Which entrance to take. The legality of the value (128Mi-2Gi, gateway backend) is determined by the control plane
    # normalize_deploy_payload unified decision, CLI does not copy a rule.
    parser.add_argument(
        "--scale-to-zero",
        action="store_true",
        default=False,
        help="scale to zero replicas when idle and wake on traffic "
        "(public exposure on the gateway backend only)",
    )
    parser.add_argument(
        "--memory-limit",
        metavar="QUANTITY",
        help="container memory limit between 128Mi and 2Gi, e.g. 1Gi "
        "(default 512Mi); Node apps may need more than the default",
    )
    parser.add_argument(
        "--site-version",
        type=int,
        help="immutable dynamic-site version to bind to this deployment",
    )


def _add_admin_commands(sub: argparse._SubParsersAction) -> None:
    """Register `sites admin`, the human-only administration surface.

    This group replaces the original top-level `sites tenant`: the tenant's user_id is now unique only within the merchant,
    When naming a tenant, the merchant must also be named, and every item in the old order must be signed. Instead of leaving two names
    Pointing to the same thing (the old one will still be 400 steadily), it's better to just leave it in one place.
    """
    admin = sub.add_parser(
        "admin",
        help="platform administration (admin token only)",
        description="The management plane only accepts admin tokens and is intentionally not exposed to agents as MCP tools.",
    )
    admin_sub = admin.add_subparsers(dest="admin_command", required=True)

    merchants = admin_sub.add_parser(
        "merchants",
        help="manage merchants",
        description="Merchants hold API keys and can substitute any tenant under their own name.",
    )
    merchants_sub = merchants.add_subparsers(
        dest="merchants_command", required=True
    )
    merchants_sub.add_parser("list", help="list merchants with quota usage")
    merchant_create = merchants_sub.add_parser(
        "create",
        help="create a merchant and print its API key once",
        description="The apiKey plaintext is only returned this time, and only the summary is stored in the library; if it is lost, you can only rotate-key.",
    )
    merchant_create.add_argument("merchant_id")
    merchant_create.add_argument("--display-name", required=True)
    merchant_create.add_argument("--max-tenants", type=int, default=None)
    merchant_create.add_argument("--max-deployments", type=int, default=None)
    merchant_show = merchants_sub.add_parser(
        "show", help="one merchant plus the tenants under it"
    )
    merchant_show.add_argument("merchant_id")
    merchant_update = merchants_sub.add_parser(
        "update", help="change the display name or quotas"
    )
    merchant_update.add_argument("merchant_id")
    merchant_update.add_argument("--display-name", default=None)
    merchant_update.add_argument("--max-tenants", type=int, default=None)
    merchant_update.add_argument("--max-deployments", type=int, default=None)
    merchant_rotate = merchants_sub.add_parser(
        "rotate-key", help="issue a new API key; the old one stops working"
    )
    merchant_rotate.add_argument("merchant_id")
    merchant_disable = merchants_sub.add_parser(
        "disable",
        help="revoke a merchant and every tenant token under it; data is kept",
        description="Deactivation closes two paths at the same time: the merchant key and the tenant's own token are invalid.",
    )
    merchant_disable.add_argument("merchant_id")

    tenants = admin_sub.add_parser(
        "tenants",
        help="manage tenants inside a merchant",
        description="The user_id is only unique within the merchant, so naming a tenant must include --merchant.",
    )
    tenants_sub = tenants.add_subparsers(dest="tenants_command", required=True)
    tenant_list = tenants_sub.add_parser(
        "list", help="list tenants; without --merchant this is the whole platform"
    )
    tenant_list.add_argument("--merchant", default="")
    tenant_create = tenants_sub.add_parser(
        "create",
        help="create a tenant and print its token once",
        description="The token plaintext is returned only this time, and only the digest is stored in the library. If it is lost, it can only be rotated.",
    )
    tenant_create.add_argument("name")
    tenant_create.add_argument("--merchant", required=True)
    tenant_create.add_argument("--max-deployments", type=int, default=None)
    tenant_create.add_argument("--max-public-routes", type=int, default=None)
    tenant_update = tenants_sub.add_parser(
        "update", help="change a tenant's quotas"
    )
    tenant_update.add_argument("name")
    tenant_update.add_argument("--merchant", required=True)
    tenant_update.add_argument("--max-deployments", type=int, default=None)
    tenant_update.add_argument("--max-public-routes", type=int, default=None)
    tenant_rotate = tenants_sub.add_parser(
        "rotate",
        help="issue a new token; also re-enables a disabled tenant",
        description=(
            "The old token is invalidated atomically. Rotating also re-enables "
            "a disabled tenant."
        ),
    )
    tenant_rotate.add_argument("name")
    tenant_rotate.add_argument("--merchant", required=True)
    tenant_disable = tenants_sub.add_parser(
        "disable",
        help="revoke a tenant's credentials; its workloads are left alone",
    )
    tenant_disable.add_argument("name")
    tenant_disable.add_argument("--merchant", required=True)

    deployments = admin_sub.add_parser(
        "deployments",
        help="every deployment on the platform, across merchants and tenants",
    )
    deployments.add_argument("--merchant", default="")
    deployments.add_argument("--phase", default="")
    deployments.add_argument("--limit", type=int, default=None)

    admin_sub.add_parser(
        "health",
        help="control-plane self-check (database, operator, registry, kubernetes)",
        description="Each item has its own reachable, and failure of one item will not cause the entire response to fail.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sites",
        description="Deploy bounded workloads through a Sites control plane.",
    )
    parser.add_argument(
        "--url", default="", help="control plane URL (default $SITES_URL)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("capabilities", help="print the bounded deployment contract")
    sub.add_parser("list", help="list deployments")
    sub.add_parser("mcp", help="serve the same capabilities over MCP on stdio")

    status = sub.add_parser(
        "status", help="read one deployment, including server-side verification"
    )
    status.add_argument("name")

    delete = sub.add_parser("delete", help="delete a deployment")
    delete.add_argument("name")

    _add_deploy_flags(sub.add_parser("deploy", help="deploy an existing image"))

    static = sub.add_parser(
        "deploy-static", help="deploy a flat static site directory"
    )
    static.add_argument("--name", required=True)
    static.add_argument("--directory", required=True)
    static.add_argument("--port", type=int, default=8080)
    static.add_argument("--health-path", default="/")
    static.add_argument(
        "--exposure", choices=("public", "internal"), default="public"
    )
    static.add_argument(
        "--image",
        # Shares the same canonical with MCP's deploy_static (see common),
        # Two versions were written in each place and drifted; still overridable when --image is given explicitly.
        default=STATIC_IMAGE,
        help="static runtime image; must serve the mounted site root",
    )

    sub.add_parser(
        "whoami", help="print this token's own identity and quota"
    )

    build = sub.add_parser(
        "build", help="submit or inspect a bounded Dockerfile source build"
    )
    build_sub = build.add_subparsers(dest="build_command", required=True)
    build_submit = build_sub.add_parser(
        "submit", help="build and deploy a local UTF-8 source directory"
    )
    build_submit.add_argument("--name", required=True)
    build_submit.add_argument("--directory", required=True)
    build_submit.add_argument("--port", type=int, default=8080)
    build_submit.add_argument("--health-path", default="/healthz")
    build_status = build_sub.add_parser("status")
    build_status.add_argument("name")
    build_delete = build_sub.add_parser("delete")
    build_delete.add_argument("name")

    _add_admin_commands(sub)

    bundle = sub.add_parser("bundle", help="submit or inspect a component bundle")
    bundle_sub = bundle.add_subparsers(dest="bundle_command", required=True)
    submit = bundle_sub.add_parser("submit", help="submit a bundle JSON file")
    submit.add_argument("file")
    bundle_status = bundle_sub.add_parser("status")
    bundle_status.add_argument("name")
    bundle_delete = bundle_sub.add_parser("delete")
    bundle_delete.add_argument("name")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = _run(args)
    except ValidationError as exc:
        json.dump(
            {"error": str(exc), "code": "sites_invalid_input"},
            sys.stderr,
            ensure_ascii=False,
        )
        sys.stderr.write("\n")
        return 2
    except SitesError as exc:
        json.dump(
            {
                "error": str(exc),
                "code": exc.code,
                "status": exc.status,
                "retryable": exc.retryable,
            },
            sys.stderr,
            ensure_ascii=False,
        )
        sys.stderr.write("\n")
        return 1
    if result:
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
