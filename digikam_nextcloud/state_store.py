"""SQLite state shared by manual runs, the UI, and the future service."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any


LOG_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}

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
CREATE TABLE IF NOT EXISTS log_entries (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    logger TEXT NOT NULL,
    run_id INTEGER,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS log_entries_ts ON log_entries(ts);
CREATE INDEX IF NOT EXISTS log_entries_run ON log_entries(run_id);
CREATE TABLE IF NOT EXISTS service_state (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS run_checkpoints (
    run_id INTEGER PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    phase TEXT NOT NULL,
    cursor_json TEXT NOT NULL DEFAULT '{}',
    counters_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS preview_actions (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    action_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS preview_actions_run ON preview_actions(run_id);
"""

# Columns added after the first release. Existing databases are upgraded in
# place, because CREATE TABLE IF NOT EXISTS cannot add a column.
ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "runs": {
        "trigger": "TEXT NOT NULL DEFAULT 'manual'",
        "auto_apply": "INTEGER NOT NULL DEFAULT 0",
        "waiting_reason": "TEXT",
        "next_attempt_at": "TEXT",
        "attempts": "INTEGER NOT NULL DEFAULT 0",
        # The person a scoped run was for, so a resumed run covers the same
        # ground rather than quietly widening to the whole library.
        "person": "TEXT",
    },
    "notifications": {
        "channel": "TEXT",
        "delivered_at": "TEXT",
    },
    "face_links": {
        # The one name both libraries last agreed on for this face.
        "synced_name": "TEXT",
    },
    "conflicts": {
        # Stable identity so the same disagreement is not raised twice.
        "identity_key": "TEXT",
        # The run whose plan already carries this decision, if any.
        "decided_run_id": "INTEGER",
        # The last run that reported this disagreement. Lets a full run close
        # the ones it no longer sees, without holding them all in memory.
        "last_seen_run_id": "INTEGER",
    },
}

ADDED_INDEXES = (
    "CREATE INDEX IF NOT EXISTS face_links_remote"
    " ON face_links(nextcloud_file_id, nextcloud_detection_id)",
    "CREATE INDEX IF NOT EXISTS conflicts_identity"
    " ON conflicts(identity_key, status)",
)


class StateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.conn.executescript(SCHEMA)
        self._add_missing_columns()
        for statement in ADDED_INDEXES:
            self.conn.execute(statement)
        self.conn.execute("INSERT OR IGNORE INTO profiles(id, name) VALUES (1, 'Default')")
        self.conn.commit()

    def _add_missing_columns(self) -> None:
        added: set[tuple[str, str]] = set()
        for table, columns in ADDED_COLUMNS.items():
            existing = {
                str(row["name"])
                for row in self.conn.execute(f"PRAGMA table_info({table})")
            }
            for name, definition in columns.items():
                if name not in existing:
                    self.conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
                    )
                    added.add((table, name))
        self._backfill(added)

    def _backfill(self, added: set[tuple[str, str]]) -> None:
        """Give a newly added column the meaning it would have had all along.

        Only runs the once, when the column first appears, so reopening the
        database never redoes it.
        """
        if ("conflicts", "decided_run_id") in added:
            # Decisions recorded before this column existed were carried by
            # their own run. Without this they would look unapplied and be
            # gathered into a pointless follow-up run.
            self.conn.execute(
                """UPDATE conflicts SET decided_run_id = run_id
                   WHERE status = 'resolved'
                     AND resolution IN ('digikam', 'memories')"""
            )
        if ("face_links", "synced_name") in added:
            # Links written by an applied change already record one agreed
            # name under two columns. Adopting it seeds the ledger, so the
            # first run after upgrading does not re-ask about faces Face Sync
            # synced itself.
            self.conn.execute(
                """UPDATE face_links SET synced_name = digikam_name
                   WHERE synced_name IS NULL
                     AND digikam_name IS NOT NULL AND digikam_name <> ''
                     AND lower(digikam_name) = lower(nextcloud_name)"""
            )

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

    def recover_unfinished_runs(self) -> dict[str, int]:
        """Re-queue everything the last service was in the middle of.

        Nothing is thrown away. A preview resumes from its checkpoint, an
        apply resumes from its journal, and both are marked as waiting to be
        picked up rather than failed.
        """
        with self.lock:
            rows = self.conn.execute(
                """SELECT id, status FROM runs
                   WHERE status IN ('queued', 'previewing', 'applying', 'deferred')"""
            ).fetchall()
            found = [(int(row["id"]), str(row["status"])) for row in rows]
            if found:
                run_ids = [run_id for run_id, _ in found]
                placeholders = ",".join("?" * len(run_ids))
                self.conn.execute(
                    f"""UPDATE runs SET status = 'waiting', waiting_reason = 'resume',
                        next_attempt_at = NULL, finished_at = NULL
                        WHERE id IN ({placeholders})""",
                    run_ids,
                )
                self.conn.execute(
                    f"""UPDATE run_progress SET phase = 'waiting',
                        error = 'Face Sync restarted. This will continue where it stopped.'
                        WHERE run_id IN ({placeholders})""",
                    run_ids,
                )
                self.conn.commit()
        counts: dict[str, int] = {}
        for _, status in found:
            counts[status] = counts.get(status, 0) + 1
        counts["total"] = len(found)
        return counts

    def resume_apply(self, run_id: int) -> None:
        """Put a parked apply back to work without rebuilding its plan."""
        with self.lock:
            self.conn.execute(
                """UPDATE runs SET status = 'applying', waiting_reason = NULL,
                   next_attempt_at = NULL, finished_at = NULL WHERE id = ?""",
                (run_id,),
            )
            self.conn.commit()

    def resume_preview(self, run_id: int) -> None:
        with self.lock:
            self.conn.execute(
                """UPDATE runs SET status = 'previewing', waiting_reason = NULL,
                   next_attempt_at = NULL, finished_at = NULL WHERE id = ?""",
                (run_id,),
            )
            self.conn.commit()

    def auto_apply_candidate(self) -> int | None:
        """The newest finished preview that was told to apply itself."""
        with self.lock:
            row = self.conn.execute(
                """SELECT id FROM runs
                   WHERE status = 'previewed' AND auto_apply = 1
                   ORDER BY id DESC LIMIT 1"""
            ).fetchone()
        return int(row[0]) if row is not None else None

    def runs_awaiting_work(self) -> list[dict[str, Any]]:
        """Runs the coordinator should pick up, oldest first."""
        with self.lock:
            rows = self.conn.execute(
                """SELECT id, mode, status, trigger, auto_apply, waiting_reason,
                          next_attempt_at, attempts
                   FROM runs WHERE status IN ('queued', 'waiting', 'deferred')
                   ORDER BY id"""
            ).fetchall()
        return [dict(row) for row in rows]

    def set_waiting(
        self,
        run_id: int,
        reason: str,
        *,
        next_attempt_at: str | None = None,
        count_attempt: bool = False,
    ) -> None:
        """Park a run until something changes, without losing its progress."""
        with self.lock:
            self.conn.execute(
                f"""UPDATE runs SET status = 'waiting', waiting_reason = ?,
                    next_attempt_at = ?
                    {', attempts = attempts + 1' if count_attempt else ''}
                    WHERE id = ?""",
                (reason, next_attempt_at, run_id),
            )
            self.conn.commit()

    def set_deferred(self, run_id: int, reason: str) -> None:
        with self.lock:
            self.conn.execute(
                """UPDATE runs SET status = 'deferred', waiting_reason = ?,
                   next_attempt_at = NULL WHERE id = ?""",
                (reason, run_id),
            )
            self.conn.commit()

    def clear_waiting(self, run_id: int) -> None:
        with self.lock:
            self.conn.execute(
                """UPDATE runs SET waiting_reason = NULL, next_attempt_at = NULL
                   WHERE id = ?""",
                (run_id,),
            )
            self.conn.commit()

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

    def recover_interrupted_previews(self) -> int:
        """Mark previews abandoned by a stopped local service as failed."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT id FROM runs WHERE status = 'previewing'"
            ).fetchall()
            run_ids = [int(row[0]) for row in rows]
            if run_ids:
                placeholders = ",".join("?" * len(run_ids))
                self.conn.execute(
                    f"""UPDATE runs SET status = 'failed', finished_at = CURRENT_TIMESTAMP
                        WHERE id IN ({placeholders})""",
                    run_ids,
                )
                self.conn.execute(
                    f"""UPDATE run_progress SET phase = 'failed',
                        error = 'Face Sync stopped before the preview finished.'
                        WHERE run_id IN ({placeholders})""",
                    run_ids,
                )
                self.conn.commit()
            return len(run_ids)

    def discard_preview(self, run_id: int) -> None:
        """Discard a saved preview while retaining it in run history."""
        with self.lock:
            row = self.conn.execute(
                "SELECT status FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Preview run not found.")
            if row["status"] != "previewed":
                raise ValueError("Only a completed preview can be discarded.")
            # Older completed previews have already been superseded by this one.
            # Mark them too so none can unexpectedly reappear later.
            self.conn.execute(
                """UPDATE runs SET status = 'discarded',
                   finished_at = COALESCE(finished_at, CURRENT_TIMESTAMP)
                   WHERE id <= ? AND status = 'previewed'""",
                (run_id,),
            )
            # The proposed changes go with it. Keeping them would leave rows
            # nothing can ever apply.
            self.conn.execute(
                "DELETE FROM preview_actions WHERE run_id <= ?", (run_id,)
            )
            self.conn.execute(
                "DELETE FROM run_checkpoints WHERE run_id <= ?", (run_id,)
            )
            self.conn.commit()

    def latest_actionable_run(self) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute(
                """SELECT id FROM runs
                   WHERE status IN ('queued', 'waiting', 'previewing', 'previewed',
                                    'applying', 'deferred', 'apply_failed')
                   ORDER BY id DESC LIMIT 1"""
            ).fetchone()
        return self.run(int(row[0])) if row is not None else None

    def create_run(
        self,
        mode: str,
        status: str = "previewing",
        *,
        trigger: str = "manual",
        auto_apply: bool = False,
        person: str = "",
    ) -> int:
        with self.lock:
            cursor = self.conn.execute(
                """INSERT INTO runs(profile_id, mode, status, trigger, auto_apply, person)
                   VALUES (1, ?, ?, ?, ?, ?)""",
                (mode, status, trigger, int(auto_apply), person or None),
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
            # These decisions are in this run's journal now, so a later
            # follow-up run must not apply them a second time.
            self.conn.execute(
                """UPDATE conflicts SET decided_run_id = ?
                   WHERE run_id = ? AND status = 'resolved'
                     AND resolution IN ('digikam', 'memories')
                     AND decided_run_id IS NULL""",
                (run_id, run_id),
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

    @staticmethod
    def conflict_identity(conflict: dict[str, Any]) -> str:
        """A key for the same disagreement seen in a later run.

        The Memories detection is stable across renames on either side. When
        it is missing, the photo and the two rectangles stand in for it.
        """
        file_id = conflict.get("nc_file_id")
        detection_id = conflict.get("nc_detection_id")
        if file_id is not None and detection_id is not None:
            return f"detection:{int(file_id)}:{int(detection_id)}"
        rects = json.dumps(
            [conflict.get("digikam_rect"), conflict.get("nextcloud_rect")],
            separators=(",", ":"),
        )
        return f"rect:{conflict.get('path', '')}:{rects}"

    def save_conflicts(
        self,
        run_id: int,
        conflicts: list[dict[str, Any]],
        *,
        close_unseen: bool = False,
    ) -> dict[str, int]:
        """Record this run's conflicts without re-asking settled questions.

        A disagreement already open from an earlier run is refreshed, not
        duplicated. When a full run no longer reports one, it was settled in
        one of the libraries and is closed.
        """
        seen: dict[str, dict[str, Any]] = {}
        for conflict in conflicts:
            seen.setdefault(self.conflict_identity(conflict), conflict)
        added = refreshed = 0
        with self.lock:
            rows = self.conn.execute(
                """SELECT c.id, c.identity_key FROM conflicts c
                   JOIN runs r ON r.id = c.run_id
                   WHERE c.status = 'open'
                     AND r.status IN ('previewed', 'applying', 'apply_failed', 'previewing')"""
            ).fetchall()
            open_keys = {str(row["identity_key"]): int(row["id"]) for row in rows}

            for identity, conflict in seen.items():
                existing = open_keys.get(identity)
                if existing is not None:
                    self.conn.execute(
                        """UPDATE conflicts SET detail_json = ?, last_seen_run_id = ?
                           WHERE id = ?""",
                        (json.dumps(conflict), run_id, existing),
                    )
                    refreshed += 1
                    continue
                self.conn.execute(
                    """INSERT INTO conflicts(
                           run_id, detail_json, identity_key, last_seen_run_id)
                       VALUES (?, ?, ?, ?)""",
                    (run_id, json.dumps(conflict), identity, run_id),
                )
                added += 1
            self.conn.commit()
        closed = self.close_conflicts_not_seen(run_id) if close_unseen else 0
        return {"added": added, "refreshed": refreshed, "closed": closed}

    def close_conflicts_not_seen(self, run_id: int) -> int:
        """Settle the questions a full run no longer asks.

        Marking each conflict with the run that last reported it means this
        works even when the run wrote them out in batches and kept none in
        memory. Only call it after a run that looked at the whole library.
        """
        with self.lock:
            cursor = self.conn.execute(
                """UPDATE conflicts
                   SET status = 'resolved', resolution = 'resolved_externally'
                   WHERE status = 'open'
                     AND (last_seen_run_id IS NULL OR last_seen_run_id != ?)
                     AND run_id IN (
                        SELECT id FROM runs
                        WHERE status IN ('previewed', 'applying', 'apply_failed', 'previewing')
                     )""",
                (run_id,),
            )
            self.conn.commit()
        return cursor.rowcount or 0

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
            if row["status"] in {"discarded", "superseded"}:
                raise ValueError("This preview was discarded, so its faces are no longer offered.")
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

    def append_logs(
        self, rows: list[tuple[str, str, str, int | None, str]]
    ) -> None:
        """Insert a batch of log records. Called only by the log writer thread."""
        if not rows:
            return
        with self.lock:
            self.conn.executemany(
                """INSERT INTO log_entries(ts, level, logger, run_id, message)
                   VALUES (?, ?, ?, ?, ?)""",
                rows,
            )
            self.conn.commit()

    def logs(
        self,
        *,
        level: str | None = None,
        run_id: int | None = None,
        query: str | None = None,
        before_id: int | None = None,
        after_id: int | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Return a page of log entries, newest first.

        ``level`` selects that level and everything more severe. ``after_id``
        returns only newer entries, which is how the interface follows a live
        run without re-reading the page it already has.
        """
        limit = max(1, min(1000, int(limit)))
        clauses: list[str] = []
        values: list[Any] = []
        if level:
            ranked = LOG_LEVELS.get(level.upper())
            if ranked is None:
                raise ValueError("Unknown log level.")
            wanted = [name for name, rank in LOG_LEVELS.items() if rank >= ranked]
            clauses.append(f"level IN ({','.join('?' * len(wanted))})")
            values.extend(wanted)
        if run_id is not None:
            clauses.append("run_id = ?")
            values.append(int(run_id))
        if query:
            clauses.append("message LIKE ? ESCAPE '\\'")
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            values.append(f"%{escaped}%")
        if before_id is not None:
            clauses.append("id < ?")
            values.append(int(before_id))
        if after_id is not None:
            clauses.append("id > ?")
            values.append(int(after_id))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.lock:
            rows = self.conn.execute(
                f"SELECT * FROM log_entries {where} ORDER BY id DESC LIMIT ?",
                (*values, limit + 1),
            ).fetchall()
        entries = [dict(row) for row in rows[:limit]]
        return {
            "entries": entries,
            "has_more": len(rows) > limit,
            "oldest_id": entries[-1]["id"] if entries else None,
            "newest_id": entries[0]["id"] if entries else None,
        }

    def delete_old_logs(self, *, days: int = 30, debug_days: int = 3) -> int:
        """Apply the retention policy. Debug lines go sooner than the rest."""
        with self.lock:
            cursor = self.conn.execute(
                "DELETE FROM log_entries WHERE ts < datetime('now', ?)",
                (f"-{max(0, int(days))} days",),
            )
            removed = cursor.rowcount or 0
            cursor = self.conn.execute(
                """DELETE FROM log_entries WHERE level = 'DEBUG'
                   AND ts < datetime('now', ?)""",
                (f"-{max(0, int(debug_days))} days",),
            )
            removed += cursor.rowcount or 0
            self.conn.commit()
        return removed

    def plan_conflicts(self, run_id: int) -> dict[str, Any]:
        """The decisions that belong in this run's plan, and only those.

        Once a run has a journal its plan is frozen, so a decision made later
        is left for a follow-up run rather than silently lengthening it.
        """
        with self.lock:
            journalled = int(
                self.conn.execute(
                    "SELECT COUNT(*) FROM run_actions WHERE run_id = ?", (run_id,)
                ).fetchone()[0]
            )
            if journalled:
                rows = self.conn.execute(
                    """SELECT id, detail_json, resolution FROM conflicts
                       WHERE run_id = ? AND status = 'resolved' AND decided_run_id = ?
                       ORDER BY id""",
                    (run_id, run_id),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    """SELECT id, detail_json, resolution FROM conflicts
                       WHERE run_id = ? AND status = 'resolved'
                         AND resolution IN ('digikam', 'memories')
                         AND decided_run_id IS NULL
                       ORDER BY id""",
                    (run_id,),
                ).fetchall()
        conflicts = []
        for row in rows:
            item = json.loads(row["detail_json"] or "{}")
            item.update({"id": int(row["id"]), "resolution": row["resolution"]})
            conflicts.append(item)
        return {"conflicts": conflicts, "remaining": 0, "total": len(conflicts)}

    def pending_decisions(self) -> list[dict[str, Any]]:
        """Settled conflicts that no run has applied yet."""
        with self.lock:
            rows = self.conn.execute(
                """SELECT id, run_id, resolution, detail_json FROM conflicts
                   WHERE status = 'resolved'
                     AND resolution IN ('digikam', 'memories')
                     AND decided_run_id IS NULL
                   ORDER BY id"""
            ).fetchall()
        decisions = []
        for row in rows:
            item = json.loads(row["detail_json"] or "{}")
            item.update(
                {
                    "id": int(row["id"]),
                    "run_id": int(row["run_id"]),
                    "resolution": str(row["resolution"]),
                }
            )
            decisions.append(item)
        return decisions

    def mark_decisions_folded(self, conflict_ids: list[int], run_id: int) -> int:
        if not conflict_ids:
            return 0
        placeholders = ",".join("?" * len(conflict_ids))
        with self.lock:
            cursor = self.conn.execute(
                f"""UPDATE conflicts SET decided_run_id = ?
                    WHERE id IN ({placeholders}) AND decided_run_id IS NULL""",
                (run_id, *conflict_ids),
            )
            self.conn.commit()
        return cursor.rowcount or 0

    # --------------------------------------------------------- checkpoints

    def save_checkpoint(
        self,
        run_id: int,
        phase: str,
        cursor: dict[str, Any],
        counters: dict[str, Any],
    ) -> None:
        """Record how far a preview has read, so it can pick up from here."""
        with self.lock:
            self.conn.execute(
                """INSERT INTO run_checkpoints(run_id, phase, cursor_json, counters_json, updated_at)
                   VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(run_id) DO UPDATE SET
                       phase = excluded.phase,
                       cursor_json = excluded.cursor_json,
                       counters_json = excluded.counters_json,
                       updated_at = CURRENT_TIMESTAMP""",
                (run_id, phase, json.dumps(cursor), json.dumps(counters)),
            )
            self.conn.commit()

    def checkpoint(self, run_id: int) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM run_checkpoints WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        return {
            "phase": str(row["phase"]),
            "cursor": json.loads(row["cursor_json"] or "{}"),
            "counters": json.loads(row["counters_json"] or "{}"),
            "updated_at": row["updated_at"],
        }

    def clear_checkpoint(self, run_id: int) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM run_checkpoints WHERE run_id = ?", (run_id,))
            self.conn.commit()

    def append_preview_actions(self, run_id: int, actions: list[dict[str, Any]]) -> int:
        """Write a batch of proposed changes out, keeping memory flat."""
        if not actions:
            return 0
        with self.lock:
            self.conn.executemany(
                "INSERT INTO preview_actions(run_id, action_json) VALUES (?, ?)",
                [(run_id, json.dumps(action)) for action in actions],
            )
            self.conn.commit()
        return len(actions)

    def record_preview_batch(
        self,
        run_id: int,
        actions: list[dict[str, Any]],
        conflicts: list[dict[str, Any]],
        phase: str,
        cursor: dict[str, Any],
        counters: dict[str, Any],
    ) -> None:
        """Write a batch's findings and its cursor together.

        One transaction, because actions written without their cursor would be
        written again by a resumed run, and counted twice.
        """
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                if actions:
                    self.conn.executemany(
                        "INSERT INTO preview_actions(run_id, action_json) VALUES (?, ?)",
                        [(run_id, json.dumps(action)) for action in actions],
                    )
                for conflict in conflicts:
                    identity = self.conflict_identity(conflict)
                    existing = self.conn.execute(
                        """SELECT c.id FROM conflicts c JOIN runs r ON r.id = c.run_id
                           WHERE c.status = 'open' AND c.identity_key = ?
                             AND r.status IN ('previewing', 'previewed', 'applying', 'apply_failed')
                           LIMIT 1""",
                        (identity,),
                    ).fetchone()
                    if existing is not None:
                        self.conn.execute(
                            """UPDATE conflicts SET detail_json = ?, last_seen_run_id = ?
                               WHERE id = ?""",
                            (json.dumps(conflict), run_id, int(existing[0])),
                        )
                    else:
                        self.conn.execute(
                            """INSERT INTO conflicts(
                                   run_id, detail_json, identity_key, last_seen_run_id)
                               VALUES (?, ?, ?, ?)""",
                            (run_id, json.dumps(conflict), identity, run_id),
                        )
                self.conn.execute(
                    """INSERT INTO run_checkpoints(
                           run_id, phase, cursor_json, counters_json, updated_at)
                       VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                       ON CONFLICT(run_id) DO UPDATE SET
                           phase = excluded.phase,
                           cursor_json = excluded.cursor_json,
                           counters_json = excluded.counters_json,
                           updated_at = CURRENT_TIMESTAMP""",
                    (run_id, phase, json.dumps(cursor), json.dumps(counters)),
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def preview_actions(self, run_id: int) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT action_json FROM preview_actions WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def count_preview_actions(self, run_id: int) -> int:
        with self.lock:
            return int(
                self.conn.execute(
                    "SELECT COUNT(*) FROM preview_actions WHERE run_id = ?", (run_id,)
                ).fetchone()[0]
            )

    def clear_preview_actions(self, run_id: int) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM preview_actions WHERE run_id = ?", (run_id,))
            self.conn.commit()

    # -------------------------------------------------------------- ledger

    def load_ledger(self) -> dict[tuple[int, int], str]:
        """Every remembered agreement, keyed by its Memories detection."""
        with self.lock:
            rows = self.conn.execute(
                """SELECT nextcloud_file_id, nextcloud_detection_id, synced_name
                   FROM face_links
                   WHERE profile_id = 1 AND synced_name IS NOT NULL
                     AND nextcloud_detection_id IS NOT NULL"""
            ).fetchall()
        return {
            (int(row["nextcloud_file_id"]), int(row["nextcloud_detection_id"])):
                str(row["synced_name"])
            for row in rows
        }

    def record_agreements(self, entries: list[dict[str, Any]]) -> None:
        """Remember the agreed name for each face, replacing any earlier row.

        Keyed on the Memories detection, because a rename changes the digiKam
        tag id and would otherwise leave a stale second row behind.
        """
        if not entries:
            return
        with self.lock:
            for entry in entries:
                file_id = entry.get("nextcloud_file_id")
                detection_id = entry.get("nextcloud_detection_id")
                if file_id is None or detection_id is None:
                    continue
                self.conn.execute(
                    """DELETE FROM face_links
                       WHERE profile_id = 1
                         AND nextcloud_file_id = ? AND nextcloud_detection_id = ?""",
                    (int(file_id), int(detection_id)),
                )
                self.conn.execute(
                    """INSERT INTO face_links(
                           profile_id, digikam_image_id, digikam_tag_id,
                           nextcloud_file_id, nextcloud_detection_id,
                           digikam_name, nextcloud_name, digikam_rect_json,
                           nextcloud_rect_json, synced_name, last_synced_at)
                       VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                    (
                        int(entry.get("digikam_image_id") or 0),
                        entry.get("digikam_tag_id"),
                        int(file_id),
                        int(detection_id),
                        str(entry.get("synced_name") or ""),
                        str(entry.get("synced_name") or ""),
                        json.dumps(entry.get("digikam_rect") or []),
                        json.dumps(entry.get("nextcloud_rect") or []),
                        str(entry.get("synced_name") or ""),
                    ),
                )
            self.conn.commit()

    def clear_ledger(self) -> int:
        """Forget every agreement, so the next run learns them again."""
        with self.lock:
            cursor = self.conn.execute("DELETE FROM face_links WHERE profile_id = 1")
            self.conn.commit()
        return cursor.rowcount or 0

    def ledger_size(self) -> int:
        with self.lock:
            return int(
                self.conn.execute(
                    "SELECT COUNT(*) FROM face_links WHERE profile_id = 1 AND synced_name IS NOT NULL"
                ).fetchone()[0]
            )

    # ------------------------------------------------------- service state

    def get_state(self, key: str, default: Any = None) -> Any:
        """Read one durable service value, such as when a pause ends."""
        with self.lock:
            row = self.conn.execute(
                "SELECT value_json FROM service_state WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return default

    def set_state(self, key: str, value: Any) -> None:
        with self.lock:
            self.conn.execute(
                """INSERT INTO service_state(key, value_json, updated_at)
                   VALUES (?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(key) DO UPDATE SET
                       value_json = excluded.value_json,
                       updated_at = CURRENT_TIMESTAMP""",
                (key, json.dumps(value)),
            )
            self.conn.commit()

    def clear_state(self, key: str) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM service_state WHERE key = ?", (key,))
            self.conn.commit()

    # ------------------------------------------------------------- activity

    def recent_runs(self, limit: int = 25, before_id: int | None = None) -> dict[str, Any]:
        """A page of run history, newest first, for the activity list."""
        limit = max(1, min(200, int(limit)))
        clause = "WHERE id < ?" if before_id is not None else ""
        values: tuple[Any, ...] = (int(before_id), limit + 1) if before_id is not None else (limit + 1,)
        with self.lock:
            rows = self.conn.execute(
                f"""SELECT id, mode, status, trigger, auto_apply, waiting_reason,
                           started_at, finished_at, summary_json
                    FROM runs {clause} ORDER BY id DESC LIMIT ?""",
                values,
            ).fetchall()
        runs = []
        for row in rows[:limit]:
            item = dict(row)
            item["summary"] = json.loads(item.pop("summary_json") or "{}")
            item["auto_apply"] = bool(item["auto_apply"])
            runs.append(item)
        return {
            "runs": runs,
            "has_more": len(rows) > limit,
            "oldest_id": runs[-1]["id"] if runs else None,
        }

    def run_counts(self) -> dict[str, int]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT status, COUNT(*) AS amount FROM runs GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["amount"]) for row in rows}

    def last_completed_run(self) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute(
                """SELECT id, status, trigger, started_at, finished_at, summary_json
                   FROM runs
                   WHERE status IN ('applied', 'applied_with_issues', 'no_changes', 'previewed')
                   ORDER BY COALESCE(finished_at, started_at) DESC, id DESC LIMIT 1"""
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["summary"] = json.loads(item.pop("summary_json") or "{}")
        return item

    # ------------------------------------------------------------ attention

    def open_conflicts(self, limit: int = 500) -> list[dict[str, Any]]:
        """Unresolved conflicts from every run that can still be applied."""
        with self.lock:
            rows = self.conn.execute(
                """SELECT c.id, c.run_id, c.detail_json, r.started_at
                   FROM conflicts c JOIN runs r ON r.id = c.run_id
                   WHERE c.status = 'open'
                     AND r.status IN ('previewed', 'applying', 'apply_failed')
                   ORDER BY c.run_id DESC, c.id
                   LIMIT ?""",
                (max(1, min(2000, int(limit))),),
            ).fetchall()
        conflicts = []
        for row in rows:
            item = json.loads(row["detail_json"] or "{}")
            item.update(
                {
                    "id": int(row["id"]),
                    "run_id": int(row["run_id"]),
                    "status": "open",
                    "resolution": None,
                    "run_started_at": row["started_at"],
                }
            )
            conflicts.append(item)
        return conflicts

    def reviewable_failures(self, limit: int = 500) -> list[dict[str, Any]]:
        """Rejected faces from every run, newest run first."""
        with self.lock:
            rows = self.conn.execute(
                """SELECT run_id FROM run_actions
                   WHERE status = 'failed'
                     AND operation IN ('insert_memories', 'create_digikam')
                   GROUP BY run_id ORDER BY run_id DESC""",
            ).fetchall()
        failures: list[dict[str, Any]] = []
        for row in rows:
            for failure in self.failed_apply_actions(int(row["run_id"])):
                if failure["reviewable"]:
                    failure["run_id"] = int(row["run_id"])
                    failures.append(failure)
                if len(failures) >= limit:
                    return failures
        return failures

    def attention(self) -> dict[str, Any]:
        """Everything waiting on a person, across every run."""
        conflicts = self.open_conflicts()
        failures = self.reviewable_failures()
        return {
            "conflicts": conflicts,
            "failures": failures,
            "conflict_count": len(conflicts),
            "failure_count": len(failures),
            "total": len(conflicts) + len(failures),
        }

    def attention_counts(self) -> dict[str, int]:
        """The counts alone, for the status poll."""
        with self.lock:
            conflicts = int(
                self.conn.execute(
                    """SELECT COUNT(*) FROM conflicts c JOIN runs r ON r.id = c.run_id
                       WHERE c.status = 'open'
                         AND r.status IN ('previewed', 'applying', 'apply_failed')"""
                ).fetchone()[0]
            )
            failures = int(
                self.conn.execute(
                    """SELECT COUNT(*) FROM run_actions WHERE status = 'failed'
                       AND operation IN ('insert_memories', 'create_digikam')"""
                ).fetchone()[0]
            )
        return {
            "conflicts": conflicts,
            "failures": failures,
            "total": conflicts + failures,
        }

    # -------------------------------------------------------- notifications

    def undelivered_notifications(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                """SELECT * FROM notifications
                   WHERE delivered_at IS NULL AND channel IS NULL
                   ORDER BY id"""
            ).fetchall()
        return [dict(row) for row in rows]

    def assign_notification_channel(self, ids: list[int], channel: str) -> None:
        """Record which channel will carry each notification."""
        if not ids:
            return
        stamp = "CURRENT_TIMESTAMP" if channel == "none" else "NULL"
        placeholders = ",".join("?" * len(ids))
        with self.lock:
            self.conn.execute(
                f"""UPDATE notifications SET channel = ?, delivered_at = {stamp}
                    WHERE id IN ({placeholders})""",
                (channel, *ids),
            )
            self.conn.commit()

    def mark_notifications_delivered(self, ids: list[int]) -> int:
        if not ids:
            return 0
        placeholders = ",".join("?" * len(ids))
        with self.lock:
            cursor = self.conn.execute(
                f"""UPDATE notifications SET delivered_at = CURRENT_TIMESTAMP
                    WHERE id IN ({placeholders}) AND delivered_at IS NULL""",
                ids,
            )
            self.conn.commit()
        return cursor.rowcount or 0

    def mark_notifications_read(self, ids: list[int] | None = None) -> int:
        with self.lock:
            if ids:
                placeholders = ",".join("?" * len(ids))
                cursor = self.conn.execute(
                    f"""UPDATE notifications SET read_at = CURRENT_TIMESTAMP
                        WHERE id IN ({placeholders}) AND read_at IS NULL""",
                    ids,
                )
            else:
                cursor = self.conn.execute(
                    "UPDATE notifications SET read_at = CURRENT_TIMESTAMP WHERE read_at IS NULL"
                )
            self.conn.commit()
        return cursor.rowcount or 0

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
