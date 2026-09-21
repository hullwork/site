"""Tool registry / gate / executor tests.

Pure gate-logic tests (permission, entitlement, risk, and their ordering) use a
FakeLedger and need no database. Anything that touches credits usage, records an
approval row, or exercises the HTTP handlers end-to-end needs real PostgreSQL and
is gated on HULL_TEST_PG_DSN (see tests/test_broker_ledger.py for the convention);
per the partition brief, these build Request/Services directly with a small in-test
fake ConfigStore rather than importing partition C's still-skeleton modules.
"""
from __future__ import annotations

import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

import psycopg

from sites.broker import entities as entities_mod
from sites.broker import mcp_client
from sites.broker import tools
from sites.broker.config_store import AppRuntime, ConfigStore
from sites.broker.db import BrokerDatabase, migrate
from sites.broker.http import HttpError, Request, Services
from sites.broker.identity import AppCaller, EndUser
from sites.broker.ledger import Ledger

_PG_DSN = os.environ.get("HULL_TEST_PG_DSN", "")


class _FakeLedger:
    def __init__(self, period: str = "2027-01", usage: dict | None = None) -> None:
        self._period = period
        self._usage = usage or {}

    def period_for(self) -> str:
        return self._period

    def usage(self, app_env_id: str, subject_type: str, subject_id: str, period: str) -> int:
        return self._usage.get((app_env_id, subject_type, subject_id, period), 0)


class _FakeConfigStore(ConfigStore):
    def __init__(self, secrets: dict[tuple[str, str], str] | None = None) -> None:
        self._secrets = secrets or {}

    def active(self, app_env_id: str) -> AppRuntime | None:
        return None

    def by_host(self, host: str) -> str | None:
        return None

    def secret(self, app_env_id: str, name: str) -> str | None:
        return self._secrets.get((app_env_id, name))


CONFIG = {
    "permissions": {
        "member": {"tickets": {"actions": ["read"]}},
        "editor": {"tickets": {"actions": ["read", "write"]}},
        "guest": {},
    },
    "entities": {"tickets": {"table": "tickets", "primary_key": "id", "columns": ["id", "title"]}},
    "capabilities": [
        {"alias": "kb", "kind": "mcp", "url": "https://mcp.example.test/mcp",
         "tools": ["search_docs"], "credential": "app", "risk": "read", "pin": None},
        {"alias": "writer", "kind": "mcp", "url": "https://mcp.example.test/mcp",
         "tools": ["update_doc"], "credential": "app", "risk": "write", "pin": None},
        {"alias": "admin", "kind": "mcp", "url": "https://mcp.example.test/mcp",
         "tools": ["delete_all"], "credential": "app", "risk": "destructive", "pin": None},
        {"alias": "connected", "kind": "mcp", "url": "https://mcp.example.test/mcp",
         "tools": ["private_search"], "credential": "end_user", "risk": "read", "pin": None},
    ],
    "plans": {"free": {"features": ["chat"], "limits": {"ai_credits_month": 100}}},
}
SETTINGS = {
    "limits": {"app_credits_month": 1000},
    "capability_secrets": {"kb": "kb_token", "writer": "writer_token"},
    "database_secret": "db_dsn",
}


def _runtime(app_env_id: str = "app1") -> AppRuntime:
    return AppRuntime(app_env_id=app_env_id, version=1, digest="d", config=CONFIG, settings=SETTINGS)


def _user(role: str = "member", plan: str = "free", org: str | None = None) -> EndUser:
    return EndUser(id="u1", org=org, role=role, plan=plan, env="dev")


def _ctx(**overrides) -> tools.GateContext:
    base = dict(
        runtime=_runtime(), user=_user(), run_as="end_user", allowed_tools=None,
        ledger=_FakeLedger(), app_env_id="app1", requires_feature=None,
    )
    base.update(overrides)
    return tools.GateContext(**base)


class ParseToolRefTests(unittest.TestCase):
    def test_entity_ref(self) -> None:
        ref = tools.parse_tool_ref("entity:tickets.read")
        self.assertEqual(ref.kind, "entity")
        self.assertEqual(ref.entity, "tickets")
        self.assertEqual(ref.action, "read")

    def test_capability_ref(self) -> None:
        ref = tools.parse_tool_ref("kb.search_docs")
        self.assertEqual(ref.kind, "capability")
        self.assertEqual(ref.capability_alias, "kb")
        self.assertEqual(ref.tool_name, "search_docs")

    def test_malformed_refs_rejected(self) -> None:
        for bad in ("entity:tickets", "entity:.read", "kb", ".search_docs"):
            with self.assertRaises(tools.InvalidToolRef):
                tools.parse_tool_ref(bad)


