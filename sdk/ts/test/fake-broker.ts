/**
 * A minimal stand-in for the Hull Broker (contract sections 2-3.4), used only by the SDK
 * tests. It signs real Ed25519 JWTs with a keypair generated per test run -- the SDK's
 * `auth.verifyUserToken` verifies signatures locally, so a fake that merely echoed back
 * an opaque string would never exercise that code path.
 *
 * Not exported by the package; lives under test/ so `dist/test/fake-broker.js` never
 * ships in `files` (package.json only publishes `dist/src`).
 */
import { createServer, type IncomingMessage, type Server } from "node:http";
import { generateKeyPairSync, sign as cryptoSign, type KeyObject } from "node:crypto";

export interface FakeUser {
  id: string;
  org: string | null;
  role: string;
  plan: string;
}

export interface FakeBroker {
  url: string;
  kid: string;
  jwks: { keys: Array<Record<string, unknown>> };
  signUserToken(user: FakeUser, appEnvId: string, overrides?: Record<string, unknown>): string;
  /** Requests this fake has received, for assertions (headers, bodies). */
  requests: Array<{ method: string; path: string; headers: Record<string, string | string[] | undefined> }>;
  toolDenied: { alias: string; tool: string } | null;
  appConfig: Record<string, unknown>;
  stop(): Promise<void>;
}

function b64url(input: Buffer | string): string {
  return Buffer.from(input).toString("base64url");
}

async function readBody(req: IncomingMessage): Promise<Record<string, unknown>> {
  const chunks: Buffer[] = [];
  for await (const chunk of req) chunks.push(chunk as Buffer);
  if (!chunks.length) return {};
  try {
    return JSON.parse(Buffer.concat(chunks).toString("utf8"));
  } catch {
    return {};
  }
}

