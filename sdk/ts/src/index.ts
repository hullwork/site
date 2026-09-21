/**
 * `@hull/sdk`: the TypeScript client for an app built on a Hull Broker (contract sections
 * 2-3.4). Zero runtime dependencies -- everything here is node:crypto, node:fs, and the
 * global `fetch`/`Headers`/`ReadableStream` Node 20 ships built in.
 *
 * `createHull` wires together: an app-token exchange that re-reads the projected
 * ServiceAccount token file on every refresh (kubelet rotates it in place), local
 * end-user-token verification against the Broker's JWKS, a permissions/plan-feature
 * check cached from `GET /_hull/v1/app-config`, a `db.withUser` helper that sets the
 * Postgres session GUCs row-level-security policies read, and thin wrappers over the
 * flow/agent/tool/usage runtime endpoints.
 */
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import type { IncomingMessage, ServerResponse } from "node:http";

import { JwksCache, decodeCompact, verifyCompact, type Jwks } from "./jwt.js";
import { HullAuthError, HullError } from "./errors.js";
import { parseSse, type SseEvent } from "./sse.js";

export { HullAuthError, HullError } from "./errors.js";
export type { SseEvent } from "./sse.js";

/** An authenticated end user, as returned by `auth.requireUser`/`auth.verifyUserToken`. */
export interface HullUser {
  id: string;
  org: string | null;
  role: string;
  plan: string;
  /** The raw compact user JWS, forwarded to the Broker as `X-Hull-User` on later calls. */
  token: string;
}

export interface HullOptions {
  brokerUrl?: string;
  tokenFile?: string;
  appEnvId?: string;
  site?: string;
  now?: () => number;
  /** Minimum gap between JWKS refetches triggered by an unknown `kid` (see JwksCache). */
  jwksRefetchIntervalMs?: number;
}

interface AppTokenState {
  token: string;
  expiresAt: number;
}

interface AppConfig {
  version: number;
  roles: string[];
  default_role: string;
  permissions: Record<string, Record<string, { actions: string[] }>>;
  plans: Record<string, { features: string[]; limits: Record<string, number | null> }>;
  default_plan: string;
  entities: Record<string, unknown>;
  auth: Record<string, unknown>;
}

export interface PgLikeClient {
  query(text: string, params?: unknown[]): Promise<unknown>;
}

export interface FlowResult {
  output: unknown;
  fallback: boolean;
  reason?: string;
  usage: { credits: number };
}

export type NodeRouteHandler = (
  req: IncomingMessage,
  res: ServerResponse,
  user: HullUser,
) => void | Promise<void>;

export type BoundRouteHandler = (req: IncomingMessage, res: ServerResponse) => Promise<void>;

// Refresh the app token a little before it expires rather than exactly at expiry, so a
// slow request that started just under the wire does not get a token that dies mid-flight.
const APP_TOKEN_REFRESH_MARGIN_SECONDS = 30;

function jsonHeaders(): Headers {
  const headers = new Headers();
  headers.set("content-type", "application/json");
  return headers;
}

async function safeJson(response: Response): Promise<Record<string, unknown> | null> {
  try {
    return (await response.json()) as Record<string, unknown>;
  } catch {
    return null;
  }
}

function parseExpiry(value: string | number, now: () => number): number {
  if (typeof value === "number") return value;
  const parsed = Date.parse(value) / 1000;
  return Number.isFinite(parsed) ? parsed : now();
}

function readCookie(header: string | undefined, name: string): string | null {
  if (!header) return null;
  for (const part of header.split(";")) {
    const [key, ...rest] = part.trim().split("=");
    if (key === name) return rest.join("=");
  }
  return null;
}

async function webStreamToAsyncIterable(
  body: ReadableStream<Uint8Array>,
): Promise<AsyncGenerator<Uint8Array>> {
  const reader = body.getReader();
  async function* read(): AsyncGenerator<Uint8Array> {
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) return;
        if (value) yield value;
      }
    } finally {
      reader.releaseLock();
    }
  }
  return read();
}

