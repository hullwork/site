"""Flow-run tests. render_prompt is pure and needs no database; handle_run needs
real PostgreSQL for the ledger (see tests/test_broker_ledger.py for the convention).
"""
from __future__ import annotations

import os
import unittest

import psycopg

from sites.broker import flows
from sites.broker.config_store import AppRuntime
from sites.broker.db import BrokerDatabase, migrate
from sites.broker.http import HttpError, Request, Services
from sites.broker.identity import AppCaller, EndUser
from sites.broker.ledger import Ledger
from sites.broker.llm import FakeLLM, LLMUnavailable, LLMUsage

_PG_DSN = os.environ.get("HULL_TEST_PG_DSN", "")
_UNSET = object()


class RenderPromptTests(unittest.TestCase):
    def test_simple_placeholder(self) -> None:
        self.assertEqual(flows.render_prompt("Hello {{input.name}}!", {"name": "Ada"}), 'Hello "Ada"!')

    def test_number_and_bool_and_null(self) -> None:
        self.assertEqual(flows.render_prompt("{{input.n}}", {"n": 3}), "3")
        self.assertEqual(flows.render_prompt("{{input.b}}", {"b": True}), "true")
        self.assertEqual(flows.render_prompt("{{input.missing}}", {}), "null")

    def test_nested_path(self) -> None:
        self.assertEqual(flows.render_prompt("{{input.a.b}}", {"a": {"b": "x"}}), '"x"')

    def test_multiple_placeholders(self) -> None:
        rendered = flows.render_prompt("{{input.a}} and {{input.b}}", {"a": 1, "b": 2})
        self.assertEqual(rendered, "1 and 2")

    def test_no_code_execution_is_literal_text(self) -> None:
        # A value containing template-like syntax is JSON-encoded, not re-interpreted.
        rendered = flows.render_prompt("{{input.x}}", {"x": "{{input.y}}"})
        self.assertEqual(rendered, '"{{input.y}}"')

    def test_object_value_is_json_encoded(self) -> None:
        self.assertEqual(flows.render_prompt("{{input.o}}", {"o": {"k": 1}}), '{"k": 1}')


def _runtime(config: dict, settings: dict, app_env_id: str = "app1") -> AppRuntime:
    return AppRuntime(app_env_id=app_env_id, version=1, digest="d", config=config, settings=settings)


def _user(role: str = "member", plan: str = "free", org: str | None = None) -> EndUser:
    return EndUser(id="u1", org=org, role=role, plan=plan, env="dev")


SUMMARIZE_FLOW = {
    "mode": "llm", "model_tier": "fast", "prompt": "Summarize: {{input.text}}",
    "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    "output_schema": {}, "fallback": {"summary": ""}, "audience": "server", "requires_feature": None,
    "budget": {"max_credits_per_call": 10},
}
JSON_FLOW = {
    "mode": "llm", "model_tier": "fast", "prompt": "{{input.text}}",
    "input_schema": {}, "output_schema": {"type": "object", "properties": {"summary": {"type": "string"}},
                                           "required": ["summary"]},
    "fallback": {"summary": ""}, "audience": "server", "requires_feature": None,
    "budget": {"max_credits_per_call": 10},
}
CONFIG = {
    "flows": {"summarize": SUMMARIZE_FLOW, "json_flow": JSON_FLOW},
    "plans": {"free": {"features": ["chat"], "limits": {"ai_credits_month": 100}}},
}
SETTINGS = {
    "ai_enabled": True,
    "model_tiers": {"fast": {"model": "glm-5.1", "input_credits_per_1k": 1000, "output_credits_per_1k": 1000}},
    "flows": {},
    "limits": {"app_credits_month": None},
}


