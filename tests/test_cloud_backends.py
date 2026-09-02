"""Contract tests for PostgreSQL configuration and S3-compatible OSS."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sites.builds import (
    build_job_resource,
    normalize_source_payload,
    persist_source,
    remove_source,
    site_build_resource,
)
from sites.object_storage import (
    ObjectStorageError,
    S3CompatibleConfig,
    S3CompatibleSourceStore,
    materialize_source,
    source_payload,
    validate_source_payload,
)
from sites.storage import DatabaseConfig, StorageError, store_from_env


class _FakeObject:
    def __init__(self, body: bytes):
        self._body = body

    def read(self, *_args: int) -> bytes:
        return self._body


class _FakeS3Client:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.calls: list[tuple[str, str]] = []

    def put_object(
        self, *, Bucket: str, Key: str, Body: bytes, **_kwargs: object
    ) -> None:
        self.calls.append((Bucket, Key))
        self.objects[Key] = Body

    def get_object(self, *, Bucket: str, Key: str) -> _FakeObject:
        self.calls.append((Bucket, Key))
        if Key not in self.objects:
            raise KeyError(Key)
        return {"Body": _FakeObject(self.objects[Key])}

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        self.calls.append((Bucket, Key))
        self.objects.pop(Key, None)


def _oss_config() -> S3CompatibleConfig:
    return S3CompatibleConfig(
        endpoint="https://s3.oss-cn-shanghai.aliyuncs.com",
        bucket="site-sources",
        prefix="sources",
        region="cn-shanghai",
        addressing_style="virtual",
        signature_version="s3",
        access_key_id_file=Path("/unused"),
        access_key_secret_file=Path("/unused"),
    )


class OSSSourceBackendTests(unittest.TestCase):
    def test_config_from_env_selects_s3_compatible_endpoint_and_signing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            key_id = Path(directory) / "access-key-id"
            secret = Path(directory) / "access-key-secret"
            key_id.write_text("id\n")
            secret.write_text("secret\n")
            with patch.dict(
                os.environ,
                {
                    "SITES_OSS_ENDPOINT": "https://s3.oss-cn-shanghai.aliyuncs.com",
                    "SITES_OSS_BUCKET": "site-sources",
                    "SITES_OSS_PREFIX": "sources",
                    "SITES_OSS_REGION": "cn-shanghai",
                    "SITES_OSS_ADDRESSING_STYLE": "virtual",
                    "SITES_OSS_SIGNATURE_VERSION": "s3",
                    "SITES_OSS_ACCESS_KEY_ID_FILE": str(key_id),
                    "SITES_OSS_ACCESS_KEY_SECRET_FILE": str(secret),
                },
                clear=True,
            ):
                config = S3CompatibleConfig.from_env()
            self.assertEqual(config.endpoint, "https://s3.oss-cn-shanghai.aliyuncs.com")
            self.assertEqual(config.region, "cn-shanghai")
            self.assertEqual(config.addressing_style, "virtual")
            self.assertEqual(config.signature_version, "s3")

    def test_config_rejects_an_unknown_signing_mode(self) -> None:
        with patch.dict(
            os.environ,
            {
                "SITES_OSS_ENDPOINT": "https://s3.example.test",
                "SITES_OSS_BUCKET": "site-sources",
                "SITES_OSS_REGION": "cn-shanghai",
                "SITES_OSS_SIGNATURE_VERSION": "s3v2",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(
                ObjectStorageError, "SITES_OSS_SIGNATURE_VERSION"
            ):
                S3CompatibleConfig.from_env()

    def test_config_rejects_a_plaintext_object_storage_endpoint(self) -> None:
        with patch.dict(
            os.environ,
            {
                "SITES_OSS_ENDPOINT": "http://s3.example.test",
                "SITES_OSS_BUCKET": "site-sources",
                "SITES_OSS_REGION": "cn-shanghai",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(
                ObjectStorageError, "SITES_OSS_ENDPOINT must use https"
            ):
                S3CompatibleConfig.from_env()

    def test_payload_round_trip_and_key_are_content_addressed(self) -> None:
        client = _FakeS3Client()
        store = S3CompatibleSourceStore(_oss_config(), client)
        payload = source_payload(
            {"Dockerfile": "FROM scratch\n", "index.html": "<html></html>"},
            normalize_source_payload(
                {
                    "name": "example",
                    "files": {
                        "Dockerfile": "FROM scratch\n",
                        "index.html": "<html></html>",
                    },
                },
                "acme",
                "alice",
            ).sha256,
        )

        source_path = f"acme/alice/example/{payload['sha256']}"
        store.put(source_path, payload)
        key = f"sources/{source_path}/source.json"
        self.assertEqual(sorted(client.objects), [key])
        self.assertEqual(json.loads(client.objects[key]), payload)
        self.assertEqual(store.get(source_path), payload)
        self.assertEqual(
            client.calls,
            [("site-sources", key)] * 2,
        )

    def test_tampered_payload_is_rejected(self) -> None:
        bundle = normalize_source_payload(
            {"name": "example", "files": {"Dockerfile": "FROM scratch\n"}},
            "acme",
            "alice",
        )
        payload = source_payload(bundle.files, bundle.sha256)
        payload["files"]["Dockerfile"] = "FROM alpine\n"
        with self.assertRaisesRegex(ObjectStorageError, "digest mismatch"):
            validate_source_payload(payload, bundle.source_path)

    def test_an_oversized_object_is_rejected_before_download(self) -> None:
        class TooLargeClient(_FakeS3Client):
            def get_object(self, *, Bucket: str, Key: str) -> dict:
                return {
                    "ContentLength": 1024 * 1024 + 1,
                    "Body": _FakeObject(b"{}"),
                }

        with self.assertRaisesRegex(ObjectStorageError, "too large"):
            S3CompatibleSourceStore(_oss_config(), TooLargeClient()).get(
                "a/b/c/" + "a" * 64
            )

    def test_materializer_does_not_chmod_the_kubelet_owned_volume_root(self) -> None:
        files = {"Dockerfile": "FROM scratch\n", "src/app.py": "print('ok')\n"}
        bundle = normalize_source_payload(
            {"name": "example", "files": files}, "acme", "alice"
        )
        payload = source_payload(files, bundle.sha256)
        with tempfile.TemporaryDirectory() as directory, patch(
            "sites.object_storage.S3CompatibleSourceStore"
        ) as store_class, patch.object(Path, "chmod") as chmod:
            store_class.return_value.get.return_value = payload
            root = Path(directory)
            materialize_source(bundle.source_path, root)
            self.assertEqual(
                sorted(call.args[0] for call in chmod.call_args_list),
                [0o444, 0o444, 0o755],
            )

    def test_build_job_materializes_oss_before_buildkit(self) -> None:
        bundle = normalize_source_payload(
            {"name": "example", "files": {"Dockerfile": "FROM scratch\n"}},
            "acme",
            "alice",
        )
        build = site_build_resource(
            bundle,
            bundle.source_path,
            namespace="sites-local",
            revision="1",
            node_port=30080,
            source_backend="oss",
        )
        self.assertEqual(build["spec"]["sourceStorage"], "oss")
        job = build_job_resource(build, namespace="sites-local")
        pod = job["spec"]["template"]["spec"]
        self.assertEqual(
            pod["securityContext"],
            {"fsGroup": 65532, "fsGroupChangePolicy": "OnRootMismatch"},
        )
        self.assertEqual(pod["initContainers"][0]["name"], "fetch-source")
        self.assertEqual(pod["initContainers"][0]["args"][1], bundle.source_path)
        buildkit = pod["containers"][0]
        workspace = next(
            mount for mount in buildkit["volumeMounts"] if mount["mountPath"] == "/workspace"
        )
        self.assertEqual(workspace["name"], "source-context")
        volumes = {volume["name"]: volume for volume in pod["volumes"]}
        self.assertIn("emptyDir", volumes["source-context"])
        self.assertEqual(volumes["oss-auth"]["secret"]["secretName"], "sites-oss-auth")

    def test_build_job_passes_s3_compatibility_settings(self) -> None:
        bundle = normalize_source_payload(
            {"name": "example", "files": {"Dockerfile": "FROM scratch\n"}},
            "acme",
            "alice",
        )
        build = site_build_resource(
            bundle,
            bundle.source_path,
            namespace="sites-local",
            revision="1",
            node_port=30080,
            source_backend="oss",
        )
        with patch.dict(
            os.environ,
            {
                "SITES_OSS_ENDPOINT": "https://s3.oss-cn-shanghai.aliyuncs.com",
                "SITES_OSS_BUCKET": "site-sources",
                "SITES_OSS_PREFIX": "sources",
                "SITES_OSS_REGION": "cn-shanghai",
                "SITES_OSS_ADDRESSING_STYLE": "virtual",
                "SITES_OSS_SIGNATURE_VERSION": "s3",
            },
        ):
            job = build_job_resource(build, namespace="sites-local")
        init_env = {
            item["name"]: item["value"]
            for item in job["spec"]["template"]["spec"]["initContainers"][0]["env"]
        }
        self.assertEqual(init_env["SITES_OSS_ENDPOINT"], "https://s3.oss-cn-shanghai.aliyuncs.com")
        self.assertEqual(init_env["SITES_OSS_REGION"], "cn-shanghai")
        self.assertEqual(init_env["SITES_OSS_ADDRESSING_STYLE"], "virtual")
        self.assertEqual(init_env["SITES_OSS_SIGNATURE_VERSION"], "s3")

    def test_persistence_and_cleanup_dispatch_to_oss(self) -> None:
        client = _FakeS3Client()
        bundle = normalize_source_payload(
            {"name": "example", "files": {"Dockerfile": "FROM scratch\n"}},
            "acme",
            "alice",
        )
        with patch("sites.builds.S3CompatibleSourceStore", return_value=S3CompatibleSourceStore(
            _oss_config(), client
        )):
            self.assertEqual(
                persist_source(bundle, backend="oss"), bundle.source_path
            )
            self.assertTrue(client.objects)
            remove_source(bundle.source_path, backend="oss")
        self.assertFalse(client.objects)


class DatabaseConfigRedactionTests(unittest.TestCase):
    def test_password_is_absent_from_repr(self) -> None:
        config = DatabaseConfig(
            "postgres.internal", 5432, "sites", "sites", "database-password"
        )
        self.assertNotIn("database-password", repr(config))

    def test_runtime_rejects_removed_database_backends(self) -> None:
        for backend in ("mysql", "sqlite"):
            with self.subTest(backend=backend), patch.dict(
                os.environ, {"SITES_DB_BACKEND": backend}, clear=True
            ):
                with self.assertRaisesRegex(StorageError, "PostgreSQL is required"):
                    store_from_env()

    def test_runtime_disables_connection_cache_for_request_threads(self) -> None:
        config = DatabaseConfig(
            "postgres.internal", 5432, "sites", "sites", "database-password"
        )
        sentinel = object()
        with patch.dict(os.environ, {"SITES_DB_BACKEND": "postgresql"}, clear=True), patch(
            "sites.storage.DatabaseConfig.from_env", return_value=config
        ), patch("sites.storage.Store.postgres", return_value=sentinel) as postgres:
            self.assertIs(store_from_env(), sentinel)
        postgres.assert_called_once_with(config, cache_connections=False)


if __name__ == "__main__":
    unittest.main()
