# Hull runtime vertical slice — cross-partition contract

This file is the single source of truth shared by every partition of this iteration.
If your code and this file disagree, this file wins; if this file is wrong, stop and
report it to the lead instead of silently deviating.

Design reference (Chinese, read for intent, not for field names):
`/home/youziba/hullwork-design/hullwork-ai-saas-平台方案.md`.

## 0. Scope of this iteration

Implement the vertical slice from the design (section 九.1) plus the foundation items it
needs. Out of scope for this iteration: billing/Stripe, automations/webhooks, analytics,
interop (A2A/MCP exposure), visual editing, multi-person task board, sandbox changes.

| Partition | Repo / branch | Owns |
|---|---|---|
| A | agent, `feat/ai-saas-slice/caller-tools` | AG-UI caller-provided tools, `runtime` agent profile, deployment switch, project pseudonym derivation + vectors |
| B | site, `feat/ai-saas-slice/control-plane` | K8s wiring for Broker (per-site ServiceAccount + projected token, NetworkPolicies, HTTPRoute rules, ReferenceGrant, env injection), Broker Deployments in the chart, `expectedRevision`, site DB role limits |
| C | site, `feat/ai-saas-slice/broker-core` | `src/sites/broker/` core: db + migrations, keys/tokens, config store + secrets, app identity, end-user auth, HTTP gateway server, admin API |
| D | site, `feat/ai-saas-slice/broker-runtime` | `src/sites/broker/` runtime: ledger, LLM client, flows, tool gate + executors (entity, MCP), agent bridge, runtime HTTP handlers |
| E | site, `feat/ai-saas-slice/sdk-manifest` | `src/sites/manifest.py`, manifest JSON Schema, `hull` CLI (plan/apply/dev-token), TypeScript SDK under `sdk/ts/` |

