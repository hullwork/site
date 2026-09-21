# Hull manifest and CLI

`hull.yaml` (or `hull.json`) is the tenant-authored declaration of a Hull app: roles,
plans, entities, permissions, capabilities, agents, and flows. This document is the
reference for the manifest shape, the `hull` CLI that turns it into a Broker config, and
the tool-reference-to-AG-UI-tool-name mapping other partitions consume. The source of
truth for every rule below is `CONTRACT.md` section 3.6; this file explains it, the
contract wins if the two disagree.

## Shape

```yaml
apiVersion: hull/v1alpha1   # optional on input; compile always writes this value
app: support-desk
auth:
  methods: [email_otp]
  registration: open            # open | invite | domain_allowlist | closed
  visibility: login_required    # public | login_required | org_members
  organizations: false
roles: [member, org_admin]
default_role: member            # optional; defaults to the first role
plans:
  free: { features: [chat], limits: { ai_credits_month: 200 } }
default_plan: free              # optional; defaults to the first plan
entities:
  tickets:
    table: tickets
    primary_key: id
    columns: [id, title, status, owner_id]
permissions:
  member:
    tickets: { actions: [read] }
secrets: [ZENDESK_TOKEN]        # names only; values are set through the admin API
capabilities:
  - alias: kb
    kind: mcp                   # only "mcp" in this slice
    url: https://mcp.example.com/mcp
    tools: [search_docs]
    credential: app              # app | end_user
    risk: read                   # read | write | destructive
    pin: null                    # optional; sha256:<64 hex> content pin
agents:
  triage:
    instructions: prompts/triage.md   # a relative .md/.txt path is inlined; anything else is used literally
    scope: classify tickets and suggest a reply
    guardrails: [never promise a refund]
    model_tier: balanced
    skills: []
    tools: [entity:tickets.read, kb.search_docs]
    run_as: end_user              # end_user | app
    requires_feature: null
    budget: { max_credits_per_run: 30 }   # required; a missing budget is a validation error
flows:
  summarize_ticket:
    mode: llm                     # only "llm" in this slice
    model_tier: fast
    prompt: prompts/summarize.md
    input_schema: {}
    output_schema: {}
    fallback: { summary: "" }
    audience: server               # server | public
    requires_feature: null
    budget: { max_credits_per_call: 10 }  # required
```

## Validation rules (`sites.manifest.validate`)

`validate(manifest, *, max_tools_per_agent=10) -> list[str]` returns one sentence per
problem, each prefixed with a JSON-pointer-like path (for example
`/agents/triage/budget/max_credits_per_run: must be present and a positive integer...`).
An empty list means the manifest is valid. In addition to the enums and cross-references
contract section 3.6 states directly (`risk`, `credential`, `run_as`, `mode: llm` only,
`audience`, the entity/table/column grammar `^[a-z_][a-z0-9_]{0,62}$`, mandatory budgets,
the per-agent tool count limit, and the tool-reference grammar), this module enforces the
structural shape a manifest needs before it can be compiled at all: required top-level
keys, list/object types, non-empty `roles`/`plans`, and that `default_role`/`default_plan`
name something declared. These structural checks are not spelled out field-by-field in the
contract, but `compile` cannot fill defaults or inline prompts from a manifest whose shape
it cannot trust, so they are enforced here rather than left to crash later.

Two rules worth calling out because they are inferred, not literal contract text:

- **Capability `kind` is restricted to `"mcp"`.** The design doc shows a `connector` kind
  (e.g. a Stripe refund tool), but billing/Stripe is explicitly out of scope for this
  iteration (contract section 0) and partition D's file list has no generic connector
  client, only `mcp_client.py`. A manifest naming any other kind is refused.
- **App/agent/flow/role/plan names use `^[a-z][a-z0-9_-]{0,62}$`.** The contract does not
  give this grammar explicitly, but these names become URL path segments (`/_hull/v1/
  agents/{name}/runs`) and JSON-pointer path components in validator output, so they need
  a safe character set; the pattern is loose enough to accept both of the contract's own
  examples (`support-desk`, `summarize_ticket`).

Capability aliases and their tool names use a tighter grammar,
`^[a-z][a-z0-9_]{0,31}$` (no hyphens, max 32 characters), because `agent_tool_name` below
concatenates them into a single flat identifier that must itself stay valid and short.

## Compilation (`sites.manifest.compile`)

`compile(manifest, read_file) -> dict` validates, fills defaults (`apiVersion`,
`default_role`, `default_plan`, capability `pin: null`, agent `skills: []` /
`guardrails: []` / `requires_feature: null` / `scope: null`, flow `requires_feature:
null`), inlines `instructions`/`prompt` when the value is a relative path ending in `.md`
or `.txt` (anything else, including literal text that happens to share that suffix, goes
through `read_file` too — the check is suffix-based, as the contract specifies), and
re-validates the result. `read_file` is injected so the CLI can resolve paths relative to
the manifest file while tests can hand in an in-memory map; the module never touches the
filesystem itself.

