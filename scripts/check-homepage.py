#!/usr/bin/env python3
"""Fail closed when the standalone GitHub Pages homepage drifts."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
WEBSITE = ROOT / "website"
EXPECTED_CANONICAL = "https://hullwork.github.io/site/"


class References(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.references: list[str] = []
        self.canonical: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag in {"a", "link"} and values.get("href"):
            self.references.append(values["href"] or "")
        if tag in {"img", "script"} and values.get("src"):
            self.references.append(values["src"] or "")
        if tag == "link" and values.get("rel") == "canonical":
            self.canonical = values.get("href")


def validate_page(page: Path) -> None:
    parser = References()
    parser.feed(page.read_text(encoding="utf-8"))
    if parser.canonical != EXPECTED_CANONICAL:
        raise SystemExit(f"{page}: canonical must be {EXPECTED_CANONICAL}")

    for reference in parser.references:
        parsed = urlsplit(reference)
        if parsed.scheme or parsed.netloc or reference.startswith(("#", "mailto:")):
            continue
        relative = parsed.path.removeprefix("./")
        target = (page.parent / relative).resolve()
        if not target.is_relative_to(WEBSITE.resolve()) or not target.exists():
            raise SystemExit(f"{page}: broken local reference {reference}")


def main() -> None:
    required = {
        "index.html",
        "404.html",
        "styles.css",
        "app.js",
        "mark.svg",
        "og.svg",
        "robots.txt",
        "sitemap.xml",
    }
    missing = sorted(name for name in required if not (WEBSITE / name).is_file())
    if missing:
        raise SystemExit(f"homepage is missing: {', '.join(missing)}")

    validate_page(WEBSITE / "index.html")
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in WEBSITE.rglob("*")
        if path.is_file()
    ).lower()
    if "kind" in source:
        raise SystemExit("homepage must remain kubeadm-only; found forbidden 'kind'")
    for proof in ("kubeadm", "http 200", "github.com/hullwork/site"):
        if proof not in source:
            raise SystemExit(f"homepage is missing required proof: {proof}")

    print("homepage contract passed")


if __name__ == "__main__":
    main()
