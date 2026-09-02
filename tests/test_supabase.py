"""Managed PostgreSQL compatibility smoke tests.

The required test suite runs against local PostgreSQL. This small lane is safe to run
against a dedicated Supabase project because it only creates random test schemas and
never changes ``public``.
"""
from __future__ import annotations

import unittest

from sites.naming import token_digest
from tests.test_support import postgres_store


class ManagedPostgresSmokeTests(unittest.TestCase):
    def test_migration_and_schema_isolation(self) -> None:
        first = postgres_store("managed-postgres-first")
        second = postgres_store("managed-postgres-second")
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        first.migrate()
        second.migrate()

        first.create_merchant(
            "acme", "Acme", token_digest("managed-pg-key"), 10, 20
        )
        first.create_tenant(
            "acme",
            "alice",
            token_digest("managed-pg-tenant"),
            max_deployments=5,
            max_public_routes=1,
        )

        self.assertEqual(first.merchant("acme")["display_name"], "Acme")
        self.assertEqual(first.tenant("acme", "alice")["max_deployments"], 5)
        self.assertIsNone(second.merchant("acme"))
        self.assertIsNone(second.tenant("acme", "alice"))


if __name__ == "__main__":
    unittest.main()
