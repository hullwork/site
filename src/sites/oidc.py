"""OpenID Connect Authorization Code + PKCE relying party for the admin console.

Standard library only, on purpose: this service ships and runs on its own, so its sign-in
path must not depend on another service's code, nor on a heavyweight crypto dependency. An
earlier design accepted sign-in assertions minted by one of its callers, which made that
caller the identity provider of this service without anyone deciding that it should be.

Scope is deliberately narrow - one flow, RS256 ID tokens, and only the claims needed to
create a normal console session. Access and refresh tokens are never stored.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

from os import getenv


FLOW_COOKIE = "sites_oidc_flow"
FLOW_SECONDS = 10 * 60
_CLOCK_SKEW_SECONDS = 60
_HTTP_TIMEOUT = 10.0
_MAX_HTTP_BODY = 512 * 1024
_DISCOVERY_TTL_SECONDS = 300.0
_JWKS_TTL_SECONDS = 300.0
# Refetching JWKS on every unknown kid turns any attacker-supplied header into an outbound
# request. One refresh per minute is enough to pick up a real key rotation.
_JWKS_MIN_REFRESH_SECONDS = 60.0

# DER prefix of DigestInfo(SHA-256) from RFC 8017 §9.2.
_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")

# Identifier prefix for tenants created from an OIDC login. It keeps them in a different
# shape from the 32-hex acting-subject pseudonyms, so the two derivations can never name the
# same tenant row inside one merchant.
_OIDC_USER_PREFIX = "oidc-"


class OidcError(PermissionError):
    """The flow is invalid, or the provider's answer cannot be trusted."""


