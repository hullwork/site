"""Metadata storage for the Sites control plane.

PostgreSQL is the only supported control-plane database.
"""
from __future__ import annotations

import contextlib
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from sites.naming import new_merchant_api_key, token_digest
from sites.validation import (
    DEFAULT_MERCHANT_KEY_TTL_SECONDS,
    DEFAULT_MAX_TENANTS,
    DEFAULT_MERCHANT_ID,
    DEFAULT_MERCHANT_MAX_DEPLOYMENTS,
)
from os import getenv
from sites import telemetry, tracing


class StorageError(RuntimeError):
    """The database is unavailable or rejected a Sites storage operation."""


class StorageConflictError(StorageError):
    """Unique constraint rejected (name/digest is already taken).

    The difference from the parent class is caller semantics: this is a 409 conflict, not a database unavailability -
    The API layer diverts 409/503 accordingly, no longer relying on copywriting guessing.
    """


# SQLSTATE for PostgreSQL unique constraint violation. No need to use `import psycopg` to determine the type - it is
# Optional dependency, and psycopg's exception object comes with diag.sqlstate, the duck judgment is enough: neither
# import, nor does it depend on the name of the exception class between versions.
_UNIQUE_VIOLATION_SQLSTATE = "23505"
VERIFICATION_FAILURES_BEFORE_ROLLBACK = 2


def _is_unique_violation(exc: BaseException) -> bool:
    """Is this the driver rejecting a duplicate key?

    PostgreSQL reports unique violations with SQLSTATE 23505.
    """
    diag = getattr(exc, "diag", None)
    return getattr(diag, "sqlstate", None) == _UNIQUE_VIOLATION_SQLSTATE


@dataclass(frozen=True)
class _Dialect:
    """The handful of places two SQL dialects actually differ here."""

    name: str
    placeholder: str
    now: str
    json_cast: str
    json_type: str
    timestamp_type: str
    identifier_type: str
    text_type: str
    schema_version_insert: str
    deployment_upsert: str
    merchant_resources_upsert: str

    def render(self, template: str) -> str:
        return template.format(
            ph=self.placeholder,
            now=self.now,
            json_cast=self.json_cast,
            json_type=self.json_type,
            ts_type=self.timestamp_type,
            id_type=self.identifier_type,
            text_type=self.text_type,
        )


# Modes that never silently fall back to an unencrypted connection. "disable" is
# an explicit, auditable decision; "prefer" and "allow" are not decisions at all.
SSLMODES = frozenset({"disable", "require", "verify-ca", "verify-full"})


@dataclass(frozen=True)
class DatabaseConfig:
    host: str
    port: int
    dbname: str
    user: str
    password: str = field(repr=False)
    connect_timeout: int = 5
    # Upper bound, in seconds, on any single statement (PostgreSQL) or socket
    # execution once a connection exists. connect_timeout alone only covers
    # the handshake: a server that accepts the connection and then never answers
    # would otherwise park the caller forever - and the sync thread parks while
    # holding the mutation lock, which stalls every write path with it. This bound
    # also applies to the migrate() DDL run by sites.api._wait_for_database at
    # startup; a migration that legitimately needs longer must raise
    # SITES_DB_STATEMENT_TIMEOUT for that rollout.
    statement_timeout: int = 10
    # "prefer" and "allow" negotiate: if the server refuses TLS they fall back to
    # plaintext and the connection succeeds anyway, so a control plane can talk to
    # its database unencrypted for years without one failed connection to show for
    # it. Only modes that fail instead of downgrading are accepted.
    sslmode: str = "require"

    def __post_init__(self) -> None:
        if self.sslmode not in SSLMODES:
            raise StorageError(
                f"database sslmode must be one of {sorted(SSLMODES)}: {self.sslmode!r}"
            )

    @classmethod
    def from_env(cls, *, default_port: int = 5432) -> "DatabaseConfig":
        password_file = Path(
            getenv(
                "SITES_DB_PASSWORD_FILE",
                "/var/run/sites-db/password",
            )
        )
        try:
            password = password_file.read_text().strip()
        except OSError as exc:
            raise StorageError("database password file is unavailable") from exc
        if not password:
            raise StorageError("database password must not be empty")
        try:
            port = int(getenv("SITES_DB_PORT", str(default_port)) or str(default_port))
            timeout = int(getenv("SITES_DB_CONNECT_TIMEOUT", "5") or "5")
            statement_timeout = int(
                getenv("SITES_DB_STATEMENT_TIMEOUT", "10") or "10"
            )
        except ValueError as exc:
            raise StorageError("database port and timeout must be integers") from exc
        if not 1 <= port <= 65535 or timeout < 1 or statement_timeout < 1:
            raise StorageError("database connection settings are invalid")
        return cls(
            host=getenv("SITES_DB_HOST", "sites-postgres") or "sites-postgres",
            port=port,
            dbname=getenv("SITES_DB_NAME", "sites") or "sites",
            user=getenv("SITES_DB_USER", "sites") or "sites",
            password=password,
            connect_timeout=timeout,
            statement_timeout=statement_timeout,
            sslmode=getenv("SITES_DB_SSLMODE", "require") or "require",
        )

    def postgres_connect_kwargs(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "dbname": self.dbname,
            "user": self.user,
            "password": self.password,
            "connect_timeout": self.connect_timeout,
            "application_name": "sites-api",
            # Session default: the server cancels any statement running longer than
            # this, so a wedged query surfaces as an error instead of a hung thread.
            "options": f"-c statement_timeout={self.statement_timeout * 1000}",
            "sslmode": self.sslmode,
            # TCP keepalives catch the other failure shape: the peer (or a NAT in
            # between) silently vanished while the connection was idle. Without them
            # the next statement waits on a dead socket until the kernel gives up,
            # which is minutes to hours. 10s idle + 3 probes at 5s = dead within ~25s.
            "keepalives": 1,
            "keepalives_idle": 10,
            "keepalives_interval": 5,
            "keepalives_count": 3,
        }

# The same table creation statement is used in two places: sites_deployments / sites_tenants, multiple merchants are created during normal startup
# During migration, a *_new shadow table keeps the primary-key rewrite and
# data copy atomic.
# So the table names must be interchangeable - sooner or later one of the two handwritten column lists will miss the new column.
_DEPLOYMENTS_TABLE_TEMPLATE = """
CREATE TABLE IF NOT EXISTS {table} (
    merchant_id {id_type} NOT NULL,
    user_id {id_type} NOT NULL,
    service_name {id_type} NOT NULL,
    cr_name {id_type} NOT NULL UNIQUE,
    image {id_type} NOT NULL,
    port INTEGER NOT NULL CHECK (port BETWEEN 1 AND 65535),
    health_path {id_type} NOT NULL,
    revision {id_type} NOT NULL,
    exposure {id_type} NOT NULL DEFAULT 'public'
        CHECK (exposure IN ('public', 'internal')),
    spec {json_type} NOT NULL,
    phase {id_type} NOT NULL,
    message {text_type} NOT NULL DEFAULT '',
    url {text_type},
    created_at {ts_type} NOT NULL DEFAULT {now},
    updated_at {ts_type} NOT NULL DEFAULT {now},
    deletion_requested_at {ts_type},
    deleted_at {ts_type},
    PRIMARY KEY (merchant_id, user_id, service_name)
)
"""

# cr_name remains global UNIQUE: it is the object name in Kubernetes, and Kubernetes does not know the merchant
# That's the thing - uniqueness has to hold right here. Snapshot recycling can only get cr_name.
_TENANTS_TABLE_TEMPLATE = """
CREATE TABLE IF NOT EXISTS {table} (
    merchant_id {id_type} NOT NULL,
    user_id {id_type} NOT NULL,
    token_sha256 {id_type} NOT NULL UNIQUE,
    max_deployments INTEGER NOT NULL,
    max_public_routes INTEGER NOT NULL,
    created_at {ts_type} NOT NULL DEFAULT {now},
    disabled_at {ts_type},
    PRIMARY KEY (merchant_id, user_id)
)
"""

# Merchant table. Field meaning and boundaries:
#   merchant_id Merchant ID, lowercase alphanumeric hyphen 1-31 characters, immutable after creation (it is
#                    The derivation of Namespace name and CR name, changing the name is equivalent to replacing all K8s objects)
#   display_name is the name displayed on the management console, variable, externally controllable text (must be escaped for rendering)
#   api_key_sha256 sha256 hexadecimal digest of merchant API key, required, globally unique;
#                    The plaintext is only returned once at the moment of issuance, and the library always only has the digest.
#   max_tenants The maximum number of tenant rows under this merchant's name, required, variable
#   max_deployments The maximum number of active deployments for **all tenants in total** under the merchant's name, required, variable;
#                    There are two levels with sites_tenants.max_deployments, and the merchant level is judged first.
#   created_at creation time, immutable
#   disabled_at is the time of deactivation; if it is not empty, it means it is disabled. All certificate paths of the merchant (including tenants under its name)
#                    own token) will be rejected. Rotating the key will clear it back to NULL
_MERCHANTS_TABLE_TEMPLATE = """
CREATE TABLE IF NOT EXISTS sites_merchants (
    merchant_id {id_type} PRIMARY KEY,
    display_name {id_type} NOT NULL,
    api_key_sha256 {id_type} NOT NULL UNIQUE,
    max_tenants INTEGER NOT NULL,
    max_deployments INTEGER NOT NULL,
    created_at {ts_type} NOT NULL DEFAULT {now},
    disabled_at {ts_type}
)
"""

# Resource limit per merchant. When this table was born, migrate() did not have an increment mechanism, and adding columns could only be detected by dialect.
# so create a separate table. Now versioned
# The migration is implemented, the new structural changes should be registered as a new version step - but "no line = use deployment-level defaults"
# This semantics is still worth retaining: existing merchants are naturally backwards compatible.
_MERCHANT_RESOURCES_TABLE_TEMPLATE = """
CREATE TABLE IF NOT EXISTS sites_merchant_resources (
    merchant_id {id_type} PRIMARY KEY,
    cpu_limit {id_type} NOT NULL,
    memory_limit {id_type} NOT NULL,
    pod_limit {id_type} NOT NULL,
    updated_at {ts_type} NOT NULL DEFAULT {now}
)
"""

# Migration version table: version is monotonically increasing, with one row for each registered step. It uses IF NOT
# The bootstrap built by EXISTS does not count the version step; empty table = this library does not have any registered versions yet
# (A completely new library, or an older version that hasn't caught up with the versioning mechanism - the latter is covered by the idempotent criterion of the step itself).
_SCHEMA_VERSIONS_TABLE_TEMPLATE = """
CREATE TABLE IF NOT EXISTS sites_schema_migrations (
    version INTEGER PRIMARY KEY,
    description {id_type} NOT NULL,
    applied_at {ts_type} NOT NULL DEFAULT {now}
)
"""

_APPLIED_SCHEMA_VERSIONS_SQL = """
SELECT version FROM sites_schema_migrations
"""

# DO NOTHING instead of bare INSERT: If two processes are cold started at the same time, they will each read the version table as empty and run separately.
# After one idempotent step, the one submitted later should not be marked as a false failure due to a primary key conflict (since the steps are idempotent,
# Records are just accounts, there is no need to rule out concurrency).
_SCHEMA_VERSION_INSERT_TEMPLATE = """
INSERT INTO sites_schema_migrations (version, description)
VALUES ({ph}, {ph})
ON CONFLICT (version) DO NOTHING
"""

