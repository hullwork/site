/**
* File module: All calls made by Sites Console to sites-api.
*
* Responsibility: Consolidate the HTTP form of contract §4 into a set of functions; no rendering, no caching, no automatic retries
* (The panel is already polling, and retrying will only double the number of requests during the failure period).
* Upstream: browser (same origin, product served by sites-api under `/console/`)
* Downstream: sites-api `/v1/*`, origin relative path - **CORS is not required, and do not add **.
* Contract: SITES_MULTI_MERCHANT_CONTRACT.md §4
* Authentication: an HttpOnly session cookie issued by `/v1/auth/*`. The console holds no
* credential of its own - there is nothing in sessionStorage for a script to read, and the
* break-glass token is sent exactly once, to `/v1/auth/local`. Unsafe requests echo the
* readable CSRF cookie in a header, because the session cookie alone rides along on
* cross-site requests.
* Failure handling: SitesApiError will be thrown if it is not 2xx; 401 will be downgraded back to the login page by the App.
 */

import { mockApi } from "./mock";
import type {
  AdminBuildListResponse,
  AdminDeploymentListResponse,
  AdminDeploymentView,
  AdminHealthResponse,
  AdminImageListResponse,
  MerchantKeyResponse,
  MerchantListResponse,
  TenantListResponse,
  TenantQuota,
  TenantTokenResponse,
  MetricsRange,
  MonitoringResponse,
} from "./types";

const CSRF_COOKIE = "sites_console_csrf";
const CSRF_HEADER = "X-Sites-Console-CSRF";

function consoleCsrf(): string {
  const item = document.cookie.split("; ").find((part) => part.startsWith(`${CSRF_COOKIE}=`));
  return item ? decodeURIComponent(item.split("=", 2)[1] ?? "") : "";
}

/** What this control plane accepts at the login page. Mirrors `GET /v1/auth/methods`. */
export interface AuthMethods {
  oidc: boolean;
  localLogin: boolean;
}

export class SitesApiError extends Error {
  readonly status: number;
  readonly body: unknown;

  constructor(status: number, message: string, body: unknown) {
    super(message);
    this.name = "SitesApiError";
    this.status = status;
    this.body = body;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const unsafe = Boolean(init?.method && init.method !== "GET");
  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      headers: {
        Accept: "application/json",
        ...(unsafe ? { [CSRF_HEADER]: consoleCsrf() } : {}),
        ...(init?.headers ?? {}),
      },
    });
  } catch (cause) {
    // fetch only rejects when the network layer fails. The panel and the API are from the same origin and process, so it is almost possible that
    // sites-api doesn't get up on its own - make it clear that it's "unable to connect to the control plane" rather than "request denied".
    // Otherwise, the troubleshooting direction will be wrong from the beginning.
    throw new SitesApiError(0, `Unable to connect to the Sites control plane: ${String(cause)}`, null);
  }
  const text = await response.text();
  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }
  if (!response.ok) {
    const detail =
      body && typeof body === "object" && "error" in body
        ? String((body as { error: unknown }).error)
        : response.statusText || `HTTP ${response.status}`;
    throw new SitesApiError(response.status, detail, body);
  }
  return body as T;
}

function jsonBody(payload: unknown): RequestInit {
  return {
    body: JSON.stringify(payload),
    headers: { "Content-Type": "application/json" },
  };
}

/** Only spell `?a=b` when necessary: an empty query will add a naked `?` to the path, making it difficult to read in the log. */
function withQuery(path: string, params: Record<string, string | number | undefined>): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") query.set(key, String(value));
  }
  const encoded = query.toString();
  return encoded ? `${path}?${encoded}` : path;
}

export interface MerchantInput {
  merchantId: string;
  displayName: string;
  maxTenants: number;
  maxDeployments: number;
}

export interface MerchantPatch {
  displayName?: string;
  maxTenants?: number;
  maxDeployments?: number;
  /** When only a portion is given, the server will merge it with the merchant's current range and will not reduce the ungiven portions back to the default value. */
  tenantQuota?: Partial<TenantQuota>;
}

export interface TenantInput {
  merchantId: string;
  userId: string;
  maxDeployments: number;
  maxPublicRoutes: number;
}

export interface TenantPatch {
  maxDeployments?: number;
  maxPublicRoutes?: number;
}

export interface DeploymentQuery {
  merchantId?: string;
  phase?: string;
  limit?: number;
}

export interface AdminDeploymentInput {
  merchantId: string;
  userId: string;
  name: string;
  image: string;
  port: number;
  healthPath: string;
  livenessPath?: string;
  exposure: "public" | "internal";
  memoryLimit?: string;
}

export interface SitesAdminApi {
  /** Which login paths this control plane has enabled. Unauthenticated on purpose. */
  authMethods(): Promise<AuthMethods>;
  /**
* Whether the session cookie is currently good for admin work. Hits `/v1/merchants` rather
* than `/v1/admin/health` - health can answer 200 while the control plane itself is broken,
* which would "log in" and then 401 on every page.
   */
  session(): Promise<void>;
  /** Break-glass: exchange the service token for a session. Refused when local login is off. */
  localLogin(token: string): Promise<void>;
  logout(): Promise<void>;
  health(): Promise<AdminHealthResponse>;
  listMerchants(): Promise<MerchantListResponse>;
  createMerchant(input: MerchantInput): Promise<MerchantKeyResponse>;
  updateMerchant(merchantId: string, patch: MerchantPatch): Promise<void>;
  rotateMerchantKey(merchantId: string): Promise<MerchantKeyResponse>;
  disableMerchant(merchantId: string): Promise<void>;
  listTenants(merchantId?: string): Promise<TenantListResponse>;
  createTenant(input: TenantInput): Promise<TenantTokenResponse>;
  updateTenant(
    merchantId: string,
    userId: string,
    patch: TenantPatch,
  ): Promise<void>;
  rotateTenantToken(
    merchantId: string,
    userId: string,
  ): Promise<TenantTokenResponse>;
  disableTenant(merchantId: string, userId: string): Promise<void>;
  listDeployments(query: DeploymentQuery): Promise<AdminDeploymentListResponse>;
  createDeployment(input: AdminDeploymentInput): Promise<AdminDeploymentView>;
  deleteDeployment(merchantId: string, userId: string, serviceName: string): Promise<void>;
  listBuilds(merchantId?: string): Promise<AdminBuildListResponse>;
  listImages(): Promise<AdminImageListResponse>;
  getClusterMetrics(range: MetricsRange): Promise<MonitoringResponse>;
  getApplicationMetrics(identity: { merchantId: string; userId: string; serviceName: string }, range: MetricsRange): Promise<MonitoringResponse>;
}

