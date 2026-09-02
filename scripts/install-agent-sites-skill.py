#!/usr/bin/env python3
"""Install or verify site' canonical Sites skill in a agent checkout."""

from __future__ import annotations

import argparse
import filecmp
import shutil
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "skills" / "sites"
AGENT_VERSION = "6"


def target_skill_text(source: Path) -> str:
    text = (source / "SKILL.md").read_text(encoding="utf-8")
    frontmatter_end = text.index("\n---\n", 4)
    return (
        text[:frontmatter_end]
        + f"\nversion: {AGENT_VERSION}"
        + text[frontmatter_end:]
    )


def trees_match(source: Path, target: Path, relative: Path = Path()) -> bool:
    if not target.is_dir():
        return False
    comparison = filecmp.dircmp(source, target)
    if comparison.left_only or comparison.right_only or comparison.funny_files:
        return False
    for name in comparison.common_files:
        source_file = source / name
        target_file = target / name
        if relative / name == Path("SKILL.md"):
            if target_file.read_text(encoding="utf-8") != target_skill_text(SOURCE):
                return False
        elif not filecmp.cmp(source_file, target_file, shallow=False):
            return False
    return all(
        trees_match(source / name, target / name, relative / name)
        for name in comparison.common_dirs
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("agent_root", type=Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    target = args.agent_root.resolve() / "builtin_skills" / "sites"
    if args.check:
        if trees_match(SOURCE, target):
            print(f"Sites skill is synchronized: {target}")
            return 0
        print(f"Sites skill differs: {target}")
        return 1

    if target.exists() and not args.force:
        parser.error(f"target exists: {target}; pass --force to replace only this skill directory")
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SOURCE, target)
    (target / "SKILL.md").write_text(target_skill_text(SOURCE), encoding="utf-8")
    print(f"Installed Sites skill: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
