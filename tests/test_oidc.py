"""Contract tests for the standalone OIDC relying party.

The provider is faked at the HTTP boundary (discovery, JWKS, token endpoint) and the ID
tokens are signed here with a fixed RSA key, so the assertions are about this repository's
verification rules rather than about a library's.

🔴 The audience cases are the point of the whole module. Three repositories share one
provider; if any of them accepts a token minted for another, splitting them into separate
trust domains bought nothing at all.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
import unittest
from unittest import mock

from sites import oidc


ISSUER = "https://idp.example.test"
CLIENT_ID = "site-console"
AUDIENCE = "site"
# The audience of some other service on the same identity provider. A token carrying it
# must not be accepted here, no matter who that service is.
OTHER_SERVICE_AUDIENCE = "some-other-service"
REDIRECT_URL = "https://sites.example.test/v1/auth/callback"
SESSION_KEY = "k" * 32
KID = "test-key-1"

# Fixed 2048-bit RSA key. Signing happens below with plain modular exponentiation, so the
# test needs no crypto dependency either - the same constraint the module itself has.
_N = "pf3FCUfXVhBb6L5RXtzAKzE00CzNWE9NGtCa1JJrMVkxPi-7egopXRwJg5Gi91isfpcrZOFafkigwSDBWPZKDhzkADFtNfQKI2c9S7k7nbxBGOZjmh2aLKI3ll4JSkzYexl4XK0bFPUDt0YkhetDHuHFwPXLoHBmE-YbUw5oFVB_-TzO0mnLqTr8T5hTJue_L56TdWY_eYly2G4o1x0waylgHU4oPPC498H_HUvB2k2DQXeU23ts19DBNH5DBtszcQ6Ec_527DhZhkWEzJwNBMADkOJaSyuys2b11OCHt9IiJTCy5NTZGnslggdUXaAJumqdXpuDU7GxVMNzVMFRKw"
_D = "H_mlCZcht436Lnju8s-iYw-dBVcEDXVlPHufv8AezwhH8JtASY-IjUuX15Tn6C7YN6CGNu4kQPxbnyhgpnL3LAXLs-_Rglmq1EwQZjRd9BIuFg5XdHosV1m-TIR71Ki98OSkp3GfLGfQWe80nOmHaf0C25tdqN_OAhpK_DJjwWsLaZ69ZVYdx7GPq1Y4dwbOXloS7s1uz3RuLqJsotjTdB6osVIF3oDPlMIwae4AnYQ51tUPxgpvre4K8OR6U90a6u4D4UbnMJSfXBKhRuz2AG6PM_xSmnxx9KbXbjnISBKDNaKSYJH0VtCXA7tZUZHVC8Oiht5tbcUABP6KpAac5Q"
_E = "AQAB"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _int(value: str) -> int:
    return int.from_bytes(
        base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)), "big"
    )


def _sign_rs256(signing_input: bytes) -> bytes:
    modulus, private = _int(_N), _int(_D)
    size = (modulus.bit_length() + 7) // 8
    tail = (
        bytes.fromhex("3031300d060960864801650304020105000420")
        + hashlib.sha256(signing_input).digest()
    )
    encoded = b"\x00\x01" + b"\xff" * (size - len(tail) - 3) + b"\x00" + tail
    return pow(int.from_bytes(encoded, "big"), private, modulus).to_bytes(size, "big")


def id_token(claims: dict, *, alg: str = "RS256", kid: str = KID, valid: bool = True) -> str:
    header = {"alg": alg, "kid": kid, "typ": "JWT"}
    segments = [
        _b64(json.dumps(header, separators=(",", ":")).encode()),
        _b64(json.dumps(claims, separators=(",", ":")).encode()),
    ]
    signing_input = ".".join(segments).encode("ascii")
    signature = _sign_rs256(signing_input)
    if not valid:
        signature = bytes(signature[:-1]) + bytes([signature[-1] ^ 0xFF])
    return ".".join(segments + [_b64(signature)])


def claims(**overrides) -> dict:
    now = int(time.time())
    base = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "alice-subject",
        "email": "alice@example.test",
        "iat": now,
        "exp": now + 300,
        "nonce": "the-nonce",
    }
    base.update(overrides)
    return base


ENV = {
    "SITES_OIDC_ISSUER": ISSUER,
    "SITES_OIDC_CLIENT_ID": CLIENT_ID,
    "SITES_OIDC_AUDIENCE": AUDIENCE,
    "SITES_OIDC_REDIRECT_URL": REDIRECT_URL,
    "SITES_OIDC_ADMIN_CLAIM": "groups",
    "SITES_OIDC_ADMIN_VALUE": "platform-admin",
    "SITES_OIDC_MERCHANT_CLAIM": "org",
    "SITES_OIDC_MERCHANT_MAP": "acme-corp=acme,globex-inc=globex",
    "SITES_OIDC_SIGNUPS_ENABLED": "false",
    "SITES_OIDC_EMAIL_DOMAINS": "",
    "SITES_OIDC_CLIENT_SECRET": "",
    "SITES_OIDC_CLIENT_SECRET_FILE": "",
    "SITES_OIDC_SCOPES": "",
}

DISCOVERY = {
    "issuer": ISSUER,
    "authorization_endpoint": f"{ISSUER}/auth",
    "token_endpoint": f"{ISSUER}/token",
    "jwks_uri": f"{ISSUER}/keys",
}

JWKS = {"keys": [{"kty": "RSA", "kid": KID, "alg": "RS256", "n": _N, "e": _E}]}


def _config(**env) -> oidc.Config:
    with mock.patch.dict("os.environ", {**ENV, **env}, clear=False):
        config = oidc.config_from_env()
    assert config is not None
    return config


class _FakeProvider:
    """Answers the three provider endpoints. Records what was posted to the token endpoint."""

    def __init__(self, token: str | None = None) -> None:
        self.token = token
        self.token_requests: list[dict] = []

    def __call__(self, request):
        url = request.full_url
        if url.endswith("/.well-known/openid-configuration"):
            return DISCOVERY
        if url.endswith("/keys"):
            return JWKS
        if url.endswith("/token"):
            from urllib.parse import parse_qs

            self.token_requests.append(
                {k: v[0] for k, v in parse_qs(request.data.decode()).items()}
            )
            return {"id_token": self.token} if self.token else {}
        raise AssertionError(f"unexpected request to {url}")


class OidcConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        oidc.reset_caches()

    def test_no_issuer_means_no_provider(self) -> None:
        with mock.patch.dict("os.environ", {**ENV, "SITES_OIDC_ISSUER": ""}, clear=False):
            self.assertIsNone(oidc.config_from_env())

    def test_a_missing_audience_refuses_to_start(self) -> None:
        """🔴 Contract §1 decision A: no default audience, not even the client id.

        A default is how two services on one provider end up sharing an audience, and from
        that moment either one's ID token opens the other.
        """
        with mock.patch.dict("os.environ", {**ENV, "SITES_OIDC_AUDIENCE": ""}, clear=False):
            with self.assertRaises(oidc.ConfigError) as caught:
                oidc.config_from_env()
        self.assertIn("SITES_OIDC_AUDIENCE", str(caught.exception))

    def test_a_missing_client_or_redirect_refuses_to_start(self) -> None:
        for name in ("SITES_OIDC_CLIENT_ID", "SITES_OIDC_REDIRECT_URL"):
            with self.subTest(missing=name):
                with mock.patch.dict("os.environ", {**ENV, name: ""}, clear=False):
                    with self.assertRaises(oidc.ConfigError):
                        oidc.config_from_env()

    def test_a_plain_http_issuer_refuses_to_start(self) -> None:
        # SITES_OIDC_ALLOW_INSECURE_ISSUER used to turn this refusal off, and on the
        # way past it also skipped the fragment check. It is gone; setting it must
        # buy nothing, or the opt-out is back without anyone deciding to bring it back.
        for extra in ({}, {"SITES_OIDC_ALLOW_INSECURE_ISSUER": "true"}):
            with self.subTest(extra=extra):
                with mock.patch.dict(
                    "os.environ",
                    {**ENV, "SITES_OIDC_ISSUER": "http://idp.example.test", **extra},
                    clear=False,
                ):
                    with self.assertRaises(oidc.ConfigError):
                        oidc.config_from_env()

    def test_signups_without_a_domain_list_refuse_to_start(self) -> None:
        # An open provider plus open signups is "anyone who can log in gets a tenant".
        with mock.patch.dict(
            "os.environ",
            {**ENV, "SITES_OIDC_SIGNUPS_ENABLED": "true", "SITES_OIDC_EMAIL_DOMAINS": ""},
            clear=False,
        ):
            with self.assertRaises(oidc.ConfigError):
                oidc.config_from_env()

    def test_a_malformed_merchant_map_refuses_to_start(self) -> None:
        with mock.patch.dict(
            "os.environ", {**ENV, "SITES_OIDC_MERCHANT_MAP": "acme-corp"}, clear=False
        ):
            with self.assertRaises(oidc.ConfigError):
                oidc.config_from_env()


class IdTokenVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        oidc.reset_caches()
        self.config = _config()
        self.provider = _FakeProvider()
        patcher = mock.patch.object(oidc, "_request_json", self.provider)
        patcher.start()
        self.addCleanup(patcher.stop)

    def verify(self, token: str, nonce: str = "the-nonce"):
        return oidc.verify_id_token(self.config, token, nonce)

    def test_a_well_formed_token_is_accepted(self) -> None:
        # Forward comparison for every rejection below: without it, a verifier that refuses
        # everything would make the whole class green.
        payload = self.verify(id_token(claims()))
        self.assertEqual(payload["sub"], "alice-subject")

    def test_a_token_for_another_repository_is_refused(self) -> None:
        """🔴 Contract §5.3: repository A's token must not open repository B."""
        with self.assertRaises(oidc.OidcError) as caught:
            self.verify(id_token(claims(aud=OTHER_SERVICE_AUDIENCE)))
        self.assertIn("audience", str(caught.exception))

    def test_an_audience_list_must_contain_this_audience_exactly(self) -> None:
        self.verify(id_token(claims(aud=[OTHER_SERVICE_AUDIENCE, AUDIENCE])))
        with self.assertRaises(oidc.OidcError):
            self.verify(id_token(claims(aud=[OTHER_SERVICE_AUDIENCE, "a-third-service"])))
        # Not a prefix, not a substring: "site-staging" is a different service.
        with self.assertRaises(oidc.OidcError):
            self.verify(id_token(claims(aud=AUDIENCE + "-staging")))

    def test_another_issuer_is_refused(self) -> None:
        with self.assertRaises(oidc.OidcError):
            self.verify(id_token(claims(iss="https://evil.example.test")))

    def test_a_broken_signature_is_refused(self) -> None:
        with self.assertRaises(oidc.OidcError):
            self.verify(id_token(claims(), valid=False))

    def test_an_unsigned_or_hmac_token_is_refused(self) -> None:
        # alg=none is the textbook forgery; HS256 is the subtler one, where the public
        # modulus from JWKS is used as if it were a shared secret.
        for alg in ("none", "HS256", "RS512"):
            with self.subTest(alg=alg):
                with self.assertRaises(oidc.OidcError):
                    self.verify(id_token(claims(), alg=alg))

    def test_an_expired_token_is_refused(self) -> None:
        now = int(time.time())
        with self.assertRaises(oidc.OidcError):
            self.verify(id_token(claims(exp=now - 3600, iat=now - 7200)))

    def test_a_token_issued_in_the_future_is_refused(self) -> None:
        now = int(time.time())
        with self.assertRaises(oidc.OidcError):
            self.verify(id_token(claims(iat=now + 3600, exp=now + 7200)))

    def test_a_replayed_nonce_is_refused(self) -> None:
        with self.assertRaises(oidc.OidcError):
            self.verify(id_token(claims(nonce="another-login")))

    def test_a_token_without_a_subject_is_refused(self) -> None:
        with self.assertRaises(oidc.OidcError):
            self.verify(id_token(claims(sub="")))

    def test_a_key_that_is_not_published_is_refused(self) -> None:
        with self.assertRaises(oidc.OidcError):
            self.verify(id_token(claims(), kid="other-key"))

    def test_a_discovery_document_for_another_issuer_is_refused(self) -> None:
        def wrong_issuer(request):
            if request.full_url.endswith("openid-configuration"):
                return {**DISCOVERY, "issuer": "https://evil.example.test"}
            return self.provider(request)

        with mock.patch.object(oidc, "_request_json", wrong_issuer):
            oidc.reset_caches()
            with self.assertRaises(oidc.OidcError):
                self.verify(id_token(claims()))


