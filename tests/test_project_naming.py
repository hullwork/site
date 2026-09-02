"""A repository-wide gate against the retired project prefix.

The products were renamed before open-sourcing and the old prefix must not come
back.  This repository had no gate for it at all: nothing here would have
noticed the prefix reappearing in a chart, a manifest, a scaffold or a document.

Two failure modes are guarded against explicitly, because both produce a green
test that protects nothing, and ``test_cloud_identifiers.py`` next door was
written against the same two:

* a scan face that is too narrow.  Here it is every git-tracked path, and
  :class:`ScanFaceTests` asserts both that the odd corners are in it - Makefile,
  Dockerfile and scripts/ have no file extension between them - and that files
  are really being read.
* a matcher that cannot fire.  :class:`MatcherTests` pins it in both
  directions, because the prefix is a substring of ``administrator``,
  ``minimum``, ``deterministic``, ``minimatch`` and ``minimal`` - all
  legitimate, all present here, so a bare substring test is not an option and an
  over-broad one gets narrowed until it guards nothing.

The patterns fire on three shapes only: the prefix bound to a product name, the
retired environment-variable namespace, and the retired dotdir prefix.
"""
from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent

RETIRED_PREFIX = "mi" + "ni"

# Spelled in fragments for the same reason the patterns are: written out, this
# file would be its own first violation, and the usual fix for that - excluding
# the guard's own file - is how a scan face starts shrinking.
_PRODUCT_WORDS = ("ag" + "ent", "sand" + "box", "si" + "te", "infra")
_PRODUCTS = "(?:" + "|".join(f"{word}(?:e?s)?" for word in _PRODUCT_WORDS) + ")"

RETIRED_NAME_PATTERNS = {
    # The prefix bound to a product name: any case, an optional leading dot, and
    # "-", "_", ".", a space or nothing at all as the separator.
    "product name": re.compile(
        rf"(?i)(?<![A-Za-z0-9])\.?{RETIRED_PREFIX}[-_. ]?{_PRODUCTS}(?![A-Za-z0-9])"
    ),
    # The retired environment-variable namespace.  The lookbehind keeps GEMINI_*
    # out; MINIO_* and MINIMUM_* have no underscore in that position.
    "environment namespace": re.compile(rf"(?<![A-Za-z0-9_]){RETIRED_PREFIX.upper()}_"),
    # Retired state/config dotdirs.  Requiring the separator keeps ".minio/" out.
    "dotdir prefix": re.compile(rf"(?i)(?<![A-Za-z0-9])\.{RETIRED_PREFIX}[-_]"),
}


def retired_name_hits(text: str, location: str = "") -> list[str]:
    """Every match, as ``location:line [shape] text`` when a location is given."""
    hits: list[str] = []
    for label, pattern in RETIRED_NAME_PATTERNS.items():
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            where = f"{location}:{line} " if location else ""
            hits.append(f"{where}[{label}] {match.group(0)}")
    return hits


def tracked_files() -> list[str]:
    """Every path git tracks.  ``check=True`` so a broken git fails, not passes."""
    output = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    return [name for name in output.decode("utf-8").rstrip("\0").split("\0") if name]


def scan_tracked() -> tuple[list[str], list[str], list[str]]:
    """Returns (offenders, paths whose contents were read, paths skipped).

    Every tracked path is scanned, including extension-less ones such as
    Makefile and Dockerfile: a suffix allowlist silently exempts exactly those.
    """
    offenders: list[str] = []
    scanned: list[str] = []
    skipped: list[str] = []
    for relative in tracked_files():
        path = ROOT / relative
        offenders += [f"{relative} (path name) {hit}" for hit in retired_name_hits(relative)]
        if path.is_file():
            # errors="replace" so an eventual binary file cannot abort the scan;
            # the patterns cannot match a replacement character.
            text = path.read_text(encoding="utf-8", errors="replace")
            offenders += retired_name_hits(text, relative)
            scanned.append(relative)
        else:
            # A tracked path that is not a regular file (a submodule gitlink)
            # has no content here; its name is still scanned above.
            skipped.append(relative)
    return offenders, scanned, skipped


