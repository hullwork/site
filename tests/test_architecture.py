"""Source-layout and import-graph contract tests."""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src" / "sites"


class ImportGraphTests(unittest.TestCase):
    def test_runtime_import_graph_is_acyclic(self) -> None:
        modules = {path.stem for path in PACKAGE.glob("*.py")}
        edges = {module: set() for module in modules}
        for source in PACKAGE.glob("*.py"):
            tree = ast.parse(source.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                else:
                    continue
                for name in names:
                    parts = name.split(".")
                    if parts[:1] == ["sites"] and len(parts) > 1:
                        if parts[1] in modules:
                            edges[source.stem].add(parts[1])

        state: dict[str, int] = {}
        path: list[str] = []

        def visit(module: str) -> None:
            state[module] = 1
            path.append(module)
            for dependency in sorted(edges[module]):
                if state.get(dependency) == 1:
                    cycle_start = path.index(dependency)
                    self.fail(
                        "runtime import cycle: "
                        + " -> ".join(path[cycle_start:] + [dependency])
                    )
                if state.get(dependency, 0) == 0:
                    visit(dependency)
            path.pop()
            state[module] = 2

        for module in sorted(modules):
            if state.get(module, 0) == 0:
                visit(module)


class RefusalTextTests(unittest.TestCase):
    """Every message the caller is left holding has to read as one sentence."""

    RUN_TOGETHER = re.compile(r"^[a-z]{2,}[A-Z][a-z]{2,}$")

    @staticmethod
    def _repository_word_counts() -> dict[str, int]:
        """How often each word occurs across the sources a name can live in.

        No hand-written vocabulary, which is what rots after a rename and then
        passes everything. A real field name is written down somewhere else too
        -- a schema, the Chart, a document -- while a word produced by joining
        two message fragments exists in exactly one place: the message.
        """
        counts: dict[str, int] = {}
        files = [
            *PACKAGE.glob("*.py"),
            *(ROOT / "charts").rglob("*.yaml"),
            *(ROOT / "docs").glob("*.md"),
            ROOT / "README.md",
        ]
        for path in files:
            for word in re.findall(r"[A-Za-z][A-Za-z0-9_]*", path.read_text(encoding="utf-8")):
                counts[word] = counts.get(word, 0) + 1
        return counts

    @classmethod
    def _run_together(cls, text: str, counts: dict[str, int]) -> str | None:
        for match in re.finditer(r"[A-Za-z][A-Za-z0-9_]*", text):
            word = match.group(0)
            if cls.RUN_TOGETHER.match(word) and counts.get(word, 0) <= 1:
                return word
        return None

    def test_the_check_can_tell_the_two_apart(self) -> None:
        """Prove the judge before trusting the sweep below.

        Its first version stripped every token containing a capital as
        "an identifier" -- including the run-together word it was looking for --
        and reported zero across the whole package. A sweep that cannot fail is
        not evidence, so both directions are asserted here.
        """
        counts = {"scaleToZero": 40, "healthPath": 30, "noThe": 1}
        self.assertEqual(
            self._run_together("there will be noThe link can receive", counts),
            "noThe",
        )
        self.assertIsNone(self._run_together("scaleToZero needs a gateway", counts))
        self.assertIsNone(self._run_together("healthPath is /", counts))

    def test_field_names_used_elsewhere_are_not_flagged(self) -> None:
        # Names that appear only inside message text still count as real when
        # the Chart, a schema or a document also writes them down.
        counts = self._repository_word_counts()
        for field in ("clusterNetwork", "fieldRef", "deploymentIntent", "claimValue"):
            with self.subTest(field=field):
                self.assertGreater(counts.get(field, 0), 1)

    def test_no_refusal_message_runs_two_sentences_together(self) -> None:
        """Two translated fragments joined without a space become one bad word.

        `"...there will be no" "The link can receive..."` reached the caller as
        "there will be noThe link can receive", and it is the only thing the
        caller gets: the request is refused with a 400 whose whole explanation is
        that sentence.
        """
        counts = self._repository_word_counts()
        offenders = []
        scanned = 0
        for source in sorted(PACKAGE.glob("*.py")):
            tree = ast.parse(source.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Raise):
                    continue
                for child in ast.walk(node):
                    if (
                        isinstance(child, ast.Constant)
                        and isinstance(child.value, str)
                        and len(child.value) > 25
                    ):
                        scanned += 1
                        text = " ".join(child.value.split())
                        word = self._run_together(text, counts)
                        if word:
                            offenders.append(f"{source.name}:{child.lineno} {word!r}")
        # A zero with a zero denominator would mean the sweep found nothing to
        # look at, which is not the same answer.
        self.assertGreater(scanned, 100, "no refusal messages were examined")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
