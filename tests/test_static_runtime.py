"""Kubernetes runtime contract for immutable static S3/OSS artifacts."""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from tests import chart

from sites.k8s_resources import (
    STATIC_ARTIFACT_AUTH_MOUNT,
    deployment_resource,
    static_artifact_secret_name,
    static_artifact_secret_resource,
    network_policy_resources,
)
from sites.operator import CONTROL_NAMESPACE, Operator
from sites.naming import namespace_for_tenant
from sites.validation import STATIC_IMAGE, STATIC_SITE_ROOT, ValidationError


_DIGEST = "a" * 64


def _spec() -> dict:
    return {
        "merchantID": "local",
        "userID": "alice",
        "serviceName": "docs",
        "image": "caller.invalid/must-not-run:latest",
        "port": 8080,
        "healthPath": "/",
        "livenessPath": "/",
        "exposure": "internal",
        "revision": "7",
        "staticArtifact": {
            "sourcePath": f"sources/local/alice/{_DIGEST}",
            "sha256": _DIGEST,
            "controlSecretName": "control-oss",
        },
    }


class StaticArtifactResourceTests(unittest.TestCase):
    def test_inline_artifact_uses_the_same_fixed_static_runtime(self) -> None:
        spec = _spec()
        spec.pop("staticArtifact")
        spec["artifact"] = {
            "files": {"index.html": "<h1>hello</h1>"},
            "sha256": _DIGEST,
        }
        deployment = deployment_resource(spec, "ulocal-alice")
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        pod_security = deployment["spec"]["template"]["spec"]["securityContext"]

        self.assertEqual(STATIC_IMAGE, container["image"])
        self.assertEqual(101, pod_security["runAsUser"])

    def test_nginx_reads_materialized_content_without_oss_credentials(self) -> None:
        deployment = deployment_resource(_spec(), "ulocal-alice")
        pod = deployment["spec"]["template"]["spec"]
        main = pod["containers"][0]
        init = pod["initContainers"][0]

        self.assertEqual(main["image"], STATIC_IMAGE)
        self.assertNotEqual(main["image"], _spec()["image"])
        self.assertEqual(init["name"], "fetch-static-artifact")
        self.assertEqual(init["command"], ["python3", "-m", "sites.static_artifacts"])
        self.assertEqual(
            init["args"],
            [
                "--source-path",
                _spec()["staticArtifact"]["sourcePath"],
                "--destination",
                STATIC_SITE_ROOT,
            ],
        )
        self.assertTrue(init["securityContext"]["readOnlyRootFilesystem"])

        main_mounts = {item["name"]: item for item in main["volumeMounts"]}
        self.assertTrue(main_mounts["static-artifact"]["readOnly"])
        self.assertEqual(main_mounts["static-artifact"]["mountPath"], STATIC_SITE_ROOT)
        self.assertNotIn("static-artifact-oss-auth", main_mounts)
        self.assertFalse(
            any(item["name"].startswith("SITES_OSS_") for item in main["env"])
        )

        init_mounts = {item["name"]: item for item in init["volumeMounts"]}
        self.assertEqual(
            init_mounts["static-artifact-oss-auth"]["mountPath"],
            STATIC_ARTIFACT_AUTH_MOUNT,
        )
        volumes = {item["name"]: item for item in pod["volumes"]}
        self.assertIn("emptyDir", volumes["static-artifact"])
        self.assertEqual(
            set(volumes["static-artifact-oss-auth"]["secret"]["items"][0]),
            {"key", "path"},
        )
        self.assertEqual(
            deployment["spec"]["template"]["metadata"]["annotations"][
                "sites.local/static-artifact-sha256"
            ],
            _DIGEST,
        )

    def test_source_path_and_digest_are_bound(self) -> None:
        bad = _spec()
        bad["staticArtifact"] = {
            **bad["staticArtifact"],
            "sourcePath": f"sources/local/alice/{'b' * 64}",
        }
        with self.assertRaisesRegex(ValidationError, "must end with"):
            deployment_resource(bad, "ulocal-alice")

    def test_private_oss_egress_is_exact_https_and_static_only(self) -> None:
        with mock.patch(
            "sites.k8s_resources.STATIC_ARTIFACT_EGRESS_CIDRS",
            ("10.20.30.0/24",),
        ):
            static_egress = network_policy_resources(
                _spec(), "ulocal-alice"
            )[1]["spec"]["egress"]
            dynamic_egress = network_policy_resources(
                {**_spec(), "staticArtifact": None}, "ulocal-alice"
            )[1]["spec"]["egress"]
        self.assertEqual(
            static_egress[-1],
            {
                "to": [{"ipBlock": {"cidr": "10.20.30.0/24"}}],
                "ports": [{"protocol": "TCP", "port": 443}],
            },
        )
        self.assertEqual(len(static_egress), len(dynamic_egress) + 1)

    def test_inline_and_object_artifacts_cannot_be_combined(self) -> None:
        with self.assertRaisesRegex(ValidationError, "mutually exclusive"):
            deployment_resource(
                {**_spec(), "artifact": {"files": {"index.html": "x"}, "sha256": _DIGEST}},
                "ulocal-alice",
            )

    def test_tenant_secret_contains_only_downloader_keys(self) -> None:
        control = {
            "data": {
                "access-key-id": "aWQ=",
                "access-key-secret": "c2VjcmV0",
                "unrelated-admin-token": "bm8=",
            }
        }
        resource = static_artifact_secret_resource(
            _spec(), "ulocal-alice", control
        )
        self.assertEqual(
            set(resource["data"]), {"access-key-id", "access-key-secret"}
        )
        self.assertNotEqual(resource["metadata"]["name"], "control-oss")
        self.assertEqual(
            resource["metadata"]["name"], static_artifact_secret_name(_spec())
        )

    def test_incomplete_control_secret_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "incomplete"):
            static_artifact_secret_resource(
                _spec(), "ulocal-alice", {"data": {"access-key-id": "aWQ="}}
            )