File ownership inside `src/sites/broker/` (no partition edits another's files):
- C: `__init__.py`, `db.py`, `tokens.py`, `secretbox.py`, `config_store.py`, `identity.py`, `auth.py`, `mailer.py`, `http.py`, `server.py`, `admin_api.py`, `__main__.py`, `worker.py`, `ingest.py`
- D: `ledger.py`, `llm.py`, `jsonschema_lite.py`, `flows.py`, `tools.py`, `mcp_client.py`, `entities.py`, `agent_bridge.py`, `runtime_api.py`
- Tests: `tests/test_broker_<module>.py`, each partition only its own modules.

## 1. Identifiers

- `appEnvId`: 32 lowercase hex. It is the project-environment pseudonym and equals the Sites
  `X-Acting-Subject` used for that environment. Regex `^[0-9a-f]{32}$`.
- Derivation (partition A implements, others only validate the format):
  `HMAC-SHA256(salt, "project\0" + tenant_id + "\0" + project_id + "\0" + env).digest()[:16].hex()`
  where `env` is `dev` or `prod`, `salt` is the same `AGENT_ACTING_SUBJECT_SALT`. The personal
  pseudonym input is `tenant_id + "\0" + subject_id`; the `"project\0"` prefix is the domain
  separator. Vectors go into `docs/acting-subject-vectors.json` under a new top-level key
  `project_vectors`, without changing existing keys or `_payload_sha256` semantics for them.
- End-user id: `"u" + 32 lowercase hex` (uuid4 hex). Org id: `"o" + 32 lowercase hex`.
- Agent runtime identity for an end user: tenant `app.<appEnvId>`, user `<endUserId>`.
  For `run_as: app`: user `app`.

## 2. Tokens (EdDSA / Ed25519 compact JWS)

Header: `{"alg":"EdDSA","typ":"JWT","kid":"<kid>"}`. Base64url without padding.
Library: `cryptography` (partition C adds it to site dependencies, pinned to `50.0.1`,
the version agent already locks).

App token claims:
`iss="hull-broker"`, `aud="hull-broker"`, `typ="app"`, `sub=<appEnvId>`, `site=<serviceName>`,
`iat`, `exp`, `jti`. TTL env `HULL_APP_TOKEN_TTL_SECONDS`, default 600 (same as the sandbox
scoped token default).

User token claims:
`iss="hull-broker"`, `aud=<appEnvId>`, `typ="user"`, `sub=<endUserId>`, `sid=<sessionId>`,
`org=<orgId|null>`, `role=<string>`, `plan=<string>`, `env="dev"|"prod"`, `iat`, `exp`.
TTL env `HULL_USER_TOKEN_TTL_SECONDS`, default 600.

JWKS: `GET /_hull/.well-known/jwks.json` →
`{"keys":[{"kty":"OKP","crv":"Ed25519","x":"<b64url>","kid":"<kid>","use":"sig","alg":"EdDSA"}]}`.
Keys load from `HULL_BROKER_SIGNING_KEYS_FILE` (JSON list of `{kid, private_key_pem, active}`);
exactly one active; inactive keys still verify. Verification rejects: bad signature, unknown
kid, wrong `typ`, wrong `aud`, expired (leeway env `HULL_TOKEN_LEEWAY_SECONDS`, default 5, same
as the sandbox ticket clock skew), missing claims.

## 3. HTTP API of the Broker gateway

All paths start with `/_hull/`. JSON bodies, UTF-8. Error envelope (one sentence, no trailing
period joining two sentences — site's refusal-text test applies):
`{"error": "<sentence>", "code": "<snake_case>"}`.
Request body limit: `HULL_MAX_BODY_BYTES`, default 65536 (same as `SITES_REQUEST_MAX_BYTES`).

### 3.1 Caller resolution

- Browser calls on the app domain: app is resolved from the `Host` header via settings `hosts`.
  Unknown host → 404 `unknown_app`.
- Server calls from the app backend: `Authorization: Bearer <appToken>`; the end user, when
  acting for one, in `X-Hull-User: <userToken>` (must have `aud == app token sub`).
- Admin calls: `Authorization: Bearer <HULL_BROKER_ADMIN_TOKEN (file: HULL_BROKER_ADMIN_TOKEN_FILE)>`,
  compared with `hmac.compare_digest`.

### 3.2 End-user auth (browser, same origin) — partition C

| Method, path | Body | Result |
|---|---|---|
| `POST /_hull/auth/email/start` | `{"email"}` | always `202 {"status":"sent"}` (also for closed registration, unknown user, rate-limited): never reveal existence |
| `POST /_hull/auth/email/verify` | `{"email","code"}` | `200 {"user":{"id","email","org","role","plan"},"token","expiresAt"}` + `Set-Cookie: __Host-hull_session=<opaque>; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=<absolute ttl>`; wrong/expired code → `401 invalid_code` |
| `POST /_hull/auth/token` | — (cookie) | `200 {"user","token","expiresAt"}`; no/invalid session → `401 no_session` |
| `POST /_hull/auth/logout` | — (cookie) | `204`, session revoked, cookie cleared |
| `GET /_hull/.well-known/jwks.json` | — | JWKS |

Registration modes from compiled config `auth.registration`: `open`, `invite`,
`domain_allowlist` (settings `allowedEmailDomains`), `closed`. A code is only mailed when the
email may sign in. OTP: 6 digits, stored as HMAC, TTL `HULL_OTP_TTL_SECONDS` (default 600),
max attempts `HULL_OTP_MAX_ATTEMPTS` (default 5), per-email and per-IP send limits configurable.
Session: opaque 32-byte token, only its SHA-256 stored, absolute TTL `HULL_SESSION_TTL_SECONDS`
(default 28800, same as the site console session). Mailer is an interface; the slice ships a
`DatabaseMailer` (rows in `broker_outbound_mail`, readable through the admin API) and tests use
an in-memory one.

### 3.3 App identity — partition C

| Method, path | Body | Result |
|---|---|---|
| `POST /_hull/v1/app-token` | `{"serviceAccountToken"}` | `200 {"token","expiresAt"}`; `401 invalid_workload` |

Validation goes through a `TokenReviewer` interface: K8s `TokenReview` with
`audiences=["hull-broker"]` using the in-cluster service account (partition B grants the RBAC);
the authenticated `system:serviceaccount:<ns>:<sa>` is mapped to an app through settings
`workloads: [{"namespace","serviceAccount","site"}]`. Tests use a fake reviewer.

### 3.4 Runtime API — partition D (handlers), partition C (routing, auth)

All require an app token. `X-Hull-User` is required unless the target runs `run_as: app`.
Idempotency: `Idempotency-Key` header (≤ 128 chars) on POSTs that spend credits.

| Method, path | Body | Result |
|---|---|---|
| `POST /_hull/v1/flows/{name}/run` | `{"input": <json>}` | `200 {"output": <json>, "fallback": false, "usage": {"credits": int}}`; on fallback `200 {"output": <fallback>, "fallback": true, "reason": "disabled"\|"ai_disabled"\|"budget_exhausted"\|"entitlement"\|"invalid_output"\|"upstream_error", "usage": {...}}`; unknown flow `404 unknown_flow`; input fails schema `400 invalid_input` |
| `POST /_hull/v1/agents/{name}/runs` | `{"threadId"?: str, "input": str}` | `200 text/event-stream`, events below; unknown agent `404`; entitlement `403 entitlement_required`; budget `402 budget_exhausted` |
| `POST /_hull/v1/agents/{name}/runs/{runId}/cancel` | — | `202` |
| `POST /_hull/v1/tools/{alias}/call` | `{"tool": str, "arguments": {}}` | `200 {"content": <json\|str>, "isError": bool}`; gate refusal `403 {"code": "tool_denied", "reason": ...}` |
| `GET /_hull/v1/usage/me` | — | `200 {"period": "YYYY-MM", "used": int, "limit": int\|null}` for the user in `X-Hull-User` |

Agent run SSE events (`event: <name>\ndata: <json>\n\n`):
`run.started {runId, threadId}`, `message.delta {text}`, `tool.started {callId, tool}`,
`tool.finished {callId, tool, ok}`, `approval.pending {approvalId, tool}`,
`run.finished {usage: {credits}}`, `run.error {code, message}`.

### 3.5 Admin API — partition C

| Method, path | Body | Result |
|---|---|---|
| `POST /_hull/admin/v1/apps/{appEnvId}/config/plan` | `{"config": <compiled>}` | `200 {"digest", "activeVersion": int\|null, "changes": [{"path","op"}]}`; invalid → `400 invalid_config {"errors": [...]}` |
| `POST /_hull/admin/v1/apps/{appEnvId}/config/apply` | `{"config", "planDigest"}` | `200 {"version", "digest"}`; digest mismatch → `409 plan_stale` |
| `GET /_hull/admin/v1/apps/{appEnvId}/config` | — | `200 {"version","digest","config"}` or `404` |
| `PUT /_hull/admin/v1/apps/{appEnvId}/settings` | settings object (3.7), merge-replace at top level | `200 {settings without secrets}` |
| `PUT /_hull/admin/v1/apps/{appEnvId}/secrets/{name}` | `{"value": str}` | `204`; values are never returned by any endpoint |
| `POST /_hull/admin/v1/apps/{appEnvId}/dev-app-token` | `{"site"?}` | `200 {"token","expiresAt"}` — only when `HULL_BROKER_DEV_TOKENS=1` |
| `PUT /_hull/admin/v1/apps/{appEnvId}/users/{userId}/plan` | `{"plan"}` | `200` |
| `GET /_hull/admin/v1/apps/{appEnvId}/usage?since=YYYY-MM-DD` | — | `200 {"rows":[{"userId","credits","calls"}]}` (partition D provides the query, C the route) |
| `GET /_hull/admin/v1/apps/{appEnvId}/mail` | — | `200 {"rows":[...]}` dev only (`HULL_BROKER_DEV_TOKENS=1`) |

Plan digest = `sha256(canonical_json({"config": compiled, "base": activeVersion}))` where
canonical JSON is `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`.
`manifest.canonical_digest(compiled)` (partition E) computes `sha256(canonical_json(compiled))`
and is what the CLI prints; the Broker computes the plan digest itself.

### 3.6 Compiled config (produced by `manifest.compile`, stored by C, consumed by C and D)

It is the normalized manifest with prompt files inlined and defaults filled. Keys are the
manifest keys (snake_case). `apiVersion` must be `"hull/v1alpha1"`.

```json
{
  "apiVersion": "hull/v1alpha1",
  "app": "support-desk",
  "auth": {"methods": ["email_otp"], "registration": "open",
           "visibility": "login_required", "organizations": false},
  "roles": ["member", "org_admin"],
  "default_role": "member",
  "plans": {"free": {"features": ["chat"], "limits": {"ai_credits_month": 200}}},
  "default_plan": "free",
  "entities": {"tickets": {"table": "tickets", "primary_key": "id",
               "columns": ["id", "title", "status", "owner_id"]}},
  "permissions": {"member": {"tickets": {"actions": ["read"]}}},
  "secrets": ["ZENDESK_TOKEN"],
  "capabilities": [{"alias": "kb", "kind": "mcp", "url": "https://mcp.example.com/mcp",
                    "tools": ["search_docs"], "credential": "app", "risk": "read",
                    "pin": null}],
  "agents": {"triage": {"instructions": "<inlined text>", "scope": "...",
             "guardrails": ["..."], "model_tier": "balanced", "skills": [],
             "tools": ["entity:tickets.read", "kb.search_docs"], "run_as": "end_user",
             "requires_feature": null, "budget": {"max_credits_per_run": 30}}},
  "flows": {"summarize_ticket": {"mode": "llm", "model_tier": "fast",
            "prompt": "<inlined text with {{input.x}} placeholders>",
            "input_schema": {}, "output_schema": {}, "fallback": {"summary": ""},
            "audience": "server", "requires_feature": null,
            "budget": {"max_credits_per_call": 10}}}
}
```

Rules the validator enforces (E) and the Broker re-checks (C on apply):
- `risk` ∈ `read|write|destructive`; `credential` ∈ `app|end_user`; `run_as` ∈ `end_user|app`;
  `mode` is `llm` only in this slice; `audience` ∈ `server|public`.
- Agent tool references: `entity:<entity>.read` or `<capability alias>.<tool>`; the tool
  must be listed in that capability's `tools`. Entity writes are out of scope.
- `run_as: app` agents may only reference `risk: read` capabilities and entity reads.
- `audience: public` flows cannot exist together with capabilities (flows have no tools in
  this slice, so only the rule is recorded).
- Every agent must have `budget.max_credits_per_run`; every flow `budget.max_credits_per_call`
  (a missing budget is a validation error, not a default).
- Per-agent tool count ≤ `max_tools_per_agent` (validator option, default 10, the upper end
  of the range Retool's docs give).
- `model_tier` must be a string; mapping to a model is a setting (3.7), not config.
- Entity/column/table names match `^[a-z_][a-z0-9_]{0,62}$`.

### 3.7 Settings (environment-side, set through admin API, stored by C)

```json
{
  "hosts": ["support-desk.example.test"],
  "workloads": [{"namespace": "...", "serviceAccount": "...", "site": "..."}],
  "ai_enabled": true,
  "allowed_email_domains": [],
  "model_tiers": {"fast": {"model": "glm-5.1", "input_credits_per_1k": 1,
                            "output_credits_per_1k": 2}},
  "flows": {"summarize_ticket": {"enabled": true}},
  "limits": {"app_credits_month": null},
  "database_secret": "database_dsn",
  "capability_secrets": {"kb": "kb_token"}
}
```
Secrets referenced by name are stored with `PUT .../secrets/{name}` and encrypted at rest
(AES-GCM, key from `HULL_BROKER_SECRET_KEY_FILE`, 32 bytes, AAD = `appEnvId + "\0" + name`).
`capability_secrets[alias]` holds a bearer token sent as `Authorization: Bearer` to the MCP
server when `credential: app`. `database_secret` holds a libpq DSN for the app schema runtime
role. Credit numbers in `model_tiers` are pricing configuration, not defaults.

## 4. Python interfaces between C and D (site repo, `src/sites/broker/`)

C provides (D imports, never edits):

```python
# db.py
class BrokerDatabase:
    @classmethod
    def from_env(cls) -> "BrokerDatabase": ...          # HULL_BROKER_DB_* like SITES_DB_*
    def transaction(self) -> ContextManager[Cursor]: ... # one connection, commit/rollback
def register_migration(component: str, version: int, description: str,
                       apply: Callable[[Cursor], None]) -> None: ...
# ledger table: broker_schema_migrations(component, version, description, applied_at),
# versions strictly +1 per component; C uses component "core", D uses "runtime".
def migrate(db: BrokerDatabase) -> None: ...

# config_store.py
@dataclass(frozen=True)
class AppRuntime:
    app_env_id: str
    version: int
    digest: str
    config: dict        # compiled config (3.6)
    settings: dict      # settings (3.7), secrets excluded
class ConfigStore:
    def active(self, app_env_id: str) -> AppRuntime | None: ...   # cached, TTL env HULL_CONFIG_CACHE_SECONDS
    def by_host(self, host: str) -> str | None: ...
    def secret(self, app_env_id: str, name: str) -> str | None: ...

# identity.py / auth.py
@dataclass(frozen=True)
class AppCaller:
    app_env_id: str
    site: str | None
@dataclass(frozen=True)
class EndUser:
    id: str
    org: str | None
    role: str
    plan: str
    env: str

# http.py
@dataclass
class Request:
    method: str; path: str; params: dict[str, str]; query: dict[str, str]
    headers: Mapping[str, str]; body: Any; host: str; client_ip: str
    cookies: dict[str, str]
    app: AppCaller | None          # set when an app token was presented and valid
    user: EndUser | None           # set when X-Hull-User was presented and valid
    runtime: AppRuntime | None     # active runtime for request.app
@dataclass
class Response:
    status: int; body: Any = None; headers: dict[str, str] = field(default_factory=dict)
@dataclass
class StreamResponse:
    events: Iterator[tuple[str, dict]]   # (event name, data); server writes SSE
    headers: dict[str, str] = field(default_factory=dict)
class Router:
    def add(self, method: str, pattern: str, handler: Callable[[Request], Response | StreamResponse],
            *, auth: str) -> None: ...   # auth ∈ "none"|"app"|"app+user"|"app+optional_user"|"admin"
class HttpError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra): ...

@dataclass
class Services:          # constructed once by server.py, passed to D's register()
    db: BrokerDatabase
    config: ConfigStore
    keys: "KeyRing"
    clock: Callable[[], float]
```

D provides (C's `server.py` imports it; C must not edit it):

```python
# runtime_api.py
def register(router: Router, services: Services) -> None: ...
def admin_usage(services: Services, app_env_id: str, since: date) -> list[dict]: ...
```

C's `server.py` must import `runtime_api` lazily inside a function so C's branch runs its own
tests before D's module exists (skip registration only when `ModuleNotFoundError` names
`sites.broker.runtime_api`).

## 5. Agent runtime extension — partition A implements, partition D consumes

Enabled only when `AGENT_CALLER_TOOLS=1`. When disabled, a request carrying caller tools or
`forwardedProps.agent == "runtime"` gets `400` and behaviour is otherwise unchanged.

Request (`POST /api/ag-ui`, trusted-proxy identity only; other identities → `403`):

```json
{
  "threadId": "...", "runId": "...",
  "messages": [{"id": "...", "role": "user", "content": "..."}],
  "tools": [{"name": "kb__search_docs", "description": "...", "parameters": {"type": "object"}}],
  "forwardedProps": {"agent": "runtime", "systemPrompt": "...", "model": "glm-5.1"}
}
```

- `agent: "runtime"` selects a built-in profile whose system prompt is `systemPrompt`
  (required, ≤ 20000 chars, the existing custom-agent system limit), with no sandbox, no
  connector, no scheduling, no sub-agent tools. Caller tools are only accepted with this
  profile.
- Tool name regex `^[A-Za-z][A-Za-z0-9_]{0,63}$`; must not collide with any built-in tool
  name; count limit `AGENT_CALLER_TOOLS_MAX` (default 16, equal to `RunBudget.max_agents`'
  default only by coincidence — document it as provisional and configurable); `parameters`
  JSON ≤ 16 KiB each.
- When the model calls a caller tool, the stream carries the normal `TOOL_CALL_START/ARGS/END`
  events and then
  `{"type":"CUSTOM","name":"hull/caller_tool_call","value":{"requestId","toolCallId","name","arguments"}}`.
  The run waits.
- The caller answers `POST /api/sessions/{threadId}/tool-results`
  `{"requestId", "content": str, "isError": bool}` with the same trusted-proxy identity →
  `202`; unknown or expired → `404`; already answered → `409`; content over
  `AGENT_CALLER_TOOL_MAX_RESULT_CHARS` (default 20000, the connector `max_result_chars`
  default) → `413`. It must work when the answer lands on another replica (reuse the HITL
  wait: in-process event plus database polling).
- Timeout `AGENT_CALLER_TOOL_TIMEOUT_SECONDS` (default 300, the existing HITL timeout); on
  timeout the model receives an error result and the run continues.
- Usage is already emitted as `CUSTOM` name `agent/event` with `value.type == "usage"` and
  fields `input`, `output`, `cached`, `cache_write`, `model`; D reads that. Cancellation uses
  the existing `POST /api/sessions/{id}/cancel`.
- Headers D sends: `X-Agent-Proxy-Secret`, `X-Agent-Tenant: app.<appEnvId>`,
  `X-Agent-User: <endUserId>|app`.

## 6. Kubernetes wiring — partition B

- Per SiteDeployment: a ServiceAccount named after the CR name (`cr_name`), no RBAC, and a
  projected token volume `hull-token` mounted at `/var/run/hull/token` with
  `audience: hull-broker`, `expirationSeconds` from `SITES_HULL_TOKEN_EXPIRATION_SECONDS`
  (default 3600; Kubernetes minimum is 600). `automountServiceAccountToken` stays false.
- Env injected when the Broker is enabled (`SITES_HULL_BROKER_ENABLED=1`):
  `HULL_BROKER_URL` (cluster URL of the gateway Service), `HULL_TOKEN_FILE=/var/run/hull/token`,
  `HULL_APP_ENV_ID=<the tenant's acting subject when it is 32 hex, else empty>`,
  `HULL_SITE=<serviceName>`.
- NetworkPolicy: tenant workload egress to the Broker gateway pods on its port (copy the
  existing `sites-postgres` rule); chart NetworkPolicy for the Broker allowing ingress from
  tenant namespaces (`sites.local/managed=true`) and from the Envoy data plane.
- Gateway exposure: HTTPRoute rules for `PathPrefix /_hull/`,
  `Exact /.well-known/agent-card.json`, `Exact /.well-known/oauth-protected-resource`
  pointing to the Broker ingress Service in the control namespace, plus the ReferenceGrant
  maintained by the operator (copy the activator pattern). Only with the gateway backend.
- Chart: Deployments `hull-broker-gateway`, `hull-broker-worker`, `hull-broker-ingest`
  (command `python3 -m sites.broker <role>`), Services, RBAC allowing `tokenreviews` create
  for the gateway ServiceAccount, all behind a `broker.enabled` value defaulting to false.
- `POST /v1/deployments` accepts `expectedRevision` (string). Present and not equal to the
  current CR `spec.revision` → `409 {"code": "revision_conflict"}`; absent → today's behaviour.
- Site DB roles get `CONNECTION LIMIT` from `SITES_DB_ROLE_CONNECTION_LIMIT` and
  `statement_timeout` from `SITES_DB_ROLE_STATEMENT_TIMEOUT_MS`; unset → no limit (today's
  behaviour).

## 7. Rules for every partition

- Match the surrounding code style; comments explain why, not what; no emoji.
- Every new config key goes into `docs/CONFIGURATION.md` (site) with its default and source.
- New test modules: agent needs them listed in both `.github/workflows/ci.yml` and
  `release.yml`; site tests are discovered automatically.
- Guards get a mutation check: remove the guard, the test must fail; restore it.
- Do not push. Commit on your branch with headers `type(scope): description`, type in
  feat|fix|refactor|perf|docs|test|chore|build|ci|style|revert, and end the message with
  `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`.
