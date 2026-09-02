"""Security and execution contract for agent-generated PostgreSQL reads."""
from __future__ import annotations

import os
import time
import unittest
import uuid
from unittest import mock

import psycopg

from sites.nl2sql import QueryRejected, ReadOnlyQueryExecutor, validate_read_query
from sites.site_database import SiteDatabaseBinding, SiteDatabaseProvisioner
from sites.storage import DatabaseConfig, StorageError


def _retry_connect(**kwargs):
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            return psycopg.connect(**kwargs)
        except psycopg.OperationalError as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(1)
    assert last_error is not None
    raise last_error


class QueryValidationTests(unittest.TestCase):
    def test_one_select_or_cte_is_allowed(self) -> None:
        self.assertEqual(validate_read_query("select 1"), "SELECT 1")
        self.assertIn(
            "SELECT",
            validate_read_query(
                "WITH totals AS (SELECT 1 AS value) SELECT value FROM totals"
            ),
        )

    def test_multiple_and_mutating_statements_are_rejected(self) -> None:
        rejected = (
            "SELECT 1; SELECT 2",
            "INSERT INTO inventory VALUES (2)",
            "UPDATE inventory SET id = 2",
            "DELETE FROM inventory",
            "DROP TABLE inventory",
            "SELECT * INTO copied FROM inventory",
            "WITH changed AS (DELETE FROM inventory RETURNING id) SELECT * FROM changed",
        )
        for query in rejected:
            with self.subTest(query=query), self.assertRaises(QueryRejected):
                validate_read_query(query)

    def test_system_and_schema_qualified_relations_are_rejected(self) -> None:
        rejected = (
            "SELECT * FROM pg_catalog.pg_roles",
            "SELECT * FROM information_schema.tables",
            "SELECT * FROM other_tenant.inventory",
            "SELECT * FROM pg_roles",
        )
        for query in rejected:
            with self.subTest(query=query), self.assertRaises(QueryRejected):
                validate_read_query(query)


    def test_a_lexer_failure_is_a_rejection_not_a_crash(self) -> None:
        """Tokenizer errors must land on the same 400 as parser errors.

        🔴 sqlglot's TokenError is a sibling of ParseError, not a subclass, so
        `except ParseError` let an unterminated comment or quote escape all the
        way to the generic handler - a 500 plus an unexpected-exception metric
        for input a model produces routinely."""
        for query in (
            "SELECT * FROM inventory/*",
            "SELECT 'unterminated",
            'SELECT "unterminated',
        ):
            with self.subTest(query=query):
                with self.assertRaises(QueryRejected):
                    validate_read_query(query)


class QueryExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        settings: dict[str, object] = {
            "host": os.environ.get("SITES_TEST_DB_HOST", "127.0.0.1"),
            "port": int(os.environ.get("SITES_TEST_DB_PORT", "55439")),
            "dbname": os.environ.get("SITES_TEST_DB_NAME", "postgres"),
            "user": os.environ.get("SITES_TEST_DB_USER", "postgres"),
            "password": os.environ.get("SITES_TEST_DB_PASSWORD", ""),
        }
        # See test_support._settings: the local test server has no TLS, and the
        # production default is "require" rather than a silent plaintext fallback.
        settings["sslmode"] = (
            os.environ.get("SITES_TEST_DB_SSLMODE", "").strip() or "disable"
        )
        self.settings = settings
        self.admin = DatabaseConfig(
            host=str(settings["host"]),
            port=int(settings["port"]),
            dbname=str(settings["dbname"]),
            user=str(settings["user"]),
            password=str(settings["password"]),
            sslmode=str(settings["sslmode"]),
        )
        suffix = uuid.uuid4().hex
        self.binding = SiteDatabaseBinding.for_site("query", suffix, "site")
        self.runtime_password = f"runtime-{uuid.uuid4().hex}"
        self.reader_password = f"reader-{uuid.uuid4().hex}"
        self.provisioner = SiteDatabaseProvisioner(self.admin)
        self.provisioner.provision(
            self.binding,
            runtime_password=self.runtime_password,
            reader_password=self.reader_password,
        )
        self.addCleanup(self._cleanup)
        runtime = self._role_config(
            self.binding.runtime_role, self.runtime_password
        )
        with _retry_connect(**runtime.postgres_connect_kwargs()) as connection:
            connection.execute("CREATE TABLE inventory (id INTEGER PRIMARY KEY)")
            connection.execute("INSERT INTO inventory VALUES (1), (2), (3)")
        self.provisioner.refresh_reader_grants(
            self.binding, runtime_password=self.runtime_password
        )
        self.executor = ReadOnlyQueryExecutor(
            self._role_config(self.binding.reader_role, self.reader_password),
            connect=_retry_connect,
        )

    def _role_config(self, role: str, password: str) -> DatabaseConfig:
        project_ref = os.environ.get("SUPABASE_PROJECT_REF", "").strip()
        user = f"{role}.{project_ref}" if project_ref else role
        return DatabaseConfig(
            host=self.admin.host,
            port=self.admin.port,
            dbname=self.admin.dbname,
            user=user,
            password=password,
            sslmode=self.admin.sslmode,
        )

    def _cleanup(self) -> None:
        with psycopg.connect(**self.settings, autocommit=True) as connection:
            connection.execute(f'DROP SCHEMA IF EXISTS "{self.binding.schema}" CASCADE')
            connection.execute(f'DROP ROLE IF EXISTS "{self.binding.reader_role}"')
            connection.execute(f'DROP ROLE IF EXISTS "{self.binding.runtime_role}"')

    def test_results_are_bounded_and_serializable(self) -> None:
        result = self.executor.execute(
            "SELECT id FROM inventory ORDER BY id", row_limit=2
        )
        self.assertEqual(result.columns, ("id",))
        self.assertEqual(result.rows, ((1,), (2,)))
        self.assertEqual(result.row_count, 2)
        self.assertTrue(result.truncated)

    def test_database_is_still_read_only_if_validation_is_bypassed(self) -> None:
        with mock.patch(
            "sites.nl2sql.validate_read_query",
            return_value="DELETE FROM inventory",
        ):
            with self.assertRaises(QueryRejected):
                self.executor.execute("SELECT 1")
        self.assertEqual(
            self.executor.execute("SELECT COUNT(*) FROM inventory").rows,
            ((3,),),
        )

    def test_semantic_query_errors_are_correctable_rejections(self) -> None:
        with self.assertRaisesRegex(QueryRejected, "does_not_exist"):
            self.executor.execute("SELECT * FROM does_not_exist")


if __name__ == "__main__":
    unittest.main()
