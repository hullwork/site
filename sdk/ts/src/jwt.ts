/**
 * Local EdDSA (Ed25519) compact-JWS verification against a JWKS (contract section 2).
 *
 * This is the whole point of "local verification": the SDK never calls the Broker to
 * check a user token it already holds, it checks the signature itself with the public
 * key from the JWKS. node:crypto has native Ed25519 support (verify() with a JWK-derived
 * KeyObject), so this needs no runtime dependency.
 */
import { createPublicKey, verify as cryptoVerify, type KeyObject } from "node:crypto";

export class JwtError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "JwtError";
  }
}

export interface Jwk {
  kty: string;
  crv: string;
  x: string;
  kid: string;
  use?: string;
  alg?: string;
}

export interface Jwks {
  keys: Jwk[];
}

export interface DecodedCompact {
  header: Record<string, unknown>;
  payload: Record<string, unknown>;
  signingInput: string;
  signature: Buffer;
}

function b64urlDecode(part: string): Buffer {
  return Buffer.from(part, "base64url");
}

/** Split and base64url-decode a compact JWS without checking the signature. */
export function decodeCompact(token: string): DecodedCompact {
  const parts = token.split(".");
  if (parts.length !== 3) {
    throw new JwtError("not a compact JWS: expected three dot-separated parts");
  }
  const [headerPart, payloadPart, signaturePart] = parts as [string, string, string];
  let header: Record<string, unknown>;
  let payload: Record<string, unknown>;
  try {
    header = JSON.parse(b64urlDecode(headerPart).toString("utf8"));
    payload = JSON.parse(b64urlDecode(payloadPart).toString("utf8"));
  } catch {
    throw new JwtError("token header or payload is not valid JSON");
  }
  return {
    header,
    payload,
    signingInput: `${headerPart}.${payloadPart}`,
    signature: b64urlDecode(signaturePart),
  };
}

function publicKeyFromJwk(jwk: Jwk): KeyObject {
  return createPublicKey({
    key: { kty: jwk.kty, crv: jwk.crv, x: jwk.x },
    format: "jwk",
  });
}

export interface VerifyOptions {
  /** Expected `aud` claim: `"hull-broker"` for an app token, the appEnvId for a user token. */
  aud: string;
  /** Expected `typ` claim: `"app"` or `"user"`. */
  typ: string;
  /** Expected `iss` claim; the Broker issues `"hull-broker"`. */
  iss?: string;
  /** Clock-skew leeway, seconds (contract default 5, `HULL_TOKEN_LEEWAY_SECONDS`). */
  leewaySeconds?: number;
  now?: () => number;
}

/** Verify a compact JWS against *jwks* and the contract's claim rules; returns the payload. */
export function verifyCompact(
  token: string,
  jwks: Jwks,
  options: VerifyOptions,
): Record<string, unknown> {
  const { header, payload, signingInput, signature } = decodeCompact(token);
  if (header["alg"] !== "EdDSA") {
    throw new JwtError("unsupported alg; expected EdDSA");
  }
  const kid = header["kid"];
  if (typeof kid !== "string") {
    throw new JwtError("token header is missing kid");
  }
  const jwk = jwks.keys.find((key) => key.kid === kid);
  if (!jwk) {
    throw new JwtError(`unknown signing key: ${kid}`);
  }
  const ok = cryptoVerify(null, Buffer.from(signingInput, "utf8"), publicKeyFromJwk(jwk), signature);
  if (!ok) {
    throw new JwtError("signature verification failed");
  }
  // Same issuer check the Broker applies (tokens.py): a JWKS shared with another
  // issuer must not make its tokens acceptable here.
  if (payload["iss"] !== (options.iss ?? "hull-broker")) {
    throw new JwtError("unexpected token issuer");
  }
  if (payload["typ"] !== options.typ) {
    throw new JwtError(`unexpected token typ; expected ${options.typ}`);
  }
  if (payload["aud"] !== options.aud) {
    throw new JwtError("token audience does not match this app");
  }
  const now = (options.now ?? (() => Date.now() / 1000))();
  const leeway = options.leewaySeconds ?? 5;
  const exp = payload["exp"];
  if (typeof exp !== "number" || now > exp + leeway) {
    throw new JwtError("token has expired");
  }
  return payload;
}

/**
 * Caches a JWKS document and refetches it when a `kid` it does not recognize is asked
 * for, so a key rotation on the Broker does not require restarting every app process.
 *
 * The `kid` comes from a token header that has not been verified yet, so anyone who
 * can reach the app could otherwise turn each request carrying a made-up `kid` into
 * a request to the Broker. Refetches for unknown keys are therefore at most one per
 * `minRefetchIntervalMs`, and concurrent callers share one fetch. The default is
 * provisional: it bounds how long a newly rotated key is refused, and can be changed
 * through `createHull({ jwksRefetchIntervalMs })`.
 */
export const DEFAULT_JWKS_REFETCH_INTERVAL_MS = 30_000;

export class JwksCache {
  private jwks: Jwks | null = null;
  private fetchedAt = -Infinity;
  private inflight: Promise<Jwks> | null = null;

  constructor(
    private readonly fetchJwks: () => Promise<Jwks>,
    private readonly minRefetchIntervalMs: number = DEFAULT_JWKS_REFETCH_INTERVAL_MS,
    private readonly nowMs: () => number = () => Date.now(),
  ) {}

  async get(kid?: string): Promise<Jwks> {
    const known = this.jwks !== null && (!kid || this.jwks.keys.some((key) => key.kid === kid));
    if (known) {
      return this.jwks as Jwks;
    }
    if (this.jwks !== null && this.nowMs() - this.fetchedAt < this.minRefetchIntervalMs) {
      return this.jwks;
    }
    if (!this.inflight) {
      this.inflight = this.fetchJwks()
        .then((jwks) => {
          this.jwks = jwks;
          this.fetchedAt = this.nowMs();
          return jwks;
        })
        .finally(() => {
          this.inflight = null;
        });
    }
    return this.inflight;
  }
}
