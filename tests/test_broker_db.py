"""Tests for sites.broker.db: schema, migrations, and the app_env_id-scoped
data-access helpers.
"""
from __future__ import annotations

import secrets
import unittest
from datetime import datetime, timedelta, timezone

from sites.broker import db as broker_db

from tests import broker_fixtures as fx


def _unique(prefix: str) -> str:
    # broker_end_users.id and broker_sessions.token_sha256 are global primary
    # keys (by design -- ids are meant to be unguessable across every app), so
    # a fixed literal here collides with a previous, uncleaned test run instead
    # of only with concurrent tests in this one.
    return f"{prefix}-{secrets.token_hex(8)}"


class MigrationLedgerTests(unittest.TestCase):
    def test_register_migration_rejects_a_gap(self) -> None:
        broker_db._MIGRATIONS.pop(("gap_test", 1), None)
        broker_db._MIGRATIONS.pop(("gap_test", 2), None)
        with self.assertRaises(RuntimeError):
            broker_db.register_migration("gap_test", 2, "skips v1", lambda cursor: None)

    def test_register_migration_must_start_at_one(self) -> None:
        broker_db._MIGRATIONS.pop(("start_test", 2), None)
        with self.assertRaises(RuntimeError):
            broker_db.register_migration("start_test", 2, "starts at 2", lambda cursor: None)

    def test_migrate_is_idempotent(self) -> None:
        db = fx.database()
        broker_db.migrate(db)  # second call must not re-apply or raise
        with db.transaction() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM broker_schema_migrations WHERE component = 'core'"
            )
            self.assertEqual(cursor.fetchone()[0], 1)


class AppScopedAccessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = fx.database()
        self.app_env_id = fx.app_env_id()
        self.other_app_env_id = fx.app_env_id()

    def test_config_versions_are_scoped_by_app_env_id(self) -> None:
        with self.db.transaction() as cursor:
            broker_db.insert_config_version(cursor, self.app_env_id, 1, "digest-a", {"a": 1})
            broker_db.insert_config_version(cursor, self.other_app_env_id, 1, "digest-b", {"b": 1})
        with self.db.transaction() as cursor:
            row = broker_db.get_config_version(cursor, self.app_env_id, 1)
            other = broker_db.get_config_version(cursor, self.other_app_env_id, 1)
        self.assertEqual(row["config"], {"a": 1})
        self.assertEqual(other["config"], {"b": 1})

    def test_settings_sync_host_and_workload_lookup_tables(self) -> None:
        with self.db.transaction() as cursor:
            broker_db.put_settings(cursor, self.app_env_id, {
                "hosts": [f"{self.app_env_id[:8]}-one.example.test", f"{self.app_env_id[:8]}-two.example.test"],
                "workloads": [{"namespace": f"ns-{self.app_env_id[:12]}", "serviceAccount": "sa-a"}],
            })
        with self.db.transaction() as cursor:
            self.assertEqual(
                broker_db.app_env_id_for_host(cursor, f"{self.app_env_id[:8]}-one.example.test"), self.app_env_id
            )
            self.assertEqual(
                broker_db.app_env_id_for_host(cursor, f"{self.app_env_id[:8]}-two.example.test"), self.app_env_id
            )
            self.assertIsNone(broker_db.app_env_id_for_host(cursor, "unknown.example.test"))
            self.assertEqual(
                broker_db.app_env_id_for_workload(cursor, f"ns-{self.app_env_id[:12]}", "sa-a"), self.app_env_id
            )
        # Replacing settings drops hosts/workloads that are no longer named.
        with self.db.transaction() as cursor:
            broker_db.put_settings(cursor, self.app_env_id, {"hosts": [f"{self.app_env_id[:8]}-two.example.test"]})
        with self.db.transaction() as cursor:
            self.assertIsNone(broker_db.app_env_id_for_host(cursor, f"{self.app_env_id[:8]}-one.example.test"))
            self.assertEqual(
                broker_db.app_env_id_for_host(cursor, f"{self.app_env_id[:8]}-two.example.test"), self.app_env_id
            )
            self.assertIsNone(broker_db.app_env_id_for_workload(cursor, f"ns-{self.app_env_id[:12]}", "sa-a"))

    def test_secrets_round_trip_ciphertext_bytes(self) -> None:
        with self.db.transaction() as cursor:
            broker_db.put_secret(cursor, self.app_env_id, "TOKEN", b"\x01opaque-ciphertext")
        with self.db.transaction() as cursor:
            self.assertEqual(
                broker_db.get_secret(cursor, self.app_env_id, "TOKEN"), b"\x01opaque-ciphertext"
            )
            self.assertIsNone(broker_db.get_secret(cursor, self.other_app_env_id, "TOKEN"))

    def test_session_and_otp_lifecycle(self) -> None:
        user_id, token_sha256 = _unique("u"), _unique("sha")
        with self.db.transaction() as cursor:
            broker_db.create_user(cursor, self.app_env_id, user_id, "a@example.com", None,
                                   "member", "free")
            now = datetime.now(timezone.utc)
            broker_db.create_session(cursor, self.app_env_id, token_sha256, user_id,
                                      now + timedelta(hours=1))
        with self.db.transaction() as cursor:
            record = broker_db.get_session(cursor, self.app_env_id, token_sha256)
            self.assertEqual(record["user_id"], user_id)
            self.assertIsNone(record["revoked_at"])
        with self.db.transaction() as cursor:
            broker_db.revoke_session(cursor, self.app_env_id, token_sha256)
        with self.db.transaction() as cursor:
            record = broker_db.get_session(cursor, self.app_env_id, token_sha256)
            self.assertIsNotNone(record["revoked_at"])

    def test_delete_expired_sessions_and_otps(self) -> None:
        user_id, token_sha256 = _unique("u"), _unique("sha")
        email = f"{_unique('otp')}@example.com"
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        with self.db.transaction() as cursor:
            broker_db.create_user(cursor, self.app_env_id, user_id, email, None,
                                   "member", "free")
            broker_db.create_session(cursor, self.app_env_id, token_sha256, user_id, past)
            broker_db.upsert_otp(cursor, self.app_env_id, email, "hmac", past)
        with self.db.transaction() as cursor:
            now = datetime.now(timezone.utc)
            self.assertGreaterEqual(broker_db.delete_expired_sessions(cursor, now), 1)
            self.assertGreaterEqual(broker_db.delete_expired_otps(cursor, now), 1)
        with self.db.transaction() as cursor:
            self.assertIsNone(broker_db.get_session(cursor, self.app_env_id, token_sha256))
            self.assertIsNone(broker_db.get_otp(cursor, self.app_env_id, email))

    def test_rate_limit_increment_enforces_the_limit_but_keeps_counting(self) -> None:
        window = datetime.now(timezone.utc)
        with self.db.transaction() as cursor:
            results = [
                broker_db.rate_limit_increment(cursor, self.app_env_id, "scope", "key",
                                                window, limit=2)
                for _ in range(4)
            ]
        self.assertEqual(results, [True, True, False, False])

    def test_user_plan_update_reports_whether_a_row_existed(self) -> None:
        user_id = _unique("u")
        with self.db.transaction() as cursor:
            broker_db.create_user(cursor, self.app_env_id, user_id, "c@example.com", None,
                                   "member", "free")
        with self.db.transaction() as cursor:
            self.assertTrue(broker_db.set_user_plan(cursor, self.app_env_id, user_id, "pro"))
            self.assertFalse(
                broker_db.set_user_plan(cursor, self.app_env_id, "no-such-user", "pro")
            )
        with self.db.transaction() as cursor:
            self.assertEqual(
                broker_db.get_user(cursor, self.app_env_id, user_id)["plan"], "pro"
            )


if __name__ == "__main__":
    unittest.main()
