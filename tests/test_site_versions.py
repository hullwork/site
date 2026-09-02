"""Immutable site-version promotion and rollback contract."""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from sites.storage import StorageConflictError
from tests.test_support import postgres_store


class SiteVersionTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = postgres_store(Path(directory.name) / "versions")
        self.store.migrate()

    def test_static_versions_are_append_only_and_rollback_moves_pointer(self) -> None:
        first = self.store.create_site_version(
            "acme",
            "alice",
            "docs",
            site_type="static",
            content_sha256="a" * 64,
            artifact_uri="s3://sites/acme/alice/docs/v1.tar.gz",
        )
        second = self.store.create_site_version(
            "acme",
            "alice",
            "docs",
            site_type="static",
            content_sha256="b" * 64,
            artifact_uri="s3://sites/acme/alice/docs/v2.tar.gz",
        )
        self.assertEqual((first["version"], second["version"]), (1, 2))
        self.assertIsNone(self.store.site("acme", "alice", "docs")["current_version"])

        self.store.promote_site_version("acme", "alice", "docs", 2)
        self.assertEqual(
            self.store.site("acme", "alice", "docs")["current_version"], 2
        )
        self.store.rollback_site("acme", "alice", "docs", 1)
        self.assertEqual(
            self.store.site("acme", "alice", "docs")["current_version"], 1
        )
        versions = self.store.list_site_versions("acme", "alice", "docs")
        self.assertEqual([row["version"] for row in versions], [2, 1])
        self.assertEqual(versions[0]["content_sha256"], "b" * 64)
        self.assertEqual(
            self.store.site_version("acme", "alice", "docs", 1)["artifact_uri"],
            "s3://sites/acme/alice/docs/v1.tar.gz",
        )
        self.assertIsNone(
            self.store.site_version("acme", "alice", "docs", 99)
        )

    def test_dynamic_version_records_the_stable_database_schema(self) -> None:
        version = self.store.create_site_version(
            "acme",
            "alice",
            "shop",
            site_type="dynamic",
            content_sha256="c" * 64,
            image="registry.example/shop@sha256:" + "d" * 64,
            database_schema="site_deadbeef",
            metadata={"scaffold": "fastapi"},
        )
        self.assertEqual(version["database_schema"], "site_deadbeef")
        self.assertEqual(version["metadata"], {"scaffold": "fastapi"})
        self.assertEqual(version["migration_status"], "not-required")

    def test_migration_artifact_status_is_bound_to_immutable_version(self) -> None:
        version = self.store.create_site_version(
            "acme",
            "alice",
            "migrating",
            site_type="dynamic",
            content_sha256="c" * 64,
            image="registry.example/shop@sha256:" + "d" * 64,
            database_schema="site_deadbeef",
            metadata={"migrationSha256": "e" * 64},
            migration_sha256="e" * 64,
            migration_sql="CREATE TABLE IF NOT EXISTS inventory (id BIGINT)",
        )
        self.assertEqual(version["migration_status"], "pending")
        claimed = self.store.begin_site_migration(
            "acme", "alice", "migrating", version["version"]
        )
        self.assertEqual(claimed["migration_sha256"], "e" * 64)
        self.store.finish_site_migration(
            "acme", "alice", "migrating", version["version"], succeeded=True
        )
        state = self.store.site_migration(
            "acme", "alice", "migrating", version["version"]
        )
        self.assertEqual(state["status"], "succeeded")
        self.assertIsNotNone(state["applied_at"])
        self.assertNotIn("migration_sql", state)

    def test_pending_migration_cannot_be_promoted(self) -> None:
        version = self.store.create_site_version(
            "acme",
            "alice",
            "pending",
            site_type="dynamic",
            content_sha256="c" * 64,
            image="registry.example/shop@sha256:" + "d" * 64,
            database_schema="site_deadbeef",
            migration_sha256="e" * 64,
            migration_sql="CREATE TABLE IF NOT EXISTS inventory (id BIGINT)",
        )
        with self.assertRaises(StorageConflictError):
            self.store.promote_site_version(
                "acme", "alice", "pending", version["version"]
            )

    def test_site_type_cannot_change_between_versions(self) -> None:
        self.store.create_site_version(
            "acme",
            "alice",
            "docs",
            site_type="static",
            content_sha256="a" * 64,
            artifact_uri="s3://sites/docs/v1.tar.gz",
        )
        with self.assertRaises(StorageConflictError):
            self.store.create_site_version(
                "acme",
                "alice",
                "docs",
                site_type="dynamic",
                content_sha256="b" * 64,
                database_schema="site_other",
            )

    def test_concurrent_creates_receive_distinct_versions(self) -> None:
        versions: list[int] = []
        errors: list[BaseException] = []

        def worker(index: int) -> None:
            try:
                record = self.store.create_site_version(
                    "acme",
                    "alice",
                    "parallel",
                    site_type="static",
                    content_sha256=str(index) * 64,
                    artifact_uri=f"s3://sites/parallel/{index}.tar.gz",
                )
                versions.append(record["version"])
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(versions), [1, 2, 3, 4])

    def test_only_ready_verified_matching_deployment_is_promoted(self) -> None:
        image = "registry.example/shop@sha256:" + "d" * 64
        self.store.create_site_version(
            "acme", "alice", "shop",
            site_type="dynamic",
            content_sha256="d" * 64,
            image=image,
            database_schema="site_deadbeef",
            metadata={
                "databaseStrategy": "shared",
                "databaseCompatibility": "backward-compatible",
            },
        )
        base = {
            "spec": {
                "merchantID": "acme", "userID": "alice", "serviceName": "shop",
                "siteVersion": 1, "image": image, "revision": "r1",
            },
            "status": {
                "phase": "Running", "ready": True,
                "verification": {"ok": True, "revision": "r1"},
            },
        }
        rejected = {
            **base,
            "status": {
                **base["status"],
                "verification": {"ok": False, "revision": "r1"},
            },
        }
        self.assertEqual(self.store.promote_verified_site_versions([rejected]), 0)
        self.assertIsNone(
            self.store.site("acme", "alice", "shop")["current_version"]
        )
        self.assertEqual(self.store.promote_verified_site_versions([base]), 1)
        self.assertEqual(
            self.store.site("acme", "alice", "shop")["current_version"], 1
        )
        self.assertEqual(self.store.promote_verified_site_versions([base]), 0)

        pending = self.store.create_site_version(
            "acme", "alice", "shop",
            site_type="dynamic",
            content_sha256="f" * 64,
            image="registry.example/shop@sha256:" + "f" * 64,
            database_schema="site_deadbeef",
            metadata={
                "databaseStrategy": "shared",
                "databaseCompatibility": "backward-compatible",
            },
            migration_sha256="a" * 64,
            migration_sql="CREATE TABLE IF NOT EXISTS pending_table (id BIGINT)",
        )
        pending_deployment = {
            "spec": {
                **base["spec"],
                "siteVersion": pending["version"],
                "image": pending["image"],
            },
            "status": base["status"],
        }
        self.assertEqual(
            self.store.promote_verified_site_versions([pending_deployment]), 0
        )
        self.assertEqual(
            self.store.site("acme", "alice", "shop")["current_version"], 1
        )

        second = self.store.create_site_version(
            "acme", "alice", "shop",
            site_type="dynamic",
            content_sha256="e" * 64,
            image="registry.example/shop@sha256:" + "e" * 64,
            database_schema="site_deadbeef",
            metadata={
                "databaseStrategy": "shared",
                "databaseCompatibility": "backward-compatible",
            },
        )
        failed = {
            "metadata": {"name": "acme-alice-shop"},
            "spec": {
                **base["spec"],
                "siteVersion": second["version"],
                "image": second["image"],
                "revision": "r2",
            },
            "status": {
                "phase": "Running", "ready": True,
                "verification": {
                    "ok": False, "revision": "r2", "consecutiveFailures": 1,
                },
            },
        }
        self.assertEqual(self.store.failed_site_version_rollbacks([failed]), [])
        failed["status"]["verification"]["consecutiveFailures"] = 2
        self.assertEqual(
            self.store.failed_site_version_rollbacks([failed]),
            [{"cr_name": "acme-alice-shop", "version": 1, "image": image}],
        )

    def test_verified_static_artifact_is_promoted_and_can_roll_back(self) -> None:
        first = self.store.create_site_version(
            "acme", "alice", "docs",
            site_type="static",
            content_sha256="a" * 64,
            artifact_uri="oss://sites/static/acme/alice/docs/" + "a" * 64 + "/artifact.json",
        )
        self.store.promote_site_version(
            "acme", "alice", "docs", first["version"]
        )
        second = self.store.create_site_version(
            "acme", "alice", "docs",
            site_type="static",
            content_sha256="b" * 64,
            artifact_uri="oss://sites/static/acme/alice/docs/" + "b" * 64 + "/artifact.json",
        )
        item = {
            "metadata": {"name": "acme-alice-docs"},
            "spec": {
                "merchantID": "acme", "userID": "alice", "serviceName": "docs",
                "siteVersion": second["version"], "image": "static-runtime",
                "staticArtifact": {"sha256": "b" * 64}, "revision": "r2",
            },
            "status": {
                "phase": "Running", "ready": True,
                "verification": {"ok": True, "revision": "r2"},
            },
        }
        self.assertEqual(self.store.promote_verified_site_versions([item]), 1)
        self.assertEqual(
            self.store.site("acme", "alice", "docs")["current_version"],
            second["version"],
        )
        self.store.promote_site_version("acme", "alice", "docs", first["version"])
        item["status"]["verification"]["ok"] = False
        item["status"]["verification"]["consecutiveFailures"] = 2
        self.assertEqual(
            self.store.failed_site_version_rollbacks([item]),
            [
                {
                    "cr_name": "acme-alice-docs",
                    "version": first["version"],
                    "site_type": "static",
                    "artifact_uri": first["artifact_uri"],
                    "content_sha256": "a" * 64,
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