class WrapUntrustedTests(unittest.TestCase):
    def test_wraps_string_and_json(self) -> None:
        wrapped = tools.wrap_untrusted("hello")
        self.assertIn("hello", wrapped)
        self.assertIn("<untrusted_tool_result>", wrapped)
        wrapped_obj = tools.wrap_untrusted({"a": 1})
        self.assertIn('"a": 1', wrapped_obj)


class _NoDbServices:
    """A Services stand-in for gate tests that never reach the DB (non-destructive)."""

    db = None
    config = None
    keys = None
    clock = staticmethod(lambda: 1_800_000_000.0)


class GatePermissionTests(unittest.TestCase):
    def test_entity_read_allowed_for_member(self) -> None:
        ctx = _ctx()
        risk, capability = tools.evaluate(ctx, _NoDbServices(), tools.parse_tool_ref("entity:tickets.read"), {})
        self.assertEqual(risk, "read")
        self.assertIsNone(capability)

    def test_entity_read_denied_for_guest(self) -> None:
        ctx = _ctx(user=_user(role="guest"))
        with self.assertRaises(tools.GateDenied) as ctxmgr:
            tools.evaluate(ctx, _NoDbServices(), tools.parse_tool_ref("entity:tickets.read"), {})
        self.assertEqual(ctxmgr.exception.reason, "permission_denied")

    def test_unknown_entity_denied(self) -> None:
        ctx = _ctx()
        with self.assertRaises(tools.GateDenied) as ctxmgr:
            tools.evaluate(ctx, _NoDbServices(), tools.parse_tool_ref("entity:ghost.read"), {})
        self.assertEqual(ctxmgr.exception.reason, "permission_denied")

    def test_run_as_app_bypasses_role_check_for_entities(self) -> None:
        ctx = _ctx(user=None, run_as="app")
        risk, _ = tools.evaluate(ctx, _NoDbServices(), tools.parse_tool_ref("entity:tickets.read"), {})
        self.assertEqual(risk, "read")

    def test_capability_tool_not_declared_denied(self) -> None:
        ctx = _ctx()
        with self.assertRaises(tools.GateDenied) as ctxmgr:
            tools.evaluate(ctx, _NoDbServices(), tools.parse_tool_ref("kb.not_a_real_tool"), {})
        self.assertEqual(ctxmgr.exception.reason, "permission_denied")

    def test_unknown_capability_alias_denied(self) -> None:
        ctx = _ctx()
        with self.assertRaises(tools.GateDenied) as ctxmgr:
            tools.evaluate(ctx, _NoDbServices(), tools.parse_tool_ref("ghost.search_docs"), {})
        self.assertEqual(ctxmgr.exception.reason, "permission_denied")

    def test_agent_scoped_allowlist_excludes_undeclared_capability(self) -> None:
        ctx = _ctx(allowed_tools=frozenset({"entity:tickets.read"}))
        with self.assertRaises(tools.GateDenied) as ctxmgr:
            tools.evaluate(ctx, _NoDbServices(), tools.parse_tool_ref("kb.search_docs"), {})
        self.assertEqual(ctxmgr.exception.reason, "permission_denied")

    def test_agent_scoped_allowlist_permits_declared_capability(self) -> None:
        ctx = _ctx(allowed_tools=frozenset({"kb.search_docs"}))
        risk, capability = tools.evaluate(ctx, _NoDbServices(), tools.parse_tool_ref("kb.search_docs"), {})
        self.assertEqual(risk, "read")
        self.assertEqual(capability["alias"], "kb")


class GateEntitlementTests(unittest.TestCase):
    def test_missing_feature_denied(self) -> None:
        ctx = _ctx(requires_feature="premium")
        with self.assertRaises(tools.GateDenied) as ctxmgr:
            tools.evaluate(ctx, _NoDbServices(), tools.parse_tool_ref("entity:tickets.read"), {})
        self.assertEqual(ctxmgr.exception.reason, "entitlement_required")

    def test_present_feature_allowed(self) -> None:
        ctx = _ctx(requires_feature="chat")
        risk, _ = tools.evaluate(ctx, _NoDbServices(), tools.parse_tool_ref("entity:tickets.read"), {})
        self.assertEqual(risk, "read")

    def test_run_as_app_with_requires_feature_denied(self) -> None:
        ctx = _ctx(user=None, run_as="app", requires_feature="premium")
        with self.assertRaises(tools.GateDenied) as ctxmgr:
            tools.evaluate(ctx, _NoDbServices(), tools.parse_tool_ref("entity:tickets.read"), {})
        self.assertEqual(ctxmgr.exception.reason, "entitlement_required")


