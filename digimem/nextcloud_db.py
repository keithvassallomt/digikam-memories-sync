"""SQL backend for Nextcloud + Recognize (optional SSH tunnel)."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Optional
from urllib.parse import unquote, urlparse

from .constants import FACE_VECTOR_DIM, IMAGE_EXT
from .geometry import zero_vector
from .matching import digikam_path_candidates
from .models import DigikamImage, FaceRegion, FileMatch, NextcloudFile, Rect
from .names import sanitize_person_name
from .paths import normalize_path, strip_nc_files_prefix
from .ssh_tunnel import SSHTunnel, rewrite_dsn_host_port

LOG = logging.getLogger(__name__)

class NextcloudDB:
    """SQL backend for Nextcloud + Recognize tables."""

    supports_insert = True

    def __init__(
        self,
        dsn: str,
        table_prefix: str = "oc_",
        user_id: str = "",
        ssh: Optional[dict[str, Any]] = None,
    ):
        self.table_prefix = table_prefix
        self.user_id = user_id
        self._tunnel: Optional[SSHTunnel] = None
        connect_dsn = dsn

        if ssh and ssh.get("enabled", True) and ssh.get("host"):
            parsed = urlparse(dsn)
            remote_host = ssh.get("remote_bind_host") or parsed.hostname or "127.0.0.1"
            remote_port = int(
                ssh.get("remote_bind_port")
                or parsed.port
                or (5432 if parsed.scheme.startswith("postgres") else 3306)
            )
            self._tunnel = SSHTunnel(
                ssh_host=str(ssh["host"]),
                ssh_user=str(ssh.get("user") or os.environ.get("USER") or "root"),
                remote_bind_host=str(remote_host),
                remote_bind_port=remote_port,
                ssh_port=int(ssh.get("port") or 22),
                identity_file=ssh.get("identity_file") or ssh.get("identity"),
                local_bind_host=str(ssh.get("local_bind_host") or "127.0.0.1"),
                local_bind_port=int(ssh.get("local_bind_port") or 0),
                extra_args=list(ssh.get("extra_args") or []),
                ready_timeout=float(ssh.get("ready_timeout") or 30),
            )
            local_port = self._tunnel.start()
            connect_dsn = rewrite_dsn_host_port(
                dsn, self._tunnel.local_bind_host, local_port
            )
            LOG.info("Connecting to DB via tunnel DSN host %s:%s", self._tunnel.local_bind_host, local_port)

        self.driver, self.conn = self._connect(connect_dsn)
        self._set_row_factory()
        self._cluster_title_cache: Optional[dict[str, int]] = None
        self._vector_cache: dict[int, Optional[list[float]]] = {}

    @staticmethod
    def _connect(dsn: str) -> tuple[str, Any]:
        if dsn.startswith("sqlite:"):
            if dsn.startswith("sqlite:////"):
                path = "/" + dsn[len("sqlite:////") :]
            elif dsn.startswith("sqlite:///"):
                path = dsn[len("sqlite:///") :]
            else:
                path = dsn[len("sqlite:") :].lstrip("/")
            return "sqlite", sqlite3.connect(path)

        parsed = urlparse(dsn)
        scheme = parsed.scheme.lower()
        user = unquote(parsed.username or "")
        password = unquote(parsed.password or "")
        host = parsed.hostname or "localhost"
        port = parsed.port
        dbname = (parsed.path or "/").lstrip("/")

        if scheme in ("mysql", "mariadb"):
            import pymysql

            conn = pymysql.connect(
                host=host,
                user=user,
                password=password,
                database=dbname,
                port=port or 3306,
                charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=False,
            )
            return "mysql", conn

        if scheme in ("postgresql", "postgres"):
            import psycopg2

            conn = psycopg2.connect(
                host=host,
                user=user,
                password=password,
                dbname=dbname,
                port=port or 5432,
            )
            return "postgres", conn

        raise ValueError(
            f"Unsupported DSN scheme {scheme!r}. "
            "Use sqlite:///..., mysql://..., or postgresql://..."
        )

    def _set_row_factory(self) -> None:
        if self.driver == "sqlite":
            self.conn.row_factory = sqlite3.Row

    def close(self) -> None:
        try:
            self.conn.close()
        finally:
            if self._tunnel:
                self._tunnel.stop()
                self._tunnel = None

    def __enter__(self) -> "NextcloudDB":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def t(self, name: str) -> str:
        if name.startswith(self.table_prefix):
            return name
        return f"{self.table_prefix}{name}"

    def _cursor(self) -> Any:
        if self.driver == "postgres":
            import psycopg2.extras

            return self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        return self.conn.cursor()

    def _execute(self, sql: str, params: tuple | list | dict = ()) -> Any:
        if self.driver != "sqlite":
            sql = sql.replace("?", "%s")
        cur = self._cursor()
        cur.execute(sql, params)
        return cur

    def _fetchall(self, sql: str, params: tuple | list | dict = ()) -> list[dict]:
        cur = self._execute(sql, params)
        rows = cur.fetchall()
        if self.driver == "sqlite":
            return [dict(r) for r in rows]
        return list(rows)

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    def list_image_files(
        self,
        path_prefix: str = "files/",
        storage_filter: Optional[str] = None,
    ) -> list[NextcloudFile]:
        LOG.info(
            "Querying Nextcloud filecache for images under %r…",
            path_prefix,
        )
        fc = self.t("filecache")
        st = self.t("storages")
        prefix = normalize_path(path_prefix)
        like = prefix.rstrip("/") + "/%"
        sql = f"""
            SELECT f.fileid AS file_id, f.path, f.name, f.size, f.storage AS storage_id
            FROM {fc} f
            JOIN {st} s ON s.numeric_id = f.storage
            WHERE f.path = ? OR f.path LIKE ?
        """
        params: list[Any] = [prefix.rstrip("/"), like]
        if storage_filter:
            sql += " AND s.id LIKE ?"
            params.append(storage_filter)

        out: list[NextcloudFile] = []
        for r in self._fetchall(sql, params):
            name = r["name"] or PurePosixPath(r["path"]).name
            ext = Path(name).suffix.lower()
            if ext and ext not in IMAGE_EXT:
                continue
            path = normalize_path(r["path"])
            out.append(
                NextcloudFile(
                    file_id=int(r["file_id"]),
                    path=path,
                    name=name,
                    size=int(r["size"] or 0),
                    storage_id=int(r["storage_id"]),
                    webdav_path=strip_nc_files_prefix(path),
                )
            )
        LOG.info("filecache returned %d image files", len(out))
        return out

    def list_face_clusters(self) -> dict[str, int]:
        if self._cluster_title_cache is not None:
            return dict(self._cluster_title_cache)
        tbl = self.t("recognize_face_clusters")
        rows = self._fetchall(
            f"SELECT id, title FROM {tbl} WHERE user_id = ?",
            (self.user_id,),
        )
        out: dict[str, int] = {}
        for r in rows:
            title = (r["title"] or "").strip()
            if title:
                out[title.lower()] = int(r["id"])
        self._cluster_title_cache = out
        LOG.info("Loaded %d named face clusters for user %s", len(out), self.user_id)
        return dict(out)

    def get_or_create_cluster(self, title: str) -> int:
        title = sanitize_person_name(title)
        if not title:
            raise ValueError("cluster title must be non-empty after sanitization")
        existing = self.list_face_clusters()
        if title.lower() in existing:
            return existing[title.lower()]

        tbl = self.t("recognize_face_clusters")
        if self.driver == "postgres":
            cur = self._execute(
                f"INSERT INTO {tbl} (title, user_id) VALUES (?, ?) RETURNING id",
                (title, self.user_id),
            )
            row = cur.fetchone()
            cid = int(row["id"] if isinstance(row, dict) else row[0])
        else:
            cur = self._execute(
                f"INSERT INTO {tbl} (title, user_id) VALUES (?, ?)",
                (title, self.user_id),
            )
            cid = int(cur.lastrowid)
        if self._cluster_title_cache is not None:
            self._cluster_title_cache[title.lower()] = cid
        return cid

    def resolve_digikam_images(
        self,
        digikam_images: list[DigikamImage],
        path_maps: list[tuple[str, str]],
    ) -> tuple[
        list[FileMatch],
        list[DigikamImage],
        dict[int, list[FaceRegion]],
    ]:
        """
        Resolve digiKam images via filecache path lookup (no full table scan).

        Builds candidate paths from path_maps, fetches matching oc_filecache
        rows in chunks, then loads Recognize detections for hits only.
        """
        # candidate_path (filecache form) -> list of digikam images that map there
        path_index: dict[str, list[tuple[DigikamImage, str]]] = {}
        skipped_no_map = 0
        for di in digikam_images:
            cands = digikam_path_candidates(di, path_maps)
            if path_maps and not cands:
                skipped_no_map += 1
                continue
            for cand in cands:
                user_rel = strip_nc_files_prefix(normalize_path(cand))
                if not user_rel or user_rel == di.name and "/" not in cand:
                    # skip bare basename unless it was the only form
                    if user_rel == di.name:
                        continue
                fc_path = normalize_path(f"files/{user_rel}")
                path_index.setdefault(fc_path, []).append((di, fc_path))

        all_paths = list(path_index.keys())
        LOG.debug(
            "DB resolve: %d digiKam images → %d candidate paths (%d skipped, no path_map)",
            len(digikam_images),
            len(all_paths),
            skipped_no_map,
        )

        # path -> NextcloudFile
        found_files: dict[str, NextcloudFile] = {}
        fc = self.t("filecache")
        chunk_size = 400
        for i in range(0, len(all_paths), chunk_size):
            chunk = all_paths[i : i + chunk_size]
            placeholders = ",".join("?" * len(chunk))
            rows = self._fetchall(
                f"""
                SELECT fileid AS file_id, path, name, size, storage AS storage_id
                FROM {fc}
                WHERE path IN ({placeholders})
                """,
                chunk,
            )
            for r in rows:
                path = normalize_path(r["path"])
                name = r["name"] or PurePosixPath(path).name
                found_files[path] = NextcloudFile(
                    file_id=int(r["file_id"]),
                    path=path,
                    name=name,
                    size=int(r["size"] or 0),
                    storage_id=int(r["storage_id"] or 0),
                    webdav_path=strip_nc_files_prefix(path),
                )

        matches: list[FileMatch] = []
        unmatched: list[DigikamImage] = []
        matched_ids: set[int] = set()

        for di in digikam_images:
            cands = digikam_path_candidates(di, path_maps)
            if path_maps and not cands:
                # Outside configured path_maps — skip (not unmatched)
                continue
            hit: Optional[NextcloudFile] = None
            for cand in cands:
                user_rel = strip_nc_files_prefix(normalize_path(cand))
                if not user_rel:
                    continue
                fc_path = normalize_path(f"files/{user_rel}")
                nf = found_files.get(fc_path)
                if nf is None:
                    continue
                if di.file_size > 0 and nf.size > 0 and nf.size != di.file_size:
                    # keep looking for a size match; accept later if unique
                    if hit is None:
                        hit = nf
                    continue
                hit = nf
                break
            if hit is None:
                unmatched.append(di)
                continue
            if di.image_id in matched_ids:
                continue
            matched_ids.add(di.image_id)
            matches.append(FileMatch(digikam=di, nextcloud=hit, method="path"))

        file_ids = [m.nextcloud.file_id for m in matches]
        faces_by_file = self.list_detections_for_files(
            file_ids, {m.nextcloud.file_id: m.nextcloud for m in matches}
        )
        return matches, unmatched, faces_by_file

    def list_detections_for_files(
        self,
        file_ids: Iterable[int],
        files_by_id: Optional[dict[int, NextcloudFile]] = None,
    ) -> dict[int, list[FaceRegion]]:
        del files_by_id
        ids = list(file_ids)
        if not ids:
            return {}
        LOG.debug("Loading Recognize detections for %d files from DB…", len(ids))
        det = self.t("recognize_face_detections")
        cl = self.t("recognize_face_clusters")
        by_file: dict[int, list[FaceRegion]] = {i: [] for i in ids}
        chunk_size = 400
        for i in range(0, len(ids), chunk_size):
            chunk = ids[i : i + chunk_size]
            placeholders = ",".join("?" * len(chunk))
            sql = f"""
                SELECT
                    d.id AS detection_id,
                    d.file_id,
                    d.x, d.y, d.height, d.width,
                    d.cluster_id,
                    d.face_vector,
                    d.threshold,
                    COALESCE(c.title, '') AS title
                FROM {det} d
                LEFT JOIN {cl} c ON c.id = d.cluster_id
                WHERE d.user_id = ?
                  AND d.file_id IN ({placeholders})
            """
            for r in self._fetchall(sql, [self.user_id, *chunk]):
                vec = r.get("face_vector")
                if isinstance(vec, str):
                    try:
                        vec = json.loads(vec)
                    except json.JSONDecodeError:
                        vec = None
                cluster_id = r["cluster_id"]
                title = (r["title"] or "").strip()
                person = title
                dav_parent = ""
                if cluster_id is None:
                    person = ""
                    dav_parent = ""
                elif int(cluster_id) < 0:
                    person = ""
                    dav_parent = "unassigned-faces"
                else:
                    person = title or f"cluster:{cluster_id}"
                    dav_parent = title if title else str(int(cluster_id))

                by_file[int(r["file_id"])].append(
                    FaceRegion(
                        person=person if title else "",
                        rect=Rect(
                            float(r["x"]),
                            float(r["y"]),
                            float(r["width"]),
                            float(r["height"]),
                        ).clamp(),
                        source="nextcloud",
                        nc_file_id=int(r["file_id"]),
                        nc_detection_id=int(r["detection_id"]),
                        nc_cluster_id=(
                            int(cluster_id) if cluster_id is not None else None
                        ),
                        dav_parent=dav_parent,
                        face_vector=vec if isinstance(vec, list) else None,
                        threshold=float(r["threshold"] or 0.0),
                    )
                )
        LOG.debug(
            "Loaded %d detections across %d files",
            sum(len(v) for v in by_file.values()),
            len(ids),
        )
        return by_file

    def assign_person(
        self,
        detection: FaceRegion,
        person: str,
        nc_file: NextcloudFile,
        cluster_id: int,
    ) -> None:
        del nc_file
        # person sanitized for API consistency; cluster_id is source of truth in DB
        _ = sanitize_person_name(person)
        if detection.nc_detection_id is None:
            raise ValueError("detection id required for DB assign")
        det = self.t("recognize_face_detections")
        self._execute(
            f"UPDATE {det} SET cluster_id = ? WHERE id = ? AND user_id = ?",
            (cluster_id, detection.nc_detection_id, self.user_id),
        )

    def insert_detection(
        self,
        file_id: int,
        rect: Rect,
        cluster_id: int,
        person: Optional[str] = None,
        face_vector: Optional[list[float]] = None,
        threshold: float = 0.0,
        confirmed: bool = False,
    ) -> int:
        del person, confirmed
        det = self.t("recognize_face_detections")
        vec = (
            face_vector
            if face_vector and len(face_vector) == FACE_VECTOR_DIM
            else zero_vector()
        )
        vec_json = json.dumps(vec)
        if self.driver == "postgres":
            cur = self._execute(
                f"""
                INSERT INTO {det}
                    (user_id, file_id, x, y, height, width, face_vector, cluster_id, threshold)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING id
                """,
                (
                    self.user_id,
                    file_id,
                    rect.x,
                    rect.y,
                    rect.h,
                    rect.w,
                    vec_json,
                    cluster_id,
                    threshold,
                ),
            )
            row = cur.fetchone()
            return int(row["id"] if isinstance(row, dict) else row[0])

        cur = self._execute(
            f"""
            INSERT INTO {det}
                (user_id, file_id, x, y, height, width, face_vector, cluster_id, threshold)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.user_id,
                file_id,
                rect.x,
                rect.y,
                rect.h,
                rect.w,
                vec_json,
                cluster_id,
                threshold,
            ),
        )
        return int(cur.lastrowid)

    def sample_cluster_vector(self, cluster_id: int) -> Optional[list[float]]:
        if cluster_id in self._vector_cache:
            return self._vector_cache[cluster_id]
        det = self.t("recognize_face_detections")
        rows = self._fetchall(
            f"""
            SELECT face_vector FROM {det}
            WHERE cluster_id = ? AND user_id = ?
            LIMIT 20
            """,
            (cluster_id, self.user_id),
        )
        result: Optional[list[float]] = None
        for r in rows:
            vec = r.get("face_vector")
            if isinstance(vec, str):
                try:
                    vec = json.loads(vec)
                except json.JSONDecodeError:
                    continue
            if isinstance(vec, list) and len(vec) == FACE_VECTOR_DIM:
                if any(float(v) != 0.0 for v in vec):
                    result = [float(v) for v in vec]
                    break
        self._vector_cache[cluster_id] = result
        return result
