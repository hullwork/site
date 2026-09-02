"""The MCP tool surface reached over HTTP, against a real control plane.

Every case here goes through a real socket, a real ``sites.api.Handler`` and a real
PostgreSQL store, faking only Kubernetes. That is deliberate and it is the point of the
file: the change under test moved a tool surface from a pipe inside the caller's own
container onto the network, and the whole question is whether the *server* still decides
who the caller is. A test that stubbed ``_authenticate``, the store, or the loopback
client would answer a different question - whether this module's own bookkeeping is
self-consistent - and would keep answering yes after the tenancy check was removed.
"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest

from sites import console_session
from sites.api import Handler, _route_template
from sites.api_mcp import endpoint_enabled_from_env
from sites.mcp import CALLER_USER_ARGUMENT, Server, _tool_definitions
from sites.naming import token_digest
from tests.test_support import postgres_store
from tests.test_tenancy import _FakeKube


ADMIN_TOKEN = "a" * 32
SESSION_KEY = "k" * 32

ACME_TENANT_TOKEN = "acme-tenant-token-" + "1" * 32
RIVAL_TENANT_TOKEN = "rival-tenant-token-" + "2" * 32
ACME_MERCHANT_KEY = "acme-merchant-key-" + "k" * 32
RIVAL_MERCHANT_KEY = "rival-merchant-key-" + "j" * 32

# A well-formed acting-subject pseudonym: 32 lowercase hex, the only shape the control
# plane accepts. Fixed rather than derived - the derivation is tested in test_interface,
# here it only has to be a legal value that is nobody's real identifier.
SUBJECT = "aaaaaaaabbbbbbbbccccccccdddddddd"
# A second pseudonym, used only by the spoof cases. Separate from SUBJECT on purpose:
# the impersonation case above legitimately creates a tenant row for SUBJECT, so
# asserting "no row appeared" against that same value would be checking the other test.
SPOOF_SUBJECT = "eeeeeeeeffffffff00000000111111ff"


def _authorization() -> dict:
    """A server-issued deployment authorization that has not expired.

    🔴 This is the *calling runtime's* artifact, not a Sites credential. It can only
    stop a write, never permit one: without a Sites credential the request is refused
    at the transport before any of this is read (see test_no_credential_is_refused).
    """
    import time

    return {
        "version": 1,
        "runId": "run-mcp-http",
        "nonce": "nonce-abcdefghijklmnopqrstuvwxyz",
        "expiresAt": time.time() + 3600,
    }


class McpOverHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        store = postgres_store(Path(cls._tmp.name) / "mcp-http.db")
        store.migrate()
        # Two merchants, so "credential A reaches for tenant B's resource" is a real
        # question rather than a restatement of "that row does not exist".
        store.create_merchant(
            "acme", "Acme", token_digest(ACME_MERCHANT_KEY), 10, 20,
            may_act_as_subjects=True,
        )
        store.create_merchant(
            "rival", "Rival", token_digest(RIVAL_MERCHANT_KEY), 10, 20,
            may_act_as_subjects=False,
        )
        store.create_tenant(
            "acme", "alice", token_digest(ACME_TENANT_TOKEN),
            max_deployments=5, max_public_routes=5,
        )
        store.create_tenant(
            "rival", "mallory", token_digest(RIVAL_TENANT_TOKEN),
            max_deployments=5, max_public_routes=5,
        )
        Handler.kube = _FakeKube()
        Handler.store = store
        Handler.service_token = ADMIN_TOKEN
        Handler.session_key = SESSION_KEY
        Handler.local_login_enabled = True
        Handler.oidc_config = None
        Handler.mutation_lock = threading.Lock()
        Handler.synchronizer = None
        Handler.static_artifacts = None
        Handler.mcp_endpoint_enabled = True
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls._tmp.cleanup()

    def setUp(self) -> None:
        Handler.kube.objects.clear()
        Handler.mcp_endpoint_enabled = True

    # --- helpers -------------------------------------------------------
    def post(
        self,
        message: dict | list | None,
        token: str = ACME_TENANT_TOKEN,
        *,
        method: str = "POST",
        subject: str = "",
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict]:
        body = (
            json.dumps(message).encode("utf-8") if message is not None else None
        )
        wire = {"Accept": "application/json, text/event-stream"}
        if token:
            wire["X-Sites-Service-Token"] = token
        if subject:
            wire["X-Acting-Subject"] = subject
        if body is not None:
            wire["Content-Type"] = "application/json"
        wire.update(headers or {})
        request = urlrequest.Request(
            f"{self.url}/mcp", data=body, method=method, headers=wire
        )
        try:
            with urlrequest.urlopen(request, timeout=20) as response:
                raw = response.read()
                return int(response.status), json.loads(raw or b"{}")
        except urlerror.HTTPError as exc:
            return int(exc.code), json.loads(exc.read() or b"{}")

    def call(self, name: str, arguments: dict, **kwargs) -> tuple[int, dict]:
        return self.post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            **kwargs,
        )

    @staticmethod
    def structured(payload: dict) -> dict:
        return payload["result"]["structuredContent"]

    @staticmethod
    def tool_error(payload: dict) -> str:
        result = payload["result"]
        assert result.get("isError"), result
        return result["content"][0]["text"]

    def deploy(self, name: str, html: str, **kwargs) -> tuple[int, dict]:
        return self.call(
            "deploy_static",
            {
                "name": name,
                "files": {"index.html": html},
                "deploymentIntent": "publish this brochure at a public URL",
                "_agent_deployment_authorization": _authorization(),
            },
            **kwargs,
        )

    # --- transport -----------------------------------------------------
    def test_initialize_echoes_the_client_protocol_version(self) -> None:
        status, payload = self.post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
            }
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(payload["result"]["serverInfo"]["name"], "sites")

    def test_http_advertises_exactly_the_same_tools_as_the_stdio_surface(self) -> None:
        """One tool surface, two transports.

        The value of this assertion is not the count. It is that a tool added, renamed
        or gated on one transport and not the other would show up here instead of as an
        agent discovering that a tool it was told about does not exist.
        """
        status, payload = self.post(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )
        self.assertEqual(status, 200, payload)
        over_http = [tool["name"] for tool in payload["result"]["tools"]]
        self.assertEqual(over_http, [t["name"] for t in _tool_definitions(None)])

    def test_get_and_delete_are_method_not_allowed(self) -> None:
        for method in ("GET", "DELETE"):
            with self.subTest(method=method):
                status, _ = self.post(None, method=method)
                self.assertEqual(status, 405)

    def test_a_json_rpc_batch_is_refused(self) -> None:
        # Batching left the MCP specification in 2025-06-18 and this server never
        # accepted it. An array must be a refusal, not a silently ignored first element.
        status, payload = self.post([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["code"], "sites_invalid_input")

    def test_a_notification_is_accepted_without_a_body(self) -> None:
        status, payload = self.post(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        self.assertEqual(status, 202, payload)
        self.assertEqual(payload, {})

    def test_a_browser_origin_is_refused(self) -> None:
        status, payload = self.call(
            "whoami", {}, headers={"Origin": "https://evil.invalid"}
        )
        self.assertEqual(status, 403, payload)
        self.assertEqual(payload["code"], "mcp_origin_refused")

    def test_a_caller_that_only_accepts_a_stream_is_told_so(self) -> None:
        status, payload = self.call(
            "whoami", {}, headers={"Accept": "text/event-stream"}
        )
        self.assertEqual(status, 406, payload)
        self.assertEqual(payload["code"], "mcp_not_acceptable")

    def test_the_route_is_a_metrics_template_not_other(self) -> None:
        # "other" is the unbounded-label bucket. MCP traffic falling into it would make
        # the busiest agent-facing route invisible in sites_api_requests_total.
        self.assertEqual(_route_template("/mcp"), "/mcp")

    def test_the_endpoint_can_be_switched_off(self) -> None:
        Handler.mcp_endpoint_enabled = False
        try:
            status, payload = self.call("whoami", {})
            self.assertEqual(status, 404, payload)
            status, _ = self.post(None, method="GET")
            self.assertEqual(status, 404)
        finally:
            Handler.mcp_endpoint_enabled = True

    def test_the_switch_refuses_a_value_that_is_neither_true_nor_false(self) -> None:
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"SITES_MCP_ENDPOINT_ENABLED": ""}):
            self.assertTrue(endpoint_enabled_from_env())
        with mock.patch.dict(os.environ, {"SITES_MCP_ENDPOINT_ENABLED": "false"}):
            self.assertFalse(endpoint_enabled_from_env())
        with mock.patch.dict(os.environ, {"SITES_MCP_ENDPOINT_ENABLED": "disabled"}):
            with self.assertRaises(RuntimeError):
                endpoint_enabled_from_env()

    # --- identity ------------------------------------------------------
    def test_no_credential_is_refused(self) -> None:
        status, payload = self.call("whoami", {}, token="")
        self.assertEqual(status, 401, payload)
        self.assertEqual(payload["error"], "invalid service token")

    def test_an_invalid_credential_is_refused(self) -> None:
        status, payload = self.call("whoami", {}, token="n" * 40)
        self.assertEqual(status, 401, payload)

    def test_identity_comes_from_the_credential(self) -> None:
        status, payload = self.call("whoami", {})
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.structured(payload)["merchantId"], "acme")
        self.assertEqual(self.structured(payload)["userId"], "alice")

    def test_a_second_credential_is_a_different_tenant(self) -> None:
        status, payload = self.call("whoami", {}, token=RIVAL_TENANT_TOKEN)
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.structured(payload)["merchantId"], "rival")
        self.assertEqual(self.structured(payload)["userId"], "mallory")

    def test_a_declared_merchant_header_is_refused(self) -> None:
        status, payload = self.call(
            "whoami", {}, headers={"X-Merchant-ID": "rival"}
        )
        self.assertEqual(status, 403, payload)
        self.assertIn("X-Merchant-ID", payload["error"])

    def test_a_declared_user_header_is_refused(self) -> None:
        status, payload = self.call(
            "whoami", {}, headers={"X-User-ID": "mallory"}
        )
        self.assertEqual(status, 403, payload)
        self.assertIn("X-User-ID", payload["error"])

    def test_a_console_session_is_not_a_credential_for_this_endpoint(self) -> None:
        """The cookie authenticates everywhere else and must not authenticate here.

        Ambient credentials plus a write endpoint is CSRF; and the loopback hop carries
        a token, not a cookie jar, so honouring one would mean a second way to carry an
        identity across it.
        """
        token, csrf = console_session.issue(
            {"sub": "alice", "mid": "acme", "uid": "alice"}, SESSION_KEY
        )
        status, payload = self.call(
            "whoami",
            {},
            token="",
            headers={
                "Cookie": f"{console_session.COOKIE}={token}",
                console_session.CSRF_HEADER: csrf,
            },
        )
        self.assertEqual(status, 401, payload)
        self.assertEqual(payload["code"], "mcp_token_credential_required")

    def test_a_key_with_the_grant_acts_for_the_named_subject(self) -> None:
        status, payload = self.call(
            "whoami", {}, token=ACME_MERCHANT_KEY, subject=SUBJECT
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.structured(payload)["merchantId"], "acme")
        self.assertEqual(self.structured(payload)["userId"], SUBJECT)

    def test_a_key_without_the_grant_cannot_act_for_a_subject(self) -> None:
        status, payload = self.call(
            "whoami", {}, token=RIVAL_MERCHANT_KEY, subject=SUBJECT
        )
        self.assertEqual(status, 403, payload)
        self.assertIn("not authorized to act for a subject", payload["error"])

    def test_a_tenant_token_cannot_act_for_a_subject(self) -> None:
        status, payload = self.call(
            "whoami", {}, token=ACME_TENANT_TOKEN, subject=SUBJECT
        )
        self.assertEqual(status, 403, payload)
        self.assertIn("not authorized to act for a subject", payload["error"])

    def test_a_subject_named_in_the_arguments_is_refused_not_believed(self) -> None:
        """🔴 The escalation this transport had to close.

        Over stdio the reserved argument was injected by a runtime that had already
        stripped model-supplied values. On the network anyone can put it in the body, so
        it is refused - and the assertion is deliberately two-sided: the call fails
        *and* the identity is unchanged. Checking only the error message would still
        pass if the argument were honoured first and complained about afterwards.
        """
        status, payload = self.call(
            "whoami", {CALLER_USER_ARGUMENT: SUBJECT}
        )
        self.assertEqual(status, 200, payload)
        self.assertIn("X-Acting-Subject", self.tool_error(payload))
        status, after = self.call("whoami", {})
        self.assertEqual(self.structured(after)["userId"], "alice")

    def test_a_subject_in_the_arguments_cannot_redirect_a_write(self) -> None:
        status, payload = self.call(
            "deploy_static",
            {
                "name": "spoofed",
                "files": {"index.html": "<h1>spoof</h1>"},
                "deploymentIntent": "publish this brochure at a public URL",
                "_agent_deployment_authorization": _authorization(),
                CALLER_USER_ARGUMENT: SPOOF_SUBJECT,
            },
        )
        self.assertIn("X-Acting-Subject", self.tool_error(payload))
        # Refused before dispatch, so the named subject never became a tenant either.
        # Tenants are created on first use, which is how a believed spoof would leave a
        # trace even when the deployment itself later failed for some other reason.
        self.assertIsNone(Handler.store.tenant("acme", SPOOF_SUBJECT))
        self.assertEqual(Handler.kube.objects, {})

    # --- tools ---------------------------------------------------------
    def test_a_read_tool_answers_from_the_live_control_plane(self) -> None:
        status, payload = self.call("capabilities", {})
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.structured(payload)["merchantId"], "acme")

    def test_a_write_tool_creates_under_the_caller_tenant(self) -> None:
        status, payload = self.deploy("brochure", "<h1>acme</h1>")
        self.assertEqual(status, 200, payload)
        created = self.structured(payload)
        self.assertEqual(created["merchantId"], "acme")
        self.assertEqual(created["userId"], "alice")
        self.assertEqual(created["serviceName"], "brochure")
        status, readback = self.call("status", {"name": "brochure"})
        self.assertEqual(status, 200, readback)
        self.assertEqual(self.structured(readback)["name"], created["name"])

    def test_one_tenant_cannot_read_another_tenants_deployment(self) -> None:
        status, payload = self.deploy("brochure", "<h1>acme</h1>")
        self.assertEqual(status, 200, payload)
        status, payload = self.call(
            "status", {"name": "brochure"}, token=RIVAL_TENANT_TOKEN
        )
        self.assertEqual(status, 200, payload)
        self.assertIn("deployment not found", self.tool_error(payload))

    def test_one_tenant_cannot_overwrite_another_tenants_deployment(self) -> None:
        """The same service name from two tenants must be two resources.

        The read-side refusal above is not enough on its own: a write that resolved the
        name globally would answer 2xx to both callers and replace one site with the
        other, which is the failure that leaves no error anywhere.
        """
        status, mine = self.deploy("brochure", "<h1>acme</h1>")
        self.assertEqual(status, 200, mine)
        status, theirs = self.deploy(
            "brochure", "<h1>rival</h1>", token=RIVAL_TENANT_TOKEN
        )
        self.assertEqual(status, 200, theirs)
        self.assertNotEqual(
            self.structured(mine)["name"], self.structured(theirs)["name"]
        )
        status, after = self.call("status", {"name": "brochure"})
        self.assertEqual(
            self.structured(after)["revision"], self.structured(mine)["revision"]
        )

    def test_a_write_without_deployment_intent_is_refused(self) -> None:
        status, payload = self.call(
            "deploy_static",
            {
                "name": "no-intent",
                "files": {"index.html": "<h1>x</h1>"},
                "deploymentIntent": "publish this at a public URL",
            },
        )
        self.assertEqual(status, 200, payload)
        self.assertIn(
            "deployment_authorization_required", self.tool_error(payload)
        )
        self.assertEqual(Handler.kube.objects, {})

    def test_an_unknown_tool_is_a_tool_error_not_a_dead_connection(self) -> None:
        status, payload = self.call("sites_rm_rf", {})
        self.assertEqual(status, 200, payload)
        self.assertIn("unknown tool", self.tool_error(payload))


class StdioSurfaceTests(unittest.TestCase):
    """The stdio transport keeps the in-process reserved argument.

    Positive control for the refusal above: the two transports differ in exactly one
    place, and that place is a constructor flag rather than an accident.
    """

    def test_stdio_still_accepts_the_reserved_caller_argument(self) -> None:
        self.assertTrue(Server()._subject_from_arguments)

    def test_the_http_transport_turns_it_off(self) -> None:
        self.assertFalse(Server(subject_from_arguments=False)._subject_from_arguments)
