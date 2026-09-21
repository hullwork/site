"""agent_bridge.py tests against a fake agent runtime HTTP server.

Needs real PostgreSQL (ledger + thread ownership + entity reads) — see
tests/test_broker_ledger.py for the HULL_TEST_PG_DSN convention. The fake server
below speaks just enough of contract §5's wire shape: POST /api/ag-ui streams a
scripted sequence of "data: {json}\\n\\n" frames, and it records every
POST .../tool-results and POST .../cancel it receives so tests can assert on them.
"""
from __future__ import annotations

import json
import os
import threading
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg

from sites.broker import agent_bridge
from sites.broker.config_store import AppRuntime, ConfigStore
from sites.broker.db import BrokerDatabase, migrate
from sites.broker.http import HttpError, Request, Services
from sites.broker.identity import AppCaller, EndUser

_PG_DSN = os.environ.get("HULL_TEST_PG_DSN", "")
_UNSET = object()


class _FakeConfigStore(ConfigStore):
    def __init__(self, secrets: dict[tuple[str, str], str] | None = None) -> None:
        self._secrets = secrets or {}

    def active(self, app_env_id: str) -> AppRuntime | None:
        return None

    def by_host(self, host: str) -> str | None:
        return None

    def secret(self, app_env_id: str, name: str) -> str | None:
        return self._secrets.get((app_env_id, name))


class _FakeAgentRuntimeHandler(BaseHTTPRequestHandler):
    script: list[dict] = []
    tool_results: list[dict] = []
    cancels: list[dict] = []
    last_ag_ui_body: dict | None = None
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        body = json.loads(raw) if raw else {}
        if self.path == "/api/ag-ui":
            type(self).last_ag_ui_body = body
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.close_connection = True
            self.end_headers()
            for event in type(self).script:
                self.wfile.write(f"data: {json.dumps(event)}\n\n".encode("utf-8"))
                self.wfile.flush()
            return
        if self.path.endswith("/tool-results"):
            type(self).tool_results.append(body)
            self.send_response(202)
            self.close_connection = True
            self.end_headers()
            return
        if self.path.endswith("/cancel"):
            type(self).cancels.append({"path": self.path, "body": body})
            self.send_response(202)
            self.close_connection = True
            self.end_headers()
            return
        self.send_response(404)
        self.close_connection = True
        self.end_headers()

    def log_message(self, *args) -> None:
        pass


def _user(role: str = "member", plan: str = "free") -> EndUser:
    return EndUser(id="u1", org=None, role=role, plan=plan, env="dev")


AGENT_CONFIG = {
    "instructions": "You help with tickets.", "scope": "tickets only",
    "guardrails": ["never invent ticket ids"], "model_tier": "fast", "skills": [],
    "tools": ["entity:tickets.read", "admin.delete_all"], "run_as": "end_user",
    "requires_feature": None, "budget": {"max_credits_per_run": 50},
}
CONFIG = {
    "agents": {"triage": AGENT_CONFIG},
    "entities": {"tickets": {"table": "tickets", "primary_key": "id", "columns": ["id", "title"]}},
    "capabilities": [
        {"alias": "admin", "kind": "mcp", "url": "https://mcp.example.test/mcp",
         "tools": ["delete_all"], "credential": "app", "risk": "destructive", "pin": None},
    ],
    "permissions": {"member": {"tickets": {"actions": ["read"]}}},
    "plans": {"free": {"features": ["chat"], "limits": {"ai_credits_month": 1000}}},
}
SETTINGS = {
    "ai_enabled": True,
    "model_tiers": {"fast": {"model": "glm-5.1", "input_credits_per_1k": 1000, "output_credits_per_1k": 1000}},
    "limits": {"app_credits_month": None},
    "database_secret": "db_dsn",
}


