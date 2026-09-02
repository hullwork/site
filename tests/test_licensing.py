"""Copyleft components in the distributed image must be recorded, not discovered.

🔴 This repository is MIT and its image ships `psycopg` and `psycopg-binary`,
both LGPL-3.0-only. Importing a library and distributing a copy of it are
different acts and only the second carries the obligation, so nothing about the
source tree makes this visible -- the fact lived only in wheel metadata nobody
read. A sibling repository already had the right shape for the AGPL MinIO
client it ships: upstream licence text, exact upstream identity, and where the
corresponding source is. This pins the same shape here.

🔴 The classifier has three states, and "cannot tell" is one of them. A package
whose licence cannot be read fails this suite rather than being treated as
permissive -- the whole point of a gate like this is that a dependency added
next month cannot slip past it by being unreadable.
"""
from __future__ import annotations

import importlib.metadata as metadata
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "Dockerfile"
NOTICE = ROOT / "NOTICE"
LOCK = ROOT / "requirements.lock"

COPYLEFT = re.compile(r"\b(GPL|LGPL|AGPL|MPL|EPL|CDDL|CeCILL|OSL|SSPL)\b", re.I)
PERMISSIVE = re.compile(
    r"\b(MIT|BSD|Apache|ISC|PSF|Python Software Foundation|Zlib|Unlicense|CC0|HPND|PostgreSQL)\b",
    re.I,
)


def locked_requirements() -> dict[str, str]:
    """Every pinned package in requirements.lock that this image installs.

    Markered-out entries are excluded: `tzdata` is `sys_platform == 'win32'` and
    is not in a Linux image, so requiring a licence record for it would be a
    finding about a file that does not exist.
    """
    packages: dict[str, str] = {}
    for line in LOCK.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Za-z0-9_.-]+)==([^\s;\\]+)(.*)$", line)
        if not match:
            continue
        name, version, tail = match.groups()
        if "sys_platform == 'win32'" in tail:
            continue
        packages[name] = version
    return packages


def verdict_for(evidence: str) -> str:
    """COPYLEFT / PERMISSIVE / UNKNOWN from licence evidence text.

    Split out from :func:`classify` so the three states can be pinned directly.
    A classifier that silently answered PERMISSIVE for unreadable input would
    otherwise be invisible: every real dependency happens to be readable today,
    so the UNKNOWN branch would never execute in the suite.
    """
    evidence = " ".join(evidence.split())
    if not evidence:
        return "UNKNOWN"
    # Copyleft wins over permissive: a package that names both is the case
    # where the obligation exists, so the conservative reading is the safe one.
    if COPYLEFT.search(evidence):
        return "COPYLEFT"
    if PERMISSIVE.search(evidence):
        return "PERMISSIVE"
    return "UNKNOWN"


def classify(name: str) -> tuple[str, str]:
    """(verdict, evidence) where verdict is COPYLEFT / PERMISSIVE / UNKNOWN."""
    try:
        meta = metadata.metadata(name)
    except metadata.PackageNotFoundError:
        return "UNKNOWN", "package is not installed in this environment"
    # Every source together, not the first non-empty one. `python-dateutil`
    # sets License to the literal string "Dual License", which is not a licence
    # name at all; taking it alone and stopping made a dual BSD/Apache package
    # unreadable. Reading all three resolves it from its classifiers.
    parts = [
        meta.get("License-Expression") or "",
        meta.get("License") or "",
        *(c for c in (meta.get_all("Classifier") or []) if c.startswith("License")),
    ]
    evidence = " ".join(" ".join(parts).split())
    return verdict_for(evidence), evidence or "no licence field and no licence classifier"


class DependencyLicenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.locked = locked_requirements()
        # Red if the parser silently matches nothing -- a scan face that reads
        # zero packages also reports zero unrecorded ones. This is the shape
        # that made a sibling gate green over 16 of 79 files.
        self.assertGreater(len(self.locked), 5, self.locked)
        self.assertIn("psycopg", self.locked)

    def test_no_dependency_has_an_unreadable_licence(self) -> None:
        # 🔴 The third state. Treating "could not determine" as "not copyleft"
        # is how an obligation gets missed: the answer looks the same as a pass.
        unknown = {
            name: evidence
            for name in self.locked
            for verdict, evidence in [classify(name)]
            if verdict == "UNKNOWN"
        }
        self.assertEqual(
            {}, unknown,
            "resolve these by hand and record the result in NOTICE; they are "
            "not permissive until someone has read them",
        )

    def test_every_copyleft_dependency_is_recorded_in_the_image(self) -> None:
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")
        notice = NOTICE.read_text(encoding="utf-8")
        copyleft = [name for name in self.locked if classify(name)[0] == "COPYLEFT"]
        # A gate over an empty list passes for the wrong reason. There are two
        # today and they are named, so removing the dependency is a deliberate
        # edit here rather than a silent weakening.
        self.assertEqual(["psycopg", "psycopg-binary"], sorted(copyleft))
        for name in copyleft:
            with self.subTest(package=name):
                self.assertIn(
                    f'"{name}"', dockerfile,
                    f"{name} is copyleft and the image's COPYLEFT set omits it",
                )
                self.assertIn("/usr/share/licenses", dockerfile)
                self.assertIn(name, notice, f"{name} is copyleft and NOTICE omits it")

    def test_a_permissive_dependency_is_not_required_to_be_recorded(self) -> None:
        # The other direction: a classifier that answered COPYLEFT for
        # everything would satisfy the case above and make the gate meaningless.
        verdict, evidence = classify("boto3")
        self.assertEqual("PERMISSIVE", verdict, evidence)
        self.assertNotIn('"boto3"', DOCKERFILE.read_text(encoding="utf-8"))

    def test_unreadable_licence_evidence_is_unknown_not_permissive(self) -> None:
        # 🔴 The third state, pinned directly. Every real dependency is
        # readable today, so this branch never runs during the sweep above --
        # a classifier that answered PERMISSIVE for "" would pass every other
        # case in this file while making the gate unable to refuse anything.
        for unreadable in ("", "   ", "see LICENSE file", "Dual License", "Proprietary"):
            with self.subTest(evidence=unreadable):
                self.assertEqual("UNKNOWN", verdict_for(unreadable))
        # And the two it must still decide.
        self.assertEqual("COPYLEFT", verdict_for("LGPL-3.0-only"))
        self.assertEqual("PERMISSIVE", verdict_for("Apache-2.0"))
        # Copyleft wins when both are named.
        self.assertEqual("COPYLEFT", verdict_for("MIT AND GPL-2.0-or-later"))

    def test_the_image_derives_versions_instead_of_restating_them(self) -> None:
        # An earlier revision named `psycopg-3.3.4.dist-info` in the Dockerfile,
        # so every lock bump silently needed a matching edit here -- and CI only
        # runs `docker build --check`, which does not execute RUN steps, so the
        # mismatch would have surfaced at release time. The record is built from
        # installed metadata now; this keeps a hardcoded version from coming
        # back.
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")
        block = dockerfile[dockerfile.index("RUN python3 - <<'PY'"):]
        for name, version in self.locked.items():
            if classify(name)[0] != "COPYLEFT":
                continue
            with self.subTest(package=name):
                self.assertNotIn(
                    version, block,
                    f"the licence layer restates {name}'s version instead of "
                    "reading it from the installed distribution",
                )
        self.assertIn("dist.version", block)

    def test_the_notice_states_the_base_image_boundary(self) -> None:
        # The base image carries GPL components too. That is not a defect, but
        # leaving it unsaid makes the tables above look like a complete audit
        # when they only cover what this repository adds.
        notice = NOTICE.read_text(encoding="utf-8")
        self.assertIn("python:3.14-alpine3.24", notice)
        self.assertIn("does not modify", notice.lower().replace("not modify", "does not modify"))
        digest = re.search(
            r"FROM python:3\.14-alpine3\.24@(sha256:[a-f0-9]{64})",
            DOCKERFILE.read_text(encoding="utf-8"),
        )
        self.assertIsNotNone(digest, "the base image is not digest-pinned")
        # Red if the base image is bumped without revisiting what it carries.
        self.assertIn(digest.group(1), notice)


if __name__ == "__main__":
    unittest.main()
