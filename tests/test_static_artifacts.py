"""Tests for private content-addressed static artifacts."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sites.object_storage import ObjectStorageError, S3CompatibleConfig
from sites.static_artifacts import (
    STATIC_ARTIFACT_MAX_TOTAL_BYTES,
    S3CompatibleStaticArtifactStore,
    StaticArtifactService,
    is_aliyun_oss_endpoint,
    materialize_static_artifact,
    normalize_static_artifact,
    static_source_path_from_uri,
)
from sites.validation import ValidationError


class _Body:
    def __init__(self, value: bytes) -> None:
        self.value = value

    def read(self, limit: int) -> bytes:
        return self.value[:limit]


class _FakeS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, **_: object) -> None:
        self.objects[(Bucket, Key)] = Body

    def get_object(self, *, Bucket: str, Key: str) -> dict:
        body = self.objects[(Bucket, Key)]
        return {"ContentLength": len(body), "Body": _Body(body)}


def _config(endpoint: str = "https://s3.example.test") -> S3CompatibleConfig:
    return S3CompatibleConfig(
        endpoint=endpoint,
        bucket="private-sites",
        prefix="tenant-artifacts",
        region="test-1",
        addressing_style="path",
        signature_version="s3v4",
        access_key_id_file=Path("/unused"),
        access_key_secret_file=Path("/unused"),
    )


class StaticArtifactTests(unittest.TestCase):
    def test_service_uploads_content_addressed_private_object(self) -> None:
        client = _FakeS3()
        service = StaticArtifactService(
            S3CompatibleStaticArtifactStore(_config(), client)
        )
        result = service.create_version_artifact(
            "acme", "alice", "docs", {"index.html": "<h1>ok</h1>", "assets/app.js": "ok"}
        )
        self.assertEqual(result["sha256"], result["contentSha256"])
        self.assertEqual(
            result["sourcePath"],
            f"acme/alice/docs/{result['contentSha256']}",
        )
        self.assertEqual(
            result["artifactUri"],
            "s3://private-sites/tenant-artifacts/static/"
            f"{result['sourcePath']}/artifact.json",
        )
        self.assertNotIn("example.test", result["artifactUri"])
        self.assertEqual(static_source_path_from_uri(result["artifactUri"]), result["sourcePath"])
        loaded = service.load(result["sourcePath"])
        self.assertEqual(loaded.files["assets/app.js"], "ok")

    def test_aliyun_endpoint_returns_oss_uri_without_credentials(self) -> None:
        result = StaticArtifactService(
            S3CompatibleStaticArtifactStore(
                _config("https://s3.oss-cn-shanghai.aliyuncs.com"), _FakeS3()
            )
        ).create_version_artifact("acme", "alice", "docs", {"index.html": "ok"})
        self.assertTrue(result["artifactUri"].startswith("oss://private-sites/"))

    def test_oss_scheme_is_decided_on_the_endpoint_host(self) -> None:
        """The OSS/S3 scheme must come from the parsed host, not a substring.

        Every ``False`` case below is accepted by ``"aliyuncs.com" in endpoint``:
        the marker sits in the path, the query, the userinfo, or is glued onto a
        longer label. Keep them here — they are what makes this test able to tell
        a host check apart from a substring check.
        """
        for endpoint, expected in (
            ("https://oss-cn-shanghai.aliyuncs.com", True),
            ("https://bucket.oss-cn-shanghai.aliyuncs.com:443", True),
            ("HTTPS://OSS-CN-SHANGHAI.ALIYUNCS.COM", True),
            ("https://oss-cn-shanghai.aliyuncs.com.", True),
            ("oss-cn-shanghai.aliyuncs.com", True),
            ("https://aliyuncs.com", True),
            ("https://evil.example/aliyuncs.com", False),
            ("https://evil.example/?x=aliyuncs.com", False),
            ("https://aliyuncs.com@evil.example", False),
            ("https://notaliyuncs.com", False),
            ("https://aliyuncs.com.evil.example", False),
            ("https://s3.example.test", False),
            ("", False),
        ):
            with self.subTest(endpoint=endpoint):
                self.assertIs(is_aliyun_oss_endpoint(endpoint), expected)

    def test_artifact_uri_scheme_survives_a_lookalike_endpoint(self) -> None:
        result = StaticArtifactService(
            S3CompatibleStaticArtifactStore(
                _config("https://evil.example/aliyuncs.com"), _FakeS3()
            )
        ).create_version_artifact("acme", "alice", "docs", {"index.html": "ok"})
        self.assertTrue(result["artifactUri"].startswith("s3://private-sites/"))

    def test_validation_requires_index_and_rejects_traversal(self) -> None:
        with self.assertRaisesRegex(ValidationError, "index.html"):
            normalize_static_artifact("acme", "alice", "docs", {"about.html": "x"})
        for path in ("../index.html", "/index.html", "assets\\index.html", "a/../../index.html"):
            with self.subTest(path=path), self.assertRaises(ValidationError):
                normalize_static_artifact("acme", "alice", "docs", {"index.html": "x", path: "x"})
        with self.assertRaisesRegex(ValidationError, "valid UTF-8"):
            normalize_static_artifact("acme", "alice", "docs", {"index.html": "\ud800"})

    def test_public_limit_leaves_request_envelope_room(self) -> None:
        self.assertLess(STATIC_ARTIFACT_MAX_TOTAL_BYTES, 64 * 1024)
        with self.assertRaisesRegex(ValidationError, "exceeds"):
            normalize_static_artifact(
                "acme", "alice", "docs", {"index.html": "x" * (STATIC_ARTIFACT_MAX_TOTAL_BYTES + 1)}
            )
        with self.assertRaisesRegex(ValidationError, "JSON encoding"):
            normalize_static_artifact(
                "acme", "alice", "docs", {"index.html": "\\" * (32 * 1024), "a.js": "\\" * (16 * 1024)}
            )

    def test_declared_digest_and_downloaded_size_are_reverified(self) -> None:
        client = _FakeS3()
        store = S3CompatibleStaticArtifactStore(_config(), client)
        artifact = normalize_static_artifact("acme", "alice", "docs", {"index.html": "ok"})
        result = store.put(artifact)
        key = next(iter(client.objects))
        payload = json.loads(client.objects[key])
        payload["sizeBytes"] += 1
        client.objects[key] = json.dumps(payload).encode()
        with self.assertRaisesRegex(ObjectStorageError, "size mismatch"):
            store.get(result["sourcePath"])
        payload["sizeBytes"] -= 1
        payload["files"]["index.html"] = "tampered"
        client.objects[key] = json.dumps(payload).encode()
        with self.assertRaisesRegex(ObjectStorageError, "sha256 does not match"):
            store.get(result["sourcePath"])
        with self.assertRaisesRegex(ValidationError, "does not match"):
            normalize_static_artifact(
                "acme", "alice", "docs", {"index.html": "ok"}, "0" * 64
            )

    def test_materialize_preserves_tree_and_accepts_exact_retry(self) -> None:
        artifact = normalize_static_artifact(
            "acme", "alice", "docs", {"index.html": "ok", "assets/app.js": "js"}
        )
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "site"
            materialize_static_artifact(artifact, destination)
            self.assertEqual((destination / "assets/app.js").read_text(), "js")
            materialize_static_artifact(artifact, destination)

            (destination / "index.html").chmod(0o644)
            (destination / "index.html").write_text("changed", encoding="utf-8")
            with self.assertRaisesRegex(ObjectStorageError, "empty directory"):
                materialize_static_artifact(artifact, destination)

    def test_uri_parser_rejects_credentials_and_wrong_shape(self) -> None:
        digest = "a" * 64
        for uri in (
            f"https://bucket/static/acme/alice/docs/{digest}/artifact.json",
            f"s3://user:secret@bucket/static/acme/alice/docs/{digest}/artifact.json",
            f"s3://bucket/static/acme/alice/docs/{digest}/artifact.json?token=x",
            f"s3://bucket/static/acme/alice/docs/{digest}/other.json",
        ):
            with self.subTest(uri=uri), self.assertRaises(ObjectStorageError):
                static_source_path_from_uri(uri)


if __name__ == "__main__":
    unittest.main()
