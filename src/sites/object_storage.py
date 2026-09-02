"""S3-compatible object storage support for content-addressed build sources.

The control plane keeps Kubernetes as the deployment source of truth.  OSS is
only an external source-volume backend accessed through its S3-compatible API:
the API writes one immutable JSON object per source digest, and a builder init
container materializes that object into an emptyDir before BuildKit starts.
Credentials are always read from files so they never enter SiteBuild specs,
environment output, or command arguments.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from os import getenv
from sites import tracing

SOURCE_OBJECT_VERSION = 1
SOURCE_OBJECT_FILE = "source.json"
SOURCE_OBJECT_MAX_BYTES = 1024 * 1024
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
_SOURCE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,119}$")
_DENIED_PARTS = {
    ".env",
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "__pycache__",
    "node_modules",
    "vendor",
}


class ObjectStorageError(RuntimeError):
    """OSS is unavailable or returned an object that violates our contract."""


@dataclass(frozen=True)
class S3CompatibleConfig:
    endpoint: str
    bucket: str
    prefix: str
    region: str
    addressing_style: str
    signature_version: str
    access_key_id_file: Path
    access_key_secret_file: Path

    @classmethod
    def from_env(cls) -> "S3CompatibleConfig":
        endpoint = (getenv("SITES_OSS_ENDPOINT", "") or "").strip().rstrip("/")
        bucket = (getenv("SITES_OSS_BUCKET", "") or "").strip()
        raw_prefix = (getenv("SITES_OSS_PREFIX", "") or "").strip().strip("/")
        region = (getenv("SITES_OSS_REGION", "") or "").strip()
        addressing_style = (
            getenv("SITES_OSS_ADDRESSING_STYLE", "virtual") or "virtual"
        ).strip().lower()
        signature_version = (
            getenv("SITES_OSS_SIGNATURE_VERSION", "s3") or "s3"
        ).strip().lower()
        parsed_endpoint = urlsplit(endpoint)
        if parsed_endpoint.scheme not in {"https", "http"}:
            raise ObjectStorageError(
                "SITES_OSS_ENDPOINT must start with https:// or http://"
            )
        if (
            not parsed_endpoint.hostname
            or parsed_endpoint.username
            or parsed_endpoint.password
            or parsed_endpoint.query
            or parsed_endpoint.fragment
        ):
            raise ObjectStorageError("SITES_OSS_ENDPOINT must be a plain endpoint URL")
        if parsed_endpoint.scheme != "https":
            raise ObjectStorageError(
                "SITES_OSS_ENDPOINT must use https:// so source data and "
                "object-storage credentials are not sent as plaintext"
            )
        if not _BUCKET_RE.fullmatch(bucket):
            raise ObjectStorageError(
                "SITES_OSS_BUCKET is not a valid S3-compatible bucket name"
            )
        prefix = PurePosixPath(raw_prefix)
        if raw_prefix and (
            prefix.is_absolute()
            or any(part in {"", ".", ".."} for part in prefix.parts)
        ):
            raise ObjectStorageError(
                "SITES_OSS_PREFIX must be a safe relative prefix"
            )
        if not region:
            raise ObjectStorageError("SITES_OSS_REGION is required")
        if addressing_style not in {"virtual", "path"}:
            raise ObjectStorageError(
                "SITES_OSS_ADDRESSING_STYLE must be virtual or path"
            )
        if signature_version not in {"s3", "s3v4"}:
            raise ObjectStorageError(
                "SITES_OSS_SIGNATURE_VERSION must be s3 or s3v4"
            )
        return cls(
            endpoint=endpoint,
            bucket=bucket,
            prefix=raw_prefix,
            region=region,
            addressing_style=addressing_style,
            signature_version=signature_version,
            access_key_id_file=Path(
                getenv(
                    "SITES_OSS_ACCESS_KEY_ID_FILE",
                    "/var/run/sites-oss/access-key-id",
                )
            ),
            access_key_secret_file=Path(
                getenv(
                    "SITES_OSS_ACCESS_KEY_SECRET_FILE",
                    "/var/run/sites-oss/access-key-secret",
                )
            ),
        )

    def client(self) -> Any:
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:
            raise ObjectStorageError(
                "the S3-compatible object storage driver is unavailable"
            ) from exc
        try:
            access_key_id = self.access_key_id_file.read_text(encoding="utf-8").strip()
            access_key_secret = (
                self.access_key_secret_file.read_text(encoding="utf-8").strip()
            )
        except OSError as exc:
            raise ObjectStorageError(
                "object storage credential files are unavailable"
            ) from exc
        if not access_key_id or not access_key_secret:
            raise ObjectStorageError("object storage credentials must not be empty")
        try:
            return boto3.client(
                "s3",
                endpoint_url=self.endpoint,
                region_name=self.region,
                aws_access_key_id=access_key_id,
                aws_secret_access_key=access_key_secret,
                config=Config(
                    signature_version=self.signature_version,
                    s3={"addressing_style": self.addressing_style},
                    retries={"max_attempts": 5, "mode": "standard"},
                ),
            )
        except Exception as exc:
            raise ObjectStorageError(
                "the S3-compatible object storage client could not be created"
            ) from exc


def validate_source_path(source_path: str) -> str:
    path = PurePosixPath(source_path)
    if (
        path.is_absolute()
        or len(path.parts) != 4
        or any(not _SOURCE_SEGMENT_RE.fullmatch(part) for part in path.parts)
    ):
        raise ObjectStorageError("invalid OSS source path")
    return path.as_posix()


def source_object_key(source_path: str) -> str:
    path = validate_source_path(source_path)
    return f"{path}/{SOURCE_OBJECT_FILE}"


def _configured_key(config: S3CompatibleConfig, source_path: str) -> str:
    key = source_object_key(source_path)
    return f"{config.prefix}/{key}" if config.prefix else key


def source_payload(files: dict[str, str], sha256: str) -> dict[str, Any]:
    return {"version": SOURCE_OBJECT_VERSION, "sha256": sha256, "files": files}


def _source_digest(files: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for name, content in sorted(files.items()):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _validate_file_name(name: object) -> str:
    if not isinstance(name, str) or not 1 <= len(name) <= 240:
        raise ObjectStorageError("OSS source file path must be 1-240 characters")
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts:
        raise ObjectStorageError("OSS source file path must be relative")
    for part in path.parts:
        if (
            part in {"", ".", ".."}
            or not _SOURCE_SEGMENT_RE.fullmatch(part)
            or part.lower() in _DENIED_PARTS
            or part.lower().startswith(".env.")
        ):
            raise ObjectStorageError("OSS source file path is not allowed")
    return path.as_posix()


def validate_source_payload(payload: object, source_path: str) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise ObjectStorageError("OSS source object must be a JSON object")
    if payload.get("version") != SOURCE_OBJECT_VERSION:
        raise ObjectStorageError("unsupported OSS source object version")
    expected_digest = payload.get("sha256")
    raw_files = payload.get("files")
    if not isinstance(expected_digest, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_digest):
        raise ObjectStorageError("OSS source object has an invalid digest")
    if PurePosixPath(validate_source_path(source_path)).parts[-1] != expected_digest:
        raise ObjectStorageError("OSS source path does not match its digest")
    if not isinstance(raw_files, dict) or not raw_files:
        raise ObjectStorageError("OSS source object has no files")
    files: dict[str, str] = {}
    for raw_name, content in raw_files.items():
        name = _validate_file_name(raw_name)
        if not isinstance(content, str):
            raise ObjectStorageError("OSS source file must be UTF-8 text")
        files[name] = content
    if _source_digest(files) != expected_digest:
        raise ObjectStorageError("OSS source object digest mismatch")
    return files


class S3CompatibleSourceStore:
    def __init__(
        self,
        config: S3CompatibleConfig | None = None,
        client: Any | None = None,
    ):
        self.config = config or S3CompatibleConfig.from_env()
        self._object_client = client

    def _object_client_instance(self) -> Any:
        if self._object_client is None:
            self._object_client = self.config.client()
        return self._object_client

    def put(self, source_path: str, payload: dict[str, Any]) -> None:
        with tracing.span("sites.storage.object.put", kind=3):
            return self._put(source_path, payload)

    def _put(self, source_path: str, payload: dict[str, Any]) -> None:
        validate_source_payload(payload, source_path)
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        key = _configured_key(self.config, source_path)
        try:
            self._object_client_instance().put_object(
                Bucket=self.config.bucket,
                Key=key,
                Body=body,
                ContentType="application/json",
            )
        except ObjectStorageError:
            raise
        except Exception as exc:
            raise ObjectStorageError("object storage source upload failed") from exc

    def get(self, source_path: str) -> dict[str, Any]:
        with tracing.span("sites.storage.object.get", kind=3):
            return self._get(source_path)

    def _get(self, source_path: str) -> dict[str, Any]:
        key = _configured_key(self.config, source_path)
        try:
            response = self._object_client_instance().get_object(
                Bucket=self.config.bucket,
                Key=key,
            )
            content_length = int(response.get("ContentLength", 0) or 0)
            if content_length > SOURCE_OBJECT_MAX_BYTES:
                raise ObjectStorageError("OSS source object is too large")
            body = response["Body"].read(SOURCE_OBJECT_MAX_BYTES + 1)
            payload = json.loads(body.decode("utf-8"))
            if len(body) > SOURCE_OBJECT_MAX_BYTES:
                raise ObjectStorageError("OSS source object is too large")
        except ObjectStorageError:
            raise
        except Exception as exc:
            raise ObjectStorageError("object storage source download failed") from exc
        validate_source_payload(payload, source_path)
        return payload

    def delete(self, source_path: str) -> None:
        with tracing.span("sites.storage.object.delete", kind=3):
            return self._delete(source_path)

    def _delete(self, source_path: str) -> None:
        key = _configured_key(self.config, source_path)
        try:
            self._object_client_instance().delete_object(
                Bucket=self.config.bucket, Key=key
            )
        except ObjectStorageError:
            raise
        except Exception as exc:
            raise ObjectStorageError("object storage source deletion failed") from exc


def materialize_source(source_path: str, destination: Path) -> None:
    files = validate_source_payload(
        S3CompatibleSourceStore().get(source_path), source_path
    )
    root = destination.resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        target = root.joinpath(*PurePosixPath(name).parts)
        resolved = target.resolve(strict=False)
        if root != resolved and root not in resolved.parents:
            raise ObjectStorageError("OSS source file escapes destination")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        target.chmod(0o444)
    for directory in (item for item in root.rglob("*") if item.is_dir()):
        directory.chmod(0o755)
    # The destination is an EmptyDir volume root owned and permissioned by
    # kubelet through the Pod fsGroup.  A non-root init container may write
    # through that group but cannot chmod the root-owned mount point.  Keep
    # kubelet's mode and only normalize paths created by this process.


def main() -> int:
    parser = argparse.ArgumentParser(description="materialize an OSS Sites source object")
    parser.add_argument("--source-path", required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    try:
        materialize_source(args.source_path, args.destination)
    except (ObjectStorageError, OSError, ValueError) as exc:
        print(f"source materialization failed: {exc}", file=__import__("sys").stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
