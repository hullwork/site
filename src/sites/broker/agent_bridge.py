"""``POST /_hull/v1/agents/{name}/runs`` (SSE) and its cancel route.

Bridges one Broker agent run to the agent runtime's caller-tools extension
(contract §5): build the system prompt and per-user tool list, call
``POST {HULL_AGENT_RUNTIME_URL}/api/ag-ui`` with trusted-proxy headers, translate
its AG-UI SSE stream into the Broker's own event vocabulary (contract §3.4), run
``hull/caller_tool_call`` requests through the same gate ``tools.py`` uses, meter
credits from ``agent/event`` usage frames, and settle once at the end.

ADAPTERS beyond contract §4/§5 (see the partition report for the full list):

- This slice only reads a *synchronous* AG-UI response: one line per ``data:``
  frame, no reconnect-with-``Last-Event-ID``. That matches the fake agent runtime
  used in tests and is a documented simplification of full SSE resilience.
- A capability tool's ``parameters`` schema is not fetched from its MCP
  ``tools/list`` at tool-list build time (that would mean a network call per
  agent-run start); it is offered to the model as ``{"type": "object"}`` and
  validated for real only inside ``tools.execute`` at call time.
- Cancellation state (``_ACTIVE_RUNS``) is an in-process dict, not a database
  table: a cancel request that lands on a different replica than the one running
  the stream will not interrupt it in this slice. The budget-triggered
  cancellation required by the brief happens inside the same request's streaming
  loop, so it does not depend on this registry at all.
"""
from __future__ import annotations

import json
import os
import re
import uuid
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterator

from sites.broker import llm as llm_mod
from sites.broker import mcp_client
from sites.broker import tools as tools_mod
from sites.broker.db import register_migration
from sites.broker.http import HttpError, Request, Response, Services, StreamResponse
from sites.broker.ledger import BudgetExceeded, Ledger
from sites import telemetry

DEFAULT_CALLER_TOOLS_MAX = 16
DEFAULT_CALLER_TOOL_MAX_RESULT_CHARS = 20000
MAX_SYSTEM_PROMPT_CHARS = 20000  # the agent runtime's own custom-agent system prompt limit
_WIRE_TOOL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")

# -- thread ownership: a user can only continue their own threads -------------------

