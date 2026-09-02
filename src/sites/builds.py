"""Bounded Dockerfile source builds for the local Sites preview.

Source is validated and written to a dedicated PVC by the API.  The operator
then runs one rootless BuildKit Job, resolves the pushed manifest digest, and
deploys that immutable image through the existing SiteDeployment controller.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from os import getenv
from sites import tracing
from sites.naming import cr_name_for
from sites.object_storage import S3CompatibleSourceStore, source_payload
from sites.validation import (
    ValidationError,
    dns_label,
    normalize_merchant_id,
    normalize_user_id,
)


def normalize_source_backend(value: str) -> str:
    backend = (value or "pvc").strip().lower()
    if backend not in {"pvc", "oss"}:
        raise ValidationError("SITES_SOURCE_BACKEND must be pvc or oss")
    return backend


SOURCE_REQUEST_MAX_BYTES = 1024 * 1024
SOURCE_MAX_TOTAL_BYTES = 512 * 1024
SOURCE_MAX_FILE_BYTES = 256 * 1024
SOURCE_MAX_FILES = 256
SOURCE_ROOT = Path(
    getenv("SITES_SOURCE_ROOT", "/var/lib/sites/sources")
)
SOURCE_PVC_NAME = getenv("SITES_SOURCE_PVC", "sites-sources") or "sites-sources"
SOURCE_BACKEND = normalize_source_backend(getenv("SITES_SOURCE_BACKEND", "pvc") or "pvc")
OSS_AUTH_MOUNT = getenv("SITES_OSS_AUTH_MOUNT", "/var/run/sites-oss") or "/var/run/sites-oss"
OSS_AUTH_SECRET = getenv("SITES_OSS_AUTH_SECRET", "sites-oss-auth") or "sites-oss-auth"
OSS_DOWNLOADER_IMAGE = getenv(
    "SITES_OSS_DOWNLOADER_IMAGE",
    "registry.convee.local:5000/sites-control:local",
)
BUILDKIT_IMAGE = getenv(
    "SITES_BUILDKIT_IMAGE",
    "moby/buildkit:v0.30.0-rootless@sha256:"
    "d76eb1caecac5733ef7553c1e90a1b21f1bb218cd1142d3553de0747b4a14ba9",
)
REGISTRY_PUSH_HOST = getenv(
    "SITES_REGISTRY_PUSH_HOST", "sites-registry.sites-local.svc:5000"
)
REGISTRY_PULL_HOST = getenv(
    "SITES_REGISTRY_PULL_HOST", "localhost:5000"
)
REGISTRY_API = getenv(
    "SITES_REGISTRY_API", "http://sites-registry.sites-local.svc:5000"
).rstrip("/")
# Credentials for control plane access to the registry. The entire **cluster aspect** of the registry requires Basic certification.
# (proxied proxy of charts/site/templates/07-build-plane.yaml), all requests from the control plane go through that side.
#
# Global registry authentication is not enabled because node containerd must pull
# workload images anonymously. Requiring credentials on that plane would mean changing
# every node runtime. The authenticated proxy covers writes and enumeration while the
# node-facing pull path remains anonymous.
# If the password file cannot be read, it will be processed as "not matched" instead of throwing an error: authentication does not need to be enabled (for example, manual apply
# List but no Secret), failure here will make the entire registry unavailable. The price is that the configuration is leaked
# Will appear as a 401 in the build, not at startup.
REGISTRY_USERNAME = getenv("SITES_REGISTRY_USERNAME", "")
# The directory where docker config is placed in the builder Job, and the Secret that provides it. Secret by
# Registry authentication is generated together with htpasswd and the plaintext password, from one source.
REGISTRY_AUTH_MOUNT = getenv(
    "SITES_REGISTRY_AUTH_MOUNT", "/etc/sites-registry"
)
REGISTRY_AUTH_SECRET = getenv(
    "SITES_REGISTRY_AUTH_SECRET", "sites-registry-auth"
)
REGISTRY_PASSWORD_FILE = getenv(
    "SITES_REGISTRY_PASSWORD_FILE", "/var/run/sites-registry/password"
)

def registry_auth_headers() -> dict[str, str]:
    """Basic authentication header; if no credentials are configured, an empty dict will be returned, and the caller will still make an anonymous request.

    The password is in the file instead of the environment variable: the environment variable will be entered in CR, `kubectl describe`, and will also be
    Printed out by `env` any time in the same Pod. If the file cannot be read, it will be processed as "not matched" instead of throwing an error——
    Not enabling authentication is the current normal state, failure here will render the entire registry unavailable.
    """
    if not REGISTRY_USERNAME:
        return {}
    try:
        password = Path(REGISTRY_PASSWORD_FILE).read_text().strip()
    except OSError:
        return {}
    if not password:
        return {}
    token = base64.b64encode(
        f"{REGISTRY_USERNAME}:{password}".encode("utf-8")
    ).decode("ascii")
    return {"Authorization": f"Basic {token}"}
BUILD_DEADLINE_SECONDS = int(
    getenv("SITES_BUILD_DEADLINE_SECONDS", "300") or "300"
)
# Where one Job drops the digest BuildKit computed for the image it pushed. The
# leading dot keeps the directory outside the ``merchant/user/service/sha`` shape
# that _source_destination accepts, so no sourcePath can ever address it.
BUILD_METADATA_DIR = ".build-metadata"
BUILD_METADATA_FILE = "metadata.json"
BUILD_METADATA_MOUNT = "/run/build-metadata"

_SOURCE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,119}$")
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
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
_DOCKERFILE_SYNTAX = re.compile(r"(?im)^\s*#\s*syntax\s*=")


@dataclass(frozen=True)
class SourceBundle:
    merchant_id: str
    user_id: str
    service_name: str
    port: int
    health_path: str
    build_only: bool
    files: dict[str, str]
    sha256: str

    @property
    def source_path(self) -> str:
        # The merchant segment comes first: user_id is only unique within the merchant, and tenants with the same name from two merchants without it will share it.
        # The same source tree and the same registry repository - delete and build on one side and delete the other one
        # The source code has been deleted. Segments rather than concatenates, so hyphens in names contribute no ambiguity.
        return f"{self.merchant_id}/{self.user_id}/{self.service_name}/{self.sha256}"

    @property
    def repository(self) -> str:
        return f"local/{self.merchant_id}/{self.user_id}/{self.service_name}"

    @property
    def tag(self) -> str:
        return self.sha256[:32]


def _source_name(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 240:
        raise ValidationError("source file path must be 1-240 characters")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts:
        raise ValidationError(f"source file path must be relative: {value!r}")
    for part in path.parts:
        if (
            part in {"", ".", ".."}
            or not _SOURCE_SEGMENT.fullmatch(part)
            or part.lower() in _DENIED_PARTS
            or part.lower().startswith(".env.")
        ):
            raise ValidationError(f"source file path is not allowed: {value!r}")
    return path.as_posix()


def normalize_source_payload(
    payload: Any, merchant_id: str, user_id: str
) -> SourceBundle:
    """Validate a small UTF-8 Docker build context and return its digest."""
    if not isinstance(payload, dict):
        raise ValidationError("request body must be a JSON object")
    allowed = {"name", "port", "healthPath", "buildOnly", "files"}
    unexpected = sorted(set(payload) - allowed)
    if unexpected:
        raise ValidationError(
            "source builds do not accept secrets, env or build overrides: "
            + ", ".join(unexpected)
        )
    normalized_merchant = normalize_merchant_id(merchant_id)
    normalized_user = normalize_user_id(user_id)
    # The same rule as normalize_deploy_payload: the username is not silently normalized and folded into two
    # Different commit names allow the latter to quietly overwrite the build and source code of the former.
    raw_name = str(payload.get("name", ""))
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", raw_name):
        raise ValidationError(
            "service name must be 1-63 lowercase letters, digits or hyphens "
            "and start with a letter or digit"
        )
    service_name = raw_name
    try:
        port = int(payload.get("port", 8080))
    except (TypeError, ValueError) as exc:
        raise ValidationError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValidationError("port must be between 1 and 65535")
    health_path = str(payload.get("healthPath", "/healthz")).strip()
    if (
        not health_path.startswith("/")
        or len(health_path) > 128
        or any(character.isspace() for character in health_path)
    ):
        raise ValidationError(
            "healthPath must be a whitespace-free path starting with /"
        )
    build_only = payload.get("buildOnly", False)
    if not isinstance(build_only, bool):
        raise ValidationError("buildOnly must be a boolean")

    raw_files = payload.get("files")
    if not isinstance(raw_files, dict) or not raw_files:
        raise ValidationError("files must be a non-empty object")
    if len(raw_files) > SOURCE_MAX_FILES:
        raise ValidationError(f"source may contain at most {SOURCE_MAX_FILES} files")

    files: dict[str, str] = {}
    total = 0
    for raw_name, content in sorted(raw_files.items(), key=lambda item: str(item[0])):
        name = _source_name(raw_name)
        if not isinstance(content, str):
            raise ValidationError(f"source file must be UTF-8 text: {name!r}")
        if "\x00" in content:
            raise ValidationError(f"source file contains a NUL byte: {name!r}")
        size = len(content.encode("utf-8"))
        if size > SOURCE_MAX_FILE_BYTES:
            raise ValidationError(
                f"source file exceeds {SOURCE_MAX_FILE_BYTES} bytes: {name!r}"
            )
        total += size
        files[name] = content
    if total > SOURCE_MAX_TOTAL_BYTES:
        raise ValidationError(
            f"source exceeds {SOURCE_MAX_TOTAL_BYTES} UTF-8 bytes (got {total})"
        )
    # {"a": ..., "a/b": ...} passes every per-name rule but cannot exist on a
    # filesystem. Caught here so the caller gets a 400 naming the offending
    # path, instead of the FileExistsError persist_source would raise once the
    # first file is already on the PVC.
    directories = {
        parent.as_posix()
        for name in files
        for parent in PurePosixPath(name).parents
        if parent.as_posix() != "."
    }
    conflicting = sorted(directories & set(files))
    if conflicting:
        raise ValidationError(
            f"source path is used as both a file and a directory: {conflicting[0]!r}"
        )
    dockerfile = files.get("Dockerfile", "")
    if not dockerfile.strip():
        raise ValidationError("source root must contain a non-empty Dockerfile")
    if _DOCKERFILE_SYNTAX.search(dockerfile):
        raise ValidationError("external Dockerfile syntax frontends are not allowed")

    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(files[name].encode("utf-8"))
        digest.update(b"\0")
    return SourceBundle(
        merchant_id=normalized_merchant,
        user_id=normalized_user,
        service_name=service_name,
        port=port,
        health_path=health_path,
        build_only=build_only,
        files=files,
        sha256=digest.hexdigest(),
    )


def _source_destination(source_path: str, root: Path = SOURCE_ROOT) -> Path:
    parts = PurePosixPath(source_path).parts
    if len(parts) != 4 or any(not _SOURCE_SEGMENT.fullmatch(part) for part in parts):
        raise ValidationError("invalid persisted source path")
    root = root.resolve()
    destination = root.joinpath(*parts)
    resolved = destination.resolve(strict=False)
    if root != resolved and root not in resolved.parents:
        raise ValidationError("persisted source path escapes source root")
    return destination


def persist_source(
    bundle: SourceBundle,
    root: Path = SOURCE_ROOT,
    *,
    backend: str = SOURCE_BACKEND,
) -> str:
    """Atomically materialize a content-addressed source tree on the build PVC."""
    with tracing.span(
        "sites.build.persist_source",
        attributes={"sites.storage.backend": backend},
    ):
        return _persist_source(bundle, root, backend=backend)


def _persist_source(
    bundle: SourceBundle,
    root: Path = SOURCE_ROOT,
    *,
    backend: str = SOURCE_BACKEND,
) -> str:
    backend = normalize_source_backend(backend)
    if backend == "oss":
        S3CompatibleSourceStore().put(
            bundle.source_path,
            source_payload(bundle.files, bundle.sha256),
        )
        return bundle.source_path
    destination = _source_destination(bundle.source_path, root)
    if destination.is_dir():
        return bundle.source_path
    parent = destination.parent
    parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".upload-", dir=parent))
    try:
        for name, content in bundle.files.items():
            target = temporary.joinpath(*PurePosixPath(name).parts)
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            target.chmod(0o444)
        for directory in (
            item for item in temporary.rglob("*") if item.is_dir()
        ):
            directory.chmod(0o755)
        # mkdtemp starts at 0700. The BuildKit Job deliberately runs under a
        # different unprivileged UID and mounts this tree read-only, so the
        # context root must be traversable without becoming writable to it.
        temporary.chmod(0o755)
        try:
            temporary.rename(destination)
        except OSError:
            if not destination.is_dir():
                raise
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return bundle.source_path


def remove_source(
    source_path: str,
    root: Path = SOURCE_ROOT,
    *,
    backend: str = SOURCE_BACKEND,
) -> None:
    backend = normalize_source_backend(backend)
    if backend == "oss":
        S3CompatibleSourceStore().delete(source_path)
        return
    destination = _source_destination(source_path, root)
    if destination.exists():
        shutil.rmtree(destination)


def _metadata_destination(subpath: str, root: Path = SOURCE_ROOT) -> Path:
    parts = PurePosixPath(subpath).parts
    if (
        len(parts) != 2
        or parts[0] != BUILD_METADATA_DIR
        or not _SOURCE_SEGMENT.fullmatch(parts[1])
    ):
        raise ValidationError("invalid build metadata path")
    return root.joinpath(*parts)


def prepare_build_metadata(subpath: str, root: Path = SOURCE_ROOT) -> None:
    """Create the drop point the BuildKit Job writes its digest into.

    kubelet would create a missing ``subPath`` directory itself, but as root
    with mode 0755, which the builder's unprivileged UID could not write. The
    directory is opened up explicitly and it is the only writable spot on this
    volume: the source trees stay 0755/0444 and no user workload mounts it.
    """
    destination = _metadata_destination(subpath, root)
    destination.mkdir(mode=0o777, parents=True, exist_ok=True)
    destination.chmod(0o777)
    # Deliberately not delete existing metadata.json. The path of this drop point contains artifactSha256, the same path
    # According to the structure, it only corresponds to the same source code, so the digest reuse inside is safe; while unconditional deletion has a
    # A path that will lose data: After the Job is completed, the ttlSecondsAfterFinished point is recycled, and the CR is still
    # When the next round of reconcile sees Job 404, it will go here - once this digest is deleted,
    # The source code tree has been cleared by remove_source the moment the build is successful, and new jobs cannot be obtained.
    # Dockerfile, so the entire build becomes an unrecoverable failure. Make this function idempotent and restore the path
    # Only then will there be something to read.
    #
    # Known boundaries: When the same source code is built twice and produces different digests (non-reproducible builds), it will be reused here
    # The previous one. Removing SiteBuild is not affected - _cleanup_build directly rmtree the entire
    # Directory, do not rely on this.


def build_metadata_digest(subpath: str, root: Path = SOURCE_ROOT) -> str:
    """Return the digest BuildKit reported for the image this Job pushed.

    Resolving the digest by asking the registry for a tag answers "what does
    this tag point at now", not "what did this build produce": the two differ
    for anyone who can write that tag between the push and the lookup. The Job
    reports its own result onto the build PVC instead, which only the control
    plane and that Job can reach.
    """
    path = _metadata_destination(subpath, root) / BUILD_METADATA_FILE
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            "the build did not report an image digest"
        ) from exc
    digest = ""
    if isinstance(payload, dict):
        digest = str(payload.get("containerimage.digest") or "")
    if not _DIGEST.fullmatch(digest):
        raise RuntimeError("the build reported an invalid image digest")
    return digest


def remove_build_metadata(subpath: str, root: Path = SOURCE_ROOT) -> None:
    destination = _metadata_destination(subpath, root)
    if destination.exists():
        shutil.rmtree(destination)


def site_build_resource(
    bundle: SourceBundle,
    source_path: str,
    *,
    namespace: str,
    revision: str,
    node_port: int | None,
    source_backend: str = SOURCE_BACKEND,
) -> dict[str, Any]:
    name = cr_name_for(bundle.merchant_id, bundle.user_id, bundle.service_name)
    resource = {
        "apiVersion": "sites.local/v1alpha1",
        "kind": "SiteBuild",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "annotations": ({
                "sites.local/traceparent": carrier
            } if (carrier := tracing.traceparent_for_current()) else {}),
            "labels": {
                "app.kubernetes.io/managed-by": "sites-api",
                "sites.local/service": bundle.service_name,
            },
        },
        "spec": {
            "merchantID": bundle.merchant_id,
            "userID": bundle.user_id,
            "serviceName": bundle.service_name,
            "sourcePath": source_path,
            "sourceStorage": normalize_source_backend(source_backend),
            "artifactSha256": bundle.sha256,
            "dockerfile": "Dockerfile",
            "repository": bundle.repository,
            "tag": bundle.tag,
            "port": bundle.port,
            "healthPath": bundle.health_path,
            "buildOnly": bundle.build_only,
            "revision": revision,
        },
    }
    if node_port is not None:
        # The API owns NodePort admission. Persist that decision so the
        # operator cannot fall back to site_deployment_resource's default
        # after an expensive build has already completed. Gateway exposure
        # has no port allocation, so its CR intentionally omits this field.
        resource["spec"]["nodePort"] = node_port
    return resource


def build_job_name(build: dict[str, Any]) -> str:
    metadata = build.get("metadata") or {}
    spec = build.get("spec") or {}
    return dns_label(
        f"build-{metadata.get('name', '')}-{str(spec.get('artifactSha256', ''))[:12]}"
    )


def build_metadata_subpath(build: dict[str, Any]) -> str:
    return f"{BUILD_METADATA_DIR}/{build_job_name(build)}"


def build_job_resource(
    build: dict[str, Any],
    *,
    namespace: str,
) -> dict[str, Any]:
    metadata = build.get("metadata") or {}
    spec = build.get("spec") or {}
    name = build_job_name(build)
    repository = str(spec["repository"])
    tag = str(spec["tag"])
    source_backend = normalize_source_backend(str(spec.get("sourceStorage") or "pvc"))
    use_oss = source_backend == "oss"

    if use_oss:
        buildkit_mounts = [
            {
                "name": "source-context",
                "mountPath": "/workspace",
                "readOnly": True,
            },
            {
                "name": "source",
                "mountPath": BUILD_METADATA_MOUNT,
                "subPath": build_metadata_subpath(build),
            },
        ]
        source_volumes = [
            {
                "name": "source-context",
                "emptyDir": {"sizeLimit": "64Mi"},
            },
        ]
        init_containers = [
            {
                "name": "fetch-source",
                "image": OSS_DOWNLOADER_IMAGE,
                "imagePullPolicy": "Always",
                "command": ["python3", "-m", "sites.object_storage"],
                "args": [
                    "--source-path", str(spec["sourcePath"]),
                    "--destination", "/workspace",
                ],
                "env": [
                    {"name": "SITES_OSS_ENDPOINT", "value": getenv("SITES_OSS_ENDPOINT", "") or ""},
                    {"name": "SITES_OSS_BUCKET", "value": getenv("SITES_OSS_BUCKET", "") or ""},
                    {"name": "SITES_OSS_PREFIX", "value": getenv("SITES_OSS_PREFIX", "") or ""},
                    {"name": "SITES_OSS_REGION", "value": getenv("SITES_OSS_REGION", "") or ""},
                    {
                        "name": "SITES_OSS_ADDRESSING_STYLE",
                        "value": getenv("SITES_OSS_ADDRESSING_STYLE", "virtual") or "virtual",
                    },
                    {
                        "name": "SITES_OSS_SIGNATURE_VERSION",
                        "value": getenv("SITES_OSS_SIGNATURE_VERSION", "s3") or "s3",
                    },
                    {"name": "SITES_OSS_ACCESS_KEY_ID_FILE", "value": f"{OSS_AUTH_MOUNT}/access-key-id"},
                    {"name": "SITES_OSS_ACCESS_KEY_SECRET_FILE", "value": f"{OSS_AUTH_MOUNT}/access-key-secret"},
                ],
                "securityContext": {
                    "runAsUser": 65532,
                    "runAsGroup": 65532,
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"]},
                    "seccompProfile": {"type": "RuntimeDefault"},
                },
                "resources": {
                    "requests": {"cpu": "10m", "memory": "32Mi"},
                    "limits": {"cpu": "250m", "memory": "128Mi"},
                },
                "volumeMounts": [
                    {"name": "source-context", "mountPath": "/workspace"},
                    {"name": "oss-auth", "mountPath": OSS_AUTH_MOUNT, "readOnly": True},
                ],
            }
        ]
        extra_volumes = [
            {
                "name": "oss-auth",
                "secret": {
                    "secretName": OSS_AUTH_SECRET,
                    "items": [
                        {"key": "access-key-id", "path": "access-key-id"},
                        {"key": "access-key-secret", "path": "access-key-secret"},
                    ],
                },
            },
            *source_volumes,
        ]
    else:
        buildkit_mounts = [
            {
                "name": "source",
                "mountPath": "/workspace",
                "subPath": str(spec["sourcePath"]),
                "readOnly": True,
            },
            {
                "name": "source",
                "mountPath": BUILD_METADATA_MOUNT,
                "subPath": build_metadata_subpath(build),
            },
        ]
        init_containers = []
        extra_volumes = []

    buildkit = {
        "name": "buildkit",
        "image": BUILDKIT_IMAGE,
        "imagePullPolicy": "IfNotPresent",
        "env": [
            {
                "name": "BUILDKITD_FLAGS",
                "value": (
                    "--oci-worker-no-process-sandbox "
                    "--oci-worker-snapshotter=native"
                ),
            },
            # Credentials for push are read from here. Give the directory explicitly instead of inferring it from $HOME/.docker:
            # If the rootless image changes user, it will change HOME, and the performance at that time is push 401, which will not happen.
            # Anything pointing to "Configuration not read".
            {"name": "DOCKER_CONFIG", "value": REGISTRY_AUTH_MOUNT},
        ],
        "command": ["buildctl-daemonless.sh"],
        "args": [
            "build",
            "--frontend", "dockerfile.v0",
            "--local", "context=/workspace",
            "--local", "dockerfile=/workspace",
            # The builder reports the digest it pushed instead of the operator
            # asking the registry what the tag resolves to afterwards.
            "--metadata-file",
            f"{BUILD_METADATA_MOUNT}/{BUILD_METADATA_FILE}",
            "--output",
            (
                "type=image,name="
                f"{REGISTRY_PUSH_HOST}/{repository}:{tag},"
                "push=true,registry.insecure=true"
            ),
        ],
        # The upstream rootless image needs its setuid newuidmap/newgidmap
        # helpers. This is explicitly a local preview boundary, not a
        # production tenant isolation profile.
        "securityContext": {
            "runAsUser": 1000,
            "runAsGroup": 1000,
            "allowPrivilegeEscalation": True,
            "seccompProfile": {"type": "Unconfined"},
            "appArmorProfile": {"type": "Unconfined"},
        },
        "resources": {
            "requests": {
                "cpu": "100m", "memory": "256Mi", "ephemeral-storage": "256Mi",
            },
            "limits": {
                "cpu": "1", "memory": "1Gi", "ephemeral-storage": "2Gi",
            },
        },
        "volumeMounts": [
            *buildkit_mounts,
            {"name": "buildkitd", "mountPath": "/home/user/.local/share/buildkit"},
            # 🔴 The credentials are hung on the file system of **this container**, and the tenant's RUN cannot be read: RUN runs on
            # The current container of buildkitd has its own mount namespace and the namespace from the image layer.
            # rootfs, no path to the Job container is visible. Only the network namespace is shared -
            # Therefore, the tenant can reach the registry, but cannot get the credentials, and the entire cluster requires authentication.
            {"name": "registry-auth", "mountPath": REGISTRY_AUTH_MOUNT, "readOnly": True},
        ],
    }

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/name": "sites-builder",
                "app.kubernetes.io/managed-by": "sites-operator",
                "sites.local/build": str(metadata["name"]),
            },
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": BUILD_DEADLINE_SECONDS,
            "ttlSecondsAfterFinished": 600,
            "template": {
                "metadata": {
                    "labels": {
                        "app.kubernetes.io/name": "sites-builder",
                        "sites.local/build": str(metadata["name"]),
                    }
                },
                "spec": {
                    "restartPolicy": "Never",
                    "serviceAccountName": "sites-builder",
                    "automountServiceAccountToken": False,
                    # kubelet creates EmptyDir roots as root.  The OSS fetcher
                    # remains non-root, so give every container the fetcher's
                    # supplementary group and let fsGroup make the shared
                    # source/buildkit volumes writable without granting root.
                    **(
                        {
                            "securityContext": {
                                "fsGroup": 65532,
                                "fsGroupChangePolicy": "OnRootMismatch",
                            }
                        }
                        if use_oss
                        else {}
                    ),
                    **({"initContainers": init_containers} if init_containers else {}),
                    "containers": [buildkit],
                    "volumes": [
                        {
                            "name": "registry-auth",
                            "secret": {
                                "secretName": REGISTRY_AUTH_SECRET,
                                "items": [{"key": "config.json", "path": "config.json"}],
                            },
                        },
                        {
                            "name": "source",
                            "persistentVolumeClaim": {"claimName": SOURCE_PVC_NAME},
                        },
                        *extra_volumes,
                        {"name": "buildkitd", "emptyDir": {"sizeLimit": "2Gi"}},
                    ],
                },
            },
        },
    }

def job_complete(job: dict[str, Any]) -> bool:
    return any(
        condition.get("type") == "Complete" and condition.get("status") == "True"
        for condition in (job.get("status") or {}).get("conditions") or []
    )


def job_failure(job: dict[str, Any]) -> str | None:
    for condition in (job.get("status") or {}).get("conditions") or []:
        if condition.get("type") == "Failed" and condition.get("status") == "True":
            return str(
                condition.get("message")
                or condition.get("reason")
                or "BuildKit Job failed"
            )
    return None


def registry_manifest_digest(repository: str, tag: str) -> str:
    """Resolve what a tag points at *right now* in the local registry.

    Deliberately not part of the build path any more, and it must not be wired
    back into one: the local registry is unauthenticated, so a tag lookup
    reports whoever wrote that tag last, not the build that produced it. See
    ``build_metadata_digest`` for the authoritative source. Kept for diagnosing
    the registry by hand, where "what does this tag resolve to" is the question
    actually being asked.
    """
    request = urllib.request.Request(
        f"{REGISTRY_API}/v2/{repository}/manifests/{tag}",
        method="HEAD",
        headers={
            "Accept": (
                "application/vnd.oci.image.manifest.v1+json, "
                "application/vnd.docker.distribution.manifest.v2+json"
            ),
            **registry_auth_headers(),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            digest = response.headers.get("Docker-Content-Digest", "")
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        raise RuntimeError("built image is unavailable in the local registry") from exc
    if not _DIGEST.fullmatch(digest):
        raise RuntimeError("local registry returned an invalid image digest")
    return digest


def delete_registry_manifest(repository: str, digest: str) -> None:
    if not _DIGEST.fullmatch(digest):
        return
    request = urllib.request.Request(
        f"{REGISTRY_API}/v2/{repository}/manifests/{digest}",
        method="DELETE",
        headers=registry_auth_headers(),
    )
    try:
        with urllib.request.urlopen(request, timeout=10):
            return
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise RuntimeError("local registry manifest deletion failed") from exc
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        raise RuntimeError("local registry is unavailable during cleanup") from exc


def immutable_image(repository: str, digest: str) -> str:
    if not _DIGEST.fullmatch(digest):
        raise ValidationError("invalid built image digest")
    return f"{REGISTRY_PULL_HOST}/{repository}@{digest}"


def site_build_response(build: dict[str, Any]) -> dict[str, Any]:
    metadata = build.get("metadata") or {}
    spec = build.get("spec") or {}
    status = build.get("status") or {}
    service_name = str(spec.get("serviceName", ""))
    phase = str(status.get("phase") or "Pending")
    return {
        "name": metadata.get("name"),
        # Two pieces of identity are given together: user_id is only unique within the merchant. If one is missing, the specific row cannot be located.
        "merchantId": spec.get("merchantID"),
        "userId": spec.get("userID"),
        "serviceName": service_name,
        "phase": phase,
        "ready": bool(status.get("ready")) and phase == "Running",
        "message": status.get("message", ""),
        "url": status.get("url"),
        "revision": spec.get("revision"),
        "artifactSha256": spec.get("artifactSha256"),
        "imageDigest": status.get("imageDigest"),
        "image": status.get("image"),
        "verification": status.get("verification"),
        "jobName": status.get("jobName") or build_job_name(build),
        "status_url": f"/v1/builds/{dns_label(service_name)}"
        if service_name
        else None,
    }
