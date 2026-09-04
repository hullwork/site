# Changelog

All notable changes to this project are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/spec/2.0.0.html).

## [Unreleased]

### Added

- A one-command, repository-owned kubeadm quickstart on a disposable Lima VM.
  It builds the control image from the checkout, installs a pinned Cilium release,
  Site, and Prometheus, deploys the included static example, and fails unless HTTP
  verification, persisted admin evidence, and observability are all available.
- A remote MCP endpoint at `POST /mcp` (stateless Streamable HTTP) on the control-plane
  API, so an agent host reaches the tool surface over the network as an external tenant
  instead of running the stdio server from a copy of this repository's source. It carries
  no authorization of its own: every tool call is re-presented as an ordinary
  authenticated `/v1/*` request with the caller's own credential. Only token credentials
  are accepted there, and a subject named in tool arguments is refused rather than
  applied. Switchable with `SITES_MCP_ENDPOINT_ENABLED` / Helm `mcpEndpoint.enabled`.
- Optional dependency-free OTLP/HTTP JSON tracing with W3C parent propagation,
  asynchronous build/deploy context, bounded non-blocking batching, drop metrics and
  Helm/Kustomize configuration.
- A provider-neutral `scripts/cluster.sh` lifecycle that accepts only standard
  kubeconfig/context, Helm values, and pre-published image references.
- A secret-value-free standalone Helm path with explicit existing-Secret key contracts,
  secure local bootstrap and smoke helpers, operator notes, and deployment documentation.
- A versioned, fail-closed benchmark contract with machine-readable specifications,
  thresholds, result schema, environment provenance, and a reproducible runner.
- An executable isolated-cluster benchmark with 60-trial minimum acceptance, raw event
  evidence, static/dynamic publication, revision matching, rollback, and cleanup scoring.
- Weekly Python dependency updates and CI vulnerability audits for Python and the console.
- PostgreSQL-only dynamic-site database provisioning with one digest-derived schema,
  writable runtime role, and read-only NL2SQL role per site, plus an optional managed
  PostgreSQL compatibility lane verified against a dedicated Supabase project.
- Immutable static/dynamic site versions, explicit promote/rollback pointers, private
  S3/OSS artifact references, bounded read-only SQL HTTP/MCP access, and version listing
  for agents preserving the last known-good target.
- Versioned static publishing with bounded nested UTF-8 files, content-addressed private
  S3/OSS envelopes, init-container digest verification, nginx credential isolation, and
  verified automatic artifact rollback.
- Authorization-gated dynamic version deployment, tenant-local runtime-only PostgreSQL
  Secrets, verified automatic promotion, and rollback to the last promoted image when a
  replacement passes Kubernetes readiness but fails server-side verification.
- Explicit code-change and schema-change classification so large compatible refactors can
  share the site schema while destructive DDL is blocked from automatic rollout.
- A read-only scaffold catalog for static HTML, Vite, FastAPI/PostgreSQL, and
  Express/PostgreSQL. It executes bounded contract checks on every query and keeps
  unexecuted build/deploy/live E2E evidence separate from the reported contract rate.
- Content-addressed dynamic-site migration artifacts with a transactional additive-DDL
  executor, immutable version status, reader-grant refresh, and deployment/promotion
  gates for incomplete or failed migrations.
- A local scaffold pipeline evaluator with per-stage durations and explicit
  passed/failed/not-run/blocked evidence; container mutation remains opt-in.
- `sites-api`, `sites-operator`, and `sites-activator` now handle SIGTERM: the
  listeners stop accepting, in-flight requests are joined, and the process exits
  instead of waiting for SIGKILL at the end of `terminationGracePeriodSeconds`
  (which is now declared explicitly, with a `preStop` sleep on the traffic-serving
  Deployments).
- Prometheus alert rules (`sites-alerts.yml`) and an activator scrape job, covering
  scrape targets down, API dependency failures, stale snapshots, operator sweep
  stalls, activator wake/forward failures, and a route table that stopped refreshing.
- `sites_operator_last_progress_timestamp_seconds`,
  `sites_activator_route_age_seconds`, and `sites_activator_route_count` metrics.
