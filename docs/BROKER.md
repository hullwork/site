# Hull Broker

The Broker is the runtime service a deployed AI SaaS app (built by
`business-app-creator` / the `hull` CLI, contract in `CONTRACT.md`) calls for
sign-in, config, and every model call. It is a data-plane component, deployed
per app environment alongside the app's own site -- nothing on its request path
goes through `sites-api` or the Kubernetes API at request time.

This document covers what partition C built: roles, the request path, failure
behaviour, and the module boundary with partition D. For the wire contract
(paths, bodies, status codes) see `CONTRACT.md` sections 2-4; this file only
explains how C's pieces fit together.

## Roles

`python -m sites.broker <role>`, one process per role, one Deployment per role
in the chart (`hull-broker-gateway`, `hull-broker-worker`, `hull-broker-ingest`,
all behind `broker.enabled`, partition B):

| Role | What it does | Health |
| --- | --- | --- |
| `gateway` | Serves every `/_hull/*` route: end-user auth, app identity, admin API, app-visible config, and (once partition D's `runtime_api` module exists) flows/agents/tools/usage. Runs migrations at startup, guarded by the same advisory lock the worker uses, so N replicas starting together serialize instead of racing. | `/healthz` always 200; `/readyz` red only when PostgreSQL is unreachable (same reasoning as `sites-api`'s `/readyz`: nothing else on this path can be fixed by removing traffic) |
| `worker` | Runs migrations once, then loops: delete expired sessions, OTP codes, and rate-limit buckets (`HULL_BROKER_WORKER_INTERVAL_SECONDS`). No HTTP traffic. | `/healthz` red when the last cycle is older than `HULL_BROKER_WORKER_STALE_SECONDS` -- readiness means "the cleanup loop is still turning", not "the process exists" |
| `ingest` | Reserved for future asynchronous ingestion (webhooks, batch imports -- out of scope per contract section 0). This slice serves only `/healthz`, so the chart can stand the Deployment up now without it doing anything yet. | always 200 |

Every role reads `HULL_BROKER_DB_*` / `HULL_BROKER_SIGNING_KEYS_FILE` /
`HULL_BROKER_SECRET_KEY_FILE` from the same environment (see
`docs/CONFIGURATION.md#hull-broker-core`); only the gateway also needs
`HULL_BROKER_ADMIN_TOKEN(_FILE)` and a `TokenReviewer`.

## Data path

One PostgreSQL database, `component = "core"` in `broker_schema_migrations`
(partition D's tables live in the same database under `component = "runtime"`,
its own version sequence). Every table carries `app_env_id`; `db.py`'s
data-access functions all take it as the first argument after the cursor, which
is the actual isolation mechanism -- there is no row-level security, the
discipline is "every function that reads or writes a row is scoped by
construction, not by remembering a WHERE clause".

```
apps (active_version pointer) ──┬── app_configs (versioned, digest per version)
                                 ├── app_settings (jsonb; hosts/workloads synced out
                                 │   into indexed lookup tables on every write)
                                 ├── app_hosts, app_workloads (indexes for the two
                                 │   resolution paths: Host header, TokenReview)
                                 ├── app_secrets (AES-GCM ciphertext, never plaintext)
                                 ├── end_users (global id, scoped by app_env_id)
                                 ├── sessions (opaque token, only its SHA-256 stored)
                                 ├── otp_codes (one per (app_env_id, email), HMAC'd)
                                 ├── rate_limits (fixed-window counters)
                                 └── outbound_mail (DatabaseMailer's sink)
```

Request path for a browser call: Host header → `ConfigStore.by_host` →
`app_env_id` → `ConfigStore.active` (cached `HULL_CONFIG_CACHE_SECONDS`) →
route handler. For a server call: `Authorization: Bearer <appToken>` →
`identity.verify_app_token` → same `ConfigStore.active` lookup. Both converge
on the same `Request.app` / `Request.runtime` fields before a handler ever
runs (`server.py`'s `_authenticate`), so route handlers never re-implement
caller resolution.

## Failure behaviour

- **Database unavailable**: `/readyz` turns red immediately; every other route
  fails its own DB call and answers `500 internal_error` (caught centrally in
  `server.py`, never a raw traceback to the caller).
- **Signing key file missing or malformed**: the process refuses to start
  (`KeyRing.from_env` raises before `serve_forever`). There is no
  degraded-but-running mode for a broken credential surface.
- **`sites.broker.runtime_api` (partition D) not yet present**: the gateway
  still starts; every D-owned route is simply absent (`404 route_not_found`),
  and `GET .../usage` answers `501 not_available` instead of guessing. This is
  what lets C's own test suite pass before D's branch merges (contract section
  4's `ModuleNotFoundError` carve-out).
- **`sites.manifest` (partition E) not yet present**: `config_store.py` falls
  back to structural-only validation (`apiVersion` and the required top-level
  keys) instead of the full semantic ruleset E owns. `plan`/`apply` both still
  work, just with a smaller error surface until E's module lands.
- **Concurrent `config/apply`**: exactly one caller gets `200`, every other
  concurrent caller with the same `planDigest` gets `409 plan_stale` -- enforced
  with `SELECT ... FOR UPDATE` on the app's row inside the same transaction
  that computes the digest and inserts the new version, not by a
  compare-and-swap the two requests could still both pass.
- **A refused `email/verify`** (wrong code, expired code, rate-limited, no
  active config) always answers `401 invalid_code` with no other information,
  and `email/start` always answers `202 {"status":"sent"}` regardless of
  whether an account exists, registration is closed, or the caller is
  rate-limited -- see the module docstring in `auth.py`.

## Module boundary with partition D

C provides `db.py` (`BrokerDatabase`, `register_migration`, `migrate`),
`config_store.py` (`AppRuntime`, `ConfigStore`), `identity.py` /
`http.py`'s `AppCaller` / `EndUser`, and `http.py`'s `Request` / `Response` /
`StreamResponse` / `Router` / `Services` / `HttpError` -- the full interface is
contract section 4, and C does not change any of those existing names or
signatures.

D provides `runtime_api.register(router, services)` and
`runtime_api.admin_usage(services, app_env_id, since)`; `server.py` imports
both lazily, inside `build_router()`, specifically so C's own test suite runs
green before D's module exists (see "Failure behaviour" above). `Services` also
carries a few C-only fields beyond the four the contract promises D (`auth`,
`mailer`, `reviewer`, `admin_token`, `dev_tokens_enabled`) -- D's `register()`
must not rely on those; they exist so C's own route modules can share one
`Services` instance.