_CANONICAL_DEPLOYMENTS_TABLE = "sites_deployments"
_CANONICAL_TENANTS_TABLE = "sites_tenants"

_SCHEMA_TEMPLATES = (
    _MERCHANTS_TABLE_TEMPLATE,
    _MERCHANT_RESOURCES_TABLE_TEMPLATE,
    _DEPLOYMENTS_TABLE_TEMPLATE.replace("{table}", _CANONICAL_DEPLOYMENTS_TABLE),
    """
    CREATE INDEX IF NOT EXISTS sites_deployments_phase_idx
    ON sites_deployments (phase)
    """,
    _TENANTS_TABLE_TEMPLATE.replace("{table}", _CANONICAL_TENANTS_TABLE),
    """
    CREATE INDEX IF NOT EXISTS sites_tenants_merchant_idx
    ON sites_tenants (merchant_id)
    """,
)

_LEGACY_DEPLOYMENTS_TABLE = "appforge_deployments"
_LEGACY_TENANTS_TABLE = "appforge_tenants"
_LEGACY_PHASE_INDEX = "appforge_deployments_phase_idx"

# coexistence of legacy and canonical names = list of names to be looked at for rejection criteria (see _refuse_conflicting_
# table_names): Checked every time it is started, regardless of which step the version table is recorded.
_CONFLICTING_TABLE_NAMES = (
    _LEGACY_DEPLOYMENTS_TABLE,
    _LEGACY_TENANTS_TABLE,
    _CANONICAL_DEPLOYMENTS_TABLE,
    _CANONICAL_TENANTS_TABLE,
)

# Used for migrating data. Columns must be written in full one by one: write one less column and let it fall on DEFAULT, created_at will
# It was erased as a moment of migration, and the damage was completely invisible afterwards.
_MIGRATION_COPY_TEMPLATES = {
    _CANONICAL_DEPLOYMENTS_TABLE: """
INSERT INTO {shadow} (
    merchant_id, user_id, service_name, cr_name, image, port, health_path,
    revision, spec, phase, message, url, created_at, updated_at,
    deletion_requested_at, deleted_at
)
SELECT
    {ph}, user_id, service_name, cr_name, image, port, health_path,
    revision, spec, phase, message, url, created_at, updated_at,
    deletion_requested_at, deleted_at
FROM sites_deployments
""",
    _CANONICAL_TENANTS_TABLE: """
INSERT INTO {shadow} (
    merchant_id, user_id, token_sha256, max_deployments, max_public_routes,
    created_at, disabled_at
)
SELECT
    {ph}, user_id, token_sha256, max_deployments, max_public_routes,
    created_at, disabled_at
FROM sites_tenants
""",
}


# ---------------------------------------------------------------------------
# Sequential migration: Each structural schema change is registered as a step with a version number, and is run sequentially at startup
# Those that are not yet in the version table. The previous two structural migrations (legacy name change, multi-merchant addition) were invented separately.
# The criterion for "detecting the current form" is set once - the third time will exceed the carrying capacity of a single idempotent bootstrap.
# So turn "which step you are currently at" into an explicit record, instead of working backwards from the shape of the library each time.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SchemaStep:
    """One ordered schema migration, committed (with its record) as one unit."""

    version: int
    description: str
    # The signature is (store, cursor): Use the ready-made transaction loop for the step body, never open _cursor yourself——
    # The two lower layers of _cursor in the PG dialect share the same connection. Exiting the inner layer will commit the outer layer's transactions in advance.
    # (See the "not nestable" boundary of Store._cursor).
    apply: Callable[["Store", Any], None]


# Orderly registry. list instead of tuple: registration entry is the only way to write it, and the order is guaranteed by it.
_SCHEMA_STEPS: list[_SchemaStep] = []


def _register_schema_step(
    version: int, description: str, apply: Callable[["Store", Any], None]
) -> None:
    """Register the next schema step. The version must be exactly one more: no number hopping, no queue jumping.

    This is all the use case for "the next person to write the schema changes": write a (store, cursor) -> None
    The idempotent function (DDL dialect template) is registered with an increasing version number. Steps to convert v2+ before submission
    Merging it into a rewrite of the existing v1 is equivalent to erasing the history of all libraries that have been stamped v1 - once the version is
    Once published, it cannot be modified, only appended.
    """
    expected = _SCHEMA_STEPS[-1].version + 1 if _SCHEMA_STEPS else 1
    if version != expected:
        raise ValueError(
            f"schema step version {version} is out of order; "
            f"the next step must be {expected}"
        )
    _SCHEMA_STEPS.append(_SchemaStep(version, description, apply))


def _apply_v1_schema(store: "Store", cursor: Any) -> None:
    """v1: The current full set of table structures, merging the two structural migrations that have occurred in history.

    Idempotence is deliberate, and it is not a makeshift: the old library without a version table may be in three historical forms (appforge_*
    Before the name change / single merchant has no merchant_id column / new form created by old migrate()), it is necessary to distinguish them
    The third set of detection criteria was invented. Therefore, for libraries that do not have version records, v1 must be completely run - the criteria are correct
    The new form of the library is all no-op, and the older form is a true upgrade.
    """
    store._migrate_legacy_tables(cursor)
    store._migrate_multi_merchant(cursor)
    for template in _SCHEMA_TEMPLATES:
        cursor.execute(store._sql(template))


_register_schema_step(
    1,
    "canonical Sites schema: absorbing the appforge rename and the "
    "merchant dimension",
    _apply_v1_schema,
)


def _apply_v2_deployment_exposure(store: "Store", cursor: Any) -> None:
    """v2: make deployment access scope a first-class snapshot field.

    Existing rows already carry the authoritative value inside ``spec``.  The
    dedicated column keeps list reads bounded while allowing clients to tell a
    healthy internal service from a broken public deployment with no URL.
    """
    columns = store._table_columns(cursor, _CANONICAL_DEPLOYMENTS_TABLE)
    if "exposure" not in columns:
        cursor.execute(
            store._sql(
                "ALTER TABLE sites_deployments ADD COLUMN exposure {id_type} "
                "NOT NULL DEFAULT 'public' "
                "CHECK (exposure IN ('public', 'internal'))"
            )
        )
    cursor.execute(
        "UPDATE sites_deployments SET exposure = "
        "COALESCE(spec ->> 'exposure', 'public')"
    )


_register_schema_step(
    2,
    "persist SiteDeployment exposure for truthful artifact presentation",
    _apply_v2_deployment_exposure,
)


def _apply_v3_deployment_runtime_state(
    store: "Store", cursor: Any
) -> None:
    """v3: persist whether a converged deployment currently has replicas.

    ``phase=Running`` means the desired deployment state converged.  For a
    scale-to-zero site that desired state may legitimately be zero replicas.
    Persisting the observed replica count separately lets administrative lists
    distinguish a dormant service from one with a live worker without turning
    a successful scale-down into a lifecycle failure.
    """
    columns = store._table_columns(cursor, _CANONICAL_DEPLOYMENTS_TABLE)
    if "scale_to_zero" not in columns:
        cursor.execute(
            "ALTER TABLE sites_deployments ADD COLUMN scale_to_zero BOOLEAN "
            "NOT NULL DEFAULT FALSE"
        )
    if "observed_replicas" not in columns:
        cursor.execute(
            "ALTER TABLE sites_deployments ADD COLUMN observed_replicas INTEGER"
        )

    # The authoritative opt-in flag is already in the stored CR spec.  Replicas
    # cannot be reconstructed from spec; the next snapshot populates that NULL.
    cursor.execute(
        "UPDATE sites_deployments SET scale_to_zero = "
        "CASE WHEN spec ->> 'scaleToZero' = 'true' "
        "THEN TRUE ELSE FALSE END"
    )


_register_schema_step(
    3,
    "persist deployment runtime scale state",
    _apply_v3_deployment_runtime_state,
)

_SITES_CATALOG_TABLE_TEMPLATE = """
CREATE TABLE IF NOT EXISTS sites_catalog (
    merchant_id {id_type} NOT NULL,
    user_id {id_type} NOT NULL,
    site_name {id_type} NOT NULL,
    site_type {id_type} NOT NULL CHECK (site_type IN ('static', 'dynamic')),
    current_version INTEGER,
    created_at {ts_type} NOT NULL DEFAULT {now},
    updated_at {ts_type} NOT NULL DEFAULT {now},
    PRIMARY KEY (merchant_id, user_id, site_name)
)
"""

_SITE_VERSIONS_TABLE_TEMPLATE = """
CREATE TABLE IF NOT EXISTS sites_versions (
    merchant_id {id_type} NOT NULL,
    user_id {id_type} NOT NULL,
    site_name {id_type} NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    content_sha256 {id_type} NOT NULL,
    artifact_uri {text_type},
    image {text_type},
    database_schema {id_type},
    metadata {json_type} NOT NULL DEFAULT '{{}}'{json_cast},
    created_at {ts_type} NOT NULL DEFAULT {now},
    PRIMARY KEY (merchant_id, user_id, site_name, version),
    FOREIGN KEY (merchant_id, user_id, site_name)
        REFERENCES sites_catalog (merchant_id, user_id, site_name)
)
"""


def _apply_v4_site_versions(store: "Store", cursor: Any) -> None:
    cursor.execute(store._sql(_SITES_CATALOG_TABLE_TEMPLATE))
    cursor.execute(store._sql(_SITE_VERSIONS_TABLE_TEMPLATE))


_register_schema_step(
    4,
    "immutable static and dynamic site versions with promotion pointer",
    _apply_v4_site_versions,
)

_SITE_VERSION_MIGRATIONS_TABLE_TEMPLATE = """
CREATE TABLE IF NOT EXISTS sites_version_migrations (
    merchant_id {id_type} NOT NULL,
    user_id {id_type} NOT NULL,
    site_name {id_type} NOT NULL,
    version INTEGER NOT NULL,
    migration_sha256 {id_type},
    migration_sql {text_type},
    status {id_type} NOT NULL
        CHECK (status IN ('not-required', 'pending', 'running', 'succeeded', 'failed')),
    started_at {ts_type},
    applied_at {ts_type},
    error {text_type},
    PRIMARY KEY (merchant_id, user_id, site_name, version),
    FOREIGN KEY (merchant_id, user_id, site_name, version)
        REFERENCES sites_versions (merchant_id, user_id, site_name, version),
    CHECK (
        (status = 'not-required' AND migration_sha256 IS NULL AND migration_sql IS NULL)
        OR
        (status <> 'not-required' AND migration_sha256 IS NOT NULL AND migration_sql IS NOT NULL)
    )
)
"""


def _apply_v5_site_version_migrations(store: "Store", cursor: Any) -> None:
    cursor.execute(store._sql(_SITE_VERSION_MIGRATIONS_TABLE_TEMPLATE))
    cursor.execute(
        "INSERT INTO sites_version_migrations "
        "(merchant_id, user_id, site_name, version, status) "
        "SELECT merchant_id, user_id, site_name, version, 'not-required' "
        "FROM sites_versions ON CONFLICT DO NOTHING"
    )


_register_schema_step(
    5,
    "bind controlled database migration artifacts to immutable site versions",
    _apply_v5_site_version_migrations,
)

