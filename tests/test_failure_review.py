"""Deciding what happens to faces the other library rejected."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from digikam_nextcloud import status as status_module
from digikam_nextcloud.app_service import AppService
from digikam_nextcloud.apply import build_apply_plan
from digikam_nextcloud.settings import SettingsStore
from digikam_nextcloud.state_store import StateStore

REJECTED = "Recognize face-import failed (HTTP 422): No face found inside the supplied rectangle"


class FailureReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = StateStore(Path(self.tmp.name) / "state.sqlite3")
        self.run_id = self.state.create_run("all")
        self.plan = [
            {
                "target": "memories", "operation": "insert_memories",
                "action": "insert", "path": f"{n}.jpg", "person": f"Person {n}",
                "rect": [0.1 * n, 0.1, 0.05, 0.08],
            }
            for n in range(4)
        ]
        self.state.finish_run(self.run_id, "previewed", {"inserted": 4})
        self.state.initialize_apply(self.run_id, self.plan)
        for item in self.state.pending_apply_actions(self.run_id):
            self.state.finish_apply_action(item["id"], "failed", error=REJECTED)
        self.state.finish_apply(self.run_id, "apply_failed")

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def failures(self):
        return self.state.failed_apply_actions(self.run_id)

    def stored(self, action_id):
        with self.state.lock:
            row = self.state.conn.execute(
                "SELECT status, action_json FROM run_actions WHERE id = ?", (action_id,)
            ).fetchone()
        return row["status"], json.loads(row["action_json"])

    # ------------------------------------------------------------ bulk retry

    def test_adding_all_remaining_queues_every_one(self):
        first = self.failures()[0]
        self.state.resolve_failed_action(
            self.run_id, first["id"], "retry",
            rect=[0.1, 0.1, 0.05, 0.08], apply_to_remaining=True,
        )
        counts = self.state.apply_counts(self.run_id)
        self.assertEqual(counts["pending"], 4)
        self.assertEqual(counts["failed"], 0)

    def test_every_face_keeps_its_own_box(self):
        failures = self.failures()
        # The one on screen is adjusted; the others must not inherit that box.
        self.state.resolve_failed_action(
            self.run_id, failures[0]["id"], "retry",
            rect=[0.5, 0.5, 0.2, 0.2], apply_to_remaining=True,
        )
        _, adjusted = self.stored(failures[0]["id"])
        self.assertEqual(adjusted["rect"], [0.5, 0.5, 0.2, 0.2])
        for failure in failures[1:]:
            _, action = self.stored(failure["id"])
            self.assertEqual(
                action["rect"], failure["rect"],
                "an untouched face keeps the box digiKam gave it")

    def test_each_one_is_marked_as_a_trusted_digikam_box(self):
        failures = self.failures()
        self.state.resolve_failed_action(
            self.run_id, failures[0]["id"], "retry",
            rect=failures[0]["rect"], apply_to_remaining=True,
        )
        for failure in failures:
            status, action = self.stored(failure["id"])
            self.assertEqual(status, "pending")
            self.assertTrue(
                action["confirmed_face"],
                "without this the detector gets a veto again and it fails again")

    def test_retrying_one_leaves_the_others_rejected(self):
        failures = self.failures()
        self.state.resolve_failed_action(
            self.run_id, failures[0]["id"], "retry",
            rect=failures[0]["rect"], apply_to_remaining=False,
        )
        counts = self.state.apply_counts(self.run_id)
        self.assertEqual(counts["pending"], 1)
        self.assertEqual(counts["failed"], 3)

    def test_keeping_all_remaining_still_works(self):
        failures = self.failures()
        self.state.resolve_failed_action(
            self.run_id, failures[0]["id"], "keep_source", apply_to_remaining=True,
        )
        self.assertEqual(self.state.apply_counts(self.run_id)["ignored"], 4)

    def test_a_bulk_retried_plan_is_still_accepted_as_the_same_plan(self):
        """Apply refuses a plan that does not match its preview. A retry
        changes the stored actions, and must stay recognisable."""
        failures = self.failures()
        self.state.resolve_failed_action(
            self.run_id, failures[0]["id"], "retry",
            rect=[0.5, 0.5, 0.2, 0.2], apply_to_remaining=True,
        )
        run = self.state.run(self.run_id)
        plan = build_apply_plan(
            {"summary": {"inserted": 4}, "actions": self.plan},
            self.state.plan_conflicts(self.run_id),
        )
        self.state.initialize_apply(self.run_id, plan)  # must not raise
        self.assertEqual(self.state.apply_counts(self.run_id)["pending"], 4)


class PartlyAppliedHomeTest(unittest.TestCase):
    """A run holding reviewed faces must not vanish from the home screen."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        library = root / "Photos"
        library.mkdir()
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(library / "digikam4.db")) as connection:
            for table in ("Images", "Tags", "TagProperties", "ImageTagProperties"):
                connection.execute(f"CREATE TABLE {table} (id INTEGER)")
            connection.commit()
        self.settings = SettingsStore(root / "config", use_keyring=False)
        self.settings.save({
            "digikam_library": str(library),
            "digikam_db": str(library / "digikam4.db"),
            "nextcloud_url": "https://cloud.test", "nc_user": "keith",
            "nc_photos_path": "Photos",
        }, "secret")
        self.state = StateStore(root / "state.sqlite3")
        self.service = AppService(self.settings, self.state)

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def status(self):
        with patch("digikam_nextcloud.app_service.digikam_is_running", return_value=False):
            self.service._cache.clear()
            return self.service.status({})

    def test_reviewed_faces_waiting_to_be_added_show_as_ready(self):
        run_id = self.state.create_run("all")
        self.state.finish_run(run_id, "previewed", {"inserted": 2})
        self.state.initialize_apply(run_id, [
            {"target": "memories", "operation": "insert_memories", "action": "insert",
             "path": f"{n}.jpg", "person": "Gail", "rect": [0, 0, 1, 1]}
            for n in range(2)
        ])
        actions = self.state.pending_apply_actions(run_id)
        self.state.finish_apply_action(actions[0]["id"], "applied", result={})
        self.state.finish_apply_action(actions[1]["id"], "failed", error=REJECTED)
        self.state.finish_apply(run_id, "apply_failed")

        self.assertEqual(
            self.status()["state"], status_module.ATTENTION,
            "while one is still rejected, it is a question for you")

        self.state.resolve_failed_action(
            run_id, actions[1]["id"], "retry", rect=[0, 0, 1, 1])
        found = self.status()
        self.assertEqual(
            found["state"], status_module.READY,
            "once reviewed it is work waiting, not work finished")
        self.assertEqual(found["run"]["apply"]["pending"], 1)

    def test_a_fully_applied_run_does_not_linger_as_ready(self):
        run_id = self.state.create_run("all")
        self.state.finish_run(run_id, "previewed", {"inserted": 1})
        self.state.initialize_apply(run_id, [
            {"target": "memories", "operation": "insert_memories", "action": "insert",
             "path": "a.jpg", "person": "Gail", "rect": [0, 0, 1, 1]},
        ])
        action = self.state.pending_apply_actions(run_id)[0]
        self.state.finish_apply_action(action["id"], "applied", result={})
        self.state.finish_apply(run_id, "applied")
        self.assertNotEqual(self.status()["state"], status_module.READY)


if __name__ == "__main__":
    unittest.main()
