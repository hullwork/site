# Authentication contract

**Audience:** authors of any client that calls this control plane — a CLI, a CI job, an
agent runtime, another platform, a browser. **Prerequisites:** a reachable `sites-api` and a
credential issued by its operator. **Supported topology:** every deployment of this
repository; nothing here depends on who the caller is.

site is released and operated on its own. It has no sibling component, no privileged
integrator, and no client whose requests mean more than any other client's. This document is
the whole of what a caller may assume about identity.

## 1. The rule everything else follows from

> **The merchant is decided by the credential. A caller can never name the merchant, the
> tenant, or the user it acts as.**

Identity here has two parts, `(merchant, tenant)`, where a tenant's `user_id` is unique only
*within* a merchant. Both parts come from the credential you present.

🔴 `X-Merchant-ID` and `X-User-ID` are **refused with 403**, not ignored — including when the
value happens to agree with the credential. Older versions of this API accepted them (from a
platform admin token plus a shared proxy secret), so a client written against that behaviour
fails on its first request here.

Failing is the point. Consider what "ignored" would have looked like for that client: it
sends `X-Merchant-ID: b`, the write lands in merchant `a` because that is what its credential
says, and the answer is `201` with a normal body. The resource exists. Nothing errored. It is
simply in a tenancy the caller never intended and does not look at — and both merchants are
real, so nobody gets a "not found" either. A refusal costs one round trip and cannot be
misread; the silent version is a cross-tenant write that surfaces whenever someone eventually
compares the two.

Refusing an *agreeing* value is the same argument one step earlier: if a matching header were
let through, the header would be a supported input, and a client would carry it around until
the day the values differ.

The one thing a caller may still declare is *which subject inside its own merchant* it is
acting for, and only with an explicit grant. See §4.

## 2. Ways to authenticate

| Credential | Who it is for | How it travels |
|---|---|---|
| Merchant API key | Machines: clients, automation, other platforms | `X-Sites-Service-Token: sitem_…` |
| Tenant token | A single tenant, usually issued for a person or one workload | `X-Sites-Service-Token: site_…` |
| Console session | Browsers, established by OIDC or by the break-glass login | `Cookie: sites_console_session=…` |
| Platform admin token | The operator, break-glass only | `X-Sites-Service-Token: <service token>` |

The two `X-Sites-Service-Token` credential kinds share one header on purpose, and the server
resolves them by looking in both tables. **Do not treat the `sitem_` / `site_` prefix as
routing information**: it exists so a human reading a log can tell them apart, and a client
that branches on it will break when the prefix changes.

### Merchant API key (the normal machine credential)

Issued by the operator with `POST /v1/merchants`; the plaintext is returned exactly once, at
creation, and the control plane stores only its SHA-256 digest. Rotate with
`POST /v1/merchants/{merchantId}/key`, which returns a new plaintext key, invalidates the
previous one immediately, and restarts the lifetime.

**Keys expire.** The response of both endpoints carries `keyExpiresAt`; the default lifetime
is 90 days and can be set per merchant with `keyTtlSeconds` at creation. An expired key is
refused with exactly the same `401 invalid service token` as an unknown one — the endpoint
will not confirm that a key digest ever existed, so a client cannot use the error to
distinguish "expired" from "wrong". Read `keyExpiresAt` and rotate before it passes.

### Tenant token

Issued with `POST /v1/tenants`, returned once, rotated with
`POST /v1/tenants/{userId}/token?merchantId=…`. It resolves to exactly one tenant row and
can never act for another (§4).

### Console session (browsers)

`GET /v1/auth/methods` returns `{"oidc": bool, "localLogin": bool}` — which doors this
deployment has. It needs no credential, because a login page cannot be drawn without it.

* **OIDC** — `GET /v1/auth/login` redirects into the deployment's own identity provider
  (Authorization Code + PKCE). The provider returns to `GET /v1/auth/callback`, which issues
  the session and redirects to `/console/`. Each deployment configures its own provider,
  client and **audience**; an ID token minted for a different service is refused.
* **Break-glass local login** — `POST /v1/auth/local` with `{"token": "<service token>"}`
  trades the platform admin token for an admin session. It bypasses the identity provider
  entirely, so **every attempt is written to the audit log** with its source address, and an
  operator may switch the whole path off (`403 local login is disabled`).

A session is an `HttpOnly` cookie plus a readable `sites_console_csrf` cookie. Every unsafe
request (`POST`, `PATCH`, `PUT`, `DELETE`) must echo that value in `X-Sites-Console-CSRF` or
it is not treated as authenticated. `POST /v1/auth/logout` clears both.