class FlowTests(unittest.TestCase):
    def setUp(self) -> None:
        oidc.reset_caches()
        self.config = _config()

    def test_begin_uses_pkce_and_binds_the_flow_to_a_signed_cookie(self) -> None:
        with mock.patch.object(oidc, "_request_json", _FakeProvider()):
            url, flow = oidc.begin(self.config, SESSION_KEY)
        from urllib.parse import parse_qs, urlsplit

        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["client_id"], [CLIENT_ID])
        self.assertEqual(query["redirect_uri"], [REDIRECT_URL])
        self.assertNotIn("code_verifier", query)
        state = oidc._unseal(flow, SESSION_KEY)
        self.assertEqual(
            query["code_challenge"][0],
            _b64(hashlib.sha256(state["verifier"].encode()).digest()),
        )
        self.assertEqual(query["state"], [state["state"]])
        self.assertEqual(query["nonce"], [state["nonce"]])

    def test_a_tampered_flow_cookie_is_refused(self) -> None:
        with mock.patch.object(oidc, "_request_json", _FakeProvider()):
            _, flow = oidc.begin(self.config, SESSION_KEY)
        encoded, _, signature = flow.rpartition(".")
        forged = json.dumps({"state": "attacker", "nonce": "n", "verifier": "v",
                             "exp": int(time.time()) + 60}, separators=(",", ":"))
        with self.assertRaises(oidc.OidcError):
            oidc.complete(
                self.config,
                {"code": ["c"], "state": ["attacker"]},
                f"{_b64(forged.encode())}.{signature}",
                SESSION_KEY,
            )
        self.assertTrue(encoded)

    def test_a_callback_without_a_flow_cookie_is_refused(self) -> None:
        with self.assertRaises(oidc.OidcError):
            oidc.complete(self.config, {"code": ["c"], "state": ["s"]}, "", SESSION_KEY)

    def test_a_state_mismatch_is_refused(self) -> None:
        with mock.patch.object(oidc, "_request_json", _FakeProvider()):
            _, flow = oidc.begin(self.config, SESSION_KEY)
            with self.assertRaises(oidc.OidcError):
                oidc.complete(
                    self.config, {"code": ["c"], "state": ["not-mine"]}, flow, SESSION_KEY
                )

    def test_a_complete_flow_sends_the_verifier_and_returns_claims(self) -> None:
        provider = _FakeProvider()
        with mock.patch.object(oidc, "_request_json", provider):
            _, flow = oidc.begin(self.config, SESSION_KEY)
            state = oidc._unseal(flow, SESSION_KEY)
            provider.token = id_token(claims(nonce=state["nonce"]))
            payload = oidc.complete(
                self.config,
                {"code": ["the-code"], "state": [state["state"]]},
                flow,
                SESSION_KEY,
            )
        self.assertEqual(payload["sub"], "alice-subject")
        exchange = provider.token_requests[-1]
        self.assertEqual(exchange["code_verifier"], state["verifier"])
        self.assertEqual(exchange["grant_type"], "authorization_code")
        self.assertEqual(exchange["redirect_uri"], REDIRECT_URL)

    def test_an_expired_flow_is_refused(self) -> None:
        with mock.patch.object(oidc, "_request_json", _FakeProvider()):
            _, flow = oidc.begin(self.config, SESSION_KEY, now=1000)
        with self.assertRaises(oidc.OidcError):
            oidc.complete(
                self.config,
                {"code": ["c"], "state": ["s"]},
                flow,
                SESSION_KEY,
                now=1000 + oidc.FLOW_SECONDS + 1,
            )


class LoginIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = _config()

    def test_the_admin_claim_produces_an_admin_login(self) -> None:
        login = oidc.login_identity(
            self.config, claims(groups=["platform-admin", "other"])
        )
        self.assertTrue(login.admin)
        self.assertEqual(login.merchant_id, "")

    def test_a_mapped_claim_lands_on_the_preset_merchant(self) -> None:
        login = oidc.login_identity(self.config, claims(org="globex-inc"))
        self.assertFalse(login.admin)
        self.assertEqual(login.merchant_id, "globex")

    def test_an_unmapped_claim_is_refused_rather_than_creating_a_merchant(self) -> None:
        """🔴 Contract §4: no login creates a tenant boundary.

        The alternative failure - putting the user in some default merchant - reads as "my
        permissions vanished" and is close to untraceable back to the login.
        """
        with self.assertRaises(oidc.OidcError) as caught:
            oidc.login_identity(self.config, claims(org="who-are-you"))
        self.assertIn("merchant", str(caught.exception))

    def test_a_missing_merchant_claim_is_refused(self) -> None:
        with self.assertRaises(oidc.OidcError):
            oidc.login_identity(self.config, claims())

    def test_derived_user_ids_cannot_collide_with_acting_subjects(self) -> None:
        # Both are opaque identifiers inside one merchant's namespace. If they shared a
        # shape, an OIDC user and a pseudonym could name the same tenant row.
        user_id = oidc.user_id_for(self.config, "alice-subject")
        self.assertRegex(user_id, r"^[a-z0-9][a-z0-9-]{0,62}$")
        self.assertTrue(user_id.startswith("oidc-"))
        self.assertNotRegex(user_id, r"^[0-9a-f]{32}$")
        self.assertEqual(user_id, oidc.user_id_for(self.config, "alice-subject"))
        self.assertNotEqual(user_id, oidc.user_id_for(self.config, "other-subject"))

    def test_signups_are_closed_by_default_and_bounded_by_domain(self) -> None:
        self.assertFalse(oidc.signup_allowed(self.config, "alice@example.test"))
        open_config = _config(
            SITES_OIDC_SIGNUPS_ENABLED="true",
            SITES_OIDC_EMAIL_DOMAINS="example.test",
        )
        self.assertTrue(oidc.signup_allowed(open_config, "alice@example.test"))
        self.assertTrue(oidc.signup_allowed(open_config, "Alice@Example.Test"))
        self.assertFalse(oidc.signup_allowed(open_config, "mallory@elsewhere.test"))
        self.assertFalse(oidc.signup_allowed(open_config, "no-domain"))


if __name__ == "__main__":
    unittest.main()
