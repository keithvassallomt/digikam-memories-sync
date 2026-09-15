"""Read face regions from digiKam digikam4.db (streamed for large libraries)."""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterator, Optional

from .constants import DEFAULT_SKIP_PERSONS, TAG_REGION_PROPERTY
from .geometry import parse_tag_region
from .models import DigikamImage, FaceRegion
from .names import sanitize_person_name
from .paths import normalize_path

LOG = logging.getLogger(__name__)

# SQLite default max host parameters is often 999
_IN_CHUNK = 800


class DigikamDB:
    """
    Read-only digiKam core DB access.

    Designed for large libraries (100k+ images, 500k+ face regions):
    builds a faced-image id index with one sequential scan (no per-batch
    DISTINCT/ORDER BY), then streams face data in O(batch) memory.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        if not self.db_path.is_file():
            raise FileNotFoundError(f"digiKam database not found: {self.db_path}")
        self.conn = sqlite3.connect(
            f"file:{self.db_path}?mode=ro",
            uri=True,
            check_same_thread=False,
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA query_only = ON")
        self.conn.execute("PRAGMA temp_store = MEMORY")
        self.conn.execute("PRAGMA cache_size = -65536")  # ~64MB page cache
        self._person_tags: Optional[dict[int, str]] = None
        self._faced_image_ids: Optional[list[int]] = None
        self._tag_region_row_count: Optional[int] = None
        self._person_image_ids: dict[str, list[int]] = {}

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "DigikamDB":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def person_tag_ids(self) -> dict[int, str]:
        if self._person_tags is not None:
            return self._person_tags
        t0 = time.monotonic()
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT t.id, t.name, tp.property, tp.value
            FROM Tags t
            JOIN TagProperties tp ON tp.tagid = t.id
            WHERE tp.property IN ('person', 'faceEngineId', 'unknownPerson',
                                  'unconfirmedPerson')
            """
        )
        tags: dict[int, str] = {}
        renamed = 0
        for row in cur.fetchall():
            raw = (row["value"] or row["name"] or "").strip()
            if not raw:
                continue
            name = sanitize_person_name(raw)
            if not name:
                continue
            if name != raw:
                renamed += 1
                LOG.debug("Person tag sanitized %r → %r", raw, name)
            tags[row["id"]] = name
        self._person_tags = tags
        LOG.info(
            "Loaded %d person tags from digiKam (%.1fs)%s",
            len(tags),
            time.monotonic() - t0,
            f", {renamed} names with / or \\ rewritten to -" if renamed else "",
        )
        return tags

    def faced_image_ids(self) -> list[int]:
        """
        Sorted list of image ids that have at least one tagRegion and status=1.

        Built once with a sequential scan + hash set (fast), not with repeated
        ``SELECT DISTINCT … ORDER BY … LIMIT`` (which pegs CPU on large DBs).
        """
        if self._faced_image_ids is not None:
            return self._faced_image_ids

        LOG.info(
            "Building digiKam faced-image index "
            "(one sequential scan of ImageTagProperties.tagRegion)…"
        )
        t0 = time.monotonic()
        cur = self.conn.cursor()
        # No ORDER BY / DISTINCT in SQL — stream rows, unique in Python
        cur.execute(
            "SELECT imageid FROM ImageTagProperties WHERE property = ?",
            (TAG_REGION_PROPERTY,),
        )

        unique: set[int] = set()
        rows_seen = 0
        last_log = t0
        while True:
            chunk = cur.fetchmany(50_000)
            if not chunk:
                break
            for (iid,) in chunk:
                unique.add(int(iid))
            rows_seen += len(chunk)
            now = time.monotonic()
            if now - last_log >= 1.5:
                LOG.info(
                    "  tagRegion scan: %s rows read, %s unique image ids (%.1fs)…",
                    f"{rows_seen:,}",
                    f"{len(unique):,}",
                    now - t0,
                )
                last_log = now

        self._tag_region_row_count = rows_seen
        LOG.info(
            "tagRegion scan complete: %s rows, %s unique image ids (%.1fs). "
            "Filtering Images.status=1…",
            f"{rows_seen:,}",
            f"{len(unique):,}",
            time.monotonic() - t0,
        )

        id_list = list(unique)
        active: list[int] = []
        t_filter = time.monotonic()
        for i in range(0, len(id_list), _IN_CHUNK):
            chunk = id_list[i : i + _IN_CHUNK]
            placeholders = ",".join("?" * len(chunk))
            cur.execute(
                f"SELECT id FROM Images WHERE status = 1 AND id IN ({placeholders})",
                chunk,
            )
            active.extend(int(r[0]) for r in cur.fetchall())
            if i == 0 or (i // _IN_CHUNK) % 25 == 0:
                LOG.info(
                    "  status filter: %s / %s candidate ids → %s active…",
                    f"{min(i + len(chunk), len(id_list)):,}",
                    f"{len(id_list):,}",
                    f"{len(active):,}",
                )

        active.sort()
        self._faced_image_ids = active
        LOG.info(
            "Faced-image index ready: %s active images (%.1fs total, filter %.1fs)",
            f"{len(active):,}",
            time.monotonic() - t0,
            time.monotonic() - t_filter,
        )
        return active

    def count_images_with_faces(self) -> int:
        return len(self.faced_image_ids())

    def count_face_regions(self) -> int:
        if self._tag_region_row_count is not None:
            return self._tag_region_row_count
        # Ensure index build populates the row count
        self.faced_image_ids()
        return int(self._tag_region_row_count or 0)

    def tag_ids_for_person(self, person: str) -> list[int]:
        """Return digiKam tag ids whose person name matches ``person`` (case-insensitive)."""
        key = sanitize_person_name(person or "").strip().lower()
        if not key:
            return []
        return [
            tid
            for tid, name in self.person_tag_ids().items()
            if (name or "").strip().lower() == key
        ]

    def image_ids_for_person(self, person: str) -> list[int]:
        """
        Active image ids that have at least one tagRegion for ``person``.

        Much cheaper than scanning the full faced-image index when testing
        or updating a single person. Results are cached per normalized name.
        """
        key = sanitize_person_name(person or "").strip().lower()
        if not key:
            LOG.warning("only_person is empty after sanitization; no images selected")
            return []
        if key in self._person_image_ids:
            return self._person_image_ids[key]

        tag_ids = self.tag_ids_for_person(person)
        if not tag_ids:
            LOG.warning(
                "No digiKam person tag matches %r (after sanitize/case-fold)",
                person,
            )
            self._person_image_ids[key] = []
            return []

        LOG.info(
            "Resolving images for person %r (%d matching tag id(s): %s)…",
            sanitize_person_name(person),
            len(tag_ids),
            tag_ids[:10] if len(tag_ids) > 10 else tag_ids,
        )
        t0 = time.monotonic()
        cur = self.conn.cursor()
        unique: set[int] = set()
        for i in range(0, len(tag_ids), _IN_CHUNK):
            chunk = tag_ids[i : i + _IN_CHUNK]
            placeholders = ",".join("?" * len(chunk))
            cur.execute(
                f"""
                SELECT imageid FROM ImageTagProperties
                WHERE property = ? AND tagid IN ({placeholders})
                """,
                (TAG_REGION_PROPERTY, *chunk),
            )
            for (iid,) in cur.fetchall():
                unique.add(int(iid))

        id_list = list(unique)
        active: list[int] = []
        for i in range(0, len(id_list), _IN_CHUNK):
            chunk = id_list[i : i + _IN_CHUNK]
            placeholders = ",".join("?" * len(chunk))
            cur.execute(
                f"SELECT id FROM Images WHERE status = 1 AND id IN ({placeholders})",
                chunk,
            )
            active.extend(int(r[0]) for r in cur.fetchall())

        active.sort()
        LOG.info(
            "Person %r: %s active images with face regions (%.1fs)",
            sanitize_person_name(person),
            f"{len(active):,}",
            time.monotonic() - t0,
        )
        self._person_image_ids[key] = active
        return active

    def _load_images_by_ids(
        self,
        image_ids: list[int],
        skip_persons: frozenset[str],
        only_persons: Optional[frozenset[str]] = None,
        *,
        require_faces: bool = True,
    ) -> list[DigikamImage]:
        if not image_ids:
            return []
        t0 = time.monotonic()
        person_tags = self.person_tag_ids()
        skip_lower = {s.lower() for s in skip_persons}
        only_lower = (
            {s.lower() for s in only_persons} if only_persons is not None else None
        )
        placeholders = ",".join("?" * len(image_ids))
        cur = self.conn.cursor()

        LOG.debug("  loading metadata for %d images…", len(image_ids))
        cur.execute(
            f"""
            SELECT
                i.id AS image_id,
                i.name AS name,
                i.fileSize AS file_size,
                COALESCE(i.uniqueHash, '') AS unique_hash,
                COALESCE(ii.width, 0) AS width,
                COALESCE(ii.height, 0) AS height,
                COALESCE(ii.orientation, 1) AS orientation,
                a.relativePath AS album_rel,
                COALESCE(ar.specificPath, '') AS root_path
            FROM Images i
            JOIN Albums a ON a.id = i.album
            JOIN AlbumRoots ar ON ar.id = a.albumRoot
            LEFT JOIN ImageInformation ii ON ii.imageid = i.id
            WHERE i.id IN ({placeholders})
            """,
            image_ids,
        )
        images: dict[int, DigikamImage] = {}
        for row in cur.fetchall():
            album_rel = row["album_rel"] or "/"
            if album_rel == "/":
                rel = row["name"]
            else:
                rel = f"{album_rel.rstrip('/')}/{row['name']}".lstrip("/")
            root = (row["root_path"] or "").rstrip("/")
            full = f"{root}/{rel}" if root else rel
            images[int(row["image_id"])] = DigikamImage(
                image_id=int(row["image_id"]),
                name=row["name"],
                relative_path=normalize_path(rel),
                full_path=normalize_path(full),
                width=int(row["width"] or 0),
                height=int(row["height"] or 0),
                file_size=int(row["file_size"] or 0),
                unique_hash=row["unique_hash"] or "",
                orientation=int(row["orientation"] or 1),
            )

        LOG.debug("  loading tagRegion rows for %d images…", len(image_ids))
        cur.execute(
            f"""
            SELECT
                itp.imageid,
                itp.tagid,
                itp.value AS region_value,
                t.name AS tag_name
            FROM ImageTagProperties itp
            JOIN Tags t ON t.id = itp.tagid
            WHERE itp.property = ?
              AND itp.imageid IN ({placeholders})
            """,
            (TAG_REGION_PROPERTY, *image_ids),
        )
        face_rows = 0
        for row in cur.fetchall():
            face_rows += 1
            img = images.get(int(row["imageid"]))
            if not img or img.width <= 0 or img.height <= 0:
                continue
            tag_id = int(row["tagid"])
            person = person_tags.get(tag_id) or sanitize_person_name(
                (row["tag_name"] or "").strip()
            )
            if not person or person.lower() in skip_lower:
                continue
            if only_lower is not None and person.lower() not in only_lower:
                continue
            rect = parse_tag_region(
                row["region_value"],
                img.width,
                img.height,
                img.orientation,
            )
            if not rect or rect.area() <= 0:
                continue
            img.faces.append(
                FaceRegion(
                    person=person,
                    rect=rect,
                    source="digikam",
                    image_path=img.relative_path,
                    digikam_image_id=img.image_id,
                    digikam_tag_id=tag_id,
                )
            )

        result = [
            images[i]
            for i in image_ids
            if i in images and (images[i].faces or not require_faces)
        ]
        LOG.debug(
            "  loaded %d images with named faces (%d tagRegion rows, %.2fs)",
            len(result),
            face_rows,
            time.monotonic() - t0,
        )
        return result

    def images_for_relative_paths(
        self,
        relative_paths: list[str],
        skip_persons: frozenset[str] = DEFAULT_SKIP_PERSONS,
    ) -> dict[str, DigikamImage]:
        """Resolve Nextcloud-relative photo paths to active digiKam images.

        The basename filter keeps the query small, while the complete normalized
        relative path prevents identically named photos in different albums from
        being confused.
        """
        wanted = {
            normalize_path(path).strip("/").lower()
            for path in relative_paths
            if normalize_path(path).strip("/")
        }
        if not wanted:
            return {}

        names = sorted({Path(path).name.lower() for path in wanted})
        image_ids: set[int] = set()
        cursor = self.conn.cursor()
        for start in range(0, len(names), _IN_CHUNK):
            chunk = names[start : start + _IN_CHUNK]
            placeholders = ",".join("?" * len(chunk))
            cursor.execute(
                f"""SELECT id FROM Images
                    WHERE status = 1 AND lower(name) IN ({placeholders})""",
                chunk,
            )
            image_ids.update(int(row[0]) for row in cursor.fetchall())

        images = self._load_images_by_ids(
            sorted(image_ids),
            skip_persons,
            require_faces=False,
        )
        resolved: dict[str, DigikamImage] = {}
        ambiguous: set[str] = set()
        for image in images:
            key = normalize_path(image.relative_path).strip("/").lower()
            if key not in wanted:
                continue
            if key in resolved:
                ambiguous.add(key)
            else:
                resolved[key] = image
        for key in ambiguous:
            resolved.pop(key, None)
            LOG.warning("Multiple active digiKam images match relative path %r", key)
        return resolved

    def iter_image_batches(
        self,
        batch_size: int = 500,
        skip_persons: frozenset[str] = DEFAULT_SKIP_PERSONS,
        limit_images: Optional[int] = None,
        only_person: Optional[str] = None,
        skip_image_ids: Optional[set[int]] = None,
        start_after_image_id: Optional[int] = None,
    ) -> Iterator[list[DigikamImage]]:
        """
        Yield batches of DigikamImage that have at least one named face.

        Slices a prebuilt id list (O(1) per batch after the one-time index).
        When ``only_person`` is set, only images/faces for that person are used
        (targeted tag lookup — not a full library scan).

        ``skip_image_ids`` drops already-processed digiKam image ids (resume).

        ``start_after_image_id`` resumes from a point in the id order without
        carrying a set of everything already done, which is what a long run
        interrupted half way needs.
        """
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")

        only_persons: Optional[frozenset[str]] = None
        if only_person:
            cleaned = sanitize_person_name(only_person).strip()
            if not cleaned:
                LOG.warning("only_person %r is empty after sanitization", only_person)
                return
            only_persons = frozenset({cleaned.lower()})
            LOG.info(
                "Preparing digiKam image batches for person %r only (batch_size=%d)…",
                cleaned,
                batch_size,
            )
            all_ids = self.image_ids_for_person(cleaned)
        else:
            LOG.info("Preparing digiKam image batches (batch_size=%d)…", batch_size)
            all_ids = self.faced_image_ids()

        if limit_images is not None:
            all_ids = all_ids[: int(limit_images)]
            LOG.info("Limited to first %d faced images", len(all_ids))

        if start_after_image_id is not None:
            cutoff = int(start_after_image_id)
            before = len(all_ids)
            # The id list is already ordered, so resuming is a slice, not a
            # membership test against everything done so far.
            all_ids = [image_id for image_id in all_ids if image_id > cutoff]
            LOG.info(
                "Resuming after image id %d (%d of %d images remain)",
                cutoff,
                len(all_ids),
                before,
            )

        skip = skip_image_ids or set()
        if skip:
            before = len(all_ids)
            all_ids = [i for i in all_ids if i not in skip]
            LOG.info(
                "Resume filter: skipped %d already-processed image ids "
                "(%d remaining of %d)",
                before - len(all_ids),
                len(all_ids),
                before,
            )

        total = len(all_ids)
        if total == 0:
            LOG.info("No faced images to process")
            return

        n_batches = (total + batch_size - 1) // batch_size
        LOG.info(
            "Will process %s images in %d batches of up to %d%s",
            f"{total:,}",
            n_batches,
            batch_size,
            f" (person={sanitize_person_name(only_person)!r})" if only_person else "",
        )

        for batch_num, offset in enumerate(range(0, total, batch_size), start=1):
            chunk = all_ids[offset : offset + batch_size]
            LOG.info(
                "digiKam batch %d / %d: loading faces for %d images "
                "(ids %d–%d, offset %d)…",
                batch_num,
                n_batches,
                len(chunk),
                chunk[0],
                chunk[-1],
                offset,
            )
            t0 = time.monotonic()
            images = self._load_images_by_ids(
                chunk, skip_persons, only_persons=only_persons
            )
            faces = sum(len(im.faces) for im in images)
            LOG.info(
                "digiKam batch %d / %d: ready — %d images / %d named faces (%.2fs)",
                batch_num,
                n_batches,
                len(images),
                faces,
                time.monotonic() - t0,
            )
            if images:
                yield images

    def load_images_with_faces(
        self,
        skip_persons: frozenset[str] = DEFAULT_SKIP_PERSONS,
        only_confirmed: bool = True,
        limit_images: Optional[int] = None,
        only_person: Optional[str] = None,
    ) -> list[DigikamImage]:
        """Load all (or limited) faced images into a list. Prefer iter_image_batches."""
        del only_confirmed
        LOG.warning(
            "load_images_with_faces() loads the full result set into memory; "
            "for large libraries use iter_image_batches() via sync()"
        )
        out: list[DigikamImage] = []
        for batch in self.iter_image_batches(
            batch_size=1000,
            skip_persons=skip_persons,
            limit_images=limit_images,
            only_person=only_person,
        ):
            out.extend(batch)
        return out
