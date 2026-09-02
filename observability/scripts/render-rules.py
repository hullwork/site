#!/usr/bin/env python3
"""Regenerate observability/alerts/sites-rules.yaml from the rendered Helm chart.

The bundled Prometheus loads its rules from a ConfigMap; an external Prometheus
wants a PrometheusRule. Two spellings of one fact, so only one of them is
written by hand - this script derives the other, and test_monitoring.py fails
when they disagree, which is what catches "edited the ConfigMap, forgot the
artifact".
"""
from __future__ import annotations

import pathlib
import subprocess

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
CHART = ROOT / "charts" / "site"
TARGET = ROOT / "observability" / "alerts" / "sites-rules.yaml"

HEADER = """# site alert rules, as an artifact any Prometheus can import.
#
# 🔴 GENERATED - do not edit by hand. The source of truth is the
# `sites-alerts.yml` key of the `sites-prometheus-config` ConfigMap in
# charts/site/templates/11-monitoring.yaml, which is what this repository's own bundled
# Prometheus loads. test_monitoring.py fails if the two drift, so an operator
# importing this file gets exactly the rules site runs against itself.
# Regenerate with: python3 observability/scripts/render-rules.py
#
# Job names: the expressions match `sites-(api|operator|activator).*` and
# `sites-envoy-gateway.*`, so both the bundled jobs (suffixed `-local`) and jobs
# named after the Services work. Keep any renaming inside those prefixes.
"""


def main() -> int:
    rendered = subprocess.run(
        [
            "helm", "template", "site", str(CHART),
            "--namespace", "sites-local",
            "--set", "monitoring.enabled=true",
        ],
        check=True, capture_output=True, text=True,
    ).stdout
    docs = [d for d in yaml.safe_load_all(rendered) if d]
    cm = next(
        d for d in docs
        if d["kind"] == "ConfigMap"
        and d["metadata"]["name"] == "sites-prometheus-config"
    )
    groups = yaml.safe_load(cm["data"]["sites-alerts.yml"])["groups"]
    rule = {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "PrometheusRule",
        "metadata": {
            "name": "sites-rules",
            "labels": {
                "app.kubernetes.io/name": "sites",
                "app.kubernetes.io/part-of": "sites",
            },
        },
        "spec": {"groups": groups},
    }
    TARGET.write_text(
        HEADER + yaml.safe_dump(rule, sort_keys=False, width=10_000),
        encoding="utf-8",
    )
    print(f"wrote {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
