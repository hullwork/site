"""PostgreSQL role and schema isolation contract for dynamic sites."""
from __future__ import annotations

import os
import base64
import hashlib
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

import psycopg

from sites.site_database import (
    SiteDatabaseBinding,
    SiteDatabaseProvisioner,
    SiteDatabaseSecretStore,
    role_database_config,
    site_data_config_from_env,
)
from sites.storage import DatabaseConfig
from sites.migrations import SiteMigrationExecutor, validate_migration_artifact


def _settings() -> dict[str, object]:
    settings: dict[str, object] = {
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


def _login_role(role: str) -> str:
    project_ref = os.environ.get("SUPABASE_PROJECT_REF", "").strip()
    return f"{role}.{project_ref}" if project_ref else role


def _connect_with_role(settings: dict[str, object], role: str, password: str):
    kwargs = {**settings, "user": _login_role(role), "password": password}
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            return psycopg.connect(**kwargs)
        except psycopg.OperationalError as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(1)
    assert last_error is not None
    raise last_error


class SiteDatabaseBindingTests(unittest.TestCase):
    def test_site_identity_is_stable_and_boundary_safe(self) -> None:
        first = SiteDatabaseBinding.for_site("acme", "alice", "shop")
        self.assertEqual(
            first, SiteDatabaseBinding.for_site("acme", "alice", "shop")
        )
        self.assertNotEqual(
            first, SiteDatabaseBinding.for_site("acme-alice", "shop", "")
        )
        for identifier in (
            first.schema,
            first.runtime_role,
            first.reader_role,
        ):
            self.assertLessEqual(len(identifier), 63)
            self.assertRegex(identifier, r"^[a-z0-9_]+$")

    def test_supavisor_role_keeps_the_project_suffix(self) -> None:
        config = DatabaseConfig("pooler", 5432, "postgres", "postgres.ref", "admin")
        role = role_database_config(config, "site_reader_abc", "reader")
        self.assertEqual(role.user, "site_reader_abc.ref")
        self.assertEqual(role.password, "reader")

    def test_runtime_host_can_differ_from_control_plane_host(self) -> None:
        config = DatabaseConfig("control-db", 5432, "sites", "sites", "admin")
        role = role_database_config(
            config,
            "site_runtime_abc",
            "runtime",
            host="postgres.sites-local.svc.cluster.local",
        )
        self.assertEqual(role.host, "postgres.sites-local.svc.cluster.local")
        self.assertEqual(role.user, "site_runtime_abc")


class SiteDataConfigTests(unittest.TestCase):
    def test_data_database_can_be_separate_from_control_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            control_password = Path(directory) / "control-password"
            data_password = Path(directory) / "data-password"
            control_password.write_text("control-secret")
            data_password.write_text("data-secret")
            with mock.patch.dict(
                os.environ,
                {
                    "SITES_DB_PASSWORD_FILE": str(control_password),
                    "SITES_DB_HOST": "control-db",
                    "SITES_DATA_DB_PASSWORD_FILE": str(data_password),
                    "SITES_DATA_DB_HOST": "data-db",
                    "SITES_DATA_DB_NAME": "sites_data",
                    "SITES_DATA_DB_USER": "data_admin",
                },
                clear=True,
            ):
                config = site_data_config_from_env()
        self.assertEqual(config.host, "data-db")
        self.assertEqual(config.dbname, "sites_data")
        self.assertEqual(config.user, "data_admin")
        self.assertEqual(config.password, "data-secret")


class _SecretKube:
    def __init__(self) -> None:
        self.body: dict[str, object] | None = None

    def create_or_patch(self, _collection: str, _path: str, body: dict) -> dict:
        self.body = body
        return body

    def get(self, _path: str) -> dict:
        assert self.body is not None
        values = self.body["stringData"]
        return {
            "data": {
                key: base64.b64encode(value.encode()).decode()
                for key, value in values.items()
            }
        }


class SiteDatabaseSecretStoreTests(unittest.TestCase):
    def test_passwords_round_trip_without_entering_metadata(self) -> None:
        kube = _SecretKube()
        store = SiteDatabaseSecretStore(kube)
        binding = SiteDatabaseBinding.for_site("acme", "alice", "shop")
        store.save(
            binding,
            runtime_password="runtime-secret",
            reader_password="reader-secret",
            runtime_config=DatabaseConfig(
                "data-db", 5432, "sites_data", binding.runtime_role,
                "runtime-secret", sslmode="require"
            ),
        )
        self.assertEqual(
            store.load(binding), ("runtime-secret", "reader-secret")
        )
        self.assertNotIn("runtime-secret", str(kube.body["metadata"]))
        self.assertNotIn("reader-secret", str(kube.body["metadata"]))
        self.assertEqual(kube.body["stringData"]["runtime-host"], "data-db")
        self.assertEqual(kube.body["stringData"]["runtime-schema"], binding.schema)


class SiteDatabaseProvisionerTests(unittest.TestCase):
    def test_runtime_and_reader_roles_are_isolated(self) -> None:
        settings = _settings()
        config = DatabaseConfig(
            host=str(settings["host"]),
            port=int(settings["port"]),
            dbname=str(settings["dbname"]),
            user=str(settings["user"]),
            password=str(settings["password"]),
            sslmode=str(settings["sslmode"]),
        )
        suffix = uuid.uuid4().hex
        binding = SiteDatabaseBinding.for_site("test", suffix, "dynamic")
        runtime_password = f"runtime-{uuid.uuid4().hex}"
        reader_password = f"reader-{uuid.uuid4().hex}"
        provisioner = SiteDatabaseProvisioner(config)
        provisioner.provision(
            binding,
            runtime_password=runtime_password,
            reader_password=reader_password,
        )

        admin_kwargs = dict(settings)
        self.addCleanup(self._cleanup, admin_kwargs, binding)
        migration_sql = (
            "CREATE TABLE IF NOT EXISTS inventory (id INTEGER PRIMARY KEY)"
        )
        artifact = validate_migration_artifact(
            migration_sql,
            hashlib.sha256(migration_sql.encode("utf-8")).hexdigest(),
            binding.schema,
        )
        SiteMigrationExecutor(
            role_database_config(config, binding.runtime_role, runtime_password)
        ).execute(artifact, binding.schema)
        with _connect_with_role(
            admin_kwargs, binding.runtime_role, runtime_password
        ) as runtime:
            runtime.execute("INSERT INTO inventory VALUES (1)")
        provisioner.refresh_reader_grants(
            binding, runtime_password=runtime_password
        )
        with _connect_with_role(
            admin_kwargs, binding.reader_role, reader_password
        ) as reader:
            self.assertEqual(reader.execute("SELECT id FROM inventory").fetchone(), (1,))
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                reader.execute("INSERT INTO inventory VALUES (2)")

    @staticmethod
    def _cleanup(
        admin_kwargs: dict[str, object], binding: SiteDatabaseBinding
    ) -> None:
        with psycopg.connect(**admin_kwargs, autocommit=True) as connection:
            connection.execute(f'DROP SCHEMA IF EXISTS "{binding.schema}" CASCADE')
            connection.execute(f'DROP ROLE IF EXISTS "{binding.reader_role}"')
            connection.execute(f'DROP ROLE IF EXISTS "{binding.runtime_role}"')


if __name__ == "__main__":
    unittest.main()
