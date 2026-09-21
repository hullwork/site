"""Broker persistence: one PostgreSQL database and a per-component migration ledger.

Two components own tables here ("core" and "runtime"). Each keeps its own strictly
increasing version sequence so the two can be developed side by side without fighting
over one number space.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass
from os import getenv
from typing import Any, Callable, Iterator

MigrationFn = Callable[[Any], None]


@dataclass(frozen=True)
class _Migration:
    component: str
    version: int
    description: str
    apply: MigrationFn


_MIGRATIONS: dict[tuple[str, int], _Migration] = {}


def register_migration(component: str, version: int, description: str,
                       apply: MigrationFn) -> None:
    """Register one schema step. Versions must be contiguous per component."""
    key = (component, version)
    existing = _MIGRATIONS.get(key)
    if existing is not None and existing.apply is not apply:
        raise RuntimeError(f"migration {component} v{version} is registered twice")
    previous = [v for (c, v) in _MIGRATIONS if c == component]
    if existing is None and previous and version != max(previous) + 1:
        raise RuntimeError(
            f"migration {component} v{version} does not follow v{max(previous)}"
        )
    if existing is None and not previous and version != 1:
        raise RuntimeError(f"migration {component} must start at v1, got v{version}")
    _MIGRATIONS[key] = _Migration(component, version, description, apply)


class BrokerDatabase:
    """Hands out one transaction at a time; callers never hold a connection."""

    def __init__(self, connect: Callable[[], Any]):
        self._connect = connect

    @classmethod
    def from_env(cls) -> "BrokerDatabase":
        dsn = (getenv("HULL_BROKER_DB_DSN", "") or "").strip()
        if not dsn:
            raise RuntimeError("HULL_BROKER_DB_DSN is required")
        import psycopg

        return cls(lambda: psycopg.connect(dsn))

    @contextlib.contextmanager
    def transaction(self) -> Iterator[Any]:
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                yield cursor
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS broker_schema_migrations (
    component TEXT NOT NULL,
    version INTEGER NOT NULL,
    description TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (component, version)
)
"""

# Several replicas may start at once; the transaction-scoped advisory lock makes the
# second one wait and then find nothing left to apply.
_MIGRATION_LOCK_KEY = 0x68756C6C  # "hull"


def migrate(db: BrokerDatabase) -> None:
    with db.transaction() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK_KEY,))
        cursor.execute(_LEDGER_DDL)
        cursor.execute("SELECT component, version FROM broker_schema_migrations")
        applied = {(row[0], int(row[1])) for row in cursor.fetchall()}
        for key in sorted(_MIGRATIONS):
            if key in applied:
                continue
            step = _MIGRATIONS[key]
            step.apply(cursor)
            cursor.execute(
                "INSERT INTO broker_schema_migrations (component, version, description)"
                " VALUES (%s, %s, %s)",
                (step.component, step.version, step.description),
            )
