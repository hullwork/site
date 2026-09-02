"""Optional real S3/OSS compatibility smoke for immutable static artifacts.

The required suite uses an in-memory fake. This lane performs one upload, download,
materialization, and exact-key cleanup only when dedicated test credentials are supplied.
"""
from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from pathlib import Path

from sites.object_storage import S3CompatibleConfig
from sites.static_artifacts import (
    S3CompatibleStaticArtifactStore,
    StaticArtifactService,
    static_artifact_object_key,
)


_REQUIRED = (
    "SITES_TEST_OSS_ENDPOINT",
    "SITES_TEST_OSS_BUCKET",
    "SITES_TEST_OSS_PREFIX",
    "SITES_TEST_OSS_REGION",
    "SITES_TEST_OSS_ACCESS_KEY_ID_FILE",
    "SITES_TEST_OSS_ACCESS_KEY_SECRET_FILE",
)


@unittest.skipUnless(
    all(os.environ.get(name) for name in _REQUIRED),
    "dedicated S3/OSS test configuration is unavailable",
)
class StaticArtifactCloudTests(unittest.TestCase):
    def test_private_content_addressed_round_trip(self) -> None:
        prefix = os.environ["SITES_TEST_OSS_PREFIX"].strip().strip("/")
        if not prefix.startswith("site-e2e/"):
            self.fail("SITES_TEST_OSS_PREFIX must be isolated below site-e2e/")
        config = S3CompatibleConfig(
            endpoint=os.environ["SITES_TEST_OSS_ENDPOINT"],
            bucket=os.environ["SITES_TEST_OSS_BUCKET"],
            prefix=prefix,
            region=os.environ["SITES_TEST_OSS_REGION"],
            addressing_style=os.environ.get(
                "SITES_TEST_OSS_ADDRESSING_STYLE", "virtual"
            ),
            signature_version=os.environ.get(
                "SITES_TEST_OSS_SIGNATURE_VERSION", "s3"
            ),
            access_key_id_file=Path(
                os.environ["SITES_TEST_OSS_ACCESS_KEY_ID_FILE"]
            ),
            access_key_secret_file=Path(
                os.environ["SITES_TEST_OSS_ACCESS_KEY_SECRET_FILE"]
            ),
        )
        store = S3CompatibleStaticArtifactStore(config)
        service = StaticArtifactService(store)
        site_name = f"static-e2e-{uuid.uuid4().hex[:12]}"
        result = service.create_version_artifact(
            "e2e", "cloud", site_name,
            {
                "index.html": "<!doctype html><h1>site OSS E2E</h1>",
                "assets/app.js": "document.documentElement.dataset.e2e='ok'",
            },
        )
        object_key = static_artifact_object_key(config, result["sourcePath"])
        client = store._object_client()
        self.addCleanup(
            client.delete_object, Bucket=config.bucket, Key=object_key
        )

        loaded = service.load(result["sourcePath"])
        self.assertEqual(loaded.sha256, result["contentSha256"])
        with tempfile.TemporaryDirectory(prefix="site-static-oss-e2e-") as root:
            destination = Path(root) / "site"
            service.materialize(result["sourcePath"], destination)
            self.assertIn("OSS E2E", (destination / "index.html").read_text())
            self.assertTrue((destination / "assets/app.js").is_file())


if __name__ == "__main__":
    unittest.main()
