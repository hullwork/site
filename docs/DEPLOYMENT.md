# Deployment and integration

## GitOps and OCI Helm package

`charts/site` is the product-owned, independently renderable deployment
contract. Helm, Argo CD, Flux, and other OCI-aware GitOps controllers can
consume it directly. Supply the real Pod CIDR of the target cluster:

```bash
helm lint charts/site --set-string clusterNetwork.podCIDR=10.244.0.0/16
helm template site charts/site --set-string clusterNetwork.podCIDR=10.244.0.0/16
```

The default Services use `ClusterIP` and do not reserve host NodePorts. Local
path provisioning, Gateway API integration, and monitoring are opt-in; embedded
PostgreSQL and the build plane are independently switchable. All images accept
digest overrides through the validated `values.schema.json` surface.

Tagged releases publish the chart to `oci://ghcr.io/hullwork/charts/site` plus a
project-owned, machine-readable release metadata asset. A runnable deployment also requires
the release workflow to have published the corresponding `site-control`
image; publishing the chart alone is not runtime acceptance.

This is a multi-namespace package. Override `namespaces.control` and
`namespaces.gateway` through Helm values; the Helm release namespace does not
relocate these resources. Release `package-metadata.json` carries the
OCI Chart digest and the digest-pinned `images.control` value using the shared
release schema.

### Standalone Helm installation

The Chart consumes existing Secrets and never embeds credentials in values or release
artifacts. Its complete key contract is under `existingSecrets` in `values.yaml`:

| Secret | Required keys | Purpose |
|---|---|---|
| `existingSecrets.api.name` | `tokenKey`, `consoleSessionKey` | API/admin authentication and independent console-session signing |
| `existingSecrets.database.name` | `usernameKey`, `databaseKey`, `passwordKey` | Embedded or external control-metadata PostgreSQL |
| `existingSecrets.registry.name` | `passwordKey`, `htpasswdKey` | Internal build registry client and proxy authentication |
| `existingSecrets.objectStorage.name` | `accessKeyIdKey`, `accessKeySecretKey` | Optional private S3/OSS; this Secret may be absent when OSS is disabled |

For a disposable development cluster, the bootstrap helper generates random values in a
mode-0700 temporary directory and applies them through stdin. It never writes credentials
to this repository or Helm values:

```bash
scripts/standalone.sh install
scripts/standalone.sh smoke
scripts/standalone.sh uninstall
```

The smoke command waits for PostgreSQL, registry, API, and operator rollouts, then probes
`/readyz` through a temporary port-forward. Uninstall deliberately preserves the namespace,
Secrets, and PVCs. Review and remove those separately when destroying the environment.

For production, create the same key contract with External Secrets, Sealed Secrets, or
your platform secret manager instead of the development helper. Override names and keys in
an operator-owned values file. Do not put plaintext values in Git.

The source Chart keeps a tag fallback for local development. Tagged releases publish a
Chart whose default control image is injected with the image digest built by that same
workflow, plus a separate `site-values-VERSION.yaml` digest-pinned override and
`SHA256SUMS`. Release CI rejects a rendered control image without `@sha256:`.

## Local kubeadm environment

Prerequisites for the complete local trial: Lima, a running Docker daemon, kubectl, Helm,
curl, uv, lsof, and Python 3.12+. Run `make quickstart-doctor` for the executable preflight.
The default three VMs allocate 8 CPUs, 10 GiB RAM, and 70 GiB of sparse disk in total;
outbound HTTPS and host ports 18090–18098 plus 18447 are required. The repository creates
one control-plane and two worker VMs on its own Lima network and bootstraps Kubernetes with
kubeadm; it does not use a shared cluster or another checkout.

```bash
make quickstart-doctor
make quickstart
SITES_QUICKSTART_WORKERS=3 make quickstart-scale  # supported range: 1-4 workers
make quickstart-status
make quickstart-access
make quickstart-token     # run in a second terminal, then log in
make quickstart-clean
```

`make quickstart` exits after verification. `make quickstart-access` must remain running
while the console is in use; Ctrl-C stops only that tunnel. Worker scale-down drains nodes
and retains `w1`, where quickstart constrains local persistent volumes. It refuses to
remove a worker carrying an unexpected local volume. `make quickstart-clean` permanently
removes the disposable VMs, their repository-owned network, and all applications inside.

