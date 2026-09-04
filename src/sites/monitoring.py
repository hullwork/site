"""Bounded Prometheus queries for the admin monitoring console.

The browser never receives PromQL and cannot submit arbitrary queries.  Keeping the
query catalogue here makes the data boundary reviewable and prevents the admin API
from becoming a general-purpose Prometheus proxy.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sites import exposure
from sites.naming import namespace_for_tenant
from sites.validation import dns_label, normalize_merchant_id, normalize_user_id


PROMETHEUS_URL = os.getenv(
    "SITES_PROMETHEUS_URL", "http://sites-prometheus.sites-local.svc:9090"
).rstrip("/")
RANGES = {
    "1h": (60 * 60, 60),
    "6h": (6 * 60 * 60, 5 * 60),
    "24h": (24 * 60 * 60, 15 * 60),
}
MAX_PROMETHEUS_BYTES = 4 * 1024 * 1024


class MonitoringError(RuntimeError):
    """The metrics backend did not return a usable Prometheus response."""


def _range(value: str | None) -> tuple[str, int, int]:
    key = (value or "1h").strip()
    if key not in RANGES:
        raise ValueError("range must be one of: 1h, 6h, 24h")
    seconds, step = RANGES[key]
    return key, seconds, step


def _query_range(query: str, start: int, end: int, step: int) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode(
        {"query": query, "start": start, "end": end, "step": step}
    )
    request = urllib.request.Request(
        f"{PROMETHEUS_URL}/api/v1/query_range?{params}",
        headers={"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=4.0) as response:
            raw = response.read(MAX_PROMETHEUS_BYTES + 1)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise MonitoringError("metrics backend unavailable") from exc
    if len(raw) > MAX_PROMETHEUS_BYTES:
        raise MonitoringError("metrics backend response is too large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MonitoringError("metrics backend returned invalid JSON") from exc
    if payload.get("status") != "success":
        raise MonitoringError("metrics backend rejected the query")
    result = ((payload.get("data") or {}).get("result") or [])
    if not isinstance(result, list):
        raise MonitoringError("metrics backend returned an invalid response")
    points: dict[int, float] = {}
    for series in result:
        for raw_timestamp, raw_value in series.get("values") or []:
            try:
                timestamp = int(float(raw_timestamp))
                value = float(raw_value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                points[timestamp] = points.get(timestamp, 0.0) + value
    return [
        {"timestamp": timestamp, "value": round(value, 6)}
        for timestamp, value in sorted(points.items())
    ]


def _series_catalog(scope: str, namespace: str = "", service: str = "") -> list[dict[str, str]]:
    window = "[2m]"
    if scope == "cluster":
        container_filter = 'job="sites-kubelet-cadvisor",container!="",image!=""'
        route_filter = 'job="sites-envoy-gateway-local",envoy_cluster_name=~"httproute/.+"'
    else:
        container_filter = (
            f'job="sites-kubelet-cadvisor",namespace="{namespace}",'
            f'pod=~"{service}-.*",container!="",image!=""'
        )
        route_filter = (
            'job="sites-envoy-gateway-local",'
            f'envoy_cluster_name=~"httproute/{namespace}/{service}/.*"'
        )
    catalog = [
        {
            "id": "cpu",
            "label": "CPU usage",
            "unit": "cores",
            "query": f"sum(rate(container_cpu_usage_seconds_total{{{container_filter}}}{window}))",
        },
        {
            "id": "memory",
            "label": "memory usage",
            "unit": "bytes",
            "query": f"sum(container_memory_working_set_bytes{{{container_filter}}})",
        },
        {
            "id": "requests",
            "label": "Request rate",
            "unit": "req/s",
            "query": f"sum(rate(envoy_cluster_upstream_rq_completed{{{route_filter}}}{window}))",
        },
        {
            "id": "errors",
            "label": "4xx / 5xx error rate",
            "unit": "percent",
            "query": (
                "100 * (sum(rate(envoy_cluster_upstream_rq_xx{"
                f"{route_filter},envoy_response_code_class=~\"4|5\"}}{window})) "
                "or vector(0)) "
                "/ clamp_min(sum(rate(envoy_cluster_upstream_rq_completed{"
                f"{route_filter}}}{window})), 0.000001)"
            ),
        },
        {
            "id": "latencyP95",
            "label": "P95 response time",
            "unit": "ms",
            "query": (
                "histogram_quantile(0.95, sum by (le) "
                f"(rate(envoy_cluster_upstream_rq_time_bucket{{{route_filter}}}{window})))"
            ),
        },
    ]
    if scope == "cluster":
        catalog.extend(
            [
                {
                    "id": "cpuCapacity",
                    "label": "CPU capacity",
                    "unit": "cores",
                    "query": 'sum(machine_cpu_cores{job="sites-kubelet-cadvisor"})',
                },
                {
                    "id": "memoryCapacity",
                    "label": "Memory capacity",
                    "unit": "bytes",
                    "query": 'sum(machine_memory_bytes{job="sites-kubelet-cadvisor"})',
                },
            ]
        )
    return catalog


def _monitoring_response(
    scope: str,
    range_key: str | None,
    *,
    namespace: str = "",
    service: str = "",
    identity: dict[str, str] | None = None,
) -> dict[str, Any]:
    key, seconds, step = _range(range_key)
    end = int(time.time())
    start = end - seconds
    traffic_available = exposure.backend().name == "gateway"
    catalog = [
        item
        for item in _series_catalog(scope, namespace, service)
        if traffic_available or item["id"] not in {"requests", "errors", "latencyP95"}
    ]
    base: dict[str, Any] = {
        "scope": scope,
        "range": {"key": key, "start": start, "end": end, "stepSeconds": step},
        "source": {
            "available": True,
            "sampledAt": dt.datetime.fromtimestamp(end, dt.timezone.utc).isoformat(),
            "retention": "24h",
            "trafficAvailable": traffic_available,
            "trafficReason": (
                None
                if traffic_available
                else "traffic metrics require gateway exposure"
            ),
        },
        "identity": identity,
    }
    try:
        with ThreadPoolExecutor(max_workers=len(catalog)) as executor:
            points = list(
                executor.map(
                    lambda item: _query_range(item["query"], start, end, step), catalog
                )
            )
    except MonitoringError:
        base["source"] = {
            "available": False,
            "sampledAt": base["source"]["sampledAt"],
            "retention": "24h",
            "error": "metrics backend unavailable",
            "trafficAvailable": traffic_available,
            "trafficReason": base["source"]["trafficReason"],
        }
        base["summary"] = {}
        base["series"] = []
        return base
    series = [
        {"id": item["id"], "label": item["label"], "unit": item["unit"], "points": values}
        for item, values in zip(catalog, points)
    ]
    base["summary"] = {
        item["id"]: (values[-1]["value"] if values else None)
        for item, values in zip(catalog, points)
    }
    base["series"] = series
    return base


def cluster_metrics(range_key: str | None) -> dict[str, Any]:
    return _monitoring_response("cluster", range_key)


def application_metrics(
    merchant_id: str, user_id: str, service_name: str, range_key: str | None
) -> dict[str, Any]:
    merchant = normalize_merchant_id(merchant_id)
    user = normalize_user_id(user_id)
    service = dns_label(service_name)
    namespace = namespace_for_tenant(merchant, user)
    return _monitoring_response(
        "application",
        range_key,
        namespace=namespace,
        service=service,
        identity={"merchantId": merchant, "userId": user, "serviceName": service},
    )
