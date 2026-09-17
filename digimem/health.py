"""Problems in the digiKam library itself, rather than between the libraries.

A sync can only reconcile what it is given. A face box drawn in the wrong place
is not a disagreement to settle; it is a mistake, and syncing it faithfully
copies the mistake or, worse, proposes it on every run and has it refused every
time, for ever.

These checks look for the two shapes that produce that. Both are suspicions
rather than verdicts, so nothing here changes anything by itself.
"""
from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from contextlib import closing
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .constants import TAG_REGION_PROPERTY
from .geometry import parse_tag_region
from .models import Rect

LOG = logging.getLogger(__name__)

# One person has one face in a photograph. Twice means one of them is wrong,
# or is a reflection or a picture within the picture, which is why it is asked
# about rather than acted on.
PERSON_TWICE = "person_twice"
# A face box almost wholly inside a different person's box. Faces do not nest.
NESTED_BOX = "nested_box"

# How much of the smaller box has to be swallowed before it is worth asking.
NESTED_COVERAGE = 0.8


@dataclass
class Box:
    """One face rectangle, as drawn."""

    person: str
    rect: list[float]
    pixels: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Issue:
    kind: str
    path: str
    image_id: int
    person: str
    boxes: list[Box] = field(default_factory=list)
    other_person: str = ""

    @property
    def identity(self) -> str:
        """What makes this the same issue on a later scan.

        Keyed by who it is about rather than by where the boxes are, so
        dismissing it stays dismissed when a box is nudged. Fixing it removes
        a box, which makes the issue stop being reported at all.
        """
        return f"{self.kind}:{self.path}:{self.person.lower()}:{self.other_person.lower()}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": self.path,
            "image_id": self.image_id,
            "person": self.person,
            "other_person": self.other_person,
            "boxes": [box.to_dict() for box in self.boxes],
            "identity": self.identity,
        }


def covered(outer: Rect, inner: Rect) -> float:
    """How much of ``inner`` lies within ``outer``, from 0 to 1."""
    x1, y1 = max(outer.x, inner.x), max(outer.y, inner.y)
    x2 = min(outer.x + outer.w, inner.x + inner.w)
    y2 = min(outer.y + outer.h, inner.y + inner.h)
    overlap = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    area = inner.w * inner.h
    return overlap / area if area > 0 else 0.0


def _faces_by_image(connection: sqlite3.Connection) -> dict[int, list[tuple[str, Rect, str]]]:
    connection.row_factory = sqlite3.Row
    images = {
        int(row["imageid"]): row
        for row in connection.execute(
            "SELECT imageid, width, height, orientation FROM ImageInformation"
        )
    }
    people = {
        int(row["tagid"]): str(row["value"])
        for row in connection.execute(
            "SELECT tagid, value FROM TagProperties WHERE property = 'person'"
        )
    }
    grouped: dict[int, list[tuple[str, Rect, str]]] = defaultdict(list)
    for row in connection.execute(
        "SELECT imageid, tagid, value FROM ImageTagProperties WHERE property = ?",
        (TAG_REGION_PROPERTY,),
    ):
        person = people.get(int(row["tagid"]))
        image = images.get(int(row["imageid"]))
        if person is None or image is None:
            continue
        rect = parse_tag_region(
            str(row["value"]),
            int(image["width"]),
            int(image["height"]),
            int(image["orientation"]),
        )
        if rect is not None:
            grouped[int(row["imageid"])].append((person, rect, str(row["value"])))
    return grouped


def _paths(connection: sqlite3.Connection) -> dict[int, str]:
    return {
        int(row["id"]): str(row["path"]).lstrip("/")
        for row in connection.execute(
            """SELECT i.id, a.relativePath || '/' || i.name AS path
               FROM Images i JOIN Albums a ON a.id = i.album
               WHERE i.status = 1"""
        )
    }


def scan(database: str | Path) -> list[Issue]:
    """Look over every named face in the library. Reads only."""
    uri = f"file:{Path(database).resolve()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute("PRAGMA query_only = ON")
        grouped = _faces_by_image(connection)
        paths = _paths(connection)

    issues: list[Issue] = []
    for image_id, faces in grouped.items():
        path = paths.get(image_id)
        if path is None:
            continue
        issues.extend(_issues_for(image_id, path, faces))
    issues.sort(key=lambda issue: (issue.path, issue.person))
    LOG.info("Library check: %s issue(s) across %s photos", len(issues), len(grouped))
    return issues


def _issues_for(
    image_id: int, path: str, faces: list[tuple[str, Rect, str]]
) -> Iterable[Issue]:
    by_person: dict[str, list[tuple[str, Rect, str]]] = defaultdict(list)
    for person, rect, pixels in faces:
        by_person[person.strip().lower()].append((person, rect, pixels))

    for entries in by_person.values():
        if len(entries) > 1:
            yield Issue(
                kind=PERSON_TWICE,
                path=path,
                image_id=image_id,
                person=entries[0][0],
                boxes=[
                    Box(person=person, rect=list(rect.as_tuple()), pixels=pixels)
                    for person, rect, pixels in entries
                ],
            )

    seen: set[tuple[str, str]] = set()
    for person, rect, pixels in faces:
        for other, other_rect, other_pixels in faces:
            if other.strip().lower() == person.strip().lower():
                continue
            if covered(other_rect, rect) < NESTED_COVERAGE:
                continue
            key = (person.strip().lower(), other.strip().lower())
            if key in seen:
                continue
            seen.add(key)
            yield Issue(
                kind=NESTED_BOX,
                path=path,
                image_id=image_id,
                person=person,
                other_person=other,
                boxes=[
                    Box(person=person, rect=list(rect.as_tuple()), pixels=pixels),
                    Box(
                        person=other,
                        rect=list(other_rect.as_tuple()),
                        pixels=other_pixels,
                    ),
                ],
            )