The quickstart is a multi-node functional topology with one control-plane, not a highly
available production cluster. Production provisioning, control-plane HA, upgrades, backup,
and disaster recovery are deliberately not implemented in this repository.

The chart defaults deploy the core API, operator, PostgreSQL, registry, build
plane, and console on the NodePort backend. Gateway routing and scale-to-zero
additionally require an Envoy Gateway installation, `gateway.enabled=true`, KEDA,
and DNS for the configured suffix. `monitoring.enabled=true` adds the optional
Prometheus stack. The activator buffers a request body up to
`SITES_ACTIVATOR_MAX_REQUEST_BYTES` (4 MiB by default) before waking a site.

## Admin console login

The client-facing side of everything below - what a caller may assume, and what it will be
refused for - is [AUTH.md](AUTH.md). This section is the operator's half:
the variables that decide it.

`sites-api` signs its own console sessions, so Secret `sites-api-token` needs a
`console-session-key` key of at least 32 characters, different from `token`. It is
mounted at `/var/run/sites/console-session-key` by `SITES_CONSOLE_SESSION_KEY_FILE`.

🔴 **This is a required addition for anyone upgrading.** The integration layer creates
`sites-api-token` with only the `token` key; until `console-session-key` is added to that
Secret, `sites-api` **CrashLoops on every start**. The failure is deliberately loud and
names itself, so an operator reading the Pod log sees the cause rather than hunting:

```
RuntimeError: SITES_CONSOLE_SESSION_KEY_FILE is required; it signs admin console
sessions and must not be the service token
```

and, when the variable is set but the Secret key is absent:

```
RuntimeError: Sites console session key cannot be read: /var/run/sites/console-session-key
```

A key shorter than 32 characters, or equal to the service token, also refuses to start with
its own message. There is no generated fallback on purpose: a per-process random key would
silently invalidate every session on restart and could not be verified by a second replica,
which is an intermittent logout - far harder to trace than a Pod that will not start.

There are two ways into the console, and one rule decides the default:

| Configuration | Local login (service token) |
|---|---|
| `SITES_OIDC_ISSUER` set | off by default; `SITES_LOCAL_LOGIN_ENABLED=true` keeps the break-glass door |
| no `SITES_OIDC_*` | on by default |
| both explicitly off | **refuses to start**, with the reason |

🔴 `SITES_LOCAL_LOGIN_ENABLED=false` disables the authentication path itself, not just the
form: `POST /v1/auth/local` answers 403 and the service token stops being accepted as a
credential anywhere. `POST /v1/auth/local` is the **break-glass** entrance. Every attempt,
successful or not, is logged as `console_login` with the source address - it is the one
credential that bypasses your identity provider, so treat those lines as an alert source.

To connect an identity provider (Authorization Code + PKCE, RS256 ID tokens):

| Variable | Required | Meaning |
|---|---|---|
| `SITES_OIDC_ISSUER` | yes | Issuer URL; `/.well-known/openid-configuration` is read from it |
| `SITES_OIDC_CLIENT_ID` | yes | Client registered for **this** service |
| `SITES_OIDC_CLIENT_SECRET` / `_FILE` | no | Omit for a public PKCE client |
| `SITES_OIDC_AUDIENCE` | yes | **No default.** Must differ from every other service on the same provider, or their ID tokens are accepted here |
| `SITES_OIDC_REDIRECT_URL` | yes | Must point at `<console origin>/v1/auth/callback` |
| `SITES_OIDC_ADMIN_CLAIM` / `_VALUE` | when local login is off | Explicit claim and value that mean "platform admin"; both are required together |
| `SITES_OIDC_MERCHANT_CLAIM` | no | Claim carrying the caller's organisation |
| `SITES_OIDC_MERCHANT_MAP` | no | `claimValue=merchantId,...` against **existing** merchants |
| `SITES_OIDC_SIGNUPS_ENABLED` | no | Whether a first-time user may have a tenant created (default off) |
| `SITES_OIDC_EMAIL_DOMAINS` | with signups | Address domains allowed to sign up |

