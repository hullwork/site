# Sites agent contract

This contract applies to the CLI, MCP, CI systems, and any agent host. Quotas and
capabilities are declared by the target control plane and must not be hard-coded into a
prompt.

## Capability discovery

```bash
sites capabilities
```

The control plane reports `metadataDatabase: postgresql`. MySQL and SQLite are
not runtime backends; callers must fail closed if the PostgreSQL dependency is unavailable.

Call the read-only `scaffolds` MCP tool (or `GET /v1/scaffolds`) before selecting a
framework. Its `contractCheckSuccessRate` is computed from executable, local validation
checks at request time. It explicitly excludes checks marked `not-run`, and
`agentEndToEndSuccessRate` remains `null` until build, registry, Kubernetes readiness, and
public-path E2E runs have been collected. Never present contract success as an Agent's
real deployment success rate.

## Deployment entry points

| Form | Command | Boundary |
|---|---|---|
| Versioned static publish | MCP `deploy_static_versioned` | Nested normalized UTF-8 relative paths; private S3/OSS; root `index.html` required |
| Static direct upload | `sites deploy-static --name X --directory ./site` | Flat UTF-8 text files; must include `index.html` |
| Existing image | `sites deploy --name X --image Y` | The image must be pullable by the target cluster |
| Multiple components | `sites bundle submit stack.json` | Atomically submits multiple components in one request |
| Source build | `sites build submit --name X --directory ./source` | Bounded UTF-8 source, built by isolated BuildKit into an immutable digest |

The curated catalog currently covers plain static HTML, Vite static output, FastAPI with
PostgreSQL, and Express with PostgreSQL. A catalog entry is guidance plus evidence, not a
server-side code generator: the Agent still creates and reviews the files. Dynamic
profiles use one PostgreSQL schema per site and the `deploy_dynamic` version flow.
Versioned static publishing has executable upload/runtime contracts and immutable rollback
bindings. Real bucket, cluster readiness, and public-path checks remain a disclosed
`not-run` E2E lane until those dependencies are available. Static inline deployment is a
legacy bounded path without version rollback.

The control plane never accepts secret values. Use `--env` for non-sensitive configuration;
secrets may only reference operator-created objects through `--secret-env NAME=secret/key`
or a read-only Secret mount.

## Success criteria

Only `status.verification.ok=true` in `sites status <name>` means the control plane has
completed real HTTP evidence collection. The result also records `httpStatus` and
`bodySha256`. The public or host entry point is a separate network path: request the
returned URL from the user side and compare its response digest with `bodySha256`.

## Identity

**[AUTH.md](AUTH.md) is the contract.** It is written for any client,
not only for agent hosts, and it is the authority for everything below. Three points decide
how an agent host is built, so they are repeated here:

- Identity has two parts, `(merchant, tenant)`, and **only the credential decides the
  merchant**. `X-Merchant-ID` and `X-User-ID` are refused with 403; there is no request that
  can name the tenancy you act in.
- An agent host serving several of its own users needs a merchant API key with the
  `mayActAsSubjects` grant and must send `X-Acting-Subject` per call - an opaque, keyed
  pseudonym it derives itself, so its users' real identifiers never cross the boundary. A key
  without the grant that sends the header is refused, not quietly demoted to its own
  identity, so one user's sites can never land in another's namespace.
- Merchant API keys expire (`keyExpiresAt`); revocation and disabling take effect on the
  next request, with no cached authorization.

Load credentials from outside the working directory with `SITES_TOKEN_FILE` or
`SITES_MERCHANT_KEY_FILE` so they do not enter configuration files, shell history, or process
arguments. List and read APIs expose only resources visible to the current identity.

## MCP

There is one tool surface and two transports for it. Both are served by the same
`sites.mcp.Server`, so the tool names, schemas and boundaries cannot differ between them.

### Remote MCP (the transport for an agent host)

```
POST https://<sites-host>/mcp
```

Streamable HTTP, stateless. One JSON-RPC message per request, one JSON response back. No
session identifier is issued, no SSE stream is opened, and JSON-RPC batching (removed from
the MCP specification in `2025-06-18`) is not accepted.

| Request | Answer |
|---|---|
| `POST /mcp` with a JSON-RPC **request** | `200` + the JSON-RPC response |
| `POST /mcp` with a JSON-RPC **notification** | `202`, empty body |
| `GET /mcp`, `DELETE /mcp` | `405` with `Allow: POST` |
| body is a JSON array (batch) | `400 sites_invalid_input` |
| `Accept` lists neither `application/json` nor `*/*` | `406 mcp_not_acceptable` |
| an `Origin` header is present | `403 mcp_origin_refused` |

An `Origin` header is refused rather than matched against an allow-list: this endpoint is
not a browser API, sends no CORS header, and needs a credential a browser cannot set
cross-origin. Refusing states the rule instead of leaving it as a side effect of two other
decisions.

**Authentication is the same as every `/v1/*` route** ([AUTH.md](AUTH.md) is the contract),
with one narrowing: `/mcp` accepts only a token credential in `X-Sites-Service-Token` -
a merchant API key, a tenant token, or the platform admin token. A console session cookie
is **not** accepted here (`401 mcp_token_credential_required`), because an ambient
credential on an endpoint that deploys is a CSRF surface.

Everything else follows from AUTH.md unchanged, and is worth restating because a tool call
looks like a place where a caller could name itself:

- The merchant and tenant are decided by the credential. `X-Merchant-ID` and `X-User-ID`
  are refused with `403` here exactly as they are on `/v1/*`.
- To act for one of your own users, send `X-Acting-Subject` (the keyed 32-hex pseudonym you
  derive yourself) **per request**, from a merchant API key that carries the
  `mayActAsSubjects` grant. A key without the grant that sends it is refused, not demoted.