@unittest.skipUnless(_PG_DSN, "HULL_TEST_PG_DSN is not set; see tests/test_broker_ledger.py")
class HandleRunTests(unittest.TestCase):
    def setUp(self) -> None:
        with psycopg.connect(_PG_DSN, autocommit=True) as connection:
            connection.execute("DROP SCHEMA public CASCADE")
            connection.execute("CREATE SCHEMA public")
        self.db = BrokerDatabase(lambda: psycopg.connect(_PG_DSN))
        migrate(self.db)
        self.services = Services(db=self.db, config=None, keys=None, clock=lambda: 1_800_000_000.0)

    def _request(self, *, name: str, input_obj, runtime: AppRuntime, headers=None, user=_UNSET) -> Request:
        return Request(
            method="POST", path=f"/_hull/v1/flows/{name}/run", params={"name": name}, query={},
            headers=headers or {}, body={"input": input_obj}, host="app.test", client_ip="127.0.0.1",
            cookies={}, app=AppCaller(app_env_id="app1", site="s"),
            user=_user() if user is _UNSET else user, runtime=runtime,
        )

    def test_unknown_flow(self) -> None:
        runtime = _runtime(CONFIG, SETTINGS)
        request = self._request(name="ghost", input_obj={}, runtime=runtime)
        with self.assertRaises(HttpError) as ctxmgr:
            flows.handle_run(request, self.services, FakeLLM("x"))
        self.assertEqual(ctxmgr.exception.status, 404)
        self.assertEqual(ctxmgr.exception.code, "unknown_flow")

    def test_invalid_input_rejected(self) -> None:
        runtime = _runtime(CONFIG, SETTINGS)
        request = self._request(name="summarize", input_obj={}, runtime=runtime)  # missing required "text"
        with self.assertRaises(HttpError) as ctxmgr:
            flows.handle_run(request, self.services, FakeLLM("x"))
        self.assertEqual(ctxmgr.exception.status, 400)
        self.assertEqual(ctxmgr.exception.code, "invalid_input")

    def test_successful_run_charges_priced_credits(self) -> None:
        runtime = _runtime(CONFIG, SETTINGS)
        llm = FakeLLM("a summary", usage=LLMUsage(input_tokens=1, output_tokens=1))
        request = self._request(name="summarize", input_obj={"text": "hello"}, runtime=runtime)
        response = flows.handle_run(request, self.services, llm)
        self.assertEqual(response.status, 200)
        self.assertFalse(response.body["fallback"])
        self.assertEqual(response.body["output"], "a summary")
        # 1 input + 1 output token at 1000 credits/1k each = 1 + 1 = 2 credits, rounded up.
        self.assertEqual(response.body["usage"]["credits"], 2)
        self.assertEqual(llm.calls[0]["messages"][0]["content"], 'Summarize: "hello"')

    def test_output_schema_parses_and_validates_json(self) -> None:
        runtime = _runtime(CONFIG, SETTINGS)
        llm = FakeLLM('{"summary": "ok"}')
        request = self._request(name="json_flow", input_obj={"text": "hi"}, runtime=runtime)
        response = flows.handle_run(request, self.services, llm)
        self.assertEqual(response.body["output"], {"summary": "ok"})
        self.assertTrue(llm.calls[0]["output_schema"])

    def test_malformed_json_output_falls_back(self) -> None:
        runtime = _runtime(CONFIG, SETTINGS)
        llm = FakeLLM("not json")
        request = self._request(name="json_flow", input_obj={"text": "hi"}, runtime=runtime)
        response = flows.handle_run(request, self.services, llm)
        self.assertTrue(response.body["fallback"])
        self.assertEqual(response.body["reason"], "invalid_output")
        self.assertEqual(response.body["usage"]["credits"], 0)

    def test_output_schema_mismatch_falls_back(self) -> None:
        runtime = _runtime(CONFIG, SETTINGS)
        llm = FakeLLM('{"wrong_field": 1}')
        request = self._request(name="json_flow", input_obj={"text": "hi"}, runtime=runtime)
        response = flows.handle_run(request, self.services, llm)
        self.assertEqual(response.body["reason"], "invalid_output")

    def test_release_on_failure_means_no_charge(self) -> None:
        runtime = _runtime(CONFIG, SETTINGS)
        llm = FakeLLM(raises=LLMUnavailable("down"))
        request = self._request(name="summarize", input_obj={"text": "hi"}, runtime=runtime)
        response = flows.handle_run(request, self.services, llm)
        self.assertEqual(response.body["reason"], "upstream_error")
        ledger = Ledger(self.db, self.services.clock)
        used = ledger.usage("app1", "user", "u1", ledger.period_for())
        self.assertEqual(used, 0)

    def test_ai_disabled_falls_back_before_any_reservation(self) -> None:
        runtime = _runtime(CONFIG, {**SETTINGS, "ai_enabled": False})
        llm = FakeLLM("x")
        request = self._request(name="summarize", input_obj={"text": "hi"}, runtime=runtime)
        response = flows.handle_run(request, self.services, llm)
        self.assertEqual(response.body["reason"], "ai_disabled")
        self.assertEqual(llm.calls, [])

    def test_flow_disabled_via_settings(self) -> None:
        runtime = _runtime(CONFIG, {**SETTINGS, "flows": {"summarize": {"enabled": False}}})
        request = self._request(name="summarize", input_obj={"text": "hi"}, runtime=runtime)
        response = flows.handle_run(request, self.services, FakeLLM("x"))
        self.assertEqual(response.body["reason"], "disabled")

    def test_requires_feature_missing_falls_back_to_entitlement(self) -> None:
        config = {**CONFIG, "flows": {**CONFIG["flows"], "summarize": {**SUMMARIZE_FLOW, "requires_feature": "premium"}}}
        runtime = _runtime(config, SETTINGS)
        request = self._request(name="summarize", input_obj={"text": "hi"}, runtime=runtime)
        response = flows.handle_run(request, self.services, FakeLLM("x"))
        self.assertEqual(response.body["reason"], "entitlement")

    def test_budget_exhausted(self) -> None:
        # The plan limit (1) is below the flow's own reservation ceiling
        # (max_credits_per_call = 10), so the reservation itself is refused before
        # any LLM call happens.
        config = {**CONFIG, "plans": {"free": {"features": ["chat"], "limits": {"ai_credits_month": 1}}}}
        runtime = _runtime(config, SETTINGS)
        request = self._request(name="summarize", input_obj={"text": "hi"}, runtime=runtime)
        llm = FakeLLM("x", usage=LLMUsage(1, 1))
        response = flows.handle_run(request, self.services, llm)
        self.assertEqual(response.body["reason"], "budget_exhausted")
        self.assertEqual(llm.calls, [])

    def test_unknown_model_tier_falls_back_to_upstream_error(self) -> None:
        config = {**CONFIG, "flows": {**CONFIG["flows"], "summarize": {**SUMMARIZE_FLOW, "model_tier": "ghost"}}}
        runtime = _runtime(config, SETTINGS)
        request = self._request(name="summarize", input_obj={"text": "hi"}, runtime=runtime)
        response = flows.handle_run(request, self.services, FakeLLM("x"))
        self.assertEqual(response.body["reason"], "upstream_error")

    def test_idempotent_replay_no_double_charge_and_no_second_llm_call(self) -> None:
        runtime = _runtime(CONFIG, SETTINGS)
        llm = FakeLLM("a summary", usage=LLMUsage(input_tokens=1, output_tokens=1))
        headers = {"Idempotency-Key": "abc123"}
        request1 = self._request(name="summarize", input_obj={"text": "hi"}, runtime=runtime, headers=headers)
        response1 = flows.handle_run(request1, self.services, llm)
        request2 = self._request(name="summarize", input_obj={"text": "hi"}, runtime=runtime, headers=headers)
        response2 = flows.handle_run(request2, self.services, llm)
        self.assertEqual(response1.body, response2.body)
        self.assertEqual(len(llm.calls), 1)
        ledger = Ledger(self.db, self.services.clock)
        self.assertEqual(ledger.usage("app1", "user", "u1", ledger.period_for()), 2)

    def test_audience_public_allows_no_user(self) -> None:
        config = {**CONFIG, "flows": {**CONFIG["flows"], "summarize": {**SUMMARIZE_FLOW, "audience": "public"}}}
        runtime = _runtime(config, {**SETTINGS, "limits": {"app_credits_month": 100}})
        request = self._request(name="summarize", input_obj={"text": "hi"}, runtime=runtime, user=None)
        response = flows.handle_run(request, self.services, FakeLLM("x", usage=LLMUsage(1, 1)))
        self.assertFalse(response.body["fallback"])

    def test_server_audience_requires_a_user(self) -> None:
        runtime = _runtime(CONFIG, SETTINGS)
        request = self._request(name="summarize", input_obj={"text": "hi"}, runtime=runtime, user=None)
        with self.assertRaises(HttpError) as ctxmgr:
            flows.handle_run(request, self.services, FakeLLM("x"))
        self.assertEqual(ctxmgr.exception.status, 401)


if __name__ == "__main__":
    unittest.main()
