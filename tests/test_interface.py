"""CLI and MCP access-layer regressions.

Both interfaces must exercise the same request path against a real HTTP service. Stubbing
the client would hide misspelled URLs, missing authentication headers, and other transport
seams.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
import unittest.mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sites.cli import _deploy_payload, build_parser, collect_site, main
from sites.client import Client, SitesError, acting_subject
from sites.mcp import (
    CALLER_USER_ARGUMENT,
    Server,
    _caller_subject,
    _tool_definitions,
    serve_stdio,
)
from sites.validation import DEPLOY_FIELDS, STATIC_IMAGE, ValidationError

CAPABILITIES = {
    "apiVersion": "sites.local/v1alpha1",
    "deploymentModes": {
        "staticInline": {"enabled": True},
        "dockerfileSource": {"enabled": True},
    },
    "limits": {
        "maxInlineArtifactFiles": 64,
        "maxInlineArtifactBytes": 61440,
        "maxSourceFiles": 256,
        "publicRoutes": 1,
    },
    "features": {
        "customDomains": False,
        "requestSecrets": False,
        "serverSideVerification": True,
    },
    "merchantId": "acme",
}

SERVICE_TOKEN = "t" * 32
# A well-formed acting-subject pseudonym: 32 lowercase hex, the one shape the control plane
# accepts. Fixed rather than derived so the tests state the wire format they assert on.
SUBJECT = "0123456789abcdef" * 2
# At least 32 bytes, because that is the floor the code enforces - a fixture below the
# floor would have had to be exempted from the rule it is supposed to exercise.
TEST_SALT = "test-salt-" + "s" * 32
SALT_ONE = "salt-one-" + "1" * 32
SALT_TWO = "salt-two-" + "2" * 32
_SALT_PATCH = unittest.mock.patch.dict(
    os.environ,
    {
        "SITES_ACTING_SUBJECT_SALT": TEST_SALT,
        "SITES_ACTING_SUBJECT_SALT_FILE": "",
        "SITES_ACTING_TENANT": "test-tenant",
    },
    clear=False,
)


def setUpModule() -> None:
    # Deriving a pseudonym needs this deployment's own salt; without it the client fails
    # closed, which is its own test above.
    _SALT_PATCH.start()


def tearDownModule() -> None:
    _SALT_PATCH.stop()


class _Handler(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def log_message(self, *_args) -> None:
        return

    def _record(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length else b""
        entry = {
            "method": self.command,
            "path": self.path,
            "token": self.headers.get("X-Sites-Service-Token"),
            "subject": self.headers.get("X-Acting-Subject"),
            # Kept in the record on purpose: the assertion that matters now is that the
            # client never sends these, and an assertion about a header nobody reads
            # cannot fail.
            "user": self.headers.get("X-User-ID"),
            "merchant": self.headers.get("X-Merchant-ID"),
            "body": json.loads(body) if body else None,
        }
        type(self).requests.append(entry)
        return entry

    def _reply(self, status: int, payload: dict) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802
        self._record()
        if self.path == "/v1/capabilities":
            return self._reply(200, CAPABILITIES)
        if self.path == "/v1/scaffolds":
            return self._reply(
                200,
                {
                    "summary": {"profiles": 1, "contractCheckSuccessRate": 1.0},
                    "methodology": {"agentEndToEndSuccessRate": None},
                    "scaffolds": [{"id": "static-html"}],
                },
            )
        if self.path == "/v1/deployments":
            return self._reply(200, {"deployments": [], "count": 0})
        if self.path == "/v1/tenants/self":
            # merchantId is deliberately different from the one in CAPABILITIES: this is the only way to "complement without overwriting"
            # Discrimination - If both sides are the same, it will not be visible even if it is covered.
            return self._reply(
                200,
                {
                    "merchantId": "beta",
                    "userId": "bob",
                    "maxDeployments": 3,
                    "maxPublicRoutes": 1,
                },
            )
        if self.path.split("?")[0] == "/v1/merchants":
            return self._reply(200, {"merchants": [], "count": 0})
        if self.path.split("?")[0] == "/v1/tenants":
            return self._reply(200, {"tenants": [], "count": 0})
        if self.path.split("?")[0] == "/v1/admin/deployments":
            return self._reply(200, {"deployments": [], "count": 0})
        if self.path == "/v1/admin/health":
            return self._reply(200, {"database": {"reachable": True}})
        if self.path.startswith("/v1/merchants/"):
            return self._reply(
                200, {"merchantId": self.path.rsplit("/", 1)[-1]}
            )
        if self.path.startswith("/v1/builds/"):
            return self._reply(
                200,
                {
                    "name": self.path.rsplit("/", 1)[-1],
                    "phase": "Building",
                    "ready": False,
                },
            )
        if self.path.startswith("/v1/bundles/"):
            return self._reply(
                200, {"name": self.path.rsplit("/", 1)[-1], "phase": "Running"}
            )
        if self.path.startswith("/v1/sites/") and self.path.endswith("/versions"):
            return self._reply(
                200,
                {
                    "siteName": self.path.split("/")[3],
                    "currentVersion": 2,
                    "versions": [{"version": 2}, {"version": 1}],
                },
            )
        if self.path.startswith("/v1/deployments/"):
            return self._reply(
                200,
                {
                    "serviceName": self.path.rsplit("/", 1)[-1],
                    "phase": "Running",
                    "verification": {"ok": True, "httpStatus": 200},
                },
            )
        return self._reply(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        entry = self._record()
        if self.path == "/v1/deployments":
            return self._reply(202, {"accepted": entry["body"]["name"]})
        if self.path == "/v1/builds":
            if getattr(type(self), "build_conflict", False):
                return self._reply(
                    409,
                    {
                        "error": "a source build with this service name already exists; "
                        "delete it before submitting a replacement",
                        "code": "build_name_exists",
                    },
                )
            return self._reply(
                202, {"name": entry["body"]["name"], "phase": "Building"}
            )
        if self.path == "/v1/bundles":
            return self._reply(202, {"name": entry["body"]["name"]})
        if self.path.startswith("/v1/sites/") and self.path.endswith("/query"):
            return self._reply(
                200,
                {
                    "siteName": self.path.split("/")[3],
                    "columns": ["value"],
                    "rows": [[1]],
                    "rowCount": 1,
                    "truncated": False,
                },
            )
        if self.path.startswith("/v1/sites/") and self.path.endswith("/versions"):
            if entry["body"].get("siteType") == "static":
                return self._reply(
                    201,
                    {
                        "siteName": self.path.split("/")[3],
                        "version": 3,
                        "contentSha256": "c" * 64,
                        "artifactUri": "oss://private-sites/object.json",
                        "staticArtifact": {
                            "sourcePath": "acme/alice/static-demo/object.json",
                            "sizeBytes": 42,
                            "fileCount": len(entry["body"]["files"]),
                        },
                    },
                )
            return self._reply(
                201,
                {
                    "siteName": self.path.split("/")[3],
                    "version": 1,
                    "databaseSchema": "site_deadbeef",
                },
            )
        if self.path == "/v1/merchants":
            return self._reply(
                201,
                {
                    "merchantId": entry["body"]["merchantId"],
                    "apiKey": "sitem_plaintext",
                    "apiKeyShownOnce": True,
                },
            )
        if self.path.split("?")[0] == "/v1/tenants":
            return self._reply(201, {"userId": entry["body"]["userId"]})
        if self.path.split("?")[0].endswith("/key"):
            return self._reply(
                200, {"apiKey": "sitem_rotated", "apiKeyShownOnce": True}
            )
        if self.path.split("?")[0].endswith("/token"):
            return self._reply(200, {"token": "site_rotated"})
        return self._reply(404, {"error": "not found"})

    def do_PATCH(self) -> None:  # noqa: N802
        entry = self._record()
        return self._reply(200, dict(entry["body"] or {}, patched=True))

    def do_DELETE(self) -> None:  # noqa: N802
        self._record()
        if self.path.split("?")[0].startswith("/v1/builds/"):
            return self._reply(200, {"deleted": True})
        if self.path.split("?")[0].startswith(
            ("/v1/merchants/", "/v1/tenants/")
        ):
            return self._reply(200, {"disabled": True})
        return self._reply(409, {"error": "still running", "code": "busy"})


class InterfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        _Handler.requests = []
        # CLI fetches credentials from the environment: command line parameters go into shell history and ps.
        patcher = unittest.mock.patch.dict(
            os.environ, {"SITES_TOKEN": SERVICE_TOKEN}, clear=False
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _client(self, **kwargs) -> Client:
        return Client(self.url, SERVICE_TOKEN, **kwargs)

    # --- client --------------------------------------------------------
    def test_client_sends_the_service_token_and_parses_json(self) -> None:
        self.assertEqual(self._client().capabilities(), CAPABILITIES)
        self.assertEqual(_Handler.requests[-1]["token"], SERVICE_TOKEN)

    def test_client_queries_a_dynamic_site_without_exposing_credentials(self) -> None:
        result = self._client().query_site("shop", "SELECT 1 AS value")
        self.assertEqual(result["rows"], [[1]])
        request = _Handler.requests[-1]
        self.assertEqual(request["path"], "/v1/sites/shop/query")
        self.assertEqual(request["body"]["rowLimit"], 100)

    def test_client_lists_immutable_site_versions(self) -> None:
        result = self._client().list_site_versions("shop")
        self.assertEqual(result["currentVersion"], 2)
        self.assertEqual(_Handler.requests[-1]["path"], "/v1/sites/shop/versions")

    def test_client_creates_static_version_without_calculating_server_digest(self) -> None:
        result = self._client().create_static_site_version(
            "docs", {"index.html": "<h1>docs</h1>"}, metadata={"generator": "agent"}
        )
        self.assertEqual(result["version"], 3)
        request = _Handler.requests[-1]
        self.assertEqual(request["path"], "/v1/sites/docs/versions")
        self.assertEqual(request["body"]["siteType"], "static")
        self.assertNotIn("contentSha256", request["body"])

    def test_client_only_claims_a_subject_when_told_to(self) -> None:
        self._client().capabilities()
        self.assertIsNone(_Handler.requests[-1]["subject"])
        self._client(subject=SUBJECT).capabilities()
        self.assertEqual(_Handler.requests[-1]["subject"], SUBJECT)

    def test_the_client_can_no_longer_name_a_merchant_or_a_tenant(self) -> None:
        """🔴 The merchant comes from the credential, so the client has nothing to say about it.

        Not merely "the client does not set a default": there is no parameter at all, and
        neither identity header leaves this process even while acting for a subject. The
        control plane refuses both headers as well (test_sites), and that pair - nobody
        sends them, and they are refused if sent - is the whole of contract §3.2's "the
        tenant may never be chosen by the caller".
        """
        with self.assertRaises(TypeError):
            Client(self.url, SERVICE_TOKEN, merchant_id="acme")
        with self.assertRaises(TypeError):
            Client(self.url, SERVICE_TOKEN, user_id="bob")
        self._client(subject=SUBJECT).capabilities()
        self.assertIsNone(_Handler.requests[-1]["merchant"])
        self.assertIsNone(_Handler.requests[-1]["user"])

    def test_client_rejects_a_malformed_acting_subject_locally(self) -> None:
        """The pseudonym must be blocked locally instead of entering the request header as is.

        'Zhang San' is a UnicodeEncodeError in http.client, and the one with line breaks is a ValueError.
        (Invalid header value) - neither in _request's except nor in MCP
        In the catch list on that side, the MCP server process exits on the spot, and the agent only sees that the connection is broken.
        I can't get a single word that explains the problem clearly.
        """
        before = len(_Handler.requests)
        bad_values = (
            "Zhang San",
            "bob\r\nX-Injected: 1",
            "bob",
            SUBJECT.upper(),
            SUBJECT[:31],
            SUBJECT + "a",
            "z" * 32,
        )
        for bad in bad_values:
            with self.subTest(subject=bad):
                with self.assertRaises(SitesError) as caught:
                    Client(self.url, SERVICE_TOKEN, subject=bad)
                self.assertEqual(caught.exception.code, "sites_invalid_subject")
        self.assertEqual(len(_Handler.requests), before)
        # Forward comparison: Without this one, a bad regular rule that rejects all values can also make the top all green, and
        # "Not a single request was sent" is precisely the most likely evidence of success in that case.
        self._client(subject=SUBJECT).capabilities()
        self.assertEqual(_Handler.requests[-1]["subject"], SUBJECT)

    def test_from_env_rejects_a_malformed_acting_subject(self) -> None:
        with unittest.mock.patch.dict(
            os.environ,
            {"SITES_URL": self.url, "SITES_ACTING_SUBJECT": "Zhang San"},
            clear=False,
        ):
            with self.assertRaises(SitesError) as caught:
                Client.from_env()
        self.assertEqual(caught.exception.code, "sites_invalid_subject")

    def test_deriving_a_subject_without_a_salt_fails_closed(self) -> None:
        """No salt must not mean "call as the service identity".

        That fallback files every user's sites under one tenant and answers 2xx while
        doing it, which is the failure nobody notices until two users see each other.
        """
        with unittest.mock.patch.dict(
            os.environ,
            {"SITES_ACTING_SUBJECT_SALT": "", "SITES_ACTING_SUBJECT_SALT_FILE": ""},
            clear=False,
        ):
            with self.assertRaises(SitesError) as caught:
                Client.subject_for("alice@example.com")
        self.assertEqual(caught.exception.code, "sites_acting_salt_missing")

    def test_a_salt_below_the_shared_floor_is_refused(self) -> None:
        """🔴 The same 32-byte floor every repository on this contract enforces.

        A short salt is the one bad input that produces a perfectly well-formed pseudonym:
        the receiving side cannot tell, no error is raised anywhere, and the only observable
        consequence is that someone who can name an account can now brute-force its
        pseudonym. It has to be refused where it is configured.
        """
        for weak in ("s" * 31, "short", "" ):
            with self.subTest(salt=weak):
                with self.assertRaises(SitesError) as caught:
                    acting_subject(weak, "tenant-a", "alice")
                self.assertIn(
                    caught.exception.code,
                    {"sites_acting_salt_too_short", "sites_acting_salt_missing"},
                )
        # Forward comparison: exactly at the floor is accepted, so the check is a floor and
        # not a rejection of everything.
        self.assertRegex(acting_subject("s" * 32, "tenant-a", "alice"), r"^[0-9a-f]{32}$")

    def test_a_short_salt_in_the_environment_is_refused_too(self) -> None:
        with unittest.mock.patch.dict(
            os.environ,
            {"SITES_ACTING_SUBJECT_SALT": "s" * 31, "SITES_ACTING_SUBJECT_SALT_FILE": ""},
            clear=False,
        ):
            with self.assertRaises(SitesError) as caught:
                Client.subject_for("alice@example.test")
        self.assertEqual(caught.exception.code, "sites_acting_salt_too_short")

    def test_the_pseudonym_is_keyed_and_separates_tenants(self) -> None:
        """Contract §3.2: keyed, and scoped by (tenant, subject).

        Both halves matter. Unkeyed, anyone who knows an address can compute the
        pseudonym of its owner offline and then ask to act as it; unscoped, the same
        account name in two of the calling deployment's own tenants collapses onto one
        row here.
        """
        digest = acting_subject(SALT_ONE, "tenant-a", "alice")
        self.assertRegex(digest, r"^[0-9a-f]{32}$")
        self.assertEqual(digest, acting_subject(SALT_ONE, "tenant-a", "alice"))
        self.assertNotEqual(digest, acting_subject(SALT_TWO, "tenant-a", "alice"))
        self.assertNotEqual(digest, acting_subject(SALT_ONE, "tenant-b", "alice"))
        # The separator is inside the derivation, not at the join: without it
        # ("tenant-a", "lice") and ("tenant-al", "ice") would be one subject.
        self.assertNotEqual(
            acting_subject(SALT_ONE, "tenant-a", "lice"),
            acting_subject(SALT_ONE, "tenant-al", "ice"),
        )

    def test_a_bad_user_id_is_an_mcp_error_result_not_a_dead_process(self) -> None:
        # This is the benefit of this verification: the agent gets a sentence that can be modified accordingly, instead of a broken one.
        # stdio pipe.
        stdin = io.StringIO(
            json.dumps(
                {
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "list", "arguments": {}},
                }
            )
            + "\n"
        )
        stdout = io.StringIO()
        serve_stdio(
            stdin,
            stdout,
            Server(
                client_factory=lambda: Client(
                    self.url, SERVICE_TOKEN, subject="Zhang San"
                )
            ),
        )
        result = json.loads(stdout.getvalue())["result"]
        self.assertTrue(result["isError"])
        self.assertIn("X-Acting-Subject", result["content"][0]["text"])

    def test_from_env_takes_a_merchant_api_key_as_the_credential(self) -> None:
        with unittest.mock.patch.dict(
            os.environ,
            {
                "SITES_TOKEN": "",
                "SITES_MERCHANT_KEY": "sitem_" + "k" * 26,
                "SITES_URL": self.url,
                "SITES_ACTING_SUBJECT": SUBJECT,
            },
            clear=False,
        ):
            Client.from_env().capabilities()
        # The merchant key and the admin token go through the same header, and the server looks up the table to distinguish; the merchant is determined by the key.
        # So the client must not send a merchant of its own that could contradict it.
        self.assertEqual(
            _Handler.requests[-1]["token"], "sitem_" + "k" * 26
        )
        self.assertEqual(_Handler.requests[-1]["subject"], SUBJECT)
        self.assertIsNone(_Handler.requests[-1]["merchant"])

    def test_from_env_refuses_two_credentials_at_once(self) -> None:
        # Old variables often remain in the shell when credentials are renewed. If you silently pick a winner, people will face the new key.
        # Troubleshoot a request made with an old token.
        with unittest.mock.patch.dict(
            os.environ,
            {"SITES_TOKEN": SERVICE_TOKEN, "SITES_MERCHANT_KEY": "sitem_x"},
            clear=False,
        ):
            with self.assertRaises(SitesError) as caught:
                Client.from_env()
        self.assertEqual(
            caught.exception.code, "sites_ambiguous_credentials"
        )

    def test_client_surfaces_the_upstream_error_contract(self) -> None:
        with self.assertRaises(SitesError) as caught:
            self._client().delete_deployment("web")
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.code, "busy")
        self.assertFalse(caught.exception.retryable)

    def test_client_refuses_a_malformed_control_plane_url(self) -> None:
        for bad in ("ftp://host", "http://user:pw@host", "http://host?a=1"):
            with self.assertRaises(SitesError):
                Client(bad, SERVICE_TOKEN)
        with self.assertRaises(SitesError):
            Client(self.url, "")

    def test_service_names_are_escaped_into_the_path(self) -> None:
        self._client().get_deployment("we/../b")
        self.assertEqual(
            _Handler.requests[-1]["path"], "/v1/deployments/we%2F..%2Fb"
        )

    # --- static site collection ---------------------------------------
    def test_static_collection_rejects_what_the_platform_would(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            with self.assertRaises(ValidationError):
                collect_site(directory)  # Empty directory

            (directory / "style.css").write_text("body{}", encoding="utf-8")
            with self.assertRaises(ValidationError) as caught:
                collect_site(directory)
            self.assertIn("index.html", str(caught.exception))

            (directory / "index.html").write_text("<h1>hi</h1>", encoding="utf-8")
            (directory / ".hidden").write_text("x", encoding="utf-8")
            files = collect_site(directory)
            self.assertEqual(sorted(files), ["index.html", "style.css"])

            (directory / "nested").mkdir()
            with self.assertRaises(ValidationError) as caught:
                collect_site(directory)
            self.assertIn("subdirector", str(caught.exception))

    # --- cli -----------------------------------------------------------
    def test_cli_deploy_builds_the_documented_payload(self) -> None:
        code = main(
            [
                "--url", self.url, "deploy",
                "--name", "api", "--image", "example.invalid/api:v1",
                "--port", "9000", "--health-path", "/readyz",
                "--liveness-path", "/healthz", "--exposure", "internal",
                "--run-as-user", "10005",
                "--env", "MODE=prod",
                "--secret-env", "TOKEN=app-keys/token",
                "--secret-mount", "app-keys:/var/run/keys",
            ]
        )
        self.assertEqual(code, 0)
        body = _Handler.requests[-1]["body"]
        self.assertEqual(body["name"], "api")
        self.assertEqual(body["livenessPath"], "/healthz")
        self.assertEqual(body["runAsUser"], 10005)
        self.assertEqual(
            body["env"],
            [
                {"name": "MODE", "value": "prod"},
                {
                    "name": "TOKEN",
                    "secretKeyRef": {"name": "app-keys", "key": "token"},
                },
            ],
        )
        self.assertEqual(
            body["secretMounts"],
            [{"secretName": "app-keys", "mountPath": "/var/run/keys"}],
        )

    def test_cli_reports_bad_input_without_touching_the_network(self) -> None:
        before = len(_Handler.requests)
        code = main(
            [
                "--url", self.url, "deploy",
                "--name", "api", "--image", "x/y:1",
                "--secret-env", "TOKEN=missing-slash",
            ]
        )
        self.assertEqual(code, 2)
        self.assertEqual(len(_Handler.requests), before)

    def test_cli_maps_an_upstream_refusal_to_a_nonzero_exit(self) -> None:
        self.assertEqual(main(["--url", self.url, "delete", "web"]), 1)

    # --- cli admin surface ---------------------------------------------
    def test_admin_tenant_commands_locate_a_row_by_merchant(self) -> None:
        # The user_id is only unique within the merchant, so every request to name a tenant must carry the merchantId.
        self.assertEqual(
            main(
                [
                    "--url", self.url, "admin", "tenants", "rotate",
                    "bob", "--merchant", "acme",
                ]
            ),
            0,
        )
        self.assertEqual(
            _Handler.requests[-1]["path"],
            "/v1/tenants/bob/token?merchantId=acme",
        )
        self.assertEqual(
            main(
                [
                    "--url", self.url, "admin", "tenants", "disable",
                    "bob", "--merchant", "acme",
                ]
            ),
            0,
        )
        self.assertEqual(
            _Handler.requests[-1]["path"], "/v1/tenants/bob?merchantId=acme"
        )

    def test_admin_tenant_create_puts_the_merchant_in_the_body(self) -> None:
        self.assertEqual(
            main(
                [
                    "--url", self.url, "admin", "tenants", "create", "bob",
                    "--merchant", "acme", "--max-deployments", "5",
                ]
            ),
            0,
        )
        self.assertEqual(
            _Handler.requests[-1]["body"],
            {"merchantId": "acme", "userId": "bob", "maxDeployments": 5},
        )

    def test_admin_merchant_create_builds_the_documented_payload(self) -> None:
        self.assertEqual(
            main(
                [
                    "--url", self.url, "admin", "merchants", "create", "acme",
                    "--display-name", "Acme", "--max-tenants", "5",
                ]
            ),
            0,
        )
        self.assertEqual(
            _Handler.requests[-1]["body"],
            {"merchantId": "acme", "displayName": "Acme", "maxTenants": 5},
        )
        self.assertEqual(_Handler.requests[-1]["path"], "/v1/merchants")

    def test_admin_patch_only_carries_the_flags_that_were_given(self) -> None:
        # Options not given cannot be considered "set as default": changing a quota once will automatically reset another quota, and
        # The caller didn't mention it at all.
        self.assertEqual(
            main(
                [
                    "--url", self.url, "admin", "merchants", "update", "acme",
                    "--max-tenants", "9",
                ]
            ),
            0,
        )
        self.assertEqual(_Handler.requests[-1]["body"], {"maxTenants": 9})

    def test_admin_update_without_any_flag_never_reaches_the_network(self) -> None:
        before = len(_Handler.requests)
        self.assertEqual(
            main(["--url", self.url, "admin", "merchants", "update", "acme"]), 2
        )
        self.assertEqual(len(_Handler.requests), before)

    def test_admin_deployment_filters_become_query_parameters(self) -> None:
        self.assertEqual(
            main(
                [
                    "--url", self.url, "admin", "deployments",
                    "--merchant", "acme", "--phase", "Running", "--limit", "10",
                ]
            ),
            0,
        )
        self.assertEqual(
            _Handler.requests[-1]["path"],
            "/v1/admin/deployments?merchantId=acme&phase=Running&limit=10",
        )
        # When no filter conditions are given, an empty query string should not be spelled out.
        self.assertEqual(main(["--url", self.url, "admin", "deployments"]), 0)
        self.assertEqual(
            _Handler.requests[-1]["path"], "/v1/admin/deployments"
        )

    def test_admin_health_is_one_command(self) -> None:
        self.assertEqual(main(["--url", self.url, "admin", "health"]), 0)
        self.assertEqual(_Handler.requests[-1]["path"], "/v1/admin/health")

    # --- mcp -----------------------------------------------------------
    def _mcp_exchange(self, messages: list[dict]) -> list[dict]:
        # The actual call is injected into this model's non-writable context by connector_runtime. The interface test is in
        # The same process is directly connected to the MCP, so this boundary is simulated for write calls with an explicit deploymentIntent.
        for message in messages:
            params = message.get("params") or {}
            arguments = params.get("arguments") or {}
            if (
                str(params.get("name", "")).startswith("deploy")
                or params.get("name") == "source_deploy"
            ) and arguments.get("deploymentIntent"):
                arguments.setdefault(
                    "_agent_deployment_authorization",
                    {
                        "version": 1,
                        "runId": f"test-{message.get('id', 'run')}",
                        "expiresAt": 4000000000,
                        "nonce": "n" * 24,
                    },
                )
        stdin = io.StringIO(
            "".join(json.dumps(message) + "\n" for message in messages)
        )
        stdout = io.StringIO()
        serve_stdio(
            stdin,
            stdout,
            Server(client_factory=lambda: Client(self.url, SERVICE_TOKEN)),
        )
        return [
            json.loads(line)
            for line in stdout.getvalue().splitlines()
            if line.strip()
        ]

    def test_mcp_handshake_echoes_the_client_protocol_version(self) -> None:
        responses = self._mcp_exchange(
            [
                {
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2026-07-28"},
                },
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
            ]
        )
        # The notification does not have an id and there should be no response.
        self.assertEqual(len(responses), 1)
        self.assertEqual(
            responses[0]["result"]["protocolVersion"], "2026-07-28"
        )

    def test_tool_descriptions_come_from_the_live_capabilities(self) -> None:
        responses = self._mcp_exchange(
            [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}]
        )
        tools = {
            tool["name"]: tool for tool in responses[0]["result"]["tools"]
        }
        self.assertIn("deploy_static", tools)
        description = tools["deploy_static"]["description"]
        # The limit number must come from the control plane and not be hard-coded in the description.
        self.assertIn("64", description)
        self.assertIn("61440", description)
        self.assertIn("Custom domain names are not supported", description)
        self.assertIn("verification", description)

    def test_deploy_image_exposes_the_scale_to_zero_switches(self) -> None:
        """The tool surface cannot be narrower than the API surface: if scaleToZero/memoryLimit cannot enter the schema,
        The agent can only bypass the bundle, and "silently losing parameters if it cannot be bypassed" is the worse way."""
        responses = self._mcp_exchange(
            [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}]
        )
        tools = {
            tool["name"]: tool for tool in responses[0]["result"]["tools"]
        }
        properties = tools["deploy_image"]["inputSchema"]["properties"]
        self.assertIn("scaleToZero", properties)
        self.assertIn("memoryLimit", properties)

    def test_tools_still_load_when_the_control_plane_is_down(self) -> None:
        stdin = io.StringIO(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"
        )
        stdout = io.StringIO()
        serve_stdio(
            stdin,
            stdout,
            Server(
                client_factory=lambda: Client(
                    "http://127.0.0.1:1", SERVICE_TOKEN, timeout=0.2
                )
            ),
        )
        tools = json.loads(stdout.getvalue())["result"]["tools"]
        description = next(
            tool["description"]
            for tool in tools
            if tool["name"] == "deploy_static"
        )
        # Boundaries must be spoken when they are unknown, rather than given a limit that may expire.
        self.assertIn("Boundaries unknown", description)

    def test_tool_call_returns_structured_content(self) -> None:
        responses = self._mcp_exchange(
            [
                {
                    "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {
                        "name": "status",
                        "arguments": {"name": "web"},
                    },
                }
            ]
        )
        result = responses[0]["result"]
        self.assertNotIn("isError", result)
        self.assertTrue(result["structuredContent"]["verification"]["ok"])

    def test_client_build_methods_hit_the_builds_endpoints(self) -> None:
        # Note: The original text of brief is type(_Handler).requests.clear(), but type() is used for classes.
        # If you get the metaclass instead of _Handler itself, AttributeError will occur; here click on this file
        # The existing writing method directly clears class attributes.
        _Handler.requests.clear()
        client = Client(self.url, SERVICE_TOKEN)
        created = client.create_build(
            {
                "name": "dynamic-web",
                "port": 8080,
                "healthPath": "/healthz",
                "files": {"Dockerfile": "FROM scratch\n"},
            }
        )
        self.assertEqual(created["phase"], "Building")
        self.assertEqual(client.get_build("dynamic-web")["phase"], "Building")
        self.assertTrue(client.delete_build("dynamic-web")["deleted"])
        paths = [(r["method"], r["path"]) for r in _Handler.requests]
        self.assertIn(("POST", "/v1/builds"), paths)
        self.assertIn(("GET", "/v1/builds/dynamic-web"), paths)
        self.assertIn(("DELETE", "/v1/builds/dynamic-web"), paths)

    def test_cli_source_build_submit_reads_a_local_directory(self) -> None:
        _Handler.requests.clear()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Dockerfile").write_text(
                "FROM scratch\nCOPY app.txt /app.txt\n", encoding="utf-8"
            )
            (root / "app.txt").write_text("hello\n", encoding="utf-8")
            self.assertEqual(
                main(
                    [
                        "--url", self.url,
                        "build", "submit",
                        "--name", "dynamic-web",
                        "--directory", directory,
                        "--port", "3000",
                        "--health-path", "/ready",
                    ]
                ),
                0,
            )
        request = _Handler.requests[-1]
        self.assertEqual((request["method"], request["path"]), ("POST", "/v1/builds"))
        self.assertEqual(request["body"]["name"], "dynamic-web")
        self.assertEqual(request["body"]["port"], 3000)
        self.assertEqual(request["body"]["healthPath"], "/ready")
        self.assertEqual(
            request["body"]["files"]["Dockerfile"],
            "FROM scratch\nCOPY app.txt /app.txt\n",
        )

    def test_cli_source_build_without_dockerfile_is_rejected_locally(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "app.txt").write_text("hello\n", encoding="utf-8")
            before = len(_Handler.requests)
            self.assertEqual(
                main(
                    ["--url", self.url, "build", "submit",
                     "--name", "dynamic-web", "--directory", directory]
                ),
                2,
            )
        self.assertEqual(len(_Handler.requests), before)

    def test_build_name_conflict_error_is_not_masquerading_as_quota(self) -> None:
        # When a build with the same name already exists, the error code must be build_name_exists instead of
        # public_route_capacity - code manipulation will cause the agent to suffer from "insufficient public route quota"
        # Check the wrong direction (actually burned multiple rounds).
        _Handler.build_conflict = True
        try:
            with self.assertRaises(SitesError) as raised:
                Client(self.url, SERVICE_TOKEN).create_build(
                    {"name": "taken", "files": {"Dockerfile": "FROM scratch\n"}}
                )
        finally:
            _Handler.build_conflict = False
        self.assertEqual(raised.exception.status, 409)
        self.assertIn("already exists", str(raised.exception))
        self.assertEqual(raised.exception.code, "build_name_exists")

    def test_a_failed_tool_call_is_an_error_result_not_a_protocol_error(
        self,
    ) -> None:
        # First carry the user's explicit deployment instructions through the deploymentIntent boundary, and then let the control plane return
        # Real business failure. The two-phase delete without force is a successful confirmation as designed.
        # Response is no longer suitable as a test carrier for "downstream failure".
        _Handler.build_conflict = True
        try:
            responses = self._mcp_exchange(
                [
                    {
                        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                        "params": {
                            "name": "source_deploy",
                            "arguments": {
                                "name": "taken",
                                "deploymentIntent": "Please deploy and go online this application",
                                "files": {"Dockerfile": "FROM scratch\n"},
                            },
                        },
                    }
                ]
            )
        finally:
            _Handler.build_conflict = False
        result = responses[0]["result"]
        self.assertTrue(result["isError"])
        self.assertIn("already exists", result["content"][0]["text"])
        self.assertNotIn("error", responses[0])

    def test_deploy_static_takes_inline_files(self) -> None:
        responses = self._mcp_exchange(
            [
                {
                    "jsonrpc": "2.0", "id": 4, "method": "tools/call",
                    "params": {
                        "name": "deploy_static",
                        "arguments": {
                            "name": "static-demo",
                            "deploymentIntent": "Please deploy this static site",
                            "files": {
                                "index.html": "<h1>hi</h1>",
                                "app.js": "console.log(1)",
                            },
                        },
                    },
                }
            ]
        )
        self.assertNotIn("isError", responses[0]["result"])
        sent = next(
            r for r in _Handler.requests
            if r["path"] == "/v1/deployments" and r["method"] == "POST"
        )
        self.assertEqual(
            sent["body"]["artifact"]["files"]["index.html"], "<h1>hi</h1>"
        )

    def test_deploy_static_without_index_html_is_an_error(self) -> None:
        responses = self._mcp_exchange(
            [
                {
                    "jsonrpc": "2.0", "id": 5, "method": "tools/call",
                    "params": {
                        "name": "deploy_static",
                        "arguments": {
                            "name": "bad",
                            "deploymentIntent": "Please deploy this static site",
                            "files": {"about.html": "x"},
                        },
                    },
                }
            ]
        )
        self.assertTrue(responses[0]["result"]["isError"])
        self.assertIn("index.html", responses[0]["result"]["content"][0]["text"])

    def test_deploy_static_versioned_creates_version_then_pins_deployment(self) -> None:
        responses = self._mcp_exchange(
            [
                {
                    "jsonrpc": "2.0", "id": 52, "method": "tools/call",
                    "params": {
                        "name": "deploy_static_versioned",
                        "arguments": {
                            "name": "static-versioned",
                            "deploymentIntent": "Publish this static site safely",
                            "files": {
                                "index.html": "<h1>v3</h1>",
                                "app.js": "console.log(3)",
                            },
                            "metadata": {"generator": "agent"},
                        },
                    },
                }
            ]
        )
        self.assertNotIn("isError", responses[0]["result"])
        version_request, deployment_request = [
            request
            for request in _Handler.requests
            if request["method"] == "POST"
            and request["path"] in {
                "/v1/sites/static-versioned/versions",
                "/v1/deployments",
            }
        ]
        self.assertEqual(version_request["body"]["siteType"], "static")
        self.assertEqual(version_request["body"]["files"]["index.html"], "<h1>v3</h1>")
        self.assertEqual(deployment_request["body"]["image"], STATIC_IMAGE)
        self.assertEqual(deployment_request["body"]["siteVersion"], 3)
        self.assertEqual(deployment_request["body"]["runAsUser"], 101)
        self.assertNotIn("artifact", deployment_request["body"])

    def test_dynamic_deploy_forwards_exact_migration_artifact(self) -> None:
        migration_sql = "CREATE TABLE IF NOT EXISTS inventory (id BIGINT)"
        responses = self._mcp_exchange(
            [
                {
                    "jsonrpc": "2.0", "id": 51, "method": "tools/call",
                    "params": {
                        "name": "deploy_dynamic",
                        "arguments": {
                            "name": "shop-migration",
                            "deploymentIntent": "Deploy this compatible database update",
                            "image": "registry.example/shop@sha256:" + "a" * 64,
                            "contentSha256": "b" * 64,
                            "changeMode": "incremental",
                            "databaseStrategy": "shared",
                            "databaseCompatibility": "backward-compatible",
                            "schemaChange": "additive",
                            "migrationStrategy": "expand-contract",
                            "migrationSha256": hashlib.sha256(
                                migration_sql.encode("utf-8")
                            ).hexdigest(),
                            "migrationSql": migration_sql,
                            "decisionRationale": "Add one idempotent table",
                        },
                    },
                }
            ]
        )
        self.assertNotIn("isError", responses[0]["result"])
        version_request = next(
            request for request in _Handler.requests
            if request["path"].endswith("/versions")
            and request["method"] == "POST"
        )
        self.assertEqual(version_request["body"]["migrationSql"], migration_sql)
        self.assertEqual(
            version_request["body"]["migrationSha256"],
            hashlib.sha256(migration_sql.encode("utf-8")).hexdigest(),
        )

    def test_source_build_tools_round_trip(self) -> None:
        responses = self._mcp_exchange(
            [
                {
                    "jsonrpc": "2.0", "id": 6, "method": "tools/call",
                    "params": {
                        "name": "source_deploy",
                        "arguments": {
                            "name": "dynamic-web",
                            "deploymentIntent": "Develop the application and then deploy it",
                            "files": {"Dockerfile": "FROM scratch\n"},
                            "port": 8080,
                            "healthPath": "/healthz",
                        },
                    },
                },
                {
                    "jsonrpc": "2.0", "id": 7, "method": "tools/call",
                    "params": {
                        "name": "build_status",
                        "arguments": {"name": "dynamic-web"},
                    },
                },
                {
                    "jsonrpc": "2.0", "id": 8, "method": "tools/call",
                    "params": {
                        "name": "source_delete",
                        "arguments": {"name": "dynamic-web"},
                    },
                },
            ]
        )
        for response in responses:
            self.assertNotIn("isError", response["result"])
        sent = next(r for r in _Handler.requests if r["path"] == "/v1/builds")
        self.assertEqual(sent["body"]["name"], "dynamic-web")
        self.assertEqual(
            sent["body"]["files"]["Dockerfile"], "FROM scratch\n"
        )

    def test_source_deploy_without_root_dockerfile_is_an_error(self) -> None:
        responses = self._mcp_exchange(
            [
                {
                    "jsonrpc": "2.0", "id": 9, "method": "tools/call",
                    "params": {
                        "name": "source_deploy",
                        "arguments": {
                            "name": "bad",
                            "deploymentIntent": "Develop the application and then deploy it",
                            "files": {"app.py": "print(1)"},
                        },
                    },
                }
            ]
        )
        self.assertTrue(responses[0]["result"]["isError"])
        self.assertIn("Dockerfile", responses[0]["result"]["content"][0]["text"])

    def test_the_tool_names_are_the_published_contract(self) -> None:
        """🔴 The tool names are a cross-repository contract, pinned here as a literal.

        They are the one part of this surface that another product hard-codes: an agent
        host builds ``mcp_<server>_<tool>`` from them, and its skills quote the result
        verbatim. When one drifts, that side gets "no such tool" and nothing in the
        message says which repository moved it - so the list is written out rather than
        derived from the code under test, which would agree with any rename.

        The order is the advertised order, because that is what a client sees; and the
        count is asserted separately so a tool added without a decision here is a red
        test rather than a silently wider surface.
        """
        listed = [
            tool["name"]
            for tool in self._mcp_exchange(
                [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}]
            )[0]["result"]["tools"]
        ]
        self.assertEqual(
            listed,
            [
                "capabilities",
                "scaffolds",
                "list",
                "status",
                "deploy_static",
                "deploy_static_versioned",
                "deploy_image",
                "whoami",
                "deploy_dynamic",
                "query_database",
                "versions",
                "deploy_bundle",
                "bundle_status",
                "delete",
                "source_deploy",
                "build_status",
                "source_delete",
            ],
        )
        self.assertEqual(len(listed), 17)

    def test_no_tool_repeats_the_server_name(self) -> None:
        """The rule the list above is an instance of.

        MCP namespaces tools by server, and a client prefixes them again on the way to
        the model. A tool that also names its own server produces
        ``mcp_site_sites_deploy_static``: the product name three times, twice of it
        noise. Asserting the rule as well as the list is deliberate - the list alone
        would be updated by whoever reintroduced a prefix, and would then document the
        mistake instead of catching it.
        """
        for name in [tool["name"] for tool in _tool_definitions(None)]:
            with self.subTest(tool=name):
                self.assertFalse(
                    name.startswith("site"),
                    f"{name} repeats the server name; MCP already namespaces by server",
                )

    def test_tools_publish_mcp_annotations(self) -> None:
        tools = {
            tool["name"]: tool
            for tool in self._mcp_exchange(
                [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}]
            )[0]["result"]["tools"]
        }
        for name in (
            "capabilities", "scaffolds", "list", "status", "whoami",
            "build_status", "bundle_status", "query_database",
            "versions",
        ):
            self.assertTrue(
                tools[name]["annotations"]["readOnlyHint"],
                f"{name} Should be marked readOnlyHint",
            )
        for name in (
            "deploy_static", "deploy_static_versioned", "deploy_image", "deploy_bundle",
            "deploy_dynamic", "source_deploy", "delete", "source_delete",
        ):
            self.assertTrue(
                tools[name]["annotations"]["destructiveHint"],
                f"{name} Should be marked with destructiveHint",
            )

    def test_every_advertised_tool_is_actually_dispatchable(self) -> None:
        """The tools listed must be able to be adjusted - listing them but not wiring them is digging a hole for the agent."""
        listed = self._mcp_exchange(
            [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}]
        )[0]["result"]["tools"]
        calls = {
            "capabilities": {},
            "scaffolds": {},
            "list": {},
            "whoami": {},
            "status": {"name": "web"},
            "bundle_status": {"name": "stack"},
            "deploy_bundle": {
                "name": "stack",
                "deploymentIntent": "Please deploy and launch the entire application",
                "components": [
                    {"name": "api", "image": "example.invalid/api:v1"}
                ],
            },
            "deploy_image": {
                "name": "api",
                "deploymentIntent": "Please deploy this image",
                "image": "example.invalid/api:v1",
            },
            "deploy_dynamic": {
                "name": "shop",
                "deploymentIntent": "Deploy this dynamic site",
                "image": "registry.example/shop@sha256:" + "a" * 64,
                "contentSha256": "a" * 64,
                "changeMode": "incremental",
                "databaseStrategy": "shared",
                "databaseCompatibility": "backward-compatible",
                "schemaChange": "none",
                "migrationStrategy": "none",
                "decisionRationale": "Same framework and compatible schema migration",
            },
            "deploy_static": {
                "name": "flat",
                "deploymentIntent": "Please deploy this static site",
                "files": {"index.html": "<h1>ok</h1>"},
            },
            "deploy_static_versioned": {
                "name": "flat-versioned",
                "deploymentIntent": "Publish this immutable static site",
                "files": {"index.html": "<h1>ok</h1>"},
            },
            "source_deploy": {
                "name": "dyn",
                "deploymentIntent": "Deploy the source code and return the public URL",
                "files": {"Dockerfile": "FROM scratch\n"},
            },
            "build_status": {"name": "dyn"},
            "source_delete": {"name": "dyn"},
            "query_database": {"name": "shop", "query": "SELECT 1"},
            "versions": {"name": "shop"},
        }
        self.assertEqual(
            {tool["name"] for tool in listed} - set(calls),
            # This has real deletion side effects, so test it separately.
            {"delete"},
        )
        for tool_name, arguments in calls.items():
            responses = self._mcp_exchange(
                [
                    {
                        "jsonrpc": "2.0", "id": 9, "method": "tools/call",
                        "params": {"name": tool_name, "arguments": arguments},
                    }
                ]
            )
            result = responses[0]["result"]
            self.assertNotIn(
                "isError", result, f"{tool_name} Not wired: {result}"
            )

    def test_an_mcp_agent_can_read_its_own_quota(self) -> None:
        # When hitting 429, the agent must tell what its upper limit is. Agent accessed via MCP
        # At one time, I could only see deployment tools and couldn't ask about it.
        responses = self._mcp_exchange(
            [
                {
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "whoami", "arguments": {}},
                }
            ]
        )
        quota = responses[0]["result"]["structuredContent"]
        self.assertEqual(quota["maxPublicRoutes"], 1)
        self.assertEqual(_Handler.requests[-1]["path"], "/v1/tenants/self")

    def test_deploy_preflight_blocks_a_known_full_public_quota(self) -> None:
        class FullQuotaClient:
            deployed = False

            def whoami(self) -> dict:
                return {"maxDeployments": 3, "maxPublicRoutes": 1}

            def list_deployments(self) -> dict:
                return {
                    "deployments": [
                        {"serviceName": "existing", "url": "https://existing"}
                    ]
                }

            def deploy(self, _payload: dict) -> dict:
                self.deployed = True
                return {"phase": "Pending"}

        client = FullQuotaClient()
        response = Server(client_factory=lambda: client).handle({
            "jsonrpc": "2.0",
            "id": 31,
            "method": "tools/call",
            "params": {
                "name": "deploy_static",
                "arguments": {
                    "name": "new-site",
                    "files": {"index.html": "ok"},
                    "deploymentIntent": "Please deploy and return to the public network URL",
                    "_agent_deployment_authorization": {
                        "version": 1,
                        "runId": "run-31",
                        "expiresAt": time.time() + 60,
                        "nonce": "n" * 24,
                        "allowInternal": False,
                    },
                },
            },
        })
        result = response["result"]
        self.assertTrue(result["isError"])
        self.assertIn("quota_preflight_blocked", result["content"][0]["text"])
        self.assertFalse(client.deployed)

    def test_build_only_does_not_consume_deployment_quota(self) -> None:
        class FullQuotaClient:
            def whoami(self) -> dict:
                raise AssertionError("build-only must not inspect deployment quota")

            def list_deployments(self) -> dict:
                raise AssertionError("build-only must not inspect deployments")

            def create_build(self, payload: dict) -> dict:
                self.payload = payload
                return {"phase": "Pending", "buildOnly": payload["buildOnly"]}

        client = FullQuotaClient()
        response = Server(client_factory=lambda: client).handle({
            "jsonrpc": "2.0",
            "id": 311,
            "method": "tools/call",
            "params": {
                "name": "source_deploy",
                "arguments": {
                    "name": "new-build",
                    "files": {"Dockerfile": "FROM scratch\n"},
                    "buildOnly": True,
                    "deploymentIntent": "Build an immutable image for deployment",
                    "_agent_deployment_authorization": {
                        "version": 1,
                        "runId": "run-311",
                        "expiresAt": time.time() + 60,
                        "nonce": "b" * 24,
                        "allowInternal": False,
                    },
                },
            },
        })
        result = response["result"]
        self.assertFalse(result.get("isError", False))
        self.assertTrue(result["structuredContent"]["buildOnly"])
        self.assertTrue(client.payload["buildOnly"])

    def test_same_name_update_bypasses_preflight_and_reports_policy(self) -> None:
        class FullQuotaClient:
            def whoami(self) -> dict:
                return {"maxDeployments": 1, "maxPublicRoutes": 1}

            def list_deployments(self) -> dict:
                return {
                    "deployments": [
                        {"serviceName": "existing", "url": "https://existing"}
                    ]
                }

            def deploy(self, _payload: dict) -> dict:
                return {"phase": "Pending"}

        response = Server(client_factory=FullQuotaClient).handle({
            "jsonrpc": "2.0",
            "id": 32,
            "method": "tools/call",
            "params": {
                "name": "deploy_static",
                "arguments": {
                    "name": "existing",
                    "files": {"index.html": "updated"},
                    "deploymentIntent": "Please deploy and return to the public network URL",
                    "_agent_deployment_authorization": {
                        "version": 1,
                        "runId": "run-32",
                        "expiresAt": time.time() + 60,
                        "nonce": "n" * 24,
                        "allowInternal": False,
                    },
                },
            },
        })
        payload = response["result"]["structuredContent"]
        self.assertEqual(payload["phase"], "Pending")
        self.assertEqual(payload["deploymentAuthorization"], {
            "policyVersion": 1,
            "allowInternal": False,
        })

    def test_administration_is_deliberately_not_an_mcp_tool(self) -> None:
        """To create/stop merchants and tenants, only the admin token and CLI are used, not the agent.

        The superficial reason is that the MCP server takes the credentials of a certain tenant or merchant, and adjusting the admin endpoint will only cause instability.
        403; The real reason is that giving agent administrator capabilities is equivalent to incorporating override into the product - one that can build
        Merchants and tools for changing quotas will be used sooner or later in a "help me increase my quota".
        """
        listed = self._mcp_exchange(
            [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}]
        )[0]["result"]["tools"]
        names = {tool["name"] for tool in listed}
        self.assertFalse(
            {
                name
                for name in names
                if ("tenant" in name or "merchant" in name or "admin" in name)
                and "whoami" not in name
            }
        )

    def test_every_tool_result_tells_the_agent_which_merchant_it_acted_as(
        self,
    ) -> None:
        """The identity is divided into two parts (merchant, tenant). Only the userId is reported and there is no deployment.

        The control plane will bring the merchantId itself; what is tested here is that it will not be lost when it is not brought - the supplementary copy
        Comes from capabilities, not client-programmed constants.
        """
        # The fake control plane's /v1/deployments deliberately does not have merchantId.
        without_capabilities = self._mcp_exchange(
            [
                {
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "list", "arguments": {}},
                }
            ]
        )
        # If you haven't asked about capabilities yet, there is no source to fill in, so you shouldn't create one out of thin air at this time.
        self.assertNotIn(
            "merchantId", without_capabilities[0]["result"]["structuredContent"]
        )

        with_capabilities = self._mcp_exchange(
            [
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                {
                    "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "list", "arguments": {}},
                },
            ]
        )
        self.assertEqual(
            with_capabilities[1]["result"]["structuredContent"]["merchantId"],
            CAPABILITIES["merchantId"],
        )

    def test_a_server_supplied_merchant_is_never_overwritten(self) -> None:
        # The identity in the cache should not be changed from what the server just said: after changing the credentials, the previous merchant was still read.
        # This is the easiest illusion to be created by this kind of "just patching things up".
        responses = self._mcp_exchange(
            [
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                {
                    "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "whoami", "arguments": {}},
                },
            ]
        )
        identity = responses[1]["result"]["structuredContent"]
        self.assertNotEqual(CAPABILITIES["merchantId"], "beta")
        self.assertEqual(identity["merchantId"], "beta")
        self.assertEqual(identity["userId"], "bob")

    def test_unknown_method_is_a_protocol_error(self) -> None:
        responses = self._mcp_exchange(
            [{"jsonrpc": "2.0", "id": 4, "method": "nope"}]
        )
        self.assertEqual(responses[0]["error"]["code"], -32601)

    def test_caller_identity_argument_selects_a_per_call_user_client(self) -> None:
        # The reserved parameters are injected by connector_runtime (the value given by the model has been stripped there), here
        # Verification only: The consumer presses it to change the per-call identity, and the parameters themselves no longer leak into the business payload.
        users = []

        class _RecordingClient:
            def __init__(self, user_id: str) -> None:
                self.user_id = user_id
                users.append(user_id)

            def list_deployments(self) -> dict:
                return {"merchantId": "local", "userId": self.user_id}

        server = Server(
            client_factory=lambda: _RecordingClient("local"),
            user_client_factory=lambda user_id: _RecordingClient(user_id),
        )
        response = server.handle(
            {
                "jsonrpc": "2.0", "id": 7, "method": "tools/call",
                "params": {
                    "name": "list",
                    "arguments": {CALLER_USER_ARGUMENT: SUBJECT},
                },
            }
        )
        payload = response["result"]["structuredContent"]
        self.assertFalse(response["result"].get("isError", False))
        # Forwarded unchanged: the runtime that knows the real account derived it, and
        # deriving again here would silently name a different tenant.
        self.assertEqual(users, [SUBJECT])
        self.assertEqual(payload["userId"], SUBJECT)

    def test_a_real_account_identifier_is_refused_not_mapped(self) -> None:
        """🔴 An account identifier must never be turned into a subject here.

        This server sees a pseudonym or nothing. If it mapped an email to something
        acceptable, the tenant it named would depend on *this* deployment's salt rather
        than on the runtime that knows who the user is - two sides silently meaning two
        different tenants, with a 2xx on both. Refusing is the only answer that cannot be
        wrong, and it names what the caller must fix.
        """
        for raw in (
            "Convee.cn@Example.com",
            "alice",
            SUBJECT.upper(),
            SUBJECT[:31],
            SUBJECT + "a",
            "u-0123456789abcdef01234567",
        ):
            with self.subTest(caller=raw):
                with self.assertRaises(SitesError) as caught:
                    _caller_subject(raw)
                self.assertEqual(
                    caught.exception.code, "sites_invalid_caller_identity"
                )
        # Forward comparison: a real pseudonym passes through untouched, so the check
        # above is a filter and not a blanket refusal.
        self.assertEqual(_caller_subject(SUBJECT), SUBJECT)
        self.assertEqual(_caller_subject(f"  {SUBJECT}  "), SUBJECT)
        self.assertEqual(_caller_subject(""), "")

    def test_a_bad_caller_identity_is_an_mcp_error_not_a_dead_process(self) -> None:
        # The refusal has to reach the agent as a result it can act on, the same way a
        # malformed subject does - not as a traceback that kills the stdio server.
        server = Server(client_factory=lambda: Client(self.url, SERVICE_TOKEN))
        response = server.handle(
            {
                "jsonrpc": "2.0", "id": 11, "method": "tools/call",
                "params": {
                    "name": "list",
                    "arguments": {CALLER_USER_ARGUMENT: "alice@example.com"},
                },
            }
        )
        self.assertTrue(response["result"]["isError"])
        self.assertIn("pseudonym", response["result"]["content"][0]["text"])

    def test_caller_identity_argument_defaults_to_the_service_identity(self) -> None:
        users = []

        class _RecordingClient:
            def __init__(self, user_id: str) -> None:
                self.user_id = user_id
                users.append(user_id)

            def list_deployments(self) -> dict:
                return {"merchantId": "local"}

        server = Server(
            client_factory=lambda: _RecordingClient("local"),
            user_client_factory=lambda user_id: _RecordingClient(user_id),
        )
        response = server.handle(
            {
                "jsonrpc": "2.0", "id": 8, "method": "tools/call",
                "params": {"name": "list", "arguments": {}},
            },
        )
        self.assertFalse(response["result"].get("isError", False))
        # No reserved parameters → use env default identity, do not construct per-call client
        self.assertEqual(users, ["local"])


class DeployContractTests(unittest.TestCase):
    """The three-sided nail in the deploy parameter contract: the single source of truth is common.DEPLOY_FIELDS.

    This list has been in three places (normalize's acceptance logic, MCP's inputSchema, MCP's forwarding
    Whitelist) were copied by hand, and the drifting forms were "the model is invisible, but the hand is blocked but is forwarded" and "an entrance
    Some switches can never give another entrance." Here it is asserted from the behavioral point of view that the three sides are consistent: change any side and
    Forgot to synchronize, red in this place first. The payload key set of CLI is only required to be a subset of it - CLI
    Deliberately only send fields that the user has given.
    """

    def test_a_grant_file_no_longer_authorizes_a_deployment(self) -> None:
        """The file-backed second authorization path is gone, not merely unused.

        It was a complete alternative to the runtime-injected authorization,
        reachable from the environment alone: point SITES_DEPLOYMENT_AUTHORIZATION_FILE
        at a 0600 file you own and six deploy tools accepted it. The guards on it were
        tight, but it was a second answer to a question that already had one. This
        drives the real tools/call path, so restoring the loader fails here.
        """
        class RefusingClient:
            deployed = False

            def whoami(self) -> dict:
                return {"maxDeployments": 10, "maxPublicRoutes": 10}

            def list_deployments(self) -> dict:
                return {"deployments": []}

            def deploy(self, _payload: dict) -> dict:
                self.deployed = True
                return {"phase": "Pending"}

        grant = {
            "version": 1,
            "runId": "codex-e2e",
            "nonce": "n" * 24,
            "expiresAt": time.time() + 60,
            "allowInternal": False,
            "allowedTools": ["deploy_static"],
            "siteNamePrefix": "codex-e2e-",
            "deploymentIntents": ["please deploy and return the public URL"],
        }
        client = RefusingClient()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "grant.json"
            path.write_text(json.dumps(grant), encoding="utf-8")
            path.chmod(0o600)
            with unittest.mock.patch.dict(
                os.environ,
                {"SITES_DEPLOYMENT_AUTHORIZATION_FILE": str(path)},
            ):
                response = Server(client_factory=lambda: client).handle({
                    "jsonrpc": "2.0",
                    "id": 91,
                    "method": "tools/call",
                    "params": {
                        "name": "deploy_static",
                        "arguments": {
                            "name": "codex-e2e-static",
                            "files": {"index.html": "ok"},
                            "deploymentIntent": "please deploy and return the public URL",
                        },
                    },
                })
        result = response["result"]
        self.assertFalse(
            client.deployed, "a grant file authorized a deployment on its own"
        )
        self.assertTrue(result.get("isError", False), result)
        self.assertIn(
            "deployment_authorization_required", result["content"][0]["text"]
        )

    def test_mcp_schema_advertises_exactly_the_deploy_fields(self) -> None:
        tools = {tool["name"]: tool for tool in _tool_definitions(None)}
        properties = tools["deploy_image"]["inputSchema"]["properties"]
        self.assertEqual(
            set(properties) - {"deploymentIntent"}, set(DEPLOY_FIELDS)
        )
        # The two attributes of back-filling were once left out of the schema: padding the key but not the type equals no padding.
        self.assertEqual(properties["secretMounts"]["type"], "array")
        self.assertEqual(properties["runAsUser"]["type"], "integer")

    def test_mcp_forwarding_whitelist_is_exactly_the_deploy_fields(self) -> None:
        # The whitelist is inlined in dispatch, and the only discriminating way to ask from the outside is at the behavioral level:
        # All keys in the list must be forwarded, and none outside the list are allowed to pass.
        seen: list[dict] = []

        class _RecordingClient:
            def capabilities(self) -> dict:
                return {}

            def whoami(self) -> dict:
                return {"maxDeployments": 3, "maxPublicRoutes": 1}

            def list_deployments(self) -> dict:
                return {"deployments": []}

            def deploy(self, payload: dict) -> dict:
                seen.append(payload)
                return {"accepted": payload.get("name")}

        server = Server(client_factory=_RecordingClient)
        response = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "deploy_image",
                    "arguments": {
                        "name": "api",
                        "deploymentIntent": "Please deploy this image",
                        "_agent_deployment_authorization": {
                            "version": 1,
                            "runId": "run-recording",
                            "expiresAt": 4000000000,
                            "nonce": "n" * 24,
                        },
                        "image": "example.invalid/api:v1",
                        "port": 8080,
                        "healthPath": "/",
                        "livenessPath": "/live",
                        "exposure": "public",
                        "env": [],
                        "secretMounts": [],
                        "runAsUser": 10001,
                        "scaleToZero": True,
                        "memoryLimit": "1Gi",
                        "siteVersion": 7,
                        # Keys outside the list: artifact is a direct cast mode selector, forward it
                        # Bypasses the index.html enforcement of deploy_static.
                        "artifact": {"files": {"index.html": "x"}},
                    },
                },
            }
        )
        self.assertFalse(response["result"].get("isError", False))
        self.assertEqual(set(seen[0]), set(DEPLOY_FIELDS))
        self.assertNotIn("artifact", seen[0])

    def test_cli_deploy_payload_stays_within_the_deploy_fields(self) -> None:
        # To satisfy all flags: the keyset must fall exactly in DEPLOY_FIELDS, and each switch
        # It's really connected - forgetting to connect the flag will make deploy always run with the default value.
        # The caller sees nothing unusual in the response.
        args = build_parser().parse_args(
            [
                "deploy",
                "--name", "api",
                "--image", "example.invalid/api:v1",
                "--liveness-path", "/live",
                "--run-as-user", "10005",
                "--env", "MODE=prod",
                "--secret-env", "TOKEN=app-keys/token",
                "--secret-mount", "app-keys:/var/run/keys",
                "--scale-to-zero",
                "--memory-limit", "1Gi",
                "--site-version", "7",
            ]
        )
        payload = _deploy_payload(args)
        self.assertLessEqual(set(payload), set(DEPLOY_FIELDS))
        for field in DEPLOY_FIELDS:
            self.assertIn(field, payload, f"{field} Not accessing CLI payload")
        self.assertIs(payload["scaleToZero"], True)
        self.assertEqual(payload["memoryLimit"], "1Gi")
        self.assertEqual(payload["siteVersion"], 7)

    def test_cli_deploy_omits_unset_options(self) -> None:
        # The switch that is not given must not appear as a whole key, instead of sending the default value: scaleToZero=False
        # After exiting, the control plane couldn't tell the difference between "the caller explicitly wants to default" and "it wasn't mentioned at all".
        args = build_parser().parse_args(
            ["deploy", "--name", "api", "--image", "example.invalid/api:v1"]
        )
        payload = _deploy_payload(args)
        self.assertNotIn("scaleToZero", payload)
        self.assertNotIn("memoryLimit", payload)
        self.assertNotIn("runAsUser", payload)
        self.assertNotIn("siteVersion", payload)
        # The default of --liveness-path is an empty string instead of None: judging by non-None will replace ""
        # Send it out, but the empty path cannot pass _probe_path, and deploy without a switch will directly get 400.
        self.assertNotIn("livenessPath", payload)

    def test_the_static_runtime_image_has_one_home(self) -> None:
        # Old account of dual source drift: CLI used to default to 1.27 and MCP used 1.29 - the same static direct cast,
        # Different entrances deploy different runtimes. canonical is only quoted on both sides of common,.
        args = build_parser().parse_args(
            ["deploy-static", "--name", "flat", "--directory", "."]
        )
        self.assertEqual(args.image, STATIC_IMAGE)
        self.assertIn("@sha256:", STATIC_IMAGE)


class DeploymentIntentBoundaryTests(unittest.TestCase):
    def test_preview_only_intent_is_rejected_before_dispatch(self) -> None:
        from sites import mcp as sites_mcp

        for intent in ("", "Just preview online, don't deploy", "preview only"):
            with self.subTest(intent=intent), self.assertRaises(
                sites_mcp.ValidationError
            ) as caught:
                sites_mcp._require_deployment_intent(
                    {"deploymentIntent": intent}
                )
            self.assertIn("Do not retry Sites", str(caught.exception))

    def test_explicit_deployment_excerpt_is_accepted(self) -> None:
        from sites import mcp as sites_mcp

        self.assertEqual(
            sites_mcp._require_deployment_intent({
                "deploymentIntent": "Develop the application and then deploy it and return the public URL",
                "_agent_deployment_authorization": {
                    "version": 1,
                    "runId": "run-explicit",
                    "expiresAt": 4000000000,
                    "nonce": "n" * 24,
                },
            }),
            "Develop the application and then deploy it and return the public URL",
        )

    def test_trusted_context_still_requires_the_intent_excerpt(self) -> None:
        from sites import mcp as sites_mcp

        authorization = {
            "version": 1,
            "runId": "run-intent",
            "expiresAt": 4000000000,
            "nonce": "n" * 24,
        }
        for intent in ("", "x", "x" * 201):
            with self.subTest(intent=intent), self.assertRaises(
                sites_mcp.ValidationError
            ) as caught:
                sites_mcp._require_deployment_intent({
                    "deploymentIntent": intent,
                    "_agent_deployment_authorization": authorization,
                })
            self.assertIn("deployment_intent_required", str(caught.exception))

    def test_malformed_tool_arguments_return_a_tool_error(self) -> None:
        response = Server(client_factory=lambda: object()).handle({
            "jsonrpc": "2.0",
            "id": "bad-arguments",
            "method": "tools/call",
            "params": {
                "name": "list",
                "arguments": ["not", "an", "object"],
            },
        })
        result = response["result"]
        self.assertTrue(result["isError"])
        self.assertIn("tool arguments must be a JSON object", result["content"][0]["text"])

    def test_public_request_cannot_be_downgraded_to_internal(self) -> None:
        from sites import mcp as sites_mcp

        arguments = {
            "exposure": "internal",
            "_agent_deployment_authorization": {
                "version": 1,
                "runId": "run-public",
                "expiresAt": 4000000000,
                "nonce": "n" * 24,
                "allowInternal": False,
            },
        }
        with self.assertRaises(sites_mcp.ValidationError) as caught:
            sites_mcp._require_standalone_exposure_authorization(arguments)
        self.assertIn(
            "internal_exposure_authorization_required", str(caught.exception)
        )
        self.assertIn("Accessible URLs will not be returned", str(caught.exception))

    def test_explicit_internal_request_allows_internal_exposure(self) -> None:
        from sites import mcp as sites_mcp

        sites_mcp._require_standalone_exposure_authorization({
            "exposure": "internal",
            "_agent_deployment_authorization": {
                "version": 1,
                "runId": "run-internal",
                "expiresAt": 4000000000,
                "nonce": "n" * 24,
                "allowInternal": True,
            },
        })

    def test_gateway_unbounded_capacity_is_rendered_unambiguously(self) -> None:
        from sites import mcp as sites_mcp

        sentence = sites_mcp._limits_sentence({
            "limits": {"publicRoutes": None},
            "deploymentModes": {},
            "features": {},
        })
        self.assertIn("there is no configured public-route capacity limit", sentence)
        self.assertNotIn("None", sentence)


if __name__ == "__main__":
    unittest.main()
