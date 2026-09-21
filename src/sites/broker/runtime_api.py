"""``register(router, services)`` and ``admin_usage(...)`` — contract §4, D's side.

C's ``server.py`` imports this module lazily and calls exactly these two functions;
nothing else here is part of the cross-partition surface. Route auth modes are this
module's own call where the contract does not pin one down (see flows.py and
tools.py module docstrings for the specific reasoning per route).
"""
from __future__ import annotations

import datetime as dt

from sites import telemetry
from sites.broker import agent_bridge, flows, tools
from sites.broker.http import Router, Services
from sites.broker.ledger import Ledger
from sites.broker.llm import HttpLLMClient, LLMClient, LLMUnavailable

# Constructed lazily, once, on first flow call — never at import/registration time.
# A gateway with AI features turned off for every app it currently serves must
# still start even when HULL_LLM_BASE_URL is not set; the first flow that actually
# needs an LLM degrades to its "upstream_error" fallback instead of crashing.
_LLM_CLIENT: LLMClient | None = None


class _UnconfiguredLLMClient:
    """Stands in for a real client when HULL_LLM_BASE_URL(/key) is missing.

    Every call raises LLMUnavailable, which flows.handle_run already turns into
    the "upstream_error" fallback — so this is the whole fix, not a special case
    flows.py has to know about.
    """

    def complete(self, **_kwargs):
        raise LLMUnavailable("the LLM upstream is not configured")


def _default_llm_client() -> LLMClient:
    global _LLM_CLIENT
    if _LLM_CLIENT is None:
        try:
            _LLM_CLIENT = HttpLLMClient.from_env()
        except ValueError:
            telemetry.log("broker_llm_not_configured", level="warn")
            _LLM_CLIENT = _UnconfiguredLLMClient()
    return _LLM_CLIENT


def register(router: Router, services: Services) -> None:
    def run_flow(request):
        return flows.handle_run(request, services, _default_llm_client())

    def run_agent(request):
        return agent_bridge.handle_run(request, services)

    def cancel_agent(request):
        return agent_bridge.handle_cancel(request, services)

    def call_tool(request):
        return tools.handle_call(request, services)

    def usage_me(request):
        return tools.usage_me(request, services)

    # audience is a per-flow config field (contract §3.6), not an auth mode; the
    # handler itself requires a user unless that specific flow is "public" — see
    # flows.py's module docstring for why "app+optional_user" is the right mode
    # for one route that serves both audiences.
    router.add("POST", "/_hull/v1/flows/{name}/run", run_flow, auth="app+optional_user")
    router.add("POST", "/_hull/v1/agents/{name}/runs", run_agent, auth="app+optional_user")
    router.add("POST", "/_hull/v1/agents/{name}/runs/{runId}/cancel", cancel_agent, auth="app+optional_user")
    router.add("POST", "/_hull/v1/tools/{alias}/call", call_tool, auth="app+user")
    router.add("GET", "/_hull/v1/usage/me", usage_me, auth="app+user")


def admin_usage(services: Services, app_env_id: str, since: dt.date) -> list[dict]:
    """Per-user settled credits and call counts since ``since`` — the admin usage report."""
    ledger = Ledger(services.db, services.clock)
    return ledger.usage_since(app_env_id, since)
