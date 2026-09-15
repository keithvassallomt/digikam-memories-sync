"""Telling whether a library has changed, cheaply.

A full comparison takes minutes. These answer the much smaller question of
whether one is worth starting, so a quiet library costs almost nothing.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
from contextlib import closing
from pathlib import Path

from .constants import TAG_REGION_PROPERTY

LOG = logging.getLogger(__name__)

# The files SQLite writes through. A change lands in one of them.
SIDECARS = ("", "-wal", "-journal")


def source_mtime(database: str | Path) -> float:
    """The newest modification time across the database and its write-ahead log.

    Used as a pre-check: if this has not moved, there is nothing to hash.
    """
    newest = 0.0
    for suffix in SIDECARS:
        path = Path(f"{database}{suffix}")
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    return newest


def digikam_fingerprint(database: str | Path) -> str:
    """A value that changes when any face region or person name changes.

    Reads two indexed columns per face region, so a library of half a million
    faces stays well under a second.
    """
    uri = f"file:{Path(database).resolve()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute("PRAGMA query_only = ON")
        regions = hashlib.sha256()
        count = 0
        for image_id, tag_id, value in connection.execute(
            """SELECT imageid, tagid, value FROM ImageTagProperties
               WHERE property = ? ORDER BY imageid, tagid, value""",
            (TAG_REGION_PROPERTY,),
        ):
            count += 1
            regions.update(f"{image_id}:{tag_id}:{value}\n".encode())

        people = hashlib.sha256()
        for tag_id, name, value in connection.execute(
            """SELECT t.id, t.name, COALESCE(tp.value, '')
               FROM Tags t JOIN TagProperties tp
                 ON tp.tagid = t.id AND tp.property = 'person'
               ORDER BY t.id"""
        ):
            people.update(f"{tag_id}:{name}:{value}\n".encode())

    return f"{regions.hexdigest()[:32]}:{people.hexdigest()[:32]}:{count}"


def digikam_changed(database: str | Path, known: str | None) -> tuple[bool, str]:
    """Whether the library differs from a remembered fingerprint."""
    current = digikam_fingerprint(database)
    return current != (known or ""), current
