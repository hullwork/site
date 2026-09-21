"""runtime_api.py wiring tests: route registration, routing round trips, admin usage,
and the lazy-LLM-client degrade path. Needs real PostgreSQL for anything that
touches the ledger (see tests/test_broker_ledger.py for the convention).
"""
from __future__ import annotations

import datetime as dt
import os
import unittest

import psycopg

from sites.broker import runtime_api
from sites.broker.config_store import AppRuntime, ConfigStore
from sites.broker.db import BrokerDatabase, migrate
from sites.broker.http import Response, Router, Services, StreamResponse
from sites.broker.identity import AppCaller, EndUser
from sites.broker.ledger import Ledger

_PG_DSN = os.environ.get("HULL_TEST_PG_DSN", "")


class _FakeConfigStore(ConfigStore):
    def active(self, app_env_id: str):
        return None

    def by_host(self, host: str):
        return None

    def secret(self, app_env_id: str, name: str):
        return None


class RegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.router = Router()
        self.services = Services(db=None, config=_FakeConfigStore(), keys=None, clock=lambda: 0.0)
        runtime_api.register(self.router, self.services)

    def test_all_five_routes_registered_with_the_right_methods_and_auth(self) -> None:
        expected = {
            ("POST", "/_hull/v1/flows/{name}/run"): "app+optional_user",
            ("POST", "/_hull/v1/agents/{name}/runs"): "app+optional_user",
            ("POST", "/_hull/v1/agents/{name}/runs/{runId}/cancel"): "app+optional_user",
            ("POST", "/_hull/v1/tools/{alias}/call"): "app+user",
            ("GET", "/_hull/v1/usage/me"): "app+user",
        }
        got = {(route.method, route.pattern.pattern): route.auth for route in self.router._routes}
        # Router stores compiled patterns; check by re-deriving the literal path
        # segments is overkill here, so match on route match() behavior instead.
        self.assertEqual(len(self.router._routes), 5)
        for (method, path_template), auth in expected.items():
            path = path_template.format(name="summarize", runId="run1", alias="kb")
            found = self.router.match(method, path)
            self.assertIsNotNone(found, f"{method} {path} did not match any route")
            route, params = found
            self.assertEqual(route.auth, auth)

    def test_wrong_method_on_a_known_path_raises_405(self) -> None:
        from sites.broker.http import HttpError

        with self.assertRaises(HttpError) as ctxmgr:
            self.router.match("GET", "/_hull/v1/flows/summarize/run")
        self.assertEqual(ctxmgr.exception.status, 405)

    def test_unknown_path_matches_nothing(self) -> None:
        self.assertIsNone(self.router.match("GET", "/_hull/v1/nope"))


