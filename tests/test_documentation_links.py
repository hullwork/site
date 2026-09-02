"""Every relative markdown link in the docs must resolve.

A document that points at a file which is not there is a dead end for the
reader it was written for, and nothing in a test suite notices: the markdown
parses, the tests pass, and only someone following the link finds out.

Checks markdown link targets and inline-code paths that look like repository
files. Anchors, external URLs and glob patterns are skipped rather than
guessed at.
"""
import pathlib
import re
import subprocess
import unittest

LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
CODEPATH = re.compile(r"`([A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:py|sh|md|ya?ml|json|toml|ts|tsx|cfg|txt))`")


def tracked(root: pathlib.Path) -> set[str]:
    listing = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, text=True, check=True
    ).stdout
    files = {f for f in listing.split("\0") if f}
    dirs = set()
    for name in files:
        parts = pathlib.PurePosixPath(name).parts
        for i in range(1, len(parts)):
            dirs.add("/".join(parts[:i]))
    return files | dirs


def resolve(root: pathlib.Path, source: str, target: str) -> str | None:
    if target.startswith(("http://", "https://", "mailto:", "#")):
        return None
    target = target.split("#", 1)[0].strip()
    if not target or any(c in target for c in "*{}$<>"):
        return None
    if target.startswith("/"):
        return target.lstrip("/")
    joined = pathlib.PurePosixPath(pathlib.PurePosixPath(source).parent, target)
    # PurePosixPath keeps ".." as a component, so a link written relative to a
    # subdirectory would never match a tracked path. Fold them by hand.
    parts: list[str] = []
    for part in joined.parts:
        if part == "..":
            if parts:
                parts.pop()
        elif part not in (".", ""):
            parts.append(part)
    return "/".join(parts)


def check(root: pathlib.Path) -> tuple[int, list[str]]:
    known = tracked(root)
    checked, broken = 0, []
    for name in sorted(f for f in known if f.endswith(".md")):
        try:
            text = (root / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        # Only real markdown links. An inline-code filename is prose naming a
        # file, often one in another repository or one that has since moved on
        # purpose; treating those as links reported 513 "broken" references
        # across five repositories, almost all of them CHANGELOG entries.
        targets = [m.group(1) for m in LINK.finditer(text)]
        for raw in targets:
            rel = resolve(root, name, raw)
            if rel is None:
                continue
            normalised = str(pathlib.PurePosixPath(rel))
            checked += 1
            if normalised in known or (root / normalised).exists():
                continue
            broken.append(f"{name} -> {raw}")
    return checked, broken



REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


class DocumentationLinkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.checked, self.broken = check(REPO_ROOT)

    def test_there_are_links_to_check(self) -> None:
        """Zero broken links out of zero links checked is not a passing gate."""
        # Floor set below the current count for this repository. It is here so
        # that a scanner which silently stops finding links cannot report a
        # clean tree; it is not a target.
        self.assertGreater(self.checked, 40)

    def test_the_resolver_handles_relative_paths(self) -> None:
        """A resolver that returned None for everything would report nothing."""
        self.assertEqual("README.md", resolve(REPO_ROOT, "docs/a.md", "../README.md"))
        self.assertEqual("docs/b.md", resolve(REPO_ROOT, "docs/a.md", "b.md"))
        self.assertIsNone(resolve(REPO_ROOT, "docs/a.md", "https://example.com"))

    def test_every_link_resolves(self) -> None:
        self.assertEqual(
            [], self.broken,
            "these documentation links point at paths that are not in the "
            f"repository: {self.broken}",
        )


if __name__ == "__main__":
    unittest.main()
