"""`hull`: the manifest-facing CLI (validate/compile/plan/apply/dev-token).

This is a second, unrelated command from `sites` (see `sites.cli`): `sites` deploys
workloads to the Sites control plane, `hull` publishes a Hull app manifest to a Broker.
They share nothing except the stdout-is-JSON, stderr-is-errors discipline and Python
version, so this module does not import `sites.cli`.

The admin token never appears on the command line (only a *path* to a file holding it
does) and is never written to stdout or stderr; every error message built here names
files, URLs, and manifest paths, never the token value.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from sites import manifest as manifest_module


class HullCliError(Exception):
    """A problem this CLI reports to the caller.

    `exit_code` defaults to 1 (broker/network problem); `resolve_manifest_path` and the
    admin-token lookup also raise this with the default so a missing local file and an
    unreachable Broker land on the same "something outside the manifest itself is wrong"
    exit status, distinct from `sites.manifest.ManifestError`'s 2 and the apply-conflict's 3.
    """

    def __init__(self, message: str, *, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def resolve_manifest_path(explicit: str | None) -> Path:
    """`./hull.yaml`, falling back to `./hull.json`, unless a path was given."""
    if explicit:
        return Path(explicit)
    yaml_path = Path("hull.yaml")
    if yaml_path.exists():
        return yaml_path
    json_path = Path("hull.json")
    if json_path.exists():
        return json_path
    raise HullCliError(
        "no manifest found: neither ./hull.yaml nor ./hull.json exists in the current directory",
        exit_code=2,
    )


def load_manifest(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HullCliError(f"cannot read manifest: {path}", exit_code=2) from exc
    if path.suffix in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError as exc:
            raise HullCliError(
                "PyYAML is not installed; install the project's dev extra to parse "
                "hull.yaml, or write the manifest as hull.json instead",
                exit_code=2,
            ) from exc
        try:
            loaded = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise HullCliError(f"manifest is not valid YAML: {path}", exit_code=2) from exc
        return loaded or {}
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise HullCliError(f"manifest is not valid JSON: {path}", exit_code=2) from exc


def make_read_file(base: Path) -> Callable[[str], str]:
    """Resolve prompt/instructions paths relative to the manifest file's own directory."""

    def read_file(relative: str) -> str:
        try:
            return (base / relative).read_text(encoding="utf-8")
        except OSError as exc:
            raise HullCliError(f"cannot read manifest-relative file: {relative}", exit_code=2) from exc

    return read_file


def _admin_token(args: argparse.Namespace) -> str:
    path = args.admin_token_file or os.environ.get("HULL_BROKER_ADMIN_TOKEN_FILE")
    if not path:
        raise HullCliError(
            "no admin token; set HULL_BROKER_ADMIN_TOKEN_FILE or pass --admin-token-file"
        )
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise HullCliError(f"cannot read admin token file: {path}") from exc


def _request(
    broker: str, method: str, path: str, token: str, body: dict | None = None
) -> tuple[int, dict]:
    url = broker.rstrip("/") + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        # HTTPError wraps the response socket itself; leaving it unread and unclosed
        # trips a ResourceWarning that (depending on the interpreter's warning filters)
        # lands on stderr ahead of the JSON error line below and breaks a caller
        # expecting stderr to be exactly one JSON object.
        with exc:
            raw = exc.read()
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {"error": raw.decode("utf-8", "replace")}
        return exc.code, parsed
    except urllib.error.URLError as exc:
        raise HullCliError(f"cannot reach the broker at {broker}: {exc.reason}") from exc


def _compiled(path: Path) -> dict:
    manifest = load_manifest(path)
    return manifest_module.compile(manifest, make_read_file(path.parent))


def cmd_validate(args: argparse.Namespace) -> dict:
    path = resolve_manifest_path(args.manifest)
    manifest = load_manifest(path)
    errors = manifest_module.validate(manifest)
    if errors:
        raise manifest_module.ManifestError(errors)
    return {"ok": True}


def cmd_compile(args: argparse.Namespace) -> dict:
    path = resolve_manifest_path(args.manifest)
    return _compiled(path)


def _plan(args: argparse.Namespace) -> tuple[dict, dict, str]:
    """Compile the manifest and ask the Broker to plan it; returns (compiled, plan, token)."""
    path = resolve_manifest_path(args.manifest)
    compiled = _compiled(path)
    token = _admin_token(args)
    status, result = _request(
        args.broker,
        "POST",
        f"/_hull/admin/v1/apps/{args.app}/config/plan",
        token,
        {"config": compiled},
    )
    if status == 400:
        raise HullCliError(
            "the broker rejected this config: " + "; ".join(result.get("errors", [])),
            exit_code=2,
        )
    if status != 200:
        raise HullCliError(
            f"plan request failed with status {status}: {result.get('error', 'unknown error')}"
        )
    return compiled, result, token


