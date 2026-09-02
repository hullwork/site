"""Deterministic validation and bounded execution for agent-generated SQL."""
from __future__ import annotations

import datetime as dt
import decimal
import uuid
from dataclasses import dataclass
from typing import Any, Callable

import psycopg
from psycopg import sql
from sqlglot import exp, parse
from sqlglot.errors import SqlglotError

from sites.storage import DatabaseConfig, StorageError

MAX_QUERY_CHARS = 20_000
DEFAULT_ROW_LIMIT = 100
MAX_ROW_LIMIT = 1_000
DEFAULT_TIMEOUT_SECONDS = 5
MAX_TIMEOUT_SECONDS = 10

_FORBIDDEN_NODES = (
    exp.Alter,
    exp.Command,
    exp.Copy,
    exp.Create,
    exp.Delete,
    exp.Drop,
    exp.Grant,
    exp.Insert,
    exp.Into,
    exp.Lock,
    exp.Merge,
    exp.Revoke,
    exp.Transaction,
    exp.TruncateTable,
    exp.Update,
)
_SYSTEM_RELATION_PREFIXES = ("pg_", "sql_")


class QueryRejected(ValueError):
    """The SQL is not inside the read-only NL2SQL contract."""


@dataclass(frozen=True)
class QueryResult:
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    row_count: int
    truncated: bool


def validate_read_query(query: str) -> str:
    """Return canonical PostgreSQL SQL after AST-level read-only validation."""
    candidate = str(query or "").strip()
    if not candidate:
        raise QueryRejected("query must not be empty")
    if len(candidate) > MAX_QUERY_CHARS:
        raise QueryRejected(f"query exceeds {MAX_QUERY_CHARS} characters")
    try:
        statements = parse(candidate, read="postgres")
    except SqlglotError as exc:
        raise QueryRejected("query is not valid PostgreSQL SQL") from exc
    if len(statements) != 1:
        raise QueryRejected("exactly one SQL statement is required")
    statement = statements[0]
    if statement is None or not isinstance(statement, exp.Query):
        raise QueryRejected("only SELECT queries are allowed")
    forbidden = next(statement.find_all(_FORBIDDEN_NODES), None)
    if forbidden is not None:
        raise QueryRejected(
            f"query contains forbidden {forbidden.key.upper()} operation"
        )
    for table in statement.find_all(exp.Table):
        # Site reader roles receive a fixed search_path.  Qualified names are
        # unnecessary for tenant data and would allow an agent to reach
        # pg_catalog, information_schema, public, or another tenant schema.
        if table.catalog or table.db:
            raise QueryRejected("schema-qualified relations are not allowed")
        relation = table.name.lower()
        if relation.startswith(_SYSTEM_RELATION_PREFIXES):
            raise QueryRejected("PostgreSQL system relations are not allowed")
    return statement.sql(dialect="postgres")


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (dt.date, dt.datetime, dt.time, uuid.UUID)):
        return value.isoformat() if hasattr(value, "isoformat") else str(value)
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return str(value)


class ReadOnlyQueryExecutor:
    """Execute validated SQL with a reader credential and hard resource bounds."""

    def __init__(
        self,
        config: DatabaseConfig,
        *,
        connect: Callable[..., Any] | None = None,
    ) -> None:
        self._config = config
        self._connect = connect or psycopg.connect

    def execute(
        self,
        query: str,
        *,
        row_limit: int = DEFAULT_ROW_LIMIT,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> QueryResult:
        if not 1 <= row_limit <= MAX_ROW_LIMIT:
            raise QueryRejected(f"row_limit must be between 1 and {MAX_ROW_LIMIT}")
        if not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
            raise QueryRejected(
                f"timeout_seconds must be between 1 and {MAX_TIMEOUT_SECONDS}"
            )
        normalized = validate_read_query(query)
        bounded = sql.SQL("SELECT * FROM ({}) AS _sites_query LIMIT {}").format(
            sql.SQL(normalized), sql.Literal(row_limit + 1)
        )
        try:
            with self._connect(
                **self._config.postgres_connect_kwargs()
            ) as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    cursor.execute(
                        "SELECT set_config('statement_timeout', %s, true)",
                        (f"{timeout_seconds}s",),
                    )
                    cursor.execute(bounded)
                    columns = tuple(
                        str(column.name) for column in (cursor.description or ())
                    )
                    fetched = cursor.fetchall()
        except psycopg.OperationalError as exc:
            raise StorageError("site database is unavailable") from exc
        except psycopg.Error as exc:
            primary = str(getattr(getattr(exc, "diag", None), "message_primary", ""))
            detail = primary.strip()[:300] or "PostgreSQL rejected the query"
            raise QueryRejected(detail) from exc
        except Exception as exc:
            raise StorageError("read-only site query failed") from exc
        truncated = len(fetched) > row_limit
        rows = tuple(
            tuple(_json_value(value) for value in row)
            for row in fetched[:row_limit]
        )
        return QueryResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
        )
