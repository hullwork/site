"""Broker persistence: one PostgreSQL database and a per-component migration ledger.

Two components own tables here ("core" and "runtime"). Each keeps its own strictly
increasing version sequence so the two can be developed side by side without fighting
over one number space.

Every table below carries ``app_env_id`` and the helper functions in this module take
it as their first argument after the cursor, so a query that forgets to filter by it
has to go out of its way to do so (call the low-level cursor directly instead of one
of these).
"""
from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from datetime import datetime
from os import getenv
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

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
        """Build a connector from ``HULL_BROKER_DB_DSN`` or the ``HULL_BROKER_DB_*``
        variables, which mirror ``SITES_DB_*`` field for field (see
        ``sites.storage.DatabaseConfig.from_env``): host/port/name/user/
        password(_FILE)/sslmode/connect+statement timeout. The DSN, when set, wins
        outright -- it is meant for tests and for operators who already assemble a
        libpq connection string elsewhere; the pieces below are meant for the chart.
        """
        import psycopg

        dsn = (getenv("HULL_BROKER_DB_DSN", "") or "").strip()
        if dsn:
            return cls(lambda: psycopg.connect(dsn))

        password = (getenv("HULL_BROKER_DB_PASSWORD", "") or "").strip()
        password_file = (getenv("HULL_BROKER_DB_PASSWORD_FILE", "") or "").strip()
        if password_file:
            try:
                password = Path(password_file).read_text().strip()
            except OSError as exc:
                raise RuntimeError(
                    "HULL_BROKER_DB_PASSWORD_FILE is set but unreadable"
                ) from exc
        if not password:
            raise RuntimeError(
                "HULL_BROKER_DB_DSN or HULL_BROKER_DB_PASSWORD(_FILE) is required"
            )
        try:
            port = int(getenv("HULL_BROKER_DB_PORT", "5432") or "5432")
            connect_timeout = int(getenv("HULL_BROKER_DB_CONNECT_TIMEOUT", "5") or "5")
            statement_timeout = int(
                getenv("HULL_BROKER_DB_STATEMENT_TIMEOUT", "10") or "10"
            )
        except ValueError as exc:
            raise RuntimeError(
                "HULL_BROKER_DB_PORT/CONNECT_TIMEOUT/STATEMENT_TIMEOUT must be integers"
            ) from exc
        sslmode = getenv("HULL_BROKER_DB_SSLMODE", "require") or "require"
        if sslmode not in {"disable", "require", "verify-ca", "verify-full"}:
            raise RuntimeError(f"HULL_BROKER_DB_SSLMODE is invalid: {sslmode!r}")
        kwargs = {
            "host": getenv("HULL_BROKER_DB_HOST", "sites-postgres") or "sites-postgres",
            "port": port,
            "dbname": getenv("HULL_BROKER_DB_NAME", "hull_broker") or "hull_broker",
            "user": getenv("HULL_BROKER_DB_USER", "hull_broker") or "hull_broker",
            "password": password,
            "connect_timeout": connect_timeout,
            "application_name": "hull-broker",
            "options": f"-c statement_timeout={statement_timeout * 1000}",
            "sslmode": sslmode,
        }
        return cls(lambda: psycopg.connect(**kwargs))

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


# --------------------------------------------------------------------------------
# Core schema (component "core", owned by partition C)
# --------------------------------------------------------------------------------