class ConfigError(RuntimeError):
    """OIDC is half-configured. Startup must fail rather than serve a broken login."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.URLError(f"OIDC endpoint refused redirect ({code})")


_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), _NoRedirectHandler()
)


@dataclass(frozen=True)
class Config:
    issuer: str
    client_id: str
    client_secret: str
    audience: str
    redirect_url: str
    scopes: str
    admin_claim: str
    admin_value: str
    merchant_claim: str
    merchant_map: dict[str, str]
    signups_enabled: bool
    email_domains: tuple[str, ...]


@dataclass(frozen=True)
class LoginIdentity:
    """What one successful login means to this control plane."""

    subject: str
    email: str
    admin: bool
    merchant_id: str
    user_id: str


def _env(name: str, default: str = "") -> str:
    return (getenv(name, default) or "").strip()


def _secret(name: str) -> str:
    direct = _env(name)
    if direct:
        return direct
    path = _env(f"{name}_FILE")
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError as exc:
        raise ConfigError(f"{name}_FILE cannot be read: {path}") from exc


def _boolean(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    if raw in {"true", "1", "yes", "on"}:
        return True
    if raw in {"false", "0", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false")


def _https_url(value: str, name: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.fragment:
        raise ConfigError(f"{name} must be an https URL")
    return value.rstrip("/")


def _merchant_map(raw: str) -> dict[str, str]:
    """Preset ``claim value = existing merchant id`` pairs.

    🔴 There is no wildcard and no "create it if missing" (contract §4). A control plane that
    invents a merchant when it meets an unknown claim value creates a boundary that nobody
    decided on, with a quota nobody chose - and the eight comparable PaaS control planes
    surveyed for this change do not have a single instance of it.
    """
    mapping: dict[str, str] = {}
    for item in raw.split(","):
        entry = item.strip()
        if not entry:
            continue
        if "=" not in entry:
            raise ConfigError(
                "SITES_OIDC_MERCHANT_MAP entries must be 'claimValue=merchantId'"
            )
        claim_value, merchant_id = (part.strip() for part in entry.split("=", 1))
        if not claim_value or not merchant_id:
            raise ConfigError(
                "SITES_OIDC_MERCHANT_MAP entries must be 'claimValue=merchantId'"
            )
        mapping[claim_value] = merchant_id
    return mapping


def config_from_env() -> Config | None:
    """The configured provider, or None when this deployment has no OIDC.

    Every incomplete combination raises instead of degrading: an issuer with no audience is
    the exact state in which one repository's ID token is accepted by another.
    """
    issuer = _env("SITES_OIDC_ISSUER")
    if not issuer:
        return None
    issuer = _https_url(issuer, "SITES_OIDC_ISSUER")
    client_id = _env("SITES_OIDC_CLIENT_ID")
    if not client_id:
        raise ConfigError("SITES_OIDC_CLIENT_ID is required with SITES_OIDC_ISSUER")
    audience = _env("SITES_OIDC_AUDIENCE")
    if not audience:
        # 🔴 No default and no fallback to the client id. Every service sharing this
        # provider must use a distinct audience, otherwise a token minted for one is
        # accepted by the next, and separating them into their own trust domains bought
        # nothing at all.
        raise ConfigError(
            "SITES_OIDC_AUDIENCE is required and has no default; it must differ "
            "from the audience of every other service sharing this provider"
        )
    redirect_url = _env("SITES_OIDC_REDIRECT_URL")
    if not redirect_url:
        raise ConfigError("SITES_OIDC_REDIRECT_URL is required with SITES_OIDC_ISSUER")
    redirect_url = _https_url(redirect_url, "SITES_OIDC_REDIRECT_URL")
    signups_enabled = _boolean("SITES_OIDC_SIGNUPS_ENABLED", False)
    email_domains = tuple(
        part.strip().lower()
        for part in _env("SITES_OIDC_EMAIL_DOMAINS").split(",")
        if part.strip()
    )
    if signups_enabled and not email_domains:
        raise ConfigError(
            "SITES_OIDC_SIGNUPS_ENABLED requires SITES_OIDC_EMAIL_DOMAINS; an open "
            "provider would otherwise mint a tenant for anyone who can log in"
        )
    return Config(
        issuer=issuer,
        client_id=client_id,
        client_secret=_secret("SITES_OIDC_CLIENT_SECRET"),
        audience=audience,
        redirect_url=redirect_url,
        scopes=_env("SITES_OIDC_SCOPES", "openid email profile") or "openid email profile",
        admin_claim=_env("SITES_OIDC_ADMIN_CLAIM", "groups") or "groups",
        admin_value=_env("SITES_OIDC_ADMIN_VALUE"),
        merchant_claim=_env("SITES_OIDC_MERCHANT_CLAIM"),
        merchant_map=_merchant_map(_env("SITES_OIDC_MERCHANT_MAP")),
        signups_enabled=signups_enabled,
        email_domains=email_domains,
    )


# --- transport -------------------------------------------------------------

_discovery_cache: dict[str, tuple[float, dict[str, str]]] = {}
_jwks_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _request_json(request: urllib.request.Request) -> dict[str, Any]:
    try:
        with _OPENER.open(request, timeout=_HTTP_TIMEOUT) as response:
            raw = response.read(_MAX_HTTP_BODY + 1)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise OidcError("the identity provider is unreachable") from exc
    if len(raw) > _MAX_HTTP_BODY:
        raise OidcError("the identity provider returned an oversized response")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OidcError("the identity provider returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise OidcError("the identity provider returned invalid JSON")
    return payload


def discover(config: Config, *, now: float | None = None) -> dict[str, str]:
    """Provider endpoints, cached. The document's own issuer must match the configured one."""
    current = time.monotonic() if now is None else now
    cached = _discovery_cache.get(config.issuer)
    if cached is not None and current - cached[0] < _DISCOVERY_TTL_SECONDS:
        return cached[1]
    document = _request_json(
        urllib.request.Request(
            f"{config.issuer}/.well-known/openid-configuration",
            headers={"Accept": "application/json"},
        )
    )
    if str(document.get("issuer", "")).rstrip("/") != config.issuer:
        raise OidcError("the discovery document belongs to another issuer")
    endpoints = {
        key: str(document.get(key, ""))
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri")
    }
    if not all(endpoints.values()):
        raise OidcError("the discovery document is incomplete")
    _discovery_cache[config.issuer] = (current, endpoints)
    return endpoints


def _jwks(jwks_uri: str, *, refresh: bool = False, now: float | None = None) -> list[dict[str, Any]]:
    current = time.monotonic() if now is None else now
    cached = _jwks_cache.get(jwks_uri)
    if cached is not None:
        age = current - cached[0]
        if age < _JWKS_TTL_SECONDS and not (
            refresh and age >= _JWKS_MIN_REFRESH_SECONDS
        ):
            return cached[1]
    document = _request_json(
        urllib.request.Request(jwks_uri, headers={"Accept": "application/json"})
    )
    keys = [key for key in document.get("keys", []) if isinstance(key, dict)]
    if not keys:
        raise OidcError("the provider published no signing keys")
    _jwks_cache[jwks_uri] = (current, keys)
    return keys


def reset_caches() -> None:
    """Drop the discovery and JWKS caches (configuration changes, tests)."""
    _discovery_cache.clear()
    _jwks_cache.clear()


