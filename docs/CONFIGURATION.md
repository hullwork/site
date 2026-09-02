# Configuration ownership

site separates deploy-time infrastructure configuration from domain
policy. This prevents the admin console from becoming a second, unaudited
GitOps controller.

| Owner | Examples | Change path |
| --- | --- | --- |
| Product contract (code) | identifier grammar, request and artifact bounds, accepted deployment fields, safe Grafana proxy allowlist | reviewed release; not runtime-configurable |
| Operator / GitOps | images and digests, namespaces, database and S3 endpoints, exposure backend, ports, storage class, OIDC, log level, reconcile/probe/activator timing | Helm values, existing Secrets, or `SITES_*` environment variables; rollout required |
| Platform administrator | merchant display name and status, merchant tenant/deployment/resource quotas, tenant deployment/public-route quotas, token/key rotation | admin API and bilingual console; persisted in PostgreSQL |
| Tenant or agent | image, port, health/liveness path, bounded environment, Secret references, exposure, scale-to-zero, memory limit, version | versioned HTTP, CLI, or MCP contract |

## Values deliberately fixed in code

The following are security or protocol invariants, not tuning knobs:

- request and inline-artifact size/file limits;
- identifier and OCI image-reference grammar;
- bounded SQL/migration grammar and statement limits;
- W3C trace-context validation;
- the Grafana proxy path and datasource allowlists;
- metric label sets that exclude merchant, tenant, and site identifiers.

Moving these into the console would let an administrator silently widen the
attack surface without a reviewed deployment. Change them through a release.

### 🔴 `clusterNetwork.podCIDR` is required, and the operator checks it

Three NetworkPolicy rules are written as "allow `0.0.0.0/0` **except** the Pod
CIDR", and for each of them the exclusion *is* the rule:

| Rule | What the exclusion is doing |
| --- | --- |
| `sites-registry` ingress :5000 | keeps ordinary workloads off the anonymous read-only registry surface, so they cannot bypass the authentication plane |
| `sites-activator` ingress :9090 | keeps them off `/scale-metrics`, whose answers are per site and therefore enumerate the tenant list |
| `sites-builder` egress | keeps tenant-authored `RUN` instructions off the cluster network and the node metadata endpoint |
| tenant site ingress (NodePort backend, built in `sites/exposure.py`) | keeps one tenant's Pods out of another tenant's site |

**A Pod CIDR that does not match the cluster excludes nothing, and those rules
become "allow everyone".** Nothing fails, nothing logs, and `kubectl get netpol`
looks exactly right — the only symptom is that isolation you believe you have is
absent. This value was a hardcoded `10.201.0.0/16` in five places until
2026-09-01, which is wrong on stock kubeadm, Calico, Flannel, GKE and EKS alike.

So there is **no default**, in the chart or in the code. Set it:

```bash
kubectl cluster-info dump | grep -m1 cluster-cidr
# or: kubectl get node -o jsonpath='{.items[0].spec.podCIDR}'

helm install site charts/site --set-string clusterNetwork.podCIDR=10.244.0.0/16
```

An unset value fails the `helm template`/`helm install` render, naming the key.

#### The operator verifies it at startup, and refuses three ways

Being configurable is not enough on its own, because the failure direction is
*open*: a wrong value produces no error anywhere. So `sites-operator` — the
process that writes these policies — checks the declaration against **its own
Pod IP** (injected by the chart from the downward API, `status.podIP`) before it
opens a port or contacts the apiserver, and exits 1 rather than proceed:

| Situation | Log line | What to do |
| --- | --- | --- |
| `clusterNetwork.podCIDR` unset or not in CIDR notation | `cluster_network_refused` … `is not set` | set it from the command above |
| this Pod's address is outside the declared network | `cluster_network_refused` … `is outside` | one of the two is wrong; both are printed |
| the Pod's own address is unavailable | `cluster_network_refused` … `cannot verify` | restore the `SITES_POD_IP` downward-API entry on the Deployment |

The third case refuses on purpose. "The declaration cannot be checked" is a
different fact from "the declaration is correct", and a guard that reports the
second when it only established the first is worse than no guard, because it is
now evidence. A CrashLoopBackOff with one of those three lines is the observable
signal this whole mechanism exists to produce.

