"""The LLM client flows use, and the settings-driven tier -> model/pricing lookup.

``LLMClient`` is a narrow protocol (one method) so flows.py can be tested against
``FakeLLM`` without a network. ``HttpLLMClient`` speaks the OpenAI chat-completions
wire format, which is what every provider behind ``HULL_LLM_BASE_URL`` in this slice
implements. Tier -> model and tier -> credit pricing come from settings
``model_tiers`` (contract §3.7); config only ever says ``model_tier: "fast"``, never
a model name, so callers must resolve one before they can price a call at all.
"""
from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from sites import telemetry

_DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
_DEFAULT_READ_TIMEOUT_SECONDS = 30.0


class LLMError(Exception):
    """Base class for LLM call failures."""


class LLMUnavailable(LLMError):
    """The upstream could not be reached or returned malformed output.

    Callers map this to the flow fallback reason ``upstream_error``.
    """


@dataclass(frozen=True)
class LLMUsage:
    input_tokens: int
    output_tokens: int
    cached_tokens: int = 0
    # Prompt-cache writes are reported apart from input tokens and cost at least as
    # much as input; there is no separate price yet, so they are billed as input.
    cache_write_tokens: int = 0


@dataclass(frozen=True)
class LLMResult:
    text: str
    usage: LLMUsage
    model: str


class LLMClient(Protocol):
    def complete(
        self, *, model: str, messages: list[dict[str, str]],
        output_schema: dict[str, Any] | None = None,
    ) -> LLMResult:
        ...


class FakeLLM:
    """Deterministic stand-in for tests. Records every call it saw."""

    def __init__(
        self,
        response: str | Any = "",
        *,
        usage: LLMUsage | None = None,
        raises: Exception | None = None,
    ) -> None:
        self._response = response
        self._usage = usage or LLMUsage(input_tokens=10, output_tokens=10)
        self._raises = raises
        self.calls: list[dict[str, Any]] = []

    def complete(
        self, *, model: str, messages: list[dict[str, str]],
        output_schema: dict[str, Any] | None = None,
    ) -> LLMResult:
        self.calls.append({"model": model, "messages": messages, "output_schema": output_schema})
        if self._raises is not None:
            raise self._raises
        text = self._response(messages) if callable(self._response) else self._response
        return LLMResult(text=text, usage=self._usage, model=model)


def _read_key_file(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise LLMUnavailable(f"could not read the LLM API key file: {type(exc).__name__}") from exc


class HttpLLMClient:
    """OpenAI-chat-completions-compatible client for ``HULL_LLM_BASE_URL``."""

    def __init__(
        self, *, base_url: str, api_key: str = "",
        connect_timeout_seconds: float = _DEFAULT_CONNECT_TIMEOUT_SECONDS,
        read_timeout_seconds: float = _DEFAULT_READ_TIMEOUT_SECONDS,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        # urllib's socket timeout covers connect and read together; the two knobs
        # stay separate in the constructor because that's the contract (D2), and
        # the larger of the two is what's actually passed to urlopen.
        self._timeout_seconds = max(connect_timeout_seconds, read_timeout_seconds)

    @classmethod
    def from_env(cls) -> "HttpLLMClient":
        base_url = os.environ.get("HULL_LLM_BASE_URL", "").strip()
        if not base_url:
            raise ValueError("HULL_LLM_BASE_URL is required")
        key_file = os.environ.get("HULL_LLM_API_KEY_FILE", "").strip()
        api_key = _read_key_file(key_file) if key_file else ""
        connect_timeout = float(os.environ.get(
            "HULL_LLM_CONNECT_TIMEOUT_SECONDS", str(_DEFAULT_CONNECT_TIMEOUT_SECONDS)
        ))
        read_timeout = float(os.environ.get(
            "HULL_LLM_READ_TIMEOUT_SECONDS", str(_DEFAULT_READ_TIMEOUT_SECONDS)
        ))
        return cls(
            base_url=base_url, api_key=api_key,
            connect_timeout_seconds=connect_timeout, read_timeout_seconds=read_timeout,
        )

    def complete(
        self, *, model: str, messages: list[dict[str, str]],
        output_schema: dict[str, Any] | None = None,
    ) -> LLMResult:
        body: dict[str, Any] = {"model": model, "messages": messages}
        if output_schema is not None:
            # JSON output mode: the widest-supported way to ask an OpenAI-compatible
            # endpoint for parseable JSON is response_format: json_object, not
            # embedding the schema itself (support for json_schema mode varies a lot
            # more across providers than json_object does).
            body["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        request = urllib.request.Request(
            f"{self._base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                raw = response.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            telemetry.log_exception("broker_llm_upstream_unreachable", exc, model=model)
            raise LLMUnavailable("the LLM upstream could not be reached") from exc
        try:
            payload = json.loads(raw)
            content = payload["choices"][0]["message"]["content"]
            usage_raw = payload.get("usage") or {}
            usage = LLMUsage(
                input_tokens=int(usage_raw.get("prompt_tokens", 0)),
                output_tokens=int(usage_raw.get("completion_tokens", 0)),
                cached_tokens=int(usage_raw.get("prompt_tokens_details", {}).get("cached_tokens", 0))
                if isinstance(usage_raw.get("prompt_tokens_details"), dict) else 0,
            )
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            telemetry.log_exception("broker_llm_malformed_response", exc, model=model)
            raise LLMUnavailable("the LLM upstream returned a malformed response") from exc
        return LLMResult(text=content, usage=usage, model=model)


@dataclass(frozen=True)
class TierConfig:
    tier: str
    model: str
    input_credits_per_1k: float
    output_credits_per_1k: float


def resolve_tier(model_tiers: Any, tier: str) -> TierConfig | None:
    """Look up a config ``model_tier`` name in settings ``model_tiers``.

    Returns ``None`` (after logging) rather than raising: an unknown tier is a
    settings/config drift, not a crash — the caller (flows.py) maps a ``None``
    here to the fallback reason ``upstream_error``.
    """
    entry = model_tiers.get(tier) if isinstance(model_tiers, dict) else None
    if not isinstance(entry, dict) or not isinstance(entry.get("model"), str) or not entry["model"]:
        telemetry.log("broker_llm_unknown_tier", level="warn", tier=tier)
        return None
    try:
        input_credits = float(entry.get("input_credits_per_1k", 0))
        output_credits = float(entry.get("output_credits_per_1k", 0))
    except (TypeError, ValueError):
        telemetry.log("broker_llm_unknown_tier", level="warn", tier=tier, reason="bad_pricing")
        return None
    return TierConfig(
        tier=tier, model=entry["model"],
        input_credits_per_1k=input_credits, output_credits_per_1k=output_credits,
    )


def price_credits(tier: TierConfig, usage: LLMUsage) -> int:
    """Credits for one call, rounded up to the nearest whole credit."""
    cost = (
        ((usage.input_tokens + usage.cache_write_tokens) / 1000.0) * tier.input_credits_per_1k
        + (usage.output_tokens / 1000.0) * tier.output_credits_per_1k
    )
    return max(0, math.ceil(cost))
