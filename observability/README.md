# Observability artifacts

site exposes its own telemetry and ships the artifacts needed to consume
it from a Prometheus you already run.

## What the three processes expose

| Process | Endpoint | Purpose |
|---|---|---|
| `sites-api` | `:8080/metrics` `/healthz` `/readyz` | `/healthz` is liveness (constant 200); `/readyz` reports database, Kubernetes and snapshot age, and only the database turns it red |
| `sites-operator` | `:9090/metrics` `/healthz` | `/healthz` is loop progress, not "a sweep finished recently" |
| `sites-activator` | admin `:9090/metrics` `/healthz` `/livez` | `/livez` is liveness; `/healthz` is readiness and stays green on a stale-but-populated route table |

Logs are structured through `src/sites/telemetry.py`. Set `SITES_LOG_FORMAT=json`
in production and `SITES_LOG_LEVEL=debug` to get one line per HTTP request. They
go to stderr - stdio MCP reserves stdout for JSON-RPC frames - and nothing
forwards them anywhere; point your collector at the container.

🔴 **No series carries a tenant or site identifier.** Request metrics are
labelled by route template only (`src/sites/api.py::_route_template`, unmatched
paths collapse to `other`), and everything else by `kind`/`outcome`/`dependency`
enumerations. The activator's admin port also serves `/scale-metrics`, which
*does* take a host name - keep that port behind the NetworkPolicy in
`charts/site/templates/09-activator.yaml`.

## Files

| File | Use |
|---|---|
| `alerts/sites-rules.yaml` | **generated** - `kubectl apply`, or lift `.spec.groups` into `rule_files` |
| `scrape/servicemonitor.yaml` | three ServiceMonitors plus equivalent static `scrape_config`s |
| `dashboards/sites-control-plane.json` | import into Grafana |
| `scripts/render-rules.py` | regenerates `alerts/sites-rules.yaml` from the rendered chart |

`alerts/sites-rules.yaml` is derived from the `sites-alerts.yml` key of the
`sites-prometheus-config` ConfigMap in `charts/site/templates/11-monitoring.yaml`, which is
the source of truth. `test_monitoring.py` fails if the two drift, so an operator
importing the artifact gets exactly the rules site runs against itself.
Rules select on `sites-(api|operator|activator).*` and `sites-envoy-gateway.*`,
which matches both the bundled job names (suffixed `-local`) and jobs named after
the Services.

## The Prometheus in `charts/site/templates/11-monitoring.yaml` is a product dependency

This repository does ship a Prometheus, and it is the one exception to the rule
below. `src/sites/monitoring.py` *queries* it to render the admin console's
cluster and application charts, so it is a feature dependency, not a collection
stack: without it the console's metrics tabs report "metrics backend
unavailable".

Point `SITES_PROMETHEUS_URL` at an existing Prometheus and the bundled
Deployment becomes optional - it needs the scrape targets in
`prometheus.yml` and the recording of `sites_*` and cadvisor series that
`monitoring.py` queries.

## Embedded panels in the console (optional)

The console can render the dashboard above inline, as same-origin iframes. It is
off unless configured, and it is an **operator** feature: the metrics carry no
tenant dimension, so every panel is cross-tenant data and only an administrator
is offered it - enforced server-side on every request, not by hiding a tab.

| Variable | Required | Meaning |
|---|---|---|
| `SITES_GRAFANA_URL` | yes | `http(s)://` origin of your Grafana. No credentials, query or fragment. |
| `SITES_GRAFANA_TOKEN` / `SITES_GRAFANA_TOKEN_FILE` | yes | Service-account token. **Viewer**, scoped to the folder holding this dashboard. |
| `SITES_GRAFANA_DATASOURCE_UID` | yes | The datasource the panels query. |
| `SITES_GRAFANA_DASHBOARD_UID` | no | Defaults to the shipped dashboard's uid. |
| `SITES_GRAFANA_ORG_ID` | no | Defaults to 1. |