class _FakeKube:
    def __init__(self) -> None:
        self.created: list[tuple[str, str, dict]] = []
        self.read: list[str] = []

    def create_or_patch(self, collection: str, path: str, body: dict) -> dict:
        self.created.append((collection, path, body))
        return body

    def get(self, path: str) -> dict:
        self.read.append(path)
        if path.endswith("/secrets/control-oss"):
            return {
                "data": {
                    "access-key-id": "aWQ=",
                    "access-key-secret": "c2VjcmV0",
                    "control-only": "bm8=",
                }
            }
        raise AssertionError(f"unexpected read: {path}")

    def delete(self, _path: str) -> None:
        return None

    def patch(self, _path: str, _body: dict) -> None:
        return None


class StaticArtifactOperatorTests(unittest.TestCase):
    def test_operator_copies_secret_before_creating_deployment(self) -> None:
        kube = _FakeKube()
        Operator(kube)._apply_workload(_spec(), "ulocal-alice")
        self.assertEqual(
            kube.read,
            [f"/api/v1/namespaces/{CONTROL_NAMESPACE}/secrets/control-oss"],
        )
        writes = [body for _collection, _path, body in kube.created]
        secret_index = next(
            index for index, body in enumerate(writes) if body.get("kind") == "Secret"
        )
        deployment_index = next(
            index
            for index, body in enumerate(writes)
            if body.get("kind") == "Deployment"
        )
        self.assertLess(secret_index, deployment_index)
        copied = writes[secret_index]
        self.assertEqual(
            set(copied["data"]), {"access-key-id", "access-key-secret"}
        )

    def test_cleanup_removes_the_derived_tenant_secret(self) -> None:
        kube = _FakeKube()
        deleted: list[str] = []
        kube.delete = deleted.append  # type: ignore[method-assign]
        spec = _spec()
        Operator(kube)._cleanup(
            {
                "metadata": {
                    "name": "local-alice-docs-0123456789abcdef",
                    "finalizers": ["sites.local/local-cleanup"],
                },
                "spec": spec,
            }
        )
        self.assertIn(
            f"/api/v1/namespaces/{namespace_for_tenant('local', 'alice')}/secrets/"
            f"{static_artifact_secret_name(spec)}",
            deleted,
        )

    def test_crd_and_operator_manifest_declare_the_runtime_contract(self) -> None:
        crd = chart.template("00-platform.yaml")
        operator = chart.template("10-control-plane.yaml")
        self.assertIn("                staticArtifact:\n", crd)
        self.assertIn("                    - sourcePath\n", crd)
        self.assertIn("SITES_STATIC_ARTIFACT_DOWNLOADER_IMAGE", operator)
        self.assertIn("SITES_STATIC_ARTIFACT_CONTROL_SECRET", operator)


if __name__ == "__main__":
    unittest.main()