const liveApi: SitesAdminApi = {
  async authMethods() {
    return await request<AuthMethods>("/v1/auth/methods");
  },

  async session() {
    await request<MerchantListResponse>("/v1/merchants");
  },

  async localLogin(token) {
    await request("/v1/auth/local", { method: "POST", ...jsonBody({ token }) });
  },

  async logout() {
    await request("/v1/auth/logout", { method: "POST" });
  },

  async health() {
    try {
      return await request<AdminHealthResponse>("/v1/admin/health");
    } catch (error) {
      // The body of 503 contains "Which item is unreachable and why", which is the only information when the control plane fails.
      // The first scene cannot be swallowed as an ordinary error.
      if (
        error instanceof SitesApiError
        && error.body
        && typeof error.body === "object"
        && error.status !== 401
      ) {
        return error.body as AdminHealthResponse;
      }
      throw error;
    }
  },

  listMerchants() {
    return request<MerchantListResponse>("/v1/merchants");
  },

  createMerchant(input) {
    return request<MerchantKeyResponse>("/v1/merchants", {
      method: "POST",
      ...jsonBody(input),
    });
  },

  async updateMerchant(merchantId, patch) {
    await request(`/v1/merchants/${encodeURIComponent(merchantId)}`, {
      method: "PATCH",
      ...jsonBody(patch),
    });
  },

  rotateMerchantKey(merchantId) {
    return request<MerchantKeyResponse>(
      `/v1/merchants/${encodeURIComponent(merchantId)}/key`,
      { method: "POST" },
    );
  },

  async disableMerchant(merchantId) {
    await request(`/v1/merchants/${encodeURIComponent(merchantId)}`, {
      method: "DELETE",
    });
  },

  listTenants(merchantId) {
    return request<TenantListResponse>(withQuery("/v1/tenants", { merchantId }));
  },

  createTenant(input) {
    return request<TenantTokenResponse>("/v1/tenants", {
      method: "POST",
      ...jsonBody(input),
    });
  },

  // user_id is only unique within the merchant, so the positioning line must contain merchantId - if it is missing on the server, it will
  // 400 instead of "guessing one", there is no branch that omits this parameter in the frontend.
  async updateTenant(merchantId, userId, patch) {
    await request(
      withQuery(`/v1/tenants/${encodeURIComponent(userId)}`, { merchantId }),
      { method: "PATCH", ...jsonBody(patch) },
    );
  },

  rotateTenantToken(merchantId, userId) {
    return request<TenantTokenResponse>(
      withQuery(`/v1/tenants/${encodeURIComponent(userId)}/token`, { merchantId }),
      { method: "POST" },
    );
  },

  async disableTenant(merchantId, userId) {
    await request(
      withQuery(`/v1/tenants/${encodeURIComponent(userId)}`, { merchantId }),
      { method: "DELETE" },
    );
  },

  listDeployments(query) {
    return request<AdminDeploymentListResponse>(
      withQuery("/v1/admin/deployments", {
        merchantId: query.merchantId,
        phase: query.phase,
        limit: query.limit,
      }),
    );
  },

  createDeployment(input) {
    return request<AdminDeploymentView>("/v1/admin/deployments", {
      method: "POST",
      ...jsonBody(input),
    });
  },

  async deleteDeployment(merchantId, userId, serviceName) {
    await request(withQuery(
      `/v1/admin/deployments/${encodeURIComponent(serviceName)}`,
      { merchantId, userId },
    ), { method: "DELETE" });
  },

  listBuilds(merchantId) {
    return request<AdminBuildListResponse>(
      withQuery("/v1/admin/builds", { merchantId }),
    );
  },

  listImages() {
    return request<AdminImageListResponse>("/v1/admin/images");
  },

  getClusterMetrics(range) {
    return request<MonitoringResponse>(withQuery("/v1/admin/metrics/cluster", { range }));
  },

  getApplicationMetrics(identity, range) {
    return request<MonitoringResponse>(withQuery("/v1/admin/metrics/application", {
      merchantId: identity.merchantId,
      userId: identity.userId,
      serviceName: identity.serviceName,
      range,
    }));
  },
};

// The backend `/v1/admin/*` and `/v1/merchants*` are implemented in parallel by another way, before it is implemented UI
// You must also be able to run through and take screenshots independently. The switch is a build-time constant. When `VITE_USE_MOCK` is not "1", the entire
// Mock modules will be dropped by DCE - fake merchant names should not appear in production products.
const USE_MOCK = import.meta.env.VITE_USE_MOCK === "1";

export const api: SitesAdminApi = USE_MOCK ? mockApi : liveApi;
export { USE_MOCK };
