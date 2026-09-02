"""Curated scaffold support catalog with executable contract evidence.

The evaluator deliberately does not claim that a framework was built or deployed when
only its bounded source/deployment contracts were exercised.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from sites.builds import normalize_source_payload
from sites.k8s_resources import deployment_resource
from sites.static_artifacts import normalize_static_artifact
from sites.validation import STATIC_IMAGE, normalize_artifact, normalize_deploy_payload
from sites.version_policy import normalize_version_policy


_DIGEST_IMAGE = "registry.example.invalid/scaffold@sha256:" + "a" * 64


@dataclass(frozen=True)
class Scaffold:
    scaffold_id: str
    framework: str
    site_type: str
    deployment_mode: str
    files: dict[str, str]
    port: int
    health_path: str
    database: str | None
    recommended_tool: str
    support_level: str
    limitations: tuple[str, ...]


_SCAFFOLDS = (
    Scaffold(
        "static-html",
        "HTML/CSS/JavaScript",
        "static",
        "static-object-storage",
        {"index.html": "<!doctype html><title>site</title><h1>ok</h1>"},
        8080,
        "/",
        None,
        "deploy_static_versioned",
        "contract-verified",
        (
            "Private S3/OSS upload and immutable runtime contracts are verified locally; live bucket, Kubernetes, and public URL verification require an E2E environment.",
        ),
    ),
    Scaffold(
        "vite-static",
        "Vite",
        "static",
        "dockerfile-source",
        {
            "Dockerfile": "FROM node:22-alpine\nWORKDIR /app\nCOPY . .\nRUN npm install && npm run build\nFROM nginxinc/nginx-unprivileged:1.29-alpine\nCOPY --from=0 /app/dist /usr/share/nginx/html\n",
            "package.json": '{"scripts":{"build":"vite build"},"devDependencies":{"vite":"latest"}}',
            "index.html": "<div id=\"app\"></div><script type=\"module\" src=\"/src.js\"></script>",
            "src.js": "document.querySelector('#app').textContent='ok'",
        },
        8080,
        "/",
        None,
        "source_deploy",
        "contract-verified",
        (
            "Package download, BuildKit build, registry push, and live URL verification require an E2E environment.",
            "Object-storage-native static publishing is not wired to this source-build path.",
        ),
    ),
    Scaffold(
        "fastapi-postgresql",
        "FastAPI",
        "dynamic",
        "dockerfile-source-then-dynamic-version",
        {
            "Dockerfile": "FROM python:3.13-slim\nWORKDIR /app\nCOPY requirements.txt .\nRUN pip install --no-cache-dir -r requirements.txt\nCOPY app.py .\nUSER 10001\nCMD [\"uvicorn\",\"app:app\",\"--host\",\"0.0.0.0\",\"--port\",\"8080\"]\n",
            "requirements.txt": "fastapi\nuvicorn\npsycopg[binary]\n",
            "app.py": "from fastapi import FastAPI\napp=FastAPI()\n@app.get('/healthz')\ndef health(): return {'ok': True}\n",
        },
        8080,
        "/healthz",
        "postgresql-schema",
        "deploy_dynamic",
        "contract-verified",
        (
            "The source build and immutable dynamic deployment are currently two steps; the image digest must be passed to deploy_dynamic.",
            "Framework startup, migration execution, and live deployment require an E2E environment.",
        ),
    ),
    Scaffold(
        "express-postgresql",
        "Express",
        "dynamic",
        "dockerfile-source-then-dynamic-version",
        {
            "Dockerfile": "FROM node:22-alpine\nWORKDIR /app\nCOPY package.json .\nRUN npm install --omit=dev\nCOPY server.js .\nUSER 10001\nCMD [\"node\",\"server.js\"]\n",
            "package.json": '{"scripts":{"start":"node server.js"},"dependencies":{"express":"latest","pg":"latest"}}',
            "server.js": "const express=require('express');const app=express();app.get('/healthz',(_,r)=>r.json({ok:true}));app.listen(8080,'0.0.0.0');\n",
        },
        8080,
        "/healthz",
        "postgresql-schema",
        "deploy_dynamic",
        "contract-verified",
        (
            "The source build and immutable dynamic deployment are currently two steps; the image digest must be passed to deploy_dynamic.",
            "Framework startup, migration execution, and live deployment require an E2E environment.",
        ),
    ),
)


def _run(name: str, check: Callable[[], None]) -> dict[str, str]:
    try:
        check()
    except Exception as exc:  # evaluation must report a failed contract, not break discovery
        return {"name": name, "status": "failed", "detail": str(exc)[:300]}
    return {"name": name, "status": "passed"}


def _required_files(scaffold: Scaffold) -> None:
    required = {"index.html"} if scaffold.site_type == "static" else {"Dockerfile"}
    missing = required - set(scaffold.files)
    if missing:
        raise ValueError("missing required files: " + ", ".join(sorted(missing)))


def _source_contract(scaffold: Scaffold) -> None:
    normalize_source_payload(
        {
            "name": scaffold.scaffold_id,
            "port": scaffold.port,
            "healthPath": scaffold.health_path,
            "files": scaffold.files,
        },
        "local",
        "scaffold-evaluator",
    )


def _deployment_contract(scaffold: Scaffold) -> None:
    payload: dict[str, Any] = {
        "name": scaffold.scaffold_id,
        "image": STATIC_IMAGE if scaffold.site_type == "static" else _DIGEST_IMAGE,
        "port": scaffold.port,
        "healthPath": scaffold.health_path,
        "scaleToZero": False,
    }
    if scaffold.deployment_mode == "static-inline":
        payload["artifact"] = {"files": normalize_artifact({"files": scaffold.files})["files"]}
    if scaffold.deployment_mode == "static-object-storage":
        artifact = normalize_static_artifact(
            "local", "scaffold-evaluator", scaffold.scaffold_id, scaffold.files
        )
        spec = normalize_deploy_payload(
            {**payload, "siteVersion": 1}, "local", "scaffold-evaluator"
        )
        spec["staticArtifact"] = {
            "sourcePath": artifact.source_path,
            "sha256": artifact.sha256,
        }
        deployment_resource(spec, "sites-scaffold-evaluator")
        return
    if scaffold.site_type == "dynamic":
        payload["siteVersion"] = 1
    normalize_deploy_payload(payload, "local", "scaffold-evaluator")


def _version_policy(scaffold: Scaffold) -> None:
    if scaffold.site_type != "dynamic":
        return
    normalized = normalize_version_policy(
        {
            "changeMode": "incremental",
            "databaseStrategy": "shared",
            "databaseCompatibility": "backward-compatible",
            "schemaChange": "none",
            "migrationStrategy": "none",
            "decisionRationale": "scaffold evaluator baseline",
        }
    )
    if normalized["databaseStrategy"] != "shared":
        raise ValueError("dynamic scaffold did not retain shared schema policy")


def scaffold_catalog() -> dict[str, Any]:
    """Evaluate and return framework support without performing external mutations."""
    profiles: list[dict[str, Any]] = []
    all_checks: list[dict[str, str]] = []
    for scaffold in _SCAFFOLDS:
        checks = [_run("required-files", lambda s=scaffold: _required_files(s))]
        if scaffold.deployment_mode not in {"static-inline", "static-object-storage"}:
            checks.append(_run("source-contract", lambda s=scaffold: _source_contract(s)))
        checks.append(_run("deployment-contract", lambda s=scaffold: _deployment_contract(s)))
        if scaffold.site_type == "dynamic":
            checks.append(_run("shared-schema-version-policy", lambda s=scaffold: _version_policy(s)))
        checks.append(
            {
                "name": "build-deploy-live-e2e",
                "status": "not-run",
                "detail": "Requires BuildKit/registry/Kubernetes and, for production static sites, S3/OSS.",
            }
        )
        all_checks.extend(checks)
        profiles.append(
            {
                "id": scaffold.scaffold_id,
                "framework": scaffold.framework,
                "siteType": scaffold.site_type,
                "deploymentMode": scaffold.deployment_mode,
                "database": scaffold.database,
                "port": scaffold.port,
                "healthPath": scaffold.health_path,
                "recommendedTool": scaffold.recommended_tool,
                "requiredFiles": sorted(scaffold.files),
                "supportLevel": scaffold.support_level,
                "limitations": list(scaffold.limitations),
                "evaluation": checks,
            }
        )
    passed = sum(check["status"] == "passed" for check in all_checks)
    failed = sum(check["status"] == "failed" for check in all_checks)
    not_run = sum(check["status"] == "not-run" for check in all_checks)
    executed = passed + failed
    return {
        "methodology": {
            "successRateName": "contractCheckSuccessRate",
            "denominator": "passed + failed executable local contract checks",
            "excludes": "not-run build/deploy/live E2E checks",
            "agentEndToEndSuccessRate": None,
        },
        "summary": {
            "profiles": len(profiles),
            "passed": passed,
            "failed": failed,
            "notRun": not_run,
            "contractCheckSuccessRate": round(passed / executed, 4) if executed else None,
        },
        "scaffolds": profiles,
    }
