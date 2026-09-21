"""The Broker schema is complete whichever process migrates it.

Every other broker test imports the runtime modules before it migrates, so they all
passed while the real gateway and worker, which migrate first, left the runtime tables
missing. These tests migrate from a fresh interpreter that has imported nothing else.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import textwrap
import unittest
import uuid
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
BROKER = ROOT / "src/sites/broker"
DSN = os.environ.get("SITES_TEST_PG_DSN", "").strip()

# Tables owned by the runtime half; each comes from a different module's migration.
RUNTIME_TABLES = ("broker_credit_reservations", "broker_tool_approvals", "broker_agent_threads")


@unittest.skipUnless(DSN, "SITES_TEST_PG_DSN is not set")
class FreshProcessMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = "hw_schema_" + uuid.uuid4().hex[:12]
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'CREATE SCHEMA "{self.schema}"')
        self.addCleanup(self._drop)

    def _drop(self) -> None:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')

    def test_migrate_all_creates_the_runtime_tables_in_a_fresh_process(self) -> None:
        script = textwrap.dedent(f"""
            import psycopg
            from sites.broker import db, schema
            dsn = {DSN!r}
            database = db.BrokerDatabase(
                lambda: psycopg.connect(dsn, options="-c search_path={self.schema}"))
            schema.migrate_all(database)
        """)
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                                cwd=ROOT, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        with psycopg.connect(DSN) as conn:
            for table in RUNTIME_TABLES:
                found = conn.execute("SELECT to_regclass(%s)", (f"{self.schema}.{table}",)).fetchone()[0]
                self.assertIsNotNone(found, f"{table} was not created by migrate_all")


class SingleMigrationEntryPointTests(unittest.TestCase):
    def test_only_schema_module_calls_db_migrate(self) -> None:
        callers = []
        for source in sorted(BROKER.glob("*.py")):
            if source.name in {"schema.py", "db.py"}:
                continue
            for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "migrate"):
                    callers.append(f"{source.name}:{node.lineno}")
        self.assertEqual(callers, [], "process entry points must migrate through schema.migrate_all")

    def test_the_check_above_can_see_a_direct_call(self) -> None:
        tree = ast.parse("broker_db.migrate(db)")
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute) and n.func.attr == "migrate"]
        self.assertEqual(len(calls), 1)

    def test_both_process_entry_points_use_migrate_all(self) -> None:
        for name in ("__main__.py", "worker.py"):
            text = (BROKER / name).read_text(encoding="utf-8")
            self.assertIn("schema.migrate_all(", text, name)


if __name__ == "__main__":
    unittest.main()
