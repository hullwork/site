# Architecture guide

This guide summarizes the authority boundaries behind the detailed module map in the [README](../README.md). It is intended for reviewers deciding where a change belongs.

## Control and data flow

```text
CLI / MCP / console
        │
        ▼
   sites-api ─── PostgreSQL metadata ─── sites-operator
        │                                   │
        │                                   ▼
        │                          SiteDeployment / SiteBuild
        │                                   │
        └── verification evidence ◄─────────┘
                                  Kubernetes workloads
                                          │
traffic ──► gateway or nodeport ──► activator or workload
```

## Responsibility boundaries

| Boundary | Owner | Non-goals |
| --- | --- | --- |
| Capability declaration | `/v1/capabilities` and `validation.py` | Hard-coded client-side capability lists |
| Identity and quota | `identity.py`, tenant/merchant APIs, admission checks | Organizational RBAC or billing |
| Control metadata | `storage.py` with PostgreSQL | Storing workload data or secret values |
| Dynamic-site data | `site_database.py` with one schema and runtime/reader roles per site | Sharing a writable role across sites or letting NL2SQL use runtime credentials |
| Site versions | Immutable `sites_versions` rows plus one atomic `current_version` pointer | Mutating a live artifact in place or deleting rollback targets during promotion |
| Static artifacts | Private content-addressed S3/OSS envelopes; credentialed initContainer materialization into a read-only nginx volume | Public buckets, signed URLs in CRs, or OSS credentials in the serving container |
| Scaffold evidence | Request-time local contract checks with E2E explicitly `not-run` | Treating contract validation as a real Agent deployment success rate |
| Kubernetes convergence | `operator.py`, `k8s_resources.py`, CRs | Direct unrecorded `kubectl` mutation as the normal path |
| Exposure | `exposure.py`, gateway or NodePort backend | Arbitrary ingress, custom domains, or public hosting |
| Wake and forwarding | `activator.py` and KEDA scale metrics | Keeping idle preview replicas permanently warm |
| Build | `builds.py`, build plane, and registry | Binary contexts or privileged tenant-controlled builds |
| Agent interface | `client.py`, CLI, and MCP | Bypassing the control plane or fabricating readiness evidence |
| Console | `console/` | Direct Kubernetes or database access from the browser |

## Review rules

- Preserve the dependency graph documented in the README; avoid adding imports from API mixins back into domain modules.
- Keep sites-api and sites-operator single-copy in the Helm chart: admission
  uses a process-local lock and reconciliation has no leader election.
- API and MCP behavior must flow through `client.py`; duplicate request and authentication behavior is a contract risk.
- Every workload change must preserve tenant naming, namespace isolation, quota accounting, and drift reconciliation.
- Build and exposure behavior must remain declarative and fail closed when prerequisites are absent.
- Creating a version does not prove deployment readiness. Promote only after artifact,
  migration, workload readiness, and verification evidence have succeeded.
- The operator has no metadata-database connection. It copies the runtime-only Secret;
  the API synchronizer owns verified promotion and rollback decisions in PostgreSQL.
- A shared site schema does not mean arbitrary in-place DDL. Version metadata separates
  code refactoring from `none`/`additive`/`compatible`/`destructive` schema impact;
  automatic rollout accepts only backward-compatible expand-contract stages.