`values-dev.yaml` declares `10.201.0.0/16` because that overlay names one
specific local topology. On any other cluster that value is wrong and the
operator will say so instead of applying policies that exclude nothing.

## Values suitable for the management backend

Merchant/tenant quotas and lifecycle state are already editable in the console
and persisted transactionally. Future dynamic policy belongs there only when it
is domain data, scoped to a merchant or tenant, auditable, and safe to apply
without restarting a process. Examples are a curated deployment-template
assignment or a per-merchant maximum memory policy.

Cluster topology, credentials, endpoints, image references, OIDC trust, storage
backends, and reconcile timing do **not** belong in PostgreSQL or the console.
They remain declarative operator configuration so Git records the desired state
and rollback remains possible.

### Optional tracing exporter

Tracing is deploy-time operator configuration, never tenant/admin policy. Helm exposes
the same bounded settings under `tracing.*`; Kustomize generates the optional
`sites-tracing` ConfigMap.

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `SITES_OTLP_HTTP_ENDPOINT` | empty (disabled) | OTLP/HTTP collector root or `/v1/traces` endpoint |
| `SITES_OTLP_QUEUE_SIZE` | `2048` | maximum spans waiting in process memory |
| `SITES_OTLP_BATCH_SIZE` | `128` | maximum spans per POST |
| `SITES_OTLP_FLUSH_SECONDS` | `1` | maximum batch collection delay |
| `SITES_OTLP_TIMEOUT_SECONDS` | `2` | collector HTTP timeout on the exporter thread |

The endpoint is intentionally not writable from the management console: changing it
redirects operational metadata and crosses a trust boundary. Invalid optional tuning
falls back to safe defaults; a collector outage drops spans and increments a metric but
does not affect product traffic.

### Remote MCP endpoint

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `SITES_MCP_ENDPOINT_ENABLED` | `true` | Whether `POST /mcp` is routed; `false` makes it answer `404` |

On by default. The endpoint carries no authorization of its own - every tool call becomes
an ordinary authenticated `/v1/*` request made with the caller's own credential - so it
grants nothing a credential does not already have, and defaulting it off would only make
an agent host's tools silently absent. Any value other than a boolean stops the process
rather than being guessed at. Helm exposes it as `mcpEndpoint.enabled`.

## Undocumented tuning variables

45 `SITES_*` variables are read by `src/sites/` but appear in no other document and in no
Chart template. They all have working defaults, so nothing breaks by leaving them unset;
they are listed here because "not discoverable" is a different problem from "not
supported". This is operator/GitOps configuration in the sense of the table above: it is
read once at process start and a change requires a rollout.

