"""Dependency-free W3C tracing with an optional OTLP/HTTP JSON exporter.

One header in, one header out, one field in every log line. No SDK, no exporter, no
sampler: a trace id is a string, and the value of carrying it is that an operator can
follow one request across services. A deployment that puts an already-instrumented gateway
or mesh in front of this one joins the same trace automatically, because ``traceparent`` is
the header those already speak - a home-grown ``X-Request-Id`` would end at this hop.

🔴 **Nothing here ever refuses a request.** Every other guard in this repository fails
closed; this one deliberately does not. A malformed trace header that returned 400 would
mean the diagnostic tooling had become a way to cause the outage it exists to explain, and
whoever sent it is usually a proxy the caller does not control. Anything unparseable is
treated as absent and a local id is generated instead.
"""
from __future__ import annotations

import contextvars
import hashlib
import re
import secrets
from dataclasses import dataclass, field
import json
import os
import queue
import threading
import time
import urllib.request
from functools import wraps
from typing import Any, Callable


TRACEPARENT_HEADER = "traceparent"
REQUEST_ID_HEADER = "X-Request-Id"

# The only version this contract accepts. W3C allows a receiver to parse later versions
# leniently; the three services on this contract agreed on the strict reading instead, so
# that all of them treat exactly the same inputs as valid.
_VERSION = "00"
_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_FLAGS_RE = re.compile(r"^[0-9a-f]{2}$")
# W3C: an all-zero trace-id or span-id is invalid. They are what a broken instrumentation
# emits when it has nothing to say, so accepting them would collapse unrelated requests
# into one trace - worse than having no trace at all, because it looks like a trace.
_ZERO_TRACE_ID = "0" * 32
_ZERO_SPAN_ID = "0" * 16
# Flags for a trace this service starts itself. Sampled, because it logs one line per
# request either way, and "not sampled" invites a downstream to drop the context entirely.
#
# 🔴 Only for traces we start. An inbound decision is inherited verbatim: overriding an
# upstream that said "00" is reversing a decision that was already made, somewhere the
# upstream cannot see it happen.
_FLAGS_SAMPLED = "01"

_current_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "sites_trace_id", default=""
)
_current_flags: contextvars.ContextVar[str] = contextvars.ContextVar(
    "sites_trace_flags", default=_FLAGS_SAMPLED
)
_current_span_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "sites_span_id", default=""
)


def _positive_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default)) or default))
    except ValueError:
        return default


@dataclass(frozen=True)
class _SpanData:
    trace_id: str
    span_id: str
    parent_span_id: str
    name: str
    kind: int
    start_ns: int
    end_ns: int
    attributes: dict[str, Any] = field(default_factory=dict)
    error: str = ""


