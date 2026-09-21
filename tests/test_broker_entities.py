"""Entity read executor tests, including the RLS proof required by the brief.

The RLS test connects as a non-superuser, non-table-owner role: PostgreSQL lets
superusers and table owners bypass row-level security even with FORCE ROW LEVEL
SECURITY set, so testing through the "postgres" superuser used elsewhere in this
suite would prove nothing. See tests/test_broker_ledger.py for the HULL_TEST_PG_DSN
convention this module shares.
"""
from __future__ import annotations

import os
import unittest

import psycopg

from sites.broker import entities

_PG_DSN = os.environ.get("HULL_TEST_PG_DSN", "")


class EntityConfigTests(unittest.TestCase):
    VALID = {"table": "tickets", "primary_key": "id", "columns": ["id", "title", "owner_id"]}

    def test_compiles_a_valid_entity(self) -> None:
        entity = entities.EntityRead.compile("tickets", self.VALID)
        self.assertEqual(entity.table, "tickets")
        self.assertEqual(entity.columns, ("id", "title", "owner_id"))
        self.assertEqual(entity.primary_key, "id")

    def test_rejects_bad_table_name(self) -> None:
        with self.assertRaises(entities.InvalidEntityConfig):
            entities.EntityRead.compile("x", {**self.VALID, "table": "Tickets; DROP TABLE x"})

    def test_rejects_primary_key_not_in_columns(self) -> None:
        with self.assertRaises(entities.InvalidEntityConfig):
            entities.EntityRead.compile("x", {**self.VALID, "primary_key": "not_a_column"})

    def test_rejects_empty_columns(self) -> None:
        with self.assertRaises(entities.InvalidEntityConfig):
            entities.EntityRead.compile("x", {**self.VALID, "columns": []})

    def test_rejects_bad_column_name(self) -> None:
        with self.assertRaises(entities.InvalidEntityConfig):
            entities.EntityRead.compile("x", {**self.VALID, "columns": ["id", "bad col"]})


class ReadFilterValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.entity = entities.EntityRead.compile(
            "tickets", {"table": "tickets", "primary_key": "id", "columns": ["id", "title"]}
        )

    def test_filter_key_must_be_a_declared_column(self) -> None:
        with self.assertRaises(entities.InvalidFilter):
            entities.read(
                dsn="unused", entity=self.entity, user_id="u1", org_id=None, role="member",
                filters={"not_a_column": "x"}, connect=lambda dsn: (_ for _ in ()).throw(AssertionError("should not connect")),
            )

    def test_limit_out_of_range_rejected_before_connecting(self) -> None:
        with self.assertRaises(entities.InvalidFilter):
            entities.read(
                dsn="unused", entity=self.entity, user_id="u1", org_id=None, role="member",
                limit=0, connect=lambda dsn: (_ for _ in ()).throw(AssertionError("should not connect")),
            )
        with self.assertRaises(entities.InvalidFilter):
            entities.read(
                dsn="unused", entity=self.entity, user_id="u1", org_id=None, role="member",
                limit=1000, row_cap=200,
                connect=lambda dsn: (_ for _ in ()).throw(AssertionError("should not connect")),
            )


@unittest.skipUnless(_PG_DSN, "HULL_TEST_PG_DSN is not set; see tests/test_broker_ledger.py")
class EntityReadRlsTests(unittest.TestCase):
    ROLE = "hull_entity_test_role"
    ROLE_PASSWORD = "hull-entity-test-only"

    def setUp(self) -> None:
        with psycopg.connect(_PG_DSN, autocommit=True) as connection:
            connection.execute("DROP SCHEMA public CASCADE")
            connection.execute("CREATE SCHEMA public")
            connection.execute(f"DROP ROLE IF EXISTS {self.ROLE}")
            connection.execute(
                f"CREATE ROLE {self.ROLE} LOGIN PASSWORD '{self.ROLE_PASSWORD}' NOSUPERUSER"
            )
            connection.execute("GRANT USAGE ON SCHEMA public TO " + self.ROLE)
            connection.execute(
                "CREATE TABLE tickets (id serial PRIMARY KEY, title text NOT NULL, owner_id text NOT NULL)"
            )
            connection.execute("GRANT SELECT ON tickets TO " + self.ROLE)
            connection.execute("ALTER TABLE tickets ENABLE ROW LEVEL SECURITY")
            connection.execute("ALTER TABLE tickets FORCE ROW LEVEL SECURITY")
            connection.execute(
                "CREATE POLICY tickets_owner_only ON tickets FOR SELECT"
                " USING (owner_id = current_setting('hull.user_id', true))"
            )
            connection.execute(
                "INSERT INTO tickets (title, owner_id) VALUES"
                " ('A ticket one', 'userA'), ('A ticket two', 'userA'), ('B ticket', 'userB')"
            )
        parts = psycopg.conninfo.conninfo_to_dict(_PG_DSN)
        parts["user"] = self.ROLE
        parts["password"] = self.ROLE_PASSWORD
        self.role_dsn = psycopg.conninfo.make_conninfo(**parts)
        self.entity = entities.EntityRead.compile(
            "tickets", {"table": "tickets", "primary_key": "id", "columns": ["id", "title", "owner_id"]}
        )

    def test_user_a_only_sees_its_own_rows(self) -> None:
        rows = entities.read(
            dsn=self.role_dsn, entity=self.entity, user_id="userA", org_id=None, role="member",
        )
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["owner_id"] == "userA" for row in rows))

    def test_user_b_cannot_read_user_a_rows(self) -> None:
        rows = entities.read(
            dsn=self.role_dsn, entity=self.entity, user_id="userB", org_id=None, role="member",
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "B ticket")

    def test_unknown_user_sees_nothing(self) -> None:
        rows = entities.read(
            dsn=self.role_dsn, entity=self.entity, user_id="nobody", org_id=None, role="member",
        )
        self.assertEqual(rows, [])

    def test_equality_filter_on_declared_column(self) -> None:
        rows = entities.read(
            dsn=self.role_dsn, entity=self.entity, user_id="userA", org_id=None, role="member",
            filters={"title": "A ticket two"},
        )
        self.assertEqual([row["title"] for row in rows], ["A ticket two"])

    def test_results_ordered_by_primary_key_and_limited(self) -> None:
        rows = entities.read(
            dsn=self.role_dsn, entity=self.entity, user_id="userA", org_id=None, role="member", limit=1,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "A ticket one")

    def test_mutation_check_missing_guc_leaks_rows_across_users(self) -> None:
        # Prove the RLS-driving GUCs are load-bearing: connecting and querying
        # without ever setting hull.user_id must see nothing (current_setting with
        # missing=true defaults to '', which the policy's owner_id = '' never
        # matches for real data) rather than accidentally seeing everyone's rows.
        with psycopg.connect(self.role_dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute("SELECT id, title, owner_id FROM tickets ORDER BY id")
                rows = cursor.fetchall()
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
