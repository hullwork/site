"""Source-layout and import-graph contract tests."""
from __future__ import annotations

import ast
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


if __name__ == "__main__":
    unittest.main()