export function createHull(options: HullOptions = {}) {
  const brokerUrl = (options.brokerUrl ?? process.env["HULL_BROKER_URL"] ?? "").replace(/\/+$/, "");
  const tokenFile = options.tokenFile ?? process.env["HULL_TOKEN_FILE"] ?? "";
  const appEnvId = options.appEnvId ?? process.env["HULL_APP_ENV_ID"] ?? "";
  void (options.site ?? process.env["HULL_SITE"] ?? ""); // accepted for symmetry with the env contract; unused by any call here
  const now = options.now ?? (() => Date.now() / 1000);

  if (!brokerUrl) {
    throw new HullError("HULL_BROKER_URL (or brokerUrl) is required to reach the Broker");
  }

  let appTokenState: AppTokenState | null = null;
  let appConfigCache: { config: AppConfig; expiresAt: number } | null = null;
  const cookieExchangeCache = new Map<string, { user: HullUser; expiresAt: number }>();

  async function fetchJwks(): Promise<Jwks> {
    const response = await fetch(`${brokerUrl}/_hull/.well-known/jwks.json`);
    if (!response.ok) {
      throw new HullError(`fetching the JWKS failed with status ${response.status}`, response.status);
    }
    return (await response.json()) as Jwks;
  }
  const jwksCache = new JwksCache(fetchJwks, options.jwksRefetchIntervalMs);

  async function getAppToken(): Promise<string> {
    if (appTokenState && appTokenState.expiresAt - APP_TOKEN_REFRESH_MARGIN_SECONDS > now()) {
      return appTokenState.token;
    }
    if (!tokenFile) {
      throw new HullError("HULL_TOKEN_FILE (or tokenFile) is required to mint an app token");
    }
    // Re-read on every exchange: kubelet rewrites the projected token file in place on
    // its own rotation schedule, independent of this process's request lifecycle.
    const serviceAccountToken = readFileSync(tokenFile, "utf8").trim();
    const response = await fetch(`${brokerUrl}/_hull/v1/app-token`, {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify({ serviceAccountToken }),
    });
    if (!response.ok) {
      throw new HullError(`app-token exchange failed with status ${response.status}`, response.status);
    }
    const body = (await response.json()) as { token: string; expiresAt: string | number };
    appTokenState = { token: body.token, expiresAt: parseExpiry(body.expiresAt, now) };
    return appTokenState.token;
  }

  async function authedFetch(
    path: string,
    init: (RequestInit & { user?: HullUser | undefined }) = {},
  ): Promise<Response> {
    const token = await getAppToken();
    const { user, headers: initHeaders, ...rest } = init;
    const headers = new Headers(initHeaders);
    headers.set("authorization", `Bearer ${token}`);
    if (user) headers.set("x-hull-user", user.token);
    return fetch(`${brokerUrl}${path}`, { ...rest, headers });
  }

  async function getAppConfig(): Promise<AppConfig> {
    if (appConfigCache && appConfigCache.expiresAt > now()) return appConfigCache.config;
    const response = await authedFetch("/_hull/v1/app-config");
    if (!response.ok) {
      throw new HullError(`app-config request failed with status ${response.status}`, response.status);
    }
    const config = (await response.json()) as AppConfig;
    const cacheControl = response.headers.get("cache-control") ?? "";
    const match = /max-age=(\d+)/.exec(cacheControl);
    const maxAge = match?.[1] ? Number(match[1]) : 0;
    appConfigCache = { config, expiresAt: now() + maxAge };
    return config;
  }

  const auth = {
    /** Verify a compact user JWS locally against the Broker's JWKS (contract section 2). */
    async verifyUserToken(token: string): Promise<HullUser> {
      if (!appEnvId) {
        throw new HullAuthError("HULL_APP_ENV_ID (or appEnvId) is required to verify a user token");
      }
      let kid: string | undefined;
      try {
        const header = decodeCompact(token).header;
        kid = typeof header["kid"] === "string" ? (header["kid"] as string) : undefined;
      } catch {
        throw new HullAuthError("malformed user token");
      }
      const jwks = await jwksCache.get(kid);
      let payload: Record<string, unknown>;
      try {
        payload = verifyCompact(token, jwks, { aud: appEnvId, typ: "user", now });
      } catch (err) {
        throw new HullAuthError(err instanceof Error ? err.message : "user token verification failed");
      }
      return {
        id: String(payload["sub"]),
        org: (payload["org"] as string | null | undefined) ?? null,
        role: String(payload["role"]),
        plan: String(payload["plan"]),
        token,
      };
    },

    /**
     * Resolve the caller for a Node request (contract section 3.1.1): an
     * `Authorization: Bearer` or `X-Hull-User` header is verified directly; otherwise, if
     * the `__Host-hull_session` cookie is present, this performs the server-to-server
     * exchange (path 2) and caches the result keyed by the SHA-256 of the cookie.
     */
    async requireUser(req: IncomingMessage): Promise<HullUser> {
      const authorization = req.headers["authorization"];
      if (typeof authorization === "string" && authorization.startsWith("Bearer ")) {
        return auth.verifyUserToken(authorization.slice("Bearer ".length));
      }
      const headerToken = req.headers["x-hull-user"];
      if (typeof headerToken === "string" && headerToken) {
        return auth.verifyUserToken(headerToken);
      }
      const cookieHeader = req.headers["cookie"];
      const cookie = readCookie(
        Array.isArray(cookieHeader) ? cookieHeader.join("; ") : cookieHeader,
        "__Host-hull_session",
      );
      if (!cookie) {
        throw new HullAuthError("no user credential on this request");
      }
      const cacheKey = createHash("sha256").update(cookie).digest("hex");
      const cached = cookieExchangeCache.get(cacheKey);
      if (cached && cached.expiresAt > now()) return cached.user;
      const hostHeader = req.headers["host"] ?? "";
      const response = await authedFetch("/_hull/auth/token", {
        method: "POST",
        headers: {
          cookie: `__Host-hull_session=${cookie}`,
          "x-hull-host": String(hostHeader),
        },
      });
      if (!response.ok) {
        throw new HullAuthError(`session exchange failed with status ${response.status}`);
      }
      const body = (await response.json()) as {
        user: { id: string; org: string | null; role: string; plan: string };
        token: string;
        expiresAt: string | number;
      };
      const user: HullUser = { ...body.user, token: body.token };
      cookieExchangeCache.set(cacheKey, { user, expiresAt: parseExpiry(body.expiresAt, now) });
      return user;
    },
  };

  /**
   * `"<entity>.<action>"` checks role permissions; a bare name with no `.` checks
   * whether the user's plan includes that feature (contract 3.1.2: "role permissions
   * plus plan features").
   */
  async function can(user: HullUser, permission: string): Promise<boolean> {
    const config = await getAppConfig();
    if (permission.includes(".")) {
      const [entity, action] = permission.split(".", 2) as [string, string | undefined];
      const grant = config.permissions[user.role]?.[entity];
      return Boolean(action && grant?.actions.includes(action));
    }
    return Boolean(config.plans[user.plan]?.features.includes(permission));
  }

  const db = {
    /**
     * Run *fn* inside a transaction with the Postgres session GUCs row-level-security
     * policies read set from *user* (contract's `hull.db.forUser`, "SDK" section of the
     * design doc). *client* is any object with a pg-compatible `query(text, params)`.
     */
    async withUser<T>(client: PgLikeClient, user: HullUser, fn: (client: PgLikeClient) => Promise<T>): Promise<T> {
      await client.query("BEGIN");
      try {
        await client.query(
          "SELECT set_config('hull.user_id',$1,true), set_config('hull.org_id',$2,true), set_config('hull.role',$3,true)",
          [user.id, user.org, user.role],
        );
        const result = await fn(client);
        await client.query("COMMIT");
        return result;
      } catch (err) {
        await client.query("ROLLBACK");
        throw err;
      }
    },
  };

  async function flow(
    name: string,
    input: unknown,
    opts: { user?: HullUser; idempotencyKey?: string } = {},
  ): Promise<FlowResult> {
    const headers = jsonHeaders();
    if (opts.idempotencyKey) headers.set("idempotency-key", opts.idempotencyKey);
    const response = await authedFetch(`/_hull/v1/flows/${encodeURIComponent(name)}/run`, {
      method: "POST",
      headers,
      body: JSON.stringify({ input }),
      ...(opts.user ? { user: opts.user } : {}),
    });
    if (!response.ok) {
      const body = await safeJson(response);
      throw new HullError(
        (body?.["error"] as string | undefined) ?? `flow run failed with status ${response.status}`,
        response.status,
        body?.["code"] as string | undefined,
      );
    }
    return (await response.json()) as FlowResult;
  }

  function agent(name: string) {
    return {
      /** An async iterator over the run's SSE events (contract 3.4). */
      stream(opts: { user?: HullUser; input: string; threadId?: string }): AsyncGenerator<SseEvent> {
        return (async function* (this: void) {
          const response = await authedFetch(`/_hull/v1/agents/${encodeURIComponent(name)}/runs`, {
            method: "POST",
            headers: jsonHeaders(),
            body: JSON.stringify({ threadId: opts.threadId, input: opts.input }),
            ...(opts.user ? { user: opts.user } : {}),
          });
          if (!response.ok || !response.body) {
            const body = await safeJson(response);
            throw new HullError(
              (body?.["error"] as string | undefined) ?? `agent run failed with status ${response.status}`,
              response.status,
              body?.["code"] as string | undefined,
            );
          }
          yield* parseSse(await webStreamToAsyncIterable(response.body));
        })();
      },

      async cancel(runId: string, opts: { user?: HullUser } = {}): Promise<void> {
        const response = await authedFetch(
          `/_hull/v1/agents/${encodeURIComponent(name)}/runs/${encodeURIComponent(runId)}/cancel`,
          { method: "POST", ...(opts.user ? { user: opts.user } : {}) },
        );
        if (!response.ok) {
          const body = await safeJson(response);
          throw new HullError(
            (body?.["error"] as string | undefined) ?? `cancel failed with status ${response.status}`,
            response.status,
            body?.["code"] as string | undefined,
          );
        }
      },
    };
  }

  const tools = {
    async call(
      alias: string,
      tool: string,
      args: unknown,
      opts: { user?: HullUser } = {},
    ): Promise<{ content: unknown; isError: boolean }> {
      const response = await authedFetch(`/_hull/v1/tools/${encodeURIComponent(alias)}/call`, {
        method: "POST",
        headers: jsonHeaders(),
        body: JSON.stringify({ tool, arguments: args }),
        ...(opts.user ? { user: opts.user } : {}),
      });
      const body = await safeJson(response);
      if (!response.ok) {
        if (response.status === 403 && body?.["code"] === "tool_denied") {
          throw new HullError(
            (body["reason"] as string | undefined) ?? "the tool gate denied this call",
            403,
            "tool_denied",
          );
        }
        throw new HullError(
          (body?.["error"] as string | undefined) ?? `tool call failed with status ${response.status}`,
          response.status,
          body?.["code"] as string | undefined,
        );
      }
      return body as unknown as { content: unknown; isError: boolean };
    },
  };

  const usage = {
    async me(user: HullUser): Promise<{ period: string; used: number; limit: number | null }> {
      const response = await authedFetch("/_hull/v1/usage/me", { user });
      if (!response.ok) {
        const body = await safeJson(response);
        throw new HullError(
          (body?.["error"] as string | undefined) ?? `usage request failed with status ${response.status}`,
          response.status,
        );
      }
      return (await response.json()) as { period: string; used: number; limit: number | null };
    },
  };

  /**
   * Node middleware helper implementing the design's "default deny routing": a route
   * that passes no permission refuses with 403 rather than silently skipping the check,
   * so a route can never forget to declare one.
   */
  function requireRoute(permission: string | undefined, handler: NodeRouteHandler): BoundRouteHandler {
    return async (req, res) => {
      if (!permission) {
        res
          .writeHead(403, { "content-type": "application/json" })
          .end(JSON.stringify({ error: "this route declares no permission and is refused by default" }));
        return;
      }
      let user: HullUser;
      try {
        user = await auth.requireUser(req);
      } catch (err) {
        const status = err instanceof HullAuthError ? err.status : 401;
        const message = err instanceof Error ? err.message : "authentication failed";
        res.writeHead(status, { "content-type": "application/json" }).end(JSON.stringify({ error: message }));
        return;
      }
      if (!(await can(user, permission))) {
        res.writeHead(403, { "content-type": "application/json" }).end(JSON.stringify({ error: "forbidden" }));
        return;
      }
      await handler(req, res, user);
    };
  }

  return { auth, can, db, flow, agent, tools, usage, requireRoute };
}

export type Hull = ReturnType<typeof createHull>;
