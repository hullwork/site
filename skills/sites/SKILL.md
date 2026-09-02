---
name: sites
description: Build, deploy, iterate, version, and query site static or PostgreSQL-backed dynamic sites through the Sites MCP. Use when the user asks agent to create, publish, modify, inspect, or query a site.
---

# Sites

Use the Sites MCP as the deployment boundary. Build and test source in the workspace, then publish only when the user asks for deployment, hosting, a public URL, or an update to an already deployed site.

The full connector tool names use the `mcp_sites_sites_*` prefix; this skill uses short names below. This skill does not grant tools, infrastructure or permissions. Do not use host Docker, a Docker socket, `kubectl`, or a prose claim as a substitute for MCP deployment evidence. If the Sites connector is unavailable, report the missing capability.

## Discover the live contract

Before the first mutation:

1. Call `capabilities`, `scaffolds`, and `whoami`.
2. Call `versions(name)` before modifying an existing site. A 404 is the expected new-site result.
3. Call `status(name)` when a deployment may already exist.

Tool descriptions and live responses override examples in this skill. Do not infer a dynamic site from its name or framework; confirm it from version metadata. `list` may include ordinary image deployments that do not have a site database.

## Choose the publishing path

### Static site

Use `deploy_static_versioned` for HTML/CSS/JavaScript that needs no server or database. This path stores immutable static content in configured S3/OSS and should be preferred over a Dockerfile build for a pure static site.

- Include `index.html`.
- Submit the complete intended file map, not a patch.
- Use the same site name for a compatible iteration so version history is preserved.

### Dynamic PostgreSQL site

Use a supported dynamic scaffold and PostgreSQL only. Do not introduce SQLite or MySQL.

The application must bind `0.0.0.0` and read its listening port from the
platform-provided `PORT` environment variable. Never hard-code `8080`: tenant
port allocation may select another port. Provide a lightweight health endpoint
that does not query PostgreSQL, and pass that exact path as `healthPath`.

1. Submit a bounded UTF-8 source map with a root `Dockerfile` through `source_deploy`. Set `buildOnly=true` and use a unique build name such as `<site>-v<next>-build`; keep the final site name stable.
2. Poll `build_status` until the build is verified and returns an immutable `@sha256:` image digest.
3. Pass that exact digest to `deploy_dynamic` under the stable final site name, with the version and database change classification.

The source builder accepts an inline file map, not a directory or archive. Read every submitted workspace file first. Reject binary or oversized inputs. Do not include dotfiles unless the live contract explicitly permits them; current validation rejects paths such as `.dockerignore`.

A `Building` response means only that source was accepted. It is not deployment success.

Do not use an ordinary source deployment with the same name as the final dynamic site: it creates a temporary `SiteDeployment` and conflicts with dynamic promotion. Keep the successful build until the final version is accepted; after promotion, the build record may be deleted because build-only digests are retained for version-aware registry cleanup.

## Classify every iteration

Classify code change and database change independently.

| Situation | `changeMode` | `schemaChange` | Migration |
| --- | --- | --- | --- |
| Local code/UI change | `incremental` | `none` | `none` |
| Large code refactor, same DB contract | `rebuild-compatible` | `none` | `none` |
| New nullable column/table/index | matching code mode | `additive` | `expand-contract` |
| Backward-compatible transition | matching code mode | `compatible` | `expand-contract` |
| Drop/rename/type rewrite or incompatible semantics | either | `destructive` | `manual-cutover` |

Compatible versions have independent source artifacts and image digests but share the site's PostgreSQL schema. Use `databaseStrategy=shared` and `databaseCompatibility=backward-compatible`. Automatic deployment intentionally rejects `destructive`; propose a new site/schema or an explicit manual cutover instead of hiding the impact.

For additive or compatible changes, submit exact idempotent PostgreSQL DDL and its SHA-256. The automatic surface is deliberately limited to supported `CREATE ... IF NOT EXISTS` and additive operations. Do not put seed DML or destructive SQL into a migration.

Use `decisionRationale` to state why the selected code and database classifications are safe. Copy the authorized deployment purpose byte-for-byte into `deploymentIntent`, including its punctuation: do not paraphrase, translate, trim, or add a final punctuation mark. Never invent authorization.

## Verify deployment and promotion

Poll rather than repeatedly resubmitting:

1. Poll `build_status` for source builds.
2. Poll `status` after deployment.
3. Call `versions` again after an iteration.

Acceptance requires all of the following:

- terminal running phase and `ready=true`;
- `verification.ok=true`;
- `verification.revision` equals the revision returned by this deployment, not an earlier revision;
- `currentVersion == deployedVersion ==` the new immutable version.

For a dynamic site, also execute one harmless reader-role query and request the
database-backed application page when a browser or HTTP probe is available. A
lightweight `/healthz` can be green while the runtime database path is blocked
by DNS or NetworkPolicy. Reject pages that merely render an exception name,
placeholder count, or misleading "healthy" badge.

During an update, status may temporarily contain a successful verification from the previous revision. Never accept it until the revision matches. A failed update is not a new promoted version; retain and report the last verified current version and the rollback/failure state.

Report the site URL, site name, site type, immutable version, artifact or image digest, revision, and verification result. Do not report credentials.

## NL2SQL

When the user asks a natural-language question about a dynamic site's data, load the separate `sites-nl2sql` skill and follow it. This deployment skill only establishes the site and version context; `query_database` remains the deterministic execution boundary. Keep query generation and schema-discovery rules in `sites-nl2sql` so the two skills cannot drift.

## Failure handling

- 400 source-path validation: fix the submitted file map once; do not change application semantics.
- Build `Failed`, missing digest, or `verification.ok=false`: stop and report the original build evidence.
- Object-storage materialization failure: classify it as infrastructure/storage blocking, not an application-code failure.
- 404 before a new deployment: continue as a new site. A 404 after submission is a failure.
- 409/429 authorization, type, or quota errors: report the boundary and current usage; do not evade it with unrelated names or tenants.
- A rollout that is ready at the Pod level but cannot create a replacement may be blocked by ResourceQuota or an old build verifier. Inspect rollout events before changing `runAsUser`, ports, or application code.

For disposable evaluation sites, keep a cleanup ledger. Delete only assets created by the current run and follow the tool's confirmation protocol. Never delete an existing user site merely to free quota.

Maintainers should read [references/evaluation.md](references/evaluation.md) before changing acceptance rules.