# --- flow ------------------------------------------------------------------


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sign(encoded: str, key: str) -> str:
    return _b64(
        hmac.new(key.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
    )


def _seal(payload: dict[str, Any], key: str) -> str:
    encoded = _b64(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    return f"{encoded}.{_sign(encoded, key)}"


def _unseal(value: str, key: str) -> dict[str, Any]:
    try:
        encoded, supplied = value.split(".", 1)
        if not hmac.compare_digest(supplied, _sign(encoded, key)):
            raise OidcError("the login flow state is not valid")
        payload = json.loads(_unb64(encoded))
    except OidcError:
        raise
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise OidcError("the login flow state is not valid") from exc
    if not isinstance(payload, dict):
        raise OidcError("the login flow state is not valid")
    return payload


def begin(config: Config, key: str, *, now: int | None = None) -> tuple[str, str]:
    """Start a login. Returns (authorization URL, signed flow-cookie value).

    State, nonce and the PKCE verifier live in a signed cookie rather than in server memory:
    the control plane keeps no login-attempt table, and a signed cookie cannot be swapped for
    another browser's flow.
    """
    if not key:
        raise ConfigError("console sessions require a signing key")
    current = int(time.time() if now is None else now)
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    challenge = _b64(hashlib.sha256(verifier.encode("ascii")).digest())
    endpoints = discover(config)
    query = urlencode(
        {
            "response_type": "code",
            "client_id": config.client_id,
            "redirect_uri": config.redirect_url,
            "scope": config.scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    flow = _seal(
        {"state": state, "nonce": nonce, "verifier": verifier, "exp": current + FLOW_SECONDS},
        key,
    )
    return f"{endpoints['authorization_endpoint']}?{query}", flow


def flow_cookie(value: str, *, secure: bool) -> str:
    suffix = "; Secure" if secure else ""
    return (
        f"{FLOW_COOKIE}={value}; Path=/v1/auth; Max-Age={FLOW_SECONDS}; "
        f"HttpOnly; SameSite=Lax{suffix}"
    )


def clear_flow_cookie(*, secure: bool) -> str:
    suffix = "; Secure" if secure else ""
    return f"{FLOW_COOKIE}=; Path=/v1/auth; Max-Age=0; HttpOnly; SameSite=Lax{suffix}"


def _exchange_code(config: Config, code: str, verifier: str) -> str:
    endpoints = discover(config)
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": config.redirect_url,
        "client_id": config.client_id,
        "code_verifier": verifier,
    }
    if config.client_secret:
        form["client_secret"] = config.client_secret
    payload = _request_json(
        urllib.request.Request(
            endpoints["token_endpoint"],
            data=urlencode(form).encode("ascii"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
    )
    id_token = str(payload.get("id_token") or "")
    if not id_token:
        raise OidcError("the provider returned no ID token")
    return id_token


def _rsa_verify(modulus: int, exponent: int, signature: bytes, message: bytes) -> bool:
    """RSASSA-PKCS1-v1_5 with SHA-256 (RFC 8017 §8.2.2), integers only.

    Verification is a public-key operation, so it needs no constant-time modular arithmetic;
    the final comparison is still constant time out of habit rather than necessity.
    """
    size = (modulus.bit_length() + 7) // 8
    if len(signature) != size:
        return False
    value = int.from_bytes(signature, "big")
    if value >= modulus:
        return False
    encoded = pow(value, exponent, modulus).to_bytes(size, "big")
    tail = _SHA256_DIGEST_INFO + hashlib.sha256(message).digest()
    if size < len(tail) + 11:
        return False
    expected = b"\x00\x01" + b"\xff" * (size - len(tail) - 3) + b"\x00" + tail
    return hmac.compare_digest(encoded, expected)


def _verify_signature(token: str, keys: list[dict[str, Any]], kid: str) -> bool:
    signing_input, _, encoded_signature = token.rpartition(".")
    try:
        signature = _unb64(encoded_signature)
    except (ValueError, UnicodeError):
        return False
    for key in keys:
        if key.get("kty") != "RSA":
            continue
        if kid and key.get("kid") and key.get("kid") != kid:
            continue
        if key.get("alg") and key.get("alg") != "RS256":
            continue
        try:
            modulus = int.from_bytes(_unb64(str(key["n"])), "big")
            exponent = int.from_bytes(_unb64(str(key["e"])), "big")
        except (KeyError, ValueError, UnicodeError):
            continue
        if _rsa_verify(modulus, exponent, signature, signing_input.encode("ascii")):
            return True
    return False


def verify_id_token(
    config: Config, token: str, nonce: str, *, now: int | None = None
) -> dict[str, Any]:
    """Validate an ID token and return its claims.

    Audience is checked for exact membership, never "starts with" or "contains": the whole
    point of a per-service audience is that another service's token fails right here.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise OidcError("the ID token is malformed")
    try:
        header = json.loads(_unb64(parts[0]))
        claims = json.loads(_unb64(parts[1]))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise OidcError("the ID token is malformed") from exc
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise OidcError("the ID token is malformed")
    if header.get("alg") != "RS256":
        # No "none", and no HMAC family: with an HMAC alg the public JWKS modulus would be
        # accepted as a shared secret, which is the classic algorithm-confusion forgery.
        raise OidcError("the ID token is not signed with RS256")
    kid = str(header.get("kid") or "")
    jwks_uri = discover(config)["jwks_uri"]
    if not _verify_signature(token, _jwks(jwks_uri), kid):
        if not _verify_signature(token, _jwks(jwks_uri, refresh=True), kid):
            raise OidcError("the ID token signature is not valid")
    current = int(time.time() if now is None else now)
    if str(claims.get("iss", "")).rstrip("/") != config.issuer:
        raise OidcError("the ID token was issued by another provider")
    audiences = claims.get("aud")
    audiences = [audiences] if isinstance(audiences, str) else audiences
    if not isinstance(audiences, list) or config.audience not in [
        str(item) for item in audiences
    ]:
        raise OidcError("the ID token was issued for another audience")
    expires_at = _numeric(claims, "exp")
    issued_at = _numeric(claims, "iat")
    not_before = _numeric(claims, "nbf")
    if expires_at is None or expires_at + _CLOCK_SKEW_SECONDS < current:
        raise OidcError("the ID token has expired")
    if issued_at is None or issued_at - _CLOCK_SKEW_SECONDS > current:
        raise OidcError("the ID token is not valid yet")
    if not_before is not None and not_before - _CLOCK_SKEW_SECONDS > current:
        raise OidcError("the ID token is not valid yet")
    if not hmac.compare_digest(str(claims.get("nonce") or ""), nonce):
        raise OidcError("the ID token replays another login")
    if not str(claims.get("sub") or ""):
        raise OidcError("the ID token carries no subject")
    return claims


def _numeric(claims: dict[str, Any], key: str) -> int | None:
    value = claims.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def complete(
    config: Config,
    query: dict[str, list[str]],
    flow_value: str,
    key: str,
    *,
    now: int | None = None,
) -> dict[str, Any]:
    """Finish a callback and return the verified ID-token claims."""
    current = int(time.time() if now is None else now)
    if not flow_value:
        raise OidcError("this login did not start here")
    flow = _unseal(flow_value, key)
    if int(flow.get("exp") or 0) < current:
        raise OidcError("this login took too long; start again")
    error = (query.get("error") or [""])[0]
    if error:
        raise OidcError("the identity provider refused the login")
    state = (query.get("state") or [""])[0]
    code = (query.get("code") or [""])[0]
    if not code or not hmac.compare_digest(str(flow.get("state") or ""), state):
        raise OidcError("this login did not start here")
    id_token = _exchange_code(config, code, str(flow.get("verifier") or ""))
    return verify_id_token(config, id_token, str(flow.get("nonce") or ""), now=now)


# --- claims -> identity ----------------------------------------------------


def _claim_values(claims: dict[str, Any], name: str) -> list[str]:
    value = claims.get(name)
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return [str(value)]


def user_id_for(config: Config, subject: str) -> str:
    """Stable, opaque tenant identifier for one provider subject.

    Derived from issuer and subject so that changing providers cannot silently hand an
    existing tenant to a new person who happens to have the same ``sub``.
    """
    digest = hashlib.sha256(f"{config.issuer}\0{subject}".encode("utf-8")).hexdigest()
    return _OIDC_USER_PREFIX + digest[:27]


def login_identity(config: Config, claims: dict[str, Any]) -> LoginIdentity:
    """Map verified claims onto (admin | merchant tenant). Raises OidcError with the reason.

    Merchant resolution is a preset table lookup and nothing else. An unmapped claim value is
    refused loudly, because the alternative failure - handing the user a working session in
    some default merchant - looks exactly like "my permissions disappeared" and is the kind
    of thing nobody manages to trace back to login.
    """
    subject = str(claims.get("sub"))
    email = str(claims.get("email") or "")
    if config.admin_value and config.admin_value in _claim_values(
        claims, config.admin_claim
    ):
        return LoginIdentity(subject, email, True, "", "")
    if not config.merchant_claim:
        raise OidcError("this account is not an administrator")
    values = _claim_values(claims, config.merchant_claim)
    for value in values:
        merchant_id = config.merchant_map.get(value)
        if merchant_id:
            return LoginIdentity(
                subject, email, False, merchant_id, user_id_for(config, subject)
            )
    raise OidcError("no merchant is mapped to this account")


def signup_allowed(config: Config, email: str) -> bool:
    """Whether a first-time user may have a tenant created for them."""
    if not config.signups_enabled:
        return False
    _, _, domain = email.partition("@")
    return bool(domain) and domain.lower() in config.email_domains