The list below is the useful subset, not the complete set. It is not schema-validated,
and an invalid value generally raises at import time rather than falling back.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SITES_LOG_LEVEL` | `info` | Minimum structured-log level; `debug` logs each request individually |
| `SITES_API_HOST` | `0.0.0.0` | Bind address for `sites-api` |
| `SITES_DEFAULT_USER_ID` | `local` | Tenant the platform admin token is pinned to |
| `SITES_ACTING_SUBJECT_SALT_FILE` | empty | File holding the `X-Acting-Subject` HMAC salt |
| `SITES_DRIFT_RESYNC_SECONDS` | `60` | Interval at which the operator re-asserts already-converged resources |
| `SITES_VERIFY_TIMEOUT` | `5` | HTTP timeout, in seconds, for one readiness-verification request |
| `SITES_VERIFY_RETRY_SECONDS` | `30` | Minimum gap between the two verification failures that trigger rollback |
| `SITES_BUILD_DEADLINE_SECONDS` | `300` | Wall-clock bound on one BuildKit Job |
| `SITES_OPERATOR_METRICS_PORT` | `9090` | Operator admin/metrics port |
| `SITES_OPERATOR_HEALTH_STALE_SECONDS` | `300` | Age at which a reconcile sweep counts as stale |
| `SITES_OPERATOR_HEALTH_STALE_SWEEPS` | `5` | Missed sweeps before the operator reports unhealthy |
| `SITES_ACTIVATOR_PORT` | `8090` | Activator data port that receives woken traffic |
| `SITES_ACTIVATOR_ADMIN_PORT` | `9090` | Activator `healthz` and `scale-metrics` port |
| `SITES_ACTIVATOR_SERVICE` | `sites-activator` | Service name the gateway routes dormant traffic to |
| `SITES_ACTIVATOR_ROUTE_REFRESH` | `5` | Route-table refresh interval in seconds |
| `SITES_ACTIVATOR_ROUTE_STALE` | `60` | Age at which the route table is treated as stale |
| `SITES_ACTIVATOR_FORCE_REFRESH_MIN_SECONDS` | `1.0` | Floor between forced route refreshes |
| `SITES_ACTIVATOR_WAKE_TIMEOUT` | `30` | Total seconds to wait for a dormant site to become reachable |
| `SITES_ACTIVATOR_POLL_INTERVAL` | `0.25` | Poll interval while waiting for a wake |
| `SITES_ACTIVATOR_CONNECT_ATTEMPTS` | `6` | Upstream connection attempts after a wake |
| `SITES_ACTIVATOR_CONNECT_RETRY_DELAY` | `0.4` | Delay between those attempts |
| `SITES_ACTIVATOR_UPSTREAM_TIMEOUT` | `10` | Upstream request timeout once connected |
| `SITES_ACTIVATOR_IDLE_WINDOW` | `60` | Idle window reported to KEDA through `scale-metrics` |
| `SITES_ACTIVATOR_MAX_INFLIGHT` | `64` | Concurrent forwarded requests before shedding |
| `SITES_KEDA_POLLING_SECONDS` | `30` | KEDA `pollingInterval` written into generated ScaledObjects |
| `SITES_KEDA_COOLDOWN_SECONDS` | `300` | KEDA `cooldownPeriod` |
| `SITES_KEDA_INITIAL_COOLDOWN_SECONDS` | `1800` | Grace period before a newly created workload may scale to zero |
| `SITES_STZ_ROUTE_TIMEOUT` | `45s` | HTTPRoute request timeout on the scale-to-zero path |
| `SITES_GATEWAY_NAME` | `sites-gateway` | Gateway resource name |
| `SITES_GATEWAY_NAMESPACE` | `sites-gateway` | Namespace holding the Gateway resource |
| `SITES_GATEWAY_DATA_PLANE_NAMESPACE` | `envoy-gateway-system` | Namespace holding the Envoy data-plane Pods |
| `SITES_GATEWAY_NODE_PORT` | `30080` | NodePort the gateway listener is published on |
| `SITES_NODE_PORT_MIN` / `SITES_NODE_PORT_MAX` | `30080` / `30088` | Allocation pool for the `nodeport` exposure backend |
| `SITES_NODE_PORT_EXCLUDED` | `30081` | Ports removed from that pool |
| `SITES_HOST_PORT_BASE` | `18090` | Base host port used by the local reference topology |
| `SITES_SOURCE_PVC` | `sites-sources` | PVC that holds source packages when `SITES_SOURCE_BACKEND=pvc` |
| `SITES_SOURCE_ROOT` | `/var/lib/sites/sources` | Mount path for that PVC |
| `SITES_OSS_AUTH_SECRET` | `sites-oss-auth` | Secret holding object-storage credentials |
| `SITES_OSS_AUTH_MOUNT` | `/var/run/sites-oss` | Where that Secret is mounted |
| `SITES_TENANT_CPU_LIMIT` | `4` | Per-tenant namespace CPU quota |
| `SITES_TENANT_MEMORY_LIMIT` | `4Gi` | Per-tenant namespace memory quota |
| `SITES_TENANT_POD_LIMIT` | `16` | Per-tenant namespace Pod quota |

## Current cleanup and remaining work

Only canonical `SITES_*` environment variables and
`X-Sites-Service-Token` are accepted. The former `APPFORGE_*` and
`X-AppForge-Service-Token` aliases were removed to keep one diagnosable
configuration surface. Stored database migrations remain versioned because
removing data migrations would risk data loss; they are not runtime aliases.

Environment reads are still distributed among process-owned modules and many
are evaluated at import time. That is acceptable for immutable Pod
configuration but makes error messages inconsistent. A future typed settings
object should validate each process once at startup; it should not create a
runtime configuration API.
