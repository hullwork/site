"""Active compiled config and environment settings per app environment.

Also the home of the admin plan/apply flow (contract section 3.5): computing the
plan digest, detecting a stale plan, and re-validating the compiled config before
it becomes active.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from dataclasses import dataclass
from os import getenv
from typing import Any

from sites.broker import db as broker_db
from sites.broker import secretbox

_REQUIRED_CONFIG_KEYS = (
    "app", "auth", "roles", "default_role", "plans", "default_plan",
    "entities", "permissions", "agents", "flows",
)
_API_VERSION = "hull/v1alpha1"


@dataclass(frozen=True)
class AppRuntime:
    app_env_id: str
    version: int
    digest: str
    config: dict
    settings: dict


class ConfigValidationError(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("compiled config failed validation")
        self.errors = errors


class ConfigStaleError(Exception):
    """The plan digest supplied to apply() no longer matches the current base."""


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_plan_digest(config: dict, base_version: int | None) -> str:
    payload = canonical_json({"config": config, "base": base_version})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _structural_errors(config: Any) -> list[str]:
    """Fallback validation used only when ``sites.manifest`` is not importable
    (partition E's module has not landed yet, or this process is running the
    C test suite in isolation). It checks the shape every compiled config must
    have, not the deep semantic rules (risk/credential/run_as vocabularies, tool
    reference resolution, budgets) that only the real validator enforces.
    """
    if not isinstance(config, dict):
        return ["config must be a JSON object"]
    errors = []
    if config.get("apiVersion") != _API_VERSION:
        errors.append(f'apiVersion must be "{_API_VERSION}"')
    for key in _REQUIRED_CONFIG_KEYS:
        if key not in config:
            errors.append(f"config is missing required key {key!r}")
    return errors


def _validate_config(config: Any) -> list[str]:
    try:
        # `import sites.manifest as manifest`, not `from sites import manifest`:
        # the latter raises a bare ImportError (not ModuleNotFoundError) once
        # `sites` itself has already imported successfully, which would slip
        # past the except clause below and fail every test in this branch
        # before partition E's module exists.
        import sites.manifest as manifest
    except ModuleNotFoundError as exc:
        if exc.name != "sites.manifest":
            raise
        return _structural_errors(config)
    validate = getattr(manifest, "validate", None)
    if validate is None:
        return _structural_errors(config)
    # The manifest validator judges what an author wrote; defaults such as
    # default_role are filled in by compile(), so it does not require them. The
    # Broker stores compiled configs, so the compiled shape is checked as well.
    errors = list(validate(config) or [])
    errors.extend(e for e in _structural_errors(config) if e not in errors)
    return errors


def _diff(old: Any, new: Any, prefix: str = "") -> list[dict]:
    changes: list[dict] = []
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old) | set(new)):
            path = f"{prefix}.{key}" if prefix else str(key)
            if key not in old:
                changes.append({"path": path, "op": "add"})
            elif key not in new:
                changes.append({"path": path, "op": "remove"})
            elif old[key] != new[key]:
                if isinstance(old[key], dict) and isinstance(new[key], dict):
                    changes.extend(_diff(old[key], new[key], path))
                else:
                    changes.append({"path": path, "op": "change"})
    elif old != new:
        changes.append({"path": prefix or "$", "op": "change"})
    return changes


def cache_seconds_from_env() -> int:
    # No prior SITES_* knob covers "how long a runtime process may keep serving a
    # config an admin already replaced" -- this is new surface area, not a mirror
    # of an existing default. 5s follows HULL_TOKEN_LEEWAY_SECONDS' precedent
    # elsewhere in this same contract for "a short, deliberately-chosen freshness
    # window": short enough that an admin who just applied a fix does not wait
    # long for every gateway replica to pick it up, long enough that a busy
    # endpoint (every authenticated request reads the active runtime) is not
    # re-querying PostgreSQL on every call.
    return int(getenv("HULL_CONFIG_CACHE_SECONDS", "5") or "5")


class ConfigStore:
    def __init__(self, db: broker_db.BrokerDatabase, secret_key: bytes, *,
                 cache_seconds: int | None = None, clock: Any = time.monotonic):
        self._db = db
        self._secret_key = secret_key
        self._cache_seconds = (
            cache_seconds_from_env() if cache_seconds is None else cache_seconds
        )
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, AppRuntime | None]] = {}

    # -- read path, used by every authenticated request -------------------------

    def active(self, app_env_id: str) -> AppRuntime | None:
        now = self._clock()
        with self._lock:
            cached = self._cache.get(app_env_id)
            if cached is not None and now - cached[0] < self._cache_seconds:
                return cached[1]
        runtime = self._load(app_env_id)
        with self._lock:
            self._cache[app_env_id] = (now, runtime)
        return runtime

    def _load(self, app_env_id: str) -> AppRuntime | None:
        with self._db.transaction() as cursor:
            version = broker_db.get_active_version(cursor, app_env_id)
            if version is None:
                return None
            row = broker_db.get_config_version(cursor, app_env_id, version)
            if row is None:
                return None
            settings = broker_db.get_settings(cursor, app_env_id)
        return AppRuntime(app_env_id=app_env_id, version=row["version"],
                           digest=row["digest"], config=row["config"], settings=settings)

    def invalidate(self, app_env_id: str) -> None:
        with self._lock:
            self._cache.pop(app_env_id, None)

    def by_host(self, host: str) -> str | None:
        with self._db.transaction() as cursor:
            return broker_db.app_env_id_for_host(cursor, host)

    def secret(self, app_env_id: str, name: str) -> str | None:
        with self._db.transaction() as cursor:
            blob = broker_db.get_secret(cursor, app_env_id, name)
        if blob is None:
            return None
        return secretbox.decrypt(self._secret_key, app_env_id, name, blob)

    # -- admin write path (contract section 3.5) ---------------------------------

    def plan(self, app_env_id: str, config: dict) -> dict:
        errors = _validate_config(config)
        if errors:
            raise ConfigValidationError(errors)
        with self._db.transaction() as cursor:
            broker_db.ensure_app(cursor, app_env_id)
            active_version = broker_db.get_active_version(cursor, app_env_id)
            active_config: dict = {}
            if active_version is not None:
                row = broker_db.get_config_version(cursor, app_env_id, active_version)
                if row is not None:
                    active_config = row["config"]
        digest = compute_plan_digest(config, active_version)
        changes = _diff(active_config, config)
        return {"digest": digest, "activeVersion": active_version, "changes": changes}

    def apply(self, app_env_id: str, config: dict, plan_digest: str) -> dict:
        errors = _validate_config(config)
        if errors:
            raise ConfigValidationError(errors)
        with self._db.transaction() as cursor:
            broker_db.ensure_app(cursor, app_env_id)
            active_version = broker_db.get_active_version_for_update(cursor, app_env_id)
            expected = compute_plan_digest(config, active_version)
            if not hmac.compare_digest(expected, plan_digest):
                raise ConfigStaleError()
            next_version = (active_version or 0) + 1
            digest = hashlib.sha256(canonical_json(config).encode("utf-8")).hexdigest()
            broker_db.insert_config_version(cursor, app_env_id, next_version, digest, config)
            broker_db.set_active_version(cursor, app_env_id, next_version)
        self.invalidate(app_env_id)
        return {"version": next_version, "digest": digest}

    def get_version(self, app_env_id: str) -> dict | None:
        """Backs ``GET .../config``: the active version, or None for a 404."""
        with self._db.transaction() as cursor:
            version = broker_db.get_active_version(cursor, app_env_id)
            if version is None:
                return None
            return broker_db.get_config_version(cursor, app_env_id, version)

    def get_settings(self, app_env_id: str) -> dict:
        with self._db.transaction() as cursor:
            return broker_db.get_settings(cursor, app_env_id)

    def put_settings(self, app_env_id: str, patch: dict) -> dict:
        """Merge-replace at the top level: keys in ``patch`` overwrite whatever was
        there, keys absent from ``patch`` are left as they were.
        """
        with self._db.transaction() as cursor:
            current = broker_db.get_settings(cursor, app_env_id)
            merged = {**current, **patch}
            broker_db.put_settings(cursor, app_env_id, merged)
        self.invalidate(app_env_id)
        return merged

    def put_secret(self, app_env_id: str, name: str, value: str) -> None:
        ciphertext = secretbox.encrypt(self._secret_key, app_env_id, name, value)
        with self._db.transaction() as cursor:
            broker_db.put_secret(cursor, app_env_id, name, ciphertext)

    def set_user_plan(self, app_env_id: str, user_id: str, plan: str) -> bool:
        with self._db.transaction() as cursor:
            return broker_db.set_user_plan(cursor, app_env_id, user_id, plan)

    def list_mail(self, app_env_id: str, limit: int = 100) -> list[dict]:
        with self._db.transaction() as cursor:
            return broker_db.list_outbound_mail(cursor, app_env_id, limit)
