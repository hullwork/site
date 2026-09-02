#!/usr/bin/env python3
"""Prepare and verify a digest-pinned site release Chart."""
from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

import yaml


DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def load_image(path: Path) -> dict[str, str]:
    value = json.loads(path.read_text(encoding="utf-8"))
    required = ("repository", "digest", "valuePath")
    if any(not isinstance(value.get(key), str) or not value[key] for key in required):
        raise ValueError(f"{path}: release image metadata requires {', '.join(required)}")
    if value["valuePath"] != "images.control":
        raise ValueError(f"{path}: image valuePath must be images.control")
    if not DIGEST.fullmatch(value["digest"]):
        raise ValueError(f"{path}: invalid control image digest")
    return value


def prepare(args: argparse.Namespace) -> None:
    image = load_image(args.image_json)
    if args.output_chart.exists():
        raise ValueError(f"refusing to overwrite existing output: {args.output_chart}")
    shutil.copytree(args.chart, args.output_chart)
    values_path = args.output_chart / "values.yaml"
    values = yaml.safe_load(values_path.read_text(encoding="utf-8"))
    values["images"]["control"]["repository"] = image["repository"]
    values["images"]["control"]["digest"] = image["digest"]
    values_path.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")

    pinned_values = {
        "images": {
            "control": {
                "repository": image["repository"],
                "digest": image["digest"],
            }
        }
    }
    args.values_output.write_text(
        yaml.safe_dump(pinned_values, sort_keys=False), encoding="utf-8"
    )


def verify(args: argparse.Namespace) -> None:
    image = load_image(args.image_json)
    expected = f"{image['repository']}@{image['digest']}"
    found: list[str] = []
    for document in yaml.safe_load_all(args.rendered.read_text(encoding="utf-8")):
        if not isinstance(document, dict):
            continue
        template = document.get("spec", {}).get("template", {}).get("spec", {})
        for container in [*template.get("initContainers", []), *template.get("containers", [])]:
            candidate = container.get("image")
            if isinstance(candidate, str) and candidate.startswith(image["repository"]):
                found.append(candidate)
    if not found:
        raise ValueError("rendered Chart contains no control image")
    invalid = [candidate for candidate in found if candidate != expected]
    if invalid:
        raise ValueError(f"rendered control images are not digest-pinned: {invalid}")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--chart", type=Path, required=True)
    prepare_parser.add_argument("--image-json", type=Path, required=True)
    prepare_parser.add_argument("--output-chart", type=Path, required=True)
    prepare_parser.add_argument("--values-output", type=Path, required=True)
    prepare_parser.set_defaults(handler=prepare)
    verify_parser = commands.add_parser("verify")
    verify_parser.add_argument("--rendered", type=Path, required=True)
    verify_parser.add_argument("--image-json", type=Path, required=True)
    verify_parser.set_defaults(handler=verify)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        args.handler(args)
    except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError) as exc:
        print(f"release chart error: {exc}", file=__import__("sys").stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
