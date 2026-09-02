"""Versioned-site lifecycle APIs and bounded dynamic-site SQL queries."""
from __future__ import annotations

import re
from urllib.parse import urlparse

from sites.nl2sql import QueryRejected, ReadOnlyQueryExecutor
from sites.migrations import SiteMigrationExecutor, validate_migration_artifact
from sites.object_storage import ObjectStorageError
from sites.storage import StorageConflictError, StorageError
from sites.validation import (
    STATIC_IMAGE,
    ValidationError,
    normalize_site_name,
    valid_image_reference,
)
from sites.k8s_resources import DATABASE_ENV_KEYS
from sites.admission import ControlPlaneBusy, acquire_mutation_lock
from sites.version_policy import normalize_version_policy


_SHA256 = re.compile(r"^[a-f0-9]{64}$")


def _version_response(record: dict) -> dict:
    metadata = record.get("metadata") or {}
    return {
        "siteName": record["site_name"],
        "version": int(record["version"]),
        "contentSha256": record["content_sha256"],
        "artifactUri": record.get("artifact_uri"),
        "image": record.get("image"),
        "databaseSchema": record.get("database_schema"),
        "changeMode": metadata.get("changeMode"),
        "schemaChange": metadata.get("schemaChange"),
        "migrationStrategy": metadata.get("migrationStrategy"),
        "migrationSha256": metadata.get("migrationSha256"),
        "migrationStatus": record.get("migration_status", "not-required"),
        "migrationAppliedAt": (
            record["migration_applied_at"].isoformat()
            if record.get("migration_applied_at") is not None
            else None
        ),
        "databaseCompatibility": metadata.get("databaseCompatibility"),
        "decisionRationale": metadata.get("decisionRationale"),
        "staticArtifact": metadata.get("staticArtifact"),
        "metadata": metadata,
        "createdAt": record["created_at"].isoformat(),
    }


def _artifact_uri(value: object) -> str:
    uri = str(value or "").strip()
    parsed = urlparse(uri)
    if parsed.scheme not in {"s3", "oss"} or not parsed.netloc or not parsed.path:
        raise ValidationError("artifactUri must be an s3:// or oss:// object URI")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValidationError("artifactUri must not contain credentials, query, or fragment")
    return uri


