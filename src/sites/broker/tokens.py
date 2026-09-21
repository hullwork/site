"""Compact EdDSA (Ed25519) JWS tokens: the Broker's own credential format.

Header is always ``{"alg":"EdDSA","typ":"JWT","kid":"<kid>"}``, base64url without
padding, per contract section 2. There is no ``none`` algorithm and no algorithm
negotiation: the header's ``alg`` is read only to reject anything that is not
``EdDSA``, never to select a verification routine.
"""
from __future__ import annotations

import base64
import json
import secrets
import time
from dataclasses import dataclass
from os import getenv
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

ISSUER = "hull-broker"
APP_AUDIENCE = "hull-broker"


class TokenError(Exception):
    """Every rejection reason verify() can produce, one code per reason."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64u(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@dataclass(frozen=True)
class _Key:
    kid: str
    private_key: Ed25519PrivateKey | None
    public_key: Ed25519PublicKey
    active: bool


class KeyRing:
    """Loads ``HULL_BROKER_SIGNING_KEYS_FILE``: exactly one active key signs new
    tokens; every key, active or not, still verifies -- that is what lets a
    rotation roll forward without invalidating tokens issued moments earlier.
    """

    def __init__(self, keys: list[_Key]):
        if not keys:
            raise RuntimeError("the broker signing key file has no keys")
        actives = [k for k in keys if k.active]
        if len(actives) != 1:
            raise RuntimeError(
                "the broker signing key file must have exactly one active key, "
                f"found {len(actives)}"
            )
        self._by_kid = {k.kid: k for k in keys}
        self._active = actives[0]

    @classmethod
    def load(cls, path: str) -> "KeyRing":
        try:
            raw = Path(path).read_text()
        except OSError as exc:
            raise RuntimeError(
                f"broker signing keys file is unreadable: {path}"
            ) from exc
        try:
            entries = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("broker signing keys file is not valid JSON") from exc
        if not isinstance(entries, list) or not entries:
            raise RuntimeError(
                "broker signing keys file must be a non-empty JSON list"
            )
        keys: list[_Key] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise RuntimeError("each signing key entry must be a JSON object")
            kid = str(entry.get("kid") or "")
            pem = entry.get("private_key_pem") or ""
            active = bool(entry.get("active"))
            if not kid or not pem:
                raise RuntimeError(
                    "each signing key needs a kid and a private_key_pem"
                )
            private_key = serialization.load_pem_private_key(
                pem.encode("utf-8"), password=None
            )
            if not isinstance(private_key, Ed25519PrivateKey):
                raise RuntimeError(f"signing key {kid} is not an Ed25519 key")
            keys.append(_Key(kid=kid, private_key=private_key,
                              public_key=private_key.public_key(), active=active))
        return cls(keys)

    @classmethod
    def from_env(cls) -> "KeyRing":
        path = (getenv("HULL_BROKER_SIGNING_KEYS_FILE", "") or "").strip()
        if not path:
            raise RuntimeError("HULL_BROKER_SIGNING_KEYS_FILE is required")
        return cls.load(path)

    @property
    def active_kid(self) -> str:
        return self._active.kid

    def sign(self, claims: dict[str, Any]) -> str:
        header = {"alg": "EdDSA", "typ": "JWT", "kid": self._active.kid}
        signing_input = (
            _b64u(json.dumps(header, separators=(",", ":"), sort_keys=True)
                  .encode("utf-8"))
            + "." + _b64u(json.dumps(claims, separators=(",", ":"), sort_keys=True)
                          .encode("utf-8"))
        )
        signature = self._active.private_key.sign(signing_input.encode("ascii"))
        return f"{signing_input}.{_b64u(signature)}"

    def verify(self, token: str, *, aud: str, typ: str, leeway: int | None = None,
               now: float | None = None) -> dict[str, Any]:
        """Reject: bad signature, unknown kid, wrong typ, wrong aud, expired,
        missing claims -- each with its own code, never a bare exception.
        """
        if leeway is None:
            leeway = int(getenv("HULL_TOKEN_LEEWAY_SECONDS", "5") or "5")
        current = time.time() if now is None else now
        parts = token.split(".") if isinstance(token, str) else []
        if len(parts) != 3:
            raise TokenError("malformed_token", "the token is not a well-formed JWS")
        header_part, payload_part, signature_part = parts
        try:
            header = json.loads(_unb64u(header_part))
            payload = json.loads(_unb64u(payload_part))
            signature = _unb64u(signature_part)
        except (ValueError, UnicodeError, json.JSONDecodeError):
            raise TokenError("malformed_token", "the token is not a well-formed JWS")
        if not isinstance(header, dict) or not isinstance(payload, dict):
            raise TokenError("malformed_token", "the token is not a well-formed JWS")
        if header.get("alg") != "EdDSA" or header.get("typ") != "JWT":
            raise TokenError("malformed_token", "the token header names an unsupported algorithm")
        kid = header.get("kid")
        key = self._by_kid.get(kid) if isinstance(kid, str) else None
        if key is None:
            raise TokenError("unknown_key", "the token was signed by an unknown key")
        signing_input = f"{header_part}.{payload_part}".encode("ascii")
        try:
            key.public_key.verify(signature, signing_input)
        except InvalidSignature:
            raise TokenError("bad_signature", "the token signature does not verify")
        if payload.get("iss") != ISSUER:
            raise TokenError("invalid_claims", "the token issuer does not match")
        if payload.get("typ") != typ:
            raise TokenError("invalid_claims", "the token type does not match this endpoint")
        if payload.get("aud") != aud:
            raise TokenError("invalid_claims", "the token audience does not match")
        for claim in ("sub", "iat", "exp"):
            if claim not in payload:
                raise TokenError("invalid_claims", "the token is missing a required claim")
        exp, iat = payload.get("exp"), payload.get("iat")
        if (isinstance(exp, bool) or isinstance(iat, bool)
                or not isinstance(exp, (int, float)) or not isinstance(iat, (int, float))):
            raise TokenError("invalid_claims", "the token timestamps are not numbers")
        if current > exp + leeway:
            raise TokenError("expired", "the token has expired")
        if iat > current + leeway:
            raise TokenError("invalid_claims", "the token was issued in the future")
        return payload

    def jwks(self) -> dict[str, Any]:
        keys = []
        for key in self._by_kid.values():
            raw_public = key.public_key.public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
            keys.append({
                "kty": "OKP", "crv": "Ed25519", "x": _b64u(raw_public),
                "kid": key.kid, "use": "sig", "alg": "EdDSA",
            })
        return {"keys": keys}


def app_token_ttl_seconds() -> int:
    return int(getenv("HULL_APP_TOKEN_TTL_SECONDS", "600") or "600")


def user_token_ttl_seconds() -> int:
    return int(getenv("HULL_USER_TOKEN_TTL_SECONDS", "600") or "600")


def issue_app_token(keys: KeyRing, app_env_id: str, site: str | None, *,
                     now: float | None = None) -> tuple[str, int]:
    """Returns (token, expiresAt epoch seconds)."""
    current = int(time.time() if now is None else now)
    expires_at = current + app_token_ttl_seconds()
    claims = {
        "iss": ISSUER, "aud": APP_AUDIENCE, "typ": "app", "sub": app_env_id,
        "site": site, "iat": current, "exp": expires_at, "jti": secrets.token_hex(16),
    }
    return keys.sign(claims), expires_at


def issue_user_token(keys: KeyRing, *, app_env_id: str, user_id: str,
                      session_id: str, org: str | None, role: str, plan: str,
                      env: str, now: float | None = None) -> tuple[str, int]:
    current = int(time.time() if now is None else now)
    expires_at = current + user_token_ttl_seconds()
    claims = {
        "iss": ISSUER, "aud": app_env_id, "typ": "user", "sub": user_id,
        "sid": session_id, "org": org, "role": role, "plan": plan, "env": env,
        "iat": current, "exp": expires_at,
    }
    return keys.sign(claims), expires_at
