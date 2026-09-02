from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import chart

from sites.builds import (
    SOURCE_MAX_TOTAL_BYTES,
    _source_destination,
    build_job_name,
    build_job_resource,
    build_metadata_digest,
    build_metadata_subpath,
    immutable_image,
    normalize_source_payload,
    persist_source,
    prepare_build_metadata,
    remove_build_metadata,
    remove_source,
    site_build_resource,
    site_build_response,
)
from sites.kube import ApiError
from sites.operator import BUILD_FINALIZER, JOB_COLLECTION_PATH, Operator
from sites.k8s_resources import workload_egress_except_cidrs
from sites.validation import ValidationError


def _bundle(files: dict[str, str] | None = None):
    return normalize_source_payload(
        {
            "name": "dynamic-web",
            "port": 8080,
            "healthPath": "/healthz",
            "files": files
            or {
                "Dockerfile": "FROM python:3.13-alpine\nCMD [\"python\",\"app.py\"]\n",
                "app.py": "print('ok')\n",
                "static/app.css": "body{}\n",
            },
        },
        "local",
        "local",
    )


def _build():
    bundle = _bundle()
    build = site_build_resource(
        bundle,
        bundle.source_path,
        namespace="sites-local",
        revision="123",
        node_port=30082,
    )
    build["metadata"].update({"generation": 1, "finalizers": []})
    return build


class BuildPlaneEgressTests(unittest.TestCase):
    """The builder runs tenant code, so it needs the tenant workload's exclusions.

    🔴 The build-plane policy carried a hand-written except list holding only the
    two cluster CIDRs. Missing 169.254.0.0/16 meant a Dockerfile could
    `RUN curl http://169.254.169.254/...` and read the node's cloud credentials,
    while the workload policy 30 lines away in k8s_resources.py excluded it and
    said so in a comment. Two copies of one list, one of them wrong."""

    def test_the_build_plane_excludes_everything_the_workload_policy_excludes(
        self,
    ) -> None:
        manifest = chart.template("07-build-plane.yaml")
        #Read the except entries straight out of the rendered YAML rather than
        #importing a constant, so a manifest edited by hand still gets caught.
        #The file holds more than one 0.0.0.0/0 rule, so anchor on the builder
        #policy document first - splitting on the CIDR alone parsed the wrong one.
        documents = [
            d
            for d in manifest.split("\n---\n")
            if "kind: NetworkPolicy" in d and "\n  name: sites-builder\n" in d
        ]
        self.assertEqual(
            len(documents), 1, "expected exactly one sites-builder NetworkPolicy"
        )
        block = documents[0].split("cidr: 0.0.0.0/0", 1)
        self.assertEqual(len(block), 2, "build plane has no 0.0.0.0/0 egress rule")
        declared = set()
        for line in block[1].splitlines():
            stripped = line.strip()
            if stripped.startswith("- ") and "/" in stripped:
                declared.add(stripped[2:].strip())
            elif stripped.startswith("ports:"):
                break
        self.assertTrue(declared, "the except list is empty")
        missing = sorted(set(workload_egress_except_cidrs()) - declared)
        self.assertEqual(
            missing,
            [],
            f"build plane egress is missing exclusions the workload policy has: {missing}",
        )


