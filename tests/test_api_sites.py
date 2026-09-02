"""HTTP boundary tests for dynamic-site read-only queries."""
from __future__ import annotations

import unittest
import hashlib
import threading
from datetime import datetime, timezone
from unittest import mock

from sites.api_sites import SitesMixin
from sites.nl2sql import QueryRejected, QueryResult
from sites.object_storage import ObjectStorageError
from sites.storage import StorageConflictError
from sites.validation import STATIC_IMAGE, ValidationError


class _Store:
    def __init__(self, site_type: str = "dynamic", current_version: int | None = 3):
        self.site_type = site_type
        self.current_version = current_version
        self.identity: tuple[str, str, str] | None = None
        self.created: dict | None = None
        self.promoted: int | None = None
        self.migration_status = "not-required"

    def site(self, merchant_id: str, user_id: str, name: str):
        self.identity = (merchant_id, user_id, name)
        return {
            "site_name": name,
            "site_type": self.site_type,
            "current_version": self.current_version,
        }

    def list_site_versions(self, merchant_id, user_id, name, *, limit):
        return [self._version(name, 2)]

    def get_deployment(self, merchant_id, user_id, name):
        return {"spec": {"siteVersion": 4}, "phase": "Deploying"}

    def create_site_version(self, merchant_id, user_id, name, **values):
        self.created = values
        return self._version(name, 4, **values)

    def promote_site_version(self, merchant_id, user_id, name, version):
        self.promoted = version
        return {"site_type": self.site_type, "current_version": version}

    def site_version(self, merchant_id, user_id, name, version):
        return self._version(name, version)

    def site_migration(self, merchant_id, user_id, name, version):
        return {"status": self.migration_status, "applied_at": None}

    def begin_site_migration(self, merchant_id, user_id, name, version):
        self.migration_status = "running"
        return {
            "migration_sha256": self.created["migration_sha256"],
            "migration_sql": self.created["migration_sql"],
        }

    def finish_site_migration(
        self, merchant_id, user_id, name, version, *, succeeded
    ):
        self.migration_status = "succeeded" if succeeded else "failed"

    @staticmethod
    def _version(name: str, version: int, **overrides):
        return {
            "site_name": name,
            "version": version,
            "content_sha256": "a" * 64,
            "artifact_uri": None,
            "image": None,
            "database_schema": None,
            "metadata": {},
            "created_at": datetime(2026, 8, 28, tzinfo=timezone.utc),
            **overrides,
        }


class _Databases:
    def __init__(self) -> None:
        self.provisioned = False
        self.refreshed = False
        self.fail_refresh = False

    def reader_config(self, merchant_id: str, user_id: str, name: str):
        return (merchant_id, user_id, name)

    def provision(self, merchant_id: str, user_id: str, name: str):
        self.provisioned = True
        return type("Binding", (), {"schema": "site_deadbeef"})()

    def refresh_reader(self, merchant_id: str, user_id: str, name: str):
        if self.fail_refresh:
            raise StorageConflictError("grant failed with private driver detail")
        self.refreshed = True

    def deployment_binding(self, merchant_id: str, user_id: str, name: str):
        return {
            "schema": "site_deadbeef",
            "controlSecretName": "site-db-deadbeefdeadbeefdeadbeef",
        }

    def runtime_config(self, merchant_id: str, user_id: str, name: str):
        return type("Config", (), {"statement_timeout": 10})()


class _StaticArtifacts:
    def __init__(self) -> None:
        self.created: dict | None = None

    def create_version_artifact(
        self, merchant_id, user_id, site_name, files, *, declared_sha256=None
    ):
        digest = hashlib.sha256(b"canonical-static-files").hexdigest()
        if declared_sha256 and declared_sha256 != digest:
            raise ValidationError("contentSha256 does not match uploaded files")
        self.created = {
            "identity": (merchant_id, user_id, site_name),
            "files": files,
        }
        return {
            "artifactUri": f"oss://private-sites/{merchant_id}/{user_id}/{site_name}/{digest}.json",
            "sourcePath": f"{merchant_id}/{user_id}/{site_name}/{digest}",
            "contentSha256": digest,
            "sizeBytes": 42,
            "fileCount": len(files),
        }