def cmd_plan(args: argparse.Namespace) -> dict:
    _, plan, _ = _plan(args)
    return plan


def cmd_apply(args: argparse.Namespace) -> dict:
    compiled, plan, token = _plan(args)
    changes = plan.get("changes", [])
    sys.stderr.write(f"Plan digest: {plan.get('digest', '')}\n")
    if changes:
        for change in changes:
            sys.stderr.write(f"  {change.get('op', '?')} {change.get('path', '?')}\n")
    else:
        sys.stderr.write("  no changes from the active version\n")
    if not args.yes:
        if not sys.stdin.isatty():
            raise HullCliError("apply needs --yes when not running in an interactive terminal")
        answer = input("Apply these changes? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            raise HullCliError("apply cancelled")
    status, applied = _request(
        args.broker,
        "POST",
        f"/_hull/admin/v1/apps/{args.app}/config/apply",
        token,
        {"config": compiled, "planDigest": plan.get("digest")},
    )
    if status == 409:
        raise HullCliError("someone applied in between, re-run plan", exit_code=3)
    if status != 200:
        raise HullCliError(
            f"apply request failed with status {status}: {applied.get('error', 'unknown error')}"
        )
    return applied


def cmd_dev_token(args: argparse.Namespace) -> dict:
    token = _admin_token(args)
    body: dict[str, Any] = {"site": args.site} if args.site else {}
    status, result = _request(
        args.broker,
        "POST",
        f"/_hull/admin/v1/apps/{args.app}/dev-app-token",
        token,
        body,
    )
    if status != 200:
        raise HullCliError(
            f"dev-token request failed with status {status}: {result.get('error', 'unknown error')}"
        )
    return result


def _run(args: argparse.Namespace) -> dict | None:
    if args.command == "validate":
        return cmd_validate(args)
    if args.command == "compile":
        return cmd_compile(args)
    if args.command == "plan":
        return cmd_plan(args)
    if args.command == "apply":
        return cmd_apply(args)
    if args.command == "dev-token":
        return cmd_dev_token(args)
    raise HullCliError(f"unknown command: {args.command}", exit_code=2)


def _add_broker_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--broker", required=True, help="Broker base URL, e.g. https://broker.internal")
    parser.add_argument("--app", required=True, help="target appEnvId")
    parser.add_argument(
        "--admin-token-file",
        default=None,
        help="path to a file holding the admin token (default $HULL_BROKER_ADMIN_TOKEN_FILE); "
        "the token itself is never a command-line value",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hull", description="Validate, compile, and publish a Hull app manifest."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="check a manifest against the Hull rules")
    validate.add_argument(
        "manifest", nargs="?", default=None,
        help="path to hull.yaml or hull.json (default: ./hull.yaml, falling back to ./hull.json)",
    )

    compile_cmd = sub.add_parser(
        "compile", help="print the normalized, prompt-inlined manifest as JSON"
    )
    compile_cmd.add_argument("manifest", nargs="?", default=None)

    plan = sub.add_parser("plan", help="ask the Broker what applying this manifest would change")
    plan.add_argument("manifest", nargs="?", default=None)
    _add_broker_flags(plan)

    apply_cmd = sub.add_parser("apply", help="plan, confirm, then publish this manifest")
    apply_cmd.add_argument("manifest", nargs="?", default=None)
    _add_broker_flags(apply_cmd)
    apply_cmd.add_argument(
        "--yes", action="store_true", default=False,
        help="apply without an interactive confirmation prompt",
    )

    dev_token = sub.add_parser(
        "dev-token",
        help="mint a development app token (the Broker must have HULL_BROKER_DEV_TOKENS=1)",
    )
    _add_broker_flags(dev_token)
    dev_token.add_argument("--site", default=None, help="site name to embed in the token")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = _run(args)
    except manifest_module.ManifestError as exc:
        json.dump({"errors": exc.errors}, sys.stderr, ensure_ascii=False)
        sys.stderr.write("\n")
        return 2
    except HullCliError as exc:
        json.dump({"error": str(exc)}, sys.stderr, ensure_ascii=False)
        sys.stderr.write("\n")
        return exc.exit_code
    if result is not None:
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
