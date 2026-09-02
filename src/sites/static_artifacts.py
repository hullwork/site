"""Private, content-addressed static-site artifacts in S3-compatible storage.

The public upload contract is deliberately smaller than the API's 64 KiB request
limit.  Objects are private JSON envelopes; runtimes receive an opaque sourcePath,
never credentials or an HTTP URL.
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

from sites.object_storage import ObjectStorageError, S3CompatibleConfig
from sites.validation import (
    ValidationError,
    normalize_merchant_id,
    normalize_site_name,
    normalize_user_id,
)


STATIC_ARTIFACT_VERSION = 1
STATIC_ARTIFACT_OBJECT_FILE = "artifact.json"
STATIC_ARTIFACT_OBJECT_NAMESPACE = "static"
# Leave room for identity fields and the enclosing API JSON below its 64 KiB cap.
STATIC_ARTIFACT_MAX_TOTAL_BYTES = 48 * 1024
STATIC_ARTIFACT_MAX_JSON_BYTES = 60 * 1024
STATIC_ARTIFACT_MAX_OBJECT_BYTES = 64 * 1024
STATIC_ARTIFACT_MAX_FILE_BYTES = 32 * 1024
STATIC_ARTIFACT_MAX_FILES = 128
STATIC_ARTIFACT_MAX_PATH_BYTES = 240
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_URI_SCHEMES = {"oss", "s3"}
_DENIED_PARTS = {".", "..", ".git", ".hg", ".svn"}


def _utf8_size(value: str, label: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValidationError(f"{label} must be valid UTF-8 text") from exc


@dataclass(frozen=True)
class StaticArtifact:
    merchant_id: str
    user_id: str
    site_name: str
    files: dict[str, str]
    sha256: str
    size_bytes: int

    @property
    def source_path(self) -> str:
        return "/".join(
            (self.merchant_id, self.user_id, self.site_name, self.sha256)
        )

    def payload(self) -> dict[str, Any]:
        return {
            "version": STATIC_ARTIFACT_VERSION,
            "sha256": self.sha256,
            "sizeBytes": self.size_bytes,
            "files": self.files,
        }


def _normalize_file_path(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError("static artifact file path must be non-empty text")
    if "\\" in value or "\x00" in value or any(ord(char) < 32 for char in value):
        raise ValidationError("static artifact file path contains unsafe characters")
    if _utf8_size(value, "static artifact file path") > STATIC_ARTIFACT_MAX_PATH_BYTES:
        raise ValidationError(
            f"static artifact file path exceeds {STATIC_ARTIFACT_MAX_PATH_BYTES} bytes"
        )
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts:
        raise ValidationError("static artifact file path must be relative")
    if any(
        part in _DENIED_PARTS
        or not part
        or part.lower().startswith(".env")
        for part in path.parts
    ):
        raise ValidationError("static artifact file path is not allowed")
    normalized = path.as_posix()
    if normalized != value:
        raise ValidationError("static artifact file path must be normalized")
    return normalized


def _content_digest(files: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for name, content in sorted(files.items()):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def normalize_static_artifact(
    merchant_id: str,
    user_id: str,
    site_name: str,
    files: object,
    declared_sha256: str | None = None,
) -> StaticArtifact:
    """Validate a bounded UTF-8 file tree containing a root ``index.html``."""
    merchant = normalize_merchant_id(merchant_id)
    user = normalize_user_id(user_id)
    site = normalize_site_name(site_name)
    if not isinstance(files, dict) or not files:
        raise ValidationError("static artifact files must be a non-empty object")
    if len(files) > STATIC_ARTIFACT_MAX_FILES:
        raise ValidationError(
            f"static artifact may contain at most {STATIC_ARTIFACT_MAX_FILES} files"
        )

    normalized: dict[str, str] = {}
    total = 0
    for raw_name, content in sorted(files.items(), key=lambda item: str(item[0])):
        name = _normalize_file_path(raw_name)
        if not isinstance(content, str):
            raise ValidationError(f"static artifact file must be UTF-8 text: {name!r}")
        if "\x00" in content:
            raise ValidationError(f"static artifact file contains a NUL byte: {name!r}")
        size = _utf8_size(content, f"static artifact file {name!r}")
        if size > STATIC_ARTIFACT_MAX_FILE_BYTES:
            raise ValidationError(
                f"static artifact file exceeds {STATIC_ARTIFACT_MAX_FILE_BYTES} bytes: {name!r}"
            )
        total += size
        normalized[name] = content
    if "index.html" not in normalized:
        raise ValidationError("static artifact root must contain index.html")
    if total > STATIC_ARTIFACT_MAX_TOTAL_BYTES:
        raise ValidationError(
            f"static artifact exceeds {STATIC_ARTIFACT_MAX_TOTAL_BYTES} UTF-8 bytes (got {total})"
        )

    directories = {
        parent.as_posix()
        for name in normalized
        for parent in PurePosixPath(name).parents
        if parent.as_posix() != "."
    }
    conflict = sorted(directories.intersection(normalized))
    if conflict:
        raise ValidationError(
            f"static artifact path is both a file and directory: {conflict[0]!r}"
        )
    request_fragment = json.dumps(
        {"files": normalized}, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    if len(request_fragment) > STATIC_ARTIFACT_MAX_JSON_BYTES:
        raise ValidationError(
            "static artifact JSON encoding leaves insufficient room in the 64 KiB request"
        )

    digest = _content_digest(normalized)
    if declared_sha256 is not None:
        declared = str(declared_sha256).strip().lower()
        if not _SHA256_RE.fullmatch(declared) or declared != digest:
            raise ValidationError("declared static artifact sha256 does not match content")
    return StaticArtifact(merchant, user, site, normalized, digest, total)


def validate_static_artifact_payload(
    payload: object, source_path: str
) -> StaticArtifact:
    """Revalidate identity, digest and byte counts from an untrusted object."""
    merchant, user, site, digest = _source_path_parts(source_path)
    if not isinstance(payload, dict) or payload.get("version") != STATIC_ARTIFACT_VERSION:
        raise ObjectStorageError("unsupported static artifact object version")
    if set(payload) != {"version", "sha256", "sizeBytes", "files"}:
        raise ObjectStorageError("static artifact object has unexpected fields")
    if payload.get("sha256") != digest:
        raise ObjectStorageError("static artifact source path does not match its digest")
    try:
        artifact = normalize_static_artifact(
            merchant, user, site, payload.get("files"), declared_sha256=digest
        )
    except ValidationError as exc:
        raise ObjectStorageError(f"invalid static artifact object: {exc}") from exc
    if type(payload.get("sizeBytes")) is not int or payload["sizeBytes"] != artifact.size_bytes:
        raise ObjectStorageError("static artifact size mismatch")
    return artifact


def _source_path_parts(source_path: str) -> tuple[str, str, str, str]:
    path = PurePosixPath(source_path)
    if path.is_absolute() or len(path.parts) != 4:
        raise ObjectStorageError("invalid static artifact source path")
    merchant, user, site, digest = path.parts
    try:
        normalized = (
            normalize_merchant_id(merchant),
            normalize_user_id(user),
            normalize_site_name(site),
        )
    except ValidationError as exc:
        raise ObjectStorageError("invalid static artifact source path") from exc
    if normalized != (merchant, user, site) or not _SHA256_RE.fullmatch(digest):
        raise ObjectStorageError("invalid static artifact source path")
    return merchant, user, site, digest


ALIYUN_OSS_HOST = "aliyuncs.com"


def _endpoint_host(endpoint: str) -> str:
    """Host of an S3-compatible endpoint, tolerating the scheme-less form."""
    candidate = endpoint.strip()
    if "//" not in candidate:
        candidate = "//" + candidate
    return (urlsplit(candidate).hostname or "").rstrip(".")


def is_aliyun_oss_endpoint(endpoint: str) -> bool:
    """Whether the endpoint addresses Aliyun OSS, decided on the parsed host.

    Testing ``"aliyuncs.com" in endpoint`` lets the marker sit anywhere in the
    URL, so ``https://evil.example/aliyuncs.com`` is judged to be OSS. Only the
    host decides, and only on whole labels: ``notaliyuncs.com`` is a different
    domain and must not match either.
    """
    host = _endpoint_host(endpoint)
    return host == ALIYUN_OSS_HOST or host.endswith("." + ALIYUN_OSS_HOST)


def static_artifact_object_key(config: S3CompatibleConfig, source_path: str) -> str:
    _source_path_parts(source_path)
    relative = (
        f"{STATIC_ARTIFACT_OBJECT_NAMESPACE}/{source_path}/"
        f"{STATIC_ARTIFACT_OBJECT_FILE}"
    )
    return f"{config.prefix}/{relative}" if config.prefix else relative


def static_source_path_from_uri(uri: str) -> str:
    """Recover and validate the opaque sourcePath from a credential-free URI."""
    parsed = urlsplit(str(uri or ""))
    if (
        parsed.scheme not in _URI_SCHEMES
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ObjectStorageError("invalid static artifact URI")
    parts = PurePosixPath(parsed.path.lstrip("/")).parts
    if len(parts) < 6 or parts[-6] != STATIC_ARTIFACT_OBJECT_NAMESPACE:
        raise ObjectStorageError("invalid static artifact URI path")
    if parts[-1] != STATIC_ARTIFACT_OBJECT_FILE:
        raise ObjectStorageError("invalid static artifact URI object")
    source_path = PurePosixPath(*parts[-5:-1]).as_posix()
    _source_path_parts(source_path)
    return source_path


class S3CompatibleStaticArtifactStore:
    def __init__(
        self,
        config: S3CompatibleConfig | None = None,
        client: Any | None = None,
    ) -> None:
        self.config = config or S3CompatibleConfig.from_env()
        self._client = client

    def _object_client(self) -> Any:
        if self._client is None:
            self._client = self.config.client()
        return self._client

    def artifact_uri(self, source_path: str) -> str:
        key = static_artifact_object_key(self.config, source_path)
        scheme = "oss" if is_aliyun_oss_endpoint(self.config.endpoint) else "s3"
        return f"{scheme}://{self.config.bucket}/{key}"

    def put(self, artifact: StaticArtifact) -> dict[str, Any]:
        validate_static_artifact_payload(artifact.payload(), artifact.source_path)
        body = json.dumps(
            artifact.payload(), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if len(body) > STATIC_ARTIFACT_MAX_OBJECT_BYTES:
            raise ObjectStorageError("static artifact object is too large")
        key = static_artifact_object_key(self.config, artifact.source_path)
        try:
            self._object_client().put_object(
                Bucket=self.config.bucket,
                Key=key,
                Body=body,
                ContentType="application/json",
            )
        except ObjectStorageError:
            raise
        except Exception as exc:
            raise ObjectStorageError("static artifact upload failed") from exc
        return {
            "contentSha256": artifact.sha256,
            "sha256": artifact.sha256,
            "artifactUri": self.artifact_uri(artifact.source_path),
            "sourcePath": artifact.source_path,
            "sizeBytes": artifact.size_bytes,
            "fileCount": len(artifact.files),
        }

    def get(self, source_path: str) -> StaticArtifact:
        key = static_artifact_object_key(self.config, source_path)
        try:
            response = self._object_client().get_object(
                Bucket=self.config.bucket, Key=key
            )
            content_length = int(response.get("ContentLength", 0) or 0)
            if content_length > STATIC_ARTIFACT_MAX_OBJECT_BYTES:
                raise ObjectStorageError("static artifact object is too large")
            body = response["Body"].read(STATIC_ARTIFACT_MAX_OBJECT_BYTES + 1)
            if len(body) > STATIC_ARTIFACT_MAX_OBJECT_BYTES:
                raise ObjectStorageError("static artifact object is too large")
            payload = json.loads(body.decode("utf-8"))
        except ObjectStorageError:
            raise
        except Exception as exc:
            raise ObjectStorageError("static artifact download failed") from exc
        return validate_static_artifact_payload(payload, source_path)


class StaticArtifactService:
    def __init__(self, store: S3CompatibleStaticArtifactStore | None = None) -> None:
        # Keep object-storage configuration lazy: dynamic-only/local control-plane
        # processes must still start when SITES_OSS_* is intentionally absent.
        self._store = store

    @property
    def store(self) -> S3CompatibleStaticArtifactStore:
        if self._store is None:
            self._store = S3CompatibleStaticArtifactStore()
        return self._store

    @classmethod
    def from_env(cls) -> "StaticArtifactService":
        """Build the production service from the existing ``SITES_OSS_*`` settings."""
        return cls()

    def create_version_artifact(
        self,
        merchant_id: str,
        user_id: str,
        site_name: str,
        files: object,
        declared_sha256: str | None = None,
    ) -> dict[str, Any]:
        artifact = normalize_static_artifact(
            merchant_id, user_id, site_name, files, declared_sha256
        )
        return self.store.put(artifact)

    def load(self, source_path: str) -> StaticArtifact:
        return self.store.get(source_path)

    def materialize(self, source_path: str, destination: Path) -> None:
        materialize_static_artifact(self.load(source_path), destination)


def materialize_static_artifact(artifact: StaticArtifact, destination: Path) -> None:
    """Write a validated artifact into a new or empty directory without traversal."""
    root = destination.resolve(strict=False)
    if destination.exists():
        if not destination.is_dir():
            raise ObjectStorageError("static artifact destination must be an empty directory")
        if any(destination.iterdir()):
            # Kubernetes retries an init container against the same EmptyDir.
            # Accept only an exact copy left by an interrupted attempt; partial,
            # changed, symlinked, and extra content remains a hard failure.
            expected_files = set(artifact.files)
            actual_files: set[str] = set()
            for item in destination.rglob("*"):
                if item.is_symlink():
                    raise ObjectStorageError(
                        "static artifact destination must be an empty directory"
                    )
                if item.is_file():
                    relative = item.relative_to(destination).as_posix()
                    actual_files.add(relative)
                    try:
                        content = item.read_text(encoding="utf-8")
                    except (OSError, UnicodeError) as exc:
                        raise ObjectStorageError(
                            "static artifact destination must be an empty directory"
                        ) from exc
                    if artifact.files.get(relative) != content:
                        raise ObjectStorageError(
                            "static artifact destination must be an empty directory"
                        )
            if actual_files == expected_files:
                return
            raise ObjectStorageError("static artifact destination must be an empty directory")
    else:
        destination.mkdir(parents=True)
    root = destination.resolve(strict=True)
    for name, content in artifact.files.items():
        target = root.joinpath(*PurePosixPath(name).parts)
        resolved = target.resolve(strict=False)
        if root not in resolved.parents:
            raise ObjectStorageError("static artifact file escapes destination")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        target.chmod(0o444)
    for directory in (item for item in root.rglob("*") if item.is_dir()):
        directory.chmod(0o555)
    root.chmod(0o555)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="materialize a versioned static Sites artifact"
    )
    parser.add_argument("--source-path", required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    try:
        StaticArtifactService.from_env().materialize(
            args.source_path, args.destination
        )
    except (ObjectStorageError, OSError, ValueError) as exc:
        print(
            f"static artifact materialization failed: {exc}",
            file=__import__("sys").stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