class SourceValidationTests(unittest.TestCase):
    def test_sitebuild_crd_declares_build_only(self) -> None:
        manifest = chart.template("00-platform.yaml")
        sitebuild = manifest[manifest.index("kind: SiteBuild") :]
        self.assertIn("                buildOnly:\n                  type: boolean", sitebuild)

    def test_gateway_build_resource_omits_node_port(self) -> None:
        resource = site_build_resource(
            _bundle(),
            "local/local/dynamic-web/source",
            namespace="sites-local",
            revision="123",
            node_port=None,
        )
        self.assertNotIn("nodePort", resource["spec"])

    def test_nested_utf8_context_is_content_addressed(self) -> None:
        first = _bundle()
        second = _bundle(dict(reversed(list(first.files.items()))))
        self.assertEqual(first.sha256, second.sha256)
        self.assertEqual(first.source_path.split("/")[-1], first.sha256)

    def test_two_merchants_sharing_a_user_and_service_stay_separated(self) -> None:
        # The user_id is only unique within the merchant, so (user, service) is not enough to locate a build. less
        # In the merchant segment, tenants of two merchants with the same name will share the source code tree, registry repository and CR name——
        # Whoever deletes the build first will clear the other party's source code and image together.
        payload = {
            "name": "web",
            "files": {"Dockerfile": "FROM scratch\n"},
        }
        first = normalize_source_payload(payload, "acme", "alice")
        second = normalize_source_payload(payload, "globex", "alice")
        self.assertEqual(first.sha256, second.sha256)
        self.assertNotEqual(first.source_path, second.source_path)
        self.assertNotEqual(first.repository, second.repository)
        self.assertNotEqual(
            site_build_resource(
                first, first.source_path, namespace="sites-local",
                revision="1", node_port=30082,
            )["metadata"]["name"],
            site_build_resource(
                second, second.source_path, namespace="sites-local",
                revision="1", node_port=30082,
            )["metadata"]["name"],
        )

    def test_requires_root_dockerfile_and_rejects_secret_paths(self) -> None:
        with self.assertRaisesRegex(ValidationError, "Dockerfile"):
            _bundle({"src/app.py": "print('x')"})
        for name in (".env", ".env.production", "src/.git/config"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValidationError, "not allowed"):
                    _bundle({"Dockerfile": "FROM scratch\n", name: "secret"})

    def test_rejects_external_frontend_and_oversized_context(self) -> None:
        with self.assertRaisesRegex(ValidationError, "syntax frontends"):
            _bundle({"Dockerfile": "# syntax=example.invalid/frontend:latest\n"})
        with self.assertRaisesRegex(ValidationError, "source exceeds"):
            _bundle(
                {
                    "Dockerfile": "FROM scratch\n",
                    "one.txt": "x" * (SOURCE_MAX_TOTAL_BYTES // 2),
                    "two.txt": "x" * (SOURCE_MAX_TOTAL_BYTES // 2),
                    "three.txt": "x",
                }
            )

    def test_rejects_a_path_used_as_both_a_file_and_a_directory(self) -> None:
        # Each name is valid on its own, but persisting them writes the file
        # "app" and then tries to mkdir it. That used to surface as a 502
        # echoing the absolute path of the sources PVC.
        with self.assertRaisesRegex(ValidationError, "file and a directory"):
            _bundle(
                {
                    "Dockerfile": "FROM scratch\n",
                    "app": "x",
                    "app/main.py": "y",
                }
            )

    def test_persistence_is_atomic_and_removable(self) -> None:
        bundle = _bundle()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_path = persist_source(bundle, root)
            destination = root / source_path
            self.assertEqual(
                (destination / "static/app.css").read_text(), "body{}\n"
            )
            self.assertEqual((destination / "app.py").stat().st_mode & 0o777, 0o444)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o755)
            self.assertEqual(persist_source(bundle, root), source_path)
            remove_source(source_path, root)
            self.assertFalse(destination.exists())


class BuildJobContractTests(unittest.TestCase):
    def test_job_has_no_host_or_kubernetes_credentials(self) -> None:
        job = build_job_resource(_build(), namespace="sites-local")
        pod = job["spec"]["template"]["spec"]
        container = pod["containers"][0]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertNotIn("hostPath", str(pod["volumes"]))
        self.assertNotIn("privileged", container["securityContext"])
        self.assertTrue(container["securityContext"]["allowPrivilegeEscalation"])
        self.assertTrue(any(mount.get("readOnly") for mount in container["volumeMounts"]))
        self.assertEqual(job["spec"]["backoffLimit"], 0)
        self.assertEqual(job["spec"]["activeDeadlineSeconds"], 300)

    def test_job_reports_its_own_digest_onto_the_build_volume(self) -> None:
        build = _build()
        container = build_job_resource(build, namespace="sites-local")[
            "spec"
        ]["template"]["spec"]["containers"][0]
        self.assertIn("--metadata-file", container["args"])
        metadata_file = container["args"][
            container["args"].index("--metadata-file") + 1
        ]
        writable = [
            mount
            for mount in container["volumeMounts"]
            if mount["name"] == "source" and not mount.get("readOnly")
        ]
        self.assertEqual(len(writable), 1)
        self.assertEqual(writable[0]["subPath"], build_metadata_subpath(build))
        self.assertTrue(metadata_file.startswith(f"{writable[0]['mountPath']}/"))

    def test_only_valid_digest_becomes_a_runtime_image(self) -> None:
        digest = "sha256:" + "a" * 64
        self.assertEqual(
            immutable_image("local/local/web", digest),
            f"localhost:5000/local/local/web@{digest}",
        )
        with self.assertRaisesRegex(ValidationError, "digest"):
            immutable_image("local/local/web", "latest")


class BuildMetadataTests(unittest.TestCase):
    def test_digest_is_read_from_what_the_builder_wrote(self) -> None:
        build = _build()
        subpath = build_metadata_subpath(build)
        digest = "sha256:" + "e" * 64
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepare_build_metadata(subpath, root)
            # The Job runs under a different unprivileged UID than the control
            # plane, so it can only report anything if this leaf is writable.
            self.assertEqual((root / subpath).stat().st_mode & 0o777, 0o777)
            written = root / subpath / "metadata.json"
            written.write_text(json.dumps({"containerimage.digest": digest}))
            self.assertEqual(build_metadata_digest(subpath, root), digest)
            written.write_text(json.dumps({"containerimage.digest": "latest"}))
            with self.assertRaisesRegex(RuntimeError, "invalid image digest"):
                build_metadata_digest(subpath, root)
            remove_build_metadata(subpath, root)
            with self.assertRaisesRegex(RuntimeError, "did not report"):
                build_metadata_digest(subpath, root)

    def test_metadata_directory_is_not_addressable_as_a_source_path(self) -> None:
        # Both live on the same PVC; only the shape of the path keeps an
        # uploaded sourcePath from pointing at another build's digest record.
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValidationError):
                _source_destination(
                    build_metadata_subpath(_build()), Path(directory)
                )


