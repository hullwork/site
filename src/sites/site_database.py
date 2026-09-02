"""PostgreSQL schema and role isolation for dynamic sites."""
from __future__ import annotations

import base64
import binascii
import hashlib
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import psycopg
from psycopg import sql

from sites.storage import DatabaseConfig, StorageError
from sites import exposure
from sites.kube import ApiError


def _identity_digest(merchant_id: str, user_id: str, site_name: str) -> str:
    identity = "\0".join((merchant_id, user_id, site_name)).encode("utf-8")
    return hashlib.sha256(identity).hexdigest()[:32]


@dataclass(frozen=True)
class SiteDatabaseBinding:
    """Stable, non-secret PostgreSQL identities assigned to one dynamic site."""

    schema: str
    runtime_role: str
    reader_role: str

    @classmethod
    def for_site(
        cls, merchant_id: str, user_id: str, site_name: str
    ) -> "SiteDatabaseBinding":
        digest = _identity_digest(merchant_id, user_id, site_name)
        return cls(
            schema=f"site_{digest}",
            runtime_role=f"site_runtime_{digest}",
            reader_role=f"site_reader_{digest}",
        )


class SiteDatabaseProvisioner:
    """Create least-privilege PostgreSQL identities for a dynamic site.

    Passwords are supplied by the caller and are never persisted here. The control
    plane is expected to put them directly into its secret backend.
    """

    def __init__(
        self,
        config: DatabaseConfig,
        *,
        connect: Callable[..., Any] | None = None,
    ) -> None:
        self._config = config
        self._connect = connect or psycopg.connect

    def provision(
        self,
        binding: SiteDatabaseBinding,
        *,
        runtime_password: str,
        reader_password: str,
    ) -> None:
        if not runtime_password or not reader_password:
            raise ValueError("site database passwords must not be empty")
        try:
            with self._connect(
                **self._config.postgres_connect_kwargs()
            ) as connection:
                with connection.cursor() as cursor:
                    self._ensure_role(
                        cursor, binding.runtime_role, runtime_password
                    )
                    self._ensure_role(cursor, binding.reader_role, reader_password)
                    # Managed providers such as Supabase deliberately prevent the
                    # project admin from SET ROLE to arbitrary login roles. Keep
                    # schema ownership at the platform layer and grant only the
                    # capabilities the site runtime needs.
                    cursor.execute(
                        sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                            sql.Identifier(binding.schema)
                        )
                    )
                    cursor.execute(
                        sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(
                            sql.Identifier(binding.schema)
                        )
                    )
                    cursor.execute(
                        sql.SQL("GRANT USAGE, CREATE ON SCHEMA {} TO {}").format(
                            sql.Identifier(binding.schema),
                            sql.Identifier(binding.runtime_role),
                        )
                    )
                    cursor.execute(
                        sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                            sql.Identifier(binding.schema),
                            sql.Identifier(binding.reader_role),
                        )
                    )
                    cursor.execute(
                        sql.SQL("ALTER ROLE {} SET search_path TO {}, pg_catalog").format(
                            sql.Identifier(binding.runtime_role),
                            sql.Identifier(binding.schema),
                        )
                    )
                    cursor.execute(
                        sql.SQL("ALTER ROLE {} SET search_path TO {}, pg_catalog").format(
                            sql.Identifier(binding.reader_role),
                            sql.Identifier(binding.schema),
                        )
                    )
                    cursor.execute(
                        sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(
                            sql.Identifier(binding.schema),
                            sql.Identifier(binding.reader_role),
                        )
                    )
        except Exception as exc:
            raise StorageError("site database provisioning failed") from exc

    def refresh_reader_grants(
        self,
        binding: SiteDatabaseBinding,
        *,
        runtime_password: str,
    ) -> None:
        """Let the table-owning runtime grant NL2SQL access after migrations."""
        if not runtime_password:
            raise ValueError("site runtime password must not be empty")
        runtime_user = binding.runtime_role
        if "." in self._config.user:
            # Supavisor routes a database role as ``role.project_ref``.
            runtime_user = f"{runtime_user}.{self._config.user.split('.', 1)[1]}"
        kwargs = self._config.postgres_connect_kwargs()
        kwargs.update(user=runtime_user, password=runtime_password)
        try:
            with self._connect(**kwargs) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(
                            sql.Identifier(binding.schema),
                            sql.Identifier(binding.reader_role),
                        )
                    )
        except Exception as exc:
            raise StorageError("site database reader grant failed") from exc

    @staticmethod
    def _ensure_role(cursor: Any, role: str, password: str) -> None:
        cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
        if cursor.fetchone() is None:
            cursor.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB "
                    "NOCREATEROLE NOREPLICATION NOINHERIT"
                ).format(sql.Identifier(role), sql.Literal(password))
            )
            return
        cursor.execute(
            sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )


def site_data_config_from_env() -> DatabaseConfig:
    """Load the site-data database, falling back to the control DB for local use."""
    control = DatabaseConfig.from_env()
    password_file_value = os.environ.get("SITES_DATA_DB_PASSWORD_FILE", "").strip()
    password = control.password
    if password_file_value:
        try:
            password = Path(password_file_value).read_text().strip()
        except OSError as exc:
            raise StorageError("site data database password file is unavailable") from exc
        if not password:
            raise StorageError("site data database password must not be empty")
    try:
        port = int(os.environ.get("SITES_DATA_DB_PORT", str(control.port)))
        connect_timeout = int(
            os.environ.get(
                "SITES_DATA_DB_CONNECT_TIMEOUT", str(control.connect_timeout)
            )
        )
        statement_timeout = int(
            os.environ.get(
                "SITES_DATA_DB_STATEMENT_TIMEOUT", str(control.statement_timeout)
            )
        )
    except ValueError as exc:
        raise StorageError("site data database port and timeouts must be integers") from exc
    if not 1 <= port <= 65535 or connect_timeout < 1 or statement_timeout < 1:
        raise StorageError("site data database connection settings are invalid")
    return DatabaseConfig(
        host=os.environ.get("SITES_DATA_DB_HOST", control.host),
        port=port,
        dbname=os.environ.get("SITES_DATA_DB_NAME", control.dbname),
        user=os.environ.get("SITES_DATA_DB_USER", control.user),
        password=password,
        connect_timeout=connect_timeout,
        statement_timeout=statement_timeout,
        sslmode=os.environ.get("SITES_DATA_DB_SSLMODE", control.sslmode),
    )


def role_database_config(
    config: DatabaseConfig,
    role: str,
    password: str,
    *,
    host: str | None = None,
) -> DatabaseConfig:
    """Bind a site role, preserving the Supavisor project-ref suffix if present."""
    user = role
    if "." in config.user:
        user = f"{role}.{config.user.split('.', 1)[1]}"
    return DatabaseConfig(
        host=host or config.host,
        port=config.port,
        dbname=config.dbname,
        user=user,
        password=password,
        connect_timeout=config.connect_timeout,
        statement_timeout=config.statement_timeout,
        sslmode=config.sslmode,
    )


class SiteDatabaseSecretStore:
    """Keep per-site database passwords in a control-plane Kubernetes Secret."""

    def __init__(self, kube: Any) -> None:
        self._kube = kube

    @staticmethod
    def secret_name(binding: SiteDatabaseBinding) -> str:
        return f"site-db-{binding.schema.removeprefix('site_')[:24]}"

    def save(
        self,
        binding: SiteDatabaseBinding,
        *,
        runtime_password: str,
        reader_password: str,
        runtime_config: DatabaseConfig | None = None,
    ) -> None:
        name = self.secret_name(binding)
        collection = f"/api/v1/namespaces/{exposure.CONTROL_NAMESPACE}/secrets"
        path = f"{collection}/{name}"
        string_data = {
            "runtime-password": runtime_password,
            "reader-password": reader_password,
        }
        if runtime_config is not None:
            string_data.update(
                {
                    "runtime-host": runtime_config.host,
                    "runtime-port": str(runtime_config.port),
                    "runtime-database": runtime_config.dbname,
                    "runtime-user": runtime_config.user,
                    "runtime-sslmode": runtime_config.sslmode,
                    "runtime-schema": binding.schema,
                }
            )
        body = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": name,
                "namespace": exposure.CONTROL_NAMESPACE,
                "labels": {"sites.local/managed": "site-database"},
            },
            "type": "Opaque",
            "stringData": string_data,
        }
        self._kube.create_or_patch(collection, path, body)

    def load(self, binding: SiteDatabaseBinding) -> tuple[str, str]:
        name = self.secret_name(binding)
        secret = self._kube.get(
            f"/api/v1/namespaces/{exposure.CONTROL_NAMESPACE}/secrets/{name}"
        )
        data = secret.get("data") if isinstance(secret, dict) else None
        if not isinstance(data, dict):
            raise StorageError("site database credential secret is malformed")
        try:
            runtime_password = base64.b64decode(
                str(data["runtime-password"]), validate=True
            ).decode("utf-8")
            reader_password = base64.b64decode(
                str(data["reader-password"]), validate=True
            ).decode("utf-8")
        except (KeyError, ValueError, binascii.Error, UnicodeDecodeError) as exc:
            raise StorageError("site database credential secret is malformed") from exc
        if not runtime_password or not reader_password:
            raise StorageError("site database credential secret is empty")
        return runtime_password, reader_password