# v6 columns on sites_merchants. Both are credential policy, not merchant identity:
#   may_act_as_subjects  whether this key may resolve X-Acting-Subject. Default FALSE, so an
#                        existing key gains nothing from the upgrade - impersonation is an
#                        explicit grant on the caller's own credential (K8s impersonation
#                        semantics), never a global switch.
#   key_expires_at       expiry of the *current* key. NULL means "no expiry" and stays the
#                        default: a TTL that every existing deployment suddenly has would
#                        expire live merchant keys on upgrade. Rotation sets it.
_MERCHANT_KEY_POLICY_STATEMENTS = (
    "ALTER TABLE sites_merchants ADD COLUMN IF NOT EXISTS "
    "may_act_as_subjects BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE sites_merchants ADD COLUMN IF NOT EXISTS "
    "key_expires_at {ts_type}",
)


def _apply_v6_merchant_key_policy(store: "Store", cursor: Any) -> None:
    for statement in _MERCHANT_KEY_POLICY_STATEMENTS:
        cursor.execute(store._sql(statement))
    # Keys that already exist get the standard lifetime counted from the upgrade rather than
    # keeping the unlimited one they were issued with. Leaving them alone would mean the
    # oldest keys in the system - the ones most likely to have leaked - are the only ones
    # that still never expire.
    cursor.execute(
        "UPDATE sites_merchants SET key_expires_at = "
        f"NOW() + INTERVAL '{DEFAULT_MERCHANT_KEY_TTL_SECONDS} seconds' "
        "WHERE key_expires_at IS NULL"
    )


_register_schema_step(
    6,
    "bound merchant API keys with an expiry and an explicit impersonation grant",
    _apply_v6_merchant_key_policy,
)

_UPSERT_TEMPLATE = """
INSERT INTO sites_deployments (
    merchant_id, user_id, service_name, cr_name, image, port, health_path,
    revision, exposure, scale_to_zero, observed_replicas, spec, phase,
    message, url
) VALUES (
    {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph},
    {ph}{json_cast}, {ph}, {ph}, {ph}
)
ON CONFLICT (merchant_id, user_id, service_name) DO UPDATE SET
    cr_name = EXCLUDED.cr_name,
    image = EXCLUDED.image,
    port = EXCLUDED.port,
    health_path = EXCLUDED.health_path,
    revision = EXCLUDED.revision,
    exposure = EXCLUDED.exposure,
    scale_to_zero = EXCLUDED.scale_to_zero,
    observed_replicas = EXCLUDED.observed_replicas,
    spec = EXCLUDED.spec,
    phase = EXCLUDED.phase,
    message = EXCLUDED.message,
    url = EXCLUDED.url,
    updated_at = {now},
    deletion_requested_at = CASE
        WHEN EXCLUDED.phase = 'Deleting'
        THEN COALESCE(
            sites_deployments.deletion_requested_at,
            {now}
        )
        ELSE NULL
    END,
    deleted_at = NULL
"""

_MERCHANT_RESOURCES_UPSERT_TEMPLATE = """
INSERT INTO sites_merchant_resources (
    merchant_id, cpu_limit, memory_limit, pod_limit
) VALUES ({ph}, {ph}, {ph}, {ph})
ON CONFLICT (merchant_id) DO UPDATE SET
    cpu_limit = EXCLUDED.cpu_limit,
    memory_limit = EXCLUDED.memory_limit,
    pod_limit = EXCLUDED.pod_limit
"""

_POSTGRES = _Dialect(
    name="postgresql",
    placeholder="%s",
    now="NOW()",
    json_cast="::jsonb",
    json_type="JSONB",
    timestamp_type="TIMESTAMPTZ",
    identifier_type="TEXT",
    text_type="TEXT",
    schema_version_insert=_SCHEMA_VERSION_INSERT_TEMPLATE,
    deployment_upsert=_UPSERT_TEMPLATE,
    merchant_resources_upsert=_MERCHANT_RESOURCES_UPSERT_TEMPLATE,
)

# List and administrative reads intentionally stay compact. The single-record
# lookup below additionally loads ``spec`` because version status needs the
# immutable ``siteVersion`` binding saved from the CR.
_DEPLOYMENT_READ_COLUMNS = """
    merchant_id, user_id, service_name, cr_name, image, port, health_path,
    revision, exposure, scale_to_zero, observed_replicas, phase, message,
    url, created_at, updated_at, deletion_requested_at, deleted_at
"""

_SELECT_TEMPLATE = f"""
SELECT
    merchant_id, user_id, service_name, cr_name, image, port, health_path,
    revision, exposure, scale_to_zero, observed_replicas, spec, phase, message,
    url, created_at, updated_at, deletion_requested_at, deleted_at
FROM sites_deployments
WHERE merchant_id = {{ph}} AND user_id = {{ph}} AND service_name = {{ph}}
"""

_LIST_TEMPLATE = f"""
SELECT{_DEPLOYMENT_READ_COLUMNS}
FROM sites_deployments
WHERE merchant_id = {{ph}} AND user_id = {{ph}} AND deleted_at IS NULL
ORDER BY updated_at DESC
LIMIT {{ph}}
"""

# Management-side aggregation: The only read path without tenant filtering. The conditions are based on which parameters the caller gave, so
# The statement is divided into two halves, the head and the tail, and are spliced in the method - after the pieces are put together, they are rendered once in dialect, {ph} in the conditional fragment
# It's the same substitution as {ph} here.
_LIST_ALL_HEAD = f"""
SELECT{_DEPLOYMENT_READ_COLUMNS}
FROM sites_deployments
WHERE deleted_at IS NULL"""

_LIST_ALL_TAIL = """
ORDER BY updated_at DESC
LIMIT {ph}
"""

_COUNT_BY_MERCHANT_SQL = """
SELECT merchant_id, COUNT(*)
FROM sites_deployments
WHERE deleted_at IS NULL
GROUP BY merchant_id
"""

_ACTIVE_DEPLOYMENT_COUNT_SQL = """
SELECT COUNT(*)
FROM sites_deployments
WHERE deleted_at IS NULL
"""

_RECORD_COLUMNS = (
    "merchant_id",
    "user_id",
    "service_name",
    "cr_name",
    "image",
    "port",
    "health_path",
    "revision",
    "exposure",
    "scale_to_zero",
    "observed_replicas",
    "phase",
    "message",
    "url",
    "created_at",
    "updated_at",
    "deletion_requested_at",
    "deleted_at",
)

_SELECT_RECORD_COLUMNS = (
    *_RECORD_COLUMNS[:11],
    "spec",
    *_RECORD_COLUMNS[11:],
)


def site_deployment_values(
    obj: dict[str, Any],
    *,
    phase: str | None = None,
    message: str | None = None,
) -> tuple[Any, ...]:
    """Map a SiteDeployment JSON object to the stable database upsert contract."""
    metadata = obj.get("metadata") or {}
    spec = obj.get("spec") or {}
    status = obj.get("status") or {}
    observed_replicas = status.get("observedReplicas")
    resolved_phase = phase or status.get("phase") or "Pending"
    if metadata.get("deletionTimestamp"):
        resolved_phase = "Deleting"
    return (
        # The merchantID is only taken from the spec without opening another parameter: the same fact has two sources, sooner or later
        # There will be a transmission error and the record will be written under the name of another merchant. If the key is missing, let it raise KeyError——
        # CRD sets merchantID as required. If it is missing, there is something wrong with the operator or CRD. It should not be
        # Quietly fall back to the default merchant.
        str(spec["merchantID"]),
        str(spec["userID"]),
        str(spec["serviceName"]),
        str(metadata["name"]),
        str(spec["image"]),
        int(spec["port"]),
        str(spec["healthPath"]),
        str(spec.get("revision", "1")),
        str(spec.get("exposure", "public")),
        bool(spec.get("scaleToZero")),
        (
            int(observed_replicas)
            if observed_replicas is not None
            else None
        ),
        json.dumps(spec, ensure_ascii=False, separators=(",", ":")),
        resolved_phase,
        message if message is not None else str(status.get("message", "")),
        status.get("url"),
    )


def _cr_name_of(obj: Any) -> str:
    """The CR's ``metadata.name``, or "" when the object is too broken to have one.

    Deliberately not throwing: This is the only positioning key for snapshot recycling, and "unreadable name" itself means that the caller must treat it differently
    A result, not an error.
    """
    if not isinstance(obj, dict):
        return ""
    metadata = obj.get("metadata")
    if not isinstance(metadata, dict):
        return ""
    name = metadata.get("name")
    return name if isinstance(name, str) and name else ""


def _log_skipped_cr(name: str, exc: BaseException) -> None:
    """A CR that is not synchronized must leave traces, otherwise skipping will look exactly the same as "none in the first place"."""
    telemetry.log_exception(
        "snapshot_cr_skipped", exc, name=name or "<unnamed>", kind="sitedeployment"
    )


@dataclass(frozen=True)
class SyncSnapshotResult:
    """What one snapshot reconciliation actually managed to do.

    ``skipped`` non-zero means that the database snapshot is inconsistent with the cluster, which is completely different from "database hangs":
    The latter will throw StorageError, the former is a silent drift, only the count can see it.
    ``reclaimed`` is false to indicate that there is no soft deletion in this round.
    """

    synced: int
    skipped: int
    soft_deleted: int
    reclaimed: bool


def _default_connect(**kwargs: Any) -> Any:
    try:
        import psycopg
    except ImportError as exc:
        raise StorageError("PostgreSQL driver is unavailable") from exc
    return psycopg.connect(**kwargs)


# deleted_at and phase='Deleted' must be written together: all "is it still alive" criteria (list, usage,
# Migration pre-check) all looks at deleted_at IS NULL, which is exactly what the API returns when Kubernetes returns 404
# Use this method to mark records as Deleted. Without this branch, deployments that are no longer in the cluster will remain active forever.
# In the collection - the usage is too high, and multi-merchant migration will exclude people by "deleting these active deployments through the API first".
# COALESCE guarantees idempotence: duplicate marking will not push the deletion time to the most recent call.
_SET_STATUS_TEMPLATE = """
UPDATE sites_deployments
SET phase = {ph},
    message = {ph},
    url = COALESCE({ph}, url),
    updated_at = {now},
    deletion_requested_at = CASE
        WHEN {ph} = 'Deleting'
        THEN COALESCE(deletion_requested_at, {now})
        ELSE deletion_requested_at
    END,
    deleted_at = CASE
        WHEN {ph} = 'Deleted'
        THEN COALESCE(deleted_at, {now})
        ELSE deleted_at
    END
WHERE merchant_id = {ph} AND user_id = {ph} AND service_name = {ph}
"""

_ACTIVE_NAMES_SQL = """
SELECT cr_name
FROM sites_deployments
WHERE deleted_at IS NULL
"""

_SOFT_DELETE_TEMPLATE = """
UPDATE sites_deployments
SET phase = 'Deleted',
    message = 'SiteDeployment no longer exists',
    updated_at = {now},
    deleted_at = {now}
WHERE cr_name = {ph} AND deleted_at IS NULL
"""


_TENANT_COLUMNS = (
    "merchant_id",
    "user_id",
    "token_sha256",
    "max_deployments",
    "max_public_routes",
    "created_at",
    "disabled_at",
)

_TENANT_READ_COLUMNS = """
    merchant_id, user_id, token_sha256, max_deployments, max_public_routes,
    created_at, disabled_at
"""

