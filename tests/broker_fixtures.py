"""Shared fixtures for tests/test_broker_*.py (partition C only).

Not itself a ``test_*.py`` module, so ``unittest discover -p 'test_*.py'`` never
tries to run it directly.
"""
from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
from pathlib import Path

import psycopg
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from sites.broker import db as broker_db
from sites.broker import tokens as broker_tokens

_MIGRATE_LOCK = threading.Lock()
_MIGRATED = False


def dsn() -> str:
    explicit = os.environ.get("SITES_TEST_PG_DSN", "").strip()
    if explicit:
        return explicit
    host = os.environ.get("SITES_TEST_DB_HOST", "127.0.0.1")
    port = os.environ.get("SITES_TEST_DB_PORT", "50771")
    name = os.environ.get("HULL_BROKER_TEST_DB_NAME", "hw_c")
    user = os.environ.get("SITES_TEST_DB_USER", "postgres")
    sslmode = os.environ.get("SITES_TEST_DB_SSLMODE", "disable")
    return f"host={host} port={port} dbname={name} user={user} sslmode={sslmode}"


def database() -> broker_db.BrokerDatabase:
    """A BrokerDatabase over the shared test database, migrated exactly once per
    process. Every test picks its own random app_env_id, so sharing the schema
    across tests never collides.
    """
    global _MIGRATED
    db = broker_db.BrokerDatabase(lambda: psycopg.connect(dsn()))
    with _MIGRATE_LOCK:
        if not _MIGRATED:
            broker_db.migrate(db)
            _MIGRATED = True
    return db


def app_env_id() -> str:
    return secrets.token_hex(16)


def keyring(*, extra_inactive: bool = False) -> broker_tokens.KeyRing:
    """A real file-backed KeyRing (exercises KeyRing.load, not just __init__)."""
    entries = [_new_key_entry(active=True)]
    if extra_inactive:
        entries.append(_new_key_entry(active=False))
    handle = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, prefix="hull-broker-test-keys-"
    )
    with handle:
        json.dump(entries, handle)
    return broker_tokens.KeyRing.load(handle.name)


def _new_key_entry(*, active: bool) -> dict:
    private_key = Ed25519PrivateKey.generate()
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    return {"kid": secrets.token_hex(4), "private_key_pem": pem, "active": active}


def secret_key() -> bytes:
    return os.urandom(32)


def truncate_all(db: broker_db.BrokerDatabase) -> None:
    """Only used by tests that need a clean slate for a global scan (secrets
    never appear anywhere); most tests avoid this and rely on unique app ids.
    """
    with db.transaction() as cursor:
        cursor.execute(
            "TRUNCATE broker_app_configs, broker_app_settings, broker_app_hosts, "
            "broker_app_workloads, broker_app_secrets, broker_sessions, "
            "broker_otp_codes, broker_rate_limits, broker_outbound_mail, "
            "broker_end_users, broker_apps CASCADE"
        )
