#!/usr/bin/env python3
"""Render backlog.md into the notes for one GitHub release.

Relative links are rewritten to point at the tag rather than the repository
root. A release is a fixed thing; its links should keep working after the files
they name have moved on.
"""
from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import changelog  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "backlog.md"
INFO_XML = ROOT / "nextcloud-app" / "digikam_face_sync" / "appinfo" / "info.xml"
REPO = "keithvassallomt/digikam-memories-sync"

#: A markdown link whose target is a path in this repository.
RELATIVE_LINK = re.compile(r"\]\((?!https?://|#|mailto:)([^)]+)\)")


def nextcloud_version() -> str:
    return ET.parse(INFO_XML).getroot().findtext("version") or "0.0.0"


def absolute_links(text: str, tag: str) -> str:
    base = f"https://github.com/{REPO}/blob/{tag}/"
    return RELATIVE_LINK.sub(lambda m: f"]({base}{m.group(1)})", text)


def render(version: str) -> str:
    entry = changelog.find(version)
    if entry is None:
        raise SystemExit(f"CHANGELOG.md has no section for {version}")
    body = entry[2]
    text = TEMPLATE.read_text(encoding="utf-8")
    text = text.replace("{{CHANGELOG}}", body)
    text = text.replace("{{NEXTCLOUD_VERSION}}", nextcloud_version())
    text = text.replace("{{VERSION}}", version)
    return absolute_links(text, f"v{version}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("-o", "--output", type=Path)
    args = parser.parse_args(argv)

    notes = render(args.version)
    if args.output:
        args.output.write_text(notes, encoding="utf-8")
        print(f"wrote {args.output}")
    else:
        print(notes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