class DynamicSiteDatabaseService:
    """Coordinate role provisioning without exposing passwords to API callers."""

    def __init__(
        self,
        config: DatabaseConfig,
        secrets_store: SiteDatabaseSecretStore,
        *,
        connect: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config
        self.provisioner = SiteDatabaseProvisioner(config, connect=connect)
        self.secrets = secrets_store

    def provision(
        self, merchant_id: str, user_id: str, site_name: str
    ) -> SiteDatabaseBinding:
        binding = SiteDatabaseBinding.for_site(merchant_id, user_id, site_name)
        try:
            runtime_password, reader_password = self.secrets.load(binding)
        except ApiError as exc:
            if exc.status != 404:
                raise StorageError("site database credential lookup failed") from exc
            runtime_password = secrets.token_urlsafe(32)
            reader_password = secrets.token_urlsafe(32)
        self.provisioner.provision(
            binding,
            runtime_password=runtime_password,
            reader_password=reader_password,
        )
        try:
            self.secrets.save(
                binding,
                runtime_password=runtime_password,
                reader_password=reader_password,
                runtime_config=role_database_config(
                    self.config,
                    binding.runtime_role,
                    runtime_password,
                    host=(
                        os.environ.get("SITES_DATA_DB_RUNTIME_HOST", "").strip()
                        or self.config.host
                    ),
                ),
            )
        except (ApiError, RuntimeError) as exc:
            raise StorageError("site database credential save failed") from exc
        return binding

    def deployment_binding(
        self, merchant_id: str, user_id: str, site_name: str
    ) -> dict[str, str]:
        binding = SiteDatabaseBinding.for_site(merchant_id, user_id, site_name)
        try:
            self.secrets.load(binding)
        except (ApiError, RuntimeError) as exc:
            raise StorageError("site database credential lookup failed") from exc
        return {
            "schema": binding.schema,
            "controlSecretName": self.secrets.secret_name(binding),
        }

    def reader_config(
        self, merchant_id: str, user_id: str, site_name: str
    ) -> DatabaseConfig:
        binding = SiteDatabaseBinding.for_site(merchant_id, user_id, site_name)
        try:
            _, reader_password = self.secrets.load(binding)
        except (ApiError, RuntimeError) as exc:
            raise StorageError("site database credential lookup failed") from exc
        return role_database_config(
            self.config, binding.reader_role, reader_password
        )

    def runtime_config(
        self, merchant_id: str, user_id: str, site_name: str
    ) -> DatabaseConfig:
        """Return an internal runtime-role config without crossing the API boundary."""
        binding = SiteDatabaseBinding.for_site(merchant_id, user_id, site_name)
        try:
            runtime_password, _ = self.secrets.load(binding)
        except (ApiError, RuntimeError) as exc:
            raise StorageError("site database credential lookup failed") from exc
        return role_database_config(
            self.config, binding.runtime_role, runtime_password
        )

    def refresh_reader(
        self, merchant_id: str, user_id: str, site_name: str
    ) -> None:
        binding = SiteDatabaseBinding.for_site(merchant_id, user_id, site_name)
        try:
            runtime_password, _ = self.secrets.load(binding)
        except (ApiError, RuntimeError) as exc:
            raise StorageError("site database credential lookup failed") from exc
        self.provisioner.refresh_reader_grants(
            binding, runtime_password=runtime_password
        )
