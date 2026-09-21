import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";

import { createHull, HullAuthError, HullError, type HullUser, type PgLikeClient } from "../src/index.js";
import { startFakeBroker, type FakeBroker } from "./fake-broker.js";

const APP_ENV_ID = "aaaaaaaabbbbbbbbccccccccdddddddd";

function tokenFilePath(content = "fake-service-account-token"): string {
  const dir = mkdtempSync(join(tmpdir(), "hull-sdk-test-"));
  const file = join(dir, "token");
  writeFileSync(file, content, "utf8");
  return file;
}

async function withBroker(fn: (broker: FakeBroker) => Promise<void>): Promise<void> {
  const broker = await startFakeBroker();
  try {
    await fn(broker);
  } finally {
    await broker.stop();
  }
}

function fixedUser(overrides: Partial<HullUser> = {}, token = "irrelevant-for-most-calls"): HullUser {
  return { id: "u" + "3".repeat(32), org: null, role: "member", plan: "free", token, ...overrides };
}

test("getAppToken exchanges the projected token file and reuses it until near expiry", async () => {
  await withBroker(async (broker) => {
    const hull = createHull({ brokerUrl: broker.url, tokenFile: tokenFilePath(), appEnvId: APP_ENV_ID });
    await hull.usage.me(fixedUser());
    await hull.usage.me(fixedUser());
    const tokenExchanges = broker.requests.filter((r) => r.path === "/_hull/v1/app-token");
    assert.equal(tokenExchanges.length, 1, "the second call should reuse the cached app token");
  });
});

test("authed requests carry the app token as Authorization: Bearer", async () => {
  await withBroker(async (broker) => {
    const hull = createHull({ brokerUrl: broker.url, tokenFile: tokenFilePath(), appEnvId: APP_ENV_ID });
    await hull.usage.me(fixedUser());
    const usageCall = broker.requests.find((r) => r.path === "/_hull/v1/usage/me");
    assert.equal(usageCall?.headers["authorization"], "Bearer fake-app-token");
  });
});

test("auth.verifyUserToken accepts a token the fake broker's key really signed", async () => {
  await withBroker(async (broker) => {
    const hull = createHull({ brokerUrl: broker.url, tokenFile: tokenFilePath(), appEnvId: APP_ENV_ID });
    const token = broker.signUserToken({ id: "u1", org: "o1", role: "org_admin", plan: "pro" }, APP_ENV_ID);
    const user = await hull.auth.verifyUserToken(token);
    assert.deepEqual(user, { id: "u1", org: "o1", role: "org_admin", plan: "pro", token });
  });
});

test("auth.verifyUserToken rejects a token signed for a different appEnvId", async () => {
  await withBroker(async (broker) => {
    const hull = createHull({ brokerUrl: broker.url, tokenFile: tokenFilePath(), appEnvId: APP_ENV_ID });
    const token = broker.signUserToken({ id: "u1", org: null, role: "member", plan: "free" }, "some-other-app");
    await assert.rejects(() => hull.auth.verifyUserToken(token), HullAuthError);
  });
});

test("auth.requireUser reads Authorization: Bearer", async () => {
  await withBroker(async (broker) => {
    const hull = createHull({ brokerUrl: broker.url, tokenFile: tokenFilePath(), appEnvId: APP_ENV_ID });
    const token = broker.signUserToken({ id: "u1", org: null, role: "member", plan: "free" }, APP_ENV_ID);
    const req = { headers: { authorization: `Bearer ${token}` } } as unknown as IncomingMessage;
    const user = await hull.auth.requireUser(req);
    assert.equal(user.id, "u1");
  });
});

test("auth.requireUser reads X-Hull-User when there is no Authorization header", async () => {
  await withBroker(async (broker) => {
    const hull = createHull({ brokerUrl: broker.url, tokenFile: tokenFilePath(), appEnvId: APP_ENV_ID });
    const token = broker.signUserToken({ id: "u1", org: null, role: "member", plan: "free" }, APP_ENV_ID);
    const req = { headers: { "x-hull-user": token } } as unknown as IncomingMessage;
    const user = await hull.auth.requireUser(req);
    assert.equal(user.id, "u1");
  });
});

test("auth.requireUser exchanges the session cookie server-to-server and caches by its sha-256", async () => {
  await withBroker(async (broker) => {
    const hull = createHull({ brokerUrl: broker.url, tokenFile: tokenFilePath(), appEnvId: APP_ENV_ID });
    const req = {
      headers: { cookie: "__Host-hull_session=opaque-session-value", host: "app.example.test" },
    } as unknown as IncomingMessage;
    const user = await hull.auth.requireUser(req);
    assert.ok(user.id);
    const first = broker.requests.filter((r) => r.path === "/_hull/auth/token").length;
    await hull.auth.requireUser(req);
    const second = broker.requests.filter((r) => r.path === "/_hull/auth/token").length;
    assert.equal(first, second, "the second call with the same cookie should hit the cache, not the broker");
  });
});