- `SITES_MUTATION_LOCK_TIMEOUT`, `SITES_DB_STATEMENT_TIMEOUT`,
  `SITES_ACTIVATOR_MAX_INFLIGHT`, `SITES_ACTIVATOR_FORCE_REFRESH_MIN_SECONDS`, and
  `SITES_VERIFY_RETRY_SECONDS` settings.

### Changed

- Administrative deployment snapshots now persist `status.verification` and the
  submitted artifact SHA-256, so the console shows the same measured evidence as the
  tenant deployment API. Existing rows are backfilled by the next snapshot sweep.
- The chart's build-registry pull address is a normal Helm value, and releases no
  longer carry an external composition descriptor or project-specific skill installer.
- **Breaking: every MCP tool lost its `sites_` self-prefix.** `sites_deploy_static` is
  now `deploy_static`, `sites_list` is `list`, and so on for all seventeen. MCP already
  namespaces tools by server and clients prefix them again, so the old names reached a
  model as `mcp_sites_sites_deploy_static` - the product named three times. There is no
  alias and no deprecation period: a client on the old names gets "no such tool", which
  is a clearer failure than a silent second spelling. Callers must update; the current
  set is always available from `tools/list`.
  Note for anyone grepping: the PostgreSQL table `sites_versions` is **not** affected and
  keeps its name; only the tool called `sites_versions` was renamed, to `versions`.
- Standalone credential bootstrap is idempotent: existing API, database,
  registry, and console-session credentials are preserved, while incomplete
  existing Secrets fail closed instead of being silently rotated.
- Isolated-cluster benchmark teardown now scales down PVC-consuming workloads
  before deleting their Pods and claims, so Kubernetes PVC protection cannot
  outlive the local-path provisioner and leave released PVs behind.
- Release charts and companion values now pin the control-plane image by the published
  image digest and verify every rendered control-plane reference before packaging.
- Release images now pass vulnerability and secret scanning under a run-scoped staging tag
  before release-tag promotion, then receive a keyless signature over the promoted digest.
- **Authentication no longer accepts a caller-declared identity.** The merchant is decided
  by the credential; `X-Merchant-ID` and `X-User-ID` are refused with 403 instead of being
  honoured or ignored. A merchant API key may act for a subject inside its own merchant by
  sending `X-Acting-Subject` (32 lowercase hex), and only when the key carries the new
  `mayActAsSubjects` grant - an unauthorized key that sends the header is refused, not
  demoted to its own identity. Every acting call is audited.
- Merchant API keys expire (`keyTtlSeconds`, 90 days by default, restarted by rotation).
  Existing keys are given the same lifetime by the v6 migration; an expired key is refused
  with the same 401 as an unknown one.
- The admin console signs in through its own OpenID Connect relying party (Authorization
  Code + PKCE, standard library only) or through the break-glass local login at
  `POST /v1/auth/local`, both of which issue an HttpOnly session cookie that is re-resolved
  against the database on every request. `SITES_CONSOLE_SESSION_KEY_FILE` is required;
  `SITES_LOCAL_LOGIN_ENABLED` disables the local authentication path itself, and disabling
  both login paths refuses to start.
- `SITES_ACTING_SUBJECT_SALT` must be at least 32 bytes. A configured-but-shorter salt
  refuses to start (`sites mcp`) and refuses to derive; an absent salt still means "this
  deployment acts for nobody" and fails closed at the call. The floor and the derivation are
  pinned by the shared cross-repository vectors in `docs/acting-subject-vectors.json`.
- A login can no longer create a merchant. Claim values are mapped to **existing** merchants
  through `SITES_OIDC_MERCHANT_MAP`, and an unmapped value is refused with 403 and a log
  line. Tenants may still be created on first use, gated by `SITES_OIDC_SIGNUPS_ENABLED`
  and `SITES_OIDC_EMAIL_DOMAINS`.

### Removed

- Removed the `APPFORGE_*` environment aliases and
  `X-AppForge-Service-Token`; only the canonical `SITES_*` configuration and
  `X-Sites-Service-Token` authentication header remain.
