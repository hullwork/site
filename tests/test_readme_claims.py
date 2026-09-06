"""The README's checkable claims, checked.

The "quick start" block is the first thing a newcomer runs, and the count in
it is their only calibration for whether ``make test`` did what the README
said. This one had drifted to 959 while ``make test`` collected 993, and
nothing in the repository could say when it stopped being true.

Only the count is gated. A wall-clock figure is a property of the machine
rather than of this repository, so it is not asserted here.

Discovery is loader-only: modules are imported and cases counted, nothing is
executed twice. The two guards below exist because the equality alone is not
a gate - it holds just as well between two empty suites, and ``discover``
turns a module that stopped importing into a case that is counted here and
only fails when run.
"""
from __future__ import annotations

import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
README = REPO_ROOT / "README.md"

#: The line the quick start tells a newcomer to run, and its claim.
CLAIM = re.compile(r"^make test\s+# (?P<count>\d+) tests$", re.M)

#: Rules out "discovery returned an empty suite", which would otherwise make
#: the equality below a statement about two zeroes.
MINIMUM_COLLECTED = 500


def collect() -> list[unittest.TestCase]:
    """Every case ``make test`` would run, without running any of them.

    Mirrors the Makefile's ``discover -s tests -t . -p 'test_*.py'``.
    """
    suite = unittest.defaultTestLoader.discover(
        start_dir=str(REPO_ROOT / "tests"),
        pattern="test_*.py",
        top_level_dir=str(REPO_ROOT),
    )
    cases: list[unittest.TestCase] = []

    def flatten(item) -> None:
        if isinstance(item, unittest.TestSuite):
            for child in item:
                flatten(child)
        else:
            cases.append(item)

    flatten(suite)
    return cases


class ReadmeTestCountTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = README.read_text(encoding="utf-8")
        self.cases = collect()

    def test_discovery_actually_collected_a_suite(self) -> None:
        """A green equality between two empty sets is not a passing gate."""
        self.assertGreaterEqual(len(self.cases), MINIMUM_COLLECTED)

    def test_discovery_did_not_swallow_an_import_error(self) -> None:
        """``discover`` turns an unimportable module into a counted case.

        It becomes a ``_FailedTest`` that only fails when *run*, so a module
        that stopped importing would still be counted here and the README
        would still agree with the total.
        """
        broken = [
            case.id() for case in self.cases
            if type(case).__name__ == "_FailedTest"
        ]
        self.assertEqual([], broken, f"modules that do not import: {broken}")

    def test_readme_states_the_count_make_test_collects(self) -> None:
        match = CLAIM.search(self.text)
        self.assertIsNotNone(
            match,
            "the `make test` line in README.md no longer has the shape this "
            "gate reads; update CLAIM and the line together",
        )
        claimed = int(match.group("count"))
        actual = len(self.cases)
        self.assertEqual(
            claimed, actual,
            "README.md line "
            f"{self.text[:match.start()].count(chr(10)) + 1} claims {claimed} "
            f"tests; `make test` collects {actual}. Replace it with:\n"
            f"make test        # {actual} tests",
        )


class EnvironmentVariableDiscoverabilityTests(unittest.TestCase):
    """Every knob the code reads has to be named somewhere a reader looks.

    README carried "45 `SITES_*` environment variables ... appear in no document
    or chart" long after the number stopped being true: measured on the same
    definition the sentence uses, it was 8. A count nobody recomputes is a claim,
    and this one drifted the safe direction -- overstating a gap that had mostly
    been closed -- which is why nothing surfaced it.
    """

    READ = re.compile(
        r"(?:getenv|environ\.get|environ)\(\s*[\"'](SITES_[A-Z0-9_]+)[\"']"
    )

    @staticmethod
    def _named_in(paths) -> set[str]:
        found: set[str] = set()
        for path in paths:
            if path.is_file():
                found |= set(
                    re.findall(r"SITES_[A-Z0-9_]+", path.read_text(encoding="utf-8"))
                )
        return found

    def _read_by_source(self) -> set[str]:
        names: set[str] = set()
        for path in (REPO_ROOT / "src" / "sites").glob("*.py"):
            names |= set(self.READ.findall(path.read_text(encoding="utf-8")))
        return names

    def test_the_scan_finds_the_variables_it_is_meant_to(self) -> None:
        """Guard the denominator: an empty read set would pass silently."""
        read = self._read_by_source()
        self.assertGreater(len(read), 50, "the getenv scan matched almost nothing")
        for known in ("SITES_HOST_PORT_BASE", "SITES_EXPOSURE_BACKEND"):
            self.assertIn(known, read)

    def test_every_variable_the_code_reads_is_named_somewhere(self) -> None:
        documented = self._named_in(
            list((REPO_ROOT / "docs").glob("*.md")) + [README]
        )
        charted = self._named_in((REPO_ROOT / "charts").rglob("*"))
        missing = sorted(self._read_by_source() - documented - charted)
        self.assertEqual(
            missing,
            [],
            "read by src/sites/ but named in no document and no chart template; "
            "add a row to docs/CONFIGURATION.md#undocumented-tuning-variables "
            f"or set it in the chart: {missing}",
        )


if __name__ == "__main__":
    unittest.main()
