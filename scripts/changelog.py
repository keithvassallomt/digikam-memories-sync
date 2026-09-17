#!/usr/bin/env python3
"""Read CHANGELOG.md, so a release cannot be cut without an entry for it.

A release whose notes say nothing is worse than no release: people cannot tell
what changed, and nothing later can reconstruct it. `just release` refuses
rather than inventing something.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"

#: ``## [1.2.3] - 2026-09-17``, the Keep a Changelog release heading.
HEADING = re.compile(r"^##\s+\[(?P<version>[^\]]+)\]\s*(?:-\s*(?P<date>\S+))?\s*$")


def sections(text: str) -> list[tuple[str, str | None, str]]:
    """Every release heading in the file, with its body."""
    found: list[tuple[str, str | None, str]] = []
    version = date = None
    body: list[str] = []
    for line in text.splitlines():
        match = HEADING.match(line)
        if match:
            if version is not None:
                found.append((version, date, "\n".join(body).strip()))
            version, date, body = match["version"], match["date"], []
            continue
        if version is not None:
            # Link definitions at the foot belong to the file, not the release.
            if re.match(r"^\[[^\]]+\]:\s+\S+", line):
                continue
            body.append(line)
    if version is not None:
        found.append((version, date, "\n".join(body).strip()))
    return found


def find(version: str) -> tuple[str, str | None, str] | None:
    for entry in sections(CHANGELOG.read_text(encoding="utf-8")):
        if entry[0] == version:
            return entry
    return None


def released() -> list[str]:
    """Released versions, newest first, ignoring Unreleased."""
    return [v for v, _, _ in sections(CHANGELOG.read_text(encoding="utf-8"))
            if v.lower() != "unreleased"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "extract"):
        one = sub.add_parser(name)
        one.add_argument("version")
    sub.add_parser("latest")

    args = parser.parse_args(argv)

    if args.command == "latest":
        versions = released()
        if not versions:
            print("no released version in CHANGELOG.md", file=sys.stderr)
            return 1
        print(versions[0])
        return 0

    entry = find(args.version)
    if entry is None:
        print(
            f"CHANGELOG.md has no '## [{args.version}]' section.\n"
            f"Add one (move the Unreleased notes into it) and try again.",
            file=sys.stderr,
        )
        return 1
    version, date, body = entry
    if not body:
        print(f"The '## [{version}]' section is empty.", file=sys.stderr)
        return 1
    if args.command == "extract":
        print(body)
    else:
        print(f"CHANGELOG.md has {version} ({date or 'no date'}), {len(body)} characters")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
