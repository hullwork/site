"""Validated, transactional PostgreSQL migrations for dynamic site schemas."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Callable

import psycopg
from psycopg import sql
from sqlglot import exp, parse
from sqlglot.errors import SqlglotError

from sites.storage import DatabaseConfig, StorageError
from sites.validation import ValidationError


# The whole HTTP request is capped at 64 KiB. Leave room for JSON escaping,
# version metadata, image, and field names so the advertised migration limit is
# reachable through the public API rather than only by direct function calls.
MAX_MIGRATION_BYTES = 48 * 1024
MAX_MIGRATION_STATEMENTS = 32


@dataclass(frozen=True)
class MigrationArtifact:
    """An exact migration payload bound to its caller-supplied digest."""

    content: str
    sha256: str
    statements: tuple[str, ...]


def validate_migration_artifact(
    content: object, expected_sha256: str, schema: str
) -> MigrationArtifact:
    """Validate an idempotent, additive DDL artifact for one site schema.

    The accepted surface is deliberately small: ``CREATE TABLE IF NOT EXISTS``,
    ``CREATE INDEX IF NOT EXISTS``, and ``ALTER TABLE ... ADD COLUMN IF NOT
    EXISTS``.  The runtime database role is already schema-scoped, while the AST
    checks prevent an artifact from explicitly addressing another schema.
    """
    if not isinstance(content, str) or not content.strip():
        raise ValidationError("migrationSql is required for this schema change")
    raw = content.encode("utf-8")
    if len(raw) > MAX_MIGRATION_BYTES:
        raise ValidationError(
            f"migrationSql must not exceed {MAX_MIGRATION_BYTES} UTF-8 bytes"
        )
    if "\x00" in content:
        raise ValidationError("migrationSql must not contain NUL bytes")
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if actual_sha256 != expected_sha256:
        raise ValidationError("migrationSql does not match migrationSha256")
    try:
        expressions = parse(content, read="postgres")
    except SqlglotError as exc:
        raise ValidationError("migrationSql is not valid PostgreSQL SQL") from exc
    if not expressions or len(expressions) > MAX_MIGRATION_STATEMENTS:
        raise ValidationError(
            f"migrationSql must contain 1-{MAX_MIGRATION_STATEMENTS} statements"
        )
    statements: list[str] = []
    for expression in expressions:
        _validate_statement(expression, schema)
        statements.append(expression.sql(dialect="postgres"))
    return MigrationArtifact(content, actual_sha256, tuple(statements))


def _validate_statement(expression: exp.Expression, schema: str) -> None:
    for table in expression.find_all(exp.Table):
        catalog = table.args.get("catalog")
        database = table.args.get("db")
        if catalog or (database and str(database.this) != schema):
            raise ValidationError("migrationSql may only address the site schema")

    if isinstance(expression, exp.Create):
        kind = str(expression.args.get("kind") or "").upper()
        if kind not in {"TABLE", "INDEX"}:
            raise ValidationError("migrationSql contains unsupported DDL")
        if not expression.args.get("exists"):
            raise ValidationError(
                "CREATE migration statements must use IF NOT EXISTS"
            )
        if expression.args.get("replace") or expression.args.get("concurrently"):
            raise ValidationError("migrationSql contains unsupported CREATE options")
        if expression.args.get("properties") is not None:
            raise ValidationError("migrationSql contains unsupported CREATE properties")
        if kind == "TABLE" and expression.args.get("expression") is not None:
            raise ValidationError("CREATE TABLE AS is not allowed in migrations")
        return

    if isinstance(expression, exp.Alter) and str(
        expression.args.get("kind") or ""
    ).upper() == "TABLE":
        actions = expression.args.get("actions") or []
        if actions and all(
            isinstance(action, exp.ColumnDef) and action.args.get("exists") is True
            for action in actions
        ):
            return
    raise ValidationError(
        "migrationSql only allows idempotent CREATE TABLE/INDEX and ADD COLUMN"
    )


class SiteMigrationExecutor:
    """Execute a validated artifact as the site's least-privilege runtime role."""

    def __init__(
        self,
        config: DatabaseConfig,
        *,
        connect: Callable[..., Any] | None = None,
        statement_timeout_ms: int = 10_000,
        lock_timeout_ms: int = 3_000,
    ) -> None:
        if statement_timeout_ms < 1 or lock_timeout_ms < 1:
            raise ValueError("migration timeouts must be positive")
        self._config = config
        self._connect = connect or psycopg.connect
        self._statement_timeout_ms = statement_timeout_ms
        self._lock_timeout_ms = lock_timeout_ms

    def execute(self, artifact: MigrationArtifact, schema: str) -> None:
        """Run all statements in one PostgreSQL transaction with local bounds."""
        try:
            with self._connect(**self._config.postgres_connect_kwargs()) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT set_config('statement_timeout', %s, true)",
                        (f"{self._statement_timeout_ms}ms",),
                    )
                    cursor.execute(
                        "SELECT set_config('lock_timeout', %s, true)",
                        (f"{self._lock_timeout_ms}ms",),
                    )
                    cursor.execute(
                        sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(
                            sql.Identifier(schema)
                        )
                    )
                    for statement in artifact.statements:
                        cursor.execute(statement)
        except Exception as exc:
            raise StorageError("site database migration failed") from exc
