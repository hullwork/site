from __future__ import annotations

import inspect
import ipaddress
import json
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from sites.broker import mcp_client


class _FakeMCPHandler(BaseHTTPRequestHandler):
    tool_calls: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length)) if length else {}
        method = body.get("method")
        type(self).tool_calls.append({"method": method, "authorization": self.headers.get("Authorization")})
        response = None
        if method == "initialize":
            response = {"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2024-11-05"}}
        elif method == "notifications/initialized":
            self.send_response(202)
            self.end_headers()
            return
        elif method == "tools/list":
            response = {
                "jsonrpc": "2.0", "id": body["id"],
                "result": {"tools": [
                    {"name": "search_docs", "description": "search", "inputSchema": {"type": "object"}}
                ]},
            }
        elif method == "tools/call":
            if body["params"]["name"] == "boom":
                response = {"jsonrpc": "2.0", "id": body["id"],
                            "result": {"isError": True, "content": "it broke"}}
            else:
                response = {"jsonrpc": "2.0", "id": body["id"],
                            "result": {"isError": False, "content": [{"type": "text", "text": "ok"}]}}
        else:
            response = {"jsonrpc": "2.0", "id": body.get("id"), "error": {"message": "unknown method"}}
        payload = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:
        pass


class FakeMCPServerCase(unittest.TestCase):
    def setUp(self) -> None:
        _FakeMCPHandler.tool_calls = []
        self.server = HTTPServer(("127.0.0.1", 0), _FakeMCPHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.thread.join, timeout=5)
        self.url = f"http://127.0.0.1:{self.server.server_port}/mcp"


class EndToEndWithLoopbackOverrideTests(FakeMCPServerCase):
    """These only work because the test override allows http+loopback."""

    def test_initialize_list_and_call_round_trip(self) -> None:
        with mcp_client._allow_loopback_for_tests():
            client = mcp_client.MCPClient(self.url, bearer_token="secret-token")
            client.initialize()
            tools = client.list_tools()
            self.assertEqual([t.name for t in tools], ["search_docs"])
            result = client.call_tool("search_docs", {"q": "hi"})
        self.assertFalse(result.is_error)
        self.assertEqual(result.content, [{"type": "text", "text": "ok"}])
        auths = [c["authorization"] for c in _FakeMCPHandler.tool_calls]
        self.assertTrue(all(a == "Bearer secret-token" for a in auths))

    def test_tool_error_surface(self) -> None:
        with mcp_client._allow_loopback_for_tests():
            client = mcp_client.MCPClient(self.url)
            result = client.call_tool("boom", {})
        self.assertTrue(result.is_error)
        self.assertEqual(result.content, "it broke")

    def test_override_is_scoped_to_the_with_block(self) -> None:
        with mcp_client._allow_loopback_for_tests():
            pass
        with self.assertRaises(mcp_client.SSRFRejected):
            mcp_client.MCPClient(self.url)


class SSRFWithoutOverrideTests(FakeMCPServerCase):
    def test_http_to_loopback_refused_without_the_test_override(self) -> None:
        with self.assertRaises(mcp_client.SSRFRejected):
            mcp_client.MCPClient(self.url)

    def test_https_scheme_required_by_default(self) -> None:
        with self.assertRaises(mcp_client.SSRFRejected):
            mcp_client._validate_capability_url("http://example.test/mcp")

    def test_userinfo_query_fragment_rejected(self) -> None:
        for bad in (
            "https://user:pass@example.test/mcp",
            "https://example.test/mcp?x=1",
            "https://example.test/mcp#frag",
        ):
            with self.assertRaises(mcp_client.SSRFRejected):
                mcp_client._validate_capability_url(bad)

    def test_control_characters_rejected(self) -> None:
        with self.assertRaises(mcp_client.SSRFRejected):
            mcp_client._validate_capability_url("https://example.test/\x00mcp")


