#!/usr/bin/env python3
"""Write a new version into the one place each component keeps it.

DigiMem's number lives in ``digimem/__init__.py`` and nowhere else —
``pyproject.toml`` reads it from there — so there is no second copy to drift.
The Nextcloud app keeps its own, because its number is a compatibility promise
to the server rather than a marketing one, and the two move independently.

``SETTINGS_VERSION`` and ``STATE_VERSION`` are deliberately untouched. They are
schema versions: bumping them with a release would trigger a migration on every
user's machine for no reason.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INIT = ROOT / "digimem" / "__init__.py"
INFO_XML = ROOT / "nextcloud-app" / "digikam_face_sync" / "appinfo" / "info.xml"


def set_app_version(version: str) -> str:
    text = INIT.read_text(encoding="utf-8")
    new, count = re.subn(
        r'^__version__ = ".*"$', f'__version__ = "{version}"', text, flags=re.M
    )
    if count != 1:
        raise SystemExit(f"expected one __version__ line in {INIT}, found {count}")
    INIT.write_text(new, encoding="utf-8")
    return version


def set_nextcloud_version(version: str) -> str:
    text = INFO_XML.read_text(encoding="utf-8")
    new, count = re.subn(
        r"<version>[^<]*</version>", f"<version>{version}</version>", text, count=1
    )
    if count != 1:
        raise SystemExit(f"expected a <version> element in {INFO_XML}")
    INFO_XML.write_text(new, encoding="utf-8")
    return version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="the new DigiMem version")
    parser.add_argument("--nextcloud", help="the new Nextcloud app version")
    args = parser.parse_args(argv)

    print(f"digimem/__init__.py      -> {set_app_version(args.version)}")
    if args.nextcloud:
        print(f"appinfo/info.xml         -> {set_nextcloud_version(args.nextcloud)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
