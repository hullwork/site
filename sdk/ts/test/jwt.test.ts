import { test } from "node:test";
import assert from "node:assert/strict";
import { generateKeyPairSync, randomUUID, sign as cryptoSign, type KeyObject } from "node:crypto";

import { decodeCompact, verifyCompact, JwksCache, JwtError, type Jwks } from "../src/jwt.js";

function b64url(input: Buffer | string): string {
  return Buffer.from(input).toString("base64url");
}

function makeSigner() {
  const { publicKey, privateKey } = generateKeyPairSync("ed25519");
  // A fresh kid per signer, not a fixed "k1": the multi-key JwksCache test below tells
  // two keys apart by kid, and a shared literal would make them indistinguishable.
  const kid = randomUUID();
  const jwk = publicKey.export({ format: "jwk" }) as Record<string, unknown>;
  const jwks: Jwks = { keys: [{ ...(jwk as object), kid, use: "sig", alg: "EdDSA" } as Jwks["keys"][number]] };
  function sign(payload: Record<string, unknown>, headerOverrides: Record<string, unknown> = {}): string {
    const header = { alg: "EdDSA", typ: "JWT", kid, ...headerOverrides };
    const signingInput = `${b64url(JSON.stringify(header))}.${b64url(JSON.stringify(payload))}`;
    const signature = cryptoSign(null, Buffer.from(signingInput, "utf8"), privateKey as KeyObject);
    return `${signingInput}.${b64url(signature)}`;
  }
  return { jwks, sign, kid };
}

test("decodeCompact rejects a token without three parts", () => {
  assert.throws(() => decodeCompact("only.two"), JwtError);
});

test("verifyCompact accepts a well-formed, correctly signed token", () => {
  const { jwks, sign } = makeSigner();
  const now = Math.floor(Date.now() / 1000);
  const token = sign({ typ: "user", aud: "app-env-1", sub: "u1", exp: now + 600, iat: now });
  const payload = verifyCompact(token, jwks, { aud: "app-env-1", typ: "user" });
  assert.equal(payload["sub"], "u1");
});

test("verifyCompact rejects a bad signature", () => {
  const { jwks, sign } = makeSigner();
  const now = Math.floor(Date.now() / 1000);
  const token = sign({ typ: "user", aud: "app-env-1", sub: "u1", exp: now + 600 });
  const tampered = token.slice(0, -4) + "AAAA";
  assert.throws(() => verifyCompact(tampered, jwks, { aud: "app-env-1", typ: "user" }), JwtError);
});

test("verifyCompact rejects the wrong audience", () => {
  const { jwks, sign } = makeSigner();
  const now = Math.floor(Date.now() / 1000);
  const token = sign({ typ: "user", aud: "someone-else", sub: "u1", exp: now + 600 });
  assert.throws(() => verifyCompact(token, jwks, { aud: "app-env-1", typ: "user" }), JwtError);
});

test("verifyCompact rejects the wrong typ", () => {
  const { jwks, sign } = makeSigner();
  const now = Math.floor(Date.now() / 1000);
  const token = sign({ typ: "app", aud: "app-env-1", sub: "u1", exp: now + 600 });
  assert.throws(() => verifyCompact(token, jwks, { aud: "app-env-1", typ: "user" }), JwtError);
});

test("verifyCompact rejects an expired token, honoring leeway", () => {
  const { jwks, sign } = makeSigner();
  const now = Math.floor(Date.now() / 1000);
  const token = sign({ typ: "user", aud: "app-env-1", sub: "u1", exp: now - 100 });
  assert.throws(() => verifyCompact(token, jwks, { aud: "app-env-1", typ: "user", leewaySeconds: 5 }), JwtError);
  // A leeway that covers the gap lets the same token through.
  const payload = verifyCompact(token, jwks, { aud: "app-env-1", typ: "user", leewaySeconds: 200 });
  assert.equal(payload["sub"], "u1");
});

test("verifyCompact rejects an unknown kid", () => {
  const { jwks, sign } = makeSigner();
  const now = Math.floor(Date.now() / 1000);
  const token = sign({ typ: "user", aud: "app-env-1", sub: "u1", exp: now + 600 }, { kid: "no-such-key" });
  assert.throws(() => verifyCompact(token, jwks, { aud: "app-env-1", typ: "user" }), JwtError);
});

test("JwksCache refetches when asked for an unknown kid", async () => {
  const { jwks: firstJwks } = makeSigner();
  const { jwks: secondJwks, sign: signWithSecond, kid: secondKid } = makeSigner();
  let fetches = 0;
  const cache = new JwksCache(async () => {
    fetches += 1;
    return fetches === 1 ? firstJwks : { keys: [...firstJwks.keys, ...secondJwks.keys] };
  });

  await cache.get(); // first fetch, only knows the first key
  assert.equal(fetches, 1);

  const now = Math.floor(Date.now() / 1000);
  const tokenFromSecondKey = signWithSecond({ typ: "user", aud: "a", sub: "u1", exp: now + 600 });
  const decoded = await cache.get(secondKid);
  assert.equal(fetches, 2); // refetched because the cached JWKS did not have this kid
  assert.ok(decoded.keys.some((k) => k.kid === secondKid));

  // A second lookup for a key it already knows about does not refetch again.
  await cache.get(secondKid);
  assert.equal(fetches, 2);
});
