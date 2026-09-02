/**
* Data contract: Sites control plane management view. [Source: SITES_MULTI_MERCHANT_CONTRACT.md §4]
*
* Responsibility: Map the JSON of sites-api to TS type as it is; do not fill in the default value or do unit conversion.
* Constraints: Field names are aligned verbatim in the contract §4, **No name change is allowed to accommodate a certain version** - five agents
* In parallel, field name misalignment is the only failure mode that cannot be found by typecheck.
* Boundary: The field marked `?` means "This version of the server may not have been provided yet", not "can be omitted". The render layer must
* There is a default copy, so you cannot rely on `!`. The timestamp is an ISO string (a product of `_iso_timestamp`),
* Not Unix seconds - intentionally different from sandbox/console's Portal view.
 */

/** Deploy phase seven states. Any new phase is defined by the server, and the frontend is only responsible for the backend copywriting. */
export type DeploymentPhase =
  | "Pending"
  | "Building"
  | "Deploying"
  | "Running"
  | "Failed"
  | "Deleting"
  | "Deleted";

/**
* Evidence detected by the control plane itself (not Kubernetes’ readiness judgment, nor agent readme).
* Must say "failed" when ok=false, even if the phase is running.
 */
export interface DeploymentVerification {
  ok: boolean;
  httpStatus?: number | null;
  bodySha256?: string | null;
  error?: string | null;
}

// --- Merchant ------------------------------------------------------------------

/** GET elements of /v1/merchants. The count field is the quota usage, not the DB row count (Contract §2.5). */
export interface MerchantView {
  merchantId: string;
  displayName: string;
  maxTenants: number;
  maxDeployments: number;
  /**Number of tenants under your name. It is undefined when not reported by the server. Do not display it as 0. */
  tenantCount?: number;
  /** Number of **active** deployments under your name (tombstones not included). Same as above, undefined ≠ 0. */
  deploymentCount?: number;
  createdAt?: string | null;
  disabledAt?: string | null;
  /**
* The resource limit of each tenant Namespace under this merchant name. Server-side constant echo—not configured separately
* The merchant provides a deployment-level default value, so there is no need to distinguish between "not configured" and "equipped with a default value", it is
* "How much can this merchant actually use?"
   */
  tenantQuota?: TenantQuota;
}

/** Three segments of ResourceQuota. The value is a Kubernetes dimensional string (`4` / `500m` / `2Gi`),
* is not a number - the frontend does not parse it, displays it as it is, and returns it as it is. */
export interface TenantQuota {
  cpu: string;
  memory: string;
  pods: string;
}

export interface MerchantListResponse {
  merchants: MerchantView[];
  count?: number;
}

/**
* Responses to POST /v1/merchants and POST /v1/merchants/{id}/key.
*
* 🔴 `apiKey` is **clear text and only appears this time** - the library only has sha256. The frontend must not write it into
* Any persistent storage (not sessionStorage), only in the React state of the one-time pop-up window
* Stay until user closes. `apiKeyShownOnce` is given explicitly by the server, and the frontend renders the warning accordingly.
* Do not rely on the frontend to remember by itself (Contract §4.3).
 */
export interface MerchantKeyResponse {
  merchantId: string;
  apiKey: string;
  apiKeyShownOnce: boolean;
  displayName?: string;
  maxTenants?: number;
  maxDeployments?: number;
}

// --- Tenant ------------------------------------------------------------------

/**
* GET elements of /v1/tenants.
*
* user_id is only unique within the merchant (Contract §0), so `userId` alone does not constitute the identity of a row,
* Any row must be positioned with `merchantId`.
 */
export interface TenantView {
  merchantId: string;
  userId: string;
  maxDeployments: number;
  maxPublicRoutes: number;
  createdAt?: string | null;
  disabledAt?: string | null;
}

export interface TenantListResponse {
  tenants: TenantView[];
  count?: number;
}

/**
* Responses to POST /v1/tenants and POST /v1/tenants/{id}/token.
* `token` has the same nature as the merchant apiKey: the clear text is only available once, and the library only contains the abstract.
 */
export interface TenantTokenResponse {
  merchantId: string;
  userId: string;
  token: string;
  /** Rotation interface exclusive: This line was originally disabled, but was re-enabled during the update. */
  reenabled?: boolean;
  maxDeployments?: number;
  maxPublicRoutes?: number;
  note?: string;
}

// ---Admin console aggregation ------------------------------------------------------------------

/**
* Elements of GET /v1/admin/deployments.
*
* Go to `list_all_deployments`, which reads the **database snapshot** instead of the Kubernetes authoritative source.
* Staleness is specified by `snapshotAgeSeconds` in the response.
* `verification` is optional because there is no status full text in the snapshot table - if the server does not
* If this is added to the aggregation, the admin console will not be able to get the evidence (there is no single detail endpoint in the admin scope).
 */