test("auth.requireUser refuses a request with no credential at all", async () => {
  await withBroker(async (broker) => {
    const hull = createHull({ brokerUrl: broker.url, tokenFile: tokenFilePath(), appEnvId: APP_ENV_ID });
    const req = { headers: {} } as unknown as IncomingMessage;
    await assert.rejects(() => hull.auth.requireUser(req), HullAuthError);
  });
});

test("can() checks role permissions for an entity.action string", async () => {
  await withBroker(async (broker) => {
    const hull = createHull({ brokerUrl: broker.url, tokenFile: tokenFilePath(), appEnvId: APP_ENV_ID });
    const user = fixedUser({ role: "member", plan: "free" });
    assert.equal(await hull.can(user, "tickets.read"), true);
    assert.equal(await hull.can(user, "tickets.update"), false);
    assert.equal(await hull.can(user, "tickets.read"), true); // app-config is cached; still correct on repeat
  });
});

test("can() checks plan features for a bare permission name", async () => {
  await withBroker(async (broker) => {
    const hull = createHull({ brokerUrl: broker.url, tokenFile: tokenFilePath(), appEnvId: APP_ENV_ID });
    const user = fixedUser({ plan: "free" });
    assert.equal(await hull.can(user, "chat"), true);
    assert.equal(await hull.can(user, "auto_triage"), false);
  });
});

test("db.withUser sets the RLS session GUCs, commits on success, rolls back on error", async () => {
  await withBroker(async (broker) => {
    const hull = createHull({ brokerUrl: broker.url, tokenFile: tokenFilePath(), appEnvId: APP_ENV_ID });
    const calls: Array<{ text: string; params?: unknown[] }> = [];
    const client: PgLikeClient = {
      async query(text, params) {
        calls.push({ text, ...(params ? { params } : {}) });
        return { rows: [] };
      },
    };
    const user = fixedUser({ id: "u9", org: "o9", role: "org_admin" });

    const result = await hull.db.withUser(client, user, async (c) => {
      await c.query("SELECT 1");
      return 42;
    });
    assert.equal(result, 42);
    assert.deepEqual(
      calls.map((c) => c.text),
      ["BEGIN", "SELECT set_config('hull.user_id',$1,true), set_config('hull.org_id',$2,true), set_config('hull.role',$3,true)", "SELECT 1", "COMMIT"],
    );
    assert.deepEqual(calls[1]?.params, ["u9", "o9", "org_admin"]);

    calls.length = 0;
    await assert.rejects(
      hull.db.withUser(client, user, async () => {
        throw new Error("boom");
      }),
      /boom/,
    );
    assert.deepEqual(
      calls.map((c) => c.text),
      ["BEGIN", "SELECT set_config('hull.user_id',$1,true), set_config('hull.org_id',$2,true), set_config('hull.role',$3,true)", "ROLLBACK"],
    );
  });
});

test("flow() posts input and returns output/fallback/usage, forwarding Idempotency-Key", async () => {
  await withBroker(async (broker) => {
    const hull = createHull({ brokerUrl: broker.url, tokenFile: tokenFilePath(), appEnvId: APP_ENV_ID });
    const result = await hull.flow(
      "summarize_ticket",
      { title: "printer on fire" },
      { user: fixedUser(), idempotencyKey: "idem-1" },
    );
    assert.equal(result.fallback, false);
    assert.deepEqual(result.usage, { credits: 3 });
    const call = broker.requests.find((r) => r.path === "/_hull/v1/flows/summarize_ticket/run");
    assert.equal(call?.headers["idempotency-key"], "idem-1");
    assert.equal(call?.headers["x-hull-user"], fixedUser().token);
  });
});

test("agent(name).stream() yields the run's SSE events as they arrive", async () => {
  await withBroker(async (broker) => {
    const hull = createHull({ brokerUrl: broker.url, tokenFile: tokenFilePath(), appEnvId: APP_ENV_ID });
    const events: Array<{ event: string; data: unknown }> = [];
    for await (const event of hull.agent("triage").stream({ user: fixedUser(), input: "help" })) {
      events.push(event);
    }
    assert.deepEqual(
      events.map((e) => e.event),
      ["run.started", "message.delta", "run.finished"],
    );
    assert.deepEqual(events[0]?.data, { runId: "run-1", threadId: "thread-1" });
    assert.deepEqual(events[1]?.data, { text: "Hello" });
  });
});