An absolute path (starting with `/`) or one containing a `..` component is refused before
`read_file` is ever called — both raise `sites.manifest.ManifestError`, the same exception
`validate` failures are wrapped in when raised through `compile`.

## Canonical digest

`canonical_json(obj) -> str` is `json.dumps(obj, sort_keys=True, separators=(",", ":"),
ensure_ascii=False)` (contract section 3.5, verbatim). `canonical_digest(compiled) -> str`
is `sha256(canonical_json(compiled)).hexdigest()`. This is not the Broker's own plan
digest — the Broker hashes `{"config": compiled, "base": activeVersion}` together, which
needs the active version number only the server has — it is a stable fingerprint of the
compiled config alone that `hull plan`/`hull apply` print so a caller can tell two compiles
of the same manifest apart from two different ones.

## Tool reference grammar and `agent_tool_name`

An agent's `tools` list holds one of two reference shapes:

- `entity:<entity>.read` — the entity must be declared in `entities`; only `read` is
  accepted (entity writes are out of scope this slice).
- `<capability alias>.<tool>` — the alias must name a declared capability, and the tool
  must appear in that capability's own `tools` list.

`sites.manifest.agent_tool_name(ref) -> str` maps either shape to the flat AG-UI tool name
partition D's caller-tool bridge exposes, matching `^[A-Za-z][A-Za-z0-9_]{0,63}$`:

| Reference | AG-UI tool name |
| --- | --- |
| `kb.search_docs` | `kb__search_docs` |
| `entity:tickets.read` | `entity__tickets__read` |

`.` and `:` become `__`. A reference that does not parse, or whose mapped name would not
fit the AG-UI grammar (bad characters, or over 64 characters — possible even when the
alias and tool names each individually pass the 32-character capability-name limit), is
refused rather than silently truncated or reused with a collision, because partition D
looks the flat name up by exact match. `validate` calls `agent_tool_name` on every tool
reference for this reason, so an overlong mapping is caught at manifest-validation time,
not at run time.

## JSON Schema

`sites.manifest.JSON_SCHEMA` is a draft 2020-12 JSON Schema written verbatim (via
`json.dumps(JSON_SCHEMA, indent=2) + "\n"`) to `docs/hull-manifest.schema.json`; a test
(`tests/test_manifest.py::SchemaFileTests`) keeps the file and the Python source in sync.
It mirrors every rule a schema validator alone can express — types, enums, patterns,
required fields. It cannot express cross-references (a permission naming a declared
entity, a capability tool reference resolving to a tool that capability lists, the
`run_as: app` + `risk: read` rule, the public-flow-vs-capabilities rule, or the per-agent
tool count limit): those are enforced by `validate` only. Treat the schema as useful for
editor autocompletion and a first-pass check, not as the full contract.

## The `hull` CLI

`hull` is unrelated to the `sites` CLI (`sites` deploys workloads to the Sites control
plane; `hull` publishes an app manifest to a Broker) and is registered as its own console
script.

| Command | What it does |
| --- | --- |
| `hull validate [path]` | Runs `validate` and prints `{"ok": true}` or the errors |
| `hull compile [path]` | Prints the compiled, prompt-inlined manifest as JSON |
| `hull plan --broker URL --app APPENV [path]` | Compiles, then asks the Broker's admin API what applying would change |
| `hull apply --broker URL --app APPENV [--yes] [path]` | Plans, shows the changes, confirms (`--yes` or an interactive prompt), then applies with the plan digest; a `409` from a concurrent apply prints "someone applied in between, re-run plan" and exits `3` |
| `hull dev-token --broker URL --app APPENV` | Mints a development app token (Broker must have `HULL_BROKER_DEV_TOKENS=1`) |

The manifest path defaults to `./hull.yaml`, falling back to `./hull.json`, if not given
explicitly. YAML parsing only happens when `yaml` (PyYAML, the project's `dev` extra) is
importable; without it, `hull.yaml` fails with a message pointing at `hull.json` instead of
an `ImportError`.

The admin token is never a command-line value — only `--admin-token-file` (or
`$HULL_BROKER_ADMIN_TOKEN_FILE`), a path to a file holding it, is. The token's contents are
never written to stdout or stderr; every error message names files, URLs, and manifest
paths, never the token itself.

Exit codes: `0` success, `1` a broker/network problem (unreachable broker, missing admin
token, interactive confirmation declined or unavailable), `2` an invalid manifest or a
config the Broker's admin API rejected (`400 invalid_config`), `3` a plan-apply conflict
(`409 plan_stale`).
