"""``POST /_hull/v1/flows/{name}/run``: validate, gate, render, call, validate, settle.

Every failure past input validation maps to a ``fallback`` response instead of
raising — a flow is meant to degrade to its configured ``fallback`` value, never to
break the caller's request. ``mode`` is ``"llm"`` only in this slice (validator
enforced at config-apply time); anything else reaching here is config drift and
falls back to ``upstream_error`` rather than crashing.

ADAPTER beyond contract §4 (see the partition report): the contract's runtime-API
auth rule ("X-Hull-User is required unless the target runs run_as: app") is written
for agents and tools, which have a ``run_as`` field; flows do not — they have
``audience: server|public`` instead. This route is registered with auth
``"app+optional_user"`` and the handler itself requires a user only when
``audience != "public"``.
"""
from __future__ import annotations

import json
import re
from typing import Any

from sites.broker import jsonschema_lite
from sites.broker.http import HttpError, Request, Response, Services
from sites.broker.ledger import BudgetExceeded, Ledger
from sites.broker.llm import LLMClient, LLMUnavailable, price_credits, resolve_tier

MAX_IDEMPOTENCY_KEY_CHARS = 128
_PLACEHOLDER = re.compile(r"\{\{input\.([a-zA-Z_][a-zA-Z0-9_.]*)\}\}")

FALLBACK_REASONS = frozenset({
    "disabled", "ai_disabled", "budget_exhausted", "entitlement", "invalid_output", "upstream_error",
})


def render_prompt(template: str, input_obj: Any) -> str:
    """Replace every ``{{input.a.b}}`` with the JSON encoding of ``input["a"]["b"]``.

    No code execution: this is string substitution over a fixed placeholder shape,
    nothing resembling an expression language. A path that does not resolve
    substitutes JSON ``null`` rather than raising — the config was already schema
    validated at apply time, so a dangling reference here is a config bug, not a
    reason to break the caller's request.
    """
    def _resolve(path: str) -> Any:
        node = input_obj
        for part in path.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return None
        return node

    def _substitute(match: "re.Match[str]") -> str:
        value = _resolve(match.group(1))
        return json.dumps(value, ensure_ascii=False)

    return _PLACEHOLDER.sub(_substitute, template)


def _user_subject(user) -> tuple[str, str] | None:
    if user is None:
        return None
    return ("org", user.org) if user.org else ("user", user.id)


def handle_run(request: Request, services: Services, llm_client: LLMClient) -> Response:
    if request.app is None or request.runtime is None:
        raise HttpError(401, "unauthenticated", "an app token is required.")
    runtime = request.runtime
    name = request.params.get("name", "")
    flow_config = (runtime.config.get("flows") or {}).get(name)
    if flow_config is None:
        raise HttpError(404, "unknown_flow", f"there is no flow named {name!r}.")

    body = request.body if isinstance(request.body, dict) else {}
    input_obj = body.get("input")
    input_schema = flow_config.get("input_schema") or {}
    if input_schema and jsonschema_lite.validate(input_obj, input_schema):
        raise HttpError(400, "invalid_input", "the flow input does not match its schema.")

    idempotency_key = request.headers.get("Idempotency-Key")
    if idempotency_key is not None and len(idempotency_key) > MAX_IDEMPOTENCY_KEY_CHARS:
        raise HttpError(400, "invalid_input", "the Idempotency-Key header is too long.")

    if flow_config.get("audience") != "public" and request.user is None:
        raise HttpError(401, "user_required", "this flow requires an end user.")

    fallback_value = flow_config.get("fallback")

    def fallback(reason: str) -> Response:
        return Response(200, {"output": fallback_value, "fallback": True, "reason": reason,
                               "usage": {"credits": 0}})

    if runtime.settings.get("ai_enabled") is False:
        return fallback("ai_disabled")
    flow_settings = (runtime.settings.get("flows") or {}).get(name) or {}
    if flow_settings.get("enabled") is False:
        return fallback("disabled")

    requires_feature = flow_config.get("requires_feature")
    if requires_feature:
        if request.user is None:
            return fallback("entitlement")
        plans = runtime.config.get("plans") or {}
        plan = plans.get(request.user.plan) or {}
        if requires_feature not in (plan.get("features") or []):
            return fallback("entitlement")

    if flow_config.get("mode") != "llm":
        return fallback("upstream_error")

    budget = flow_config.get("budget") or {}
    call_cap = budget.get("max_credits_per_call")
    if not isinstance(call_cap, int) or call_cap <= 0:
        return fallback("upstream_error")

    plans = runtime.config.get("plans") or {}
    user_limit = None
    if request.user is not None:
        plan = plans.get(request.user.plan) or {}
        user_limit = (plan.get("limits") or {}).get("ai_credits_month")
    app_limit = (runtime.settings.get("limits") or {}).get("app_credits_month")

    ledger = Ledger(services.db, services.clock)
    try:
        reservation = ledger.reserve(
            app_env_id=request.app.app_env_id, scope=f"flow:{name}", amount=call_cap,
            idempotency_key=idempotency_key,
            idempotency_owner=request.user.id if request.user is not None else "app",
            user_subject=_user_subject(request.user),
            user_limit=user_limit, app_limit=app_limit, call_cap=call_cap,
        )
    except BudgetExceeded:
        return fallback("budget_exhausted")

    if reservation.replayed and reservation.response is not None:
        return Response(200, reservation.response)

    tier = resolve_tier(runtime.settings.get("model_tiers"), flow_config.get("model_tier"))
    if tier is None:
        ledger.release(reservation.id, app_env_id=request.app.app_env_id)
        return fallback("upstream_error")

    rendered = render_prompt(flow_config.get("prompt", ""), input_obj)
    output_schema = flow_config.get("output_schema") or None
    try:
        result = llm_client.complete(
            model=tier.model, messages=[{"role": "user", "content": rendered}], output_schema=output_schema,
        )
    except LLMUnavailable:
        ledger.release(reservation.id, app_env_id=request.app.app_env_id)
        return fallback("upstream_error")

    if output_schema:
        try:
            output = json.loads(result.text)
        except json.JSONDecodeError:
            ledger.release(reservation.id, app_env_id=request.app.app_env_id)
            return fallback("invalid_output")
        if jsonschema_lite.validate(output, output_schema):
            ledger.release(reservation.id, app_env_id=request.app.app_env_id)
            return fallback("invalid_output")
    else:
        output = result.text

    credits = price_credits(tier, result.usage)
    response_body = {"output": output, "fallback": False, "usage": {"credits": credits}}
    settled = ledger.settle(
        reservation.id, app_env_id=request.app.app_env_id, actual_amount=credits, response=response_body,
    )
    return Response(200, {**response_body, "usage": {"credits": settled.settled_amount}})
