"""Entity read executor: a bounded SELECT against the app's own PostgreSQL schema.

The app owns row-level security on its tables; this module's job is to make sure
every read goes through the app's RLS by setting the ``hull.user_id`` /
``hull.org_id`` / ``hull.role`` session GUCs inside the same transaction as the
query, and to never build SQL out of anything but identifiers the compiled config
already declared. Filter *values* are always bound as parameters; filter *keys*,
the table name, the primary key and every selected column come only from the
entity's compiled config (contract §3.6), never from the caller's request body.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

import psycopg
from psycopg import sql

DEFAULT_ROW_LIMIT = 50
MAX_ROW_LIMIT = 200
DEFAULT_STATEMENT_TIMEOUT_SECONDS = 5

# Same identifier shape the manifest validator enforces (contract §3.6): defense in
# depth in case a config ever reaches this module without having gone through it.
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class EntityError(Exception):
    """The read could not be executed at all (connection/driver failure)."""


class InvalidEntityConfig(EntityError):
    """The compiled config for this entity is not shaped the way §3.6 requires."""


class InvalidFilter(ValueError):
    """The caller asked to filter or limit outside what this entity declares."""


@dataclass(frozen=True)
class EntityRead:
    name: str
    table: str
    columns: tuple[str, ...]
    primary_key: str

    @classmethod
    def compile(cls, name: str, config: dict[str, Any]) -> "EntityRead":
        table = config.get("table")
        primary_key = config.get("primary_key")
        columns = config.get("columns")
        if not (isinstance(table, str) and _IDENTIFIER.match(table)):
            raise InvalidEntityConfig(f"entity {name!r} has no valid table name")
        if not (isinstance(primary_key, str) and _IDENTIFIER.match(primary_key)):
            raise InvalidEntityConfig(f"entity {name!r} has no valid primary_key")
        if not (isinstance(columns, list) and columns and all(
            isinstance(c, str) and _IDENTIFIER.match(c) for c in columns
        )):
            raise InvalidEntityConfig(f"entity {name!r} has no valid columns list")
        if primary_key not in columns:
            raise InvalidEntityConfig(f"entity {name!r} primary_key must be a declared column")
        return cls(name=name, table=table, columns=tuple(columns), primary_key=primary_key)


def read(
    *,
    dsn: str,
    entity: EntityRead,
    user_id: str,
    org_id: str | None,
    role: str,
    filters: dict[str, Any] | None = None,
    limit: int = DEFAULT_ROW_LIMIT,
    row_cap: int = MAX_ROW_LIMIT,
    statement_timeout_seconds: int = DEFAULT_STATEMENT_TIMEOUT_SECONDS,
    connect: Callable[..., Any] = psycopg.connect,
) -> list[dict[str, Any]]:
    """Run ``SELECT <columns> FROM <table> WHERE <eq filters> ORDER BY <pk> LIMIT n``.

    Every identifier in the query text comes from ``entity`` (compiled config);
    every filter value is bound as a parameter, never interpolated.
    """
    filters = filters or {}
    if not 1 <= limit <= row_cap:
        raise InvalidFilter(f"limit must be between 1 and {row_cap}")
    for key in filters:
        if key not in entity.columns:
            raise InvalidFilter(f"{key!r} is not a declared column of entity {entity.name!r}")

    select_list = sql.SQL(", ").join(sql.Identifier(c) for c in entity.columns)
    if filters:
        where_clause = sql.SQL(" AND ").join(
            sql.SQL("{} = {}").format(sql.Identifier(key), sql.Placeholder()) for key in filters
        )
    else:
        where_clause = sql.SQL("TRUE")
    query = sql.SQL("SELECT {} FROM {} WHERE {} ORDER BY {} LIMIT {}").format(
        select_list, sql.Identifier(entity.table), where_clause,
        sql.Identifier(entity.primary_key), sql.Literal(limit),
    )

    try:
        with connect(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (f"{statement_timeout_seconds}s",),
                )
                # These three GUCs are what the app's RLS policies read via
                # current_setting(..., true); setting them inside this same
                # transaction, before the query, is what makes the isolation real
                # rather than advisory.
                cursor.execute("SELECT set_config('hull.user_id', %s, true)", (user_id,))
                cursor.execute("SELECT set_config('hull.org_id', %s, true)", (org_id or "",))
                cursor.execute("SELECT set_config('hull.role', %s, true)", (role,))
                cursor.execute(query, list(filters.values()))
                columns = [d.name for d in (cursor.description or ())]
                rows = cursor.fetchall()
    except psycopg.Error as exc:
        raise EntityError("entity read failed") from exc
    return [dict(zip(columns, row)) for row in rows]
