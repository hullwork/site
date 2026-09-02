"""No private cloud account or instance identifier may enter the release tree.

None of these shapes is a credential, so a secret scanner (gitleaks, run
separately in CI) is blind to every one of them: an OSS bucket named after the
account UID has no entropy signature and no ``key``-looking neighbour.  They
still name the account that operates this deployment and turn an open
repository into an enumerable target list.  gitleaks and this guard are two
different jobs; one does not substitute for the other.

Two failure modes are guarded against explicitly, because both produce a green
test that protects nothing:

* a scan face that is too narrow.  ``scripts/test-oss.sh`` carried the account
  UID for the whole life of the repository while a sibling implementation of
  this guard only ever looked at its deployment-template directories.  Here the
  face is every git-tracked file, and :meth:`ScanFaceTests` asserts that.
* patterns that match nothing.  :meth:`PatternTests` feeds each pattern a
  synthetic violation of the shape it claims to guard and requires a hit.

The samples below are assembled from fragments at runtime on purpose: written
as plain literals they would make this file its own first violation, and the
usual fix for that -- excluding the guard's own file -- is how a scan face
starts shrinking.  This is not hypothetical: the first draft of this module
spelled the real UID out in this docstring and the guard failed on itself.
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parent.parent
_ALIYUN = "aliyuncs" + ".com"

CLOUD_IDENTIFIER_PATTERNS = {
    # An Alibaba Cloud RDS host: rm-<instance> . mysql . rds . <the domain
    # below>.  A reachable managed database, named in full.
    "rds instance endpoint": re.compile(r"\.rds\." + re.escape(_ALIYUN)),
    # The instance id on its own, e.g. in a kubeconfig or a runbook.
    "rds instance id": re.compile(r"\brm-[0-9a-z]{8,}\b"),
    # A 16-digit Alibaba Cloud account UID, usually embedded in an OSS bucket
    # name.  A bare \d{16} would misfire on container image digests: some 16 of
    # a sha256's 64 hex characters happen to be all-digits in about one digest
    # in forty.  The hex lookarounds drop that to a run at the very start or end
    # of the token, ~1 in 900, and the accepted cost is that a UID glued
    # straight onto hex characters is missed.
    "cloud account uid": re.compile(r"(?<![0-9a-fA-F])\d{16}(?![0-9a-fA-F])"),
    # Bailian workspace-dedicated inference domain (llm-*.<region>.maas...).
    "bailian workspace domain": re.compile(r"maas\." + re.escape(_ALIYUN)),
    # An access key id is a credential, but it is also an account identifier and
    # costs nothing to add here rather than relying on the scanner alone.
    "aliyun access key id": re.compile(r"\bLTAI[0-9A-Za-z]{12,}\b"),
}


def tracked_files() -> list[pathlib.Path]:
    """Every file git tracks. ``check=True`` so a broken git fails, not passes."""
    listing = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    return [ROOT / name for name in listing.split("\0") if name]


def cloud_identifier_hits(paths: list[pathlib.Path] | None = None) -> list[str]:
    hits: list[str] = []
    for path in sorted(tracked_files() if paths is None else paths):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for label, pattern in CLOUD_IDENTIFIER_PATTERNS.items():
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                name = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
                hits.append(f"{name}:{line} [{label}] {match.group(0)}")
    return hits


class CloudIdentifierLeakTests(unittest.TestCase):
    def test_repository_carries_no_cloud_account_identifiers(self) -> None:
        self.assertEqual([], cloud_identifier_hits())


class ScanFaceTests(unittest.TestCase):
    """The guard is only worth its green if it looks at the whole repository."""

    def test_scan_face_covers_every_tracked_area(self) -> None:
        names = {path.relative_to(ROOT).as_posix() for path in tracked_files()}
        # One witness per area a directory-scoped guard would have skipped --
        # scripts/ is where the UID actually lived.
        for witness in (
            "scripts/test-oss.sh",
            "tests/test_cloud_identifiers.py",
            "docs/DEPLOYMENT.md",
            "README.md",
            "src/sites/storage.py",
            "charts/site/values.yaml",
            "charts/site/templates/10-control-plane.yaml",
            "console/src/mock.ts",
            "evaluation/README.md",
            ".github/workflows/ci.yml",
        ):
            self.assertIn(witness, names)

    def test_scan_face_is_not_empty(self) -> None:
        self.assertGreater(len(tracked_files()), 100)


class PatternTests(unittest.TestCase):
    """Mutation control: every pattern must flag the shape it claims to guard."""

    def test_each_pattern_flags_its_own_shape(self) -> None:
        samples = {
            "rds instance endpoint": "host: rm-" + "uf6abcdef" + ".mysql" + ".rds." + _ALIYUN,
            "rds instance id": "instance: rm-" + "uf6abcdef" + "12345",
            "cloud account uid": "bucket: site-" + "1544957340" + "007056",
            "bailian workspace domain": "https://llm-abc.cn-beijing." + "maas." + _ALIYUN,
            "aliyun access key id": "id: " + "LTAI" + "5tExampleKeyIdAA",
        }
        self.assertEqual(set(CLOUD_IDENTIFIER_PATTERNS), set(samples))
        for label, sample in samples.items():
            with self.subTest(label=label):
                self.assertTrue(
                    CLOUD_IDENTIFIER_PATTERNS[label].search(sample),
                    f"{label} matched nothing in its own sample",
                )

    def test_the_scanner_reports_a_planted_file(self) -> None:
        """End-to-end over the reader and the reporter, not just the regex.

        The probe is handed in rather than committed: a guard that stages files
        into the caller's index to prove itself is a worse trade than this.
        """
        with tempfile.TemporaryDirectory() as directory:
            planted = pathlib.Path(directory) / "overlay.yaml"
            planted.write_text(
                "host: rm-" + "uf6abcdef" + ".mysql" + ".rds." + _ALIYUN + "\n"
                "bucket: site-" + "1544957340" + "007056" + "\n",
                encoding="utf-8",
            )
            hits = cloud_identifier_hits([planted])
        self.assertTrue(all("overlay.yaml:" in hit for hit in hits), hits)
        self.assertEqual(
            {"rds instance endpoint", "rds instance id", "cloud account uid"},
            {hit.split("[", 1)[1].split("]", 1)[0] for hit in hits},
            hits,
        )

    def test_public_endpoints_and_placeholders_are_not_identifiers(self) -> None:
        for benign in (
            "https://oss-cn-shanghai.aliyuncs.com",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "SITES_TEST_OSS_BUCKET:-REPLACE_ME_OSS_BUCKET",
            'revision: "1754800000000000000"',
            "sha256:0c79d56aee561a1d81c63f00eee5fb5fe29279560cdc55e91425133104c7fbe6",
            "expiresAt: 4000000000",
        ):
            with self.subTest(benign=benign):
                self.assertFalse(
                    [
                        label
                        for label, pattern in CLOUD_IDENTIFIER_PATTERNS.items()
                        if pattern.search(benign)
                    ]
                )


if __name__ == "__main__":
    unittest.main()
