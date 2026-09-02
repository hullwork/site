"""Shared PostgreSQL fixtures for integration-style tests."""
from __future__ import annotations

import atexit
import os
import threading
import uuid
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

from sites.storage import DatabaseConfig, Store


_LOCK = threading.Lock()
_SCHEMAS: dict[str, str] = {}
_STORES: list[Store] = []


def _settings() -> dict[str, Any]:
    settings: dict[str, Any] = {
        "host": os.environ.get("SITES_TEST_DB_HOST", "127.0.0.1"),
        "port": int(os.environ.get("SITES_TEST_DB_PORT", "55439")),
        "dbname": os.environ.get("SITES_TEST_DB_NAME", "postgres"),
        "user": os.environ.get("SITES_TEST_DB_USER", "postgres"),
        "password": os.environ.get("SITES_TEST_DB_PASSWORD", ""),
    }
    # The throwaway server these tests talk to speaks no TLS. "disable" says so
    # out loud; the production default is "require" precisely because the old
    # "prefer" would have papered over this by connecting in plaintext anyway.
    settings["sslmode"] = os.environ.get("SITES_TEST_DB_SSLMODE", "").strip() or "disable"
    return settings


def _schema_for(key: str | Path) -> str:
    identity = str(key)
    with _LOCK:
        existing = _SCHEMAS.get(identity)
        if existing is not None:
            return existing
        schema = f"test_{uuid.uuid4().hex}"
        with psycopg.connect(**_settings(), autocommit=True) as connection:
            connection.execute(
                sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
            )
        _SCHEMAS[identity] = schema
        return schema


def postgres_store(key: str | Path) -> Store:
    """Return a Store isolated in the PostgreSQL schema assigned to ``key``."""
    schema = _schema_for(key)
    settings = _settings()
    config = DatabaseConfig(
        host=str(settings["host"]),
        port=int(settings["port"]),
        dbname=str(settings["dbname"]),
        user=str(settings["user"]),
        password=str(settings["password"]),
        sslmode=str(settings["sslmode"]),
    )

    def connect(**kwargs: Any) -> Any:
        options = str(kwargs.get("options") or "").strip()
        kwargs["options"] = f"{options} -c search_path={schema},pg_catalog".strip()
        return psycopg.connect(**kwargs)

    cache_connections = os.environ.get(
        "SITES_TEST_DB_CACHE_CONNECTIONS", "false"
    ).lower() in {"1", "true", "yes"}
    store = Store.postgres(
        config,
        connect=connect,
        cache_connections=cache_connections,
    )
    with _LOCK:
        _STORES.append(store)
    return store


def postgres_connection(key: str | Path, *, autocommit: bool = False) -> Any:
    """Open a raw connection in the same isolated schema as ``postgres_store``."""
    schema = _schema_for(key)
    settings = _settings()
    return psycopg.connect(
        **settings,
        autocommit=autocommit,
        options=f"-c search_path={schema},pg_catalog",
    )


def _cleanup() -> None:
    settings = _settings()
    with _LOCK:
        stores = tuple(_STORES)
        _STORES.clear()
        schemas = tuple(_SCHEMAS.values())
        _SCHEMAS.clear()
    for store in stores:
        store.close()
    if not schemas:
        return
    try:
        with psycopg.connect(**settings, autocommit=True) as connection:
            for schema in schemas:
                connection.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                        sql.Identifier(schema)
                    )
                )
    except Exception:
        # Test process shutdown must not hide the real test result.
        return


atexit.register(_cleanup)