test("agent(name).cancel() posts to the cancel endpoint", async () => {
  await withBroker(async (broker) => {
    const hull = createHull({ brokerUrl: broker.url, tokenFile: tokenFilePath(), appEnvId: APP_ENV_ID });
    await hull.agent("triage").cancel("run-1", { user: fixedUser() });
    const call = broker.requests.find((r) => r.path === "/_hull/v1/agents/triage/runs/run-1/cancel");
    assert.ok(call);
  });
});

test("tools.call() returns content on success", async () => {
  await withBroker(async (broker) => {
    const hull = createHull({ brokerUrl: broker.url, tokenFile: tokenFilePath(), appEnvId: APP_ENV_ID });
    const result = await hull.tools.call("kb", "search_docs", { q: "printer" }, { user: fixedUser() });
    assert.equal(result.isError, false);
    assert.deepEqual(result.content, { echoed: { q: "printer" } });
  });
});

test("tools.call() surfaces a tool_denied gate refusal as a typed error", async () => {
  await withBroker(async (broker) => {
    broker.toolDenied = { alias: "kb", tool: "delete_everything" };
    const hull = createHull({ brokerUrl: broker.url, tokenFile: tokenFilePath(), appEnvId: APP_ENV_ID });
    await assert.rejects(
      hull.tools.call("kb", "delete_everything", {}, { user: fixedUser() }),
      (err: unknown) => err instanceof HullError && err.code === "tool_denied" && err.status === 403,
    );
  });
});

test("usage.me() forwards X-Hull-User and returns the usage envelope", async () => {
  await withBroker(async (broker) => {
    const hull = createHull({ brokerUrl: broker.url, tokenFile: tokenFilePath(), appEnvId: APP_ENV_ID });
    const result = await hull.usage.me(fixedUser());
    assert.deepEqual(result, { period: "2026-09", used: 12, limit: 200 });
  });
});

// --- requireRoute (default-deny routing) -----------------------------------------------

async function withRoutedServer(
  handler: (req: IncomingMessage, res: ServerResponse) => void | Promise<void>,
  fn: (baseUrl: string) => Promise<void>,
): Promise<void> {
  const server = createServer((req, res) => void handler(req, res));
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("test server failed to bind");
  try {
    await fn(`http://127.0.0.1:${address.port}`);
  } finally {
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
}

test("requireRoute refuses with 403 when the route declares no permission", async () => {
  await withBroker(async (broker) => {
    const hull = createHull({ brokerUrl: broker.url, tokenFile: tokenFilePath(), appEnvId: APP_ENV_ID });
    const route = hull.requireRoute(undefined, () => {
      throw new Error("handler must not run when no permission was declared");
    });
    await withRoutedServer(route, async (baseUrl) => {
      const response = await fetch(baseUrl);
      assert.equal(response.status, 403);
      const body = (await response.json()) as { error: string };
      assert.match(body.error, /declares no permission/);
    });
  });
});

test("requireRoute refuses with 401 when there is no user credential", async () => {
  await withBroker(async (broker) => {
    const hull = createHull({ brokerUrl: broker.url, tokenFile: tokenFilePath(), appEnvId: APP_ENV_ID });
    const route = hull.requireRoute("tickets.read", () => {
      throw new Error("handler must not run without a user");
    });
    await withRoutedServer(route, async (baseUrl) => {
      const response = await fetch(baseUrl);
      assert.equal(response.status, 401);
    });
  });
});

test("requireRoute refuses with 403 when the user's role lacks the permission", async () => {
  await withBroker(async (broker) => {
    const hull = createHull({ brokerUrl: broker.url, tokenFile: tokenFilePath(), appEnvId: APP_ENV_ID });
    const token = broker.signUserToken({ id: "u1", org: null, role: "member", plan: "free" }, APP_ENV_ID);
    const route = hull.requireRoute("tickets.update", () => {
      throw new Error("handler must not run without the permission");
    });
    await withRoutedServer(route, async (baseUrl) => {
      const response = await fetch(baseUrl, { headers: { authorization: `Bearer ${token}` } });
      assert.equal(response.status, 403);
    });
  });
});

test("requireRoute runs the handler when the permission is granted", async () => {
  await withBroker(async (broker) => {
    const hull = createHull({ brokerUrl: broker.url, tokenFile: tokenFilePath(), appEnvId: APP_ENV_ID });
    const token = broker.signUserToken({ id: "u1", org: null, role: "member", plan: "free" }, APP_ENV_ID);
    const route = hull.requireRoute("tickets.read", (_req, res, user) => {
      res.writeHead(200, { "content-type": "application/json" }).end(JSON.stringify({ userId: user.id }));
    });
    await withRoutedServer(route, async (baseUrl) => {
      const response = await fetch(baseUrl, { headers: { authorization: `Bearer ${token}` } });
      assert.equal(response.status, 200);
      const body = (await response.json()) as { userId: string };
      assert.equal(body.userId, "u1");
    });
  });
});
