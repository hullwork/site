# Hull Broker: runtime (partition D)

This document covers `src/sites/broker/{ledger,llm,jsonschema_lite,flows,tools,
mcp_client,entities,agent_bridge,runtime_api}.py` — the half of the Broker that
serves `POST /_hull/v1/flows/{name}/run`, `POST /_hull/v1/agents/{name}/runs`
(and its cancel route), `POST /_hull/v1/tools/{alias}/call`, and
`GET /_hull/v1/usage/me`. The cross-partition contract is
`docs/HULL_RUNTIME_CONTRACT.md`; this document is the implementation notes for the
D side of it — read the contract first, this second.

## Module map

| Module | Owns |
| --- | --- |
| `ledger.py` | Double-entry credit reservation/settlement/release, per-period usage, admin usage rows |
| `llm.py` | `LLMClient` protocol, the OpenAI-chat-compatible HTTP client, `FakeLLM`, tier → model/pricing |
| `jsonschema_lite.py` | The JSON Schema subset flows/tools validate against, plus `supported()` for config-time rejection |
| `flows.py` | `POST /_hull/v1/flows/{name}/run`: validate → gate → render → call → validate → settle |
| `tools.py` | Tool registry, the five-check gate, entity/capability executors, `POST /_hull/v1/tools/{alias}/call`, `GET /_hull/v1/usage/me` |
| `mcp_client.py` | SSRF-defended streamable-HTTP JSON-RPC client for capability tool calls |
| `entities.py` | The bounded, RLS-respecting `SELECT` executor for `entity:<name>.read` |
| `agent_bridge.py` | `POST /_hull/v1/agents/{name}/runs` (SSE) and its cancel route; bridges to the agent runtime's caller-tools extension |
| `runtime_api.py` | `register(router, services)` and `admin_usage(...)` — the two functions contract §4 says C imports |

## The credit ledger

Three per-(app, subject, period) buckets, maintained as columns for atomic
conditional updates and mirrored into an append-only `broker_credit_entries` audit
trail: `available` (implicit, `limit - reserved - settled`), `reserved` (an
in-flight hold), `settled` (actually spent). `reserve` debits `available` and
credits `reserved` with one row-locked, conditional `UPDATE ... WHERE reserved +
settled + amount <= limit`; `settle` releases the hold and credits `settled` with
the (limit-clamped) actual amount, returning the unused hold to `available`;
`release` reverses a reservation outright. Concurrency correctness — two
reservations racing for the last credits, exactly one wins — falls out of
PostgreSQL's row lock on the `broker_credit_periods` row: the loser's `UPDATE`
matches zero rows and its whole transaction rolls back, so nothing needs manual
compensation. `tests/test_broker_ledger.py` proves this with real threads against
real PostgreSQL, not a mock.

A reservation can hold two subjects at once — the resolved user-or-org subject and
the app subject — both checked inside the same transaction, so a call that passes
the generous per-user limit but blows the app-wide cap rolls back cleanly.

Idempotency (`Idempotency-Key`, contract §3.4) is scoped per `(app_env_id, scope,
idempotency_key)`, where `scope` is `flow:<name>` or `agent:<name>` — the same key
reused across two different flows does not collide. A replayed key returns the
stored reservation; if it was already settled, the caller gets the stored response
back verbatim with no new charge.

In-memory segment pre-allocation (claiming a batch of credits once per process and
decrementing locally, to cut the per-call round trip) is called out in the design
as a later optimization. It is **not implemented** — every reservation round-trips
to PostgreSQL. The seam is documented in `ledger.py`'s module docstring.

## The five-check gate (`tools.py`)