- Removed `control_sso` and the federated console session it granted: it made another
  repository the identity provider of this one and signed assertions with the bearer token
  it also had to hand that repository (NIST SP 800-57 §5.2 vs RFC 6750).
- Removed the trusted-proxy path (`SITES_TRUSTED_PROXY_TOKEN_FILE`, `X-Sites-Trusted-Proxy`
  and the optional manifest patch). It let an admin token plus a shared secret act as any
  merchant and any tenant, which is the same "the caller names the tenant" shape as above.
- Removed `SITES_MERCHANT_ID`, `SITES_USER_ID` and `SITES_PROXY_TOKEN` from the client and
  MCP server; `SITES_ACTING_SUBJECT` (or `SITES_ACTING_SUBJECT_SALT` with
  `SITES_ACTING_TENANT`) replaces them.

### Changed (breaking)

- `clusterNetwork.podCIDR` is a required Helm value with no default, and
  `SITES_CLUSTER_POD_CIDR` a required operator environment variable. It was a
  hardcoded `10.201.0.0/16` in five places (`src/sites/k8s_resources.py`,
  `src/sites/topology.py`, and three NetworkPolicy rules in the chart) --
  a guess about somebody else's cluster. Those rules are "allow 0.0.0.0/0
  except the Pod CIDR", so a value that matches no Pod excludes nothing and
  they degrade to "allow everyone", without failing and without looking wrong
  in `kubectl get netpol`. A default would have kept exactly that as the
  behaviour of every unconfigured install. Set it from
  `kubectl cluster-info dump | grep -m1 cluster-cidr`; an unset value fails the
  chart render.
- `sites-operator` verifies that value against its own Pod IP (from the
  downward API) before opening a port, and exits 1 if the address is outside
  the declared network -- or if the address is unavailable, because "cannot
  check" is not "checked". `sites.k8s_resources.CLUSTER_POD_CIDR` and
  `WORKLOAD_EGRESS_EXCEPT_CIDRS` are replaced by `cluster_pod_cidr()` and
  `workload_egress_except_cidrs()`; `sites.topology` re-exports them instead of
  keeping a second copy.

### Added

- `NOTICE` and per-component records inside the image at
  `/usr/share/licenses/`, in the shape `hullwork/infra` already uses for the AGPL
  MinIO client. This repository is MIT and its image distributes `psycopg` and
  `psycopg-binary`, both LGPL-3.0-only; importing a library and shipping a copy
  of it are different acts and only the second carries the obligation, so
  nothing in the source tree made it visible. Each record holds the upstream
  licence as published in the wheel, the exact version, where the corresponding
  source is, and that the package is installed unmodified. `NOTICE` also states
  what the base image contributes and that it is not modified here.
- A licence gate over `requirements.lock` that fails on any dependency whose
  licence cannot be read, rather than treating unreadable as permissive.

### Fixed

- `SafeStdout` drains at interpreter exit. Its drain thread is a daemon, so
  everything still queued was dropped when a process exited -- which is worst
  for the line explaining *why* it is exiting. Measured on the real operator
  entrypoint: a refusal to start printed its reason and died with completely
  empty stdout and stderr.
- `SafeStdout` now emits the "dropped N log segments" notice it was written to
  emit. It was computed only on the branch that sleeps and continues, so it
  could never be written, while the counter behind it was advanced anyway.
  Measured: 194 segments dropped, zero notices. All three resident entrypoints
  install this proxy, so a truncated container log was indistinguishable from
  a quiet one.
- `POST /v1/sites` refuses fields outside the deployment contract instead of
  dropping them. `scaleToZeroo: true` used to deploy a site with scale-to-zero
  off and answer 200 without mentioning the key it ignored.
- The activator's background route refresher survives an error that is neither
  `ApiError` nor `RuntimeError`. One of those ended the thread for the life of
  the process: the table froze, `/healthz` kept answering 200 because a stale
  non-empty table is deliberately still Ready, and every site created after
  that moment was invisible to the activator.
- Reclaim generated benchmark PVCs and wait for their PVs before uninstalling the
  chart-owned local-path provisioner, so a successful isolated run leaves no Released PVs.