@unittest.skipUnless(_PG_DSN, "HULL_TEST_PG_DSN is not set; see tests/test_broker_ledger.py")
class AgentBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        with psycopg.connect(_PG_DSN, autocommit=True) as connection:
            connection.execute("DROP SCHEMA public CASCADE")
            connection.execute("CREATE SCHEMA public")
            connection.execute("CREATE TABLE tickets (id serial PRIMARY KEY, title text NOT NULL)")
            connection.execute("INSERT INTO tickets (title) VALUES ('only ticket')")
        self.db = BrokerDatabase(lambda: psycopg.connect(_PG_DSN))
        migrate(self.db)
        self.services = Services(
            db=self.db, config=_FakeConfigStore({("app1", "db_dsn"): _PG_DSN}), keys=None,
            clock=lambda: 1_800_000_000.0,
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeAgentRuntimeHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.thread.join, timeout=5)
        os.environ["HULL_AGENT_RUNTIME_URL"] = f"http://127.0.0.1:{self.server.server_port}"
        _FakeAgentRuntimeHandler.script = []
        _FakeAgentRuntimeHandler.tool_results = []
        _FakeAgentRuntimeHandler.cancels = []
        _FakeAgentRuntimeHandler.last_ag_ui_body = None
        self.addCleanup(os.environ.pop, "HULL_AGENT_RUNTIME_URL", None)

    def _request(self, *, name: str = "triage", input_text: str = "hi", thread_id: str | None = None,
                 user=_UNSET, runtime: AppRuntime | None = None) -> Request:
        body: dict = {"input": input_text}
        if thread_id:
            body["threadId"] = thread_id
        return Request(
            method="POST", path=f"/_hull/v1/agents/{name}/runs", params={"name": name}, query={},
            headers={}, body=body, host="app.test", client_ip="127.0.0.1", cookies={},
            app=AppCaller(app_env_id="app1", site="s"),
            user=_user() if user is _UNSET else user,
            runtime=runtime or AppRuntime(app_env_id="app1", version=1, digest="d", config=CONFIG, settings=SETTINGS),
        )

    def test_unknown_agent(self) -> None:
        request = self._request(name="ghost")
        with self.assertRaises(HttpError) as ctxmgr:
            agent_bridge.handle_run(request, self.services)
        self.assertEqual(ctxmgr.exception.status, 404)

    def test_missing_input(self) -> None:
        request = self._request(input_text="")
        with self.assertRaises(HttpError) as ctxmgr:
            agent_bridge.handle_run(request, self.services)
        self.assertEqual(ctxmgr.exception.code, "invalid_input")

    def test_user_required_for_end_user_run_as(self) -> None:
        request = self._request(user=None)
        with self.assertRaises(HttpError) as ctxmgr:
            agent_bridge.handle_run(request, self.services)
        self.assertEqual(ctxmgr.exception.status, 401)

    def test_budget_exhausted_before_streaming_starts(self) -> None:
        config = {**CONFIG, "plans": {"free": {"features": ["chat"], "limits": {"ai_credits_month": 1}}}}
        runtime = AppRuntime(app_env_id="app1", version=1, digest="d", config=config, settings=SETTINGS)
        request = self._request(runtime=runtime)
        with self.assertRaises(HttpError) as ctxmgr:
            agent_bridge.handle_run(request, self.services)
        self.assertEqual(ctxmgr.exception.status, 402)
        self.assertEqual(ctxmgr.exception.code, "budget_exhausted")

    def test_successful_run_streams_and_settles(self) -> None:
        _FakeAgentRuntimeHandler.script = [
            {"type": "TEXT_MESSAGE_CONTENT", "delta": "Hi there"},
            {"type": "CUSTOM", "name": "agent/event",
             "value": {"type": "usage", "input": 1, "output": 1, "cached": 0, "model": "glm-5.1"}},
            {"type": "RUN_FINISHED"},
        ]
        request = self._request()
        response = agent_bridge.handle_run(request, self.services)
        events = list(response.events)
        names = [name for name, _ in events]
        self.assertEqual(names, ["run.started", "message.delta", "run.finished"])
        self.assertEqual(events[1][1], {"text": "Hi there"})
        # 1 input + 1 output token @ 1000 credits/1k = 2 credits.
        self.assertEqual(events[2][1], {"usage": {"credits": 2}})
        self.assertEqual(_FakeAgentRuntimeHandler.last_ag_ui_body["forwardedProps"]["agent"], "runtime")
        self.assertIn("You help with tickets.", _FakeAgentRuntimeHandler.last_ag_ui_body["forwardedProps"]["systemPrompt"])

    def test_entity_tool_call_executes_and_posts_wrapped_result(self) -> None:
        _FakeAgentRuntimeHandler.script = [
            {"type": "TOOL_CALL_START", "toolCallId": "c1", "toolCallName": "entity__tickets__read"},
            {"type": "CUSTOM", "name": "hull/caller_tool_call",
             "value": {"requestId": "req1", "toolCallId": "c1", "name": "entity__tickets__read", "arguments": {}}},
            {"type": "RUN_FINISHED"},
        ]
        request = self._request()
        response = agent_bridge.handle_run(request, self.services)
        list(response.events)  # drain
        self.assertEqual(len(_FakeAgentRuntimeHandler.tool_results), 1)
        result = _FakeAgentRuntimeHandler.tool_results[0]
        self.assertEqual(result["requestId"], "req1")
        self.assertFalse(result["isError"])
        self.assertIn("only ticket", result["content"])
        self.assertIn("<untrusted_tool_result>", result["content"])

    def test_a_failing_tool_is_reported_to_the_runtime_instead_of_left_waiting(self) -> None:
        _FakeAgentRuntimeHandler.script = [
            {"type": "TOOL_CALL_START", "toolCallId": "c1", "toolCallName": "entity__tickets__read"},
            {"type": "CUSTOM", "name": "hull/caller_tool_call",
             "value": {"requestId": "req1", "toolCallId": "c1", "name": "entity__tickets__read", "arguments": {}}},
            {"type": "RUN_FINISHED"},
        ]
        with mock.patch.object(agent_bridge.tools_mod, "execute", side_effect=RuntimeError("boom")):
            events = list(agent_bridge.handle_run(self._request(), self.services).events)
        self.assertEqual(len(_FakeAgentRuntimeHandler.tool_results), 1)
        self.assertTrue(_FakeAgentRuntimeHandler.tool_results[0]["isError"])
        self.assertNotIn("boom", _FakeAgentRuntimeHandler.tool_results[0]["content"])
        self.assertEqual(events[-1][0], "run.finished")

    def test_an_unexpected_failure_mid_stream_ends_with_run_error_and_cancels(self) -> None:
        _FakeAgentRuntimeHandler.script = [
            {"type": "CUSTOM", "name": "hull/caller_tool_call",
             "value": {"requestId": "req1", "toolCallId": "c1", "name": "entity__tickets__read", "arguments": {}}},
            {"type": "RUN_FINISHED"},
        ]
        with mock.patch.object(agent_bridge, "_handle_caller_tool_call", side_effect=RuntimeError("boom")):
            events = list(agent_bridge.handle_run(self._request(), self.services).events)
        self.assertEqual(events[-1], ("run.error", {"code": "internal_error", "message": "the agent run failed."}))
        self.assertEqual(len(_FakeAgentRuntimeHandler.cancels), 1)

    def test_destructive_tool_call_yields_approval_pending(self) -> None:
        _FakeAgentRuntimeHandler.script = [
            {"type": "CUSTOM", "name": "hull/caller_tool_call",
             "value": {"requestId": "req2", "toolCallId": "c2", "name": "admin__delete_all", "arguments": {}}},
            {"type": "RUN_FINISHED"},
        ]
        request = self._request()
        response = agent_bridge.handle_run(request, self.services)
        events = list(response.events)
        pending = [data for name, data in events if name == "approval.pending"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["tool"], "admin__delete_all")
        self.assertTrue(_FakeAgentRuntimeHandler.tool_results[0]["isError"])

    def test_upstream_run_error_mapped(self) -> None:
        _FakeAgentRuntimeHandler.script = [{"type": "RUN_ERROR", "code": "boom", "message": "it broke"}]
        request = self._request()
        response = agent_bridge.handle_run(request, self.services)
        events = list(response.events)
        self.assertEqual(events[-1], ("run.error", {"code": "boom", "message": "it broke"}))

    def test_budget_exceeded_mid_run_cancels_upstream(self) -> None:
        config = {**CONFIG, "agents": {"triage": {**AGENT_CONFIG, "budget": {"max_credits_per_run": 1}}}}
        runtime = AppRuntime(app_env_id="app1", version=1, digest="d", config=config, settings=SETTINGS)
        _FakeAgentRuntimeHandler.script = [
            {"type": "CUSTOM", "name": "agent/event",
             "value": {"type": "usage", "input": 100, "output": 100, "cached": 0, "model": "glm-5.1"}},
            {"type": "TEXT_MESSAGE_CONTENT", "delta": "should not be reached"},
            {"type": "RUN_FINISHED"},
        ]
        request = self._request(runtime=runtime)
        response = agent_bridge.handle_run(request, self.services)
        events = list(response.events)
        names = [name for name, _ in events]
        self.assertNotIn("Should not be reached", [d.get("text") for n, d in events if n == "message.delta"])
        self.assertEqual(names[-1], "run.error")
        self.assertEqual(events[-1][1]["code"], "budget_exhausted")
        self.assertEqual(len(_FakeAgentRuntimeHandler.cancels), 1)

    def test_thread_ownership_enforced(self) -> None:
        _FakeAgentRuntimeHandler.script = [{"type": "RUN_FINISHED"}]
        first = self._request()
        response = agent_bridge.handle_run(first, self.services)
        list(response.events)
        thread_id = None
        with self.db.transaction() as cursor:
            cursor.execute("SELECT thread_id FROM broker_agent_threads WHERE app_env_id = 'app1'")
            thread_id = cursor.fetchone()[0]

        _FakeAgentRuntimeHandler.script = [{"type": "RUN_FINISHED"}]
        second = self._request(thread_id=thread_id)
        response2 = agent_bridge.handle_run(second, self.services)
        list(response2.events)  # same user, same thread: allowed

        other_user = EndUser(id="u2", org=None, role="member", plan="free", env="dev")
        third = self._request(thread_id=thread_id, user=other_user)
        with self.assertRaises(HttpError) as ctxmgr:
            agent_bridge.handle_run(third, self.services)
        self.assertEqual(ctxmgr.exception.status, 403)

    def test_cancel_mid_stream(self) -> None:
        _FakeAgentRuntimeHandler.script = [
            {"type": "TEXT_MESSAGE_CONTENT", "delta": "first"},
            {"type": "TEXT_MESSAGE_CONTENT", "delta": "second"},
            {"type": "RUN_FINISHED"},
        ]
        request = self._request()
        response = agent_bridge.handle_run(request, self.services)
        events_iter = iter(response.events)
        first_event = next(events_iter)
        self.assertEqual(first_event[0], "run.started")
        run_id = first_event[1]["runId"]

        cancel_request = Request(
            method="POST", path=f"/_hull/v1/agents/triage/runs/{run_id}/cancel",
            params={"name": "triage", "runId": run_id}, query={}, headers={}, body=None,
            host="app.test", client_ip="127.0.0.1", cookies={},
            app=AppCaller(app_env_id="app1", site="s"), user=_user(), runtime=None,
        )
        cancel_response = agent_bridge.handle_cancel(cancel_request, self.services)
        self.assertEqual(cancel_response.status, 202)

        remaining = list(events_iter)
        names = [name for name, _ in remaining]
        self.assertIn("run.error", names)
        self.assertEqual(dict(remaining)["run.error"]["code"], "cancelled")
        self.assertEqual(len(_FakeAgentRuntimeHandler.cancels), 1)

    def test_cancel_by_another_user_or_app_is_ignored(self) -> None:
        _FakeAgentRuntimeHandler.script = [
            {"type": "TEXT_MESSAGE_CONTENT", "delta": "first"},
            {"type": "RUN_FINISHED"},
        ]
        events_iter = iter(agent_bridge.handle_run(self._request(), self.services).events)
        run_id = next(events_iter)[1]["runId"]
        other_user = EndUser(id="u" + "f" * 32, org=None, role="member", plan="free", env="dev")
        for app_id, user in (("app1", other_user), ("app2", _user()), ("app1", None)):
            cancel = Request(
                method="POST", path=f"/_hull/v1/agents/triage/runs/{run_id}/cancel",
                params={"name": "triage", "runId": run_id}, query={}, headers={}, body=None,
                host="app.test", client_ip="127.0.0.1", cookies={},
                app=AppCaller(app_env_id=app_id, site="s"), user=user, runtime=None,
            )
            self.assertEqual(agent_bridge.handle_cancel(cancel, self.services).status, 202)
        remaining = [name for name, _ in events_iter]
        self.assertEqual(remaining[-1], "run.finished")
        self.assertEqual(_FakeAgentRuntimeHandler.cancels, [])

    def test_build_system_prompt_includes_guardrails(self) -> None:
        prompt = agent_bridge.build_system_prompt(AGENT_CONFIG)
        self.assertIn("never invent ticket ids", prompt)
        self.assertIn("tickets only", prompt)

    def test_build_tool_list_excludes_destructive_only_by_gate_not_risk(self) -> None:
        from sites.broker import tools as tools_mod
        from sites.broker.ledger import Ledger

        runtime = AppRuntime(app_env_id="app1", version=1, digest="d", config=CONFIG, settings=SETTINGS)
        ctx = tools_mod.GateContext(
            runtime=runtime, user=_user(), run_as="end_user",
            allowed_tools=frozenset(AGENT_CONFIG["tools"]), ledger=Ledger(self.db, self.services.clock),
            app_env_id="app1",
        )
        tool_defs, wire_to_ref = agent_bridge.build_tool_list(ctx, AGENT_CONFIG)
        names = {t["name"] for t in tool_defs}
        # Both are permitted for an end user (destructive only blocks run_as: app);
        # the destructive one surfaces approval.pending only when actually called.
        self.assertEqual(names, {"entity__tickets__read", "admin__delete_all"})
        self.assertEqual(wire_to_ref["entity__tickets__read"], "entity:tickets.read")


if __name__ == "__main__":
    unittest.main()
