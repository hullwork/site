/**
 * Error types the SDK throws. Kept separate from index.ts so browser.ts (which never
 * touches node:crypto or node:http) can import just the error shapes it needs.
 */

/** The caller has no usable end-user credential on this request (contract 3.1.1/3.2). */
export class HullAuthError extends Error {
  readonly status = 401;

  constructor(message: string) {
    super(message);
    this.name = "HullAuthError";
  }
}

/** Anything else the Broker, or this SDK's own request building, refuses. */
export class HullError extends Error {
  readonly status?: number;
  readonly code?: string;

  constructor(message: string, status?: number, code?: string) {
    super(message);
    this.name = "HullError";
    this.status = status;
    this.code = code;
  }
}
