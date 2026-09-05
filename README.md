# site

[![CI](https://github.com/hullwork/site/actions/workflows/ci.yml/badge.svg)](https://github.com/hullwork/site/actions/workflows/ci.yml)
[![Homepage](https://img.shields.io/badge/homepage-live-b9ff66?logo=githubpages&logoColor=111)](https://hullwork.github.io/site/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**A deployment control plane that lets an AI agent ship a website to Kubernetes — and
then proves the deployment actually serves traffic.**

See the project story, architecture, and one-command demo at
**[hullwork.github.io/site](https://hullwork.github.io/site/)**.

An agent asks for a deployment over HTTP, CLI, or MCP. The control plane admits it against
a tenant quota, converges it through a Kubernetes custom resource, and — once the workload
is ready — makes a real HTTP request to the resulting address and records the status code
and the SHA-256 of the response body in `status.verification`. "Deployed" is a measurement,
not a claim.

**Status: alpha.** The reference topology targets local and controlled
environments, not a public hosting platform — [Known limitations](#known-limitations) is deliberately specific.

## See the proof locally

### Host prerequisites

The supported source-checkout trial runs on macOS or Linux with hardware virtualization,
outbound HTTPS, and enough capacity for a Lima VM configured with **6 CPUs, 8 GiB RAM,
and a 40 GiB sparse disk**. Install Git, Lima, Docker, `kubectl`, Helm, `curl`, `uv`,
`lsof`, and Python 3.12+. Docker must be running, not merely installed.

On macOS with Homebrew, the command-line dependencies are:

```bash
brew install git lima kubectl helm uv python lsof
brew install --cask docker
open -a Docker                    # wait until Docker reports that it is running
```

Linux users should install [Docker Engine](https://docs.docker.com/engine/install/),
[Lima](https://lima-vm.io/docs/installation/),
[`kubectl`](https://kubernetes.io/docs/tasks/tools/), and
[Helm](https://helm.sh/docs/intro/install/) using their distribution's supported path.
Run `make quickstart-doctor` at any time for an explicit dependency, Docker-daemon, Python,
and local-port check. The first uncached run downloads a VM and several container images;
depending on the network it can take 5–25 minutes and needs ports 18090–18098 and 18447.

The fastest path is deliberately end to end. With the prerequisites above, one command
creates a disposable single-node Kubernetes
cluster with `kubeadm`, builds the image from this checkout, installs Site and Prometheus,
deploys the included HTML example, and refuses to pass until both server-side HTTP
verification and the administrative observability APIs return real evidence:

```bash
git clone https://github.com/hullwork/site.git
cd site
make quickstart
```

A successful run ends with facts rather than only a Helm exit code:

```text
Site kubeadm quickstart passed
  elapsed: <seconds>
  phase: Running
  HTTP verification: 200
  body SHA-256: <digest of the served response>
  public URL: http://127.0.0.1:18090
  public URL body: matches verification digest
  observability: available
```

`make quickstart` finishes after it has printed the verified public URL; it does not need
to stay open. To see the same application, dependency health, verification result, and
metrics in the management console, use two terminals. Keep terminal 1 running because it
owns the local console tunnel and opens the browser automatically when possible:

```bash
# terminal 1
make quickstart-access

# terminal 2
make quickstart-token
```

Paste the second command's value into **管理员 token / Admin token** and choose
**进入控制台 / Enter the console**. Only paste this secret into the local console URL
printed by terminal 1; do not commit or share it. The included application is visible
immediately under **应用 / Applications** as
`local/local/hello-site`; its HTTP verification and digests are in **Run details**, and
its **Open** action reaches the browser-visible address. CPU/memory samples are under
**Monitor → Single application**. Use **Deploy application** to create or update an
application from an existing container image; choose **Public** to receive a browser URL,
or **Internal** when no public URL should exist. The token remains in
a Kubernetes Secret and is never written into this checkout or browser storage. Inspect
the running objects with `make quickstart-status`. Pressing Ctrl-C in terminal 1 only
closes console access; the application and cluster keep running. `make quickstart-clean`
permanently deletes the disposable `site-quickstart` VM, all applications inside it, and
the repository-local kubeconfig; it does not touch another cluster or repository.

This trial harness is entirely owned by this repository: it uses Lima's default network,
bootstraps Kubernetes directly with kubeadm, and builds the control image locally. No
other checkout, shared cluster configuration, or pre-published Site image is required.

```text
 ┌─ an agent asks for a deployment ────────────────────────────────────────┐
 │  MCP tools          `sites` CLI          HTTP API          console      │
 └────────────────────────────────┬────────────────────────────────────────┘
                                  │  merchant API key
                                  │  the tenant is decided by the credential;
                                  │  a caller-declared identity is refused
 ┌────────────────────────────────▼────────────────────────────────────────┐
 │  sites-api      admission · quotas · tenancy                            │
 │    │            one replica: the admission lock is in-process           │
 │    └── control metadata ──► PostgreSQL                                  │
 └────────────────────────────────┬────────────────────────────────────────┘
                                  │  writes desired state
 ┌────────────────────────────────▼────────────────────────────────────────┐
 │  SiteDeployment  ·  SiteBuild        Kubernetes custom resources        │
 └────────────────────────────────┬────────────────────────────────────────┘
                                  │  reconciled by
 ┌────────────────────────────────▼────────────────────────────────────────┐
 │  sites-operator     one replica: no leader election                     │
 │    ├── build plane ──► image from a source tree, or an image you name   │
 │    ├── workload ─────► Deployment · Service · Ingress                    │
 │    └── once ready ───► fetches the real address over HTTP and records   │
 │                        the status code and the SHA-256 of the body in   │
 │                        status.verification                              │
 │                        "deployed" is a measurement, not a claim         │
 └─────────────────────────────────────────────────────────────────────────┘

 serving a request, including the first one after idle
 ┌─────────────────────────────────────────────────────────────────────────┐
 │  traffic ──► gateway ──► sites-activator ──► workload                   │
 │                            │                                            │
 │                            └── scaled to zero? hold the request, scale  │
 │                                up, then forward it — the caller waits,  │
 │                                it does not see an error                 │
 └─────────────────────────────────────────────────────────────────────────┘

 Not owned here: cluster provisioning, custom domains, public ingress,
 billing, organizational RBAC.
```

## Development quickstart

### Build and test it (no Kubernetes cluster needed)

Prerequisites: [uv](https://docs.astral.sh/uv/), Docker, and Python 3.12+. The suite runs in
roughly one to two minutes on a warm checkout.

```bash
git clone https://github.com/hullwork/site.git
cd site
uv sync --locked --extra dev
make test-db     # starts a throwaway PostgreSQL on 127.0.0.1:55439
make test        # 1031 tests
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

### Install it into an existing cluster

The one-command kubeadm path above is the supported source-checkout trial. For another
cluster, the control image must already be pullable by that cluster, and you must supply
the cluster's real Pod CIDR. A wrong CIDR is a tenant-isolation failure, so the Chart
does not guess one. Released versions publish a matching image and OCI Chart together;
before the first release, build and publish or load the image yourself.

```bash
kubectl config current-context
SITES_KUBE_CONTEXT=my-context \
SITES_CONTROL_IMAGE_REPOSITORY=registry.example/site-control \
SITES_CONTROL_IMAGE_TAG=v0.1.0 \
SITES_CLUSTER_POD_CIDR=10.244.0.0/16 \
SITES_LOCAL_PATH_PROVISIONER_ENABLED=false \
scripts/cluster.sh up
```

The bootstrap helper generates credentials into a mode-0700 temporary directory and never
writes them into the repository or a values file. **Do not use it for production.** For
the lower-level Helm lifecycle and production Secret contract, see
[docs/STANDALONE.md](docs/STANDALONE.md) and
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#standalone-helm-installation). Point the CLI at
the forwarded API and ask the deployed control plane what it can do rather than
memorizing limits that change with a release:

```bash
export SITES_URL=http://127.0.0.1:18091   # the client default
sites capabilities
```

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

The diagram above the quick start shows the layout; this section is the
detail behind it.


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

Every request is scoped to the merchant its credential names. A caller cannot declare a tenant in a request body — that is refused, not ignored — and the admin token is not an API key and cannot act for a subject.

[docs/AUTH.md](docs/AUTH.md) is the contract: the credential types, what each one may do, how the acting subject is derived, and the error codes you get when it is wrong.

## Agent integration

An agent reaches this over MCP — the tools, their arguments and their failure modes are in [docs/AGENT_CONTRACT.md](docs/AGENT_CONTRACT.md), which is what the agent side is written against.

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

The chart is the supported path and renders 36 objects into one namespace. `clusterNetwork.podCIDR` has no default and the operator refuses to start if its own address falls outside what you declared — a wrong value would silently turn the NetworkPolicies into allow-all.

[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) has the values, the secrets you create, and the integration seams; [docs/CONFIGURATION.md](docs/CONFIGURATION.md) has every setting.

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

Report vulnerabilities through [private advisories](https://github.com/hullwork/site/security/advisories/new);
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
pip install "site @ git+https://github.com/hullwork/site.git"
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

Site is an independent open-source project. This repository owns its Python package, CLI
and MCP server, control-plane image, console, Helm chart, kubeadm trial environment, and
tests. Consumers integrate only through the documented HTTP, MCP, package, and chart
contracts; no sibling repository is a build or runtime prerequisite.

- Support and questions: [SUPPORT.md](SUPPORT.md)
- Community expectations: [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)
- License: [MIT](LICENSE)