`tools.evaluate(ctx, services, ref, arguments)` runs, in this order: **permission**
(role → entity actions from config, or the calling agent's declared tool list for
capabilities) → **entitlement** (`requires_feature` against the user's plan) →
**risk** (`run_as: app` may only use `risk: read`; entity writes are out of scope
this slice regardless of role) → **credits** (does the subject have any headroom
left this period — not a reservation, just a threshold read) → **approval**
(`risk: destructive` records a pending row in `broker_tool_approvals` and refuses
with `approval_required`). `tests/test_broker_tools.py`'s `GateOrderTests` prove the
order itself, not just that each check works, by engineering scenarios where two
checks would both fail and asserting only the earlier one's reason comes back —
and a mutation check (swap two checks, confirm those tests go red, restore) backs
that up.

There is no decide endpoint for a pending approval in this slice — the brief
allows skipping it if time is short, and it was. `broker_tool_approvals` rows are
written and readable, but nothing resolves them yet.

## SSRF defense (`mcp_client.py`)

Every capability URL is one a tenant put in their own config, so it gets the same
treatment BYOK upstreams get in `hullwork/agent` `gateway/gateway.py`: syntax
validation, then `socket.getaddrinfo` resolution where **every** answer must be a
public address (not just the first), then a TCP connection pinned to exactly those
resolved addresses while TLS still verifies the hostname — closing the
DNS-rebinding window between "checked" and "connected". The predicate
(`_is_public_address`) and the pinned-connection classes are a direct port of that
module's `_is_public_address` / `byok_resolve_public` / `_PinnedHTTPSConnection`.

The one addition here: a process-local, `threading.local`-backed test override for
loopback addresses (`mcp_client._allow_loopback_for_tests()`), reachable only by
importing that private context manager directly from a test module — there is no
settings key, env var, or config field that reaches it, so a tenant's compiled
config can never turn it on. Tests that need a real local HTTP fixture use it;
`tests/test_broker_mcp_client.py`'s `SSRFWithoutOverrideTests` prove the override
is off by default, and a mutation check confirms removing the address-class check
entirely turns every SSRF test red.

Pin checking (`capabilities[].pin`, contract §3.6) hashes the exact tool
definition `tools/list` returns (`canonical_tool_digest`, sha256 of canonical
JSON) and refuses the call — not the whole capability — when it does not match a
configured pin. `tests/test_broker_tools.py` proves both directions (mismatch
disables, match allows) against a fake local MCP server, with a mutation check on
the comparison itself.

## Row-level security proof (`entities.py`)

Entity reads set `hull.user_id` / `hull.org_id` / `hull.role` via `set_config(...,
true)` inside the same transaction as the query, so the app's own RLS policies see
them through `current_setting(..., true)`. `tests/test_broker_entities.py` proves
isolation against real PostgreSQL with a throwaway `tickets` table, a real RLS
policy, and — importantly — a **non-superuser, non-owner** role for the query
connection: PostgreSQL lets superusers and table owners bypass RLS even with
`FORCE ROW LEVEL SECURITY`, so testing through a superuser would have proven
nothing. A mutation check (drop the `hull.user_id` `set_config` call) turns the
cross-user isolation tests red.

## Flows (`flows.py`)

`render_prompt` replaces `{{input.a.b}}` with the **JSON encoding** of
`input["a"]["b"]` — literal string substitution, no expression language, so there
is nothing resembling code execution in a caller-supplied template. Every failure
past input-schema validation maps to a `200` fallback response instead of raising;
`FALLBACK_REASONS` enumerates the six the contract defines. `ai_enabled`,
per-flow `enabled`, and `requires_feature` are checked, in that order, before any
credit reservation happens at all, so a disabled flow never touches the ledger.

## Agent bridge (`agent_bridge.py`)

Builds the per-run system prompt (instructions + scope + guardrails; skills are
empty this slice) and the per-user tool list (`tools.would_allow` — permission +
entitlement + risk only, since credits/approval are call-time concerns), calls the
agent runtime's `POST /api/ag-ui` with trusted-proxy headers, and translates its
AG-UI SSE stream into the Broker's six-event vocabulary (contract §3.4). A
`hull/caller_tool_call` custom event runs through the exact same
`tools.evaluate`/`tools.execute` the direct HTTP route uses, and the result is
wrapped as untrusted data (`tools.wrap_untrusted`) before it is POSTed back as the
tool's answer — a model never sees a tool result unmarked as data. Usage is
metered from `agent/event` usage frames using the agent's `model_tier` pricing; the
run is cancelled upstream the moment metered credits would exceed the reservation.
Thread ownership is a `broker_agent_threads` row per thread id, so continuing
someone else's `threadId` is a `403`, proven in
`tests/test_broker_agent_bridge.py` against real PostgreSQL.

Known simplifications, documented in the module docstring: only the synchronous
one-JSON-frame-per-`data:`-line shape of an AG-UI SSE response is read (no
reconnect/replay); a capability tool's `parameters` schema offered to the model is
a generic `{"type": "object"}` rather than fetched from `tools/list` at
tool-list-build time (fetched for real, and pin-checked, only when the tool is
actually called); and the cancel route's run registry is an in-process dict, so a
cancel landing on a different replica than the one streaming the run will not
interrupt it in this slice — the budget-triggered cancellation the brief asks for
does not depend on that registry at all, since it runs inside the same request's
streaming loop.

## Adapters beyond contract §4/§5

The contract intentionally leaves a few things to the implementer; each is called
out in the owning module's docstring, summarized here:

1. `POST /_hull/v1/tools/{alias}/call` accepts both an `entity:<name>` alias
   (routing to the entity executor) and a plain capability alias — the contract
   names the route without saying which.
2. "The agent's tool list" (the permission check for capability tools) is an
   explicit `allowed_tools: frozenset[str] | None` on `tools.GateContext`:
   `agent_bridge.py` passes the running agent's declared tools; the direct HTTP
   route passes `None`, meaning "no agent scoping, only declared-in-config
   matters."
3. The runtime-API auth rule ("`X-Hull-User` required unless `run_as: app`") is
   written for agents/tools, which have `run_as`; flows have `audience` instead.
   The flows route is registered `auth="app+optional_user"`, and `flows.py`
   itself requires a user unless the specific flow's `audience == "public"`.
4. The "credits" gate check for tool calls is a headroom read (`usage < limit`),
   not a reservation — tool calls carry no `usage` field in their response
   (contract §3.4), so there is nothing to charge, and reservations stay the job
   of flows and agent runs.

## What is not here

- No approval-decide endpoint (see above).
- No in-memory ledger pre-allocation (see above).
- Full SSE resilience for the agent bridge (reconnect, replay) — see above.
- Capability tool JSON Schemas in the model's tool list are generic, not fetched
  ahead of time — see above.
