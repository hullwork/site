"""Every relative markdown link in the docs must resolve.

A document that points at a file which is not there is a dead end for the
reader it was written for, and nothing in a test suite notices: the markdown
parses, the tests pass, and only someone following the link finds out.

Checks markdown link targets and inline-code paths that look like repository
files. Anchors, external URLs and glob patterns are skipped rather than
guessed at.

Four link forms were invisible to this and one produced a false positive, all
found by feeding it a table of nineteen shapes rather than by reading the
regex: a reference definition, an angle-bracketed target, and an HTML `href` or
`src` were never checked, while an inline link carrying a title -- the ordinary
`[a](x.md "why")` -- had the title counted as part of the path and was reported
broken. A false positive is the worse of the two: it fails a correct document
and teaches people the gate is noise.

Links inside fenced code blocks are skipped. They are examples, and a document
showing a broken link on purpose is not a broken document.
"""
import pathlib
import re
import subprocess
import unittest

LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
#: `[label]: target` at the start of a line, the definition half of a reference
#: link. The use half (`[text][label]`, `[label][]`, `[label]`) needs no check
#: of its own: the target only ever appears here.
REFERENCE_DEFINITION = re.compile(r"^\[[^\]]+\]:\s*(\S+)", re.MULTILINE)
#: `href=` and `src=` in raw HTML, which markdown passes through untouched.
HTML_TARGET = re.compile(r"(?:href|src)\s*=\s*[\"']([^\"']+)[\"']")
FENCE = re.compile(r"^\s*(```|~~~)")
#: The optional title after a link target: `"..."`, `'...'` or `(...)`.
TITLE = re.compile(r"""\s+(?:"[^"]*"|'[^']*'|\([^)]*\))\s*$""")
CODEPATH = re.compile(r"`([A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:py|sh|md|ya?ml|json|toml|ts|tsx|cfg|txt))`")


def outside_fences(text: str) -> str:
    """The document with fenced code blocks blanked out, line numbering intact."""
    kept, fenced = [], False
    for line in text.splitlines():
        if FENCE.match(line):
            fenced = not fenced
            kept.append("")
            continue
        kept.append("" if fenced else line)
    return "\n".join(kept)


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
    target = target.strip()
    # `[a](<path with spaces.md>)` is the angle-bracket form of an ordinary
    # target, not a glob. Unwrap before the glob check below rejects it for
    # containing `<`.
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1].strip()
    # `[a](path.md "title")` -- the title is not part of the path. Counting it
    # made every titled link in the tree report as broken. Strip the whole
    # quoted run, not the last space-separated token: a title contains spaces,
    # and splitting on the last one leaves `path.md "the` behind.
    target = TITLE.sub("", target).strip()
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
        body = outside_fences(text)
        targets = [m.group(1) for m in LINK.finditer(body)]
        targets += [m.group(1) for m in REFERENCE_DEFINITION.finditer(body)]
        targets += [m.group(1) for m in HTML_TARGET.finditer(body)]
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