class FakeBuildKube:
    """A real API server will return 404 for a job that does not exist, and this stub must also return it.

    In the past, it returned self.job unconditionally for any /jobs/ path, so in reconcile_build
    The three branches of "Create a job if it is no longer available", "Clean it if the build fails" and "Still under construction" are structurally unreachable.
    The coverage is zero - and the first branch is the one where 409 is written as the termination status Failed.
    """

    def __init__(self, build: dict, job: dict | None) -> None:
        self.build = build
        self.job = job
        self.patched: list[tuple[str, dict]] = []
        self.created: list[tuple[str, dict]] = []
        self.deleted: list[tuple[str, dict | None]] = []
        # The next time create throws the error first. Whoever creates a Job with the same name between GET and POST, put that
        # Job is put into conflicting_job, and the party that subsequently GETs it again can get it back.
        self.create_error: ApiError | None = None
        self.conflicting_job: dict | None = None
        self.site_deployment = {
            "metadata": {"name": build["metadata"]["name"]},
            "spec": {"serviceName": build["spec"]["serviceName"]},
            "status": {
                "phase": "Running",
                "ready": True,
                "message": "Deployment rollout completed",
                "url": "http://127.0.0.1:18090",
                "verification": {
                    "ok": True,
                    "httpStatus": 200,
                    "bodySha256": "f" * 64,
                },
            },
        }

    def get(self, path: str) -> dict:
        if "/jobs/" in path:
            if self.job is None:
                raise ApiError(404, f'jobs.batch "{path.rsplit("/", 1)[-1]}" not found')
            return self.job
        if "/sitedeployments/" in path:
            return self.site_deployment
        raise ApiError(404, "not found")

    def create(self, path: str, body: dict) -> dict:
        self.created.append((path, body))
        error, self.create_error = self.create_error, None
        if error is not None:
            self.job = self.conflicting_job
            raise error
        if path == JOB_COLLECTION_PATH:
            self.job = body
        return body

    def patch(self, path: str, body: dict) -> dict:
        self.patched.append((path, body))
        return body

    def create_or_patch(self, collection: str, path: str, body: dict) -> dict:
        self.site_deployment = {**body, "status": self.site_deployment["status"]}
        return self.site_deployment

    def delete(self, path: str, body: dict | None = None) -> dict:
        self.deleted.append((path, body))
        return {}


