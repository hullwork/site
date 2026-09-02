#!/usr/bin/env python3
"""Run bounded scaffold evaluations and emit machine-readable evidence.

The default lane only uses a temporary directory and localhost. Container image
builds may download dependencies and mutate the local container runtime, so they
must be enabled explicitly with ``--allow-container-builds``.
"""
from __future__ import annotations

import argparse
import functools
import http.server
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from sites.scaffolds import (  # noqa: E402
    _SCAFFOLDS,
    _deployment_contract,
    _required_files,
    _source_contract,
    _version_policy,
)


Result = dict[str, Any]


def _result(
    stage: str,
    status: str,
    started: float,
    detail: str | None = None,
) -> Result:
    item: Result = {
        "stage": stage,
        "status": status,
        "durationMs": round((time.monotonic() - started) * 1000, 3),
    }
    if detail:
        item["detail"] = detail[:1000]
    return item


def _run(stage: str, operation: Callable[[], None]) -> Result:
    started = time.monotonic()
    try:
        operation()
    except Exception as exc:  # evidence should preserve failures across profiles
        return _result(stage, "failed", started, f"{type(exc).__name__}: {exc}")
    return _result(stage, "passed", started)


def _materialize(scaffold: Any, directory: Path) -> None:
    _required_files(scaffold)
    for relative_name, contents in scaffold.files.items():
        target = directory / relative_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8")


@contextmanager
def _static_server(directory: Path) -> Iterator[str]:
    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

    handler = functools.partial(QuietHandler, directory=str(directory))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(
        target=server.serve_forever,
        name="site-scaffold-evaluator",
        daemon=True,
    )
    thread.start()
    try:
        port = int(server.server_address[1])
        yield f"http://127.0.0.1:{port}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _fetch_ok(url: str, expected: bytes = b"ok", timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    last_error = "no response"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                body = response.read()
                if response.status == 200 and expected in body:
                    return
                last_error = f"HTTP {response.status}: expected marker not found"
        except (OSError, urllib.error.URLError) as exc:
            last_error = str(exc)
        time.sleep(0.1)
    raise RuntimeError(f"health check failed for {url}: {last_error}")