### Platform admin token

The operator's break-glass credential. It resolves to one pinned identity,
`(local, local)`, and **cannot act for anyone else**. It is not an API key: it carries no
grants, so it is refused if it tries to impersonate. An operator may disable it entirely
(§2, local login), in which case it stops being accepted on every endpoint, not just at the
login form.

### The MCP endpoint (`POST /mcp`)

The MCP tool surface is served on this same API and authenticates through this same
contract - it is not a second door, and the four credentials above are the four it has.
Two things are specific to that route:

* Only a token credential is accepted. A **console session cookie is refused**
  (`401 mcp_token_credential_required`), because an ambient credential on an endpoint that
  deploys is a CSRF surface, and the browser has the console for the same operations.
* The acting subject is carried by `X-Acting-Subject` only. A subject named in the tool
  arguments (`_agent_user_id`, a reserved argument of the stdio transport) is **refused**,
  never applied and never quietly dropped - see §4 and [AGENT_CONTRACT.md](AGENT_CONTRACT.md).

Everything else - what decides the merchant, when revocation takes effect, what each
refusal means - is the rest of this document, unchanged.

## 3. Revocation is immediate, and it is re-checked per request

On **every** request the control plane re-reads the merchant and tenant rows behind the
credential. There is no cached authorization and no grace period, so a client observes:

* merchant disabled → `403 merchant is disabled` on the tenant-token path, and
  `401 invalid service token` on the merchant-key path (the key stops resolving at all);
* tenant disabled → `403 tenant is disabled`;
* key rotated → the previous key stops working on the next request;
* key expired → `401 invalid service token`;
* console session whose tenant was disabled or unbound → `403`, even though the cookie is
  still validly signed and unexpired.

A client should treat 401/403 as "re-read your configuration", not as "retry".

## 4. Acting for a subject: `X-Acting-Subject`

A merchant API key may act for individual subjects **inside its own merchant**:

```
X-Acting-Subject: <32 lowercase hexadecimal characters>
```

The value is an **opaque pseudonym that is stable within one merchant**. It is not an email
address, not a user id, and not anything the receiving side can reverse. Derive it on your
own side:

```
acting_subject = HMAC-SHA256(salt, tenant_id + "\0" + subject_id)[:16]  → 32 hex chars
```

* `salt` is your deployment's secret and never leaves it. Keyed derivation is what stops
  anyone here — or anyone who reads this database — from recomputing the pseudonym of an
  account they can name. **It must be at least 32 bytes**; a shorter one still produces a
  well-formed pseudonym, so nothing downstream can notice that the scheme was weakened.
* `tenant_id` is *your* internal tenant scope, so the same account name in two of your
  tenants cannot collapse onto one tenant here. The `\0` separator is inside the derivation,
  so `("acme", "lice")` and `("acmel", "ice")` stay distinct.
* Lowercase hex is a subset of every identifier syntax on both sides, which is why no
  agreement about identifier characters is needed.

**Verify your implementation before you write a client.**
[`acting-subject-vectors.json`](acting-subject-vectors.json) carries fixed
`(salt, tenant_id, subject_id) → expected` vectors, including the pair that proves the NUL
separator is doing its job. They are the shared artifact of this contract: every
implementation runs the same numbers rather than sharing code. Two mistakes account for
almost every mismatch — truncating the **hex string** to 16 characters instead of the
**digest** to 16 bytes, and joining with `:` or `-` instead of NUL.

**How strictly you require the salt is your decision, and services on this contract are
deliberately not uniform about it.** A service whose whole purpose is to act for other
people's users should demand a salt unconditionally and refuse to start without one — for
it, "no salt" is not a mode, it is a broken deployment. This control plane does the
opposite: acting is an *optional* capability here, a deployment that only ever uses tenant
tokens is entirely normal, so an absent salt starts fine and fails closed at the first
attempt to act (a salt that is present but under 32 bytes still refuses to start, because
that one never fails closed anywhere). If you are comparing implementations across services
on this contract, **that difference is a difference in responsibility, not drift** — what
must not differ is the derivation itself, which is what the shared vectors pin.

The rules, all fail-closed:

| Situation | Result |
|---|---|
| Key **with** `mayActAsSubjects`, valid header | Acts as `(that merchant, that subject)`; the tenant row is created on first use |
| Key **with** `mayActAsSubjects`, header absent | `400` — the subject is never guessed |
| Key **without** the grant, header present | 🔴 `403 this key is not authorized to act for a subject` — **not ignored, not demoted to the key's own identity** |
| Key **without** the grant, header absent | Acts as the merchant's own default tenant |
| Malformed header (wrong length, uppercase, non-hex) | `400` |
| Tenant token, admin token or console session with the header | `403` |

Whether you may speak for someone else is a property of **your own credential**, the way
Kubernetes impersonation works — never a global switch. Ask the operator to set
`mayActAsSubjects` on your merchant; there is no request that can grant it.

Every acting call is audited on both outcomes: `auth_acting_call key=<digest prefix>
acting_as=<pseudonym> route=<method path> outcome=<allow|deny>`.

## 5. How a merchant and a tenant come into existence

**A merchant is never created automatically.** Not by a login, not by a first request, not by
an unrecognised claim. A merchant is a tenancy boundary with a quota and an owner; one that
appears by itself has none of those, and the client that caused it would not know either.

* **Merchants** are created by the operator (`POST /v1/merchants`). For OIDC logins, the
  operator maps a claim value to an **already existing** merchant id through
  `SITES_OIDC_MERCHANT_MAP`. There is no wildcard and no fallback merchant: an unmapped
  claim value is refused with `403 no merchant is mapped to this account` and logged.
  A user landing in some default merchant instead would read as "my permissions vanished",
  which is close to untraceable back to a login.
* **Tenants** are created on first use inside a merchant that already exists:
  * through a merchant API key acting for a subject — always, bounded by the merchant's
    `maxTenants` (`429 merchant_tenant_quota_exceeded`);
  * through an OIDC login — only when the operator opened signups
    (`SITES_OIDC_SIGNUPS_ENABLED`) **and** the account's email domain is on the allow list
    (`SITES_OIDC_EMAIL_DOMAINS`). Otherwise
    `403 this account has no tenant and signups are closed`. An account whose tenant already
    exists can always sign in, whether signups are open or not.

⚠️ **`SITES_OIDC_SIGNUPS_ENABLED` governs people signing themselves up, and nothing else.**
Closing it does **not** stop tenants from being created through a merchant API key acting
for a subject — that path stays open and is bounded by `maxTenants`, not by signups. The two
are deliberately separate switches because they answer different questions: *how does a
person register here* and *how does another organisation's service call us*. To bound the
second one, set the merchant's `maxTenants`, or withhold the `mayActAsSubjects` grant.

## 6. Refusals

Bodies are `{"error": "<text>"}`, plus `"code"` where a stable machine-readable code exists.

| Status | Body `error` | What the client should do |
|---|---|---|
| 401 | `invalid service token` | The credential is unknown, wrong, expired, or belongs to a disabled merchant. **These are deliberately indistinguishable** — the endpoint is not an oracle for which credentials exist. Check the credential and its `keyExpiresAt`; ask the operator if it should be valid. |
| 401 | `invalid console session` | The session cookie is unusable. Log in again. |
| 403 | `X-Merchant-ID is not accepted; the merchant and tenant are determined by the credential` | Remove the header. Same for `X-User-ID`. Your merchant is whatever the credential says. |
| 403 | `this key is not authorized to act for a subject` | Your key has no `mayActAsSubjects` grant. Ask the operator; do not drop the header and proceed, or your work lands on the wrong tenant. |
| 403 | `this token is not authorized to act for a subject` | A tenant token cannot impersonate. Use a merchant key. |
| 403 | `the admin token is not authorized to act for a subject` | Same, for the platform admin token. |
| 403 | `merchant is disabled` | The merchant behind your credential is disabled. Operator action. |
| 403 | `tenant is disabled` | That tenant is disabled. Operator action. |
| 403 | `local login is disabled` | This deployment turned the break-glass path off. Use the identity provider. |
| 403 | `no merchant is mapped to this account` | Your OIDC claim value is not in the operator's mapping table. |
| 403 | `this account has no tenant and signups are closed` | You have no tenant yet and self-service is off. |
| 403 | `invalid console session or CSRF token` | Send the `sites_console_csrf` value in `X-Sites-Console-CSRF`. |
| 403 | `this endpoint requires the admin token` | An operator-only endpoint. |
| 400 | `X-Acting-Subject is required with this key` / `… must be 32 lowercase hexadecimal characters` | Fix the derivation; see §4. |
| 404 | `no identity provider is configured` | This deployment has no OIDC; use `/v1/auth/methods` first. |
| 429 | code `merchant_tenant_quota_exceeded` | The merchant is at its `maxTenants` ceiling. Operator action; retrying will not help. |
| 503 | `database unavailable` | Transient. Retry with back-off. |
| 401 | code `mcp_token_credential_required` | `POST /mcp` only. Your console session is valid, but that endpoint takes a token credential. Use a merchant key or tenant token. |
| 403 | code `mcp_origin_refused` | `POST /mcp` only. The request carried an `Origin` header. This is not a browser API; call it server-side. |
| 406 | code `mcp_not_acceptable` | `POST /mcp` only. Your `Accept` header excludes `application/json`; the endpoint never opens a stream. |