_TENANT_INSERT_TEMPLATE = """
INSERT INTO sites_tenants (
    merchant_id, user_id, token_sha256, max_deployments, max_public_routes
) VALUES ({ph}, {ph}, {ph}, {ph}, {ph})
"""

# The token is globally unique, so this query does not require merchant - instead, it must include merchant_id
# Return together: The token is the only entry on the authentication side that can determine the identity of the merchant.
_TENANT_BY_TOKEN_TEMPLATE = f"""
SELECT{_TENANT_READ_COLUMNS}
FROM sites_tenants
WHERE token_sha256 = {{ph}} AND disabled_at IS NULL
"""

_TENANT_BY_ID_TEMPLATE = f"""
SELECT{_TENANT_READ_COLUMNS}
FROM sites_tenants
WHERE merchant_id = {{ph}} AND user_id = {{ph}}
"""

_TENANT_LIST_TEMPLATE = f"""
SELECT{_TENANT_READ_COLUMNS}
FROM sites_tenants
ORDER BY merchant_id, created_at
"""

_TENANT_LIST_BY_MERCHANT_TEMPLATE = f"""
SELECT{_TENANT_READ_COLUMNS}
FROM sites_tenants
WHERE merchant_id = {{ph}}
ORDER BY created_at
"""

_TENANT_COUNT_TEMPLATE = """
SELECT COUNT(*)
FROM sites_tenants
WHERE merchant_id = {ph}
"""

_TENANT_DISABLE_TEMPLATE = """
UPDATE sites_tenants
SET disabled_at = {now}
WHERE merchant_id = {ph} AND user_id = {ph} AND disabled_at IS NULL
"""

# Reissue the token and deactivate it by the way: someone comes to sign a new certificate for this tenant, indicating that it should be alive.
# Deactivation only clears disabled_at but the record is still there. Without this path, a disabled name will be permanently occupied.
# The only constraint is that no tenant with the same name can be built, and there is no restored entrance.
_TENANT_ROTATE_TEMPLATE = """
UPDATE sites_tenants
SET token_sha256 = {ph},
    disabled_at = NULL
WHERE merchant_id = {ph} AND user_id = {ph}
"""


_MERCHANT_COLUMNS = (
    "merchant_id",
    "display_name",
    "api_key_sha256",
    "max_tenants",
    "max_deployments",
    "created_at",
    "disabled_at",
    "may_act_as_subjects",
    "key_expires_at",
)

_MERCHANT_READ_COLUMNS = """
    merchant_id, display_name, api_key_sha256, max_tenants, max_deployments,
    created_at, disabled_at, may_act_as_subjects, key_expires_at
"""

_MERCHANT_INSERT_TEMPLATE = """
INSERT INTO sites_merchants (
    merchant_id, display_name, api_key_sha256, max_tenants, max_deployments,
    may_act_as_subjects, key_expires_at
) VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})
"""

# Deactivated merchants cannot be found by key: the certificate itself should be invalid. The admin console needs to check the deactivated merchants.
# merchant() / list_merchants(), those two are not filtered - otherwise deactivation will be equivalent to disappearing from the console.
# There is no entrance to recovery.
# An expired key is filtered in the same statement as an unknown one, so both end in the
# single 401 of identity._identity_from_credential. Answering "expired" separately would
# turn this endpoint into an oracle for "this key digest exists".
_MERCHANT_BY_KEY_TEMPLATE = f"""
SELECT{_MERCHANT_READ_COLUMNS}
FROM sites_merchants
WHERE api_key_sha256 = {{ph}} AND disabled_at IS NULL
  AND (key_expires_at IS NULL OR key_expires_at > {{now}})
"""

_MERCHANT_BY_ID_TEMPLATE = f"""
SELECT{_MERCHANT_READ_COLUMNS}
FROM sites_merchants
WHERE merchant_id = {{ph}}
"""

_MERCHANT_LIST_SQL = f"""
SELECT{_MERCHANT_READ_COLUMNS}
FROM sites_merchants
ORDER BY created_at
"""

# The expiry is written by the same statement that installs the digest: a key and its
# lifetime are one fact. Two statements leave a window where the new key carries the
# previous key's expiry.
_MERCHANT_ROTATE_TEMPLATE = """
UPDATE sites_merchants
SET api_key_sha256 = {ph},
    key_expires_at = {ph},
    disabled_at = NULL
WHERE merchant_id = {ph}
"""

_MERCHANT_DISABLE_TEMPLATE = """
UPDATE sites_merchants
SET disabled_at = {now}
WHERE merchant_id = {ph} AND disabled_at IS NULL
"""