export async function startFakeBroker(): Promise<FakeBroker> {
  const { publicKey, privateKey } = generateKeyPairSync("ed25519");
  const kid = "test-key-1";
  const publicJwk = publicKey.export({ format: "jwk" }) as Record<string, unknown>;
  const jwks = { keys: [{ ...publicJwk, kid, use: "sig", alg: "EdDSA" }] };

  function sign(payload: Record<string, unknown>, typ: string): string {
    const header = { alg: "EdDSA", typ: "JWT", kid };
    const signingInput = `${b64url(JSON.stringify(header))}.${b64url(JSON.stringify({ typ, ...payload }))}`;
    const signature = cryptoSign(null, Buffer.from(signingInput, "utf8"), privateKey as KeyObject);
    return `${signingInput}.${b64url(signature)}`;
  }

  function signUserToken(user: FakeUser, appEnvId: string, overrides: Record<string, unknown> = {}): string {
    const nowSeconds = Math.floor(Date.now() / 1000);
    return sign(
      {
        iss: "hull-broker",
        aud: appEnvId,
        sub: user.id,
        sid: "sid-fake",
        org: user.org,
        role: user.role,
        plan: user.plan,
        env: "dev",
        iat: nowSeconds,
        exp: nowSeconds + 600,
        ...overrides,
      },
      "user",
    );
  }

  const requests: FakeBroker["requests"] = [];
  let toolDenied: FakeBroker["toolDenied"] = null;
  const appConfig: Record<string, unknown> = {
    version: 1,
    roles: ["member"],
    default_role: "member",
    permissions: { member: { tickets: { actions: ["read"] } } },
    plans: { free: { features: ["chat"], limits: { ai_credits_month: 200 } } },
    default_plan: "free",
    entities: {},
    auth: {},
  };

  const server: Server = createServer((req, res) => {
    void (async () => {
      const url = new URL(req.url ?? "/", "http://localhost");
      requests.push({ method: req.method ?? "GET", path: url.pathname, headers: req.headers });

      const send = (status: number, body: unknown, extraHeaders: Record<string, string> = {}) => {
        const raw = JSON.stringify(body);
        res.writeHead(status, { "content-type": "application/json", ...extraHeaders });
        res.end(raw);
      };

      if (url.pathname === "/_hull/.well-known/jwks.json" && req.method === "GET") {
        send(200, jwks);
        return;
      }
      if (url.pathname === "/_hull/v1/app-token" && req.method === "POST") {
        await readBody(req);
        send(200, { token: "fake-app-token", expiresAt: Math.floor(Date.now() / 1000) + 600 });
        return;
      }
      if (url.pathname === "/_hull/auth/token" && req.method === "POST") {
        const user: FakeUser = { id: "u" + "1".repeat(32), org: null, role: "member", plan: "free" };
        const token = signUserToken(user, "aaaaaaaabbbbbbbbccccccccdddddddd");
        send(200, {
          user: { ...user, email: "person@example.test" },
          token,
          expiresAt: Math.floor(Date.now() / 1000) + 600,
        });
        return;
      }
      if (url.pathname === "/_hull/v1/app-config" && req.method === "GET") {
        send(200, appConfig, { "cache-control": "max-age=60" });
        return;
      }
      if (url.pathname.startsWith("/_hull/v1/flows/") && url.pathname.endsWith("/run") && req.method === "POST") {
        const body = await readBody(req);
        send(200, { output: { summary: `summary of ${JSON.stringify(body.input)}` }, fallback: false, usage: { credits: 3 } });
        return;
      }
      if (/^\/_hull\/v1\/agents\/[^/]+\/runs$/.test(url.pathname) && req.method === "POST") {
        res.writeHead(200, { "content-type": "text/event-stream" });
        res.write('event: run.started\ndata: {"runId":"run-1","threadId":"thread-1"}\n\n');
        res.write(": keep-alive comment\n\n");
        res.write('event: message.delta\ndata: {"text":"Hel');
        res.write('lo"}\n\n');
        res.write('event: run.finished\ndata: {"usage":{"credits":2}}\n\n');
        res.end();
        return;
      }
      if (/^\/_hull\/v1\/agents\/[^/]+\/runs\/[^/]+\/cancel$/.test(url.pathname) && req.method === "POST") {
        res.writeHead(202);
        res.end();
        return;
      }
      if (/^\/_hull\/v1\/tools\/[^/]+\/call$/.test(url.pathname) && req.method === "POST") {
        const body = await readBody(req);
        const alias = url.pathname.split("/")[4] ?? "";
        if (toolDenied && toolDenied.alias === alias && toolDenied.tool === body["tool"]) {
          send(403, { code: "tool_denied", reason: "risk exceeds the caller's grant" });
          return;
        }
        send(200, { content: { echoed: body["arguments"] }, isError: false });
        return;
      }
      if (url.pathname === "/_hull/v1/usage/me" && req.method === "GET") {
        send(200, { period: "2026-09", used: 12, limit: 200 });
        return;
      }
      if (url.pathname === "/_hull/auth/email/start" && req.method === "POST") {
        send(202, { status: "sent" });
        return;
      }
      if (url.pathname === "/_hull/auth/email/verify" && req.method === "POST") {
        const user: FakeUser = { id: "u" + "2".repeat(32), org: null, role: "member", plan: "free" };
        send(200, {
          user: { ...user, email: "person@example.test" },
          token: signUserToken(user, "aaaaaaaabbbbbbbbccccccccdddddddd"),
          expiresAt: Math.floor(Date.now() / 1000) + 600,
        });
        return;
      }
      if (url.pathname === "/_hull/auth/logout" && req.method === "POST") {
        res.writeHead(204);
        res.end();
        return;
      }
      send(404, { error: "not_found" });
    })();
  });

  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("fake broker failed to bind a port");

  return {
    url: `http://127.0.0.1:${address.port}`,
    kid,
    jwks,
    signUserToken,
    requests,
    get toolDenied() {
      return toolDenied;
    },
    set toolDenied(value: { alias: string; tool: string } | null) {
      toolDenied = value;
    },
    appConfig,
    async stop() {
      await new Promise<void>((resolve, reject) => server.close((err) => (err ? reject(err) : resolve())));
    },
  };
}
