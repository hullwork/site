"""Controlled dynamic-site migration validation and execution tests."""
from __future__ import annotations

import hashlib
import unittest

from sites.migrations import SiteMigrationExecutor, validate_migration_artifact
from sites.storage import DatabaseConfig, StorageError
from sites.validation import ValidationError


def _artifact(sql: str, schema: str = "site_deadbeef"):
    return validate_migration_artifact(
        sql, hashlib.sha256(sql.encode("utf-8")).hexdigest(), schema
    )


class MigrationValidationTests(unittest.TestCase):
    def test_digest_is_bound_to_exact_utf8_content(self) -> None:
        sql = "CREATE TABLE IF NOT EXISTS inventory (id BIGINT PRIMARY KEY)"
        artifact = _artifact(sql)
        self.assertEqual(artifact.content, sql)
        with self.assertRaisesRegex(ValidationError, "does not match"):
            validate_migration_artifact(sql + "\n", artifact.sha256, "site_deadbeef")

    def test_accepts_only_idempotent_additive_ddl(self) -> None:
        sql = """
        CREATE TABLE IF NOT EXISTS inventory (id BIGINT PRIMARY KEY);
        ALTER TABLE inventory ADD COLUMN IF NOT EXISTS label TEXT;
        CREATE INDEX IF NOT EXISTS inventory_label_idx ON inventory(label);
        """
        self.assertEqual(len(_artifact(sql).statements), 3)
        rejected = (
            "DROP TABLE inventory",
            "DELETE FROM inventory",
            "ALTER TABLE inventory DROP COLUMN label",
            "CREATE TABLE inventory (id BIGINT)",
            "CREATE VIEW inventory_view AS SELECT 1",
            "CREATE TEMP TABLE IF NOT EXISTS scratch (id BIGINT)",
        )
        for statement in rejected:
            with self.subTest(statement=statement):
                with self.assertRaises(ValidationError):
                    _artifact(statement)

    def test_rejects_cross_schema_ddl(self) -> None:
        with self.assertRaisesRegex(ValidationError, "site schema"):
            _artifact("CREATE TABLE IF NOT EXISTS public.inventory (id BIGINT)")


class _Cursor:
    def __init__(self, *, fail: bool = False) -> None:
        self.commands: list[tuple[object, object]] = []
        self.fail = fail

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, command, parameters=None):
        self.commands.append((command, parameters))
        if self.fail and isinstance(command, str) and command.startswith("CREATE"):
            raise RuntimeError("driver detail that must stay internal")


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor
        self.exited_with = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_args):
        self.exited_with = exc_type
        return False

    def cursor(self):
        return self._cursor


class MigrationExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = DatabaseConfig(
            host="db.invalid",
            port=5432,
            dbname="sites",
            user="site_runtime_deadbeef",
            password="never-return-this",
        )

    def test_executor_sets_local_bounds_and_runs_one_transaction(self) -> None:
        cursor = _Cursor()
        connection = _Connection(cursor)
        executor = SiteMigrationExecutor(
            self.config,
            connect=lambda **_kwargs: connection,
            statement_timeout_ms=4321,
            lock_timeout_ms=1234,
        )
        executor.execute(
            _artifact("CREATE TABLE IF NOT EXISTS inventory (id BIGINT)"),
            "site_deadbeef",
        )
        rendered = [str(command) for command, _ in cursor.commands]
        self.assertIn("statement_timeout", rendered[0])
        self.assertEqual(cursor.commands[0][1], ("4321ms",))
        self.assertIn("lock_timeout", rendered[1])
        self.assertEqual(cursor.commands[1][1], ("1234ms",))
        self.assertIn("search_path", rendered[2])
        self.assertTrue(rendered[3].startswith("CREATE TABLE"))
        self.assertIsNone(connection.exited_with)

    def test_driver_failure_is_wrapped_without_credential_or_detail(self) -> None:
        connection = _Connection(_Cursor(fail=True))
        executor = SiteMigrationExecutor(
            self.config, connect=lambda **_kwargs: connection
        )
        with self.assertRaisesRegex(StorageError, "site database migration failed") as caught:
            executor.execute(
                _artifact("CREATE TABLE IF NOT EXISTS inventory (id BIGINT)"),
                "site_deadbeef",
            )
        self.assertNotIn(self.config.password, str(caught.exception))
        self.assertNotIn("driver detail", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