- Require two consecutive revision-bound server-side verification failures before
  automatic version recovery, preventing a transient Service readiness-edge probe from
  rolling a healthy update back before the operator's bounded retry.
- The Helm chart license now agrees with the repository MIT license, while contributor
  setup and test instructions use the supported local database and standalone commands.
- The activator no longer turns an apiserver outage into a data-plane outage: a
  failed availability check forwards the request instead of dropping the connection,
  readiness stays green while a non-empty route table is merely stale, and liveness
  moved to `/livez` so the last good copy of the table is not killed with the process.
- Turning `scaleToZero` off now deletes the site's ScaledObject instead of leaving
  KEDA and the operator fighting over the replica count.
- Control-plane writes waiting on the admission lock give up with 503
  `control_plane_busy` after a timeout instead of queueing forever behind a stalled
  holder; PostgreSQL connections carry statement timeouts and
  keepalives.
- `sites-api` no longer crash-loops when the apiserver is unreachable at startup;
  the first snapshot sync is retried by the sync loop like every later one.
- The operator health check counts per-resource progress, not only completed
  sweeps, so a large but advancing sweep is no longer indistinguishable from a
  deadlock; failed verification probes back off instead of re-probing every round.
- A running site that loses readiness gets a fresh deploy window instead of being
  reported `Failed` immediately.
- The reference `sites-api` manifest no longer sets `SITES_TRUSTED_PROXY_TOKEN_FILE`:
  the quick-start Secret has no `trusted-proxy-token` key, so the variable made
  `sites-api` crash-loop on `FileNotFoundError` and would have blocked the printed
  admin token from deploying. Opting in is now an explicit JSON patch
  (`manifests/optional/sites-api-trusted-proxy.patch.json`), and a variable that
  points at a missing file means "no trusted proxy" with one warning log line.

- Added the documented `sites build submit`, `sites build status`, and
  `sites build delete` CLI commands for the existing source-build HTTP contract.
- Added pinned-action, container-base, and console dependency update automation.

### Changed

- Moved the Python runtime package to a standard `src/sites` layout. Built wheels no
  longer include tests or a duplicate top-level `safe_stdout` module.
- Shared API exception mappings moved out of the composition root, removing the runtime
  import cycle between `api.py` and endpoint mixins; a contract test now keeps the
  runtime import graph acyclic.
- Narrowed the container build context so local bytecode, caches, generated artifacts,
  and tests cannot enter the runtime image.
- Normalized dependency lock URLs to the official Python Package Index and made local
  and CI commands honor the lockfile.
- Added bounded request handling to the scale-to-zero activator and security/cache
  headers to API responses.
- HTTP server headers no longer disclose the Python runtime version.
- The activator now retries only connection establishment, so a partially sent
  non-idempotent request is never replayed.
- Required HTTPS for S3-compatible source storage endpoints.
- Pinned the default static-site runtime image to an immutable digest.
- Changed the local-cluster smoke probe from liveness to database-aware readiness.
- Image references now follow the Docker registry/tag/digest grammar before admission.

### Fixed

- MCP write tools now reject an empty or oversized `deploymentIntent` even when trusted
  runtime authorization is present, and malformed tool arguments no longer terminate the
  stdio server.
- Database configuration no longer includes the password in its generated `repr`.
- The one-time credential dialog is now a keyboard-accessible modal with focus trapping,
  Escape handling, and focus restoration.
- Console federation rejects malformed signed timestamps and logout now requires the
  console CSRF token when a session cookie is supplied.
- The local-cluster quick start no longer configures an unused trusted-proxy token, which
  made the printed admin token unable to deploy the documented default identity.
- Source-build status now mirrors the resulting deployment's verification evidence, so
  `sites build status` and MCP `build_status` expose the documented acceptance
  contract.

### Removed

- Removed the MySQL runtime backend and dependency. The control plane now requires
  PostgreSQL and rejects `SITES_DB_BACKEND=mysql|sqlite`; required integration tests use
  disposable PostgreSQL schemas too.
- Removed stale private kubeadm/Lima deployment history and broken documentation
  references from public manifests and comments.