class GateRiskTests(unittest.TestCase):
    def test_run_as_app_may_not_use_a_write_capability(self) -> None:
        ctx = _ctx(user=None, run_as="app", allowed_tools=frozenset({"writer.update_doc"}))
        with self.assertRaises(tools.GateDenied) as ctxmgr:
            tools.evaluate(ctx, _NoDbServices(), tools.parse_tool_ref("writer.update_doc"), {})
        self.assertEqual(ctxmgr.exception.reason, "risk_denied")

    def test_end_user_may_use_a_write_capability(self) -> None:
        ctx = _ctx(allowed_tools=frozenset({"writer.update_doc"}))
        risk, _ = tools.evaluate(ctx, _NoDbServices(), tools.parse_tool_ref("writer.update_doc"), {})
        self.assertEqual(risk, "write")

    def test_entity_write_action_refused_this_slice(self) -> None:
        ctx = _ctx(user=_user(role="editor"))  # editor has "write" in permissions, risk still refuses
        with self.assertRaises(tools.GateDenied) as ctxmgr:
            tools.evaluate(ctx, _NoDbServices(), tools.parse_tool_ref("entity:tickets.write"), {})
        self.assertEqual(ctxmgr.exception.reason, "risk_denied")


class GateCreditsTests(unittest.TestCase):
    def test_app_limit_exhausted_denies(self) -> None:
        ctx = _ctx(ledger=_FakeLedger(usage={("app1", "app", "app1", "2027-01"): 1000}))
        with self.assertRaises(tools.GateDenied) as ctxmgr:
            tools.evaluate(ctx, _NoDbServices(), tools.parse_tool_ref("entity:tickets.read"), {})
        self.assertEqual(ctxmgr.exception.reason, "budget_exhausted")

    def test_user_limit_exhausted_denies(self) -> None:
        ctx = _ctx(ledger=_FakeLedger(usage={("app1", "user", "u1", "2027-01"): 100}))
        with self.assertRaises(tools.GateDenied) as ctxmgr:
            tools.evaluate(ctx, _NoDbServices(), tools.parse_tool_ref("entity:tickets.read"), {})
        self.assertEqual(ctxmgr.exception.reason, "budget_exhausted")

    def test_org_subject_used_when_user_has_org(self) -> None:
        ctx = _ctx(user=_user(org="o1"), ledger=_FakeLedger(usage={("app1", "org", "o1", "2027-01"): 100}))
        with self.assertRaises(tools.GateDenied) as ctxmgr:
            tools.evaluate(ctx, _NoDbServices(), tools.parse_tool_ref("entity:tickets.read"), {})
        self.assertEqual(ctxmgr.exception.reason, "budget_exhausted")

    def test_under_limit_allowed(self) -> None:
        ctx = _ctx(ledger=_FakeLedger(usage={("app1", "user", "u1", "2027-01"): 5}))
        risk, _ = tools.evaluate(ctx, _NoDbServices(), tools.parse_tool_ref("entity:tickets.read"), {})
        self.assertEqual(risk, "read")


