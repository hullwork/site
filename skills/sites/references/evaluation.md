# Sites skill evaluation

Last run: 2026-08-28, Codex CLI -> site stdio MCP -> local Lima cluster.

## Scenario results

| Scenario | Result | Evidence |
| --- | --- | --- |
| New static site | Passed | Version 1 promoted; HTTP verification 200 |
| Static second iteration | Passed | Version 3 promoted after visual upgrade; earlier versions retained; HTTP verification 200 |
| New dynamic PostgreSQL admin | Passed after platform repair | OSS source fetch, image build, additive migration, readiness, and PostgreSQL schema completed; the old same-name flow exposed a non-atomic version conflict |
| Dynamic second iteration | Passed | `buildOnly=true` under unique build names produced verified digests; visual/runtime repair promoted version 8 while sharing the existing schema |
| Dynamic NL2SQL | Passed | Reader-role query returned 0 rows without truncation |
| Query static site | Passed refusal | `site_not_dynamic`, HTTP 409 |
| Query missing site | Passed refusal | `site_not_found`, HTTP 404 |
| Write, multi-statement, and system-catalog SQL | Passed refusal | All returned `sql_query_rejected`, HTTP 400 after the catalog guard repair |

The initial environment-inclusive deployment result was 2/3 (66.7%) before platform repair. Final requested deploy/iterate acceptance is 4/4 (100%): static creation, static iteration, dynamic creation, and dynamic iteration all reached verified promotion. This is an end-to-end system rate, not a model-only score. The dedicated SQL safety matrix passed 5/5 covered refusals: static site, missing site, write SQL, multiple statements, and system catalogs.

The final regression contained 702 passing tests with 10 environment-specific skips. Browser QA found a defect that health-only verification missed: the dynamic page could render a PostgreSQL timeout while `/healthz` remained green. The platform now injects a cross-namespace database DNS name and allows only labeled dynamic-site Pods in managed tenant namespaces to reach the control-plane PostgreSQL Pod on TCP 5432. Both polished example pages returned HTTP 200; the dynamic page rendered the real count and no database exception.

## Rules derived from failures

- Dotfile paths such as `.dockerignore` are rejected by the current bounded source validator.
- Use `buildOnly=true` with a unique build name for dynamic images; an ordinary same-name source deployment conflicts with final dynamic promotion.
- Stop after a terminal build failure; inspect infrastructure evidence before changing application code.
- Kubelet-owned EmptyDir roots must use Pod `fsGroup`; the non-root downloader must not chmod the mount root.
- A successful verification from the previous revision can remain visible while an update is pending. Match the verification revision.
- `list` alone does not prove that an old deployment is a versioned dynamic site. Confirm with version metadata.
- PostgreSQL reader roles can see some system catalogs by default; AST validation must reject qualified and `pg_*` relations before execution.
- Keep environment-inclusive deployment success separate from agent policy compliance and scaffold contract tests.
- For every dynamic scaffold, deploy once with a non-default allocated `PORT` and verify both the lightweight health endpoint and the database-backed root page. A hard-coded listener can otherwise pass a build check and fail only after promotion.
- Database reachability is a two-sided policy: tenant egress and PostgreSQL ingress must both allow the narrowly labeled workload. NL2SQL success from the API does not prove the application Pod can connect.
- Build-verifier workloads consume tenant quota until they scale down. Inspect Kubernetes rollout events before guessing that a ready-image failure is caused by user IDs or application code, and do not delete a build still referenced by immutable version history merely to free a slot.
