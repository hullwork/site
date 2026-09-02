#!/usr/bin/env python3
"""Run a declared site benchmark profile and emit fail-closed evidence."""
from __future__ import annotations

import argparse
import importlib.util
import json
import platform
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "evaluation" / "benchmark-spec.v1.json"
THRESHOLDS_PATH = ROOT / "evaluation" / "thresholds.v1.json"
EVALUATOR_PATH = ROOT / "scripts" / "evaluate-scaffolds.py"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def command_version(command: str, *args: str) -> str | None:
    try:
        result = subprocess.run(
            [command, *args], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (result.stdout or result.stderr).strip().splitlines()
    return text[0][:300] if text else None


def environment() -> dict[str, Any]:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    return {
        "gitCommit": commit,
        "gitDirty": dirty,
        "python": sys.version.splitlines()[0],
        "platform": platform.platform(),
        "tools": {
            "docker": command_version("docker", "--version"),
            "helm": command_version("helm", "version", "--short"),
            "kubectl": command_version("kubectl", "version", "--client"),
        },
    }


def evaluator() -> Any:
    spec = importlib.util.spec_from_file_location("site_scaffold_evaluator", EVALUATOR_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load scaffold evaluator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def score_contract(raw: dict[str, Any], stage_spec: dict[str, list[str]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    profiles = {profile["id"]: profile for profile in raw["scaffolds"]}
    scored: list[dict[str, Any]] = []
    scored_keys: set[tuple[str, str]] = set()
    for profile_id, expected_stages in stage_spec.items():
        actual = {stage["stage"]: stage for stage in profiles.get(profile_id, {}).get("stages", [])}
        for stage_name in expected_stages:
            item = dict(actual.get(stage_name, {"stage": stage_name, "status": "not-run", "detail": "declared scored stage was missing"}))
            item["profile"] = profile_id
            scored.append(item)
            scored_keys.add((profile_id, stage_name))
    unscored = [
        {**stage, "profile": profile["id"]}
        for profile in raw["scaffolds"]
        for stage in profile["stages"]
        if (profile["id"], stage["stage"]) not in scored_keys
    ]
    return scored, unscored


def summarize(stages: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {
        "passed": sum(stage["status"] == "passed" for stage in stages),
        "failed": sum(stage["status"] == "failed" for stage in stages),
        "blocked": sum(stage["status"] == "blocked" for stage in stages),
        "notRun": sum(stage["status"] == "not-run" for stage in stages),
    }
    total = len(stages)
    counts["successRate"] = round(counts["passed"] / total, 4) if total else 0.0
    return counts


def accepted(summary: dict[str, Any], thresholds: dict[str, Any]) -> bool:
    return (
        summary["successRate"] >= thresholds["minimumSuccessRate"]
        and summary["failed"] <= thresholds["maximumFailed"]
        and summary["blocked"] <= thresholds["maximumBlocked"]
        and summary["notRun"] <= thresholds["maximumNotRun"]
    )


def run_contract() -> dict[str, Any]:
    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    thresholds_document = json.loads(THRESHOLDS_PATH.read_text(encoding="utf-8"))
    started = now()
    raw = evaluator().evaluate(allow_container_builds=False)
    scored, unscored = score_contract(raw, spec["profiles"]["contract"]["scoredStages"])
    summary = summarize(scored)
    thresholds = thresholds_document["profiles"]["contract"]
    passed = accepted(summary, thresholds)
    return {
        "schemaVersion": 1,
        "benchmarkVersion": spec["benchmarkVersion"],
        "runId": str(uuid.uuid4()),
        "profile": "contract",
        "startedAt": started,
        "finishedAt": now(),
        "environment": environment(),
        "thresholds": thresholds,
        "summary": summary,
        "scoredStages": scored,
        "unscoredStages": unscored,
        "valid": True,
        "passed": passed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=["contract", "cluster"], required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="print a cluster execution plan without contacting Kubernetes")
    parser.add_argument("--context", default="", help="explicit isolated kubectl context for the cluster profile")
    parser.add_argument("--namespace", default="", help="new benchmark-only namespace")
    parser.add_argument("--trials", type=int, default=60)
    parser.add_argument("--control-image", default="", help="release control image pinned by sha256")
    parser.add_argument("--dynamic-image", default="", help="HTTP fixture image pinned by sha256")
    parser.add_argument("--timeout-seconds", type=int, default=600)
    args = parser.parse_args()
    if args.profile == "contract":
        if args.dry_run or any((args.context, args.namespace, args.control_image, args.dynamic_image)):
            parser.error("cluster-only options cannot be used with the contract profile")
        result = run_contract()
    else:
        from cluster_benchmark import ClusterConfig, plan, run

        missing = [name for name, value in (
            ("--context", args.context), ("--namespace", args.namespace),
            ("--control-image", args.control_image), ("--dynamic-image", args.dynamic_image),
        ) if not value]
        if missing:
            parser.error("cluster profile requires " + ", ".join(missing))
        config = ClusterConfig(
            context=args.context, namespace=args.namespace, trials=args.trials,
            control_image=args.control_image, dynamic_image=args.dynamic_image,
            timeout_seconds=args.timeout_seconds,
        )
        result = plan(config) if args.dry_run else run(config)
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0 if args.dry_run or result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