_CORE_V1_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS broker_apps (
        app_env_id TEXT PRIMARY KEY CHECK (app_env_id ~ '^[0-9a-f]{32}$'),
        active_version INTEGER,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS broker_app_configs (
        app_env_id TEXT NOT NULL REFERENCES broker_apps(app_env_id),
        version INTEGER NOT NULL,
        digest TEXT NOT NULL,
        config JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (app_env_id, version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS broker_app_settings (
        app_env_id TEXT PRIMARY KEY REFERENCES broker_apps(app_env_id),
        settings JSONB NOT NULL DEFAULT '{}'::jsonb,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS broker_app_hosts (
        host TEXT PRIMARY KEY,
        app_env_id TEXT NOT NULL REFERENCES broker_apps(app_env_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS broker_app_workloads (
        namespace TEXT NOT NULL,
        service_account TEXT NOT NULL,
        app_env_id TEXT NOT NULL REFERENCES broker_apps(app_env_id),
        PRIMARY KEY (namespace, service_account)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS broker_app_secrets (
        app_env_id TEXT NOT NULL REFERENCES broker_apps(app_env_id),
        name TEXT NOT NULL,
        ciphertext BYTEA NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (app_env_id, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS broker_end_users (
        id TEXT PRIMARY KEY,
        app_env_id TEXT NOT NULL REFERENCES broker_apps(app_env_id),
        email TEXT NOT NULL,
        org_id TEXT,
        role TEXT NOT NULL,
        plan TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (app_env_id, email)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS broker_sessions (
        token_sha256 TEXT PRIMARY KEY,
        app_env_id TEXT NOT NULL REFERENCES broker_apps(app_env_id),
        user_id TEXT NOT NULL REFERENCES broker_end_users(id),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        expires_at TIMESTAMPTZ NOT NULL,
        revoked_at TIMESTAMPTZ
    )
    """,
    "CREATE INDEX IF NOT EXISTS broker_sessions_expires_idx ON broker_sessions (expires_at)",
    """
    CREATE TABLE IF NOT EXISTS broker_otp_codes (
        app_env_id TEXT NOT NULL REFERENCES broker_apps(app_env_id),
        email TEXT NOT NULL,
        code_hmac TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        expires_at TIMESTAMPTZ NOT NULL,
        consumed_at TIMESTAMPTZ,
        PRIMARY KEY (app_env_id, email)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS broker_rate_limits (
        app_env_id TEXT NOT NULL,
        scope TEXT NOT NULL,
        bucket_key TEXT NOT NULL,
        window_start TIMESTAMPTZ NOT NULL,
        count INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (app_env_id, scope, bucket_key, window_start)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS broker_outbound_mail (
        id BIGSERIAL PRIMARY KEY,
        app_env_id TEXT NOT NULL REFERENCES broker_apps(app_env_id),
        to_email TEXT NOT NULL,
        subject TEXT NOT NULL,
        body TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
)


def _migrate_core_v1(cursor: Any) -> None:
    for statement in _CORE_V1_STATEMENTS:
        cursor.execute(statement)


register_migration("core", 1, "core: apps, configs, settings, secrets, end users, "
                    "sessions, otp codes, rate limits, outbound mail", _migrate_core_v1)


# --------------------------------------------------------------------------------
# Data access helpers. Every function takes app_env_id right after the cursor, so a
# call site that means to scope a query has nowhere to leave that argument out.
# --------------------------------------------------------------------------------


def ensure_app(cursor: Any, app_env_id: str) -> None:
    cursor.execute(
        "INSERT INTO broker_apps (app_env_id) VALUES (%s) "
        "ON CONFLICT (app_env_id) DO NOTHING",
        (app_env_id,),
    )


def get_active_version(cursor: Any, app_env_id: str) -> int | None:
    cursor.execute(
        "SELECT active_version FROM broker_apps WHERE app_env_id = %s", (app_env_id,)
    )
    row = cursor.fetchone()
    return None if row is None else row[0]


def get_active_version_for_update(cursor: Any, app_env_id: str) -> int | None:
    """Same as :func:`get_active_version`, but locks the row until the caller's
    transaction ends. Two concurrent ``apply`` calls both read a consistent
    "current" version this way instead of a race where both see the version
    before either committed -- which is what makes exactly one of them land on
    a stale digest.
    """
    cursor.execute(
        "SELECT active_version FROM broker_apps WHERE app_env_id = %s FOR UPDATE",
        (app_env_id,),
    )
    row = cursor.fetchone()
    return None if row is None else row[0]


def get_config_version(cursor: Any, app_env_id: str, version: int) -> dict | None:
    cursor.execute(
        "SELECT version, digest, config FROM broker_app_configs "
        "WHERE app_env_id = %s AND version = %s",
        (app_env_id, version),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return {"version": row[0], "digest": row[1], "config": _as_dict(row[2])}


def insert_config_version(cursor: Any, app_env_id: str, version: int, digest: str,
                           config: dict) -> None:
    ensure_app(cursor, app_env_id)
    cursor.execute(
        "INSERT INTO broker_app_configs (app_env_id, version, digest, config) "
        "VALUES (%s, %s, %s, %s)",
        (app_env_id, version, digest, json.dumps(config, sort_keys=True)),
    )


def set_active_version(cursor: Any, app_env_id: str, version: int) -> None:
    cursor.execute(
        "UPDATE broker_apps SET active_version = %s, updated_at = now() "
        "WHERE app_env_id = %s",
        (version, app_env_id),
    )


def get_settings(cursor: Any, app_env_id: str) -> dict:
    cursor.execute(
        "SELECT settings FROM broker_app_settings WHERE app_env_id = %s", (app_env_id,)
    )
    row = cursor.fetchone()
    return {} if row is None else _as_dict(row[0])


class SettingConflict(Exception):
    """A host or workload in the settings already belongs to another app."""

    def __init__(self, kind: str, value: str) -> None:
        super().__init__(f"{kind} {value!r} belongs to another app")
        self.kind = kind
        self.value = value


def put_settings(cursor: Any, app_env_id: str, settings: dict) -> None:
    ensure_app(cursor, app_env_id)
    # Hosts and workloads decide which app a request or a workload is; letting a
    # later write move one to itself would let any app take over another's domain.
    # Freeing one requires removing it from the owning app's settings first.
    for host in settings.get("hosts") or []:
        cursor.execute("SELECT app_env_id FROM broker_app_hosts WHERE host = %s", (host,))
        row = cursor.fetchone()
        if row is not None and row[0] != app_env_id:
            raise SettingConflict("host", host)
    for workload in settings.get("workloads") or []:
        namespace, service_account = workload.get("namespace"), workload.get("serviceAccount")
        if not namespace or not service_account:
            continue
        cursor.execute(
            "SELECT app_env_id FROM broker_app_workloads WHERE namespace = %s AND service_account = %s",
            (namespace, service_account),
        )
        row = cursor.fetchone()
        if row is not None and row[0] != app_env_id:
            raise SettingConflict("workload", f"{namespace}/{service_account}")
    cursor.execute(
        "INSERT INTO broker_app_settings (app_env_id, settings) VALUES (%s, %s) "
        "ON CONFLICT (app_env_id) DO UPDATE SET settings = EXCLUDED.settings, "
        "updated_at = now()",
        (app_env_id, json.dumps(settings, sort_keys=True)),
    )
    cursor.execute("DELETE FROM broker_app_hosts WHERE app_env_id = %s", (app_env_id,))
    for host in settings.get("hosts") or []:
        cursor.execute(
            "INSERT INTO broker_app_hosts (host, app_env_id) VALUES (%s, %s) "
            "ON CONFLICT (host) DO UPDATE SET app_env_id = EXCLUDED.app_env_id",
            (host, app_env_id),
        )
    cursor.execute(
        "DELETE FROM broker_app_workloads WHERE app_env_id = %s", (app_env_id,)
    )
    for workload in settings.get("workloads") or []:
        namespace = workload.get("namespace")
        service_account = workload.get("serviceAccount")
        if not namespace or not service_account:
            continue
        cursor.execute(
            "INSERT INTO broker_app_workloads (namespace, service_account, app_env_id) "
            "VALUES (%s, %s, %s) ON CONFLICT (namespace, service_account) "
            "DO UPDATE SET app_env_id = EXCLUDED.app_env_id",
            (namespace, service_account, app_env_id),
        )


def app_env_id_for_host(cursor: Any, host: str) -> str | None:
    cursor.execute("SELECT app_env_id FROM broker_app_hosts WHERE host = %s", (host,))
    row = cursor.fetchone()
    return None if row is None else row[0]


def app_env_id_for_workload(cursor: Any, namespace: str, service_account: str) -> str | None:
    cursor.execute(
        "SELECT app_env_id FROM broker_app_workloads "
        "WHERE namespace = %s AND service_account = %s",
        (namespace, service_account),
    )
    row = cursor.fetchone()
    return None if row is None else row[0]


def put_secret(cursor: Any, app_env_id: str, name: str, ciphertext: bytes) -> None:
    ensure_app(cursor, app_env_id)
    cursor.execute(
        "INSERT INTO broker_app_secrets (app_env_id, name, ciphertext) "
        "VALUES (%s, %s, %s) ON CONFLICT (app_env_id, name) "
        "DO UPDATE SET ciphertext = EXCLUDED.ciphertext, updated_at = now()",
        (app_env_id, name, ciphertext),
    )


def get_secret(cursor: Any, app_env_id: str, name: str) -> bytes | None:
    cursor.execute(
        "SELECT ciphertext FROM broker_app_secrets WHERE app_env_id = %s AND name = %s",
        (app_env_id, name),
    )
    row = cursor.fetchone()
    return None if row is None else bytes(row[0])


def find_user_by_email(cursor: Any, app_env_id: str, email: str) -> dict | None:
    cursor.execute(
        "SELECT id, org_id, role, plan FROM broker_end_users "
        "WHERE app_env_id = %s AND email = %s",
        (app_env_id, email),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return {"id": row[0], "email": email, "org": row[1], "role": row[2], "plan": row[3]}


def get_user(cursor: Any, app_env_id: str, user_id: str) -> dict | None:
    cursor.execute(
        "SELECT id, email, org_id, role, plan FROM broker_end_users "
        "WHERE app_env_id = %s AND id = %s",
        (app_env_id, user_id),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return {"id": row[0], "email": row[1], "org": row[2], "role": row[3], "plan": row[4]}


def create_user(cursor: Any, app_env_id: str, user_id: str, email: str,
                 org: str | None, role: str, plan: str) -> None:
    ensure_app(cursor, app_env_id)
    cursor.execute(
        "INSERT INTO broker_end_users (id, app_env_id, email, org_id, role, plan) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (user_id, app_env_id, email, org, role, plan),
    )


def set_user_plan(cursor: Any, app_env_id: str, user_id: str, plan: str) -> bool:
    cursor.execute(
        "UPDATE broker_end_users SET plan = %s "
        "WHERE app_env_id = %s AND id = %s",
        (plan, app_env_id, user_id),
    )
    return cursor.rowcount > 0


def create_session(cursor: Any, app_env_id: str, token_sha256: str, user_id: str,
                    expires_at: datetime) -> None:
    cursor.execute(
        "INSERT INTO broker_sessions (token_sha256, app_env_id, user_id, expires_at) "
        "VALUES (%s, %s, %s, %s)",
        (token_sha256, app_env_id, user_id, expires_at),
    )


def get_session(cursor: Any, app_env_id: str, token_sha256: str) -> dict | None:
    cursor.execute(
        "SELECT user_id, expires_at, revoked_at FROM broker_sessions "
        "WHERE app_env_id = %s AND token_sha256 = %s",
        (app_env_id, token_sha256),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return {"user_id": row[0], "expires_at": row[1], "revoked_at": row[2]}


def revoke_session(cursor: Any, app_env_id: str, token_sha256: str) -> None:
    cursor.execute(
        "UPDATE broker_sessions SET revoked_at = now() "
        "WHERE app_env_id = %s AND token_sha256 = %s AND revoked_at IS NULL",
        (app_env_id, token_sha256),
    )


def delete_expired_sessions(cursor: Any, before: datetime) -> int:
    cursor.execute("DELETE FROM broker_sessions WHERE expires_at < %s", (before,))
    return cursor.rowcount


def upsert_otp(cursor: Any, app_env_id: str, email: str, code_hmac: str,
                expires_at: datetime) -> None:
    ensure_app(cursor, app_env_id)
    cursor.execute(
        "INSERT INTO broker_otp_codes (app_env_id, email, code_hmac, attempts, "
        "expires_at) VALUES (%s, %s, %s, 0, %s) "
        "ON CONFLICT (app_env_id, email) DO UPDATE SET code_hmac = EXCLUDED.code_hmac, "
        "attempts = 0, created_at = now(), expires_at = EXCLUDED.expires_at, "
        "consumed_at = NULL",
        (app_env_id, email, code_hmac, expires_at),
    )


def get_otp(cursor: Any, app_env_id: str, email: str) -> dict | None:
    cursor.execute(
        "SELECT code_hmac, attempts, expires_at, consumed_at FROM broker_otp_codes "
        "WHERE app_env_id = %s AND email = %s",
        (app_env_id, email),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return {"code_hmac": row[0], "attempts": row[1], "expires_at": row[2],
            "consumed_at": row[3]}


def increment_otp_attempts(cursor: Any, app_env_id: str, email: str) -> None:
    cursor.execute(
        "UPDATE broker_otp_codes SET attempts = attempts + 1 "
        "WHERE app_env_id = %s AND email = %s",
        (app_env_id, email),
    )


def consume_otp(cursor: Any, app_env_id: str, email: str) -> None:
    cursor.execute(
        "UPDATE broker_otp_codes SET consumed_at = now() "
        "WHERE app_env_id = %s AND email = %s",
        (app_env_id, email),
    )


def delete_expired_otps(cursor: Any, before: datetime) -> int:
    cursor.execute("DELETE FROM broker_otp_codes WHERE expires_at < %s", (before,))
    return cursor.rowcount


def rate_limit_increment(cursor: Any, app_env_id: str, scope: str, key: str,
                          window_start: datetime, limit: int) -> bool:
    """Increment the bucket for this window and report whether the limit held.

    Returns True when the caller is still within the limit (and the increment counts
    against it), False when this call would exceed it (the increment still happened,
    so a caller that retries a refused action does not get a free extra attempt).
    """
    cursor.execute(
        "INSERT INTO broker_rate_limits (app_env_id, scope, bucket_key, window_start, "
        "count) VALUES (%s, %s, %s, %s, 1) "
        "ON CONFLICT (app_env_id, scope, bucket_key, window_start) "
        "DO UPDATE SET count = broker_rate_limits.count + 1 "
        "RETURNING count",
        (app_env_id, scope, key, window_start),
    )
    count = cursor.fetchone()[0]
    return count <= limit


def delete_expired_rate_limits(cursor: Any, before: datetime) -> int:
    cursor.execute("DELETE FROM broker_rate_limits WHERE window_start < %s", (before,))
    return cursor.rowcount


def insert_outbound_mail(cursor: Any, app_env_id: str, to_email: str, subject: str,
                          body: str) -> None:
    ensure_app(cursor, app_env_id)
    cursor.execute(
        "INSERT INTO broker_outbound_mail (app_env_id, to_email, subject, body) "
        "VALUES (%s, %s, %s, %s)",
        (app_env_id, to_email, subject, body),
    )


def list_outbound_mail(cursor: Any, app_env_id: str, limit: int = 100) -> list[dict]:
    cursor.execute(
        "SELECT id, to_email, subject, body, created_at FROM broker_outbound_mail "
        "WHERE app_env_id = %s ORDER BY id DESC LIMIT %s",
        (app_env_id, limit),
    )
    return [
        {"id": row[0], "to": row[1], "subject": row[2], "body": row[3],
         "createdAt": row[4].isoformat()}
        for row in cursor.fetchall()
    ]


def _as_dict(value: Any) -> dict:
    """psycopg returns JSONB already decoded; tests sometimes pass raw JSON text."""
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)
