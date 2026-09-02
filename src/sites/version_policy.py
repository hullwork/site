"""Pure validation for immutable site-version database compatibility metadata."""
from __future__ import annotations

import re
from typing import Any

from sites.validation import ValidationError


_SHA256 = re.compile(r"^[a-f0-9]{64}$")


def normalize_version_policy(body: dict[str, Any]) -> dict[str, Any]:
    """Return the normalized shared-schema migration policy for one version.

    This remains free of storage and Kubernetes I/O so the API, scaffold
    evaluator, and tests exercise the same decision boundary.
    """
    change_mode = str(body.get("changeMode") or "incremental")
    database_strategy = str(body.get("databaseStrategy") or "shared")
    database_compatibility = str(
        body.get("databaseCompatibility") or "backward-compatible"
    )
    schema_change = str(body.get("schemaChange") or "none")
    migration_strategy = str(body.get("migrationStrategy") or "none")
    migration_sha256 = str(body.get("migrationSha256") or "")

    if change_mode not in {"incremental", "rebuild-compatible"}:
        raise ValidationError(
            "rebuild-breaking requires a fresh or cloned database schema, "
            "which is not supported by this deployment path"
        )
    if database_strategy != "shared":
        raise ValidationError(
            "databaseStrategy must be shared until fresh/clone provisioning is available"
        )
    if database_compatibility != "backward-compatible":
        raise ValidationError(
            "shared database versions must declare backward-compatible migrations"
        )
    if schema_change not in {"none", "additive", "compatible", "destructive"}:
        raise ValidationError(
            "schemaChange must be none, additive, compatible, or destructive"
        )
    if schema_change == "destructive":
        raise ValidationError(
            "destructive schema changes require a staged expand-contract "
            "migration or an explicitly authorized manual cutover"
        )
    expected_strategy = "none" if schema_change == "none" else "expand-contract"
    if migration_strategy != expected_strategy:
        raise ValidationError(
            f"schemaChange={schema_change} requires migrationStrategy={expected_strategy}"
        )
    if schema_change != "none" and not _SHA256.fullmatch(migration_sha256):
        raise ValidationError(
            "migrationSha256 is required for additive or compatible schema changes"
        )
    return {
        "changeMode": change_mode,
        "databaseStrategy": database_strategy,
        "databaseCompatibility": database_compatibility,
        "schemaChange": schema_change,
        "migrationStrategy": migration_strategy,
        "migrationSha256": migration_sha256 or None,
        "decisionRationale": str(body.get("decisionRationale") or "")[:1000],
    }
