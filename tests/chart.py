"""The rendered Helm chart, read the way the retired ``manifests/`` tree was.

``manifests/`` held a second, hand-maintained copy of every resource in
``charts/site/templates/``.  Nothing installed it -- there is no ``kubectl
apply`` target and the README documents pip plus ``scripts/standalone.sh`` --
and nothing read it at runtime; the only references to it left in ``src/sites``
were comments.  The two copies were held together by a checksum gate that
proved they had not *changed*, never that they still *agreed*, and they had in
fact drifted: the retired copy still pinned the control-plane Service to
``NodePort`` 30081 while the chart had moved to ``ClusterIP``.

So the assertions that used to inspect that copy now inspect ``helm template``
output.  That is a strictly stronger subject: it is the thing an install
actually creates, values substituted and conditionals resolved.

A missing ``helm`` raises rather than skipping.  Every caller here is the only
join between a cluster-side spelling and the code that assumes it; a skip would
retire all of them at once and still report green.
"""
from __future__ import annotations

import functools
import pathlib
import re
import shutil
import subprocess

import yaml


ROOT = pathlib.Path(__file__).resolve().parent.parent
CHART = ROOT / "charts" / "site"

# Every optional component turned on.  The chart is now the only copy of these
# resources, so rendering with a feature off would silently drop the resources
# an assertion is about and leave the assertion looking for something that was
# never emitted.
# The Pod CIDR the suite declares. It has no default in the chart on purpose, so
# every `helm template` call has to supply one; stating it here once keeps that
# requirement real while leaving the assertions a fixed value to reason about.
# ChartPodCidrExclusionTests deliberately renders with a *different* value as
# well, because pinning only this one cannot tell a rendered value from a
# literal that happens to match it.
TEST_POD_CIDR = "10.201.0.0/16"

FEATURE_VALUES = (
    "--set-string", f"clusterNetwork.podCIDR={TEST_POD_CIDR}",
    "--set", "gateway.enabled=true",
    "--set", "monitoring.enabled=true",
    "--set", "buildPlane.enabled=true",
    "--set", "localPathProvisioner.enabled=true",
    "--set", "postgresql.embedded.enabled=true",
    "--set", "tracing.enabled=true",
    "--set-string", "tracing.endpoint=http://collector.invalid:4318/v1/traces",
)

_SOURCE = re.compile(r"^# Source: site/templates/(\S+)", re.MULTILINE)


@functools.lru_cache(maxsize=None)
def render(*overrides: str) -> str:
    """``helm template`` with every optional component enabled.

    ``overrides`` are appended after :data:`FEATURE_VALUES`, so a caller can
    turn a feature back off or pin a value it is specifically asserting about.
    """
    if shutil.which("helm") is None:
        raise RuntimeError(
            "helm is required: it renders the only copy of the cluster "
            "resources these assertions check"
        )
    return subprocess.run(
        [
            "helm", "template", "site", str(CHART),
            "--namespace", "sites-local",
            *FEATURE_VALUES,
            *overrides,
        ],
        check=True, capture_output=True, text=True,
    ).stdout


def template(name: str, *overrides: str) -> str:
    """The rendered text of one template, e.g. ``08-gateway.yaml``.

    Keeping the per-template face means an assertion that named a manifest
    still names one file rather than searching the whole release, so a resource
    moving between templates stays visible instead of being absorbed.
    """
    chunks = [
        chunk for chunk in render(*overrides).split("\n---\n")
        if any(match == name for match in _SOURCE.findall(chunk))
    ]
    if not chunks:
        rendered = sorted(set(_SOURCE.findall(render(*overrides))))
        raise AssertionError(
            f"{name} rendered no documents; the release contains {rendered}"
        )
    return "\n---\n".join(chunks)


def documents(name: str, *overrides: str) -> list[dict]:
    """Every non-empty document one template renders to."""
    return [
        doc for doc in yaml.safe_load_all(template(name, *overrides)) if doc
    ]


def document(name: str, kind: str, metadata_name: str, *overrides: str) -> dict:
    """One rendered object, by kind and name.  Absence is an error, not a skip."""
    for doc in documents(name, *overrides):
        if doc["kind"] == kind and doc["metadata"]["name"] == metadata_name:
            return doc
    raise AssertionError(f"{kind}/{metadata_name} is not in {name}")
