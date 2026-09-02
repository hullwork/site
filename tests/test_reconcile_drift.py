"""Reconcile → _should_apply → _apply_workload wiring.

DriftResyncTest (test_telemetry) only exercises the ``_should_apply`` predicate. With the
predicate stubbed to a constant in ``reconcile`` those cases still pass, so nothing pinned
the actual wiring: "a converged site is reasserted after the drift window" and "a
converged site inside the window is left alone". These cases drive ``reconcile`` against a
fake apiserver that stores objects by path and answers 404 for anything missing, so a
deleted Deployment is visible as a deleted Deployment.
"""
from __future__ import annotations

import copy
import os
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from sites import operator as operator_module
from sites.kube import ApiError
from sites.operator import COLLECTION_PATH, Operator
from sites.validation import normalize_deploy_payload


@contextmanager
def using_backend(name: str):
    previous = os.environ.get("SITES_EXPOSURE_BACKEND")
    os.environ["SITES_EXPOSURE_BACKEND"] = name
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("SITES_EXPOSURE_BACKEND", None)
        else:
            os.environ["SITES_EXPOSURE_BACKEND"] = previous


def _merge(base: dict, patch_body: dict) -> dict:
    """JSON merge-patch, enough for metadata/status writes."""
    result = dict(base)
    for key, value in patch_body.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        elif value is None:
            result.pop(key, None)
        else:
            result[key] = value
    return result


class PathStoreKube:
    """Objects live at their API path; a missing path is a 404 like the real apiserver.

    A Deployment that is written gets a converged status attached, standing in for
    the Deployment controller, so a stored Deployment reads as ready and a removed
    one reads as gone.
    """

    def __init__(self) -> None:
        self.objects: dict[str, dict] = {}
        self.applied: list[str] = []
        self.deleted: list[str] = []

    def get(self, path: str) -> dict:
        if path in self.objects:
            return copy.deepcopy(self.objects[path])
        raise ApiError(404, f"{path} not found")

    def create(self, path: str, body: dict) -> dict:
        self.objects[f"{path}/{body['metadata']['name']}"] = copy.deepcopy(body)
        return body

    def create_or_patch(self, collection: str, path: str, body: dict) -> dict:
        stored = copy.deepcopy(body)
        if "/deployments/" in path:
            replicas = int((stored.get("spec") or {}).get("replicas", 1))
            stored.setdefault("metadata", {})["generation"] = 1
            stored["status"] = {
                "observedGeneration": 1,
                "updatedReplicas": replicas,
                "availableReplicas": replicas,
                "unavailableReplicas": 0,
            }
        self.objects[path] = stored
        self.applied.append(path)
        return body

    def patch(self, path: str, body: dict) -> dict:
        target = path[: -len("/status")] if path.endswith("/status") else path
        if target not in self.objects:
            raise ApiError(404, f"{target} not found")
        self.objects[target] = _merge(self.objects[target], body)
        return self.objects[target]

    def delete(self, path: str, body: dict | None = None) -> dict:
        if path not in self.objects:
            raise ApiError(404, f"{path} not found")
        self.deleted.append(path)
        return self.objects.pop(path)


class ReconcileDriftWiringTest(unittest.TestCase):
    NAME = "local-local-demo-abcdef0123456789"

    def setUp(self) -> None:
        with using_backend("gateway"):
            spec = normalize_deploy_payload(
                {
                    "name": "demo",
                    "image": "example.invalid/demo:v1",
                    "port": 8080,
                    "healthPath": "/healthz",
                },
                "local",
                "local",
            )
        self.kube = PathStoreKube()
        self.cr_path = f"{COLLECTION_PATH}/{self.NAME}"
        self.kube.objects[self.cr_path] = {
            "metadata": {"name": self.NAME, "generation": 1},
            "spec": spec,
        }
        self.operator = Operator(self.kube)
        # No network: verification is not the subject here.
        patcher = patch.object(Operator, "_verification_for", return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _sweep(self) -> None:
        with using_backend("gateway"):
            self.operator.reconcile(self.kube.get(self.cr_path))

    def _status(self) -> dict:
        return self.kube.objects[self.cr_path].get("status") or {}

    def _deployment_paths(self) -> list[str]:
        return [path for path in self.kube.applied if "/deployments/" in path]

    def _converge(self) -> str:
        """First sweep builds the workload; second sweep sees it Running and starts the window."""
        self._sweep()
        self.assertEqual(self._status().get("phase"), "Running")
        self.assertTrue(self._status().get("ready"))
        deployment_path = self._deployment_paths()[0]
        self.assertIn(deployment_path, self.kube.objects)
        # First settled sighting still applies once and records the timestamp.
        self._sweep()
        self.assertIn(self.NAME, self.operator._applied_at)
        return deployment_path

    def test_deleted_deployment_is_rebuilt_after_the_drift_window(self) -> None:
        """Self-healing survives the drift resync: a Deployment that vanished comes back."""
        deployment_path = self._converge()
        self.kube.objects.pop(deployment_path)
        applies_before = len(self._deployment_paths())
        self.operator._applied_at[self.NAME] -= operator_module.DRIFT_RESYNC_SECONDS + 1
        self._sweep()
        self.assertIn(deployment_path, self.kube.objects, "Deployment must be recreated")
        self.assertEqual(len(self._deployment_paths()), applies_before + 1)
        self.assertEqual(self._status().get("phase"), "Running")

    def test_converged_site_inside_the_window_is_not_reasserted(self) -> None:
        """Downscaling, not skipping: inside the window a settled site costs no writes."""
        self._converge()
        applies_before = len(self.kube.applied)
        self._sweep()
        self._sweep()
        self.assertEqual(
            len(self.kube.applied), applies_before, "no apply while the window is open"
        )
        self.assertEqual(self._status().get("phase"), "Running")

    def test_unsettled_site_is_applied_every_sweep(self) -> None:
        """A site that has not converged keeps getting the desired state every sweep."""
        self._sweep()
        applies_before = len(self.kube.applied)
        # Simulate the apiserver reporting the CR as still deploying.
        self.kube.objects[self.cr_path]["status"] = {
            **self._status(), "phase": "Deploying", "ready": False,
        }
        self._sweep()
        self.assertGreater(len(self.kube.applied), applies_before)


if __name__ == "__main__":
    unittest.main()