class _Handler(SitesMixin):
    def __init__(self, body: dict, store: _Store | None = None) -> None:
        self.body = body
        self.store = store or _Store()
        self.site_databases = _Databases()
        self.static_artifacts = _StaticArtifacts()
        self.mutation_lock = threading.Lock()
        self.mutation_lock_timeout = 0.1
        self.response: tuple[int, dict] | None = None

    def _authenticate(self):
        return ("acme", "alice")

    def _read_body(self):
        return self.body

    def _json(self, status: int, payload: dict) -> None:
        self.response = (status, payload)


class SiteQueryApiTests(unittest.TestCase):
    @mock.patch("sites.api_sites.ReadOnlyQueryExecutor")
    def test_query_uses_authenticated_site_identity(self, executor_class) -> None:
        executor_class.return_value.execute.return_value = QueryResult(
            columns=("id",), rows=((1,),), row_count=1, truncated=False
        )
        handler = _Handler({"query": "SELECT id FROM inventory"})
        handler._query_dynamic_site("shop")
        self.assertEqual(handler.store.identity, ("acme", "alice", "shop"))
        self.assertEqual(handler.response[0], 200)
        self.assertEqual(handler.response[1]["rows"], [[1]])
        self.assertEqual(handler.response[1]["version"], 3)

    def test_static_site_cannot_use_database_query(self) -> None:
        handler = _Handler({"query": "SELECT 1"}, _Store(site_type="static"))
        handler._query_dynamic_site("docs")
        self.assertEqual(handler.response[0], 409)
        self.assertEqual(handler.response[1]["code"], "site_not_dynamic")

    def test_static_version_requires_object_storage_uri(self) -> None:
        handler = _Handler(
            {
                "siteType": "static",
                "contentSha256": "a" * 64,
                "artifactUri": "https://example.invalid/site.tar.gz",
            }
        )
        handler._create_site_version("docs")
        self.assertEqual(handler.response[0], 400)

    def test_static_version_files_are_uploaded_and_digest_is_recorded(self) -> None:
        handler = _Handler(
            {
                "siteType": "static",
                "files": {"index.html": "<h1>versioned</h1>"},
                "metadata": {"generator": "agent"},
            }
        )
        handler._create_site_version("docs")
        self.assertEqual(handler.response[0], 201)
        self.assertEqual(
            handler.static_artifacts.created["identity"],
            ("acme", "alice", "docs"),
        )
        self.assertEqual(
            handler.store.created["content_sha256"],
            hashlib.sha256(b"canonical-static-files").hexdigest(),
        )
        self.assertTrue(handler.store.created["artifact_uri"].startswith("oss://"))
        self.assertEqual(handler.response[1]["staticArtifact"]["fileCount"], 1)
        self.assertNotIn("secret", repr(handler.response[1]).lower())

    def test_static_version_rejects_declared_digest_mismatch(self) -> None:
        handler = _Handler(
            {
                "siteType": "static",
                "contentSha256": "a" * 64,
                "files": {"index.html": "x"},
            }
        )
        handler._create_site_version("docs")
        self.assertEqual(handler.response[0], 400)
        self.assertIn("does not match", handler.response[1]["error"])

    def test_static_upload_failure_does_not_leak_storage_details(self) -> None:
        handler = _Handler(
            {"siteType": "static", "files": {"index.html": "x"}}
        )
        handler.static_artifacts.create_version_artifact = mock.Mock(
            side_effect=ObjectStorageError("secret endpoint detail")
        )
        handler._create_site_version("docs")
        self.assertEqual(handler.response[0], 503)
        self.assertEqual(handler.response[1]["code"], "static_artifact_unavailable")
        self.assertNotIn("secret endpoint", repr(handler.response))

    @mock.patch("sites.api_sites.SiteMigrationExecutor")
    def test_dynamic_version_provisions_stable_schema(self, executor_class) -> None:
        migration_sql = "CREATE TABLE IF NOT EXISTS inventory (id BIGINT PRIMARY KEY)"
        handler = _Handler(
            {
                "siteType": "dynamic",
                "contentSha256": "b" * 64,
                "image": "registry.example/shop@sha256:" + "c" * 64,
                "metadata": {"scaffold": "fastapi"},
                "schemaChange": "additive",
                "migrationStrategy": "expand-contract",
                "migrationSha256": hashlib.sha256(migration_sql.encode()).hexdigest(),
                "migrationSql": migration_sql,
            }
        )
        handler._create_site_version("shop")
        self.assertEqual(handler.response[0], 201)
        self.assertTrue(handler.site_databases.provisioned)
        self.assertEqual(handler.store.created["database_schema"], "site_deadbeef")
        self.assertNotIn("password", repr(handler.response[1]).lower())
        self.assertEqual(handler.response[1]["schemaChange"], "additive")
        self.assertEqual(handler.response[1]["migrationStatus"], "succeeded")
        executor_class.return_value.execute.assert_called_once()
        self.assertTrue(handler.site_databases.refreshed)

    def test_destructive_schema_change_requires_manual_cutover(self) -> None:
        handler = _Handler(
            {
                "siteType": "dynamic",
                "contentSha256": "b" * 64,
                "image": "registry.example/shop@sha256:" + "c" * 64,
                "changeMode": "rebuild-compatible",
                "schemaChange": "destructive",
                "migrationStrategy": "manual-cutover",
            }
        )
        handler._create_site_version("shop")
        self.assertEqual(handler.response[0], 400)
        self.assertIn("destructive", handler.response[1]["error"])

    def test_migration_content_must_match_declared_digest(self) -> None:
        handler = _Handler(
            {
                "siteType": "dynamic",
                "contentSha256": "b" * 64,
                "image": "registry.example/shop@sha256:" + "c" * 64,
                "schemaChange": "additive",
                "migrationStrategy": "expand-contract",
                "migrationSha256": "d" * 64,
                "migrationSql": "CREATE TABLE IF NOT EXISTS inventory (id BIGINT)",
            }
        )
        handler._create_site_version("shop")
        self.assertEqual(handler.response[0], 400)
        self.assertIn("does not match", handler.response[1]["error"])

    @mock.patch("sites.api_sites.SiteMigrationExecutor")
    def test_migration_failure_is_persisted_without_driver_details(
        self, executor_class
    ) -> None:
        migration_sql = "CREATE TABLE IF NOT EXISTS inventory (id BIGINT)"
        executor_class.return_value.execute.side_effect = StorageConflictError(
            "password=must-not-cross-api"
        )
        handler = _Handler(
            {
                "siteType": "dynamic",
                "contentSha256": "b" * 64,
                "image": "registry.example/shop@sha256:" + "c" * 64,
                "schemaChange": "additive",
                "migrationStrategy": "expand-contract",
                "migrationSha256": hashlib.sha256(migration_sql.encode()).hexdigest(),
                "migrationSql": migration_sql,
            }
        )
        handler._create_site_version("shop")
        self.assertEqual(handler.response[0], 409)
        self.assertEqual(handler.response[1]["migrationStatus"], "failed")
        self.assertEqual(handler.store.migration_status, "failed")
        self.assertNotIn("must-not-cross-api", repr(handler.response))

    @mock.patch("sites.api_sites.SiteMigrationExecutor")
    def test_reader_grant_failure_marks_migration_failed(self, executor_class) -> None:
        migration_sql = "CREATE TABLE IF NOT EXISTS inventory (id BIGINT)"
        handler = _Handler(
            {
                "siteType": "dynamic",
                "contentSha256": "b" * 64,
                "image": "registry.example/shop@sha256:" + "c" * 64,
                "schemaChange": "additive",
                "migrationStrategy": "expand-contract",
                "migrationSha256": hashlib.sha256(migration_sql.encode()).hexdigest(),
                "migrationSql": migration_sql,
            }
        )
        handler.site_databases.fail_refresh = True
        handler._create_site_version("shop")
        self.assertEqual(handler.response[0], 409)
        self.assertEqual(handler.store.migration_status, "failed")
        self.assertNotIn("private driver detail", repr(handler.response))

    def test_dynamic_promotion_refreshes_reader_before_pointer(self) -> None:
        handler = _Handler({"version": 2})
        handler._promote_site_version("shop")
        self.assertEqual(handler.response[0], 200)
        self.assertTrue(handler.site_databases.refreshed)
        self.assertEqual(handler.store.promoted, 2)

    def test_promotion_fails_retryably_when_snapshot_sync_holds_the_fence(self) -> None:
        handler = _Handler({"version": 2})
        handler.mutation_lock.acquire()
        handler.mutation_lock_timeout = 0
        try:
            handler._promote_site_version("shop")
        finally:
            handler.mutation_lock.release()
        self.assertEqual(handler.response[0], 503)
        self.assertEqual(handler.response[1]["code"], "control_plane_busy")
        self.assertIsNone(handler.store.promoted)

    def test_versions_list_marks_current_pointer(self) -> None:
        handler = _Handler({})
        handler._list_site_versions("shop")
        self.assertEqual(handler.response[0], 200)
        self.assertEqual(handler.response[1]["currentVersion"], 3)
        self.assertEqual(handler.response[1]["versions"][0]["version"], 2)
        self.assertEqual(handler.response[1]["deployedVersion"], 4)
        self.assertEqual(handler.response[1]["deploymentPhase"], "Deploying")

    def test_deployment_binding_accepts_only_matching_immutable_image(self) -> None:
        image = "registry.example/shop@sha256:" + "c" * 64
        store = _Store()
        store.site_version = lambda *_args: store._version(
            "shop", 4, image=image, database_schema="site_deadbeef"
        )
        handler = _Handler({}, store)
        desired = {
            "spec": {
                "merchantID": "acme", "userID": "alice", "serviceName": "shop",
                "siteVersion": 4, "image": image, "env": [],
            }
        }
        handler._bind_dynamic_site_version(desired)
        self.assertEqual(desired["spec"]["database"]["schema"], "site_deadbeef")
        store.migration_status = "pending"
        with self.assertRaisesRegex(StorageConflictError, "migration"):
            handler._bind_dynamic_site_version(desired)
        store.migration_status = "not-required"
        desired["spec"]["image"] = "registry.example/shop@sha256:" + "d" * 64
        with self.assertRaises(StorageConflictError):
            handler._bind_dynamic_site_version(desired)

    def test_static_deployment_binding_uses_immutable_private_artifact(self) -> None:
        store = _Store(site_type="static")
        store.site_version = lambda *_args: store._version(
            "docs",
            4,
            artifact_uri="oss://private-sites/acme/alice/docs/object.json",
            metadata={
                "staticArtifact": {
                    "sourcePath": "acme/alice/docs/" + "a" * 64,
                    "sizeBytes": 10,
                    "fileCount": 1,
                }
            },
        )
        handler = _Handler({}, store)
        desired = {
            "spec": {
                "merchantID": "acme",
                "userID": "alice",
                "serviceName": "docs",
                "siteVersion": 4,
                "image": STATIC_IMAGE,
            }
        }
        handler._bind_dynamic_site_version(desired)
        self.assertEqual(
            desired["spec"]["staticArtifact"],
            {
                "sourcePath": "acme/alice/docs/" + "a" * 64,
                "sha256": "a" * 64,
                "sizeBytes": 10,
                "fileCount": 1,
            },
        )
        self.assertNotIn("artifact_uri", repr(desired))
        desired["spec"]["image"] = "nginx:latest"
        with self.assertRaisesRegex(StorageConflictError, "fixed static"):
            handler._bind_dynamic_site_version(desired)

    @mock.patch("sites.api_sites.ReadOnlyQueryExecutor")
    def test_rejected_sql_is_a_bounded_400(self, executor_class) -> None:
        executor_class.return_value.execute.side_effect = QueryRejected(
            "only SELECT queries are allowed"
        )
        handler = _Handler({"query": "DELETE FROM inventory"})
        handler._query_dynamic_site("shop")
        self.assertEqual(handler.response[0], 400)
        self.assertEqual(handler.response[1]["code"], "sql_query_rejected")


if __name__ == "__main__":
    unittest.main()