_THREADS_DDL = """
CREATE TABLE broker_agent_threads (
    thread_id TEXT PRIMARY KEY,
    app_env_id TEXT NOT NULL,
    user_id TEXT,
    agent_name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


def _apply_v3(cursor: Any) -> None:
    cursor.execute(_THREADS_DDL)


register_migration("runtime", 3, "agent thread ownership", _apply_v3)

# In-process registry of runs currently being streamed, for the cancel route (see
# the module docstring for its single-replica limitation).
_ACTIVE_RUNS: dict[str, dict[str, Any]] = {}


class AgentRuntimeUnavailable(Exception):
    pass


def _thread_owner(db: Any, thread_id: str) -> tuple[str, str] | None:
    with db.transaction() as cursor:
        cursor.execute(
            "SELECT app_env_id, user_id FROM broker_agent_threads WHERE thread_id = %s", (thread_id,),
        )
        row = cursor.fetchone()
    if row is None:
        return None
    return (row[0], row[1] if row[1] is not None else "app")


def _record_thread(db: Any, thread_id: str, app_env_id: str, user_key: str, agent_name: str) -> None:
    stored_user = None if user_key == "app" else user_key
    with db.transaction() as cursor:
        cursor.execute(
            "INSERT INTO broker_agent_threads (thread_id, app_env_id, user_id, agent_name)"
            " VALUES (%s, %s, %s, %s)",
            (thread_id, app_env_id, stored_user, agent_name),
        )


def _user_subject(user) -> tuple[str, str] | None:
    if user is None:
        return None
    return ("org", user.org) if user.org else ("user", user.id)


def _read_proxy_secret() -> str:
    path = os.environ.get("HULL_AGENT_PROXY_SECRET_FILE", "").strip()
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


# -- prompt / tool list building ------------------------------------------------


def build_system_prompt(agent_config: dict[str, Any]) -> str:
    parts = [str(agent_config.get("instructions") or "")]
    scope = agent_config.get("scope")
    if scope:
        parts.append(f"Scope: {scope}")
    guardrails = agent_config.get("guardrails") or []
    if guardrails:
        parts.append("Guardrails:\n" + "\n".join(f"- {g}" for g in guardrails))
    # Skills are empty in this slice; the shape is here so a later slice only has
    # to populate agent_config["skills"], not touch the prompt assembly.
    skills = agent_config.get("skills") or []
    if skills:
        lines = [
            f"- {s.get('name', '')}: {s.get('description', '')}" for s in skills if isinstance(s, dict)
        ]
        parts.append("Skills:\n" + "\n".join(lines))
    prompt = "\n\n".join(part for part in parts if part)
    return prompt[:MAX_SYSTEM_PROMPT_CHARS]


def _wire_tool_name(raw_ref: str) -> str:
    return raw_ref.replace("entity:", "entity__").replace(".", "__")


def build_tool_list(ctx: tools_mod.GateContext, agent_config: dict[str, Any]) -> tuple[list[dict], dict[str, str]]:
    """Only tools :func:`tools.would_allow` grants this user, as §5 wire tool defs."""
    declared = agent_config.get("tools") or []
    tool_defs: list[dict[str, Any]] = []
    wire_to_ref: dict[str, str] = {}
    for raw in declared:
        if len(tool_defs) >= DEFAULT_CALLER_TOOLS_MAX:
            break
        try:
            ref = tools_mod.parse_tool_ref(raw)
        except tools_mod.InvalidToolRef:
            continue
        allowed, capability = tools_mod.would_allow(ctx, ref)
        if not allowed:
            continue
        wire_name = _wire_tool_name(raw)
        if not _WIRE_TOOL_NAME.match(wire_name):
            continue
        if ref.kind == "entity":
            description = f"Read rows from the {ref.entity!r} entity."
            parameters = {"type": "object", "properties": {
                "filters": {"type": "object"}, "limit": {"type": "integer"},
            }}
        else:
            description = f"Call the {ref.tool_name!r} tool of the {ref.capability_alias!r} capability."
            parameters = {"type": "object"}  # see module docstring: not fetched from tools/list here
        tool_defs.append({"name": wire_name, "description": description, "parameters": parameters})
        wire_to_ref[wire_name] = raw
    return tool_defs, wire_to_ref


# -- HTTP to the agent runtime ----------------------------------------------------


def _stream_agent_events(base_url: str, headers: dict[str, str], payload: dict[str, Any],
                          *, timeout_seconds: float) -> Iterator[dict[str, Any]]:
    request = urllib.request.Request(
        f"{base_url}/api/ag-ui", data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST",
    )
    try:
        response = urllib.request.urlopen(request, timeout=timeout_seconds)
    except (urllib.error.URLError, OSError) as exc:
        raise AgentRuntimeUnavailable("the agent runtime could not be reached") from exc
    try:
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            chunk = line[len("data:"):].strip()
            if not chunk:
                continue
            try:
                yield json.loads(chunk)
            except json.JSONDecodeError:
                continue
    except (urllib.error.URLError, OSError) as exc:
        raise AgentRuntimeUnavailable("the agent runtime connection dropped") from exc
    finally:
        response.close()


def _post_json(base_url: str, path: str, headers: dict[str, str], body: dict[str, Any]) -> None:
    request = urllib.request.Request(
        f"{base_url}{path}", data=json.dumps(body).encode("utf-8"), headers=headers, method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=10).close()
    except (urllib.error.URLError, OSError) as exc:
        telemetry.log_exception("broker_agent_bridge_callback_failed", exc, path=path)


def _cancel_upstream(base_url: str, headers: dict[str, str], thread_id: str, run_id: str) -> None:
    _post_json(base_url, f"/api/sessions/{thread_id}/cancel", headers, {"run_id": run_id})


def _post_tool_result(base_url: str, headers: dict[str, str], thread_id: str,
                       request_id: str, content: str, is_error: bool) -> None:
    _post_json(base_url, f"/api/sessions/{thread_id}/tool-results", headers,
               {"requestId": request_id, "content": content, "isError": is_error})


# -- caller tool execution --------------------------------------------------------


def _handle_caller_tool_call(
    *, services: Services, gate_ctx: tools_mod.GateContext, wire_to_ref: dict[str, str],
    base_url: str, headers: dict[str, str], thread_id: str, value: dict[str, Any],
) -> Iterator[tuple[str, dict[str, Any]]]:
    request_id = value.get("requestId")
    wire_name = value.get("name", "")
    arguments = value.get("arguments") if isinstance(value.get("arguments"), dict) else {}
    raw_ref = wire_to_ref.get(wire_name)
    if raw_ref is None or request_id is None:
        if request_id is not None:
            _post_tool_result(base_url, headers, thread_id, request_id, "unknown tool.", True)
        return

    ref = tools_mod.parse_tool_ref(raw_ref)
    try:
        risk, capability = tools_mod.evaluate(gate_ctx, services, ref, arguments)
    except tools_mod.GateDenied as denied:
        if denied.reason == "approval_required":
            yield ("approval.pending", {"approvalId": denied.approval_id, "tool": wire_name})
            _post_tool_result(
                base_url, headers, thread_id, request_id,
                "this action requires human approval and cannot complete automatically in this slice.", True,
            )
        else:
            _post_tool_result(base_url, headers, thread_id, request_id,
                               f"tool call refused: {denied.reason}.", True)
        return

    try:
        result = tools_mod.execute(services, gate_ctx, ref, capability, arguments)
    except (mcp_client.NotConnected, mcp_client.MCPUnavailable) as exc:
        result = mcp_client.ToolCallResult(content=str(exc), is_error=True)
    except Exception as exc:
        # The agent runtime is blocked waiting for this answer; without one it would
        # sit until its caller-tool timeout. Report the failure as the tool result.
        telemetry.log_exception("broker_agent_tool_failed", exc, tool=wire_name)
        result = mcp_client.ToolCallResult(content="the tool failed.", is_error=True)

    content = tools_mod.wrap_untrusted(result.content)
    if len(content) > DEFAULT_CALLER_TOOL_MAX_RESULT_CHARS:
        content = content[:DEFAULT_CALLER_TOOL_MAX_RESULT_CHARS]
    _post_tool_result(base_url, headers, thread_id, request_id, content, result.is_error)


# -- the run itself -----------------------------------------------------------------


def _drive_run(
    *, services: Services, app_env_id: str, user_header: str, gate_ctx: tools_mod.GateContext,
    ledger: Ledger, reservation, run_id: str, thread_id: str, tool_defs: list[dict],
    wire_to_ref: dict[str, str], system_prompt: str, model_name: str, input_text: str,
    tier: llm_mod.TierConfig | None,
) -> Iterator[tuple[str, dict[str, Any]]]:
    base_url = os.environ.get("HULL_AGENT_RUNTIME_URL", "").rstrip("/")
    headers = {
        "Content-Type": "application/json",
        "X-Agent-Proxy-Secret": _read_proxy_secret(),
        "X-Agent-Tenant": f"app.{app_env_id}",
        "X-Agent-User": user_header,
    }
    payload = {
        "threadId": thread_id, "runId": run_id,
        "messages": [{"id": "m" + uuid.uuid4().hex[:12], "role": "user", "content": input_text}],
        "tools": tool_defs,
        "forwardedProps": {"agent": "runtime", "systemPrompt": system_prompt, "model": model_name},
    }

    total_credits = 0
    tool_names: dict[str, str] = {}
    cancel_state = {"cancelled": False}
    _ACTIVE_RUNS[run_id] = {"thread_id": thread_id, "cancel": cancel_state,
                            "app_env_id": app_env_id, "user": user_header}
    yield ("run.started", {"runId": run_id, "threadId": thread_id})

    if not base_url:
        yield ("run.error", {"code": "upstream_error", "message": "the agent runtime is not configured."})
        _ACTIVE_RUNS.pop(run_id, None)
        ledger.settle(reservation.id, app_env_id=app_env_id, actual_amount=0)
        return

    try:
        for event in _stream_agent_events(base_url, headers, payload, timeout_seconds=60.0):
            if cancel_state["cancelled"]:
                # handle_cancel already forwarded the cancel to the agent runtime
                # before flipping this flag; this loop only has to stop reading.
                yield ("run.error", {"code": "cancelled", "message": "the run was cancelled."})
                return

            etype = event.get("type")
            if etype == "TEXT_MESSAGE_CONTENT":
                delta = event.get("delta")
                if delta:
                    yield ("message.delta", {"text": delta})
            elif etype == "TOOL_CALL_START":
                call_id = str(event.get("toolCallId", ""))
                tool_name = str(event.get("toolCallName", ""))
                tool_names[call_id] = tool_name
                yield ("tool.started", {"callId": call_id, "tool": tool_name})
            elif etype == "TOOL_CALL_RESULT":
                call_id = str(event.get("toolCallId", ""))
                yield ("tool.finished", {"callId": call_id, "tool": tool_names.get(call_id, ""), "ok": True})
            elif etype == "CUSTOM":
                custom_name = event.get("name")
                value = event.get("value") if isinstance(event.get("value"), dict) else {}
                if custom_name == "agent/event" and value.get("type") == "usage" and tier is not None:
                    usage = llm_mod.LLMUsage(
                        input_tokens=int(value.get("input") or 0),
                        output_tokens=int(value.get("output") or 0),
                        cached_tokens=int(value.get("cached") or 0),
                    )
                    total_credits += llm_mod.price_credits(tier, usage)
                    if total_credits > reservation.amount:
                        _cancel_upstream(base_url, headers, thread_id, run_id)
                        yield ("run.error", {"code": "budget_exhausted",
                                              "message": "the run exceeded its credit budget."})
                        return
                elif custom_name == "hull/caller_tool_call":
                    for out_event in _handle_caller_tool_call(
                        services=services, gate_ctx=gate_ctx, wire_to_ref=wire_to_ref,
                        base_url=base_url, headers=headers, thread_id=thread_id, value=value,
                    ):
                        yield out_event
            elif etype == "RUN_FINISHED":
                yield ("run.finished", {"usage": {"credits": total_credits}})
                return
            elif etype == "RUN_ERROR":
                yield ("run.error", {"code": event.get("code", "agent_error"),
                                      "message": event.get("message", "the agent run failed.")})
                return
        # The stream ended without a RUN_FINISHED/RUN_ERROR frame.
        yield ("run.error", {"code": "upstream_error", "message": "the agent run ended without a result."})
    except AgentRuntimeUnavailable:
        yield ("run.error", {"code": "upstream_error", "message": "the agent runtime could not be reached."})
    except Exception as exc:
        # The response is already a 200 event stream, so an unexpected failure can
        # only be reported in-band; the upstream run is cancelled so it stops spending.
        telemetry.log_exception("broker_agent_run_failed", exc, run_id=run_id)
        _cancel_upstream(base_url, headers, thread_id, run_id)
        yield ("run.error", {"code": "internal_error", "message": "the agent run failed."})
    finally:
        _ACTIVE_RUNS.pop(run_id, None)
        # Idempotent: whichever branch above already settled (or none did), this
        # charges exactly what was metered and releases the rest of the hold.
        ledger.settle(reservation.id, app_env_id=app_env_id, actual_amount=total_credits)


def handle_run(request: Request, services: Services) -> Response | StreamResponse:
    if request.app is None or request.runtime is None:
        raise HttpError(401, "unauthenticated", "an app token is required.")
    runtime = request.runtime
    name = request.params.get("name", "")
    agent_config = (runtime.config.get("agents") or {}).get(name)
    if agent_config is None:
        raise HttpError(404, "unknown_agent", f"there is no agent named {name!r}.")

    run_as = agent_config.get("run_as", "end_user")
    if run_as != "app" and request.user is None:
        raise HttpError(401, "user_required", "this agent requires an end user.")

    requires_feature = agent_config.get("requires_feature")
    if requires_feature:
        plans = runtime.config.get("plans") or {}
        plan = plans.get(request.user.plan) if request.user else None
        features = (plan or {}).get("features") or []
        if requires_feature not in features:
            raise HttpError(403, "entitlement_required",
                             "this agent requires a feature the user's plan does not include.")

    body = request.body if isinstance(request.body, dict) else {}
    input_text = body.get("input")
    if not isinstance(input_text, str) or not input_text:
        raise HttpError(400, "invalid_input", "the request body must include a text input.")

    user_key = request.user.id if request.user else "app"
    thread_id = body.get("threadId")
    if thread_id:
        owner = _thread_owner(services.db, thread_id)
        if owner is None or owner != (request.app.app_env_id, user_key):
            raise HttpError(403, "thread_forbidden", "this thread does not belong to the caller.")
    else:
        thread_id = "t" + uuid.uuid4().hex
        _record_thread(services.db, thread_id, request.app.app_env_id, user_key, name)

    budget = agent_config.get("budget") or {}
    max_credits_per_run = budget.get("max_credits_per_run")
    if not isinstance(max_credits_per_run, int) or max_credits_per_run <= 0:
        raise HttpError(500, "misconfigured_agent", "this agent has no valid run budget.")

    plans = runtime.config.get("plans") or {}
    user_limit = None
    if request.user is not None:
        plan = plans.get(request.user.plan) or {}
        user_limit = (plan.get("limits") or {}).get("ai_credits_month")
    app_limit = (runtime.settings.get("limits") or {}).get("app_credits_month")

    ledger = Ledger(services.db, services.clock)
    try:
        reservation = ledger.reserve(
            app_env_id=request.app.app_env_id, scope=f"agent:{name}", amount=max_credits_per_run,
            user_subject=_user_subject(request.user), user_limit=user_limit, app_limit=app_limit,
            call_cap=max_credits_per_run,
        )
    except BudgetExceeded:
        raise HttpError(402, "budget_exhausted", "the user has no remaining AI credit budget.") from None

    run_id = "run" + uuid.uuid4().hex
    gate_ctx = tools_mod.GateContext(
        runtime=runtime, user=request.user, run_as=run_as,
        allowed_tools=frozenset(agent_config.get("tools") or []), ledger=ledger,
        app_env_id=request.app.app_env_id, requires_feature=None,  # already checked above, once per run
    )
    tool_defs, wire_to_ref = build_tool_list(gate_ctx, agent_config)
    tier = llm_mod.resolve_tier(runtime.settings.get("model_tiers"), agent_config.get("model_tier"))
    model_name = tier.model if tier is not None else str(agent_config.get("model_tier") or "")

    events = _drive_run(
        services=services, app_env_id=request.app.app_env_id, user_header=user_key, gate_ctx=gate_ctx,
        ledger=ledger, reservation=reservation, run_id=run_id, thread_id=thread_id, tool_defs=tool_defs,
        wire_to_ref=wire_to_ref, system_prompt=build_system_prompt(agent_config), model_name=model_name,
        input_text=input_text, tier=tier,
    )
    return StreamResponse(events=events)


def handle_cancel(request: Request, services: Services) -> Response:
    if request.app is None:
        raise HttpError(401, "unauthenticated", "an app token is required.")
    run_id = request.params.get("runId", "")
    active = _ACTIVE_RUNS.get(run_id)
    caller = request.user.id if request.user else "app"
    # Only the app and user that started a run may cancel it. A run that is not
    # theirs answers exactly like one that is not active, so run ids cannot be probed.
    if active is not None and (active["app_env_id"] != request.app.app_env_id
                               or active["user"] != caller):
        active = None
    if active is not None:
        active["cancel"]["cancelled"] = True
        base_url = os.environ.get("HULL_AGENT_RUNTIME_URL", "").rstrip("/")
        if base_url:
            headers = {
                "Content-Type": "application/json",
                "X-Agent-Proxy-Secret": _read_proxy_secret(),
                "X-Agent-Tenant": f"app.{request.app.app_env_id}",
                "X-Agent-User": caller,
            }
            _cancel_upstream(base_url, headers, active["thread_id"], run_id)
    return Response(202, {})