class RetiredProjectPrefixTests(unittest.TestCase):
    def test_tracked_repository_is_free_of_the_retired_prefix(self) -> None:
        offenders, scanned, skipped = scan_tracked()
        self.assertEqual([], offenders)
        self.assertEqual(len(tracked_files()), len(scanned) + len(skipped))


class ScanFaceTests(unittest.TestCase):
    """The guard is only worth its green if it looks at the whole repository."""

    def test_scan_face_covers_every_tracked_area(self) -> None:
        names = set(tracked_files())
        # One witness per area a directory- or suffix-scoped guard would miss;
        # the extension-less ones are the reason there is no suffix allowlist.
        for witness in (
            "Makefile",
            "Dockerfile",
            "LICENSE",
            "README.md",
            "scripts/test-oss.sh",
            "docs/DEPLOYMENT.md",
            "src/sites/storage.py",
            "charts/site/values.yaml",
            "charts/site/templates/10-control-plane.yaml",
            "console/src/mock.ts",
            ".github/workflows/ci.yml",
            "tests/test_project_naming.py",
        ):
            self.assertIn(witness, names)

    def test_scan_face_actually_reads_files(self) -> None:
        # A filter that quietly drops every file also reports zero violations.
        _, scanned, _ = scan_tracked()
        self.assertGreater(len(scanned), 150)


class MatcherTests(unittest.TestCase):
    """Mutation control: the matcher must be red on the shapes it claims to
    guard and green on the legitimate words it is a substring of."""

    def test_matcher_flags_every_retired_shape(self) -> None:
        prefix = RETIRED_PREFIX
        must_match = [
            f"{prefix}{separator}{word}{plural}"
            for word in _PRODUCT_WORDS
            for separator in ("-", "_", ".", " ", "")
            for plural in ("", "s")
        ]
        must_match += [
            f"{prefix.upper()}-{_PRODUCT_WORDS[0].upper()}",
            f"{prefix.capitalize()}{_PRODUCT_WORDS[1].capitalize()}",
            f".{prefix}-{_PRODUCT_WORDS[3]}",
            f".{prefix}-state",
            f"{prefix.upper()}_FOO",
            f"{prefix.upper()}_INFRA_ROOT",
            f"export {prefix.upper()}_HOME=/tmp",
            f"clusters/{prefix}-{_PRODUCT_WORDS[0]}/kustomization.yaml",
            f"image: registry.example.com/{prefix}-{_PRODUCT_WORDS[1]}:v1",
        ]
        for sample in must_match:
            with self.subTest(sample=sample):
                self.assertTrue(
                    retired_name_hits(sample),
                    f"naming gate missed {sample!r}",
                )

    def test_matcher_leaves_legitimate_words_alone(self) -> None:
        # An over-broad pattern breaks on all of these.
        # Most of them occur in this tree; gpt-4o-* is a third-party model.
        must_not_match = (
            "gpt-4o-" + RETIRED_PREFIX,
            "administrator",
            "administration",
            "administrative",
            "administrators",
            "deterministic",
            "deterministically",
            "minimum",
            "minimal",
            "minimumSuccessRate",
            "minimumValidTrials",
            "minio",
            "minio-mc",
            "minio-client",
            "minio_mc_image",
            "MINIO_ROOT_USER",
            "minimax",
            "MiniMax-M2.7",
            "minimatch",
            "mining",
            "minify",
            "ministered",
            "GEMINI_API_KEY",
            ".minio/config",
        )
        for sample in must_not_match:
            with self.subTest(sample=sample):
                self.assertEqual(
                    [], retired_name_hits(sample),
                    f"naming gate over-matched {sample!r}",
                )

    def test_the_scanner_reports_where_the_hit_is(self) -> None:
        # End-to-end over the reporter, not just the regex: a guard that says
        # only "something is wrong" is a guard nobody can act on.
        planted = "clean line\n" + RETIRED_PREFIX + "-" + _PRODUCT_WORDS[0] + "\n"
        self.assertEqual(
            ["overlay.yaml:2 [product name] " + RETIRED_PREFIX + "-" + _PRODUCT_WORDS[0]],
            retired_name_hits(planted, "overlay.yaml"),
        )


if __name__ == "__main__":
    unittest.main()