Merchants are never created by a login. An unmapped claim value is refused with 403 and
logged (`console_login_signup_refused` / `console_login_merchant_unavailable`); the
alternative - landing the user in some default merchant - looks exactly like "my
permissions vanished" and is nearly impossible to trace back to the login.

When local login is disabled (including its default-off state once OIDC is
configured), `SITES_OIDC_ADMIN_CLAIM` **and** `SITES_OIDC_ADMIN_VALUE` are both
required. Startup fails if either is empty. A merchant-only mapping is not an
administrator path: it cannot create the first merchant or repair platform auth.
If local break-glass login is explicitly kept on, merchant-only OIDC remains a
valid configuration.

⚠️ `SITES_OIDC_SIGNUPS_ENABLED` covers **people signing themselves up through the identity
provider, and nothing else**. Turning it off does not stop tenants appearing under a
merchant whose API key holds `mayActAsSubjects` - that path creates a tenant row on first
use for every new subject it acts for, bounded by the merchant's `maxTenants` and by
nothing else. "How does a person register" and "how does another organisation's service
call us" are separate questions and have separate controls; to bound the second, lower
`maxTenants` or withdraw the grant.

## Control-plane timeouts

`sites-api` bounds the two waits that previously had no limit. Both are read at
startup:

| Variable | Default | Meaning |
|---|---|---|
| `SITES_MUTATION_LOCK_TIMEOUT` | `10` (seconds) | How long a write request (`POST`/`DELETE` on deployments, builds, bundles) waits for the in-process mutation lock before giving up. |
| `SITES_DB_STATEMENT_TIMEOUT` | `10` (seconds) | Server-side PostgreSQL `statement_timeout`. Also bounds the startup migration; raise it for a rollout whose migration needs longer. |

A write that hits the lock timeout returns `503` with `"code": "control_plane_busy"`.
Nothing was written; the request is safe to retry after a short back-off.
`SITES_DB_CONNECT_TIMEOUT` (default `5`) still bounds only the connection handshake.

## Consumer boundary

A consumer needs only one of these contracts:

- HTTP API: configure the control-plane URL and tenant or merchant credentials.
- MCP: run `sites mcp`.
- CLI or Python package: install `site` from this repository.
- GitOps: consume a fixed revision of `charts/site` and the corresponding image.

Consumers may pin source or manifest revisions with a submodule, but should not inject
Python modules, Docker build files, or deployment scripts into this repository.
Cross-cluster databases, external gateways, product UIs, and similar wiring belong to the
consumer's integration layer.

## PostgreSQL and OSS integration

PostgreSQL is the only metadata backend. Use a dedicated database and account, and pass
the password through `SITES_DB_PASSWORD_FILE` rather than an environment variable.

`SITES_DB_SSLMODE` defaults to `require`, and `SITES_DATA_DB_SSLMODE` inherits it.
Accepted values are `require`, `verify-ca`, `verify-full`, and `disable`. libpq's
`prefer` and `allow` are rejected on purpose: both fall back to an unencrypted
connection when the server declines TLS, so a control plane configured with either
one can talk to its database in plaintext indefinitely without a single failed
connection to reveal it. Use `disable` if you genuinely want no TLS -- it is the same
outcome, stated rather than negotiated -- and `verify-full` where you have a CA.

Dynamic site data uses a separate PostgreSQL connection in production. Each site gets a
stable digest-derived schema, a writable runtime role, and a read-only NL2SQL role. The
platform account retains schema ownership; site roles are never superusers and receive
no cross-schema grants. Local development and the optional Supabase compatibility lane
may use the same PostgreSQL instance, but control metadata and site data must remain in
separate databases or credential domains in production.

Configure that connection independently from control metadata:

```text
SITES_DATA_DB_HOST=<managed-postgresql-host>
SITES_DATA_DB_PORT=5432
SITES_DATA_DB_NAME=<site-data-database>
SITES_DATA_DB_USER=<provisioning-role>
SITES_DATA_DB_PASSWORD_FILE=/var/run/sites-data-db/password
SITES_DATA_DB_CONNECT_TIMEOUT=5
SITES_DATA_DB_STATEMENT_TIMEOUT=10
SITES_DATA_DB_SSLMODE=require
```