Distinguish them by **status plus `code`** where present. Error prose may be reworded; see §8.

## 7. Client configuration

`sites.client` (the CLI and the bundled MCP server) reads:

| Variable | Meaning |
|---|---|
| `SITES_URL` | Base URL of the control plane |
| `SITES_TOKEN` / `SITES_TOKEN_FILE` | Tenant or admin token |
| `SITES_MERCHANT_KEY` / `SITES_MERCHANT_KEY_FILE` | Merchant API key |
| `SITES_ACTING_SUBJECT` | A precomputed pseudonym |
| `SITES_ACTING_SUBJECT_SALT` / `_FILE` | Salt for deriving one (§4); required to act for anyone, **minimum 32 bytes** |
| `SITES_ACTING_TENANT` | Your own tenant scope in the derivation |

Setting both `SITES_TOKEN` and `SITES_MERCHANT_KEY` is an error rather than a silent
preference: the usual cause is a stale variable left in a shell, and picking a winner means
debugging a request that was made with the credential you thought you had replaced.

Prefer the `_FILE` forms so secrets stay out of configuration files, shell history and
process arguments. Deriving a pseudonym with no salt configured **fails closed** — it does
not fall back to the service identity, because that files every user's resources under one
tenant while answering `2xx`.

**Derive once, at the boundary you own.** The pseudonym is computed by the runtime that
knows the real account, and everything downstream of that point forwards it unchanged. A
second derivation - re-hashing a pseudonym you received - produces a different, equally
well-formed pseudonym, so the two sides silently mean two different tenants and neither
reports anything. A component that receives a value which is not already a pseudonym must
refuse it rather than map it.

The salt is **yours**, not this control plane's: it is never sent here, and nothing here can
check that you have one. Two configurations are therefore refused at your own startup rather
than per call — a salt shorter than 32 bytes, and `SITES_ACTING_TENANT` set with no salt at
all (an acting setup that was left half-finished). Having no acting configuration at all is
fine and simply means you call as your own identity.

## 8. Compatibility

**Stable** — a change here is a breaking change, announced in the [changelog](../CHANGELOG.md):

* the header names `X-Sites-Service-Token`, `X-Acting-Subject`, `X-Sites-Console-CSRF`;
* the `X-Acting-Subject` derivation, its 32-lowercase-hex form, the 32-byte salt floor,
  and the vectors in `acting-subject-vectors.json`;
* the rule in §1, and the fail-closed direction of every rule in §4;
* the HTTP status of each situation in §6, and any `"code"` value;
* `/v1/auth/methods`, `/v1/auth/login`, `/v1/auth/callback`, `/v1/auth/local`,
  `/v1/auth/logout`;
* the request field `mayActAsSubjects` on merchant creation, and the response field
  `apiKey` that carries the plaintext key exactly once.

  🔴 Those last two are read by a provisioning script in a **different repository**,
  which mints this deployment's credential during cluster bring-up. Nothing in either
  repository's test suite covers that pairing: this side asserts what it emits, the other
  side asserts what it writes into its own Secret, and neither one talks to the other
  until a real bring-up runs. Renaming either key therefore fails at the *first
  provisioning of a new cluster*, as an empty Secret rather than as an error — the script
  reads a key that is not there and writes what it got. Announce a rename the way any
  other breaking change here is announced; do not treat it as an internal field name.

**Not stable** — do not parse or branch on these:

* the prose of any `error` string (assert on status and `code`);
* the `sitem_` / `site_` credential prefixes;
* cookie contents, session lifetime, and anything about the console's internals;
* which OIDC signing algorithms are accepted beyond the current RS256 (it may widen).

**Deliberately indistinguishable, and it will stay that way**: unknown credential, expired
credential, and credential of a disabled merchant all answer `401 invalid service token`.
Do not build logic that needs to tell them apart.

Operators: the configuration variables behind all of this are in
[DEPLOYMENT.md](DEPLOYMENT.md).
