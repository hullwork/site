"""Structured logging and metrics with zero third-party dependencies.

Exports consistent logs and metrics for sites-api and sites-operator. Collection, disk
retention, and alerting belong to Loki, Prometheus, and related infrastructure.

Logs always go to stderr: stdio MCP reserves stdout for JSON-RPC frames. Logging calls
flush explicitly so file-directed stderr remains useful during incident diagnosis. Metrics
are in-process and dependency-free; api and operator each own a registry and are
distinguished by scrape job rather than shared global state.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from typing import Any, Iterable

from sites import tracing

# Service name. fall to this value before configure(), so that even if a certain path forgets to configure,
# Empty service fields will not appear in the logs (empty fields will cause log queries to silently miss these rows).
_service = "sites"

# text = human readable, json = machine readable. The default text keeps the existing local development experience unchanged;
# For production, set SITES_LOG_FORMAT=json in the Deployment and let Loki/ES parse it.
_LOG_FORMAT = (os.environ.get("SITES_LOG_FORMAT") or "text").strip().lower()

_LEVELS = ("debug", "info", "warn", "error")
_MIN_LEVEL = (os.environ.get("SITES_LOG_LEVEL") or "info").strip().lower()
if _MIN_LEVEL not in _LEVELS:
    _MIN_LEVEL = "info"


def configure(service: str) -> None:
    """Note down the service name of the current process for use in logs and metric labels."""
    global _service
    if service:
        _service = service


def _enabled(level: str) -> bool:
    try:
        return _LEVELS.index(level) >= _LEVELS.index(_MIN_LEVEL)
    except ValueError:
        return True


def log(event: str, level: str = "info", **fields: Any) -> None:
    """Keep a structured journal.

    `event` is a stable machine-readable event name (such as `reconcile_failed`), not a sentence for people to read——
    Sentences are placed in the `message` field. Only when the event name is stable can you create alarms and panels based on it in the log system;
    Spelling variable content into the event name is equivalent to generating a new event name every time an error occurs, and the aggregation will never be aligned.
    """
    if not _enabled(level):
        return
    record: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
        "level": level,
        "service": _service,
        "event": event,
    }
    # The one field that makes these lines joinable with another service's. Omitted rather
    # than emitted empty outside a traced scope (startup, background sweeps): an empty
    # value would match every log query for "requests with no trace".
    trace_id = tracing.current_trace_id()
    if trace_id:
        record["trace_id"] = trace_id
    for key, value in fields.items():
        if value is not None:
            record[key] = value
    if _LOG_FORMAT == "json":
        line = json.dumps(record, ensure_ascii=False, default=str)
    else:
        extra = " ".join(
            f"{k}={v}"
            for k, v in record.items()
            if k not in ("ts", "level", "service", "event")
        )
        line = f"[{_service}] {level.upper()} {event}"
        if extra:
            line = f"{line} {extra}"
    # `sys.stderr` must be evaluated when called and cannot be cached to module-level variables: when the resident process starts
    # Will use safe_stdout.install() to replace sys.stderr with a non-blocking substitute (the log pipe is full
    # to drop logs instead of freezing the thread, see the safe_stdout module with the bug on 2026-08-17). cache
    # A reference will bypass that layer of protection, and silently bypass it - the log will be output as usual, only when the pipeline
    # Only when it is really full will it be exposed in the form of "threads collectively stuck".
    print(line, file=sys.stderr, flush=True)


# /readyz and /healthz answer without a credential, so anything that reaches
# their bodies is readable by anything that can open a TCP connection to the
# port. A driver message can carry the address of the thing that failed - psycopg
# answers a refused connection with `connection to server at "10.0.0.5", port
# 5432 failed` and an unresolvable name with `failed to resolve host
# 'pg.internal'` - which is topology disclosure, not diagnosis. The class of
# failure is what the endpoint is for; the address belongs in the log, which
# already needs cluster access to read.
_REDACTED = "<redacted>"
_ENDPOINT_PATTERNS = (
    re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE),   # scheme://host/...
    re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"),                # bare IPv4
    re.compile(r"\b(?:[A-Za-z0-9_-]+\.)+[A-Za-z]{2,}\b"),       # dotted hostname
)
_MAX_ERROR_CHARS = 200


def redact_endpoints(message: str) -> str:
    """Strip host-shaped substrings from text served on an unauthenticated endpoint.

    Over-redaction is the safe direction: losing a dotted token from an error
    sentence costs a word, keeping one hands out an address. The exception type
    travels in its own field and is never redacted, so a reader still learns
    which class of failure happened.

    Truncation stays as well as redaction: a message carrying a whole DSN or SQL
    statement should not be walkable out one probe at a time.
    """
    for pattern in _ENDPOINT_PATTERNS:
        message = pattern.sub(_REDACTED, message)
    if len(message) > _MAX_ERROR_CHARS:
        message = message[:_MAX_ERROR_CHARS] + "..."
    return message


def log_exception(event: str, exc: BaseException, **fields: Any) -> None:
    """Unified form of failure path: exception type and message are divided into two fields.

    Types are separated into fields so that they can be aggregated - messages often contain high-cardinality content such as CR names/paths.
    Aggregation by message will break up the same type of fault into thousands of independent entries.
    """
    log(
        event,
        level="error",
        error_type=type(exc).__name__,
        error=str(exc),
        **fields,
    )


class _Metric:
    """Metric base class: name, help text, and values bucketed by label combination."""

    kind = "untyped"

    def __init__(self, name: str, help_text: str, labels: tuple[str, ...] = ()):
        self.name = name
        self.help_text = help_text
        self.labels = labels
        self._values: dict[tuple[str, ...], float] = {}
        self._lock = threading.Lock()

    def _key(self, label_values: tuple[str, ...]) -> tuple[str, ...]:
        if len(label_values) != len(self.labels):
            raise ValueError(
                f"{self.name} expects {len(self.labels)} label(s), "
                f"got {len(label_values)}"
            )
        return label_values

    def _render_labels(self, key: tuple[str, ...]) -> str:
        if not self.labels:
            return ""
        pairs = ",".join(
            f'{name}="{_escape(value)}"' for name, value in zip(self.labels, key)
        )
        return "{" + pairs + "}"

    def samples(self) -> Iterable[tuple[str, float]]:
        with self._lock:
            items = sorted(self._values.items())
        for key, value in items:
            yield self.name + self._render_labels(key), value


class Counter(_Metric):
    """Monotonically increasing counter."""

    kind = "counter"

    def inc(self, *label_values: str, amount: float = 1.0) -> None:
        key = self._key(label_values)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def ensure(self, *label_values: str) -> None:
        """Explicitly register a tag combination as 0.

        The counter only appears on the first occurrence, so "Never failed" and "Metric not connected" on the crawler side
        They look exactly the same - no difference can be seen when the alarm is written as `rate(...failed) > 0`, so write
        `absent(...)` will falsely report instances that really never fail. Known tag combinations at startup
        ensure once, let 0 be a real observation value.
        """
        key = self._key(label_values)
        with self._lock:
            self._values.setdefault(key, 0.0)


class Gauge(_Metric):
    """Instantaneous value that can be increased or decreased."""

    kind = "gauge"

    def set(self, value: float, *label_values: str) -> None:
        key = self._key(label_values)
        with self._lock:
            self._values[key] = float(value)


class Histogram(_Metric):
    """Fixed bucket histogram, only exposing the _bucket/_sum/_count trio."""

    kind = "histogram"

    def __init__(
        self,
        name: str,
        help_text: str,
        buckets: tuple[float, ...],
        labels: tuple[str, ...] = (),
    ):
        super().__init__(name, help_text, labels)
        self.buckets = tuple(sorted(buckets))
        self._counts: dict[tuple[str, ...], list[int]] = {}
        self._sums: dict[tuple[str, ...], float] = {}
        self._totals: dict[tuple[str, ...], int] = {}

    def observe(self, value: float, *label_values: str) -> None:
        key = self._key(label_values)
        with self._lock:
            counts = self._counts.setdefault(key, [0] * len(self.buckets))
            for index, bound in enumerate(self.buckets):
                if value <= bound:
                    counts[index] += 1
            self._sums[key] = self._sums.get(key, 0.0) + value
            self._totals[key] = self._totals.get(key, 0) + 1

    def samples(self) -> Iterable[tuple[str, float]]:
        with self._lock:
            keys = sorted(self._totals)
            counts = {k: list(self._counts[k]) for k in keys}
            sums = dict(self._sums)
            totals = dict(self._totals)
        for key in keys:
            base_labels = list(zip(self.labels, key))
            for index, bound in enumerate(self.buckets):
                pairs = base_labels + [("le", _format_float(bound))]
                rendered = ",".join(
                    f'{name}="{_escape(value)}"' for name, value in pairs
                )
                yield f"{self.name}_bucket{{{rendered}}}", counts[key][index]
            pairs = base_labels + [("le", "+Inf")]
            rendered = ",".join(f'{name}="{_escape(value)}"' for name, value in pairs)
            yield f"{self.name}_bucket{{{rendered}}}", totals[key]
            suffix = self._render_labels(key)
            yield f"{self.name}_sum{suffix}", sums[key]
            yield f"{self.name}_count{suffix}", totals[key]


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_float(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return repr(value)


class Registry:
    """A collection of metrics within the process."""

    def __init__(self) -> None:
        self._metrics: list[_Metric] = []
        self._lock = threading.Lock()

    def register(self, metric: _Metric) -> Any:
        with self._lock:
            self._metrics.append(metric)
        return metric

    def counter(
        self, name: str, help_text: str, labels: tuple[str, ...] = ()
    ) -> Counter:
        return self.register(Counter(name, help_text, labels))

    def gauge(self, name: str, help_text: str, labels: tuple[str, ...] = ()) -> Gauge:
        return self.register(Gauge(name, help_text, labels))

    def histogram(
        self,
        name: str,
        help_text: str,
        buckets: tuple[float, ...],
        labels: tuple[str, ...] = (),
    ) -> Histogram:
        return self.register(Histogram(name, help_text, buckets, labels))

    def render(self) -> str:
        """Render to Prometheus text format.

        Metrics without any samples still output two lines of HELP/TYPE - this allows the crawler to differentiate
        "This metric exists but there is no data yet" and "This metric does not exist at all in this version".
        """
        lines: list[str] = []
        with self._lock:
            metrics = list(self._metrics)
        for metric in metrics:
            lines.append(f"# HELP {metric.name} {metric.help_text}")
            lines.append(f"# TYPE {metric.name} {metric.kind}")
            for sample_name, value in metric.samples():
                lines.append(f"{sample_name} {_format_value(value)}")
        lines.append("")
        return "\n".join(lines)


def _format_value(value: float) -> str:
    if isinstance(value, int) or value == int(value):
        return str(int(value))
    return f"{value:.6f}".rstrip("0").rstrip(".")


class Timer:
    """`with Timer() as t:` then reads `t.seconds`."""

    __slots__ = ("started", "seconds")

    def __enter__(self) -> "Timer":
        self.started = time.monotonic()
        self.seconds = 0.0
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.seconds = time.monotonic() - self.started


# Default delay bucket, seconds. The upper bound is 60 because both reconciliation and construction can take minutes.
# When the top of the bucket is lower than the true distribution, p99 will be pressed on the last bucket, seemingly always "stuck just at the upper bound".
DEFAULT_LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)
