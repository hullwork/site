"""Tables that GitHub will not render as tables.

The first version of this gate required a blank line above every table, and a
second version required one below. Both were wrong: rendering the same file
through GitHub's own markdown API with and without those blank lines produces
identical HTML. The premise was never checked against the renderer, only
against the assumption - the "fix" was verified by rendering the corrected file
and finding seven tables, without ever rendering the uncorrected one, which
also has seven.

Probed against the API, exactly two shapes fail: a table whose first line
directly follows a list item, and one that directly follows a block quote.
Both render as literal pipes. A table after a heading or an ordinary paragraph
is fine.
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SEPARATOR = re.compile(r"^\|[ \-:|]+\|?$")
LIST_ITEM = re.compile(r"^\s*([-*+]|\d+\.)\s")
BLOCK_QUOTE = re.compile(r"^\s*>")


def tracked_markdown() -> list[pathlib.Path]:
    """``check=True`` so a broken git fails this rather than passing it."""
    listing = subprocess.run(
        ["git", "ls-files", "-z", "*.md"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    return [REPO_ROOT / name for name in listing.split("\0") if name]


def tables_that_will_not_render(path: pathlib.Path) -> list[int]:
    lines = path.read_text(encoding="utf-8").splitlines()
    found, fenced = [], False
    for index, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced or not index:
            continue
        following = lines[index + 1] if index + 1 < len(lines) else ""
        if not (line.strip().startswith("|") and SEPARATOR.match(following.strip())):
            continue
        previous = lines[index - 1]
        if LIST_ITEM.match(previous) or BLOCK_QUOTE.match(previous):
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
                SEPARATOR.match(line.strip())
                for line in path.read_text(encoding="utf-8").splitlines()
            )
        ]
        self.assertGreaterEqual(len(with_tables), 5)

    def test_the_checker_flags_the_shapes_that_fail(self) -> None:
        """The two shapes were confirmed against GitHub's renderer, not guessed."""
        self.assertEqual([2], _flag("- item\n| a | b |\n| --- | --- |\n| 1 | 2 |"))
        self.assertEqual([2], _flag("> quote\n| a | b |\n| --- | --- |\n| 1 | 2 |"))
        self.assertEqual([], _flag("## H\n| a | b |\n| --- | --- |\n| 1 | 2 |"))
        self.assertEqual([], _flag("text\n| a | b |\n| --- | --- |\n| 1 | 2 |"))

    def test_no_table_follows_a_list_item_or_a_quote(self) -> None:
        broken = {
            path.relative_to(REPO_ROOT).as_posix(): lines
            for path in self.files
            if (lines := tables_that_will_not_render(path))
        }
        self.assertEqual(
            {}, broken,
            "a table directly after a list item or a block quote renders as "
            f"literal pipes; insert a blank line: {broken}",
        )


def _flag(text: str) -> list[int]:
    """Run the detector over a literal document, for the self-test above."""
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
        handle.write(text)
        temporary = pathlib.Path(handle.name)
    try:
        return tables_that_will_not_render(temporary)
    finally:
        temporary.unlink()


if __name__ == "__main__":
    unittest.main()