- 🔴 **`_agent_user_id` in the tool arguments is refused over HTTP.** It is a reserved
  argument of the stdio transport, where a trusted in-process runtime injects it after
  stripping model-supplied values. On the network the acting subject travels with the
  credential; a subject named in the request body is a caller-declared identity and is
  rejected rather than ignored, so an agent host can never silently file one user's site
  under another's tenant.

Each tool call becomes an ordinary authenticated `/v1/*` request made with the calling
credential, so quotas, impersonation grants, merchant/tenant disabling and revocation all
apply to MCP exactly as they apply to REST, and take effect on the next call.

`_agent_deployment_authorization` is the **calling runtime's** artifact, not a Sites
credential: it lets an agent host stop its own model from deploying without user intent.
It authorizes nothing on this side - without a valid Sites credential the request is
refused before it is ever read.

Operators can switch the endpoint off entirely (`SITES_MCP_ENDPOINT_ENABLED=false`, Helm
`mcpEndpoint.enabled=false`), which makes `/mcp` answer `404`. It is on by default: it
grants no capability the caller's credential does not already have on `/v1/*`.

#### Client configuration for an agent host

```json
{
  "mcpServers": {
    "sites": {
      "type": "http",
      "url": "${SITES_URL}/mcp",
      "headers": {
        "X-Sites-Service-Token": "${SITES_TOKEN}",
        "Accept": "application/json, text/event-stream"
      }
    }
  }
}
```

`SITES_URL` is the control-plane base URL (for example
`https://sites.example.com`, or `http://sites-api.sites-local.svc:8080` from inside the
same cluster) and `SITES_TOKEN` is the merchant API key or tenant token. Load the token
from a file or a secret store, never from a checked-in configuration file.

An agent host serving several of its own users adds one more header per call:

```
X-Acting-Subject: <HMAC-SHA256(salt, tenantId + "\0" + subjectId)[:16] as 32 lowercase hex>
```

See [AUTH.md §4](AUTH.md) for the derivation and `docs/acting-subject-vectors.json` for
test vectors. The salt belongs to the agent host and never crosses the boundary.

### stdio MCP (local development and the CLI)

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

`sites mcp` is a convenience for a developer or CI job that already has the `sites` CLI and
a credential on the same machine. It is **not** a second authorization path: it is an
ordinary `sites.client` caller presenting an ordinary credential to the same `/v1/*` API,
so the server-side rules that decide tenancy are the same rules, reached the same way. It
is not a way to reach Sites from an agent host - that is what the HTTP endpoint above is
for, and copying this repository's source into another product's image to get the tools is
exactly the coupling the HTTP transport exists to remove.

### The tool surface

`capabilities`, `scaffolds`, `list`, `status`,
`deploy_static`, `deploy_static_versioned`, `deploy_image`,
`whoami`, `deploy_dynamic`, `query_database`, `versions`,
`deploy_bundle`, `bundle_status`, `delete`, `source_deploy`,
`build_status`, `source_delete`.

Call `tools/list` for the authoritative set and its schemas; the descriptions are generated
from `/v1/capabilities`, so the limits they quote are the live ones and a hard-coded copy
of this list will drift. The CLI and MCP share `sites.client`; do not bypass the control
plane with Docker socket or direct `kubectl` access and then claim a deployment succeeded.

NL2SQL generation is a agent skill responsibility. The `query_database` MCP
tool accepts the generated SQL only after AST validation, then executes with the site's
reader role inside a read-only transaction with row and timeout caps. Agents should call
`versions` before a change to record the current immutable rollback target. Creating
or promoting a version remains a write operation and is not silently performed by a
read-only MCP tool. `deploy_dynamic` is the deployment-authorized workflow: it creates
the immutable version, provisions the isolated PostgreSQL roles, deploys the exact pinned
image, and leaves promotion or rollback to verified control-plane evidence.

For static iterations, use `deploy_static_versioned`: it creates the private
content-addressed artifact and immutable version before deploying. Do not put bucket
credentials, signed URLs, or arbitrary images in the request. The fixed runtime downloads
and revalidates the exact digest, and verified failures are rolled back to the last promoted
artifact.

Code-change size and schema risk are separate decisions. Agents must classify each dynamic
version with `changeMode`, `schemaChange`, `migrationStrategy`, and a bounded rationale:

- `none`: application-only change; no migration artifact.
- `additive`: new tables, columns, or indexes through `expand-contract`.
- `compatible`: a larger refactor whose intermediate schema still supports the promoted
  version; also requires `expand-contract` and a migration digest.
- `destructive`: rename/drop/type rewrite/table split or merge. Automatic deployment is
  rejected; stage it into compatible versions or request an explicitly authorized manual
  cutover.

For `additive` or `compatible`, submit the exact UTF-8 `migrationSql` whose SHA-256 is
`migrationSha256`. The control plane accepts only bounded, idempotent additive PostgreSQL
DDL, executes it as the site's runtime role in one transaction with statement and lock
timeouts, refreshes reader grants, and records `migrationStatus` on the immutable version.
A version whose migration is pending, running, or failed cannot be deployed or promoted.
The API never returns the SQL artifact or driver error details.

MCP write tools first make read-only queries for the current tenant quota and deployment
list. If the list already proves that the tenant deployment or public-route limit is full,
and the target is not an in-place update with the same name, the tool returns
`quota_preflight_blocked` before POST. Uncertain states and concurrent races are still
decided atomically by control-plane admission. `deploymentAuthorization` in the result
records only the policy version and `allowInternal` decision so authorization differences
between release versions can be explained; it contains no nonce.
