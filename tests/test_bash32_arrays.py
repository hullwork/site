"""Empty arrays must expand under `set -u` on bash 3.2.

macOS still ships bash 3.2.57 -- Apple froze it at the last GPLv2 release --
and every setup path in this repository starts with a shell script. On bash 3.2
and 4.3, `"${arr[@]}"` on an empty array under `set -u` is an unbound variable
error; bash 4.4 changed that. Verified on all three, in containers:

    3.2.57  "${empty[@]}"                 -> empty[@]: unbound variable
    4.3.48  "${empty[@]}"                 -> empty[@]: unbound variable
    5.2.21  "${empty[@]}"                 -> ok
    3.2/4.3/5.2  ${empty[@]+"${empty[@]}"} -> ok, and elements containing
                                              spaces still arrive unsplit

Every developer on Linux sees the scripts work. Every developer on a Mac sees
them abort. site/scripts/cluster.sh failed at the `helm` line of `up()` --
its two optional argument arrays are empty unless you set an image override
or a kube context, which is exactly the default path a newcomer takes.

The `warnings`/`problems` arrays in the preflight scripts are worse still:
empty is the *success* case, so preflight crashed precisely when it had
nothing to report.

What this checks: an array initialised `name=()` in a script that sets `-u`,
expanded as `"${name[@]}"` without the `${name[@]+...}` guard.

What it does not check: arrays that reach empty some other way (`set --`,
`unset`, a `local -a` declaration, an array built in a sourced file). Those
are real, and a script can still be broken on 3.2 with this test green. It
catches the shape all 26 occurrences in this repository had.
"""
from __future__ import annotations

import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

#: `name=()` alone on a line, optionally `local`/`declare`-qualified.
EMPTY_INIT = re.compile(
    r"^[ \t]*(?:local |declare |typeset )?([A-Za-z_][A-Za-z0-9_]*)=\(\)[ \t]*$",
    re.MULTILINE,
)
#: `set -u`, `set -eu`, `set -euo pipefail`, `set -o nounset`.
NOUNSET = re.compile(r"^[ \t]*set[ \t]+(?:-[a-z]*u[a-z]*\b|-o[ \t]+nounset\b)",
                     re.MULTILINE)


def unguarded(text: str, name: str) -> list[int]:
    """Line numbers where `name` is expanded without the `+` guard.

    The guarded form contains the unguarded one as a substring, so this looks
    at what precedes the match rather than grepping for the bare text -- the
    first version of this check reported all 26 sites as still broken after
    they were fixed, which is the same answer it gives for a script nobody
    touched.
    """
    plain = f'"${{{name}[@]}}"'
    guard = f'${{{name}[@]+'
    hits = []
    for number, line in enumerate(text.splitlines(), start=1):
        start = 0
        while True:
            at = line.find(plain, start)
            if at < 0:
                break
            if guard not in line[max(0, at - len(guard) - 2):at + 1]:
                hits.append(number)
            start = at + 1
    return hits


class TheCheckItself(unittest.TestCase):
    """A scanner nobody scans reports a clean repository either way.

    Each of these pins one way this file has already been wrong, or could be:
    a `guard` substring test that matched the fixed form too, a regex that
    missed `local name=()`, and a `set -u` detector that missed the spelling
    the scripts actually use.
    """

    def test_the_plain_form_is_reported(self) -> None:
        self.assertEqual([2], unguarded('x\nhelm "${a[@]}" up\n', "a"))

    def test_the_guarded_form_is_not(self) -> None:
        # The guarded form contains the plain one, so a naive `in` test calls
        # every fixed line broken and every broken line broken alike.
        self.assertEqual([], unguarded('helm ${a[@]+"${a[@]}"} up\n', "a"))

    def test_a_guard_for_another_array_does_not_cover_this_one(self) -> None:
        line = 'helm ${b[@]+"${b[@]}"} "${a[@]}"\n'
        self.assertEqual([1], unguarded(line, "a"))
        self.assertEqual([], unguarded(line, "b"))

    def test_empty_init_finds_the_qualified_forms(self) -> None:
        # `local name=()` is how two of the occurrences in this repository
        # were written, and the first scan that missed it reported them clean.
        for line in ("a=()", "  a=()", "local a=()", "  declare a=()",
                     "\ttypeset a=()"):
            with self.subTest(line=line):
                self.assertEqual(["a"], EMPTY_INIT.findall(line + "\n"), line)
        self.assertEqual([], EMPTY_INIT.findall("a=(1)\n"))
        self.assertEqual([], EMPTY_INIT.findall("echo a=()\n"))

    def test_nounset_spellings(self) -> None:
        for line in ("set -u", "set -eu", "set -euo pipefail", "set -o nounset",
                     "  set -euo pipefail"):
            with self.subTest(line=line):
                self.assertTrue(NOUNSET.search(line + "\n"), line)
        for line in ("set -e", "set -o pipefail", "# set -u"):
            with self.subTest(line=line):
                self.assertIsNone(NOUNSET.search(line + "\n"), line)


class Bash32EmptyArrays(unittest.TestCase):
    def test_no_unguarded_empty_array_expansion(self) -> None:
        scripts = sorted(
            p for p in REPO_ROOT.rglob("*.sh")
            if ".git" not in p.parts and "node_modules" not in p.parts
        )
        self.assertTrue(scripts, "no shell scripts found; check REPO_ROOT")

        broken: list[str] = []
        checked = 0
        for script in scripts:
            text = script.read_text(encoding="utf-8", errors="replace")
            if not NOUNSET.search(text):
                continue
            checked += 1
            for name in sorted(set(EMPTY_INIT.findall(text))):
                for line in unguarded(text, name):
                    broken.append(
                        f"{script.relative_to(REPO_ROOT)}:{line}: "
                        f'"${{{name}[@]}}" aborts on bash 3.2 when {name} is '
                        f'empty; write ${{{name}[@]+"${{{name}[@]}}"}}'
                    )
        self.assertTrue(checked, "no script sets -u; check NOUNSET")
        self.assertEqual([], broken, "\n" + "\n".join(broken))


if __name__ == "__main__":
    unittest.main()
