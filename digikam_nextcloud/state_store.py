"""SQLite state shared by manual runs, the UI, and the future service."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL DEFAULT 'Default',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    profile_id INTEGER NOT NULL REFERENCES profiles(id),
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT,
    summary_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS face_links (
    id INTEGER PRIMARY KEY,
    profile_id INTEGER NOT NULL REFERENCES profiles(id),
    digikam_image_id INTEGER NOT NULL,
    digikam_tag_id INTEGER,
    nextcloud_file_id INTEGER NOT NULL,
    nextcloud_detection_id INTEGER,
    digikam_name TEXT NOT NULL,
    nextcloud_name TEXT NOT NULL,
    digikam_rect_json TEXT NOT NULL,
    nextcloud_rect_json TEXT NOT NULL,
    last_synced_at TEXT,
    UNIQUE(profile_id, digikam_image_id, digikam_tag_id, nextcloud_file_id, nextcloud_detection_id)
);
CREATE TABLE IF NOT EXISTS conflicts (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    face_link_id INTEGER REFERENCES face_links(id),
    status TEXT NOT NULL DEFAULT 'open',
    resolution TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY,
    run_id INTEGER REFERENCES runs(id),
    event_type TEXT NOT NULL,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    target TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    read_at TEXT
);
"""


class StateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.conn.executescript(SCHEMA)
        self.conn.execute("INSERT OR IGNORE INTO profiles(id, name) VALUES (1, 'Default')")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def create_notification(
        self,
        event_type: str,
        title: str,
        message: str,
        target: str,
        *,
        run_id: int | None = None,
    ) -> int:
        with self.lock:
            cursor = self.conn.execute(
                """INSERT INTO notifications(run_id, event_type, title, message, target)
                   VALUES (?, ?, ?, ?, ?)""",
                (run_id, event_type, title, message, target),
            )
            self.conn.commit()
            return int(cursor.lastrowid)

    def unread_notifications(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM notifications WHERE read_at IS NULL ORDER BY id DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def create_run(self, mode: str, status: str = "previewing") -> int:
        with self.lock:
            cursor = self.conn.execute(
                "INSERT INTO runs(profile_id, mode, status) VALUES (1, ?, ?)",
                (mode, status),
            )
            self.conn.commit()
            return int(cursor.lastrowid)

    def finish_run(self, run_id: int, status: str, summary: dict[str, Any]) -> None:
        with self.lock:
            self.conn.execute(
                """UPDATE runs SET status = ?, summary_json = ?,
                   finished_at = CURRENT_TIMESTAMP WHERE id = ?""",
                (status, json.dumps(summary), run_id),
            )
            self.conn.commit()