The API stores per-site runtime and reader passwords in control-plane Kubernetes Secrets;
it never returns them through HTTP or MCP. For a version-aware dynamic deployment, the
operator copies only `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD`, `PGSSLMODE`,
and `SITES_DATABASE_SCHEMA` into a tenant-local Secret. Reader credentials remain in the
control namespace. Production should encrypt Kubernetes Secrets at rest or replace this
adapter with an external secret manager.

The synchronizer promotes only a `Running`, ready deployment whose server-side
verification passed for the same revision. If an update becomes ready but verification
fails, it rewrites the CR to the last promoted image or static artifact and version. A first deployment has
no prior target and therefore remains unpromoted on failure. The general image, source
build, bundle, and legacy inline-static paths remain unversioned; consequently the broad
`deploymentHistory` capability remains false even though dynamic and versioned-static
protection is available.

Additive and compatible schema changes carry a content-addressed `migrationSql` artifact.
The API validates the exact UTF-8 digest, rejects cross-schema, temporary, DML, and
destructive statements, and permits only idempotent `CREATE TABLE/INDEX IF NOT EXISTS` or
`ALTER TABLE ADD COLUMN IF NOT EXISTS`. Execution uses the site's runtime role in one
transaction with statement and lock timeouts. Migration state is stored against the
immutable version; reader grants must refresh successfully before the version becomes
deployable. The broad 64 KiB request cap leaves a separately published 48 KiB migration
artifact budget.

Supabase is supported for development compatibility tests through its session-mode
Supavisor endpoint. Run `make test-supabase`; the script reads the dedicated project
password from macOS Keychain or `SITES_TEST_DB_PASSWORD` and only creates disposable
schemas and roles. It never drops or migrates `public`.

Source builds default to the `sites-sources` PVC. To use a private OSS bucket, site
connects through the bucket's S3-compatible endpoint. Give `sites-api` and
`sites-operator` the same configuration and pre-create `sites-oss-auth` for builder Jobs:

```text
SITES_SOURCE_BACKEND=oss
SITES_OSS_ENDPOINT=https://s3.oss-cn-shanghai.aliyuncs.com
SITES_OSS_BUCKET=<private bucket>
SITES_OSS_PREFIX=<optional prefix>
SITES_OSS_REGION=cn-shanghai
SITES_OSS_ADDRESSING_STYLE=virtual
SITES_OSS_SIGNATURE_VERSION=s3
SITES_OSS_ACCESS_KEY_ID_FILE=/var/run/sites-oss/access-key-id
SITES_OSS_ACCESS_KEY_SECRET_FILE=/var/run/sites-oss/access-key-secret
```

The endpoint, region, addressing style, and signature settings also work with other
S3-compatible services. For Aliyun OSS in Shanghai, the `s3` signature and virtual-host
addressing shown above are recommended.

The OSS Secret must contain only the `access-key-id` and `access-key-secret` file keys. Do
not use a primary-account AK, and do not configure the bucket as public-read-write.
`deploy_static_versioned` uses the same private bucket settings. The operator copies
only those two keys into a derived tenant Secret mounted on the downloader initContainer;
the nginx container receives neither the Secret nor OSS environment. The downloader reads
the content-addressed JSON envelope, revalidates identity/digest/size, and materializes it
into a read-only site volume before nginx starts. Uploads are limited to 48 KiB of UTF-8
content within the API's 64 KiB request boundary.

Before a release, run `make test-oss` with a dedicated least-privilege test identity in
the `site-oss-test-access-key-id` and `site-oss-test-access-key-secret`
Keychain services (account `site`), or provide the two
`SITES_TEST_OSS_ACCESS_KEY_*_FILE` paths. The test refuses a non-E2E prefix, deletes only
the exact content-addressed object it created, and never uses the personal-account loader.

The default workload policy permits public Internet endpoints but excludes RFC1918. For a
private/VPC object-storage endpoint, set `SITES_STATIC_ARTIFACT_EGRESS_CIDRS` on the
operator to the provider's exact comma-separated IPv4 CIDRs. Only TCP/443 is opened, and
only for workloads carrying `staticArtifact`; leaving it empty preserves the existing
private-network deny policy.

When the console is exposed through an HTTPS edge, set
`SITES_CONSOLE_SECURE_COOKIES=true` on `sites-api` so federation cookies carry the
`Secure` attribute. Keep the default `false` only for local HTTP development.