def _localhost_static_smoke(directory: Path) -> None:
    with _static_server(directory) as url:
        _fetch_ok(url)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _command(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _container_e2e(scaffold: Any, directory: Path) -> list[Result]:
    docker = shutil.which("docker")
    if docker is None:
        now = time.monotonic()
        return [
            _result("container-build", "blocked", now, "docker CLI is unavailable"),
            _result("container-runtime-health", "blocked", now, "container-build did not run"),
        ]

    tag = f"site-scaffold-eval-{scaffold.scaffold_id}:{uuid.uuid4().hex[:12]}"
    build_started = time.monotonic()
    try:
        build = _command([docker, "build", "--tag", tag, str(directory)], timeout=900)
    except subprocess.TimeoutExpired:
        return [
            _result("container-build", "failed", build_started, "container build timed out after 900 seconds"),
            _result("container-runtime-health", "blocked", time.monotonic(), "container-build failed"),
        ]
    if build.returncode != 0:
        detail = (build.stderr or build.stdout or "container build failed")[-1000:]
        return [
            _result("container-build", "failed", build_started, detail),
            _result("container-runtime-health", "blocked", time.monotonic(), "container-build failed"),
        ]

    results = [_result("container-build", "passed", build_started)]
    container_name = f"site-scaffold-eval-{uuid.uuid4().hex[:12]}"
    host_port = _free_port()
    runtime_started = time.monotonic()
    try:
        run = _command(
            [
                docker,
                "run",
                "--detach",
                "--name",
                container_name,
                "--publish",
                f"127.0.0.1:{host_port}:{scaffold.port}",
                "--env",
                "PGHOST=127.0.0.1",
                "--env",
                "PGPORT=5432",
                "--env",
                "PGDATABASE=sites",
                "--env",
                "PGUSER=site_runtime",
                "--env",
                "PGPASSWORD=evaluation-only",
                "--env",
                "PGSSLMODE=disable",
                "--env",
                "SITES_DATABASE_SCHEMA=site_evaluation",
                tag,
            ],
            timeout=60,
        )
        if run.returncode != 0:
            raise RuntimeError((run.stderr or run.stdout or "container failed to start")[-1000:])
        _fetch_ok(f"http://127.0.0.1:{host_port}{scaffold.health_path}", timeout=30)
    except Exception as exc:
        results.append(_result("container-runtime-health", "failed", runtime_started, str(exc)))
    else:
        results.append(_result("container-runtime-health", "passed", runtime_started))
    finally:
        _command([docker, "rm", "--force", container_name], timeout=30)
        _command([docker, "image", "rm", "--force", tag], timeout=60)
    return results


def evaluate(*, allow_container_builds: bool = False) -> dict[str, Any]:
    profiles: list[dict[str, Any]] = []
    all_results: list[Result] = []
    container_profiles_attempted = 0
    container_profiles_passed = 0

    with tempfile.TemporaryDirectory(prefix="site-scaffold-eval-") as temp:
        root = Path(temp)
        for scaffold in _SCAFFOLDS:
            directory = root / scaffold.scaffold_id
            directory.mkdir()
            results = [_run("source-generation", lambda s=scaffold, d=directory: _materialize(s, d))]
            if scaffold.deployment_mode not in {"static-inline", "static-object-storage"}:
                results.append(_run("source-contract", lambda s=scaffold: _source_contract(s)))
            results.append(_run("deployment-contract", lambda s=scaffold: _deployment_contract(s)))
            if scaffold.site_type == "dynamic":
                results.append(_run("shared-schema-version-policy", lambda s=scaffold: _version_policy(s)))

            if scaffold.scaffold_id == "static-html":
                results.append(_run("localhost-runtime-smoke", lambda d=directory: _localhost_static_smoke(d)))
                results.append(
                    _result(
                        "production-deploy-live-e2e",
                        "not-run",
                        time.monotonic(),
                        "Requires an explicitly configured S3/OSS and deployment environment.",
                    )
                )
            elif allow_container_builds:
                container_profiles_attempted += 1
                container_results = _container_e2e(scaffold, directory)
                results.extend(container_results)
                if all(item["status"] == "passed" for item in container_results):
                    container_profiles_passed += 1
                results.append(
                    _result(
                        "cluster-deploy-live-e2e",
                        "not-run",
                        time.monotonic(),
                        "Container smoke does not deploy to Kubernetes or production storage.",
                    )
                )
            else:
                results.extend(
                    [
                        _result(
                            "container-build",
                            "not-run",
                            time.monotonic(),
                            "Pass --allow-container-builds; builds may pull dependencies and mutate the local container runtime.",
                        ),
                        _result(
                            "container-runtime-health",
                            "not-run",
                            time.monotonic(),
                            "container-build was not enabled",
                        ),
                        _result(
                            "cluster-deploy-live-e2e",
                            "not-run",
                            time.monotonic(),
                            "Requires a separately configured cluster E2E lane.",
                        ),
                    ]
                )
            all_results.extend(results)
            profiles.append({"id": scaffold.scaffold_id, "stages": results})

    counts = {
        status: sum(item["status"] == status for item in all_results)
        for status in ("passed", "failed", "not-run", "blocked")
    }
    executed = counts["passed"] + counts["failed"]
    return {
        "methodology": {
            "localCheckSuccessRateDenominator": "passed + failed local and contract stages",
            "scaffoldContainerSuccessRateDenominator": "profiles whose container E2E was explicitly attempted",
            "agentEndToEndSuccessRate": None,
            "agentRateReason": "This evaluator measures scaffold pipeline stages, not autonomous Agent task outcomes.",
        },
        "options": {"allowContainerBuilds": allow_container_builds},
        "summary": {
            **counts,
            "localCheckSuccessRate": round(counts["passed"] / executed, 4) if executed else None,
            "scaffoldContainerSuccessRate": (
                round(container_profiles_passed / container_profiles_attempted, 4)
                if container_profiles_attempted
                else None
            ),
        },
        "scaffolds": profiles,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--allow-container-builds",
        action="store_true",
        help="allow local Docker builds/runs, including dependency downloads",
    )
    parser.add_argument("--output", type=Path, help="write JSON evidence to this path")
    args = parser.parse_args(argv)
    evidence = evaluate(allow_container_builds=args.allow_container_builds)
    rendered = json.dumps(evidence, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    if evidence["summary"]["failed"]:
        return 1
    if args.allow_container_builds and evidence["summary"]["blocked"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