class Store:
    """Deployment records stored in PostgreSQL.

    Responsibilities: Responsible for the reading and writing of deployment business records and snapshot convergence; not responsible for the Kubernetes state itself -
    That's the operator's business, only its observations are saved here.
    """

    def __init__(
        self,
        dialect: _Dialect,
        open_connection: Callable[[], Any],
        *,
        cache_connections: bool = True,
    ):
        self._dialect = dialect
        self._open_connection = open_connection
        self._cache_connections = cache_connections
        # Per-thread connection caching keeps PostgreSQL connections isolated.
        self._thread_state = threading.local()
        self._connections_lock = threading.Lock()
        self._connections: dict[int, Any] = {}

    @classmethod
    def postgres(
        cls,
        config: DatabaseConfig,
        *,
        connect: Callable[..., Any] | None = None,
        cache_connections: bool = True,
    ) -> "Store":
        connector = connect or _default_connect
        return cls(
            _POSTGRES,
            lambda: connector(**config.postgres_connect_kwargs()),
            cache_connections=cache_connections,
        )

    @property
    def backend(self) -> str:
        return self._dialect.name

    def _sql(self, template: str) -> str:
        return self._dialect.render(template)

    @contextlib.contextmanager
    def _transaction(self, connection: Any) -> Iterator[Any]:
        """Run one statement group as one transaction on ``connection``.

        In addition to commit/rollback, psycopg3's ``with connection`` will also
        Connection off** (unlike psycopg2). Use it as a transaction circle, which means "one operation per time"
        One of the root causes of "TCP+SCRAM handshake" also makes the connection cache useless; so PG dialect
        Manual commit/rollback, connection left to caller (cached or explicitly closed).

        The cursors of both drivers are not dependable context managers and must be turned off manually.
        """
        with tracing.span(
            "sites.storage.transaction",
            kind=3,
            attributes={"db.system": self._dialect.name},
        ):
            cursor = connection.cursor()
            try:
                yield cursor
            except BaseException:
                # Rollback itself fails (the connection is disconnected) and cannot overcome the business exception; the connection will then be
                # _cursor is deprecated, no need to save it here.
                with contextlib.suppress(Exception):
                    connection.rollback()
                raise
            else:
                connection.commit()
            finally:
                cursor.close()

    def _borrow_connection(self) -> Any:
        """PostgreSQL: take this thread's connection, opening one if needed.

        One connection per thread: psycopg's connections are not thread-safe, and there are no connections between threads.
        Transactions to be shared. Run a lightweight statement to detect activity before lending - the cached connection may be operated twice
        It was quietly disconnected by the network or the server. Instead of letting the business statement discover it, it is better to replace it here;
        The round trip is still far cheaper than a full TCP+SCRAM handshake.

        Connections arise and die with threads: when the thread exits, threading.local clears the reference, CPython reference
        Counting will turn it off on the spot. Requesting a threaded HTTP service therefore cannot be reused (it is new every time
        Threads), but they are no worse than before; what really benefits from reuse are long-lived threads - snapshot synchronizer,
        operator.
        """
        connection = getattr(self._thread_state, "connection", None)
        if connection is not None:
            try:
                health = connection.cursor()
                try:
                    health.execute("SELECT 1")
                finally:
                    health.close()
                return connection
            except Exception:
                self._discard_connection(connection)
        connection = self._open_connection()
        self._thread_state.connection = connection
        with self._connections_lock:
            self._connections[id(connection)] = connection
        return connection

    def _discard_connection(self, connection: Any) -> None:
        if getattr(self._thread_state, "connection", None) is connection:
            self._thread_state.connection = None
        with self._connections_lock:
            self._connections.pop(id(connection), None)
        with contextlib.suppress(Exception):
            connection.close()

    def close(self) -> None:
        """Close every cached PostgreSQL connection owned by this store."""
        with self._connections_lock:
            connections = tuple(self._connections.values())
            self._connections.clear()
        self._thread_state.connection = None
        for connection in connections:
            with contextlib.suppress(Exception):
                connection.close()

    @contextlib.contextmanager
    def _cursor(self) -> Iterator[Any]:
        """Run one statement group in a transaction.

        PostgreSQL: Borrow the cached connection of this thread (see _borrow_connection) without locking. affairs
        Abandon cached connections when ending in exception - _transaction has rolled back the transaction, but the connection has since
        The status is no longer worthy of trust (a disconnection or server restart may not be exposed until this moment), discard it next time
        Heavy handshake, conservative and cheap.

        Not nestable: The two lower layers of _cursor in the PG dialect share the same connection, and exiting the inner layer will cause the outer layer’s transactions to
        Commit in advance. There is no nested usage in the existing code. This boundary is written for people who will add methods in the future.
        """
        if not self._cache_connections:
            connection = self._open_connection()
            try:
                with self._transaction(connection) as cursor:
                    yield cursor
            finally:
                connection.close()
            return
        connection = self._borrow_connection()
        try:
            with self._transaction(connection) as cursor:
                yield cursor
        except Exception:
            self._discard_connection(connection)
            raise

    def migrate(self) -> None:
        """Bring the schema up to the newest registered step, one step per transaction.

        Transaction boundaries are the core of this mechanism: the statement of each step and its version record enter and exit the same transaction
        ——Crash in the middle, the completed steps will be left together with the record, restart from the breakpoint, and will not replay.
        PostgreSQL DDL is transactional, so a failed step and its version record
        are rolled back together.
        The version table is created by itself using IF NOT EXISTS, and does not count as a version step; if it does not exist, it will always start from v1
        Run each step (see _apply_v1_schema: Do not invent detection for "Where should the old library stamp?",
        The idempotent criterion of steps unifies the three historical forms). The default merchant is left at the end and does not belong to any
        Version step: It is a data seeding not a schema change, which runs idempotently every time it is started.
        """
        try:
            with self._cursor() as cursor:
                # The coexistence disablement of the same name is placed before the version distribution: it is an invariant that must be established every time it is launched.
                # It is not a criterion of "which step should be run" - after the version table is recorded as v1, this gate is still there.
                existing = self._existing_tables(cursor, _CONFLICTING_TABLE_NAMES)
                self._refuse_conflicting_table_names(existing)
                cursor.execute(self._sql(_SCHEMA_VERSIONS_TABLE_TEMPLATE))
                cursor.execute(_APPLIED_SCHEMA_VERSIONS_SQL)
                applied = {int(row[0]) for row in cursor.fetchall()}
            for step in _SCHEMA_STEPS:
                if step.version in applied:
                    continue
                with self._cursor() as cursor:
                    step.apply(self, cursor)
                    cursor.execute(
                        self._sql(self._dialect.schema_version_insert),
                        (step.version, step.description),
                    )
            with self._cursor() as cursor:
                self._ensure_default_merchant(cursor)
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError("database schema migration failed") from exc

    def _table_columns(self, cursor: Any, table: str) -> set[str]:
        """Column names of ``table``; empty when the table does not exist.

        The table name only comes from constants in this module.
        """
        cursor.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s",
            (table,),
        )
        return {str(row[0]) for row in cursor.fetchall()}

    def _migrate_multi_merchant(self, cursor: Any) -> None:
        """Add the merchant dimension to the two tenant-scoped tables.

        The idempotent criterion is "whether there is a merchant_id column in the table": empty database (neither table exists) and migrated database
        All libraries are returned directly, and subsequent CREATE TABLE IF NOT EXISTS will fill in the empty libraries.

        PostgreSQL uses a shadow table so the data copy and primary-key change
        remain one atomic migration.

        The price is that the constraint name on the PostgreSQL side will be suffixed with ``_new`` (``..._new_pkey`` /
        ``..._new_cr_name_key``). Constraints match by column, ON CONFLICT and uniqueness are not affected.
        It just looks awkward in psql.
        """
        tenant_columns = self._table_columns(cursor, _CANONICAL_TENANTS_TABLE)
        deployment_columns = self._table_columns(
            cursor, _CANONICAL_DEPLOYMENTS_TABLE
        )
        pending = [
            (table, template)
            for table, columns, template in (
                (
                    _CANONICAL_DEPLOYMENTS_TABLE,
                    deployment_columns,
                    _DEPLOYMENTS_TABLE_TEMPLATE,
                ),
                (
                    _CANONICAL_TENANTS_TABLE,
                    tenant_columns,
                    _TENANTS_TABLE_TEMPLATE,
                ),
            )
            if columns and "merchant_id" not in columns
        ]
        if not pending:
            return

        if _CANONICAL_DEPLOYMENTS_TABLE in {table for table, _ in pending}:
            self._refuse_migration_with_active_deployments(cursor)

        cursor.execute(self._sql(_MERCHANTS_TABLE_TEMPLATE))
        for table, template in pending:
            shadow = f"{table}_new"
            # Defensive cleanup for an interrupted or older migration.
            cursor.execute(f"DROP TABLE IF EXISTS {shadow}")
            cursor.execute(self._sql(template.replace("{table}", shadow)))
            cursor.execute(
                self._sql(_MIGRATION_COPY_TEMPLATES[table].replace(
                    "{shadow}", shadow
                )),
                (DEFAULT_MERCHANT_ID,),
            )
            cursor.execute(f"DROP TABLE {table}")
            cursor.execute(f"ALTER TABLE {shadow} RENAME TO {table}")
        # The index is not rebuilt here: DROP TABLE takes its index with it, and the following
        # _SCHEMA_TEMPLATES Use IF NOT EXISTS to create them back in the same transaction. write twice
        # Just one more list that will miss the new index.

    def _refuse_migration_with_active_deployments(self, cursor: Any) -> None:
        """Refuse to migrate while live deployments exist.

        The derivation formula of cr_name changes from (user, service) to (merchant, user, service):
        The name of the CR created before the migration did not match the one calculated by the new formula. After the migration, it became unrecyclable.
        Orphan (the old CR remains in the cluster, and a new one will be created on the next request).

        There is no "simply recalculate cr_name and then change the CR name" here: Kubernetes does not support object renaming, so it will be deleted.
        Rebuild - Migration scripts on the startup path should not silently rebuild other people's online workloads.
        """
        cursor.execute(_ACTIVE_DEPLOYMENT_COUNT_SQL)
        row = cursor.fetchone()
        active = int(row[0]) if row else 0
        if active:
            raise StorageError(
                f"{active} active deployment(s) exist; the multi-merchant "
                "migration changes cr_name derivation and would orphan their "
                "CRs. Delete them through the API first, then restart."
            )

    def _ensure_default_merchant(self, cursor: Any) -> None:
        """Make sure the fallback merchant exists on every startup.

        The admin token resolves to DEFAULT_MERCHANT_ID, so this line must exist - and it
        cannot simply be inserted by the migration: a brand-new database runs no migration
        at all, so the row would be missing on the very first start.

        The key uses a random value generated on the spot, and the plain text is discarded directly: this line is the identity rather than the credential entry.
        To use its API key, ask the administrator to explicitly rotate it once. Hard-coding a predictable summary equals
        Keep the same key for all deployments.
        """
        cursor.execute(
            self._sql(_MERCHANT_BY_ID_TEMPLATE), (DEFAULT_MERCHANT_ID,)
        )
        if cursor.fetchone() is not None:
            return
        cursor.execute(
            self._sql(_MERCHANT_INSERT_TEMPLATE),
            (
                DEFAULT_MERCHANT_ID,
                "Local",
                token_digest(new_merchant_api_key()),
                DEFAULT_MAX_TENANTS,
                DEFAULT_MERCHANT_MAX_DEPLOYMENTS,
                False,
                None,
            ),
        )

    def _existing_tables(self, cursor: Any, names: tuple[str, ...]) -> set[str]:
        """Which of ``names`` exists as tables.

        Table names only come from constants in this module.
        """
        quoted = ", ".join(f"'{name}'" for name in names)
        cursor.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() "
            f"AND table_name IN ({quoted})"
        )
        return {str(row[0]) for row in cursor.fetchall()}

    def _refuse_conflicting_table_names(self, existing: set[str]) -> None:
        """Refuse to start while pre-Sites and Sites tables share one schema.

        If both names exist, choosing either table would risk hiding records.
        Refuse the startup migration so an operator can reconcile them
        explicitly instead of silently losing one side.
        """
        pairs = (
            (_LEGACY_DEPLOYMENTS_TABLE, _CANONICAL_DEPLOYMENTS_TABLE),
            (_LEGACY_TENANTS_TABLE, _CANONICAL_TENANTS_TABLE),
        )
        for legacy, canonical in pairs:
            if legacy in existing and canonical in existing:
                raise StorageError(
                    f"both legacy table {legacy!r} and Sites table "
                    f"{canonical!r} exist; reconcile them before startup"
                )

    def _migrate_legacy_tables(self, cursor: Any) -> None:
        """Atomically rename the pre-Sites tables before creating new ones.

        Part of the v1 step (see _apply_v1_schema); migrate() before version distribution
        There is also a _refuse_conflicting_table_names pre-check. The same criterion is retained here.
        Make the step self-contained - no external prerequisites are required when calling it directly against the cursor.
        """
        existing = self._existing_tables(cursor, _CONFLICTING_TABLE_NAMES)
        self._refuse_conflicting_table_names(existing)
        pairs = (
            (_LEGACY_DEPLOYMENTS_TABLE, _CANONICAL_DEPLOYMENTS_TABLE),
            (_LEGACY_TENANTS_TABLE, _CANONICAL_TENANTS_TABLE),
        )
        for legacy, canonical in pairs:
            if legacy in existing:
                cursor.execute(f"ALTER TABLE {legacy} RENAME TO {canonical}")
        # A renamed table keeps its old named index. Recreate it with the
        # canonical identity below.
        cursor.execute(f"DROP INDEX IF EXISTS {_LEGACY_PHASE_INDEX}")

    def ping(self) -> None:
        try:
            with self._cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError("database health check failed") from exc

    def upsert_site_deployment(
        self,
        obj: dict[str, Any],
        *,
        phase: str | None = None,
        message: str | None = None,
    ) -> None:
        values = site_deployment_values(obj, phase=phase, message=message)
        try:
            with self._cursor() as cursor:
                cursor.execute(self._sql(self._dialect.deployment_upsert), values)
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError("deployment upsert failed") from exc

    def get_deployment(
        self, merchant_id: str, user_id: str, service_name: str
    ) -> dict[str, Any] | None:
        try:
            with self._cursor() as cursor:
                cursor.execute(
                    self._sql(_SELECT_TEMPLATE),
                    (merchant_id, user_id, service_name),
                )
                row = cursor.fetchone()
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError("deployment query failed") from exc
        if row is None:
            return None
        return dict(zip(_SELECT_RECORD_COLUMNS, row, strict=True))

    def list_deployments(
        self,
        merchant_id: str,
        user_id: str,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 200:
            raise ValueError("deployment list limit must be between 1 and 200")
        try:
            with self._cursor() as cursor:
                cursor.execute(
                    self._sql(_LIST_TEMPLATE), (merchant_id, user_id, limit)
                )
                rows = cursor.fetchall()
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError("deployment list query failed") from exc
        return [
            dict(zip(_RECORD_COLUMNS, row, strict=True))
            for row in rows
        ]

    def set_status(
        self,
        merchant_id: str,
        user_id: str,
        service_name: str,
        phase: str,
        message: str,
        *,
        url: str | None = None,
    ) -> None:
        try:
            with self._cursor() as cursor:
                cursor.execute(
                    self._sql(_SET_STATUS_TEMPLATE),
                    (
                        phase,
                        message,
                        url,
                        phase,
                        phase,
                        merchant_id,
                        user_id,
                        service_name,
                    ),
                )
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError("status update failed") from exc

    def sync_snapshot(self, items: list[dict[str, Any]]) -> SyncSnapshotResult:
        """Upsert current CRs and soft-delete records whose CR disappeared.

        A bad CR cannot freeze the entire platform reconciliation. The previous N times of upsert plus full soft delete were squeezed into the same transaction
        Here, any CR missing field (or port crossing CHECK, or cr_name colliding with another row) will
        Roll back the entire batch - this round did not even perform soft deletion, and the caller only printed one sentence to enter the next round, so
        Deployments that have disappeared from the cluster are permanently deleted_at IS NULL: they always appear in the list and are always included.
        Usage, has been blocking the migration of many merchants.

        Therefore, fault tolerance is performed line by line: good lines are written as usual, bad lines are skipped and counted, and soft deletions are run as usual. Three things must be true:

        1. **Existence and parsability are two different things**. A CR parsing failed, indicating "this round is not synchronized"
        Success", not "this CR is gone". Its name is still included in observed_names, so it will never be
        The following recycling is regarded as a disappeared CR and is soft deleted.
        2. **Entries whose names cannot even be read will not be recycled in this round**. Soft deletion is located according to cr_name and cannot be recognized.
        The entry means that any row may be it - there is no way to prove that a row "really does not exist in the cluster",
        It shouldn't be touched. Objects in a true cluster must have names. This refers to the situation where the collection itself is no longer trustworthy.
        3. **Single write failure cannot pollute the transaction**. PostgreSQL will set the entire transaction to
        aborted, all subsequent statements will fail; SAVEPOINT/ROLLBACK TO has the same syntax in both dialects, use it
        Circle the failure of each item in its own cell.

        The return count is exposed by the caller (currently api.py's synchronizer ignores return values). Real database failure
        StorageError is still thrown, and the synchronizer's retry logic still takes effect.
        """
        observed_names: set[str] = set()
        unidentified = 0
        rows: list[tuple[str, tuple[Any, ...]]] = []
        for item in items:
            name = _cr_name_of(item)
            if name:
                # Register first and then parse: CRs that fail to parse must also be counted as "still existing".
                observed_names.add(name)
            else:
                unidentified += 1
            try:
                rows.append((name, site_deployment_values(item)))
            except Exception as exc:
                _log_skipped_cr(name, exc)

        skipped = len(items) - len(rows)
        synced = 0
        try:
            with self._cursor() as cursor:
                for name, values in rows:
                    cursor.execute("SAVEPOINT sites_sync_item")
                    try:
                        cursor.execute(
                            self._sql(self._dialect.deployment_upsert), values
                        )
                    except Exception as exc:
                        cursor.execute("ROLLBACK TO SAVEPOINT sites_sync_item")
                        _log_skipped_cr(name, exc)
                        skipped += 1
                    else:
                        synced += 1
                    cursor.execute("RELEASE SAVEPOINT sites_sync_item")
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError("snapshot synchronization failed") from exc

        if unidentified:
            telemetry.log(
                "snapshot_reclamation_skipped",
                level="warn",
                unidentified=unidentified,
                message=(
                    "collection item(s) have no metadata.name, so no record "
                    "can be proven absent from the cluster"
                ),
            )
            return SyncSnapshotResult(
                synced=synced, skipped=skipped, soft_deleted=0, reclaimed=False
            )

        # Recycle and open a separate transaction: The omission of a few items above does not affect this, because the criterion observed_names is
        # It is calculated outside the library and does not depend on what was written in this round. The entire paragraph above throws StorageError that
        # The database is really unavailable and has been raised above - even soft deletion cannot be done in that case.
        soft_deleted = 0
        try:
            with self._cursor() as cursor:
                cursor.execute(_ACTIVE_NAMES_SQL)
                active_names = {str(row[0]) for row in cursor.fetchall()}
                for missing_name in active_names - observed_names:
                    cursor.execute(
                        self._sql(_SOFT_DELETE_TEMPLATE), (missing_name,)
                    )
                    soft_deleted += 1
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError("snapshot synchronization failed") from exc
        return SyncSnapshotResult(
            synced=synced,
            skipped=skipped,
            soft_deleted=soft_deleted,
            reclaimed=True,
        )

    # --- admin aggregation ---------------------------------------------
    def list_all_deployments(
        self,
        *,
        merchant_id: str | None = None,
        phase: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """List active deployments across every merchant and tenant.

        🔴 This is the only query in this module that allows no tenant filtering, and can only be protected by _require_admin
        Processor call. Any tenant path must go through list_deployments(merchant_id, user_id) ——
        Once it leaks into the tenant path, it is an override that delivers the full platform deployment list to any tenant.

        When merchant_id / phase is None, no corresponding condition is added.
        """
        if not 1 <= limit <= 200:
            raise ValueError("deployment list limit must be between 1 and 200")
        conditions = ""
        values: list[Any] = []
        if merchant_id is not None:
            conditions += " AND merchant_id = {ph}"
            values.append(merchant_id)
        if phase is not None:
            conditions += " AND phase = {ph}"
            values.append(phase)
        values.append(limit)
        sql = self._sql(_LIST_ALL_HEAD + conditions + _LIST_ALL_TAIL)
        try:
            with self._cursor() as cursor:
                cursor.execute(sql, tuple(values))
                rows = cursor.fetchall()
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError("deployment list query failed") from exc
        return [dict(zip(_RECORD_COLUMNS, row, strict=True)) for row in rows]

    def count_deployments_by_merchant(self) -> dict[str, int]:
        """Active deployment rows per merchant, for the console's usage column.

        Boundary: This is a number for display purposes only, not a quota criterion. Quotas inherit Kubernetes CR counts
        (Excluding tombstones with non-empty deletionTimestamp), because CR is what really occupies cluster resources;
        Database rows will briefly become inconsistent with the cluster before the snapshot converges.
        """
        try:
            with self._cursor() as cursor:
                cursor.execute(_COUNT_BY_MERCHANT_SQL)
                rows = cursor.fetchall()
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError("deployment count query failed") from exc
        return {str(row[0]): int(row[1]) for row in rows}

    # --- partial updates -----------------------------------------------
    def _update_row(
        self,
        table: str,
        updates: dict[str, Any],
        where: str,
        where_values: tuple[Any, ...],
        *,
        subject: str,
    ) -> None:
        """Write the caller-supplied subset of columns onto one row.

        The semantics of PATCH is "fields not provided are unchanged", so the list can only be determined at runtime. List all
        Literals come from the caller code, never from the request content - this is what can be spelled out with f-string here
        As a premise of SQL, values still take placeholders.

        Empty updates directly throw ValueError instead of silently succeeding: the latter will cause the PATCH processor to "a field"
        I haven't changed the "reported success" or "changed it".

        Bounds: Does not return the number of affected rows. This is a silent zero-row update when the target does not exist. The caller must distinguish
        404, you have to check it first - the same one as the existing rotate_tenant_token / disable_tenant
        Promise, no exceptions will be made here.
        """
        if not updates:
            raise ValueError(f"{subject} requires at least one field to change")
        assignments = ", ".join(f"{column} = {{ph}}" for column in updates)
        sql = self._sql(f"UPDATE {table} SET {assignments} WHERE {where}")
        try:
            with self._cursor() as cursor:
                cursor.execute(sql, (*updates.values(), *where_values))
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError(f"{subject} failed") from exc

    # --- tenants -------------------------------------------------------
    def create_tenant(
        self,
        merchant_id: str,
        user_id: str,
        token_sha256: str,
        *,
        max_deployments: int,
        max_public_routes: int,
    ) -> None:
        """Register a tenant. Only the digest of the token is stored, and the plaintext is returned to the caller only at the moment of creation."""
        try:
            with self._cursor() as cursor:
                cursor.execute(
                    self._sql(_TENANT_INSERT_TEMPLATE),
                    (
                        merchant_id,
                        user_id,
                        token_sha256,
                        max_deployments,
                        max_public_routes,
                    ),
                )
        except StorageError:
            raise
        except Exception as exc:
            # Only unique constraint conflicts are translated as "existing": if connection failure is also translated as this, the API will
            # If a database failure returns 409, the troubleshooter will be directed to check for a non-existent conflict.
            if _is_unique_violation(exc):
                raise StorageConflictError(
                    f"tenant {user_id!r} already exists under merchant "
                    f"{merchant_id!r}"
                ) from exc
            raise StorageError("tenant creation failed") from exc

    def tenant_by_token(self, token_sha256: str) -> dict[str, Any] | None:
        """Resolve a tenant from a token digest; disabled tenants resolve to None.

        The token is globally unique, so there is no need for merchant parameters here - the returned record contains merchant_id,
        The authentication side determines whether the merchant has been deactivated based on this.
        """
        return self._tenant_query(_TENANT_BY_TOKEN_TEMPLATE, (token_sha256,))

    def tenant(self, merchant_id: str, user_id: str) -> dict[str, Any] | None:
        return self._tenant_query(_TENANT_BY_ID_TEMPLATE, (merchant_id, user_id))

    def _tenant_query(
        self, template: str, values: tuple[Any, ...]
    ) -> dict[str, Any] | None:
        try:
            with self._cursor() as cursor:
                cursor.execute(self._sql(template), values)
                row = cursor.fetchone()
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError("tenant query failed") from exc
        if row is None:
            return None
        return dict(zip(_TENANT_COLUMNS, row, strict=True))

    def list_tenants(
        self, *, merchant_id: str | None = None
    ) -> list[dict[str, Any]]:
        """List tenants. merchant_id is None = all platforms, only the admin path can be adjusted like this."""
        if merchant_id is None:
            template, values = _TENANT_LIST_TEMPLATE, ()
        else:
            template, values = _TENANT_LIST_BY_MERCHANT_TEMPLATE, (merchant_id,)
        try:
            with self._cursor() as cursor:
                cursor.execute(self._sql(template), values)
                rows = cursor.fetchall()
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError("tenant list query failed") from exc
        return [dict(zip(_TENANT_COLUMNS, row, strict=True)) for row in rows]

    def count_tenants(self, merchant_id: str) -> int:
        """Tenant rows under one merchant, for the max_tenants check."""
        try:
            with self._cursor() as cursor:
                cursor.execute(self._sql(_TENANT_COUNT_TEMPLATE), (merchant_id,))
                row = cursor.fetchone()
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError("tenant count query failed") from exc
        return int(row[0]) if row else 0

    def update_tenant_quota(
        self,
        merchant_id: str,
        user_id: str,
        *,
        max_deployments: int | None = None,
        max_public_routes: int | None = None,
    ) -> None:
        """Change one tenant's quota. None means "this item does not change"."""
        updates: dict[str, Any] = {}
        if max_deployments is not None:
            updates["max_deployments"] = max_deployments
        if max_public_routes is not None:
            updates["max_public_routes"] = max_public_routes
        self._update_row(
            "sites_tenants",
            updates,
            "merchant_id = {ph} AND user_id = {ph}",
            (merchant_id, user_id),
            subject="tenant quota update",
        )

    def rotate_tenant_token(
        self, merchant_id: str, user_id: str, token_sha256: str
    ) -> None:
        """Issue a new token for a tenant and clear any disabled state.

        The old digest is directly overwritten, so the previous token becomes invalid immediately - exactly what you want after a leak.
        """
        try:
            with self._cursor() as cursor:
                cursor.execute(
                    self._sql(_TENANT_ROTATE_TEMPLATE),
                    (token_sha256, merchant_id, user_id),
                )
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError("tenant token rotation failed") from exc

    def disable_tenant(self, merchant_id: str, user_id: str) -> None:
        try:
            with self._cursor() as cursor:
                cursor.execute(
                    self._sql(_TENANT_DISABLE_TEMPLATE), (merchant_id, user_id)
                )
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError("tenant disable failed") from exc

    # --- merchants -----------------------------------------------------
    def create_merchant(
        self,
        merchant_id: str,
        display_name: str,
        api_key_sha256: str,
        max_tenants: int,
        max_deployments: int,
        *,
        may_act_as_subjects: bool = False,
        key_expires_at: datetime | None = None,
    ) -> None:
        """Register a merchant. Similar to the tenant token, the API key only stores the digest.

        Impersonation defaults to off: a caller that never asked for it must not be able to
        resolve X-Acting-Subject just because it holds a merchant key.
        """
        try:
            with self._cursor() as cursor:
                cursor.execute(
                    self._sql(_MERCHANT_INSERT_TEMPLATE),
                    (
                        merchant_id,
                        display_name,
                        api_key_sha256,
                        max_tenants,
                        max_deployments,
                        may_act_as_subjects,
                        key_expires_at,
                    ),
                )
        except StorageError:
            raise
        except Exception as exc:
            # merchant_id and api_key_sha256 are both unique constraints, and the driver only gives a sentence of "constraint conflict".
            # Can't tell which one it is - the copywriter states both possibilities, no guessing. Other abnormalities (cannot connect,
            # Timeout) no longer pretends to be a conflict, and uses a common failure text to make the API reflect 503.
            if _is_unique_violation(exc):
                raise StorageConflictError(
                    f"merchant {merchant_id!r} already exists, or its API "
                    "key digest is already registered"
                ) from exc
            raise StorageError("merchant creation failed") from exc

    def merchant(self, merchant_id: str) -> dict[str, Any] | None:
        """Look up a merchant, disabled ones included (the admin console must be able to see and restore)."""
        return self._merchant_query(_MERCHANT_BY_ID_TEMPLATE, (merchant_id,))

    def merchant_by_api_key(self, api_key_sha256: str) -> dict[str, Any] | None:
        """Resolve a merchant from an API key digest; disabled ones resolve to None."""
        return self._merchant_query(_MERCHANT_BY_KEY_TEMPLATE, (api_key_sha256,))

    def _merchant_query(
        self, template: str, values: tuple[Any, ...]
    ) -> dict[str, Any] | None:
        try:
            with self._cursor() as cursor:
                cursor.execute(self._sql(template), values)
                row = cursor.fetchone()
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError("merchant query failed") from exc
        if row is None:
            return None
        return dict(zip(_MERCHANT_COLUMNS, row, strict=True))

    def list_merchants(self) -> list[dict[str, Any]]:
        try:
            with self._cursor() as cursor:
                cursor.execute(self._sql(_MERCHANT_LIST_SQL))
                rows = cursor.fetchall()
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError("merchant list query failed") from exc
        return [dict(zip(_MERCHANT_COLUMNS, row, strict=True)) for row in rows]

    def merchant_resources(self, merchant_id: str) -> dict[str, str] | None:
        """The resource limit of this merchant; returns None if not allocated (the caller uses the default value)."""
        with self._cursor() as cursor:
            cursor.execute(
                self._sql(
                    "SELECT cpu_limit, memory_limit, pod_limit "
                    "FROM sites_merchant_resources WHERE merchant_id = {ph}"
                ),
                (merchant_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return {"cpu": str(row[0]), "memory": str(row[1]), "pods": str(row[2])}

    def set_merchant_resources(
        self, merchant_id: str, quota: dict[str, str]
    ) -> None:
        """Write the entire copy without updating part of it: the three values together constitute a resource package, which can be easily allocated if modified separately.
        "The CPU is mentioned but the memory is not mentioned" is a contradictory position. The caller is responsible for reading the present value first and then merging it.
        """
        with self._cursor() as cursor:
            cursor.execute(
                    self._sql(self._dialect.merchant_resources_upsert),
                (
                    merchant_id,
                    quota["cpu"],
                    quota["memory"],
                    quota["pods"],
                ),
            )

    def update_merchant(
        self,
        merchant_id: str,
        *,
        display_name: str | None = None,
        max_tenants: int | None = None,
        max_deployments: int | None = None,
        may_act_as_subjects: bool | None = None,
    ) -> None:
        """Change one merchant's mutable fields in a single statement.

        Quota and display name are columns on the same table, and the where conditions are also the same. There is no reason to separate them when changing them together.
        Two transactions - the column set is synthesized into **one** UPDATE during runtime, and a single statement is naturally atomic and does not exist
        "The name has been changed but the quota has not been changed" is the placement point for the partial update. Caller to change multiple items at the same time (Merchant PATCH handler)
        Should go here; update_merchant_quota / update_merchant_display_name reserved
        The original signature and delegation are here, and the behavior when changing a single item is exactly the same as before.

        merchant_id is not in any modifiable field: it is derived from the Namespace name and CR name, and can be renamed
        It is equivalent to replacing all Kubernetes objects under this merchant's name. display_name is a purely display field,
        Does not participate in any derivation; the content is externally controllable text and must be treated as plain text by the rendering party. There is no school here
        Verify that it is not empty - request verification belongs to the API layer.
        """
        updates: dict[str, Any] = {}
        if display_name is not None:
            updates["display_name"] = display_name
        if max_tenants is not None:
            updates["max_tenants"] = max_tenants
        if max_deployments is not None:
            updates["max_deployments"] = max_deployments
        if may_act_as_subjects is not None:
            updates["may_act_as_subjects"] = may_act_as_subjects
        self._update_row(
            "sites_merchants",
            updates,
            "merchant_id = {ph}",
            (merchant_id,),
            subject="merchant update",
        )

    def update_merchant_quota(
        self,
        merchant_id: str,
        *,
        max_tenants: int | None = None,
        max_deployments: int | None = None,
    ) -> None:
        """Change one merchant's quota. None means "this item does not change".

        If you want to change the display name at the same time, use update_merchant, which calls one transaction at a time.
        """
        self.update_merchant(
            merchant_id,
            max_tenants=max_tenants,
            max_deployments=max_deployments,
        )

    def update_merchant_display_name(
        self, merchant_id: str, display_name: str
    ) -> None:
        """Rename a merchant for display purposes only.

        If you want to change the quota at the same time, use update_merchant, which calls one transaction at a time - two independent
        The UPDATE used to leave a partial update saying "the name has been changed but the quota has not been changed" when it failed in the middle.
        """
        self.update_merchant(merchant_id, display_name=display_name)

    def rotate_merchant_key(
        self,
        merchant_id: str,
        api_key_sha256: str,
        expires_at: datetime | None = None,
    ) -> None:
        """Issue a new API key with its expiry and clear any disabled state.

        The same reason as rotate_tenant_token: someone comes to sign a new certificate for this merchant, indicating that it should
        is alive; without this path, deactivation is irreversible.
        """
        try:
            with self._cursor() as cursor:
                cursor.execute(
                    self._sql(_MERCHANT_ROTATE_TEMPLATE),
                    (api_key_sha256, expires_at, merchant_id),
                )
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError("merchant key rotation failed") from exc

    def disable_merchant(self, merchant_id: str) -> None:
        """Soft-disable a merchant. Only set disabled_at, the record and its tenants will not be deleted.

        To deactivate, both paths must be closed on the authentication side at the same time: the merchant key cannot be found (SQL has been filtered), and it
        The tenant's own token must also be rejected - that is done at the API layer, relying on tenant_by_token
        Check this table again for the returned merchant_id.
        """
        try:
            with self._cursor() as cursor:
                cursor.execute(
                    self._sql(_MERCHANT_DISABLE_TEMPLATE), (merchant_id,)
                )
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError("merchant disable failed") from exc

    # --- immutable site versions -------------------------------------
    def create_site_version(
        self,
        merchant_id: str,
        user_id: str,
        site_name: str,
        *,
        site_type: str,
        content_sha256: str,
        artifact_uri: str | None = None,
        image: str | None = None,
        database_schema: str | None = None,
        metadata: dict[str, Any] | None = None,
        migration_sha256: str | None = None,
        migration_sql: str | None = None,
    ) -> dict[str, Any]:
        """Append one immutable version and return its persisted record."""
        if site_type not in {"static", "dynamic"}:
            raise ValueError("site_type must be static or dynamic")
        if not content_sha256:
            raise ValueError("content_sha256 must not be empty")
        if site_type == "static" and not artifact_uri:
            raise ValueError("static site versions require artifact_uri")
        if site_type == "dynamic" and not database_schema:
            raise ValueError("dynamic site versions require database_schema")
        if (migration_sha256 is None) != (migration_sql is None):
            raise ValueError("migration_sha256 and migration_sql must be supplied together")
        try:
            with self._cursor() as cursor:
                cursor.execute(
                    "INSERT INTO sites_catalog "
                    "(merchant_id, user_id, site_name, site_type) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (merchant_id, user_id, site_name) DO NOTHING",
                    (merchant_id, user_id, site_name, site_type),
                )
                cursor.execute(
                    "SELECT site_type FROM sites_catalog "
                    "WHERE merchant_id = %s AND user_id = %s AND site_name = %s "
                    "FOR UPDATE",
                    (merchant_id, user_id, site_name),
                )
                existing = cursor.fetchone()
                if existing is None:
                    raise StorageError("site catalog row disappeared")
                if str(existing[0]) != site_type:
                    raise StorageConflictError(
                        f"site {site_name!r} already exists as {existing[0]}"
                    )
                cursor.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 FROM sites_versions "
                    "WHERE merchant_id = %s AND user_id = %s AND site_name = %s",
                    (merchant_id, user_id, site_name),
                )
                version = int(cursor.fetchone()[0])
                cursor.execute(
                    "INSERT INTO sites_versions "
                    "(merchant_id, user_id, site_name, version, content_sha256, "
                    "artifact_uri, image, database_schema, metadata) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb) "
                    "RETURNING merchant_id, user_id, site_name, version, "
                    "content_sha256, artifact_uri, image, database_schema, "
                    "metadata, created_at",
                    (
                        merchant_id,
                        user_id,
                        site_name,
                        version,
                        content_sha256,
                        artifact_uri,
                        image,
                        database_schema,
                        json.dumps(metadata or {}, separators=(",", ":")),
                    ),
                )
                row = cursor.fetchone()
                migration_status = "pending" if migration_sql is not None else "not-required"
                cursor.execute(
                    "INSERT INTO sites_version_migrations "
                    "(merchant_id, user_id, site_name, version, migration_sha256, "
                    "migration_sql, status) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (
                        merchant_id,
                        user_id,
                        site_name,
                        version,
                        migration_sha256,
                        migration_sql,
                        migration_status,
                    ),
                )
        except (StorageError, StorageConflictError):
            raise
        except Exception as exc:
            raise StorageError("site version creation failed") from exc
        record = self._site_version_record(row)
        record.update(migration_status=migration_status, migration_applied_at=None)
        return record

    def begin_site_migration(
        self, merchant_id: str, user_id: str, site_name: str, version: int
    ) -> dict[str, Any]:
        """Claim a pending immutable-version migration for execution."""
        try:
            with self._cursor() as cursor:
                cursor.execute(
                    "UPDATE sites_version_migrations SET status = 'running', "
                    "started_at = NOW(), applied_at = NULL, error = NULL WHERE "
                    "merchant_id = %s AND user_id = %s AND site_name = %s "
                    "AND version = %s AND status = 'pending' "
                    "RETURNING migration_sha256, migration_sql",
                    (merchant_id, user_id, site_name, version),
                )
                row = cursor.fetchone()
        except Exception as exc:
            raise StorageError("site migration claim failed") from exc
        if row is None:
            raise StorageConflictError("site migration is not pending")
        return {"migration_sha256": str(row[0]), "migration_sql": str(row[1])}

    def finish_site_migration(
        self,
        merchant_id: str,
        user_id: str,
        site_name: str,
        version: int,
        *,
        succeeded: bool,
    ) -> None:
        """Persist the terminal status without recording driver errors or secrets."""
        status = "succeeded" if succeeded else "failed"
        try:
            with self._cursor() as cursor:
                cursor.execute(
                    "UPDATE sites_version_migrations SET status = %s, "
                    "applied_at = CASE WHEN %s THEN NOW() ELSE NULL END, "
                    "error = CASE WHEN %s THEN NULL ELSE 'migration execution failed' END "
                    "WHERE merchant_id = %s AND user_id = %s AND site_name = %s "
                    "AND version = %s AND status = 'running'",
                    (
                        status,
                        succeeded,
                        succeeded,
                        merchant_id,
                        user_id,
                        site_name,
                        version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise StorageConflictError("site migration is not running")
        except (StorageError, StorageConflictError):
            raise
        except Exception as exc:
            raise StorageError("site migration status update failed") from exc

    def site_migration(
        self, merchant_id: str, user_id: str, site_name: str, version: int
    ) -> dict[str, Any] | None:
        """Return public-safe migration state; never return the SQL artifact."""
        try:
            with self._cursor() as cursor:
                cursor.execute(
                    "SELECT migration_sha256, status, started_at, applied_at "
                    "FROM sites_version_migrations WHERE merchant_id = %s "
                    "AND user_id = %s AND site_name = %s AND version = %s",
                    (merchant_id, user_id, site_name, version),
                )
                row = cursor.fetchone()
        except Exception as exc:
            raise StorageError("site migration query failed") from exc
        if row is None:
            return None
        return {
            "migration_sha256": row[0],
            "status": str(row[1]),
            "started_at": row[2],
            "applied_at": row[3],
        }

    def promote_site_version(
        self,
        merchant_id: str,
        user_id: str,
        site_name: str,
        version: int,
    ) -> dict[str, Any]:
        """Atomically move the live pointer to an existing immutable version."""
        try:
            with self._cursor() as cursor:
                cursor.execute(
                    "SELECT 1 FROM sites_versions AS version_row JOIN "
                    "sites_version_migrations AS migration USING "
                    "(merchant_id, user_id, site_name, version) WHERE "
                    "version_row.merchant_id = %s AND version_row.user_id = %s "
                    "AND version_row.site_name = %s AND version_row.version = %s "
                    "AND migration.status IN ('not-required', 'succeeded')",
                    (merchant_id, user_id, site_name, version),
                )
                if cursor.fetchone() is None:
                    raise StorageConflictError(
                        f"site version {site_name!r} v{version} does not exist"
                    )
                cursor.execute(
                    "UPDATE sites_catalog SET current_version = %s, "
                    "updated_at = NOW() WHERE merchant_id = %s AND user_id = %s "
                    "AND site_name = %s RETURNING merchant_id, user_id, "
                    "site_name, site_type, current_version, created_at, updated_at",
                    (version, merchant_id, user_id, site_name),
                )
                row = cursor.fetchone()
        except (StorageError, StorageConflictError):
            raise
        except Exception as exc:
            raise StorageError("site version promotion failed") from exc
        if row is None:
            raise StorageConflictError(f"site {site_name!r} does not exist")
        return self._site_record(row)

    def rollback_site(
        self,
        merchant_id: str,
        user_id: str,
        site_name: str,
        version: int,
    ) -> dict[str, Any]:
        """Rollback is promotion of an older immutable version."""
        return self.promote_site_version(
            merchant_id, user_id, site_name, version
        )

    def site(
        self, merchant_id: str, user_id: str, site_name: str
    ) -> dict[str, Any] | None:
        try:
            with self._cursor() as cursor:
                cursor.execute(
                    "SELECT merchant_id, user_id, site_name, site_type, "
                    "current_version, created_at, updated_at FROM sites_catalog "
                    "WHERE merchant_id = %s AND user_id = %s AND site_name = %s",
                    (merchant_id, user_id, site_name),
                )
                row = cursor.fetchone()
        except Exception as exc:
            raise StorageError("site query failed") from exc
        return None if row is None else self._site_record(row)

    def list_site_versions(
        self,
        merchant_id: str,
        user_id: str,
        site_name: str,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        try:
            with self._cursor() as cursor:
                cursor.execute(
                    "SELECT merchant_id, user_id, site_name, version, "
                    "content_sha256, artifact_uri, image, database_schema, "
                    "metadata, created_at FROM sites_versions WHERE "
                    "merchant_id = %s AND user_id = %s AND site_name = %s "
                    "ORDER BY version DESC LIMIT %s",
                    (merchant_id, user_id, site_name, limit),
                )
                rows = cursor.fetchall()
        except Exception as exc:
            raise StorageError("site version list failed") from exc
        return [self._site_version_record(row) for row in rows]

    def site_version(
        self,
        merchant_id: str,
        user_id: str,
        site_name: str,
        version: int,
    ) -> dict[str, Any] | None:
        try:
            with self._cursor() as cursor:
                cursor.execute(
                    "SELECT merchant_id, user_id, site_name, version, "
                    "content_sha256, artifact_uri, image, database_schema, "
                    "metadata, created_at FROM sites_versions WHERE "
                    "merchant_id = %s AND user_id = %s AND site_name = %s "
                    "AND version = %s",
                    (merchant_id, user_id, site_name, version),
                )
                row = cursor.fetchone()
        except Exception as exc:
            raise StorageError("site version query failed") from exc
        return None if row is None else self._site_version_record(row)

    def promote_verified_site_versions(self, items: list[dict[str, Any]]) -> int:
        """Promote only versions backed by a ready, verified deployment CR."""
        candidates: list[tuple[str, str, str, int, str, str]] = []
        for item in items:
            spec = item.get("spec") or {}
            status = item.get("status") or {}
            verification = status.get("verification") or {}
            if not (
                status.get("phase") == "Running"
                and status.get("ready") is True
                and verification.get("ok") is True
                and verification.get("revision") == str(spec.get("revision", "1"))
            ):
                continue
            try:
                version = int(spec["siteVersion"])
                candidates.append(
                    (
                        str(spec["merchantID"]),
                        str(spec["userID"]),
                        str(spec["serviceName"]),
                        version,
                        str(spec["image"]),
                        str((spec.get("staticArtifact") or {}).get("sha256") or ""),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        if not candidates:
            return 0
        promoted = 0
        try:
            with self._cursor() as cursor:
                for merchant_id, user_id, site_name, version, image, artifact_sha in candidates:
                    cursor.execute(
                        "UPDATE sites_catalog AS catalog SET current_version = %s, "
                        "updated_at = NOW() FROM sites_versions AS version_row, "
                        "sites_version_migrations AS migration WHERE "
                        "catalog.merchant_id = %s AND catalog.user_id = %s AND "
                        "catalog.site_name = %s AND "
                        "version_row.merchant_id = catalog.merchant_id AND "
                        "version_row.user_id = catalog.user_id AND "
                        "version_row.site_name = catalog.site_name AND "
                        "version_row.version = %s AND ((catalog.site_type = 'dynamic' "
                        "AND version_row.image = %s) OR (catalog.site_type = 'static' "
                        "AND version_row.artifact_uri IS NOT NULL AND "
                        "version_row.content_sha256 = %s)) AND "
                        "migration.merchant_id = version_row.merchant_id AND "
                        "migration.user_id = version_row.user_id AND "
                        "migration.site_name = version_row.site_name AND "
                        "migration.version = version_row.version AND "
                        "migration.status IN ('not-required', 'succeeded') AND "
                        "catalog.current_version IS DISTINCT FROM %s",
                        (
                            version,
                            merchant_id,
                            user_id,
                            site_name,
                            version,
                            image,
                            artifact_sha,
                            version,
                        ),
                    )
                    promoted += int(cursor.rowcount or 0)
        except Exception as exc:
            raise StorageError("verified site version promotion failed") from exc
        return promoted

    def failed_site_version_rollbacks(
        self, items: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Resolve last-known-good targets for verified rollout failures."""
        candidates: list[tuple[str, str, str, int, str, str, str]] = []
        for item in items:
            metadata = item.get("metadata") or {}
            spec = item.get("spec") or {}
            status = item.get("status") or {}
            verification = status.get("verification") or {}
            try:
                consecutive_failures = int(
                    verification.get("consecutiveFailures") or 0
                )
            except (TypeError, ValueError):
                consecutive_failures = 0
            if not (
                metadata.get("name")
                and status.get("phase") == "Running"
                and status.get("ready") is True
                and verification.get("ok") is False
                and verification.get("revision") == str(spec.get("revision", "1"))
                # A single readiness-edge probe can race the Service endpoint
                # update. The operator retries failed evidence after its bounded
                # backoff; only two consecutive server-side failures make a
                # forward rollout eligible for automatic recovery.
                and consecutive_failures >= VERIFICATION_FAILURES_BEFORE_ROLLBACK
            ):
                continue
            try:
                candidates.append(
                    (
                        str(spec["merchantID"]), str(spec["userID"]),
                        str(spec["serviceName"]), int(spec["siteVersion"]),
                        str(metadata["name"]),
                        str(spec.get("image") or ""),
                        str((spec.get("staticArtifact") or {}).get("sha256") or ""),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        targets: list[dict[str, Any]] = []
        try:
            with self._cursor() as cursor:
                for (
                    merchant_id, user_id, site_name, attempted, cr_name,
                    attempted_image, attempted_sha,
                ) in candidates:
                    cursor.execute(
                        "SELECT catalog.current_version, catalog.site_type, "
                        "current_row.image, current_row.artifact_uri, "
                        "current_row.content_sha256 FROM "
                        "sites_catalog AS catalog JOIN sites_versions AS current_row ON "
                        "current_row.merchant_id = catalog.merchant_id AND "
                        "current_row.user_id = catalog.user_id AND "
                        "current_row.site_name = catalog.site_name AND "
                        "current_row.version = catalog.current_version JOIN "
                        "sites_versions AS attempted_row ON "
                        "attempted_row.merchant_id = catalog.merchant_id AND "
                        "attempted_row.user_id = catalog.user_id AND "
                        "attempted_row.site_name = catalog.site_name AND "
                        "attempted_row.version = %s WHERE "
                        "catalog.merchant_id = %s AND catalog.user_id = %s AND "
                        "catalog.site_name = %s AND "
                        "catalog.current_version IS NOT NULL AND "
                        "catalog.current_version <> %s AND "
                        "((catalog.site_type = 'dynamic' AND attempted_row.image = %s "
                        "AND attempted_row.metadata ->> 'databaseStrategy' = 'shared' "
                        "AND attempted_row.metadata ->> 'databaseCompatibility' = "
                        "'backward-compatible') OR (catalog.site_type = 'static' AND "
                        "attempted_row.content_sha256 = %s AND "
                        "attempted_row.artifact_uri IS NOT NULL))",
                        (
                            attempted, merchant_id, user_id, site_name, attempted,
                            attempted_image, attempted_sha,
                        ),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        continue
                    site_type = str(row[1])
                    if site_type == "dynamic":
                        if not row[2]:
                            continue
                        targets.append(
                            {
                                "cr_name": cr_name,
                                "version": int(row[0]),
                                "image": str(row[2]),
                            }
                        )
                        continue
                    if site_type == "static" and row[3]:
                        targets.append(
                            {
                                "cr_name": cr_name,
                                "version": int(row[0]),
                                "site_type": "static",
                                "artifact_uri": str(row[3]),
                                "content_sha256": str(row[4]),
                            }
                        )
        except Exception as exc:
            raise StorageError("failed site version rollback lookup failed") from exc
        return targets

    @staticmethod
    def _site_record(row: tuple[Any, ...]) -> dict[str, Any]:
        return dict(
            zip(
                (
                    "merchant_id",
                    "user_id",
                    "site_name",
                    "site_type",
                    "current_version",
                    "created_at",
                    "updated_at",
                ),
                row,
                strict=True,
            )
        )

    @staticmethod
    def _site_version_record(row: tuple[Any, ...]) -> dict[str, Any]:
        return dict(
            zip(
                (
                    "merchant_id",
                    "user_id",
                    "site_name",
                    "version",
                    "content_sha256",
                    "artifact_uri",
                    "image",
                    "database_schema",
                    "metadata",
                    "created_at",
                ),
                row,
                strict=True,
            )
        )


def store_from_env() -> Store:
    """Create the PostgreSQL control-plane store from the environment."""
    backend = (getenv("SITES_DB_BACKEND", "") or "").strip().lower()
    if backend and backend not in {"postgres", "postgresql"}:
        raise StorageError(
            f"unsupported SITES_DB_BACKEND: {backend!r}; PostgreSQL is required"
        )
    # ThreadingHTTPServer creates a fresh request thread for each connection.
    # A thread-local connection cannot be reused there, while the store's
    # connection registry keeps it alive after the request thread exits. Close
    # each request connection deterministically at this API boundary instead.
    return Store.postgres(DatabaseConfig.from_env(), cache_connections=False)