export interface AdminDeploymentView {
  /** CR name. Different from serviceName: the former contains merchant/user summary and is globally unique. */
  name?: string | null;
  merchantId: string;
  userId: string;
  serviceName: string;
  phase: DeploymentPhase | string;
  ready?: boolean;
  /** Whether there is currently a worker. Detached from lifecycle phase: The phase of the dormant site is still Running. */
  runtimeState?: "Active" | "Dormant" | "Unknown" | string;
  scaleToZero?: boolean;
  observedReplicas?: number | null;
  message?: string;
  url?: string | null;
  image?: string | null;
  port?: number | null;
  healthPath?: string | null;
  revision?: string | null;
  createdAt?: string | null;
  updatedAt?: string | null;
  deletionRequestedAt?: string | null;
  verification?: DeploymentVerification | null;
  artifactSha256?: string | null;
}

export interface AdminDeploymentListResponse {
  deployments: AdminDeploymentView[];
  count?: number;
  /**
* The number of seconds since the last time the Kubernetes snapshot was synchronized on the control plane, determined by the **server** at the moment of response
* Calculated well, not affected by browser clock.
* undefined = This version of the server does not report this field and does not make staleness judgment;
* null = The server clearly stated that "synchronization has not been successful even once". The two cannot be merged, otherwise the old control plane will be
* Has been falsely reported as a shutdown.
   */
  snapshotAgeSeconds?: number | null;
}

/** Element of GET /v1/admin/builds. The reason for failure is in `message`. */
export interface AdminBuildView {
  name?: string | null;
  merchantId: string;
  userId: string;
  serviceName?: string;
  phase: DeploymentPhase | string;
  message?: string;
  image?: string | null;
  imageDigest?: string | null;
  jobName?: string | null;
  revision?: string | null;
}

export interface AdminBuildListResponse {
  builds: AdminBuildView[];
  count?: number;
}

/** GET /v1/admin/images: `_catalog` of local registry + tag of each repo. */
export interface ImageTagView {
  tag: string;
  digest?: string | null;
}

export interface ImageRepositoryView {
  name: string;
  tags?: ImageTagView[];
  /** The reason why the tag of the repo cannot be listed (the registry part must be available and can be displayed). */
  error?: string | null;
}

export interface AdminImageListResponse {
  repositories: ImageRepositoryView[];
  count?: number;
  /** Digest query is limited by the number of times and the wall clock budget; when true, null digest does not mean that the image is damaged. */
  digestsTruncated?: boolean;
  /** When the entire registry is unreachable, repositories are empty and the reasons are explained here. */
  registry?: HealthProbe;
}

// ---Health ------------------------------------------------------------------

/**
* Covenant §4.4 Shape of crucifixion. Each item may be `{reachable:false, error:"..."}`——
* The server does not display the entire 500 due to a single item failure. The frontend must also render items one by one. The screen cannot be white because one item is missing:
* The administrator comes to this page exactly when something goes wrong.
 */
export interface HealthProbe {
  reachable: boolean;
  error?: string | null;
}

export interface AdminHealthResponse {
  database?: HealthProbe & {
    backend?: string;
    snapshotAgeSeconds?: number | null;
  };
  operator?: HealthProbe & { lastReconcileSeconds?: number | null };
  registry?: HealthProbe & { repositoryCount?: number };
  kubernetes?: HealthProbe & { version?: string };
  /**
   * Embedded Grafana panels, when the operator configured one. Rides on the
   * admin health call rather than a route of its own: this response is already
   * admin-only, and the panels are an operator view because the metrics behind
   * them carry no tenant dimension.
   */
  grafana?: GrafanaCapability;
}

/** One embeddable panel of the shipped dashboard. */
export interface GrafanaPanel {
  id: number;
  title: string;
}

/**
 * Data contract for the `grafana` block of `GET /v1/admin/health`. Source:
 * src/sites/grafana_proxy.py::capabilities.
 *
 * `enabled: false` is the normal case for a deployment with no Grafana; the
 * console renders nothing and the repository stays deployable without one.
 */
export interface GrafanaCapability {
  enabled: boolean;
  /** Same-origin prefix sites-api proxies panels on. Never a Grafana address. */
  route?: string;
  dashboardUid?: string;
  panels?: GrafanaPanel[];
}

export type MetricsRange = "1h" | "6h" | "24h";

export interface MetricPoint {
  timestamp: number;
  value: number;
}

export interface MetricSeries {
  id: "cpu" | "memory" | "requests" | "errors" | "latencyP95" | string;
  label: string;
  unit: "cores" | "bytes" | "req/s" | "percent" | "ms" | string;
  points: MetricPoint[];
}

export interface MonitoringResponse {
  scope: "cluster" | "application";
  range: { key: MetricsRange; start: number; end: number; stepSeconds: number };
  source: { available: boolean; sampledAt: string; retention: string; error?: string };
  identity?: { merchantId: string; userId: string; serviceName: string } | null;
  summary: Record<string, number | null>;
  series: MetricSeries[];
}