All three of the first group or nothing: with a URL and no token the iframe fills
with 401s, which is worse than an absent tab, and without the datasource uid the
proxy cannot bound what `/api/ds/query` may reach (below).

### What the proxy will and will not forward

Requests go to ``/grafana/`` on this origin; the browser never holds a Grafana
credential and never learns a Grafana address. ``src/sites/grafana_proxy.py`` holds a **closed
allowlist** of the paths a solo panel needs - the panel document, Grafana's own
bundle, its frontend settings, the dashboard model, plugin settings, and
`POST /api/ds/query`. Everything else is 403.

🔴 The allowlist is a code constant on purpose. Proxying `/grafana/*` wholesale
while attaching the service-account token would republish the entire Grafana API
- including `/api/datasources/proxy/...`, which is "run any query against any
datasource" - to every administrator of this console. A configurable allowlist is
the same hole with a delay on it.

🔴 `POST /api/ds/query` is checked in the **body**, not just by URL. It is
Grafana's generic query endpoint and dispatches on a datasource uid inside the
request, so it is only read-only for the datasource it names: a Grafana with a
SQL datasource attached would otherwise turn that one endpoint into arbitrary
SQL, and a folder-scoped Viewer does not stop it because OSS Grafana does not
scope datasource permissions by folder. The proxy parses the body and refuses any
query naming a uid other than the configured one - and refuses the whole request,
not just the offending query, because forwarding the rest would still have run it.

Credentials do not cross in either direction: the console session cookie is
dropped before the request leaves, and Grafana's `Set-Cookie` is dropped before
the response returns.

### Framing policy

The console's own CSP is untouched - still `default-src 'self'` with
`frame-ancestors 'none'`, and no `frame-src`. The proxied panel response carries
its own narrower policy with `frame-ancestors 'self'` and
`X-Frame-Options: SAMEORIGIN`, because that response *is* the framed document and
the console's `'none'` would make the browser refuse to render our own iframe.

## Collection and tracing boundary

The optional reference Chart can install the small Prometheus described above
because the console queries it. This directory is not a general observability
stack: it contains no Loki, Grafana, Alertmanager, notification routing, or
long-term metrics store. Operators may disable the bundled Prometheus and point
`SITES_PROMETHEUS_URL` at their existing collector.

W3C `traceparent` is validated and propagated across API, Kubernetes client,
operator, activator, storage and outbound calls, and the trace id is attached to
structured logs. When `SITES_OTLP_HTTP_ENDPOINT` is set, site also emits real
OTLP/HTTP JSON spans: API and activator requests are `SERVER` spans; Kubernetes,
storage and forwarding hops are children; `SiteDeployment`/`SiteBuild` resources carry
the context into the asynchronous operator reconcile span. No endpoint means no exporter
thread and no required tracing dependency.

The exporter is deliberately one bounded daemon: request threads only use `put_nowait`,
batches default to 128 spans, the queue defaults to 2048, and collector failures are not
retried in the request path. Loss is visible through the low-cardinality
`sites_tracing_export_total{outcome}` counter and `SitesTraceExportDrops` alert. Configure:

```yaml
# Helm values
tracing:
  enabled: true
  endpoint: http://tempo.monitoring.svc:4318
```

Replace the `sites-tracing` literals rendered by
`charts/site/templates/12-tracing.yaml`. The endpoint may be the collector root or its
`/v1/traces` path. Queue, batch, flush and timeout settings map to the documented
`SITES_OTLP_*` environment variables.

The split is deliberate:

| | Owner | Lives in |
|---|---|---|
| Instrumentation (`/metrics`, structured logs, health endpoints, optional OTLP spans) | this repository | the service code |
| Artifacts describing how to consume it (alerts, dashboards, scrape config) | this repository | `observability/` |
| Collector choice, long-term storage and notification channels | the operator | wherever they like |

Metrics remain pull-based. Traces are push-based only when the operator explicitly
supplies a standard OTLP/HTTP endpoint; site does not bundle or require a backend.
