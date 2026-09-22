"""Careful, idempotent writes to digiKam's SQLite database."""
from __future__ import annotations

import os
import sqlite3
import sys
import time
import uuid
import signal
from pathlib import Path
from typing import Any

try:  # The desktop extra. Linux can manage without it; nothing else can.
    import psutil
except ImportError:  # pragma: no cover - exercised by the no-psutil platforms
    psutil = None  # type: ignore[assignment]

from .geometry import displayed_dimensions, parse_tag_region
from .models import Rect
from .names import person_names_match, sanitize_person_name
from .paths import normalize_path


class DigikamChangedError(RuntimeError):
    """The database no longer matches the preview that the user approved."""


DIGIKAM_PROCESS_NAMES = frozenset({"digikam", "digikam.exe"})

#: Where digiKam parks a rectangle its detector found. A confirmed face is a
#: ``tagRegion`` under the person's own tag, so the two never mix.
DETECTED_FACE_PROPERTY = "autodetectedFace"
#: The tags digiKam holds those rectangles under while nobody has said who
#: they are. ``ignoredPerson`` is deliberately not here: Ignored is a decision.
UNNAMED_FACE_PROPERTIES = ("unknownPerson", "unconfirmedPerson")


def _process_ids_from_proc() -> list[int]:
    """Read /proc directly, so Linux needs no dependency at all."""
    proc = Path("/proc")
    try:
        entries = proc.iterdir()
    except OSError:
        return []
    matches: list[int] = []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "comm").read_text(errors="ignore").strip().lower()
            executable = os.path.basename(
                (entry / "cmdline").read_bytes().split(b"\0", 1)[0].decode(errors="ignore")
            ).lower()
        except (OSError, IndexError):
            continue
        if command in DIGIKAM_PROCESS_NAMES or executable in DIGIKAM_PROCESS_NAMES:
            matches.append(int(entry.name))
    return sorted(matches)


def _process_ids_from_psutil() -> list[int]:
    """The same question asked through psutil, which answers it anywhere."""
    if psutil is None:
        return []
    matches: list[int] = []
    for process in psutil.process_iter(["pid", "name"]):
        try:
            name = (process.info.get("name") or "").lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if name in DIGIKAM_PROCESS_NAMES:
            matches.append(int(process.info["pid"]))
    return sorted(matches)


def digikam_probe_supported() -> bool:
    """Whether this machine can answer "is digiKam open?" at all.

    A machine that cannot must not be told digiKam is closed, because the
    deferral gate would then never fire and writes would land under a running
    digiKam.
    """
    return psutil is not None or sys.platform.startswith("linux")


def digikam_process_ids() -> list[int]:
    """Return matching digiKam process IDs.

    Linux keeps the dependency-free /proc scan. Everywhere else needs psutil,
    and so does a Linux box whose /proc is unreadable.
    """
    if sys.platform.startswith("linux"):
        found = _process_ids_from_proc()
        if found:
            return found
    return _process_ids_from_psutil()


def digikam_is_running() -> bool:
    """Best-effort process check. False also means "could not tell"."""
    return bool(digikam_process_ids())


def _request_exit(process_id: int) -> None:
    """Ask one process to quit, without escalating to a forced kill."""
    if sys.platform == "win32":
        if psutil is None:  # pragma: no cover - guarded by the caller
            raise RuntimeError("Closing digiKam needs psutil on this platform.")
        try:
            psutil.Process(process_id).terminate()
        except psutil.NoSuchProcess as error:
            raise ProcessLookupError(str(error)) from error
        except psutil.AccessDenied as error:
            raise PermissionError(str(error)) from error
        return
    os.kill(process_id, signal.SIGTERM)


def terminate_digikam(timeout: float = 6.0) -> dict[str, Any]:
    """Ask digiKam to terminate, without escalating to a forced kill."""
    if not digikam_probe_supported():
        return {"supported": False, "closed": False, "remaining": []}
    process_ids = digikam_process_ids()
    if not process_ids:
        return {"supported": True, "closed": True, "remaining": []}
    for process_id in process_ids:
        try:
            _request_exit(process_id)
        except ProcessLookupError:
            continue
        except PermissionError as error:
            raise RuntimeError("DigiMem is not allowed to close digiKam.") from error
    deadline = time.monotonic() + max(0.0, timeout)
    remaining = process_ids
    while remaining and time.monotonic() < deadline:
        time.sleep(0.1)
        running = set(digikam_process_ids())
        remaining = [process_id for process_id in process_ids if process_id in running]
    return {"supported": True, "closed": not remaining, "remaining": remaining}


