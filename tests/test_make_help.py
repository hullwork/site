"""`make help` has to list every target, or it is worse than no help at all.

This listing used to be 14 hand-written ``echo`` lines: a second copy of the
target list, kept in the same file as the targets it describes and compared
against them by nothing. It had already lost ``help`` itself, and nothing could
have said when.

It is now generated from the targets. That narrows the failure mode to exactly
one thing -- a target with no annotation is silently absent -- and this module
closes it. A listing that claims to be complete while omitting a target is worse
than no listing, because a reader who trusts it stops looking.

``tests/test_readme_claims.py`` guards the other half of the same first minute:
the test count the README quotes.
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
MAKEFILE = REPO_ROOT / "Makefile"

#: ``target: ## — one line``. The em dash is what the help target splits on.
DESCRIBED = re.compile(r"^(?P<target>[a-z][a-z0-9-]*):[^\n]*## — (?P<text>.+)$", re.M)


def phony_targets() -> list[str]:
    match = re.search(
        r"^\.PHONY:(?P<targets>.+)$", MAKEFILE.read_text(encoding="utf-8"), re.M,
    )
    assert match is not None, "the Makefile no longer declares .PHONY"
    return match.group("targets").split()


class HelpListingTests(unittest.TestCase):
    def test_there_are_targets_to_check(self) -> None:
        """Rules out "the .PHONY pattern stopped matching", which would make
        every assertion below a pass over an empty list."""
        self.assertGreater(len(phony_targets()), 5)

    def test_every_phony_target_carries_a_description(self) -> None:
        text = MAKEFILE.read_text(encoding="utf-8")
        described = {match.group("target") for match in DESCRIBED.finditer(text)}
        missing = sorted(set(phony_targets()) - described)
        self.assertEqual(
            [], missing,
            "these targets would be missing from `make help`, which claims to "
            f"list every one of them: {missing}",
        )

    def test_no_description_is_empty(self) -> None:
        text = MAKEFILE.read_text(encoding="utf-8")
        blank = [
            match.group("target") for match in DESCRIBED.finditer(text)
            if not match.group("text").strip()
        ]
        self.assertEqual([], blank)

    def test_help_actually_prints_them(self) -> None:
        """Run make rather than re-implement its grep.

        A test that reproduced the extraction would agree with a broken help
        target as readily as with a working one.
        """
        result = subprocess.run(
            ["make", "help"], cwd=REPO_ROOT,
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        for target in phony_targets():
            with self.subTest(target=target):
                self.assertIn(f"{target} — ", result.stdout)


if __name__ == "__main__":
    unittest.main()