class SitesMixin:
    def _static_artifact_service(self):
        service = getattr(self, "static_artifacts", None)
        if service is None:
            # Object storage is optional for dynamic/legacy deployments. Resolve
            # its configuration only when a versioned static upload needs it.
            from sites.static_artifacts import StaticArtifactService

            service = StaticArtifactService.from_env()
            type(self).static_artifacts = service
        return service

    def _bind_dynamic_site_version(self, desired: dict) -> None:
        spec = desired["spec"]
        if "siteVersion" not in spec:
            return
        version = self.store.site_version(
            spec["merchantID"],
            spec["userID"],
            spec["serviceName"],
            int(spec["siteVersion"]),
        )
        if version is None:
            raise StorageConflictError("site version does not exist")
        if version.get("artifact_uri"):
            if spec.get("image") != STATIC_IMAGE:
                raise StorageConflictError(
                    "deployment image must match the fixed static runtime image"
                )
            static_artifact = (version.get("metadata") or {}).get("staticArtifact")
            if not isinstance(static_artifact, dict):
                raise StorageConflictError(
                    "static site version has no deployable artifact binding"
                )
            source_path = str(static_artifact.get("sourcePath") or "").strip()
            sha256 = str(version.get("content_sha256") or "").strip()
            expected_source_path = (
                f"{spec['merchantID']}/{spec['userID']}/{spec['serviceName']}/{sha256}"
            )
            if (
                source_path != expected_source_path
                or not _SHA256.fullmatch(sha256)
            ):
                raise StorageConflictError("static site artifact binding is invalid")
            # Only content-addressed object coordinates cross into the CR. Object
            # storage credentials remain private to the control plane/runtime.
            spec["staticArtifact"] = {
                "sourcePath": source_path,
                "sha256": sha256,
                **{
                    key: static_artifact[key]
                    for key in ("sizeBytes", "fileCount")
                    if key in static_artifact
                },
            }
            return
        if not version.get("database_schema"):
            raise StorageConflictError("dynamic site version does not exist")
        migration = self.store.site_migration(
            spec["merchantID"],
            spec["userID"],
            spec["serviceName"],
            int(spec["siteVersion"]),
        )
        if migration is None or migration["status"] not in {
            "not-required",
            "succeeded",
        }:
            raise StorageConflictError(
                "dynamic site version migration has not completed successfully"
            )
        if version.get("image") != spec["image"]:
            raise StorageConflictError(
                "deployment image must match the immutable dynamic site version"
            )
        database = self.site_databases.deployment_binding(
            spec["merchantID"], spec["userID"], spec["serviceName"]
        )
        if database["schema"] != version["database_schema"]:
            raise StorageConflictError("dynamic site database binding does not match version")
        declared = {str(item.get("name") or "") for item in spec.get("env") or []}
        reserved = declared.intersection(DATABASE_ENV_KEYS)
        if reserved:
            raise ValidationError(
                "versioned dynamic sites reserve PostgreSQL environment variables: "
                + ", ".join(sorted(reserved))
            )
        spec["database"] = {
            **database,
            "secretName": database["controlSecretName"],
        }
    def _list_site_versions(self, raw_name: str) -> None:
        identity = self._authenticate()
        if identity is None:
            return
        merchant_id, user_id = identity
        try:
            site_name = normalize_site_name(raw_name)
            site = self.store.site(merchant_id, user_id, site_name)
            if site is None:
                self._json(404, {"error": "site not found", "code": "site_not_found"})
                return
            versions = self.store.list_site_versions(
                merchant_id, user_id, site_name, limit=100
            )
            for version_record in versions:
                migration = self.store.site_migration(
                    merchant_id, user_id, site_name, int(version_record["version"])
                )
                if migration is not None:
                    version_record["migration_status"] = migration["status"]
                    version_record["migration_applied_at"] = migration["applied_at"]
            deployment = self.store.get_deployment(
                merchant_id, user_id, site_name
            )
        except ValidationError as exc:
            self._json(400, {"error": str(exc), "code": "sites_invalid_input"})
            return
        except StorageError:
            self._json(503, {"error": "database unavailable"})
            return
        self._json(
            200,
            {
                "siteName": site_name,
                "siteType": site["site_type"],
                "currentVersion": site["current_version"],
                "deployedVersion": (
                    ((deployment or {}).get("spec") or {}).get("siteVersion")
                ),
                "deploymentPhase": (deployment or {}).get("phase"),
                "versions": [_version_response(record) for record in versions],
                "count": len(versions),
            },
        )

    def _create_site_version(self, raw_name: str) -> None:
        identity = self._authenticate()
        if identity is None:
            return
        merchant_id, user_id = identity
        try:
            site_name = normalize_site_name(raw_name)
            body = self._read_body()
            site_type = str(body.get("siteType") or "").strip()
            content_sha256 = str(body.get("contentSha256") or "").strip().lower()
            if site_type not in {"static", "dynamic"}:
                raise ValidationError("siteType must be static or dynamic")
            metadata = body.get("metadata") or {}
            if not isinstance(metadata, dict):
                raise ValidationError("metadata must be a JSON object")
            if "staticArtifact" in metadata:
                raise ValidationError("metadata.staticArtifact is reserved")
            version_policy = normalize_version_policy(body)
            metadata = {
                **metadata,
                **version_policy,
            }
            artifact_uri = None
            image = None
            database_schema = None
            migration_artifact = None
            if site_type == "static":
                if version_policy["schemaChange"] != "none":
                    raise ValidationError(
                        "static site versions must declare schemaChange=none"
                    )
                if body.get("migrationSql") is not None:
                    raise ValidationError("static site versions cannot contain migrationSql")
                files = body.get("files")
                if files is not None:
                    published = self._static_artifact_service().create_version_artifact(
                        merchant_id,
                        user_id,
                        site_name,
                        files,
                        declared_sha256=content_sha256 or None,
                    )
                    content_sha256 = str(published.get("contentSha256") or "")
                    artifact_uri = _artifact_uri(published.get("artifactUri"))
                    source_path = str(published.get("sourcePath") or "").strip()
                    expected_source_path = (
                        f"{merchant_id}/{user_id}/{site_name}/{content_sha256}"
                    )
                    if source_path != expected_source_path:
                        raise ValidationError(
                            "static artifact service returned an invalid sourcePath"
                        )
                    metadata["staticArtifact"] = {
                        "sourcePath": source_path,
                        **{
                            key: published[key]
                            for key in ("sizeBytes", "fileCount")
                            if key in published
                        },
                    }
                else:
                    artifact_uri = _artifact_uri(body.get("artifactUri"))
            else:
                if body.get("files") is not None:
                    raise ValidationError("dynamic site versions cannot contain files")
                image = str(body.get("image") or "").strip()
                if (
                    not valid_image_reference(image)
                    or not re.search(r"@sha256:[a-f0-9]{64}$", image)
                ):
                    raise ValidationError("dynamic site image must be pinned by sha256 digest")
                binding = self.site_databases.provision(
                    merchant_id, user_id, site_name
                )
                database_schema = binding.schema
                if version_policy["schemaChange"] == "none":
                    if body.get("migrationSql") is not None:
                        raise ValidationError(
                            "schemaChange=none cannot contain migrationSql"
                        )
                else:
                    migration_artifact = validate_migration_artifact(
                        body.get("migrationSql"),
                        str(version_policy["migrationSha256"]),
                        database_schema,
                    )
            if not _SHA256.fullmatch(content_sha256):
                raise ValidationError(
                    "contentSha256 must be 64 lowercase hexadecimal characters"
                )
            record = self.store.create_site_version(
                merchant_id,
                user_id,
                site_name,
                site_type=site_type,
                content_sha256=content_sha256,
                artifact_uri=artifact_uri,
                image=image,
                database_schema=database_schema,
                metadata=metadata,
                migration_sha256=(
                    migration_artifact.sha256 if migration_artifact else None
                ),
                migration_sql=(
                    migration_artifact.content if migration_artifact else None
                ),
            )
            if migration_artifact is not None:
                claimed = self.store.begin_site_migration(
                    merchant_id, user_id, site_name, int(record["version"])
                )
                try:
                    # Re-validate persisted bytes and digest before execution.
                    executable = validate_migration_artifact(
                        claimed["migration_sql"],
                        claimed["migration_sha256"],
                        database_schema,
                    )
                    runtime_config = self.site_databases.runtime_config(
                        merchant_id, user_id, site_name
                    )
                    SiteMigrationExecutor(
                        runtime_config,
                        statement_timeout_ms=runtime_config.statement_timeout * 1000,
                    ).execute(executable, database_schema)
                    # Tables are owned by the runtime role. Reader grants are part
                    # of migration success so automatic sync promotion cannot expose
                    # a version that NL2SQL is unable to inspect.
                    self.site_databases.refresh_reader(
                        merchant_id, user_id, site_name
                    )
                except (StorageError, ValidationError):
                    self.store.finish_site_migration(
                        merchant_id,
                        user_id,
                        site_name,
                        int(record["version"]),
                        succeeded=False,
                    )
                    self._json(
                        409,
                        {
                            "error": "site database migration failed",
                            "code": "site_migration_failed",
                            "siteName": site_name,
                            "version": int(record["version"]),
                            "migrationStatus": "failed",
                        },
                    )
                    return
                self.store.finish_site_migration(
                    merchant_id,
                    user_id,
                    site_name,
                    int(record["version"]),
                    succeeded=True,
                )
                record["migration_status"] = "succeeded"
                migration = self.store.site_migration(
                    merchant_id, user_id, site_name, int(record["version"])
                )
                record["migration_applied_at"] = (
                    migration or {}
                ).get("applied_at")
        except (ValidationError, ValueError) as exc:
            self._json(400, {"error": str(exc), "code": "sites_invalid_input"})
            return
        except StorageConflictError as exc:
            self._json(409, {"error": str(exc), "code": "site_version_conflict"})
            return
        except ObjectStorageError:
            self._json(
                503,
                {
                    "error": "static artifact storage unavailable",
                    "code": "static_artifact_unavailable",
                },
            )
            return
        except StorageError:
            self._json(503, {"error": "site version creation failed"})
            return
        self._json(201, _version_response(record))

    def _promote_site_version(self, raw_name: str) -> None:
        identity = self._authenticate()
        if identity is None:
            return
        merchant_id, user_id = identity
        try:
            site_name = normalize_site_name(raw_name)
            body = self._read_body()
            version = int(body.get("version"))
            if version < 1:
                raise ValidationError("version must be a positive integer")
            with acquire_mutation_lock(
                self.mutation_lock, self.mutation_lock_timeout
            ):
                site = self.store.site(merchant_id, user_id, site_name)
                if site is None:
                    self._json(404, {"error": "site not found", "code": "site_not_found"})
                    return
                target = self.store.site_version(
                    merchant_id, user_id, site_name, version
                )
                if target is None:
                    self._json(
                        409,
                        {
                            "error": f"site version {site_name!r} v{version} does not exist",
                            "code": "site_version_conflict",
                        },
                    )
                    return
                migration = self.store.site_migration(
                    merchant_id, user_id, site_name, version
                )
                if migration is None or migration["status"] not in {
                    "not-required",
                    "succeeded",
                }:
                    raise StorageConflictError(
                        "site version migration has not completed successfully"
                    )
                # Runtime migrations create tables as the per-site owner. Refresh the
                # reader grants before exposing this version to NL2SQL callers.
                if site["site_type"] == "dynamic":
                    self.site_databases.refresh_reader(merchant_id, user_id, site_name)
                promoted = self.store.promote_site_version(
                    merchant_id, user_id, site_name, version
                )
        except ControlPlaneBusy:
            self._json(
                503,
                {"error": "control plane busy, retry later", "code": "control_plane_busy"},
            )
            return
        except (ValidationError, ValueError, TypeError) as exc:
            self._json(400, {"error": str(exc), "code": "sites_invalid_input"})
            return
        except StorageConflictError as exc:
            self._json(409, {"error": str(exc), "code": "site_version_conflict"})
            return
        except StorageError:
            self._json(503, {"error": "site version promotion failed"})
            return
        self._json(
            200,
            {
                "siteName": site_name,
                "siteType": promoted["site_type"],
                "currentVersion": promoted["current_version"],
            },
        )

    def _query_dynamic_site(self, raw_name: str) -> None:
        identity = self._authenticate()
        if identity is None:
            return
        merchant_id, user_id = identity
        try:
            site_name = normalize_site_name(raw_name)
            body = self._read_body()
            query = str(body.get("query") or "")
            row_limit = int(body.get("rowLimit", 100))
            timeout_seconds = int(body.get("timeoutSeconds", 5))
        except (ValidationError, ValueError, TypeError) as exc:
            self._json(400, {"error": str(exc), "code": "sites_invalid_input"})
            return
        try:
            site = self.store.site(merchant_id, user_id, site_name)
        except StorageError:
            self._json(503, {"error": "database unavailable"})
            return
        if site is None:
            self._json(404, {"error": "site not found", "code": "site_not_found"})
            return
        if site["site_type"] != "dynamic":
            self._json(
                409,
                {
                    "error": "SQL queries require a dynamic site",
                    "code": "site_not_dynamic",
                },
            )
            return
        if site["current_version"] is None:
            self._json(
                409,
                {
                    "error": "site has no promoted version",
                    "code": "site_not_promoted",
                },
            )
            return
        try:
            config = self.site_databases.reader_config(
                merchant_id, user_id, site_name
            )
            result = ReadOnlyQueryExecutor(config).execute(
                query,
                row_limit=row_limit,
                timeout_seconds=timeout_seconds,
            )
        except QueryRejected as exc:
            self._json(400, {"error": str(exc), "code": "sql_query_rejected"})
            return
        except StorageError:
            self._json(
                503,
                {
                    "error": "site database query failed",
                    "code": "site_database_unavailable",
                },
            )
            return
        self._json(
            200,
            {
                "siteName": site_name,
                "version": int(site["current_version"]),
                "columns": list(result.columns),
                "rows": [list(row) for row in result.rows],
                "rowCount": result.row_count,
                "truncated": result.truncated,
            },
        )
