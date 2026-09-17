"""Telling whether a library has changed, cheaply.

A full comparison takes minutes. These answer the much smaller question of
whether one is worth starting, so a quiet library costs almost nothing.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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


@dataclass(frozen=True)
class Fingerprint:
    """What one look at a library saw.

    ``value`` answers "did anything change". ``people`` answers "who was it
    about", which is what lets a sync look at one person instead of all of
    them. A face whose tag is not a person is in ``value`` and in nobody's
    entry, so a change there reads as "changed, attributable to no one" and
    the caller falls back to looking at everything.
    """

    value: str
    people: dict[str, str] = field(default_factory=dict)


def read_digikam(database: str | Path) -> Fingerprint:
    """Read both answers in one pass over the face regions.

    Reads two indexed columns per face region, so a library of half a million
    faces stays well under a second.
    """
    uri = f"file:{Path(database).resolve()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute("PRAGMA query_only = ON")
        names = {
            int(tag_id): str(name)
            for tag_id, name in connection.execute(
                """SELECT t.id, t.name FROM Tags t JOIN TagProperties tp
                     ON tp.tagid = t.id AND tp.property = 'person'"""
            )
        }
        regions = hashlib.sha256()
        per_person: dict[str, Any] = {}
        count = 0
        for image_id, tag_id, value in connection.execute(
            """SELECT imageid, tagid, value FROM ImageTagProperties
               WHERE property = ? ORDER BY imageid, tagid, value""",
            (TAG_REGION_PROPERTY,),
        ):
            count += 1
            line = f"{image_id}:{tag_id}:{value}\n".encode()
            regions.update(line)
            person = names.get(int(tag_id))
            if person is None:
                continue
            digest = per_person.get(person)
            if digest is None:
                digest = per_person[person] = hashlib.sha256()
            digest.update(line)

        people = hashlib.sha256()
        for tag_id, name, value in connection.execute(
            """SELECT t.id, t.name, COALESCE(tp.value, '')
               FROM Tags t JOIN TagProperties tp
                 ON tp.tagid = t.id AND tp.property = 'person'
               ORDER BY t.id"""
        ):
            people.update(f"{tag_id}:{name}:{value}\n".encode())

    return Fingerprint(
        value=f"{regions.hexdigest()[:32]}:{people.hexdigest()[:32]}:{count}",
        people={person: digest.hexdigest()[:32] for person, digest in per_person.items()},
    )


def digikam_fingerprint(database: str | Path) -> str:
    """A value that changes when any face region or person name changes."""
    return read_digikam(database).value


def digikam_changed(database: str | Path, known: str | None) -> tuple[bool, str]:
    """Whether the library differs from a remembered fingerprint."""
    current = digikam_fingerprint(database)
    return current != (known or ""), current


def _by_key(mapping: Any) -> dict[str, tuple[str, str]]:
    """Index a per-person map by a name that survives a change of case."""
    indexed: dict[str, tuple[str, str]] = {}
    if isinstance(mapping, dict):
        for name, digest in mapping.items():
            indexed[str(name).strip().lower()] = (str(name), str(digest))
    return indexed


def changed_people(before: Any, after: Any) -> set[str]:
    """Who differs between two per-person maps.

    A person who gained, lost or moved a face differs. So does one who has
    appeared or disappeared, which is what a rename looks like from here: it
    shows up as two people, the old name and the new one. The name returned is
    how the library spells it now, because that is what a scoped run has to
    look for.
    """
    first = _by_key(before)
    second = _by_key(after)
    names: set[str] = set()
    for key in set(first) | set(second):
        was = first.get(key)
        now = second.get(key)
        if (was[1] if was else None) == (now[1] if now else None):
            continue
        names.add((now or was)[0])
    return names