class AddressClassificationTests(unittest.TestCase):
    def test_public_address_allowed(self) -> None:
        self.assertTrue(mcp_client._is_public_address(ipaddress.ip_address("8.8.8.8")))

    def test_private_loopback_link_local_multicast_reserved_refused(self) -> None:
        for raw in ("10.0.0.5", "127.0.0.1", "169.254.169.254", "224.0.0.1", "0.0.0.0", "192.168.1.1"):
            with self.subTest(raw=raw):
                self.assertFalse(mcp_client._is_public_address(ipaddress.ip_address(raw)))

    def test_ipv4_mapped_ipv6_unwrapped_before_classification(self) -> None:
        mapped_public = ipaddress.ip_address("::ffff:8.8.8.8")
        mapped_private = ipaddress.ip_address("::ffff:10.0.0.1")
        self.assertTrue(mcp_client._is_public_address(mapped_public))
        self.assertFalse(mcp_client._is_public_address(mapped_private))

    def test_loopback_override_allows_only_loopback_not_private(self) -> None:
        with mcp_client._allow_loopback_for_tests():
            self.assertTrue(mcp_client._is_public_address(ipaddress.ip_address("127.0.0.1")))
            self.assertFalse(mcp_client._is_public_address(ipaddress.ip_address("10.0.0.5")))

    def test_override_flag_is_not_reachable_from_the_public_constructor(self) -> None:
        # The only way to reach the test override is importing the private context
        # manager directly; MCPClient itself takes no parameter that could let a
        # tenant's settings turn this on.
        params = inspect.signature(mcp_client.MCPClient.__init__).parameters
        self.assertNotIn("allow_loopback", params)
        self.assertNotIn("insecure", params)
        self.assertNotIn("verify", params)


class ResolvePublicMonkeypatchTests(unittest.TestCase):
    def test_refuses_when_any_answer_is_private(self) -> None:
        original = socket.getaddrinfo

        def fake_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
            if host == "internal.example.test":
                return [
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port)),
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.9", port)),
                ]
            return original(host, port, family, type, proto, flags)

        socket.getaddrinfo = fake_getaddrinfo
        try:
            with self.assertRaises(mcp_client.SSRFRejected):
                mcp_client._resolve_public("internal.example.test", 443)
        finally:
            socket.getaddrinfo = original

    def test_dns_failure_is_an_ssrf_rejection_not_a_crash(self) -> None:
        original = socket.getaddrinfo

        def fake_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
            raise socket.gaierror("nope")

        socket.getaddrinfo = fake_getaddrinfo
        try:
            with self.assertRaises(mcp_client.SSRFRejected):
                mcp_client._resolve_public("nowhere.example.test", 443)
        finally:
            socket.getaddrinfo = original


class UnavailableTests(FakeMCPServerCase):
    def test_unknown_method_error_frame_raises_unavailable(self) -> None:
        with mcp_client._allow_loopback_for_tests():
            client = mcp_client.MCPClient(self.url)
            with self.assertRaises(mcp_client.MCPUnavailable):
                client._call("not/a/real/method")

    def test_unreachable_server_raises_unavailable(self) -> None:
        with mcp_client._allow_loopback_for_tests():
            client = mcp_client.MCPClient("http://127.0.0.1:1/mcp", timeout_seconds=1)
            with self.assertRaises(mcp_client.MCPUnavailable):
                client.initialize()


class CanonicalDigestTests(unittest.TestCase):
    def test_digest_is_order_independent(self) -> None:
        a = mcp_client.canonical_tool_digest({"name": "x", "description": "d"})
        b = mcp_client.canonical_tool_digest({"description": "d", "name": "x"})
        self.assertEqual(a, b)

    def test_digest_changes_with_content(self) -> None:
        a = mcp_client.canonical_tool_digest({"name": "x"})
        b = mcp_client.canonical_tool_digest({"name": "y"})
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