class BuildOperatorTests(unittest.TestCase):
    def test_deleting_build_only_keeps_the_versioned_registry_digest(self) -> None:
        build = _build()
        build["metadata"]["finalizers"] = [BUILD_FINALIZER]
        build["spec"]["buildOnly"] = True
        build["status"] = {"imageDigest": "sha256:" + "d" * 64}
        kube = FakeBuildKube(build, None)
        with (
            patch("sites.operator.remove_source"),
            patch("sites.operator.remove_build_metadata"),
            patch("sites.operator.delete_registry_manifest") as delete_digest,
        ):
            Operator(kube)._cleanup_build(build)
        delete_digest.assert_not_called()
        self.assertEqual(kube.patched[-1][1], {"metadata": {"finalizers": []}})

    def test_gateway_build_deploys_without_a_node_port(self) -> None:
        build = _build()
        build["metadata"]["finalizers"] = [BUILD_FINALIZER]
        del build["spec"]["nodePort"]
        job = {
            "status": {
                "conditions": [{"type": "Complete", "status": "True"}]
            }
        }
        kube = FakeBuildKube(build, job)
        digest = "sha256:" + "b" * 64
        with (
            patch.dict(os.environ, {"SITES_EXPOSURE_BACKEND": "gateway"}),
            patch("sites.operator.build_metadata_digest", return_value=digest),
            patch("sites.operator.remove_source"),
            patch("sites.builds.urllib.request.urlopen"),
        ):
            Operator(kube).reconcile_build(build)
        self.assertIn(f"@{digest}", kube.site_deployment["spec"]["image"])
        self.assertNotIn("nodePort", kube.site_deployment["spec"])
        self.assertEqual(self._statuses(kube)[-1]["phase"], "Running")

    def test_completed_job_deploys_the_digest_the_builder_reported(self) -> None:
        build = _build()
        build["metadata"]["finalizers"] = [BUILD_FINALIZER]
        job = {
            "status": {
                "conditions": [{"type": "Complete", "status": "True"}]
            }
        }
        kube = FakeBuildKube(build, job)
        digest = "sha256:" + "b" * 64
        with (
            patch("sites.operator.build_metadata_digest", return_value=digest),
            patch("sites.operator.remove_source"),
            # Asking the unauthenticated registry what the tag resolves to is
            # what let another writer swap the image between push and lookup.
            patch(
                "sites.builds.urllib.request.urlopen",
                side_effect=AssertionError("the registry must not be consulted"),
            ),
        ):
            Operator(kube).reconcile_build(build)
        self.assertIn(f"@{digest}", kube.site_deployment["spec"]["image"])
        self.assertEqual(kube.site_deployment["spec"]["nodePort"], 30082)
        # When the build is handed over to deployment, the merchant segment must follow suit: if it is lost, the SiteDeployment will be rejected by the CRD.
        # The failure point is after an expensive build, and the troubleshooting direction cannot be directed back here.
        self.assertEqual(
            kube.site_deployment["spec"]["merchantID"], build["spec"]["merchantID"]
        )
        statuses = [body["status"] for path, body in kube.patched if path.endswith("/status")]
        self.assertEqual(statuses[-1]["phase"], "Running")
        self.assertEqual(statuses[-1]["imageDigest"], digest)
        self.assertEqual(
            statuses[-1]["verification"],
            kube.site_deployment["status"]["verification"],
        )

    def test_build_only_returns_a_verified_digest_without_a_deployment(self) -> None:
        build = _build()
        build["metadata"]["finalizers"] = [BUILD_FINALIZER]
        build["spec"]["buildOnly"] = True
        job = {"status": {"conditions": [{"type": "Complete", "status": "True"}]}}
        kube = FakeBuildKube(build, job)
        digest = "sha256:" + "c" * 64
        with (
            patch("sites.operator.build_metadata_digest", return_value=digest),
            patch("sites.operator.remove_source"),
        ):
            Operator(kube).reconcile_build(build)
        self.assertEqual(
            kube.site_deployment["spec"],
            {"serviceName": build["spec"]["serviceName"]},
        )
        status = self._statuses(kube)[-1]
        self.assertEqual(status["phase"], "Running")
        self.assertTrue(status["ready"])
        self.assertEqual(status["imageDigest"], digest)
        self.assertEqual(status["verification"]["revision"], build["spec"]["revision"])
        self.assertEqual(status["verification"]["kind"], "registry-digest")

    def test_build_response_exposes_result_verification(self) -> None:
        build = _build()
        build["status"] = {
            "phase": "Running",
            "ready": True,
            "verification": {"ok": True, "httpStatus": 200},
        }
        self.assertEqual(
            site_build_response(build)["verification"],
            {"ok": True, "httpStatus": 200},
        )

    @staticmethod
    def _statuses(kube: FakeBuildKube) -> list[dict]:
        return [
            body["status"]
            for path, body in kube.patched
            if path.endswith("/status")
        ]

    def test_a_missing_job_is_created_after_its_metadata_drop_point(self) -> None:
        build = _build()
        build["metadata"]["finalizers"] = [BUILD_FINALIZER]
        kube = FakeBuildKube(build, None)
        prepared: list[tuple[str, int]] = []
        with patch(
            "sites.operator.prepare_build_metadata",
            # The drop point must exist before the Job: otherwise, kubelet will change the subPath for it.
            # The directory is created, root 0755, but the unprivileged UID of the builder cannot be written into it.
            side_effect=lambda subpath: prepared.append(
                (subpath, len(kube.created))
            ),
        ):
            Operator(kube).reconcile_build(build)
        self.assertEqual(prepared, [(build_metadata_subpath(build), 0)])
        self.assertEqual([path for path, _ in kube.created], [JOB_COLLECTION_PATH])
        self.assertEqual(
            kube.created[0][1]["metadata"]["name"], build_job_name(build)
        )
        self.assertEqual(self._statuses(kube)[-1]["phase"], "Building")

    def test_a_job_another_replica_already_created_is_not_a_failure(self) -> None:
        """A Job with the same name already exists and cannot be written as Failed - Failed is the termination state in the CRD.

        Two reachable paths: When the control plane is rolled out, both copies read 404, each is built once, and the second copy gets 409;
        Or the newly deleted build is immediately retried using the same source code, and the old job is still terminated in the background. naked create
        This will cause 409 to appear in run_once as Failed, and the caller will give up accordingly - and two seconds later
        That build actually worked.
        """
        build = _build()
        build["metadata"]["finalizers"] = [BUILD_FINALIZER]
        kube = FakeBuildKube(build, None)
        kube.create_error = ApiError(
            409, f'jobs.batch "{build_job_name(build)}" already exists'
        )
        kube.conflicting_job = {"metadata": {"name": build_job_name(build)}}
        with patch("sites.operator.prepare_build_metadata"):
            Operator(kube).reconcile_build(build)
        status = self._statuses(kube)[-1]
        self.assertEqual(status["phase"], "Building")
        # Pin the original text instead of just assertNotIn: only assert "no already exists", an empty line
        # message can also make use cases green.
        self.assertEqual(status["message"], "Waiting for the bounded BuildKit Job")

    def test_a_conflicting_job_that_already_finished_still_deploys(self) -> None:
        # Taking the "Exists" branch means that you can read the status of that Job: it has been completed, this round
        # The baton should be handed over directly to the deployment, rather than waiting for the next round.
        build = _build()
        build["metadata"]["finalizers"] = [BUILD_FINALIZER]
        kube = FakeBuildKube(build, None)
        kube.create_error = ApiError(409, "already exists")
        kube.conflicting_job = {
            "status": {"conditions": [{"type": "Complete", "status": "True"}]}
        }
        digest = "sha256:" + "c" * 64
        with (
            patch("sites.operator.prepare_build_metadata"),
            patch("sites.operator.build_metadata_digest", return_value=digest),
            patch("sites.operator.remove_source"),
        ):
            Operator(kube).reconcile_build(build)
        self.assertIn(f"@{digest}", kube.site_deployment["spec"]["image"])
        self.assertEqual(self._statuses(kube)[-1]["phase"], "Running")

    def test_a_job_that_vanishes_after_a_conflict_is_retried_not_failed(self) -> None:
        # 409 and then GET again and 404: The Job with the same name disappeared between these two calls. Can’t read this round
        # The state is left to the next round of reconstruction - a race state should not become a terminated state.
        build = _build()
        build["metadata"]["finalizers"] = [BUILD_FINALIZER]
        kube = FakeBuildKube(build, None)
        kube.create_error = ApiError(409, "already exists")
        kube.conflicting_job = None
        with patch("sites.operator.prepare_build_metadata"):
            Operator(kube).reconcile_build(build)
        self.assertEqual(self._statuses(kube)[-1]["phase"], "Building")

    def test_a_failed_build_drops_its_source_and_metadata(self) -> None:
        # Keeping the source code on the PVC will occupy the tenant quota in vain; keeping the metadata will allow the next source code to be read again
        # A digest that does not belong to it.
        build = _build()
        build["metadata"]["finalizers"] = [BUILD_FINALIZER]
        kube = FakeBuildKube(
            build,
            {
                "status": {
                    "conditions": [
                        {
                            "type": "Failed",
                            "status": "True",
                            "message": "Job has reached the specified backoff limit",
                        }
                    ]
                }
            },
        )
        with (
            patch("sites.operator.remove_source") as removed_source,
            patch("sites.operator.remove_build_metadata") as removed_metadata,
        ):
            Operator(kube).reconcile_build(build)
        removed_source.assert_called_once_with(
            build["spec"]["sourcePath"], backend="pvc"
        )
        removed_metadata.assert_called_once_with(build_metadata_subpath(build))
        status = self._statuses(kube)[-1]
        self.assertEqual(status["phase"], "Failed")
        self.assertIn("backoff limit", status["message"])
        self.assertEqual(kube.created, [])

    def test_a_legacy_build_without_an_assigned_port_fails_before_building(self) -> None:
        build = _build()
        build["metadata"]["finalizers"] = [BUILD_FINALIZER]
        del build["spec"]["nodePort"]
        kube = FakeBuildKube(build, {})
        Operator(kube).reconcile_build(build)
        self.assertEqual(kube.created, [])
        statuses = [
            body["status"]
            for path, body in kube.patched
            if path.endswith("/status")
        ]
        self.assertEqual(statuses[-1]["phase"], "Failed")
        self.assertIn("persisted nodePort", statuses[-1]["message"])

    def test_deleting_a_build_takes_its_builder_pod_with_it(self) -> None:
        build = _build()
        build["metadata"]["finalizers"] = [BUILD_FINALIZER]
        build["metadata"]["deletionTimestamp"] = "2026-08-13T00:00:00Z"
        kube = FakeBuildKube(build, {})
        with (
            patch("sites.operator.remove_source"),
            patch("sites.operator.remove_build_metadata"),
        ):
            Operator(kube).reconcile_build(build)
        deletes = dict(kube.deleted)
        job_path = next(path for path in deletes if "/jobs/" in path)
        # batch/v1 Jobs orphan their dependents unless told otherwise, which
        # left the BuildKit Pod running — still holding 1 CPU / 1Gi and still
        # able to push to the registry — after its Job object was gone.
        self.assertEqual(deletes[job_path]["propagationPolicy"], "Background")


if __name__ == "__main__":
    unittest.main()
