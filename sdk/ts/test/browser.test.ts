/**
 * browser.ts calls `fetch` with a same-origin relative path, which only resolves against
 * a real page origin -- something Node has no equivalent of. These tests replace
 * `globalThis.fetch` with a recording stub instead of standing up a fake broker, which is
 * enough to prove the request/response and caching logic without needing a browser
 * environment.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { signIn, getUserToken, signOut } from "../src/browser.js";

interface RecordedCall {
  path: string;
  init?: RequestInit;
}

function stubFetch(responses: Record<string, { status: number; body: unknown }>) {
  const calls: RecordedCall[] = [];
  const original = globalThis.fetch;
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    const path = String(input);
    calls.push({ path, ...(init ? { init } : {}) });
    const entry = responses[path];
    if (!entry) throw new Error(`unexpected fetch to ${path}`);
    // The Fetch spec forbids a body on a "null body status" response (204/205/304);
    // undici enforces this, and the real /_hull/auth/logout response actually is a
    // bodyless 204.
    const nullBodyStatus = entry.status === 204 || entry.status === 205 || entry.status === 304;
    return new Response(nullBodyStatus ? null : JSON.stringify(entry.body), {
      status: entry.status,
      headers: nullBodyStatus ? {} : { "content-type": "application/json" },
    });
  }) as typeof fetch;
  return { calls, restore: () => { globalThis.fetch = original; } };
}

test("signIn.start posts the email and does not throw on the always-202 response", async () => {
  const { calls, restore } = stubFetch({
    "/_hull/auth/email/start": { status: 202, body: { status: "sent" } },
  });
  try {
    await signIn.start("person@example.test");
    assert.equal(calls.length, 1);
    assert.equal(calls[0]?.init?.method, "POST");
    assert.equal(calls[0]?.init?.body, JSON.stringify({ email: "person@example.test" }));
  } finally {
    restore();
  }
});

test("signIn.verify returns the user and caches the token for getUserToken", async () => {
  const expiresAt = Math.floor(Date.now() / 1000) + 600;
  const { restore } = stubFetch({
    "/_hull/auth/email/verify": {
      status: 200,
      body: {
        user: { id: "u1", email: "person@example.test", org: null, role: "member", plan: "free" },
        token: "signed-user-token",
        expiresAt,
      },
    },
  });
  try {
    const user = await signIn.verify("person@example.test", "123456");
    assert.equal(user.id, "u1");
    const token = await getUserToken(); // should be served from cache, not a new fetch
    assert.equal(token, "signed-user-token");
  } finally {
    restore();
  }
});

test("getUserToken refetches once the cached token has expired", async () => {
  const past = Math.floor(Date.now() / 1000) - 10;
  const { calls: firstCalls, restore: restore1 } = stubFetch({
    "/_hull/auth/email/verify": {
      status: 200,
      body: {
        user: { id: "u1", email: "person@example.test", org: null, role: "member", plan: "free" },
        token: "stale-token",
        expiresAt: past,
      },
    },
  });
  await signIn.verify("person@example.test", "123456");
  restore1();
  void firstCalls;

  const future = Math.floor(Date.now() / 1000) + 600;
  const { calls: secondCalls, restore: restore2 } = stubFetch({
    "/_hull/auth/token": {
      status: 200,
      body: {
        user: { id: "u1", email: "person@example.test", org: null, role: "member", plan: "free" },
        token: "fresh-token",
        expiresAt: future,
      },
    },
  });
  try {
    const token = await getUserToken();
    assert.equal(token, "fresh-token");
    assert.equal(secondCalls.length, 1);
  } finally {
    restore2();
  }
});

test("signOut clears the cached token and hits the logout endpoint", async () => {
  const expiresAt = Math.floor(Date.now() / 1000) + 600;
  const { restore: restore1 } = stubFetch({
    "/_hull/auth/email/verify": {
      status: 200,
      body: {
        user: { id: "u1", email: "person@example.test", org: null, role: "member", plan: "free" },
        token: "signed-user-token",
        expiresAt,
      },
    },
  });
  await signIn.verify("person@example.test", "123456");
  restore1();

  const { calls, restore: restore2 } = stubFetch({
    "/_hull/auth/logout": { status: 204, body: {} },
    "/_hull/auth/token": { status: 200, body: { user: {}, token: "should-not-be-used", expiresAt } },
  });
  try {
    await signOut();
    assert.equal(calls.length, 1);
    assert.equal(calls[0]?.path, "/_hull/auth/logout");
  } finally {
    restore2();
  }
});

test("a non-ok response's error message is surfaced", async () => {
  const { restore } = stubFetch({
    "/_hull/auth/email/verify": { status: 401, body: { error: "invalid_code" } },
  });
  try {
    await assert.rejects(signIn.verify("person@example.test", "000000"), /invalid_code/);
  } finally {
    restore();
  }
});
