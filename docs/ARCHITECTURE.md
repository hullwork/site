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
| Hull Broker wiring | `k8s_resources.py`, `exposure.py`, `operator.py` (per-CR ServiceAccount, env injection, NetworkPolicy, `/_hull/` HTTPRoute rules, ReferenceGrant); Broker process itself is `src/sites/broker/` | Sites does not run the Broker's auth, ledger, or agent logic; it only wires a tenant workload to one |

## Hull Broker data path

Off by default (`SITES_HULL_BROKER_ENABLED=0`, `broker.enabled: false`). Enabling both
adds a second, narrower control-plane surface alongside the one described above: Sites
still owns admission, quotas, and workload convergence; the Broker (CONTRACT.md,
`src/sites/broker/`) owns end-user auth, app config, and AI usage for the tenant
application that workload runs.

```text
 end user's browser
        │
        ▼
 gateway ──PathPrefix /_hull/, /.well-known/*──► hull-broker-gateway (control ns)
        │
        └──everything else──► activator or the tenant workload, as today

 tenant workload (in its own Namespace)
        │  HULL_BROKER_URL + hull-token (ServiceAccount projected volume,
        │  audience "hull-broker", NetworkPolicy egress to the gateway's port)
        ▼
 hull-broker-gateway ──TokenReview (cluster-scoped RBAC, gateway SA only)──► apiserver
        │
        ▼
 hull-broker-{worker,ingest} (background: ledger, usage, mail -- no inbound Service)
```

Sites' half of this: `k8s_resources.deployment_resource` gives each tenant Pod the
`hull-token` volume, the four `HULL_*` env vars (control-plane values, not overridable
by the tenant's own declared `env`), and the matching NetworkPolicy egress;
`exposure.GatewayExposure.route_resources` adds the `/_hull/` and `/.well-known/*`
HTTPRoute rules ahead of the tenant's own route, so they resolve to the Broker even for
a scaled-to-zero site; `operator._reconcile_hull_broker_grant` maintains the
cross-namespace ReferenceGrant those rules need, the same pattern as the activator grant.
None of this validates a token, terminates TLS for `/_hull/` traffic beyond what the
shared Gateway already does, or knows anything about the Broker's own data model - that
half is `src/sites/broker/`, reachable only through the HTTP contract in CONTRACT.md.

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
