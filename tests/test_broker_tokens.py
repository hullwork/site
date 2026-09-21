"""Tests for sites.broker.tokens: the Ed25519 compact-JWS KeyRing."""
from __future__ import annotations

import json
import time
import unittest

from sites.broker import tokens as broker_tokens

from tests import broker_fixtures as fx


class KeyRingBasicsTests(unittest.TestCase):
    def test_exactly_one_active_key_is_required(self) -> None:
        no_active = [fx._new_key_entry(active=False)]
        two_active = [fx._new_key_entry(active=True), fx._new_key_entry(active=True)]
        for entries in (no_active, two_active):
            with self.subTest(entries=len(entries)):
                with self.assertRaises(RuntimeError):
                    broker_tokens.KeyRing(_keys_from_entries(entries))

    def test_sign_then_verify_round_trips(self) -> None:
        keys = fx.keyring()
        token, expires_at = broker_tokens.issue_app_token(keys, "a" * 32, "site-1")
        claims = keys.verify(token, aud=broker_tokens.APP_AUDIENCE, typ="app")
        self.assertEqual(claims["sub"], "a" * 32)
        self.assertEqual(claims["site"], "site-1")
        self.assertEqual(claims["exp"], expires_at)

    def test_header_is_exactly_the_contract_shape(self) -> None:
        keys = fx.keyring()
        token, _ = broker_tokens.issue_app_token(keys, "a" * 32, None)
        header_b64 = token.split(".")[0]
        header = json.loads(broker_tokens._unb64u(header_b64))
        self.assertEqual(header, {"alg": "EdDSA", "typ": "JWT", "kid": keys.active_kid})

    def test_jwks_shape(self) -> None:
        keys = fx.keyring()
        jwks = keys.jwks()
        self.assertEqual(len(jwks["keys"]), 1)
        entry = jwks["keys"][0]
        self.assertEqual(entry["kty"], "OKP")
        self.assertEqual(entry["crv"], "Ed25519")
        self.assertEqual(entry["use"], "sig")
        self.assertEqual(entry["alg"], "EdDSA")
        self.assertEqual(entry["kid"], keys.active_kid)

    def test_inactive_key_still_verifies(self) -> None:
        keys = fx.keyring(extra_inactive=True)
        inactive_kid = [k for k in keys._by_kid if k != keys.active_kid][0]
        # Forge a token as if it had been signed while that key was active, by
        # signing with the private key object directly.
        inactive = keys._by_kid[inactive_kid]
        header = {"alg": "EdDSA", "typ": "JWT", "kid": inactive_kid}
        claims = {"iss": "hull-broker", "aud": "hull-broker", "typ": "app", "sub": "x",
                  "iat": int(time.time()), "exp": int(time.time()) + 60}
        signing_input = (
            broker_tokens._b64u(json.dumps(header, sort_keys=True).encode())
            + "." + broker_tokens._b64u(json.dumps(claims, sort_keys=True).encode())
        )
        signature = inactive.private_key.sign(signing_input.encode("ascii"))
        token = f"{signing_input}.{broker_tokens._b64u(signature)}"
        verified = keys.verify(token, aud="hull-broker", typ="app")
        self.assertEqual(verified["sub"], "x")


def _keys_from_entries(entries: list[dict]) -> list[broker_tokens._Key]:
    from cryptography.hazmat.primitives import serialization
    keys = []
    for entry in entries:
        private_key = serialization.load_pem_private_key(
            entry["private_key_pem"].encode(), password=None
        )
        keys.append(broker_tokens._Key(kid=entry["kid"], private_key=private_key,
                                        public_key=private_key.public_key(),
                                        active=entry["active"]))
    return keys


class RejectionMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.keys = fx.keyring()
        self.token, self.expires_at = broker_tokens.issue_app_token(self.keys, "a" * 32, "s")

    def test_bad_signature(self) -> None:
        header, payload, signature = self.token.split(".")
        # Flipping the *encoded* last character can land entirely in base64's
        # padding bits and decode to the same byte, which made this test flaky
        # (~50%) rather than reliably wrong. Flip a bit in the decoded bytes
        # instead, so the signature is guaranteed to differ.
        raw = bytearray(broker_tokens._unb64u(signature))
        raw[0] ^= 0xFF
        tampered_signature = broker_tokens._b64u(bytes(raw))
        with self.assertRaises(broker_tokens.TokenError) as ctx:
            self.keys.verify(f"{header}.{payload}.{tampered_signature}",
                              aud=broker_tokens.APP_AUDIENCE, typ="app")
        self.assertEqual(ctx.exception.code, "bad_signature")

    def test_unknown_kid(self) -> None:
        header = broker_tokens._b64u(json.dumps(
            {"alg": "EdDSA", "typ": "JWT", "kid": "does-not-exist"}
        ).encode())
        _, payload, signature = self.token.split(".")
        with self.assertRaises(broker_tokens.TokenError) as ctx:
            self.keys.verify(f"{header}.{payload}.{signature}",
                              aud=broker_tokens.APP_AUDIENCE, typ="app")
        self.assertEqual(ctx.exception.code, "unknown_key")

    def test_wrong_typ(self) -> None:
        with self.assertRaises(broker_tokens.TokenError) as ctx:
            self.keys.verify(self.token, aud=broker_tokens.APP_AUDIENCE, typ="user")
        self.assertEqual(ctx.exception.code, "invalid_claims")

    def test_wrong_aud(self) -> None:
        with self.assertRaises(broker_tokens.TokenError) as ctx:
            self.keys.verify(self.token, aud="not-hull-broker", typ="app")
        self.assertEqual(ctx.exception.code, "invalid_claims")

    def test_expired(self) -> None:
        with self.assertRaises(broker_tokens.TokenError) as ctx:
            self.keys.verify(self.token, aud=broker_tokens.APP_AUDIENCE, typ="app",
                              now=self.expires_at + 100)
        self.assertEqual(ctx.exception.code, "expired")

    def test_expiry_respects_leeway(self) -> None:
        # Just past exp, but inside the leeway window: still valid.
        claims = self.keys.verify(self.token, aud=broker_tokens.APP_AUDIENCE, typ="app",
                                   now=self.expires_at + 1, leeway=5)
        self.assertEqual(claims["sub"], "a" * 32)

    def test_missing_claims(self) -> None:
        header = self.token.split(".")[0]
        payload = broker_tokens._b64u(json.dumps({"iss": "hull-broker"}).encode())
        forged_signature = self.keys._active.private_key.sign(
            f"{header}.{payload}".encode("ascii")
        )
        token = f"{header}.{payload}.{broker_tokens._b64u(forged_signature)}"
        with self.assertRaises(broker_tokens.TokenError) as ctx:
            self.keys.verify(token, aud=broker_tokens.APP_AUDIENCE, typ="app")
        self.assertEqual(ctx.exception.code, "invalid_claims")

    def test_malformed_token(self) -> None:
        for garbage in ("", "not-a-token", "a.b", "a.b.c.d"):
            with self.subTest(token=garbage):
                with self.assertRaises(broker_tokens.TokenError) as ctx:
                    self.keys.verify(garbage, aud=broker_tokens.APP_AUDIENCE, typ="app")
                self.assertEqual(ctx.exception.code, "malformed_token")


if __name__ == "__main__":
    unittest.main()
