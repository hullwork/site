"""Every Markdown table in this repository renders as a table.

GitHub needs a blank line above a table. Without one the pipes are absorbed
into the preceding paragraph and printed literally - the README's own Quick
Start block was published that way, and nothing said so: the files parse, the
tests pass, and editors that render tables leniently show them correctly. Only
the published page is wrong, which is the one view no test was looking at.
"""
from __future__ import annotations

import pathlib
import subprocess
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SEPARATOR_CHARS = set("|-: ")


def _is_separator(line: str) -> bool:
    stripped = line.strip()
    return (
        stripped.startswith("|")
        and "-" in stripped
        and set(stripped) <= SEPARATOR_CHARS
    )


def tracked_markdown() -> list[pathlib.Path]:
    """``check=True`` so a broken git fails the check rather than passing it."""
    listing = subprocess.run(
        ["git", "ls-files", "-z", "*.md"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    return [REPO_ROOT / name for name in listing.split("\0") if name]


def tables_without_a_blank_line_above(path: pathlib.Path) -> list[int]:
    lines = path.read_text(encoding="utf-8").splitlines()
    found, fenced = [], False
    for index, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        following = lines[index + 1] if index + 1 < len(lines) else ""
        if not (line.strip().startswith("|") and _is_separator(following)):
            continue
        if index and lines[index - 1].strip():
            found.append(index + 1)
    return found


class MarkdownTableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.files = tracked_markdown()

    def test_there_are_markdown_files_to_check(self) -> None:
        """An empty file list would make the assertion below vacuously true."""
        self.assertGreaterEqual(len(self.files), 10)

    def test_the_checker_recognises_a_table(self) -> None:
        """Rules out a separator pattern that matches nothing at all."""
        with_tables = [
            path for path in self.files
            if any(
                _is_separator(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            )
        ]
        self.assertGreaterEqual(len(with_tables), 5)

    def test_every_table_has_a_blank_line_above_it(self) -> None:
        broken = {
            path.relative_to(REPO_ROOT).as_posix(): lines
            for path in self.files
            if (lines := tables_without_a_blank_line_above(path))
        }
        self.assertEqual(
            {}, broken,
            "these tables will print as literal pipes on GitHub; insert a "
            f"blank line above each: {broken}",
        )


if __name__ == "__main__":
    unittest.main()
