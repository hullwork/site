"""Hull app manifest: validation, compilation, and the digest the CLI and the Broker share.

The manifest is the tenant-authored declaration of a Hull app: roles, plans, entities,
permissions, capabilities, agents, and flows (contract section 3.6). `validate` enforces
the same rules the Broker's admin API re-checks on apply, so a rejection from `hull
validate` matches what `hull apply` would hit later. `compile` produces the normalized
form partition C stores and partitions C and D read: defaults filled, prompt/instructions
files inlined, `apiVersion` pinned. `canonical_json`/`canonical_digest` are the exact
hashing contract shares with the Broker's own plan digest (section 3.5), so the CLI can
print a digest that means the same thing as the one the server computed for a plan.

`agent_tool_name` is the tool-reference-to-AG-UI-tool-name mapping partition D also
implements; it lives here because the manifest is what defines the grammar of a tool
reference in the first place. See docs/HULL_MANIFEST.md for the reference documentation
partition D was told to read.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Any, Callable

from sites.broker import jsonschema_lite

API_VERSION = "hull/v1alpha1"

# Contract 3.6, verbatim: "Entity/column/table names match ^[a-z_][a-z0-9_]{0,62}$."
_ENTITY_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
# App/agent/flow names become URL path segments (contract 3.4/3.5) and JSON-pointer path
# components in these very error messages; a plain lowercase slug (contract's own examples
# mix '-', "support-desk", and '_', "summarize_ticket") keeps both readable and keeps this
# grammar free of characters ('/', '~') JSON Pointer would need to escape.
_SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
# Roles, plans, and feature names are compared for membership only, never placed in a URL
# or hashed into an AG-UI tool name, so they get the same slug grammar as a courtesy
# consistency rule rather than a hard protocol requirement.
_ROLE_PLAN_FEATURE_RE = _SLUG_RE
# Capability aliases and their tool names are concatenated by agent_tool_name() into one
# AG-UI tool name (`^[A-Za-z][A-Za-z0-9_]{0,63}$`, contract section 3.6/"Tool reference
# grammar"): no '-' (not in that grammar) and short enough that two of them plus a "__"
# separator still fit in 64 characters.
_ALIAS_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_SECRET_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_PIN_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_AGENT_TOOL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")

_PERMISSION_ACTIONS = {"read", "create", "update", "delete"}
_CREDENTIALS = {"app", "end_user"}
_RISKS = {"read", "write", "destructive"}
_RUN_AS = {"end_user", "app"}
_AUDIENCES = {"server", "public"}
_CAPABILITY_KINDS = {"mcp"}  # connectors (e.g. Stripe refunds) are out of scope this slice.


class ManifestError(ValueError):
    """Raised by `compile` when the manifest fails validation.

    Carries every error `validate` found (or, for a path-inlining failure, the single
    error describing it) so a caller can report all of them at once instead of fixing
    one and re-running to discover the next.
    """

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors) if errors else "invalid manifest")
        self.errors = list(errors)


def agent_tool_name(ref: str) -> str:
    """Map a manifest tool reference to the flat AG-UI tool name partition D exposes.

    `kb.search_docs` -> `kb__search_docs`; `entity:tickets.read` -> `entity__tickets__read`.
    Both reference grammars use a single separator character (`.`, or `:` then `.`) that an
    AG-UI tool name cannot contain, so each becomes `__`. A reference that does not parse,
    or whose mapped name does not fit `^[A-Za-z][A-Za-z0-9_]{0,63}$`, is refused here rather
    than silently truncated or reused with a collision, because partition D looks the flat
    name up by exact match.
    """
    if ref.startswith("entity:"):
        entity, sep, action = ref[len("entity:") :].partition(".")
        if not sep or not entity or not action:
            raise ValueError(f"not a valid tool reference: {ref!r}")
        mapped = f"entity__{entity}__{action}"
    else:
        alias, sep, tool = ref.partition(".")
        if not sep or not alias or not tool:
            raise ValueError(f"not a valid tool reference: {ref!r}")
        mapped = f"{alias}__{tool}"
    if not _AGENT_TOOL_NAME_RE.fullmatch(mapped):
        raise ValueError(f"tool reference maps to an invalid AG-UI tool name: {ref!r}")
    return mapped


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def validate(manifest: dict, *, max_tools_per_agent: int = 10) -> list[str]:
    """Check *manifest* against contract section 3.6 and return one sentence per problem.

    Each entry starts with a JSON-pointer-like path so a caller (or a test) can tell which
    field is wrong without parsing the sentence. An empty list means the manifest is valid.
    Structural checks (types, cross-references between sections) are included even though
    the contract states them informally, because `compile` cannot fill defaults or inline
    prompts from a manifest whose shape it cannot trust.
    """
    errors: list[str] = []

    def err(path: str, message: str) -> None:
        errors.append(f"{path}: {message}")

    if not isinstance(manifest, dict):
        return ["/: a manifest must be a JSON object"]

    api_version = manifest.get("apiVersion")
    if api_version is not None and api_version != API_VERSION:
        err("/apiVersion", f"must be {API_VERSION!r} when present")

    app = manifest.get("app")
    if not isinstance(app, str) or not _SLUG_RE.fullmatch(app):
        err("/app", "must be a lowercase slug matching ^[a-z][a-z0-9_-]{0,62}$")

    roles = manifest.get("roles")
    role_names: set[str] = set()
    if not isinstance(roles, list) or not roles:
        err("/roles", "must be a non-empty list of role names")
    else:
        for index, role in enumerate(roles):
            path = f"/roles/{index}"
            if not isinstance(role, str) or not _ROLE_PLAN_FEATURE_RE.fullmatch(role):
                err(path, "role name must match ^[a-z][a-z0-9_-]{0,62}$")
            elif role in role_names:
                err(path, f"duplicate role name {role!r}")
            else:
                role_names.add(role)

    default_role = manifest.get("default_role")
    if default_role is not None and default_role not in role_names:
        err("/default_role", "must name a role declared in roles")

    plans = manifest.get("plans")
    plan_names: set[str] = set()
    if not isinstance(plans, dict) or not plans:
        err("/plans", "must be a non-empty object of plan name to plan")
    else:
        for name, plan in plans.items():
            path = f"/plans/{name}"
            if not isinstance(name, str) or not _ROLE_PLAN_FEATURE_RE.fullmatch(name):
                err(path, "plan name must match ^[a-z][a-z0-9_-]{0,62}$")
                continue
            plan_names.add(name)
            if not isinstance(plan, dict):
                err(path, "must be an object")
                continue
            features = plan.get("features", [])
            if not isinstance(features, list) or not all(isinstance(f, str) for f in features):
                err(f"{path}/features", "must be a list of feature name strings")
            limits = plan.get("limits", {})
            if not isinstance(limits, dict):
                err(f"{path}/limits", "must be an object")
            else:
                for limit_name, limit_value in limits.items():
                    if limit_value is not None and not _is_positive_int(limit_value):
                        err(
                            f"{path}/limits/{limit_name}",
                            "must be null (unlimited) or a positive integer",
                        )

    default_plan = manifest.get("default_plan")
    if default_plan is not None and default_plan not in plan_names:
        err("/default_plan", "must name a plan declared in plans")

    entities = manifest.get("entities", {})
    entity_names: set[str] = set()
    if not isinstance(entities, dict):
        err("/entities", "must be an object of entity name to entity")
        entities = {}
    for name, entity in entities.items():
        path = f"/entities/{name}"
        if not isinstance(name, str) or not _ENTITY_NAME_RE.fullmatch(name):
            err(path, "entity name must match ^[a-z_][a-z0-9_]{0,62}$")
            continue
        entity_names.add(name)
        if not isinstance(entity, dict):
            err(path, "must be an object")
            continue
        table = entity.get("table")
        if not isinstance(table, str) or not _ENTITY_NAME_RE.fullmatch(table):
            err(f"{path}/table", "table name must match ^[a-z_][a-z0-9_]{0,62}$")
        primary_key = entity.get("primary_key")
        if not isinstance(primary_key, str) or not _ENTITY_NAME_RE.fullmatch(primary_key):
            err(f"{path}/primary_key", "primary_key must match ^[a-z_][a-z0-9_]{0,62}$")
        columns = entity.get("columns")
        if not isinstance(columns, list) or not columns:
            err(f"{path}/columns", "must be a non-empty list of column names")
        else:
            column_names: set[str] = set()
            for index, column in enumerate(columns):
                cpath = f"{path}/columns/{index}"
                if not isinstance(column, str) or not _ENTITY_NAME_RE.fullmatch(column):
                    err(cpath, "column name must match ^[a-z_][a-z0-9_]{0,62}$")
                elif column in column_names:
                    err(cpath, f"duplicate column name {column!r}")
                else:
                    column_names.add(column)
            if isinstance(primary_key, str) and primary_key not in column_names:
                err(f"{path}/primary_key", "primary_key must be one of columns")

    permissions = manifest.get("permissions", {})
    if not isinstance(permissions, dict):
        err("/permissions", "must be an object of role name to entity permissions")
        permissions = {}
    for role, grants in permissions.items():
        path = f"/permissions/{role}"
        if role not in role_names:
            err(path, "permission role must be declared in roles")
        if not isinstance(grants, dict):
            err(path, "must be an object of entity name to a permission grant")
            continue
        for entity, grant in grants.items():
            epath = f"{path}/{entity}"
            if entity not in entity_names:
                err(epath, "permission entity must be declared in entities")
            if not isinstance(grant, dict):
                err(epath, "must be an object")
                continue
            actions = grant.get("actions")
            if not isinstance(actions, list) or not actions:
                err(f"{epath}/actions", "must be a non-empty list of actions")
            else:
                for index, action in enumerate(actions):
                    if action not in _PERMISSION_ACTIONS:
                        err(
                            f"{epath}/actions/{index}",
                            f"action must be one of {sorted(_PERMISSION_ACTIONS)}",
                        )

    secrets = manifest.get("secrets", [])
    secret_names: set[str] = set()
    if not isinstance(secrets, list):
        err("/secrets", "must be a list of secret names")
    else:
        for index, secret in enumerate(secrets):
            path = f"/secrets/{index}"
            if not isinstance(secret, str) or not _SECRET_RE.fullmatch(secret):
                err(path, "secret name must match ^[A-Z][A-Z0-9_]*$")
            elif secret in secret_names:
                err(path, f"duplicate secret name {secret!r}")
            else:
                secret_names.add(secret)

    capabilities = manifest.get("capabilities", [])
    capability_by_alias: dict[str, dict] = {}
    if not isinstance(capabilities, list):
        err("/capabilities", "must be a list of capability objects")
        capabilities = []
    for index, capability in enumerate(capabilities):
        path = f"/capabilities/{index}"
        if not isinstance(capability, dict):
            err(path, "must be an object")
            continue
        alias = capability.get("alias")
        if not isinstance(alias, str) or not _ALIAS_RE.fullmatch(alias):
            err(f"{path}/alias", "capability alias must match ^[a-z][a-z0-9_]{0,31}$")
        elif alias in capability_by_alias:
            err(f"{path}/alias", f"duplicate capability alias {alias!r}")
        else:
            capability_by_alias[alias] = capability
        kind = capability.get("kind")
        if kind not in _CAPABILITY_KINDS:
            err(f"{path}/kind", f"kind must be one of {sorted(_CAPABILITY_KINDS)} in this slice")
        url = capability.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            err(f"{path}/url", "must be an http:// or https:// URL")
        credential = capability.get("credential")
        if credential not in _CREDENTIALS:
            err(f"{path}/credential", f"credential must be one of {sorted(_CREDENTIALS)}")
        risk = capability.get("risk")
        if risk not in _RISKS:
            err(f"{path}/risk", f"risk must be one of {sorted(_RISKS)}")
        tools = capability.get("tools")
        if not isinstance(tools, list) or not tools:
            err(f"{path}/tools", "must be a non-empty list of tool names")
        else:
            seen_tools: set[str] = set()
            for tindex, tool in enumerate(tools):
                tpath = f"{path}/tools/{tindex}"
                if not isinstance(tool, str) or not _ALIAS_RE.fullmatch(tool):
                    err(tpath, "tool name must match ^[a-z][a-z0-9_]{0,31}$")
                elif tool in seen_tools:
                    err(tpath, f"duplicate tool name {tool!r}")
                else:
                    seen_tools.add(tool)
        pin = capability.get("pin")
        if pin is not None and (not isinstance(pin, str) or not _PIN_RE.fullmatch(pin)):
            err(f"{path}/pin", "pin must be null or a sha256:<64 hex chars> digest")

    agents = manifest.get("agents", {})
    if not isinstance(agents, dict):
        err("/agents", "must be an object of agent name to agent")
        agents = {}
    for name, agent in agents.items():
        path = f"/agents/{name}"
        if not isinstance(name, str) or not _SLUG_RE.fullmatch(name):
            err(path, "agent name must match ^[a-z][a-z0-9_-]{0,62}$")
        if not isinstance(agent, dict):
            err(path, "must be an object")
            continue
        instructions = agent.get("instructions")
        if not isinstance(instructions, str) or not instructions:
            err(f"{path}/instructions", "must be a non-empty string")
        model_tier = agent.get("model_tier")
        if not isinstance(model_tier, str) or not model_tier:
            err(f"{path}/model_tier", "must be a non-empty string")
        scope = agent.get("scope")
        if scope is not None and not isinstance(scope, str):
            err(f"{path}/scope", "must be null or a string")
        guardrails = agent.get("guardrails", [])
        if not isinstance(guardrails, list) or not all(isinstance(g, str) for g in guardrails):
            err(f"{path}/guardrails", "must be a list of strings")
        skills = agent.get("skills", [])
        if not isinstance(skills, list) or not all(isinstance(s, str) for s in skills):
            err(f"{path}/skills", "must be a list of strings")
        run_as = agent.get("run_as")
        if run_as not in _RUN_AS:
            err(f"{path}/run_as", f"run_as must be one of {sorted(_RUN_AS)}")
        requires_feature = agent.get("requires_feature")
        if requires_feature is not None and not isinstance(requires_feature, str):
            err(f"{path}/requires_feature", "must be null or a string")
        budget = agent.get("budget")
        if not isinstance(budget, dict) or not _is_positive_int(budget.get("max_credits_per_run")):
            err(
                f"{path}/budget/max_credits_per_run",
                "must be present and a positive integer; a missing budget is a validation "
                "error, not a default",
            )
        tools = agent.get("tools", [])
        if not isinstance(tools, list):
            err(f"{path}/tools", "must be a list of tool references")
            tools = []
        if len(tools) > max_tools_per_agent:
            err(
                f"{path}/tools",
                f"has {len(tools)} tools, more than the limit of {max_tools_per_agent}",
            )
        seen_refs: set[str] = set()
        for tindex, ref in enumerate(tools):
            tpath = f"{path}/tools/{tindex}"
            if not isinstance(ref, str):
                err(tpath, "tool reference must be a string")
                continue
            if ref in seen_refs:
                err(tpath, f"duplicate tool reference {ref!r}")
            seen_refs.add(ref)
            if ref.startswith("entity:"):
                entity, sep, action = ref[len("entity:") :].partition(".")
                if not sep or action != "read":
                    err(tpath, "an entity tool reference must be entity:<entity>.read; entity writes are out of scope")
                elif entity not in entity_names:
                    err(tpath, "entity tool reference names an entity that is not declared")
            else:
                alias, sep, tool = ref.partition(".")
                if not sep:
                    err(
                        tpath,
                        "tool reference must be entity:<entity>.read or "
                        "<capability alias>.<tool>",
                    )
                elif alias not in capability_by_alias:
                    err(tpath, "tool reference names a capability alias that is not declared")
                else:
                    capability = capability_by_alias[alias]
                    if tool not in (capability.get("tools") or []):
                        err(tpath, "tool is not listed in that capability's tools")
                    if run_as == "app" and capability.get("risk") != "read":
                        err(
                            tpath,
                            "a run_as: app agent may only reference risk: read capabilities",
                        )
            try:
                agent_tool_name(ref)
            except ValueError as exc:
                err(tpath, str(exc))

    flows = manifest.get("flows", {})
    if not isinstance(flows, dict):
        err("/flows", "must be an object of flow name to flow")
        flows = {}
    any_public_flow = False
    for name, flow in flows.items():
        path = f"/flows/{name}"
        if not isinstance(name, str) or not _SLUG_RE.fullmatch(name):
            err(path, "flow name must match ^[a-z][a-z0-9_-]{0,62}$")
        if not isinstance(flow, dict):
            err(path, "must be an object")
            continue
        mode = flow.get("mode")
        if mode != "llm":
            err(f"{path}/mode", "mode must be 'llm'; this slice implements no other mode")
        model_tier = flow.get("model_tier")
        if not isinstance(model_tier, str) or not model_tier:
            err(f"{path}/model_tier", "must be a non-empty string")
        prompt = flow.get("prompt")
        if not isinstance(prompt, str) or not prompt:
            err(f"{path}/prompt", "must be a non-empty string")
        for key in ("input_schema", "output_schema"):
            schema = flow.get(key, {})
            if not isinstance(schema, dict):
                err(f"{path}/{key}", "must be an object")
                continue
            # The Broker validates flow input and model output with a small
            # JSON Schema subset; a keyword outside it would be silently ignored,
            # so a model answer the author meant to reject would pass as valid.
            unsupported = jsonschema_lite.supported(schema)
            if unsupported:
                err(f"{path}/{key}", "uses JSON Schema keywords the Broker does not enforce: "
                    + ", ".join(unsupported))
        if "fallback" not in flow:
            err(f"{path}/fallback", "must be present; it is what a disabled or over-budget call returns")
        audience = flow.get("audience")
        if audience not in _AUDIENCES:
            err(f"{path}/audience", f"audience must be one of {sorted(_AUDIENCES)}")
        elif audience == "public":
            any_public_flow = True
        requires_feature = flow.get("requires_feature")
        if requires_feature is not None and not isinstance(requires_feature, str):
            err(f"{path}/requires_feature", "must be null or a string")
        budget = flow.get("budget")
        if not isinstance(budget, dict) or not _is_positive_int(budget.get("max_credits_per_call")):
            err(
                f"{path}/budget/max_credits_per_call",
                "must be present and a positive integer; a missing budget is a validation "
                "error, not a default",
            )

    if any_public_flow and capabilities:
        err(
            "/flows",
            "a public-audience flow cannot coexist with any capabilities declared in this manifest",
        )

    return errors


def _inline_path(value: str, read_file: Callable[[str], str]) -> str:
    """Return *value* unchanged, or the file it names when it looks like a prompt path.

    "Looks like a prompt path" is exactly what the contract says: a relative path ending
    in `.md` or `.txt`. Anything else -- including literal instruction text that happens
    to end in one of those two suffixes -- is inlined too, because the grammar is
    suffix-based by design and there is no separate "this is literal text" marker.
    """
    if not (value.endswith(".md") or value.endswith(".txt")):
        return value
    if value.startswith("/"):
        raise ManifestError(["absolute paths are refused; use a path relative to the manifest"])
    if any(part == ".." for part in PurePosixPath(value).parts):
        raise ManifestError(["path traversal with '..' is refused"])
    return read_file(value)


def compile(manifest: dict, read_file: Callable[[str], str]) -> dict:
    """Normalize *manifest*, inline its prompt/instructions files, and re-validate.

    `read_file` is injected so the CLI can resolve paths relative to the manifest file
    while tests can hand in an in-memory map; this module never touches the filesystem.
    """
    errors = validate(manifest)
    if errors:
        raise ManifestError(errors)

    compiled = json.loads(json.dumps(manifest))
    compiled["apiVersion"] = API_VERSION

    roles = compiled.get("roles") or []
    compiled["default_role"] = compiled.get("default_role") or (roles[0] if roles else None)

    plans = compiled.get("plans") or {}
    plan_keys = list(plans.keys())
    compiled["default_plan"] = compiled.get("default_plan") or (plan_keys[0] if plan_keys else None)

    compiled.setdefault("entities", {})
    compiled.setdefault("permissions", {})
    compiled.setdefault("secrets", [])

    for capability in compiled.get("capabilities") or []:
        capability.setdefault("pin", None)

    inline_errors: list[str] = []
    for name, agent in (compiled.get("agents") or {}).items():
        agent.setdefault("skills", [])
        agent.setdefault("guardrails", [])
        agent.setdefault("requires_feature", None)
        agent.setdefault("scope", None)
        try:
            agent["instructions"] = _inline_path(agent.get("instructions", ""), read_file)
        except ManifestError as exc:
            inline_errors.append(f"/agents/{name}/instructions: {exc.errors[0]}")

    for name, flow in (compiled.get("flows") or {}).items():
        flow.setdefault("requires_feature", None)
        try:
            flow["prompt"] = _inline_path(flow.get("prompt", ""), read_file)
        except ManifestError as exc:
            inline_errors.append(f"/flows/{name}/prompt: {exc.errors[0]}")

    if inline_errors:
        raise ManifestError(inline_errors)

    compile_errors = validate(compiled)
    if compile_errors:
        raise ManifestError(compile_errors)
    return compiled


def canonical_json(obj: Any) -> str:
    """The exact serialization contract section 3.5 hashes for the plan digest."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_digest(compiled: dict) -> str:
    """`sha256(canonical_json(compiled))`: a stable fingerprint of the compiled config.

    `hull plan`/`hull apply` print the Broker's plan digest, not this one. This is not the Broker's plan digest (contract 3.5 hashes `{"config", "base"}`
    together, which needs the active version number only the server has); it is the
    digest of the compiled config alone, useful for the CLI to show the caller a stable
    fingerprint of what it is about to send.
    """
    return hashlib.sha256(canonical_json(compiled).encode("utf-8")).hexdigest()


JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://hullwork.github.io/site/hull-manifest.schema.json",
    "title": "Hull app manifest",
    "description": (
        "Structural shape of a Hull manifest (contract section 3.6). Cross-references "
        "(an entity permission naming a declared entity, a capability tool reference "
        "resolving to a tool that capability lists, the run_as: app + risk: read rule, "
        "the public-flow-vs-capabilities rule, and the per-agent tool count limit) are "
        "not expressible in JSON Schema and are enforced by sites.manifest.validate "
        "instead; this schema only rejects what a schema validator alone can catch."
    ),
    "type": "object",
    "required": ["app", "roles", "plans", "agents", "flows"],
    "properties": {
        "apiVersion": {"const": "hull/v1alpha1"},
        "app": {"type": "string", "pattern": "^[a-z][a-z0-9_-]{0,62}$"},
        "auth": {
            "type": "object",
            "properties": {
                "methods": {"type": "array", "items": {"type": "string"}},
                "registration": {
                    "type": "string",
                    "enum": ["open", "invite", "domain_allowlist", "closed"],
                },
                "visibility": {
                    "type": "string",
                    "enum": ["public", "login_required", "org_members"],
                },
                "organizations": {"type": "boolean"},
            },
            "additionalProperties": True,
        },
        "roles": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string", "pattern": "^[a-z][a-z0-9_-]{0,62}$"},
        },
        "default_role": {"type": "string"},
        "plans": {
            "type": "object",
            "minProperties": 1,
            "propertyNames": {"pattern": "^[a-z][a-z0-9_-]{0,62}$"},
            "additionalProperties": {
                "type": "object",
                "properties": {
                    "features": {"type": "array", "items": {"type": "string"}},
                    "limits": {
                        "type": "object",
                        "additionalProperties": {
                            "type": ["integer", "null"],
                            "exclusiveMinimum": 0,
                        },
                    },
                },
            },
        },
        "default_plan": {"type": "string"},
        "entities": {
            "type": "object",
            "propertyNames": {"pattern": "^[a-z_][a-z0-9_]{0,62}$"},
            "additionalProperties": {
                "type": "object",
                "required": ["table", "primary_key", "columns"],
                "properties": {
                    "table": {"type": "string", "pattern": "^[a-z_][a-z0-9_]{0,62}$"},
                    "primary_key": {"type": "string", "pattern": "^[a-z_][a-z0-9_]{0,62}$"},
                    "columns": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "pattern": "^[a-z_][a-z0-9_]{0,62}$"},
                    },
                },
            },
        },
        "permissions": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "additionalProperties": {
                    "type": "object",
                    "required": ["actions"],
                    "properties": {
                        "actions": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "type": "string",
                                "enum": ["read", "create", "update", "delete"],
                            },
                        }
                    },
                },
            },
        },
        "secrets": {
            "type": "array",
            "items": {"type": "string", "pattern": "^[A-Z][A-Z0-9_]*$"},
        },
        "capabilities": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["alias", "kind", "url", "tools", "credential", "risk"],
                "properties": {
                    "alias": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,31}$"},
                    "kind": {"type": "string", "enum": ["mcp"]},
                    "url": {"type": "string", "pattern": "^https?://"},
                    "tools": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,31}$"},
                    },
                    "credential": {"type": "string", "enum": ["app", "end_user"]},
                    "risk": {"type": "string", "enum": ["read", "write", "destructive"]},
                    "pin": {
                        "type": ["string", "null"],
                        "pattern": "^sha256:[0-9a-f]{64}$",
                    },
                },
            },
        },
        "agents": {
            "type": "object",
            "propertyNames": {"pattern": "^[a-z][a-z0-9_-]{0,62}$"},
            "additionalProperties": {
                "type": "object",
                "required": ["instructions", "model_tier", "tools", "run_as", "budget"],
                "properties": {
                    "instructions": {"type": "string", "minLength": 1},
                    "scope": {"type": ["string", "null"]},
                    "guardrails": {"type": "array", "items": {"type": "string"}},
                    "model_tier": {"type": "string", "minLength": 1},
                    "skills": {"type": "array", "items": {"type": "string"}},
                    "tools": {"type": "array", "items": {"type": "string"}},
                    "run_as": {"type": "string", "enum": ["end_user", "app"]},
                    "requires_feature": {"type": ["string", "null"]},
                    "budget": {
                        "type": "object",
                        "required": ["max_credits_per_run"],
                        "properties": {
                            "max_credits_per_run": {"type": "integer", "exclusiveMinimum": 0}
                        },
                    },
                },
            },
        },
        "flows": {
            "type": "object",
            "propertyNames": {"pattern": "^[a-z][a-z0-9_-]{0,62}$"},
            "additionalProperties": {
                "type": "object",
                "required": [
                    "mode",
                    "model_tier",
                    "prompt",
                    "fallback",
                    "audience",
                    "budget",
                ],
                "properties": {
                    "mode": {"const": "llm"},
                    "model_tier": {"type": "string", "minLength": 1},
                    "prompt": {"type": "string", "minLength": 1},
                    "input_schema": {"type": "object"},
                    "output_schema": {"type": "object"},
                    "fallback": {},
                    "audience": {"type": "string", "enum": ["server", "public"]},
                    "requires_feature": {"type": ["string", "null"]},
                    "budget": {
                        "type": "object",
                        "required": ["max_credits_per_call"],
                        "properties": {
                            "max_credits_per_call": {"type": "integer", "exclusiveMinimum": 0}
                        },
                    },
                },
            },
        },
    },
}