class GateOrderTests(unittest.TestCase):
    """Proves the five checks run in the contract's order, not just that each works."""

    def test_permission_beats_entitlement(self) -> None:
        ctx = _ctx(user=_user(role="guest"), requires_feature="premium")
        with self.assertRaises(tools.GateDenied) as ctxmgr:
            tools.evaluate(ctx, _NoDbServices(), tools.parse_tool_ref("entity:tickets.read"), {})
        self.assertEqual(ctxmgr.exception.reason, "permission_denied")

    def test_entitlement_beats_risk(self) -> None:
        ctx = _ctx(
            user=None, run_as="app", requires_feature="premium",
            allowed_tools=frozenset({"writer.update_doc"}),
        )
        with self.assertRaises(tools.GateDenied) as ctxmgr:
            tools.evaluate(ctx, _NoDbServices(), tools.parse_tool_ref("writer.update_doc"), {})
        self.assertEqual(ctxmgr.exception.reason, "entitlement_required")

    def test_risk_beats_credits(self) -> None:
        ctx = _ctx(
            user=None, run_as="app", allowed_tools=frozenset({"writer.update_doc"}),
            ledger=_FakeLedger(usage={("app1", "app", "app1", "2027-01"): 1000}),
        )
        with self.assertRaises(tools.GateDenied) as ctxmgr:
            tools.evaluate(ctx, _NoDbServices(), tools.parse_tool_ref("writer.update_doc"), {})
        self.assertEqual(ctxmgr.exception.reason, "risk_denied")

    def test_credits_beats_approval(self) -> None:
        ctx = _ctx(
            allowed_tools=frozenset({"admin.delete_all"}),
            ledger=_FakeLedger(usage={("app1", "app", "app1", "2027-01"): 1000}),
        )
        with self.assertRaises(tools.GateDenied) as ctxmgr:
            tools.evaluate(ctx, _NoDbServices(), tools.parse_tool_ref("admin.delete_all"), {})
        self.assertEqual(ctxmgr.exception.reason, "budget_exhausted")


class _FakeMCPHandler(BaseHTTPRequestHandler):
    tools_list: list = [{"name": "search_docs", "description": "search", "inputSchema": {"type": "object"}}]

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length)) if length else {}
        method = body.get("method")
        if method == "notifications/initialized":
            self.send_response(202)
            self.end_headers()
            return
        if method == "initialize":
            response = {"jsonrpc": "2.0", "id": body["id"], "result": {}}
        elif method == "tools/list":
            response = {"jsonrpc": "2.0", "id": body["id"], "result": {"tools": type(self).tools_list}}
        elif method == "tools/call":
            response = {"jsonrpc": "2.0", "id": body["id"],
                        "result": {"isError": False, "content": [{"type": "text", "text": "found it"}]}}
        else:
            response = {"jsonrpc": "2.0", "id": body.get("id"), "error": {"message": "unknown"}}
        payload = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:
        pass


