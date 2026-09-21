/**
 * Browser-side helper (contract 3.1.1 path 1 and 3.2): calls the app's own origin, which
 * resolves to the Broker gateway through the app's `Host` header (section 3.1), and keeps
 * the returned user token in memory only -- the session cookie itself is HttpOnly and
 * never readable from here.
 */

export interface BrowserUser {
  id: string;
  email: string;
  org: string | null;
  role: string;
  plan: string;
}

interface TokenResponse {
  user: BrowserUser;
  token: string;
  expiresAt: string | number;
}

let cachedToken: { token: string; expiresAt: number } | null = null;

function parseExpiry(value: string | number): number {
  if (typeof value === "number") return value;
  return Date.parse(value) / 1000;
}

async function request<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method: "POST",
    ...(body !== undefined ? { headers: { "content-type": "application/json" }, body: JSON.stringify(body) } : {}),
    credentials: "same-origin",
  });
  const parsed = (await response.json().catch(() => null)) as (T & { error?: string }) | null;
  if (!response.ok) {
    throw new Error(parsed?.error ?? `request to ${path} failed with status ${response.status}`);
  }
  return parsed as T;
}

export const signIn = {
  /** `POST /_hull/auth/email/start` -- always succeeds; never reveals whether the address exists. */
  async start(email: string): Promise<void> {
    await request("/_hull/auth/email/start", { email });
  },

  /** `POST /_hull/auth/email/verify` -- caches the returned user token until it expires. */
  async verify(email: string, code: string): Promise<BrowserUser> {
    const result = await request<TokenResponse>("/_hull/auth/email/verify", { email, code });
    cachedToken = { token: result.token, expiresAt: parseExpiry(result.expiresAt) };
    return result.user;
  },
};

/** Returns a cached user token, refreshing it from the session cookie when it has expired. */
export async function getUserToken(): Promise<string> {
  if (cachedToken && cachedToken.expiresAt > Date.now() / 1000) {
    return cachedToken.token;
  }
  const result = await request<TokenResponse>("/_hull/auth/token");
  cachedToken = { token: result.token, expiresAt: parseExpiry(result.expiresAt) };
  return cachedToken.token;
}

export async function signOut(): Promise<void> {
  await request("/_hull/auth/logout");
  cachedToken = null;
}