@unittest.skipUnless(_PG_DSN, "HULL_TEST_PG_DSN is not set; see tests/test_broker_ledger.py")
class EndToEndRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        with psycopg.connect(_PG_DSN, autocommit=True) as connection:
            connection.execute("DROP SCHEMA public CASCADE")
            connection.execute("CREATE SCHEMA public")
        self.db = BrokerDatabase(lambda: psycopg.connect(_PG_DSN))
        migrate(self.db)
        self.services = Services(db=self.db, config=_FakeConfigStore(), keys=None, clock=lambda: 1_800_000_000.0)
        self.router = Router()
        runtime_api.register(self.router, self.services)
        self.addCleanup(os.environ.pop, "HULL_LLM_BASE_URL", None)
        os.environ.pop("HULL_LLM_BASE_URL", None)
        runtime_api._LLM_CLIENT = None  # force re-resolution per test

    def _runtime(self, config, settings):
        return AppRuntime(app_env_id="app1", version=1, digest="d", config=config, settings=settings)

    def test_usage_me_round_trip_through_the_router(self) -> None:
        from sites.broker.http import Request

        route, params = self.router.match("GET", "/_hull/v1/usage/me")
        request = Request(
            method="GET", path="/_hull/v1/usage/me", params=params, query={}, headers={}, body=None,
            host="app.test", client_ip="127.0.0.1", cookies={},
            app=AppCaller(app_env_id="app1", site="s"),
            user=EndUser(id="u1", org=None, role="member", plan="free", env="dev"),
            runtime=self._runtime({"plans": {"free": {"features": [], "limits": {"ai_credits_month": 50}}}}, {}),
        )
        response = route.handler(request)
        self.assertIsInstance(response, Response)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["used"], 0)
        self.assertEqual(response.body["limit"], 50)

    def test_flow_route_degrades_gracefully_when_llm_is_unconfigured(self) -> None:
        from sites.broker.http import Request

        config = {
            "flows": {"summarize": {
                "mode": "llm", "model_tier": "fast", "prompt": "{{input.x}}",
                "input_schema": {}, "output_schema": {}, "fallback": {"summary": ""},
                "audience": "server", "requires_feature": None, "budget": {"max_credits_per_call": 5},
            }},
            "plans": {"free": {"features": [], "limits": {"ai_credits_month": 100}}},
        }
        settings = {
            "ai_enabled": True,
            "model_tiers": {"fast": {"model": "m", "input_credits_per_1k": 1, "output_credits_per_1k": 1}},
            "limits": {"app_credits_month": None},
        }
        route, params = self.router.match("POST", "/_hull/v1/flows/summarize/run")
        request = Request(
            method="POST", path="/_hull/v1/flows/summarize/run", params=params, query={}, headers={},
            body={"input": {"x": 1}}, host="app.test", client_ip="127.0.0.1", cookies={},
            app=AppCaller(app_env_id="app1", site="s"),
            user=EndUser(id="u1", org=None, role="member", plan="free", env="dev"),
            runtime=self._runtime(config, settings),
        )
        response = route.handler(request)
        self.assertEqual(response.status, 200)
        self.assertTrue(response.body["fallback"])
        self.assertEqual(response.body["reason"], "upstream_error")

    def test_agent_route_returns_a_stream_response(self) -> None:
        from sites.broker.http import Request

        os.environ["HULL_AGENT_RUNTIME_URL"] = "http://127.0.0.1:1"  # deliberately unreachable
        self.addCleanup(os.environ.pop, "HULL_AGENT_RUNTIME_URL", None)
        config = {"agents": {"triage": {
            "instructions": "help", "scope": "", "guardrails": [], "model_tier": "fast", "skills": [],
            "tools": [], "run_as": "end_user", "requires_feature": None, "budget": {"max_credits_per_run": 10},
        }}, "plans": {"free": {"features": [], "limits": {"ai_credits_month": 100}}}}
        settings = {"model_tiers": {"fast": {"model": "m", "input_credits_per_1k": 1, "output_credits_per_1k": 1}},
                    "limits": {"app_credits_month": None}}
        route, params = self.router.match("POST", "/_hull/v1/agents/triage/runs")
        request = Request(
            method="POST", path="/_hull/v1/agents/triage/runs", params=params, query={}, headers={},
            body={"input": "hi"}, host="app.test", client_ip="127.0.0.1", cookies={},
            app=AppCaller(app_env_id="app1", site="s"),
            user=EndUser(id="u1", org=None, role="member", plan="free", env="dev"),
            runtime=self._runtime(config, settings),
        )
        response = route.handler(request)
        self.assertIsInstance(response, StreamResponse)
        events = list(response.events)
        self.assertEqual(events[0][0], "run.started")
        self.assertEqual(events[-1][0], "run.error")  # unreachable runtime URL


@unittest.skipUnless(_PG_DSN, "HULL_TEST_PG_DSN is not set; see tests/test_broker_ledger.py")
class AdminUsageTests(unittest.TestCase):
    def setUp(self) -> None:
        with psycopg.connect(_PG_DSN, autocommit=True) as connection:
            connection.execute("DROP SCHEMA public CASCADE")
            connection.execute("CREATE SCHEMA public")
        self.db = BrokerDatabase(lambda: psycopg.connect(_PG_DSN))
        migrate(self.db)
        self.services = Services(db=self.db, config=_FakeConfigStore(), keys=None, clock=lambda: 1_800_000_000.0)

    def test_delegates_to_ledger_usage_since(self) -> None:
        ledger = Ledger(self.db, self.services.clock)
        r = ledger.reserve(app_env_id="app1", scope="flow:x", amount=10, user_subject=("user", "u1"))
        ledger.settle(r.id, app_env_id="app1", actual_amount=4)
        rows = runtime_api.admin_usage(self.services, "app1", dt.date(2000, 1, 1))
        self.assertEqual(rows, [{"userId": "u1", "credits": 4, "calls": 1}])


if __name__ == "__main__":
    unittest.main()