@unittest.skipUnless(_PG_DSN, "HULL_TEST_PG_DSN is not set; see tests/test_broker_ledger.py")
class ApprovalAndHttpIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        with psycopg.connect(_PG_DSN, autocommit=True) as connection:
            connection.execute("DROP SCHEMA public CASCADE")
            connection.execute("CREATE SCHEMA public")
            connection.execute("CREATE TABLE tickets (id serial PRIMARY KEY, title text NOT NULL)")
            connection.execute("INSERT INTO tickets (title) VALUES ('first'), ('second')")
        self.db = BrokerDatabase(lambda: psycopg.connect(_PG_DSN))
        migrate(self.db)
        self.services = Services(
            db=self.db,
            config=_FakeConfigStore({("app1", "db_dsn"): _PG_DSN, ("app1", "kb_token"): "secret-kb-token"}),
            keys=None,
            clock=lambda: 1_800_000_000.0,
        )

    def _request(self, *, alias: str, tool: str, arguments: dict | None = None, user: EndUser | None = None) -> Request:
        return Request(
            method="POST", path=f"/_hull/v1/tools/{alias}/call", params={"alias": alias}, query={},
            headers={}, body={"tool": tool, "arguments": arguments or {}}, host="app.test", client_ip="127.0.0.1",
            cookies={}, app=AppCaller(app_env_id="app1", site="s"), user=user if user is not None else _user(),
            runtime=_runtime(),
        )

    def test_destructive_capability_records_an_approval_row(self) -> None:
        request = self._request(alias="admin", tool="delete_all", arguments={"x": 1})
        with self.assertRaises(HttpError) as ctxmgr:
            tools.handle_call(request, self.services)
        self.assertEqual(ctxmgr.exception.status, 403)
        self.assertEqual(ctxmgr.exception.code, "tool_denied")
        approval_id = ctxmgr.exception.extra["approvalId"]
        with self.db.transaction() as cursor:
            cursor.execute(
                "SELECT app_env_id, user_id, tool_ref, arguments_json, status FROM broker_tool_approvals WHERE id = %s",
                (approval_id,),
            )
            row = cursor.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "app1")
        self.assertEqual(row[1], "u1")
        self.assertEqual(row[2], "admin.delete_all")
        self.assertEqual(json.loads(row[3]), {"x": 1})
        self.assertEqual(row[4], "pending")

    def test_entity_read_end_to_end_through_handle_call(self) -> None:
        request = self._request(alias="entity:tickets", tool="read")
        response = tools.handle_call(request, self.services)
        self.assertEqual(response.status, 200)
        self.assertFalse(response.body["isError"])
        self.assertEqual(len(response.body["content"]), 2)

    def test_capability_call_end_to_end_with_bearer_token(self) -> None:
        _FakeMCPHandler.tools_list = [
            {"name": "search_docs", "description": "search", "inputSchema": {"type": "object"}}
        ]
        server = HTTPServer(("127.0.0.1", 0), _FakeMCPHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(thread.join, timeout=5)
        config = dict(CONFIG)
        config["capabilities"] = [
            {"alias": "kb", "kind": "mcp", "url": f"http://127.0.0.1:{server.server_port}/mcp",
             "tools": ["search_docs"], "credential": "app", "risk": "read", "pin": None},
        ]
        runtime = AppRuntime(app_env_id="app1", version=1, digest="d", config=config, settings=SETTINGS)
        request = self._request(alias="kb", tool="search_docs", arguments={"q": "hi"})
        request.runtime = runtime
        with mcp_client._allow_loopback_for_tests():
            response = tools.handle_call(request, self.services)
        self.assertEqual(response.status, 200)
        self.assertFalse(response.body["isError"])
        self.assertEqual(response.body["content"], [{"type": "text", "text": "found it"}])

    def test_pin_mismatch_disables_the_tool(self) -> None:
        _FakeMCPHandler.tools_list = [
            {"name": "search_docs", "description": "search", "inputSchema": {"type": "object"}}
        ]
        server = HTTPServer(("127.0.0.1", 0), _FakeMCPHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(thread.join, timeout=5)
        config = dict(CONFIG)
        config["capabilities"] = [
            {"alias": "kb", "kind": "mcp", "url": f"http://127.0.0.1:{server.server_port}/mcp",
             "tools": ["search_docs"], "credential": "app", "risk": "read", "pin": "0" * 64},
        ]
        runtime = AppRuntime(app_env_id="app1", version=1, digest="d", config=config, settings=SETTINGS)
        request = self._request(alias="kb", tool="search_docs")
        request.runtime = runtime
        with mcp_client._allow_loopback_for_tests():
            response = tools.handle_call(request, self.services)
        self.assertTrue(response.body["isError"])
        self.assertIn("pin mismatch", response.body["content"])

    def test_pin_match_allows_the_tool(self) -> None:
        _FakeMCPHandler.tools_list = [
            {"name": "search_docs", "description": "search", "inputSchema": {"type": "object"}}
        ]
        correct_pin = mcp_client.canonical_tool_digest(_FakeMCPHandler.tools_list[0])
        server = HTTPServer(("127.0.0.1", 0), _FakeMCPHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(thread.join, timeout=5)
        config = dict(CONFIG)
        config["capabilities"] = [
            {"alias": "kb", "kind": "mcp", "url": f"http://127.0.0.1:{server.server_port}/mcp",
             "tools": ["search_docs"], "credential": "app", "risk": "read", "pin": correct_pin},
        ]
        runtime = AppRuntime(app_env_id="app1", version=1, digest="d", config=config, settings=SETTINGS)
        request = self._request(alias="kb", tool="search_docs")
        request.runtime = runtime
        with mcp_client._allow_loopback_for_tests():
            response = tools.handle_call(request, self.services)
        self.assertFalse(response.body["isError"])

    def test_end_user_credential_capability_is_not_connected(self) -> None:
        request = self._request(alias="connected", tool="private_search")
        response = tools.handle_call(request, self.services)
        self.assertTrue(response.body["isError"])
        self.assertIn("end-user connection", response.body["content"])

    def test_usage_me_reports_period_used_and_limit(self) -> None:
        ledger = Ledger(self.db, self.services.clock)
        reservation = ledger.reserve(
            app_env_id="app1", scope="flow:x", amount=10, user_subject=("user", "u1"), user_limit=100,
        )
        ledger.settle(reservation.id, app_env_id="app1", actual_amount=7)
        request = self._request(alias="entity:tickets", tool="read")
        response = tools.usage_me(request, self.services)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["used"], 7)
        self.assertEqual(response.body["limit"], 100)
        self.assertEqual(response.body["period"], ledger.period_for())


if __name__ == "__main__":
    unittest.main()
