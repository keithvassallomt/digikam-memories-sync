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
CREATE TABLE IF NOT EXISTS run_progress (
    run_id INTEGER PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    phase TEXT NOT NULL,
    current INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0,
    detail_json TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS run_results (
    run_id INTEGER PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    result_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS apply_runs (
    run_id INTEGER PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    backup_path TEXT,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS run_actions (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    target TEXT NOT NULL,
    operation TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    action_json TEXT NOT NULL,
    result_json TEXT,
    error TEXT,
    applied_at TEXT,
    UNIQUE(run_id, position)
);
CREATE INDEX IF NOT EXISTS run_actions_status_idx ON run_actions(run_id, status, position);
CREATE TABLE IF NOT EXISTS ignored_faces (
    id INTEGER PRIMARY KEY,
    profile_id INTEGER NOT NULL REFERENCES profiles(id),
    source TEXT NOT NULL,
    path TEXT NOT NULL,
    person TEXT NOT NULL,
    rect_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(profile_id, source, path, person, rect_json)
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

    def recover_interrupted_applies(self) -> int:
        """Turn jobs abandoned by a stopped local service into resumable failures."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT id FROM runs WHERE status = 'applying'"
            ).fetchall()
            run_ids = [int(row[0]) for row in rows]
            if run_ids:
                placeholders = ",".join("?" * len(run_ids))
                self.conn.execute(
                    f"""UPDATE runs SET status = 'apply_failed', finished_at = CURRENT_TIMESTAMP
                        WHERE id IN ({placeholders})""",
                    run_ids,
                )
                self.conn.execute(
                    f"""UPDATE run_progress SET phase = 'apply_failed',
                        error = 'Face Sync stopped before Apply finished.'
                        WHERE run_id IN ({placeholders})""",
                    run_ids,
                )
                self.conn.commit()
            return len(run_ids)

    def latest_actionable_run(self) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute(
                """SELECT id FROM runs
                   WHERE status IN ('previewing', 'previewed', 'applying', 'apply_failed')
                   ORDER BY id DESC LIMIT 1"""
            ).fetchone()
        return self.run(int(row[0])) if row is not None else None

    def create_run(self, mode: str, status: str = "previewing") -> int:
        with self.lock:
            cursor = self.conn.execute(
                "INSERT INTO runs(profile_id, mode, status) VALUES (1, ?, ?)",
                (mode, status),
            )
            run_id = int(cursor.lastrowid)
            self.conn.execute(
                "INSERT INTO run_progress(run_id, phase) VALUES (?, 'starting')",
                (run_id,),
            )
            self.conn.commit()
            return run_id

    def update_progress(
        self,
        run_id: int,
        phase: str,
        current: int,
        total: int,
        detail: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with self.lock:
            self.conn.execute(
                """UPDATE run_progress SET phase = ?, current = ?, total = ?,
                   detail_json = ?, error = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE run_id = ?""",
                (phase, current, total, json.dumps(detail or {}), error, run_id),
            )
            self.conn.commit()

    def save_result(self, run_id: int, result: dict[str, Any]) -> None:
        with self.lock:
            self.conn.execute(
                """INSERT INTO run_results(run_id, result_json) VALUES (?, ?)
                   ON CONFLICT(run_id) DO UPDATE SET result_json = excluded.result_json""",
                (run_id, json.dumps(result)),
            )
            self.conn.commit()

    def finish_run(self, run_id: int, status: str, summary: dict[str, Any]) -> None:
        with self.lock:
            self.conn.execute(
                """UPDATE runs SET status = ?, summary_json = ?,
                   finished_at = CURRENT_TIMESTAMP WHERE id = ?""",
                (status, json.dumps(summary), run_id),
            )
            self.conn.commit()

    def initialize_apply(self, run_id: int, plan: list[dict[str, Any]]) -> None:
        with self.lock:
            existing = self.conn.execute(
                "SELECT COUNT(*) FROM run_actions WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
            if existing and int(existing) != len(plan):
                raise ValueError("The saved Apply plan does not match this preview.")
            if existing:
                saved = self.conn.execute(
                    """SELECT id,action_json,result_json FROM run_actions
                       WHERE run_id = ? ORDER BY position""",
                    (run_id,),
                ).fetchall()
                plan_matches = True
                confirmed_upgrades: list[tuple[str, int]] = []
                for row, original in zip(saved, plan, strict=True):
                    current = json.loads(row["action_json"])
                    if current == original:
                        continue
                    result = json.loads(row["result_json"] or "{}")
                    source_rect_name = (
                        "digikam_rect"
                        if current.get("operation") == "insert_memories"
                        else "nextcloud_rect"
                    )
                    source_rect = current.get(source_rect_name)
                    current_without_rect = {
                        k: v
                        for k, v in current.items()
                        if k not in {"rect", source_rect_name, "confirmed_face"}
                    }
                    original_without_rect = {k: v for k, v in original.items() if k != "rect"}
                    if not (
                        result.get("review") == "adjusted"
                        and source_rect == original.get("rect")
                        and current_without_rect == original_without_rect
                    ):
                        plan_matches = False
                        break
                    if (
                        current.get("operation") == "insert_memories"
                        and current.get("confirmed_face") is not True
                    ):
                        current["confirmed_face"] = True
                        confirmed_upgrades.append((json.dumps(current), int(row["id"])))
                if not plan_matches:
                    raise ValueError("The saved Apply plan does not match this preview.")
                if confirmed_upgrades:
                    self.conn.executemany(
                        "UPDATE run_actions SET action_json = ? WHERE id = ?",
                        confirmed_upgrades,
                    )
            if not existing:
                self.conn.executemany(
                    """INSERT INTO run_actions(run_id, position, target, operation, action_json)
                       VALUES (?, ?, ?, ?, ?)""",
                    [
                        (run_id, position, action["target"], action["operation"], json.dumps(action))
                        for position, action in enumerate(plan)
                    ],
                )
            self.conn.execute(
                "INSERT OR IGNORE INTO apply_runs(run_id) VALUES (?)", (run_id,)
            )
            self.conn.execute(
                """UPDATE run_actions SET status = 'pending', error = NULL
                   WHERE run_id = ? AND status = 'failed'""",
                (run_id,),
            )
            self.conn.execute(
                "UPDATE runs SET status = 'applying', finished_at = NULL WHERE id = ?",
                (run_id,),
            )
            self.conn.commit()

    def set_apply_backup(self, run_id: int, path: str) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE apply_runs SET backup_path = ? WHERE run_id = ?", (path, run_id)
            )
            self.conn.commit()

    def apply_backup(self, run_id: int) -> str | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT backup_path FROM apply_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return str(row[0]) if row is not None and row[0] else None

    def pending_apply_actions(self, run_id: int) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                """SELECT id, position, action_json FROM run_actions
                   WHERE run_id = ? AND status = 'pending' ORDER BY position""",
                (run_id,),
            ).fetchall()
        return [
            {"id": int(row["id"]), "position": int(row["position"]), "action": json.loads(row["action_json"])}
            for row in rows
        ]

    def finish_apply_action(
        self, action_id: int, status: str, *, result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        if status not in {"applied", "failed", "ignored"}:
            raise ValueError("Invalid action status.")
        with self.lock:
            self.conn.execute(
                """UPDATE run_actions SET status = ?, result_json = ?, error = ?,
                   applied_at = CASE WHEN ? = 'applied' THEN CURRENT_TIMESTAMP ELSE applied_at END
                   WHERE id = ?""",
                (status, json.dumps(result) if result is not None else None, error, status, action_id),
            )
            self.conn.commit()

    def apply_counts(self, run_id: int) -> dict[str, int]:
        with self.lock:
            rows = self.conn.execute(
                """SELECT status, target, COUNT(*) AS amount FROM run_actions
                   WHERE run_id = ? GROUP BY status, target""",
                (run_id,),
            ).fetchall()
        counts = {
            "total": 0, "applied": 0, "failed": 0, "pending": 0, "ignored": 0,
            "memories": 0, "digikam": 0,
        }
        for row in rows:
            amount = int(row["amount"])
            counts["total"] += amount
            counts[str(row["status"])] += amount
            counts[str(row["target"])] += amount
        return counts

    def remaining_apply_counts(self, run_id: int) -> dict[str, int]:
        with self.lock:
            rows = self.conn.execute(
                """SELECT target,COUNT(*) AS amount FROM run_actions
                   WHERE run_id = ? AND status IN ('pending','failed') GROUP BY target""",
                (run_id,),
            ).fetchall()
        result = {"total": 0, "memories": 0, "digikam": 0}
        for row in rows:
            amount = int(row["amount"])
            result["total"] += amount
            result[str(row["target"])] += amount
        return result

    @staticmethod
    def _ignored_face_key(action: dict[str, Any]) -> tuple[str, str, str, str] | None:
        operation = str(action.get("operation") or action.get("action") or "")
        if operation in {"insert_memories", "insert"}:
            source = "digikam"
        elif operation == "create_digikam":
            source = "memories"
        else:
            return None
        try:
            source_rect = (
                action.get("digikam_rect", action.get("rect"))
                if source == "digikam"
                else action.get("nextcloud_rect", action.get("rect"))
            )
            rect = [round(float(value), 8) for value in source_rect]
        except (KeyError, TypeError, ValueError):
            return None
        if len(rect) != 4:
            return None
        return (
            source,
            str(action.get("path", "")),
            str(action.get("person", "")),
            json.dumps(rect, separators=(",", ":")),
        )

    def filter_ignored_actions(
        self, actions: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT source,path,person,rect_json FROM ignored_faces WHERE profile_id = 1"
            ).fetchall()
        ignored = {
            (str(row["source"]), str(row["path"]), str(row["person"]), str(row["rect_json"]))
            for row in rows
        }
        kept = [action for action in actions if self._ignored_face_key(action) not in ignored]
        skipped = [action for action in actions if self._ignored_face_key(action) in ignored]
        return kept, skipped

    def failed_apply_actions(self, run_id: int) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                """SELECT id,position,target,operation,action_json,error FROM run_actions
                   WHERE run_id = ? AND status = 'failed' ORDER BY position""",
                (run_id,),
            ).fetchall()
        failures = []
        for row in rows:
            action = json.loads(row["action_json"])
            operation = str(row["operation"])
            source = "digikam" if str(row["target"]) == "memories" else "memories"
            failures.append({
                "id": int(row["id"]),
                "position": int(row["position"]),
                "operation": operation,
                "source": source,
                "destination": "memories" if source == "digikam" else "digikam",
                "path": str(action.get("path", "")),
                "person": str(action.get("person", "")),
                "rect": action.get("rect"),
                "nc_file_id": action.get("nc_file_id"),
                "error": str(row["error"] or "Unknown error"),
                "reviewable": operation in {"insert_memories", "create_digikam"},
            })
        return failures

    def failed_apply_action(self, run_id: int, action_id: int) -> dict[str, Any]:
        for failure in self.failed_apply_actions(run_id):
            if failure["id"] == action_id:
                return failure
        raise ValueError("Failed face change not found.")

    def resolve_failed_action(
        self,
        run_id: int,
        action_id: int,
        decision: str,
        *,
        rect: list[float] | None = None,
        apply_to_remaining: bool = False,
    ) -> dict[str, Any]:
        if decision not in {"keep_source", "retry"}:
            raise ValueError("Choose whether to keep this face here or retry it.")
        with self.lock:
            run = self.conn.execute(
                "SELECT status FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if run is None or run["status"] != "apply_failed":
                raise ValueError("This run has no failed changes to review.")
            row = self.conn.execute(
                """SELECT id,operation,action_json,error FROM run_actions
                   WHERE id = ? AND run_id = ? AND status = 'failed'""",
                (action_id, run_id),
            ).fetchone()
            if row is None:
                raise ValueError("Failed face change not found.")

            if decision == "retry":
                if rect is None or len(rect) != 4:
                    raise ValueError("The adjusted face box is invalid.")
                values = [float(value) for value in rect]
                x, y, width, height = values
                if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1 or y + height > 1:
                    raise ValueError("The adjusted face box must stay inside the photo.")
                action = json.loads(row["action_json"])
                if str(row["operation"]) == "insert_memories":
                    action.setdefault("digikam_rect", action["rect"])
                    action["confirmed_face"] = True
                elif str(row["operation"]) == "create_digikam":
                    action.setdefault("nextcloud_rect", action["rect"])
                action["rect"] = values
                self.conn.execute(
                    """UPDATE run_actions SET status = 'pending', action_json = ?,
                       result_json = '{"review":"adjusted"}', error = NULL
                       WHERE id = ?""",
                    (json.dumps(action), action_id),
                )
            else:
                if self._ignored_face_key(
                    {"operation": row["operation"], **json.loads(row["action_json"])}
                ) is None:
                    raise ValueError("This failure must be retried rather than kept one-sided.")
                if apply_to_remaining:
                    rows = self.conn.execute(
                        """SELECT id,operation,action_json,error FROM run_actions
                           WHERE run_id = ? AND status = 'failed'
                           AND operation IN ('insert_memories','create_digikam')""",
                        (run_id,),
                    ).fetchall()
                else:
                    rows = [row]
                for candidate in rows:
                    action = json.loads(candidate["action_json"])
                    key = self._ignored_face_key({"operation": candidate["operation"], **action})
                    if key is None:
                        continue
                    self.conn.execute(
                        """INSERT OR IGNORE INTO ignored_faces(
                               profile_id,source,path,person,rect_json,reason)
                           VALUES (1,?,?,?,?,?)""",
                        (*key, str(candidate["error"] or "Kept in source library")),
                    )
                    self.conn.execute(
                        "UPDATE run_actions SET status = 'ignored', error = NULL WHERE id = ?",
                        (int(candidate["id"]),),
                    )
            self.conn.commit()
        return self.failure_review(run_id)

    def failure_review(self, run_id: int) -> dict[str, Any]:
        failures = self.failed_apply_actions(run_id)
        counts = self.apply_counts(run_id)
        return {
            "run_id": run_id,
            "failures": failures,
            "remaining": len(failures),
            "pending": counts["pending"],
            "ignored": counts["ignored"],
            "applied": counts["applied"],
        }

    def apply_failures(self, run_id: int, limit: int = 10) -> list[dict[str, str]]:
        with self.lock:
            rows = self.conn.execute(
                """SELECT action_json, error FROM run_actions
                   WHERE run_id = ? AND status = 'failed'
                   ORDER BY position LIMIT ?""",
                (run_id, limit),
            ).fetchall()
        failures = []
        for row in rows:
            action = json.loads(row["action_json"])
            failures.append(
                {
                    "path": str(action.get("path", "")),
                    "operation": str(action.get("operation", "")),
                    "error": str(row["error"] or "Unknown error"),
                }
            )
        return failures

    def finish_apply(self, run_id: int, status: str) -> dict[str, Any]:
        counts = self.apply_counts(run_id)
        with self.lock:
            self.conn.execute(
                "UPDATE runs SET status = ?, finished_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, run_id),
            )
            self.conn.execute(
                "UPDATE apply_runs SET finished_at = CURRENT_TIMESTAMP WHERE run_id = ?",
                (run_id,),
            )
            self.conn.commit()
        return {**counts, "backup_path": self.apply_backup(run_id)}

    def save_face_link(self, link: dict[str, Any]) -> None:
        values = (
            int(link["digikam_image_id"]), int(link["digikam_tag_id"]),
            int(link["nextcloud_file_id"]), int(link["nextcloud_detection_id"]),
        )
        with self.lock:
            row = self.conn.execute(
                """SELECT id FROM face_links WHERE profile_id = 1
                   AND digikam_image_id = ? AND digikam_tag_id = ?
                   AND nextcloud_file_id = ? AND nextcloud_detection_id = ?""",
                values,
            ).fetchone()
            payload = (
                str(link["digikam_name"]), str(link["nextcloud_name"]),
                json.dumps(link["digikam_rect"]), json.dumps(link["nextcloud_rect"]),
            )
            if row is None:
                self.conn.execute(
                    """INSERT INTO face_links(
                           profile_id, digikam_image_id, digikam_tag_id,
                           nextcloud_file_id, nextcloud_detection_id,
                           digikam_name, nextcloud_name, digikam_rect_json,
                           nextcloud_rect_json, last_synced_at)
                       VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                    (*values, *payload),
                )
            else:
                self.conn.execute(
                    """UPDATE face_links SET digikam_name = ?, nextcloud_name = ?,
                       digikam_rect_json = ?, nextcloud_rect_json = ?,
                       last_synced_at = CURRENT_TIMESTAMP WHERE id = ?""",
                    (*payload, int(row[0])),
                )
            self.conn.commit()

    def save_conflicts(self, run_id: int, conflicts: list[dict[str, Any]]) -> None:
        with self.lock:
            self.conn.executemany(
                "INSERT INTO conflicts(run_id, detail_json) VALUES (?, ?)",
                [(run_id, json.dumps(conflict)) for conflict in conflicts],
            )
            self.conn.commit()

    def conflicts_for_run(self, run_id: int) -> dict[str, Any]:
        with self.lock:
            run = self.conn.execute("SELECT id FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run is None:
                raise ValueError("Preview run not found.")
            rows = self.conn.execute(
                "SELECT * FROM conflicts WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
        conflicts = []
        for row in rows:
            item = json.loads(row["detail_json"] or "{}")
            item.update(
                {
                    "id": int(row["id"]),
                    "run_id": int(row["run_id"]),
                    "status": row["status"],
                    "resolution": row["resolution"],
                }
            )
            conflicts.append(item)
        resolved = sum(item["status"] == "resolved" for item in conflicts)
        return {
            "conflicts": conflicts,
            "total": len(conflicts),
            "resolved": resolved,
            "remaining": len(conflicts) - resolved,
        }

    def conflict_for_run(self, run_id: int, conflict_id: int) -> dict[str, Any]:
        result = self.conflicts_for_run(run_id)
        for conflict in result["conflicts"]:
            if conflict["id"] == conflict_id:
                return conflict
        raise ValueError("Conflict not found.")

    def resolve_conflict(
        self,
        run_id: int,
        conflict_id: int,
        resolution: str,
        *,
        apply_to_remaining: bool = False,
    ) -> dict[str, Any]:
        if resolution not in {"digikam", "memories"}:
            raise ValueError("Choose either digiKam or Memories.")
        with self.lock:
            row = self.conn.execute(
                """SELECT c.id, r.status FROM conflicts c
                   JOIN runs r ON r.id = c.run_id
                   WHERE c.id = ? AND c.run_id = ?""",
                (conflict_id, run_id),
            ).fetchone()
            if row is None:
                raise ValueError("Conflict not found.")
            if row["status"] not in {"previewing", "previewed"}:
                raise ValueError("Conflict decisions cannot change after Apply has started.")
            self.conn.execute(
                """UPDATE conflicts SET status = 'resolved', resolution = ?
                   WHERE id = ? AND run_id = ?""",
                (resolution, conflict_id, run_id),
            )
            if apply_to_remaining:
                self.conn.execute(
                    """UPDATE conflicts SET status = 'resolved', resolution = ?
                       WHERE run_id = ? AND status = 'open'""",
                    (resolution, run_id),
                )
            self.conn.commit()
        return self.conflicts_for_run(run_id)

    def run(self, run_id: int) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute(
                """SELECT r.*, p.phase, p.current, p.total, p.detail_json,
                          p.error, rr.result_json
                   FROM runs r
                   LEFT JOIN run_progress p ON p.run_id = r.id
                   LEFT JOIN run_results rr ON rr.run_id = r.id
                   WHERE r.id = ?""",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["summary"] = json.loads(result.pop("summary_json"))
        result["progress"] = {
            "phase": result.pop("phase"),
            "current": result.pop("current"),
            "total": result.pop("total"),
            **json.loads(result.pop("detail_json") or "{}"),
        }
        raw_result = result.pop("result_json")
        result["result"] = json.loads(raw_result) if raw_result else None
        counts = self.apply_counts(run_id)
        result["apply"] = {
            **counts,
            "backup_path": self.apply_backup(run_id),
            "errors": self.apply_failures(run_id),
        } if counts["total"] or self.apply_backup(run_id) else None
        return result
