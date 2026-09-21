"""Credit ledger tests against real PostgreSQL.

Set HULL_TEST_PG_DSN to a throwaway database before running this module; every
test starts by dropping and recreating the "public" schema, so point it only at a
database that is safe to destroy (mirrors tests/test_tenancy.py's PostgresMigrationTests
convention: SITES_TEST_PG_DSN there, HULL_TEST_PG_DSN here per the partition D brief).

    HULL_TEST_PG_DSN='host=127.0.0.1 port=50771 dbname=hw_d user=postgres sslmode=disable' \\
        .venv/bin/python -m unittest tests.test_broker_ledger -v
"""
from __future__ import annotations

import os
import threading
import unittest

import psycopg

from sites.broker import ledger
from sites.broker.db import BrokerDatabase, migrate

_PG_DSN = os.environ.get("HULL_TEST_PG_DSN", "")


def _clock(seconds: float = 1_800_000_000.0):
    # 2027-01-13T... UTC, a fixed instant so period_for() is deterministic across a test.
    return lambda: seconds


@unittest.skipUnless(_PG_DSN, "HULL_TEST_PG_DSN is not set; see the module docstring")
class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        with psycopg.connect(_PG_DSN, autocommit=True) as connection:
            connection.execute("DROP SCHEMA public CASCADE")
            connection.execute("CREATE SCHEMA public")
        self.db = BrokerDatabase(lambda: psycopg.connect(_PG_DSN))
        migrate(self.db)
        self.ledger = ledger.Ledger(self.db, _clock())

    def test_reserve_then_settle_frees_the_unused_hold(self) -> None:
        reservation = self.ledger.reserve(
            app_env_id="app1", scope="flow:summarize", amount=10,
            user_subject=("user", "u1"), user_limit=100,
        )
        self.assertEqual(reservation.status, "reserved")
        self.assertFalse(reservation.replayed)

        settled = self.ledger.settle(reservation.id, app_env_id="app1", actual_amount=4)
        self.assertEqual(settled.status, "settled")
        self.assertEqual(settled.settled_amount, 4)

        used = self.ledger.usage("app1", "user", "u1", self.ledger.period_for())
        self.assertEqual(used, 4)

        # The other 6 credits are back in "available": a second reservation for
        # 96 credits (4 already settled + 96 = 100, the limit) must succeed.
        second = self.ledger.reserve(
            app_env_id="app1", scope="flow:summarize", amount=96,
            user_subject=("user", "u1"), user_limit=100,
        )
        self.assertEqual(second.status, "reserved")

    def test_settle_excess_clamps_to_the_reservation(self) -> None:
        reservation = self.ledger.reserve(
            app_env_id="app1", scope="flow:x", amount=5, user_subject=("user", "u1"), user_limit=50,
        )
        settled = self.ledger.settle(reservation.id, app_env_id="app1", actual_amount=9)
        self.assertEqual(settled.settled_amount, 5)  # clamped, never charges past the hold
        used = self.ledger.usage("app1", "user", "u1", self.ledger.period_for())
        self.assertEqual(used, 5)

    def test_release_returns_the_full_hold(self) -> None:
        reservation = self.ledger.reserve(
            app_env_id="app1", scope="flow:x", amount=20, user_subject=("user", "u1"), user_limit=20,
        )
        # The whole limit is held; a second reservation must fail until released.
        with self.assertRaises(ledger.BudgetExceeded):
            self.ledger.reserve(
                app_env_id="app1", scope="flow:x", amount=1, user_subject=("user", "u1"), user_limit=20,
            )
        released = self.ledger.release(reservation.id, app_env_id="app1")
        self.assertEqual(released.status, "released")
        again = self.ledger.reserve(
            app_env_id="app1", scope="flow:x", amount=20, user_subject=("user", "u1"), user_limit=20,
        )
        self.assertEqual(again.status, "reserved")

    def test_user_level_budget_exceeded(self) -> None:
        with self.assertRaises(ledger.BudgetExceeded) as ctx:
            self.ledger.reserve(
                app_env_id="app1", scope="flow:x", amount=51,
                user_subject=("user", "u1"), user_limit=50,
            )
        self.assertEqual(ctx.exception.level, "user")

    def test_org_subject_used_instead_of_user_when_present(self) -> None:
        # Two users of the same org share the org's pool.
        self.ledger.reserve(
            app_env_id="app1", scope="flow:x", amount=30, user_subject=("org", "o1"), user_limit=40,
        )
        with self.assertRaises(ledger.BudgetExceeded) as ctx:
            self.ledger.reserve(
                app_env_id="app1", scope="flow:x", amount=20, user_subject=("org", "o1"), user_limit=40,
            )
        self.assertEqual(ctx.exception.level, "org")

    def test_app_level_budget_exceeded_independent_of_user_limit(self) -> None:
        # A generous per-user limit does not bypass a tighter app-wide cap.
        with self.assertRaises(ledger.BudgetExceeded) as ctx:
            self.ledger.reserve(
                app_env_id="app1", scope="flow:x", amount=11,
                user_subject=("user", "u1"), user_limit=1000, app_limit=10,
            )
        self.assertEqual(ctx.exception.level, "app")

    def test_call_cap_is_a_pure_comparison_no_db_round_trip_needed(self) -> None:
        with self.assertRaises(ledger.BudgetExceeded) as ctx:
            self.ledger.reserve(
                app_env_id="app1", scope="flow:x", amount=31,
                user_subject=("user", "u1"), user_limit=1000, call_cap=30,
            )
        self.assertEqual(ctx.exception.level, "call")

    def test_run_as_app_has_no_user_subject_only_app_limit_applies(self) -> None:
        reservation = self.ledger.reserve(
            app_env_id="app1", scope="agent:triage", amount=5, app_limit=5,
        )
        self.assertEqual(reservation.status, "reserved")
        with self.assertRaises(ledger.BudgetExceeded) as ctx:
            self.ledger.reserve(app_env_id="app1", scope="agent:triage", amount=1, app_limit=5)
        self.assertEqual(ctx.exception.level, "app")

    def test_idempotent_replay_returns_the_same_reservation_no_double_charge(self) -> None:
        first = self.ledger.reserve(
            app_env_id="app1", scope="flow:x", amount=10, idempotency_key="k1",
            user_subject=("user", "u1"), user_limit=10,
        )
        self.ledger.settle(first.id, app_env_id="app1", actual_amount=7, response={"ok": True})

        replay = self.ledger.reserve(
            app_env_id="app1", scope="flow:x", amount=10, idempotency_key="k1",
            user_subject=("user", "u1"), user_limit=10,
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.id, first.id)
        self.assertEqual(replay.status, "settled")
        self.assertEqual(replay.response, {"ok": True})

        # Only one settlement ever happened: usage reflects 7, not 14.
        used = self.ledger.usage("app1", "user", "u1", self.ledger.period_for())
        self.assertEqual(used, 7)

    def test_idempotency_key_is_scoped_per_flow_not_global(self) -> None:
        a = self.ledger.reserve(
            app_env_id="app1", scope="flow:a", amount=5, idempotency_key="shared",
            user_subject=("user", "u1"), user_limit=100,
        )
        b = self.ledger.reserve(
            app_env_id="app1", scope="flow:b", amount=5, idempotency_key="shared",
            user_subject=("user", "u1"), user_limit=100,
        )
        self.assertNotEqual(a.id, b.id)
        self.assertFalse(b.replayed)

    def test_concurrent_reservations_race_for_the_last_credits_exactly_one_wins(self) -> None:
        # Real threads, real PostgreSQL connections (BrokerDatabase opens one
        # connection per transaction), racing for a limit that only one amount fits.
        results: list[str] = []
        lock = threading.Lock()
        barrier = threading.Barrier(2)

        def attempt(scope: str) -> None:
            barrier.wait()
            try:
                self.ledger.reserve(
                    app_env_id="app1", scope=scope, amount=60,
                    user_subject=("user", "racer"), user_limit=100,
                )
                with lock:
                    results.append("ok")
            except ledger.BudgetExceeded:
                with lock:
                    results.append("exceeded")

        threads = [
            threading.Thread(target=attempt, args=(f"flow:race{i}",)) for i in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(sorted(results), ["exceeded", "ok"])

    def test_usage_since_reports_settled_credits_and_call_counts_per_user(self) -> None:
        import datetime as dt

        r1 = self.ledger.reserve(app_env_id="app1", scope="flow:x", amount=10, user_subject=("user", "u1"))
        self.ledger.settle(r1.id, app_env_id="app1", actual_amount=6)
        r2 = self.ledger.reserve(app_env_id="app1", scope="flow:y", amount=10, user_subject=("user", "u1"))
        self.ledger.settle(r2.id, app_env_id="app1", actual_amount=3)
        r3 = self.ledger.reserve(app_env_id="app1", scope="flow:x", amount=10, user_subject=("user", "u2"))
        self.ledger.settle(r3.id, app_env_id="app1", actual_amount=1)

        rows = self.ledger.usage_since("app1", dt.date(2000, 1, 1))
        by_user = {row["userId"]: row for row in rows}
        self.assertEqual(by_user["u1"]["credits"], 9)
        self.assertEqual(by_user["u1"]["calls"], 2)
        self.assertEqual(by_user["u2"]["credits"], 1)
        self.assertEqual(by_user["u2"]["calls"], 1)

    def test_settling_twice_is_a_no_op(self) -> None:
        reservation = self.ledger.reserve(app_env_id="app1", scope="flow:x", amount=10, user_subject=("user", "u1"))
        self.ledger.settle(reservation.id, app_env_id="app1", actual_amount=5)
        self.ledger.settle(reservation.id, app_env_id="app1", actual_amount=5)
        used = self.ledger.usage("app1", "user", "u1", self.ledger.period_for())
        self.assertEqual(used, 5)

    def test_releasing_a_settled_reservation_is_a_no_op(self) -> None:
        reservation = self.ledger.reserve(app_env_id="app1", scope="flow:x", amount=10, user_subject=("user", "u1"))
        self.ledger.settle(reservation.id, app_env_id="app1", actual_amount=5)
        result = self.ledger.release(reservation.id, app_env_id="app1")
        self.assertEqual(result.status, "settled")
        used = self.ledger.usage("app1", "user", "u1", self.ledger.period_for())
        self.assertEqual(used, 5)

    def test_reservation_not_found(self) -> None:
        with self.assertRaises(ledger.ReservationNotFound):
            self.ledger.settle("rmissing", app_env_id="app1", actual_amount=1)
        with self.assertRaises(ledger.ReservationNotFound):
            self.ledger.release("rmissing", app_env_id="app1")

    def test_migration_registers_at_version_1_of_the_runtime_component(self) -> None:
        # Only asserts ledger's own step, not exclusivity: when this module runs
        # as part of the full discovered suite, tools.py (v2) and agent_bridge.py
        # (v3) are import-cached into the same process and register too — the
        # per-component contiguity check in db.py is what actually proves versions
        # don't collide, not this test.
        with self.db.transaction() as cursor:
            cursor.execute(
                "SELECT version, description FROM broker_schema_migrations"
                " WHERE component = 'runtime' AND version = 1"
            )
            rows = cursor.fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIn("credit ledger", rows[0][1])


if __name__ == "__main__":
    unittest.main()