class _Exporter:
    """A single bounded worker; request threads never perform collector I/O."""

    def __init__(
        self,
        endpoint: str,
        service: str,
        *,
        queue_size: int,
        batch_size: int,
        flush_seconds: float,
        timeout_seconds: float,
        observe: Callable[[str, int], None] | None,
    ):
        self.endpoint = endpoint.rstrip("/")
        if not self.endpoint.endswith("/v1/traces"):
            self.endpoint += "/v1/traces"
        self.service = service
        self.batch_size = batch_size
        self.flush_seconds = flush_seconds
        self.timeout_seconds = timeout_seconds
        self.observe = observe
        self.queue: queue.Queue[_SpanData] = queue.Queue(maxsize=queue_size)
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="sites-otlp-exporter", daemon=True
        )
        self._thread.start()

    def submit(self, span: _SpanData) -> None:
        try:
            self.queue.put_nowait(span)
            self._observe("queued", 1)
        except queue.Full:
            self._observe("dropped_queue_full", 1)

    def _observe(self, outcome: str, amount: int) -> None:
        if self.observe:
            try:
                self.observe(outcome, amount)
            except Exception:
                pass

    @staticmethod
    def _attribute(value: Any) -> dict[str, Any]:
        if isinstance(value, bool):
            return {"boolValue": value}
        if isinstance(value, int):
            return {"intValue": str(value)}
        if isinstance(value, float):
            return {"doubleValue": value}
        return {"stringValue": str(value)[:1024]}

    def _payload(self, spans: list[_SpanData]) -> bytes:
        encoded = []
        for span in spans:
            attributes = [
                {"key": key, "value": self._attribute(value)}
                for key, value in sorted(span.attributes.items())
                if value is not None
            ]
            encoded.append(
                {
                    "traceId": span.trace_id,
                    "spanId": span.span_id,
                    **({"parentSpanId": span.parent_span_id} if span.parent_span_id else {}),
                    "name": span.name,
                    "kind": span.kind,
                    "startTimeUnixNano": str(span.start_ns),
                    "endTimeUnixNano": str(span.end_ns),
                    "attributes": attributes,
                    "status": {
                        "code": 2 if span.error else 1,
                        **({"message": span.error[:256]} if span.error else {}),
                    },
                }
            )
        body = {
            "resourceSpans": [{
                "resource": {"attributes": [{
                    "key": "service.name",
                    "value": {"stringValue": self.service},
                }]},
                "scopeSpans": [{
                    "scope": {"name": "site", "version": "1"},
                    "spans": encoded,
                }],
            }]
        }
        return json.dumps(body, separators=(",", ":")).encode("utf-8")

    def _export(self, spans: list[_SpanData]) -> None:
        request = urllib.request.Request(
            self.endpoint,
            data=self._payload(spans),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                if not 200 <= response.status < 300:
                    raise OSError(f"OTLP collector returned HTTP {response.status}")
            self._observe("exported", len(spans))
        except Exception:
            # No retry here: retrying makes an unavailable collector monopolize the one
            # worker and turns a bounded queue into a delayed outage. Prometheus records
            # the loss, while the request path remains unaffected.
            self._observe("dropped_export_failure", len(spans))

    def _run(self) -> None:
        while not self._stop.is_set() or not self.queue.empty():
            try:
                first = self.queue.get(
                    timeout=(
                        min(self.flush_seconds, 0.1)
                        if self._stop.is_set()
                        else self.flush_seconds
                    )
                )
            except queue.Empty:
                continue
            batch = [first]
            deadline = time.monotonic() + self.flush_seconds
            while len(batch) < self.batch_size:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    batch.append(
                        self.queue.get(timeout=min(remaining, 0.1))
                    )
                except queue.Empty:
                    if self._stop.is_set():
                        break
                    continue
            self._export(batch)

    def close(self, timeout: float = 5.0) -> None:
        """Drain queued spans without making process shutdown unbounded."""
        self._stop.set()
        self._thread.join(timeout=max(0.0, timeout))


_exporter: _Exporter | None = None
_exporter_lock = threading.Lock()


def configure(
    service: str,
    observe: Callable[[str, int], None] | None = None,
) -> bool:
    """Start the optional exporter once. Empty endpoint keeps tracing correlation-only."""
    global _exporter
    endpoint = (os.environ.get("SITES_OTLP_HTTP_ENDPOINT") or "").strip()
    if not endpoint:
        return False
    with _exporter_lock:
        if _exporter is None:
            try:
                flush = max(0.01, float(os.environ.get("SITES_OTLP_FLUSH_SECONDS", "1") or "1"))
                timeout = max(0.01, float(os.environ.get("SITES_OTLP_TIMEOUT_SECONDS", "2") or "2"))
            except ValueError:
                flush, timeout = 1.0, 2.0
            _exporter = _Exporter(
                endpoint,
                service,
                queue_size=_positive_int("SITES_OTLP_QUEUE_SIZE", 2048),
                batch_size=_positive_int("SITES_OTLP_BATCH_SIZE", 128),
                flush_seconds=flush,
                timeout_seconds=timeout,
                observe=observe,
            )
    return True


def shutdown(timeout: float = 5.0) -> None:
    """Best-effort bounded exporter drain for graceful process termination."""
    global _exporter
    with _exporter_lock:
        exporter, _exporter = _exporter, None
    if exporter is not None:
        exporter.close(timeout)


def parse_trace_context(value: str) -> tuple[str, str] | None:
    """``(trace-id, trace-flags)`` of a well-formed ``traceparent``, or None."""
    parts = (value or "").strip().split("-")
    if len(parts) != 4:
        return None
    version, trace_id, span_id, flags = parts
    if version != _VERSION:
        return None
    if not _TRACE_ID_RE.fullmatch(trace_id) or trace_id == _ZERO_TRACE_ID:
        return None
    if not _SPAN_ID_RE.fullmatch(span_id) or span_id == _ZERO_SPAN_ID:
        return None
    if not _FLAGS_RE.fullmatch(flags):
        return None
    return trace_id, flags


def parse_traceparent(value: str) -> str | None:
    """The trace-id of a well-formed ``traceparent``, or None for anything else."""
    context = parse_trace_context(value)
    return context[0] if context else None


def trace_id_from_request_id(request_id: str) -> str:
    """Derive a trace id from an ``X-Request-Id``, identically in every service.

    Deterministic on purpose: a caller that only speaks ``X-Request-Id`` still gets one
    trace id across all of them, so its logs join up without it having to know what
    ``traceparent`` is.
    """
    derived = hashlib.sha256(request_id.encode("utf-8")).digest()[:16].hex()
    # Structurally unreachable, and cheaper to handle than to argue about.
    return new_trace_id() if derived == _ZERO_TRACE_ID else derived


def new_trace_id() -> str:
    while True:
        candidate = secrets.token_bytes(16).hex()
        if candidate != _ZERO_TRACE_ID:
            return candidate


def new_span_id() -> str:
    while True:
        candidate = secrets.token_bytes(8).hex()
        if candidate != _ZERO_SPAN_ID:
            return candidate


def inbound_context(headers: Any) -> tuple[str, str]:
    """``(trace id, flags)`` for an inbound request, by the shared priority order.

    ``traceparent`` -> ``X-Request-Id`` -> locally generated. Never raises, never refuses.
    Flags are inherited only with a usable ``traceparent``; every other path is a trace
    this service started, and it samples its own.
    """
    inherited = parse_trace_context(headers.get(TRACEPARENT_HEADER, "") or "")
    if inherited:
        return inherited
    request_id = (headers.get(REQUEST_ID_HEADER, "") or "").strip()
    if request_id:
        return trace_id_from_request_id(request_id), _FLAGS_SAMPLED
    return new_trace_id(), _FLAGS_SAMPLED


def trace_id_for(headers: Any) -> str:
    """The trace id of an inbound request. See :func:`inbound_context`."""
    return inbound_context(headers)[0]


def outbound_traceparent(trace_id: str = "", flags: str = "") -> str:
    """A ``traceparent`` for an outbound call, parented to the active span."""
    resolved = trace_id or current_trace_id() or new_trace_id()
    parent = _current_span_id.get() or new_span_id()
    return f"{_VERSION}-{resolved}-{parent}-{flags or current_flags()}"


def outbound_headers(trace_id: str = "", flags: str = "") -> dict[str, str]:
    """Headers to merge into any downstream request."""
    return {TRACEPARENT_HEADER: outbound_traceparent(trace_id, flags)}


def current_trace_id() -> str:
    """The trace id bound to this context, or empty outside a traced scope."""
    return _current_trace_id.get()


def current_flags() -> str:
    """The sampling decision in force, inherited from the caller when there was one."""
    return _current_flags.get()


def bind(trace_id: str, flags: str = _FLAGS_SAMPLED) -> tuple:
    """Bind a trace context; pass the returned token to :func:`release`."""
    return (_current_trace_id.set(trace_id), _current_flags.set(flags))


def release(token: tuple) -> None:
    trace_token, flags_token = token
    _current_trace_id.reset(trace_token)
    _current_flags.reset(flags_token)


class Span:
    """Small synchronous span context manager using OTLP's numeric SpanKind values."""

    def __init__(
        self,
        name: str,
        *,
        kind: int = 1,
        attributes: dict[str, Any] | None = None,
        trace_id: str = "",
        parent_span_id: str = "",
        flags: str = "",
    ):
        self.name = name
        self.kind = kind
        self.attributes = dict(attributes or {})
        self.trace_id = trace_id or current_trace_id() or new_trace_id()
        self.flags = flags or current_flags()
        self.span_id = new_span_id()
        self.parent_span_id = parent_span_id or _current_span_id.get()
        self.error = ""
        self._started_ns = 0
        self._trace_token: tuple | None = None
        self._span_token: contextvars.Token[str] | None = None

    def __enter__(self) -> "Span":
        self._started_ns = time.time_ns()
        self._trace_token = bind(self.trace_id, self.flags)
        self._span_token = _current_span_id.set(self.span_id)
        return self

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def set_error(self, error: BaseException | str) -> None:
        self.error = type(error).__name__ if isinstance(error, BaseException) else str(error)

    def __exit__(self, exc_type: Any, exc: BaseException | None, _tb: Any) -> None:
        if exc is not None:
            self.set_error(exc)
        ended_ns = time.time_ns()
        if self._span_token is not None:
            _current_span_id.reset(self._span_token)
        if self._trace_token is not None:
            release(self._trace_token)
        exporter = _exporter
        if exporter is not None and self.flags == _FLAGS_SAMPLED:
            exporter.submit(
                _SpanData(
                    trace_id=self.trace_id,
                    span_id=self.span_id,
                    parent_span_id=self.parent_span_id,
                    name=self.name,
                    kind=self.kind,
                    start_ns=self._started_ns,
                    end_ns=ended_ns,
                    attributes=self.attributes,
                    error=self.error,
                )
            )


def span(
    name: str,
    *,
    kind: int = 1,
    attributes: dict[str, Any] | None = None,
    trace_id: str = "",
    parent_span_id: str = "",
    flags: str = "",
) -> Span:
    return Span(
        name,
        kind=kind,
        attributes=attributes,
        trace_id=trace_id,
        parent_span_id=parent_span_id,
        flags=flags,
    )


def inbound_parent_span_id(headers: Any) -> str:
    parts = (headers.get(TRACEPARENT_HEADER, "") or "").strip().split("-")
    return parts[2] if len(parts) == 4 and parse_trace_context("-".join(parts)) else ""


def traceparent_for_current() -> str:
    """A propagation carrier for an asynchronous resource, or empty outside a trace."""
    return outbound_traceparent() if current_trace_id() else ""


def traced_resource(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Trace a reconciler using ``sites.local/traceparent`` on its resource."""
    def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(function)
        def wrapped(self: Any, resource: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
            carrier = str(
                ((resource.get("metadata") or {}).get("annotations") or {}).get(
                    "sites.local/traceparent", ""
                )
            )
            parsed = parse_trace_context(carrier)
            trace_id, flags = parsed or (new_trace_id(), _FLAGS_SAMPLED)
            parts = carrier.split("-") if parsed else []
            parent = parts[2] if len(parts) == 4 else ""
            with span(
                name,
                attributes={"sites.operation": "reconcile"},
                trace_id=trace_id,
                parent_span_id=parent,
                flags=flags,
            ):
                return function(self, resource, *args, **kwargs)
        return wrapped
    return decorate