def digikam_database_is_free(database: str | Path, timeout: float = 1.0) -> bool:
    """Confirm nothing else holds a write lock on the database.

    The process check can miss a digiKam running elsewhere against a shared
    library, so the lock itself is the second opinion.
    """
    try:
        connection = sqlite3.connect(str(database), timeout=max(0.0, timeout))
    except sqlite3.Error:
        return False
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.rollback()
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        connection.close()


def create_sqlite_backup(database: str | Path, destination: str | Path) -> Path:
    """Create a consistent backup even when the source database uses WAL."""
    source_path = Path(database).resolve()
    backup_path = Path(destination).resolve()
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    if backup_path.exists():
        return backup_path
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    target = sqlite3.connect(backup_path)
    try:
        source.backup(target)
        target.commit()
    except Exception:
        target.close()
        backup_path.unlink(missing_ok=True)
        raise
    finally:
        source.close()
        try:
            target.close()
        except Exception:
            pass
    return backup_path


class DigikamWriter:
    REQUIRED_TABLES = {
        "Images", "Albums", "AlbumRoots", "ImageInformation", "Tags",
        "TagsTree", "TagProperties", "ImageTags", "ImageTagProperties",
    }

    def __init__(self, database: str | Path):
        self.path = Path(database).resolve()
        self.conn = sqlite3.connect(self.path, timeout=2.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 2000")
        tables = {
            str(row[0])
            for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not self.REQUIRED_TABLES.issubset(tables):
            self.close()
            raise ValueError("The digiKam database does not have the expected face tables.")
        self._person_cache: dict[str, int] = {}
        self._unnamed_tags: set[int] | None = None

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "DigikamWriter":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    @staticmethod
    def _relative_path(row: sqlite3.Row) -> str:
        album = str(row["album_rel"] or "/")
        value = str(row["name"]) if album == "/" else f"{album.rstrip('/')}/{row['name']}"
        return normalize_path(value).strip("/")

    def _image(self, path: str, image_id: int | None = None) -> sqlite3.Row:
        columns = """
            SELECT i.id AS image_id, i.name, a.relativePath AS album_rel,
                   COALESCE(ii.width, 0) AS width,
                   COALESCE(ii.height, 0) AS height,
                   COALESCE(ii.orientation, 1) AS orientation
            FROM Images i
            JOIN Albums a ON a.id = i.album
            LEFT JOIN ImageInformation ii ON ii.imageid = i.id
        """
        wanted = normalize_path(path).strip("/").lower()
        rows: list[sqlite3.Row]
        if image_id is not None:
            rows = self.conn.execute(columns + " WHERE i.id = ? AND i.status = 1", (image_id,)).fetchall()
        else:
            rows = self.conn.execute(
                columns + " WHERE lower(i.name) = lower(?) AND i.status = 1",
                (Path(path).name,),
            ).fetchall()
        matches = [row for row in rows if self._relative_path(row).lower() == wanted]
        if not matches:
            raise DigikamChangedError(f"Photo is no longer available in digiKam: {path}")
        if len(matches) > 1:
            raise DigikamChangedError(f"More than one digiKam photo matches: {path}")
        row = matches[0]
        if int(row["width"] or 0) <= 0 or int(row["height"] or 0) <= 0:
            raise DigikamChangedError(f"digiKam has no image dimensions for: {path}")
        return row

    def _person_tag(self, person: str, *, create: bool) -> int | None:
        cleaned = sanitize_person_name(person).strip()
        key = cleaned.lower()
        if not key:
            raise ValueError("A face name cannot be empty.")
        if key in self._person_cache:
            return self._person_cache[key]
        row = self.conn.execute(
            """SELECT t.id FROM Tags t
               JOIN TagProperties tp ON tp.tagid = t.id
               WHERE tp.property = 'person'
                 AND (lower(COALESCE(tp.value, t.name)) = lower(?) OR lower(t.name) = lower(?))
               ORDER BY t.id LIMIT 1""",
            (cleaned, cleaned),
        ).fetchone()
        if row is not None:
            tag_id = int(row[0])
            self._person_cache[key] = tag_id
            return tag_id
        if not create:
            return None
        parent = self.conn.execute(
            """SELECT t.id FROM Tags t
               WHERE lower(t.name) = 'people'
               ORDER BY CASE WHEN t.pid = 0 THEN 0 ELSE 1 END, t.id LIMIT 1"""
        ).fetchone()
        if parent is None:
            raise RuntimeError("digiKam's People tag could not be found.")
        cursor = self.conn.execute(
            "INSERT INTO Tags(pid, name) VALUES (?, ?)", (int(parent[0]), cleaned)
        )
        tag_id = int(cursor.lastrowid)
        engine_uuid = "{" + str(uuid.uuid4()) + "}"
        self.conn.executemany(
            "INSERT INTO TagProperties(tagid, property, value) VALUES (?, ?, ?)",
            [
                (tag_id, "person", cleaned),
                (tag_id, "faceEngineId", cleaned),
                (tag_id, "faceEngineUuid", engine_uuid),
            ],
        )
        self._person_cache[key] = tag_id
        return tag_id

    @staticmethod
    def _pixel_region(rect: Rect, image: sqlite3.Row) -> str:
        width, height = displayed_dimensions(
            int(image["width"]), int(image["height"]), int(image["orientation"])
        )
        clamped = rect.clamp()
        x = max(0, min(width - 1, round(clamped.x * width)))
        y = max(0, min(height - 1, round(clamped.y * height)))
        w = max(1, min(width - x, round(clamped.w * width)))
        h = max(1, min(height - y, round(clamped.h * height)))
        return f'<rect x="{x}" y="{y}" width="{w}" height="{h}"/>'

    def _face_rows(
        self, image: sqlite3.Row, *, prop: str = "tagRegion"
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT itp.rowid, itp.tagid, itp.value, t.name,
                      COALESCE((SELECT value FROM TagProperties
                                WHERE tagid = itp.tagid AND property = 'person' LIMIT 1), t.name) AS person
               FROM ImageTagProperties itp JOIN Tags t ON t.id = itp.tagid
               WHERE itp.imageid = ? AND itp.property = ?""",
            (int(image["image_id"]), prop),
        ).fetchall()
        out = []
        for row in rows:
            rect = parse_tag_region(
                str(row["value"]), int(image["width"]), int(image["height"]), int(image["orientation"])
            )
            if rect is not None:
                out.append({"row": row, "rect": rect})
        return out

    def _unnamed_face_tags(self) -> set[int]:
        """The tag ids digiKam keeps its own unidentified faces under."""
        if self._unnamed_tags is None:
            rows = self.conn.execute(
                "SELECT tagid FROM TagProperties WHERE property IN (?, ?)",
                UNNAMED_FACE_PROPERTIES,
            ).fetchall()
            self._unnamed_tags = {int(row[0]) for row in rows}
        return self._unnamed_tags

    def _clear_unnamed_box(self, image: sqlite3.Row, rect: Rect) -> int:
        """Take away digiKam's own unnamed rectangle for a face just named.

        digiKam removes it itself when somebody confirms a face, and showing
        the same face twice, once with the name just written and once as a
        stranger, is what leaving it behind would look like.
        """
        removed = 0
        for face in self._face_rows(image, prop=DETECTED_FACE_PROPERTY):
            if int(face["row"]["tagid"]) not in self._unnamed_face_tags():
                # A suggestion digiKam made for a real person. Its own
                # business, and not this face's to withdraw.
                continue
            if face["rect"].iou(rect) < 0.4:
                continue
            self.conn.execute(
                "DELETE FROM ImageTagProperties WHERE rowid = ?",
                (int(face["row"]["rowid"]),),
            )
            removed += 1
        return removed

    def locate_face(
        self, path: str, person: str, rect: tuple[float, float, float, float],
        *, image_id: int | None = None,
    ) -> dict[str, int] | None:
        image = self._image(path, image_id)
        wanted = Rect(*map(float, rect)).clamp()
        candidates = [
            face for face in self._face_rows(image)
            if person_names_match(str(face["row"]["person"] or ""), person)
        ]
        if not candidates:
            return None
        best = max(candidates, key=lambda face: face["rect"].iou(wanted))
        if best["rect"].iou(wanted) < 0.4:
            return None
        return {
            "digikam_image_id": int(image["image_id"]),
            "digikam_tag_id": int(best["row"]["tagid"]),
        }

    def create_face(
        self, path: str, person: str, rect: tuple[float, float, float, float],
        *, image_id: int | None = None,
    ) -> dict[str, Any]:
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            image = self._image(path, image_id)
            wanted = Rect(*map(float, rect)).clamp()
            pairs = [(face, face["rect"].iou(wanted)) for face in self._face_rows(image)]
            overlaps = [(face, score) for face, score in pairs if score >= 0.4]
            if overlaps:
                face, _ = max(overlaps, key=lambda item: item[1])
                if not person_names_match(str(face["row"]["person"] or ""), person):
                    raise DigikamChangedError(
                        f"A different digiKam face now overlaps the approved region in {path}."
                    )
                result = {
                    "changed": False,
                    "digikam_image_id": int(image["image_id"]),
                    "digikam_tag_id": int(face["row"]["tagid"]),
                }
                self.conn.commit()
                return result
            tag_id = int(self._person_tag(person, create=True) or 0)
            value = self._pixel_region(wanted, image)
            self.conn.execute(
                "INSERT OR IGNORE INTO ImageTags(imageid, tagid) VALUES (?, ?)",
                (int(image["image_id"]), tag_id),
            )
            self.conn.executemany(
                "INSERT INTO ImageTagProperties(imageid, tagid, property, value) VALUES (?, ?, ?, ?)",
                [
                    (int(image["image_id"]), tag_id, "tagRegion", value),
                    (int(image["image_id"]), tag_id, "faceToTrain", value),
                ],
            )
            cleared = self._clear_unnamed_box(image, wanted)
            self.conn.commit()
            return {
                "changed": True,
                "digikam_image_id": int(image["image_id"]),
                "digikam_tag_id": tag_id,
                "cleared_unnamed": cleared,
            }
        except Exception:
            self.conn.rollback()
            raise

    def remove_face(
        self, path: str, person: str, rect: tuple[float, float, float, float],
        *, image_id: int | None = None,
    ) -> dict[str, Any]:
        """Take one face rectangle out, and only that one.

        Matched on the stored value rather than on a fresh rendering of the
        rectangle, so a rounding difference can never delete the neighbour.
        The person keeps the tag if they still have another face here.
        """
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            image = self._image(path, image_id)
            wanted = Rect(*map(float, rect)).clamp()
            candidates = [
                face for face in self._face_rows(image)
                if person_names_match(str(face["row"]["person"] or ""), person)
            ]
            best = max(candidates, key=lambda face: face["rect"].iou(wanted), default=None)
            if best is None or best["rect"].iou(wanted) < 0.9:
                # Already gone, or moved by whoever was here first.
                self.conn.commit()
                return {"changed": False, "digikam_image_id": int(image["image_id"])}
            tag_id = int(best["row"]["tagid"])
            self.conn.execute(
                """DELETE FROM ImageTagProperties
                   WHERE imageid = ? AND tagid = ?
                     AND property IN ('tagRegion', 'faceToTrain') AND value = ?""",
                (int(image["image_id"]), tag_id, str(best["row"]["value"])),
            )
            left = self.conn.execute(
                """SELECT COUNT(*) FROM ImageTagProperties
                   WHERE imageid = ? AND tagid = ? AND property = 'tagRegion'""",
                (int(image["image_id"]), tag_id),
            ).fetchone()[0]
            if not left:
                # That was their only face here, so the photo is no longer
                # theirs either.
                self.conn.execute(
                    "DELETE FROM ImageTags WHERE imageid = ? AND tagid = ?",
                    (int(image["image_id"]), tag_id),
                )
            self.conn.commit()
            return {
                "changed": True,
                "digikam_image_id": int(image["image_id"]),
                "digikam_tag_id": tag_id,
                "faces_left": int(left),
            }
        except Exception:
            self.conn.rollback()
            raise

    def move_face(
        self, path: str, person: str, was: tuple[float, float, float, float],
        now: tuple[float, float, float, float], *, image_id: int | None = None,
    ) -> dict[str, Any]:
        """Redraw one face where a person said it belongs.

        Correcting a rectangle in review says the box is wrong, not that a
        different box should be sent onward this once. Leaving the original
        behind is what makes the same face come back every run: it still has
        no counterpart, so it is proposed again, and refused again.
        """
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            image = self._image(path, image_id)
            before, after = Rect(*map(float, was)).clamp(), Rect(*map(float, now)).clamp()
            candidates = [
                face for face in self._face_rows(image)
                if person_names_match(str(face["row"]["person"] or ""), person)
            ]
            best = max(
                candidates, key=lambda face: face["rect"].iou(before), default=None
            )
            if best is None or best["rect"].iou(before) < 0.4:
                # Already moved, or removed, by whoever was here first.
                self.conn.commit()
                return {"changed": False, "digikam_image_id": int(image["image_id"])}
            if best["rect"].iou(after) >= 0.99:
                self.conn.commit()
                return {
                    "changed": False,
                    "digikam_image_id": int(image["image_id"]),
                    "digikam_tag_id": int(best["row"]["tagid"]),
                }
            value = self._pixel_region(after, image)
            self.conn.execute(
                """UPDATE ImageTagProperties SET value = ?
                   WHERE imageid = ? AND tagid = ? AND property IN
                         ('tagRegion', 'faceToTrain') AND value = ?""",
                (value, int(image["image_id"]), int(best["row"]["tagid"]),
                 str(best["row"]["value"])),
            )
            self.conn.commit()
            return {
                "changed": True,
                "digikam_image_id": int(image["image_id"]),
                "digikam_tag_id": int(best["row"]["tagid"]),
            }
        except Exception:
            self.conn.rollback()
            raise

    def reassign_face(
        self, path: str, old_person: str, new_person: str,
        rect: tuple[float, float, float, float], *,
        image_id: int | None = None, old_tag_id: int | None = None,
    ) -> dict[str, Any]:
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            image = self._image(path, image_id)
            wanted = Rect(*map(float, rect)).clamp()
            faces = self._face_rows(image)
            target = [
                face for face in faces
                if person_names_match(str(face["row"]["person"] or ""), new_person)
                and face["rect"].iou(wanted) >= 0.4
            ]
            candidates = [
                face for face in faces
                if (old_tag_id is None or int(face["row"]["tagid"]) == old_tag_id)
                and person_names_match(str(face["row"]["person"] or ""), old_person)
                and face["rect"].iou(wanted) >= 0.4
            ]
            if target and candidates:
                raise DigikamChangedError(
                    f"More than one digiKam face now overlaps the approved region in {path}."
                )
            if target:
                result = {
                    "changed": False,
                    "digikam_image_id": int(image["image_id"]),
                    "digikam_tag_id": int(target[0]["row"]["tagid"]),
                }
                self.conn.commit()
                return result
            if not candidates:
                raise DigikamChangedError(
                    f"The digiKam face approved in the preview has changed: {path}."
                )
            source = max(candidates, key=lambda face: face["rect"].iou(wanted))
            source_tag = int(source["row"]["tagid"])
            value = str(source["row"]["value"])
            target_tag = int(self._person_tag(new_person, create=True) or 0)
            self.conn.execute(
                "INSERT OR IGNORE INTO ImageTags(imageid, tagid) VALUES (?, ?)",
                (int(image["image_id"]), target_tag),
            )
            self.conn.execute(
                """UPDATE ImageTagProperties SET tagid = ?
                   WHERE imageid = ? AND tagid = ?
                     AND property IN ('tagRegion', 'faceToTrain') AND value = ?""",
                (target_tag, int(image["image_id"]), source_tag, value),
            )
            remaining = self.conn.execute(
                "SELECT 1 FROM ImageTagProperties WHERE imageid = ? AND tagid = ? LIMIT 1",
                (int(image["image_id"]), source_tag),
            ).fetchone()
            if remaining is None:
                self.conn.execute(
                    "DELETE FROM ImageTags WHERE imageid = ? AND tagid = ?",
                    (int(image["image_id"]), source_tag),
                )
            self.conn.commit()
            return {
                "changed": True,
                "digikam_image_id": int(image["image_id"]),
                "digikam_tag_id": target_tag,
            }
        except Exception:
            self.conn.rollback()
            raise
