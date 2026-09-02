# site

[![CI](https://github.com/convee/site/actions/workflows/ci.yml/badge.svg)](https://github.com/convee/site/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**A deployment control plane that lets an AI agent ship a website to Kubernetes — and
then proves the deployment actually serves traffic.**

An agent asks for a deployment over HTTP, CLI, or MCP. The control plane admits it against
a tenant quota, converges it through a Kubernetes custom resource, and — once the workload
is ready — makes a real HTTP request to the resulting address and records the status code
and the SHA-256 of the response body in `status.verification`. "Deployed" is a measurement,
not a claim.

> **Status: alpha.** The reference topology targets local and controlled environments, not
> a public hosting platform. See [Known limitations](#known-limitations) before adopting it
> — that section is deliberately specific.

> **Where this fits.** `site` is one of four independently released repositories.
> [`convee/platform-composition`](https://github.com/convee/platform-composition) is the
> only place that describes all four together: what each one is, where the boundaries
> between them are, and how to install the set on an enterprise cluster. This README does
> not repeat any of that — it is about `site` alone.

## Why this instead of a generic deploy tool

- **Success is evidence, not an exit code.** `status.verification` carries `httpStatus` and
  `bodySha256` from a request the control plane made itself. Redirects are not followed and
  only 2xx counts, because those are the two facts a tenant cannot forge. The evidence is
  bounded on purpose: it proves something at that address returned 2xx with that body
  digest — not that the body is semantically correct, since the tenant produced it.
- **The caller cannot name the tenancy it writes into.** Identity is a `(merchant, tenant)`
  pair decided entirely by the credential. `X-Merchant-ID` and `X-User-ID` are **refused
  with 403**, not ignored, so a misconfigured client fails loudly instead of quietly
  filling a stranger's namespace.
- **The submission surface is small by construction.** Secret *values* are never accepted —
  only references to Secrets an operator already created. Binary build contexts are
  rejected. Static direct deployment takes flat text files only.
- **Rollback needs no state carried backwards.** Automatic schema migration is restricted by
  a sqlglot AST allow-list in [`migrations.py`](src/sites/migrations.py) to
  `CREATE TABLE/INDEX IF NOT EXISTS` and `ADD COLUMN IF NOT EXISTS`; anything that fails to
  parse, or that names another schema, raises. DDL can therefore only ever be additive, so
  an older image still runs against a newer schema.
- **Multi-tenant isolation is derived, not concatenated.** Namespace and resource names come
  from a SHA-256 digest of both identity parts. Concatenation is not injective — merchant
  `a-ub` + tenant `c` and merchant `a` + tenant `b-uc` would collide onto one Namespace, and
  the Namespace *is* the isolation boundary.

## Quickstart

### Build and test it (no Kubernetes cluster needed)

Prerequisites: [uv](https://docs.astral.sh/uv/), Docker, and Python 3.12+. The suite runs in
roughly one to two minutes on a warm checkout.

```bash
git clone https://github.com/convee/site.git
cd site
uv sync --locked --extra dev
make test-db     # starts a throwaway PostgreSQL on 127.0.0.1:55439
make test        # 1002 tests
make test-db-down
```

`make test` runs the same suite CI runs. PostgreSQL is real; Kubernetes is faked by
`FakeDeployKube`, so multi-tenant isolation (`tests/test_tenancy.py`) and scale-to-zero
reconciliation (`tests/test_scale_to_zero.py`) are exercised without a cluster.

Explore the contract without deploying anything:

```bash
uv run --locked sites --help
uv run --locked python scripts/evaluate-scaffolds.py   # non-mutating scaffold evaluation
```

### Install it into a cluster

Requires a working `kubectl` context, plus Helm. This installs into the context `kubectl`
currently points at — check it first.

```bash
kubectl config current-context
make standalone-install    # bootstraps development-only Secrets, installs the Chart
make standalone-smoke      # waits for rollouts, port-forwards, requires /readyz to pass
make standalone-uninstall  # keeps the namespace, Secrets, and PVCs
```

The bootstrap helper generates credentials into a mode-0700 temporary directory and never
writes them into the repository or a values file. **Do not use it for production** — see
[docs/STANDALONE.md](docs/STANDALONE.md) and
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#standalone-helm-installation).

Then point the CLI at it and ask the control plane what it can do, rather than memorizing
limits that change with the deployed version:

```bash
export SITES_URL=http://127.0.0.1:18091   # the client default
sites capabilities
```

## Deployment forms

| Form | Command | Boundary |
|---|---|---|
| Versioned static publish | MCP `deploy_static_versioned` | Nested UTF-8 tree, private S3/OSS, immutable version and rollback |
| Static direct upload | `sites deploy-static --name X --directory ./site` | Flat UTF-8 text files; legacy, no version rollback |
| Existing image | `sites deploy --name X --image Y` | The image must be pullable by the target cluster |
| Multiple components | `sites bundle submit stack.json` | One atomic request; examples in [`deploy-specs/`](deploy-specs/) |
| Source build | `sites build submit --name X --directory ./source` | Bounded UTF-8 tree with a root `Dockerfile`, built by isolated BuildKit to an immutable digest |

Agents should call `scaffolds` over MCP (or `GET /v1/scaffolds`) to pick among the
curated static HTML, Vite, FastAPI/PostgreSQL, and Express/PostgreSQL profiles. That
response runs bounded source, deployment, and shared-schema policy checks and reports a
`contractCheckSuccessRate`. Checks that were not actually executed stay `not-run`, and
`agentEndToEndSuccessRate` stays `null` — the API does not invent an end-to-end number.

**Not provided:** request-level Secrets, custom domains, or general Internet publishing.

## Architecture

One codebase and one image (`sites-control`), split by entry point into three resident
processes plus a build plane:

```text
        CLI / MCP / console
                 │
                 ▼
            sites-api ──── PostgreSQL (control metadata)
                 │
                 ▼
   SiteDeployment / SiteBuild custom resources
                 │
                 ▼
          sites-operator ──── Kubernetes workloads
                 │
                 └──► verification evidence ──► status.verification

   traffic ──► gateway or NodePort ──► sites-activator ──► workload
```

| Role | Entry point | Responsibility |
|---|---|---|
| sites-api | `python3 -m sites.api` | Authentication, quotas, admission; deployment/bundle/build/tenant APIs; console serving; direct Kubernetes CR reads and writes |
| sites-operator | `python3 -m sites.operator` | Reconciles `SiteDeployment` and `SiteBuild` to the desired state, derives phase from observed state, and re-asserts converged resources on a slower drift-resync sweep (60s default) |
| sites-activator | `python3 -m sites.activator` | Scale-to-zero wake and forwarding: route refresh, waking through the scale subresource, and `/scale-metrics` for KEDA |
| Build plane | [`charts/site/templates/07-build-plane.yaml`](charts/site/templates/07-build-plane.yaml) | sites-builder (rootless BuildKit Jobs), sites-registry, and registry-proxy |
| Console | Included in the sites-api image | React management console served at `/console` |

**What it does not own:** cluster provisioning, custom domains, public ingress, billing, or
organizational RBAC. Cluster start/stop and provider-specific wiring live outside this
repository; `scripts/cluster.sh up|status|verify` is the generic seam, taking only a
kubeconfig, context, values file, and image reference.

`sites-api` uses an in-process admission lock and `sites-operator` has no leader election,
so the reference Deployments intentionally run **one replica each**.

### Module map

`api.py` is the composition root and combines eight endpoint mixins (auth, tenants,
merchants, admin, builds, bundles, deployments, sites).

<details>
<summary>Per-module responsibilities under <code>src/sites/</code></summary>

| File under `src/sites/` | Responsibility |
|---|---|
| `api.py` | Composition root: combines mixins, assembles `serve()`, applies exception mappings, owns metric route templates |
| `api_errors.py` | Ordered exception-to-HTTP mappings shared by mutation endpoints |
| `api_auth.py`, `api_tenants.py`, `api_merchants.py`, `api_admin.py`, `api_builds.py`, `api_bundles.py`, `api_deployments.py`, `api_sites.py` | The endpoint mixins |
| `identity.py` | Pure authentication: `(headers, store, tokens) → Identity or Refusal` |
| `admission.py` | Pure admission, quota, and port-allocation logic plus refusal exceptions |
| `validation.py` | Input validation (`normalize_*`), identity and quota constants, `DEPLOY_FIELDS`, `STATIC_IMAGE` |
| `naming.py` | Name derivation (Namespace and CR names), token minting, digests |
| `k8s_resources.py` | Kubernetes resource builders, cluster topology constants, tenant-quota normalization |
| `operator.py` | Reconcile loop, SiteBuild lifecycle, readiness verification probing, metrics |
| `activator.py` | Route table, wake, request forwarding, admin endpoints (`healthz`, `scale-metrics`) |
| `exposure.py` | Exposure backend policy (`SITES_EXPOSURE_BACKEND=nodeport` or `gateway`) and declarative capability attributes |
| `storage.py` | PostgreSQL control metadata: schema-version tables, ordered migrations, thread-local connection reuse |
| `sync.py` | Kubernetes snapshot synchronization, verified promotion, and rollback decisions |
| `site_database.py`, `migrations.py`, `nl2sql.py` | Per-site schema and roles / bounded additive-DDL validator and executor / read-only SQL validation |
| `builds.py`, `object_storage.py`, `static_artifacts.py` | Source builds and registry client / content-addressed S3/OSS objects / static artifact envelopes |
| `oidc.py`, `console_session.py` | Authorization Code + PKCE provider integration / independently signed console sessions |
| `kube.py` | Thin Kubernetes client over urllib: five verbs, no watch — a polling model |
| `client.py` | The single API egress shared by CLI and MCP: request assembly, auth headers, error translation |
| `cli.py`, `mcp.py` | The `sites` command / the MCP tool surface over stdio |
| `telemetry.py`, `tracing.py`, `monitoring.py`, `grafana_proxy.py` | Structured logs and Prometheus text metrics / optional OTLP-HTTP export / alert-rule definitions / bounded Grafana proxy |
| `serializers.py`, `registry_client.py`, `scaffolds.py`, `version_policy.py`, `safe_stdout.py`, `shutdown.py`, `topology.py` | Response projections, registry client, curated profiles, version policy, non-blocking stdio proxies, signal handling, topology constants |

</details>

The dependency graph is acyclic: `kube`/`telemetry` → `exposure` → the domain core
(`validation`, `naming`, `k8s_resources`) → storage/builds/client/admission/identity →
the three process entry points → `cli` and `mcp`, both of which egress through `client`.
Changing the domain core's public surface is a repository-wide change.

## Identity and multi-tenancy

One control plane can serve mutually untrusted tenants. Identity has two parts: a
**merchant** (API key, prefix `sitem_`) and a **tenant** (`user_id`, token prefix `site_`).
`user_id` is unique only *within* a merchant, so naming a tenant requires both parts.

There are four mutually exclusive credentials, and each one fully determines the merchant:
a tenant token, a merchant API key, the platform admin token, and a console session. All
four failure modes return an identical 401, so the endpoint cannot be used to probe which
merchant or tenant names exist.

- A merchant key may act for a subject **inside its own merchant** by sending
  `X-Acting-Subject` (32 lowercase hex, derived by the caller as
  `HMAC-SHA256(salt, tenant_id + "\0" + subject_id)[:16]`), and only if the key carries the
  `mayActAsSubjects` grant. A key without the grant that sends the header gets 403 — it is
  not quietly demoted to its own identity. A key *with* the grant that omits the header gets
  400 rather than a guessed default.
- A missing tenant row is created on first use. A missing **merchant** never is; logins do
  not create merchants, and an unmapped claim is refused rather than landed in a default.
- Merchant API keys expire (90 days by default, restarted by rotation). An expired key is
  refused with the same 401 as an unknown one. Revocation is re-checked per request.
- Namespaces, custom-resource names, network policies, and public routes are separated by
  the digest of the `(merchant, tenant)` pair.
- Quotas are two-layered: tenant (deployments, public routes) and merchant (tenant count,
  aggregate deployments). Exceeding either returns 429 with `quota_exceeded` or
  `merchant_quota_exceeded`. Namespace CPU, memory, and Pod totals are enforced separately
  by Kubernetes.

[docs/AUTH.md](docs/AUTH.md) is the authority: credentials, revocation timing,
`X-Acting-Subject`, how merchants and tenants come into existence, and every refusal a
client can receive.

### Admin console login

The console signs in through the deployment's own OpenID Connect provider (Authorization
Code + PKCE, RS256 ID tokens, and an audience that must be configured and must differ from
every other service on that provider). With no provider configured, the service token is
accepted at `POST /v1/auth/local` instead.

**Note:** that local login is the break-glass entrance — it bypasses the identity provider
entirely, so every attempt is written to the audit log as `console_login` with the source
address. `SITES_LOCAL_LOGIN_ENABLED=false` closes the authentication path itself: the
endpoint answers 403 and the service token stops being accepted as a credential anywhere.
The API refuses to start if the configuration would leave no way in at all, and when OIDC
replaces the local path, both `SITES_OIDC_ADMIN_CLAIM` and `SITES_OIDC_ADMIN_VALUE` become
mandatory — merchant-only claims cannot bootstrap the management plane.

```bash
# Administrator (admin token)
sites admin merchants create acme --display-name "Acme"        # API key printed once, in clear
sites admin tenants create alice --merchant acme --max-deployments 5
sites admin tenants rotate alice --merchant acme               # new token; also re-enables a disabled tenant

# Tenant
SITES_TOKEN=site_... sites whoami
```

The management APIs accept admin tokens only and are intentionally **not** exposed to agents
as MCP tools.

## Agent integration

Two interfaces, same capabilities:

- **CLI** — usable by any agent that can run a shell, with no configuration file.
- **MCP** — `sites mcp` speaks stdio. Tool descriptions are generated at startup from
  `GET /v1/capabilities`, so the boundaries an agent sees always match the control plane.
  Assets deployed through MCP belong to the calling user, not the service identity. Write
  tools run a read-only quota and deployment preflight and return `quota_preflight_blocked`
  before POST when a quota is already full; final admission is still decided atomically by
  the control plane.

```json
{
  "mcpServers": {
    "sites": {
      "command": "sites",
      "args": ["mcp"],
      "env": {
        "SITES_URL": "http://127.0.0.1:18091",
        "SITES_TOKEN_FILE": "/etc/sites/token"
      }
    }
  }
}
```

Use `SITES_TOKEN_FILE` to keep the credential outside the working directory. MCP
configuration usually lives there, and an agent that can read a clear-text token may copy it
into a shell command where it leaks through shell history or `ps`.

The reusable agent workflow — static publishing, dynamic PostgreSQL schema deployment,
immutable versions, revision-matched verification, and guarded NL2SQL — is maintained in
[`skills/sites`](skills/sites) so other agent developers do not copy instructions by hand:

```bash
python scripts/install-agent-sites-skill.py /path/to/agent --check   # exits non-zero if it differs
python scripts/install-agent-sites-skill.py /path/to/agent --force   # replaces only builtin_skills/sites
```

See [docs/AGENT_CONTRACT.md](docs/AGENT_CONTRACT.md) for the complete agent contract.

## Site data, versions, and rollback

Dynamic sites get a stable PostgreSQL schema plus separate runtime and reader roles.
`POST /v1/sites/{name}/versions` provisions the binding without returning credentials;
version-aware deployments copy only runtime connection fields into the tenant namespace.

Promotion happens only after rollout readiness **and** server-side verification for the same
revision. Two consecutive verification failures for one revision, separated by the bounded
retry window, rewrite the `SiteDeployment` back to the last promoted image and version; a
single readiness-edge probe failure is kept as evidence but cannot trigger rollback.

Code-change size and schema risk are classified separately. A large rewrite can still be
`rebuild-compatible`; schema impact is independently one of `none`, `additive`,
`compatible`, or `destructive`. Destructive DDL is rejected outright; additive and
compatible migrations must use expand-contract with a content digest, and the matching
`migrationSql` runs transactionally through the narrow validator described above. A pending
or failed migration blocks both deployment and promotion.

`deploy_static_versioned` validates a bounded UTF-8 tree, uploads a private
content-addressed `s3://` or `oss://` envelope, creates an immutable version, and deploys it
with the fixed nginx runtime. Only the tenant initContainer receives a minimal copy of the
two object-storage keys; it verifies and materializes the artifact into an `emptyDir`, and
nginx mounts the result read-only. Verified rollout failure repoints the CR at the last
promoted artifact.

## Deployment and configuration

`charts/site` is the product-owned, independently renderable deployment contract — Helm,
Argo CD, Flux, and other OCI-aware controllers consume it without cloning anything else.

```bash
make chart-lint      # helm lint charts/site
make chart-render    # helm template site charts/site
```

Chart templates apply in filename-prefix order: `00-platform` (CRDs) → `01-storage` →
`05-postgres` → `07-build-plane` → `08-gateway` (Envoy Gateway and the sites-exposure
ConfigMap) → `09-activator` → `10-control-plane` (api and operator) → `11-monitoring`
(optional) → `12-tracing` (optional). CRDs, storage, and the three control-plane processes
are static; the operator creates tenant Namespaces, Deployments, Services, HTTPRoutes,
ScaledObjects, and NetworkPolicies dynamically from custom resources.

**Exposure.** The `gateway` backend uses Envoy Gateway plus HTTPRoute (domain
`<svc>-<digest>.<suffix>`); the `nodeport` backend allocates from a port pool. Switching
only changes `SITES_EXPOSURE_BACKEND`, and drift resync migrates existing routes.
`scaleToZero` defaults to **off** and is never inferred — it must be requested explicitly,
and it requires the gateway backend.

**Scale to zero.** KEDA polls the activator's `/scale-metrics` → idle workloads scale to
zero → traffic reaches the activator → it looks up the route, patches the scale subresource
to wake the site, and forwards the request after bounded connection retries. Lifecycle phase
and replica state are reported separately: a dormant site is still `phase=Running`, with
`runtimeState=Dormant` and `observedReplicas=0`.

**Metadata storage.** PostgreSQL only. The local reference deployment owns an isolated
StatefulSet and PVC via `05-postgres.yaml`; production can substitute a managed endpoint for
the stable `sites-postgres` Service contract. SQLite and MySQL are not runtime or test
backends.

**Source-build storage.** `SITES_SOURCE_BACKEND=pvc` (default) writes into the
`sites-sources` PVC. With `oss`, the API writes the same content-addressed package to a
private bucket over the S3-compatible API, and the builder Job verifies the digest in an
initContainer before BuildKit runs. Access keys are read only from files in the
`sites-oss-auth` Secret and never enter a SiteBuild CR or an environment variable.

Which knobs belong where — code, GitOps, admin console, or tenant request — is settled in
[docs/CONFIGURATION.md](docs/CONFIGURATION.md). Full operator reference, including OIDC
variables, timeouts, and the Secret key contract, is in
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

### Monitoring and tracing

[`charts/site/templates/11-monitoring.yaml`](charts/site/templates/11-monitoring.yaml)
ships a single-replica Prometheus that scrapes the api, operator, and activator admin ports
plus the gateway and cadvisor. Alert rules live in the same ConfigMap
(`sites-prometheus-config`, key `sites-alerts.yml`) and cover what readiness probes cannot
see: a dependency flipping to 0, a stale snapshot or reconcile sweep, cold-start and
forwarding failures, and any control-plane target disappearing. There is no Alertmanager in
the reference deployment, so read firing rules from the Prometheus UI at `/alerts` or query
`ALERTS{alertstate="firing"}`. `tests/test_monitoring.py` checks every metric name in the
rules against the registrations in `src/sites/*.py` — a misspelled name never errors, it
just never fires.

Tracing is optional and standalone-safe. Set Helm `tracing.enabled=true` and
`tracing.endpoint` to emit dependency-free OTLP-HTTP JSON spans. The API and activator
accept upstream W3C `traceparent`, and build/deploy resources preserve it through
asynchronous reconciliation. Collector failure never fails traffic; span loss surfaces as
`sites_tracing_export_total`.

## Security boundaries

- Authentication is mandatory for deployment, administration, and MCP access. There is no
  "unconfigured means open" path: the terminal branch of `authenticate()` is a 401 refusal,
  and disabling local login stops the service token being accepted as a credential anywhere
  — including as admin.
- The control plane accepts secret *references*, never secret values.
- Source builds are bounded to UTF-8 text contexts and run in the isolated build plane.
  Binary contexts are rejected.
- S3-compatible source storage must use HTTPS; object-storage credentials are read from
  Secret-backed files.
- The control-plane PostgreSQL NetworkPolicy sets `policyTypes: [Ingress, Egress]` with
  `egress: []` — a default-deny that does not even permit DNS, rather than a policy that
  only adds allows.
- `SITES_DB_SSLMODE` defaults to `require` and rejects libpq's `prefer` and `allow`, because
  both silently fall back to plaintext when the server declines TLS.
- Metric label sets deliberately exclude merchant, tenant, and site identifiers.

Report vulnerabilities through [private advisories](https://github.com/convee/site/security/advisories/new);
see [SECURITY.md](SECURITY.md).

## Known limitations

These are real and current. They are listed because an adopter who discovers them in
production is worse off than one who reads them here.

**Availability**

- `sites-api`, `sites-operator`, `sites-activator`, and Envoy are all **single-replica** in
  the reference Chart. Admission uses a process-local lock and reconciliation has no leader
  election, so scaling past one replica requires distributed admission and leader election
  first.
- `sync_once()` holds the API's mutation lock across a Kubernetes `GET`. The Kubernetes
  client timeout (10s) and the mutation-lock acquire timeout (`SITES_MUTATION_LOCK_TIMEOUT`,
  10s) are the same order of magnitude, so a slow apiserver can make write paths return
  `503 control_plane_busy`.

**Rollback**

- The rollback *decision* is computed inside the mutation lock, but the `kube.patch` that
  executes it runs **outside** the lock. A concurrent manual roll-forward can therefore be
  silently reverted.
- Automatic rollback compares `consecutiveFailures` from the CR's verification status. A CR
  written before that field existed reads as `0`, so automatic rollback **never fires** for
  it. The direction is fail-safe, but it is undocumented behavior in the resource itself.

**Unproven paths**

- The published [60-trial cluster benchmark](docs/BENCHMARK_REPORT_2026-09-01.md) ran every
  trial with `exposure: internal` and `maxPublicRoutes: 0`. **The gateway scale-to-zero and
  activator cold-start chain was therefore never executed once** in that run. It has unit
  coverage and dedicated histograms and alerts, but no benchmark evidence.
- Rollback p95 was 77.8s against a static/dynamic publish p95 near 8.1s — a long tail that
  is measured but not thresholded.
- Production S3/OSS, managed PostgreSQL, concurrent multi-tenant load, and live Tempo trace
  acceptance are all separate lanes that have not been run.

**Housekeeping**

- **Historical UIDs remain in the Git history.** The working tree is clean, but publishing
  with history attached does not clear them; a `git filter-repo` pass is required before the
  repository is made public.
- 45 `SITES_*` environment variables are read by `src/sites/` but appear in no document or
  chart — mostly activator, gateway, NodePort-pool, KEDA, and operator tuning. They have
  working defaults, and the most useful ones are now listed under
  [Undocumented tuning variables](docs/CONFIGURATION.md#undocumented-tuning-variables), but
  the set is not yet complete or schema-validated.
- `api.py`, `operator.py`, and `storage.py` are large, and environment reads are spread
  across process-owned modules instead of validated once at startup.

## Installation as a package

```bash
pip install "site @ git+https://github.com/convee/site.git"
sites --help
```

For source development use `uv sync --locked --extra dev`. The importable package lives at
`src/sites/`, so the clone directory name does not affect Python import semantics.

## Development

The full CI-equivalent sequence:

```bash
uv sync --locked --extra dev
make test-db && make test && make test-db-down
make console                 # npm ci, lint, typecheck, build
make chart-lint chart-render
make benchmark               # fail-closed deterministic contract profile
docker build --check .
```

Optional lanes: `make test-supabase` validates the same schema-isolation path against
managed PostgreSQL (keep the dedicated test password in the macOS Keychain under
`supabase-site-test-db`, or inject `SITES_TEST_DB_PASSWORD` from your CI secret store), and
`make test-oss` runs the private S3/OSS static artifact end-to-end lane. Both need
credentials this repository does not ship.

`scripts/evaluate-scaffolds.py` materializes the four curated profiles in a temporary
directory, exercises local contracts and a localhost static smoke test, and records stage
duration and status. Container builds need the explicit `--allow-container-builds` switch;
Kubernetes and production S3/OSS deployment remain separate E2E lanes.

See [CONTRIBUTING.md](CONTRIBUTING.md) for review expectations and the PR checklist.

## Documentation

| Document | Use it for |
|---|---|
| [docs/README.md](docs/README.md) | Full documentation index |
| [docs/AUTH.md](docs/AUTH.md) | The authentication contract — authoritative for any client |
| [docs/AGENT_CONTRACT.md](docs/AGENT_CONTRACT.md) | Capability discovery, deployment forms, success evidence, MCP setup |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Operator reference: prerequisites, Secrets, OIDC, timeouts, boundaries |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Responsibility boundaries and review rules |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | Which settings belong in code, GitOps, the console, or a request |
| [docs/STANDALONE.md](docs/STANDALONE.md) | Standalone Helm lifecycle |
| [CHANGELOG.md](CHANGELOG.md) | Notable user-facing and operational changes |

## Project

This repository was split out of the `sites/` history of `convee/agent`. It now owns its
Python package, CLI and MCP server, control-plane image, console, Helm chart, and tests.
Consumers integrate through the HTTP, MCP, package, and chart contracts; neither `agent` nor
any infrastructure repository is a build or runtime prerequisite.

- Support and questions: [SUPPORT.md](SUPPORT.md)
- Community expectations: [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)
- License: [MIT](LICENSE)
