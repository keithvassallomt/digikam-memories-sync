"""Deciding what happens to faces the other library rejected."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from digimem import status as status_module
from digimem.app_service import AppService
from digimem.apply import build_apply_plan
from digimem.settings import SettingsStore
from tests.test_apply import make_writable_digikam
from digimem.state_store import StateStore

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

    def test_applying_a_reviewed_face_does_not_block_the_next_apply(self):
        """Applying a reviewed face overwrites its review marker with its
        result. The plan check must not then read the finished row as an
        unexplained edit, or one more face left to add becomes unreachable."""
        failures = self.failures()
        self.state.resolve_failed_action(
            self.run_id, failures[0]["id"], "retry",
            rect=[0.5, 0.5, 0.2, 0.2], apply_to_remaining=True,
        )
        plan = build_apply_plan(
            {"summary": {"inserted": 4}, "actions": self.plan},
            self.state.plan_conflicts(self.run_id),
        )
        self.state.initialize_apply(self.run_id, plan)
        # Everything but the last one goes through, as a partial apply would.
        pending = self.state.pending_apply_actions(self.run_id)
        for item in pending[:-1]:
            self.state.finish_apply_action(
                item["id"], "applied", result={"changed": True, "nc_detection_id": 1},
            )
        self.state.finish_apply(self.run_id, "apply_failed")

        self.state.initialize_apply(self.run_id, plan)  # must not raise
        counts = self.state.apply_counts(self.run_id)
        self.assertEqual(counts["applied"], 3)
        self.assertEqual(counts["pending"], 1)


class ReviewMarkerTest(unittest.TestCase):
    """The marker that explains an edited action outlives the apply."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.sqlite3"
        self.state = StateStore(self.path)
        self.run_id = self.state.create_run("all")
        self.plan = [{
            "target": "memories", "operation": "insert_memories", "action": "insert",
            "path": "0.jpg", "person": "Person", "rect": [0.1, 0.1, 0.05, 0.08],
        }]
        self.state.finish_run(self.run_id, "previewed", {"inserted": 1})
        self.state.initialize_apply(self.run_id, self.plan)

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def marker(self, action_id):
        with self.state.lock:
            row = self.state.conn.execute(
                "SELECT review, result_json FROM run_actions WHERE id = ?", (action_id,)
            ).fetchone()
        return row["review"], row["result_json"]

    def test_a_retry_records_the_marker_outside_the_result(self):
        item = self.state.pending_apply_actions(self.run_id)[0]
        self.state.finish_apply_action(item["id"], "failed", error=REJECTED)
        self.state.finish_apply(self.run_id, "apply_failed")
        self.state.resolve_failed_action(
            self.run_id, self.state.failed_apply_actions(self.run_id)[0]["id"],
            "retry", rect=[0.5, 0.5, 0.2, 0.2],
        )
        review, result = self.marker(item["id"])
        self.assertEqual(review, "adjusted")
        self.assertIsNone(result, "the failed attempt is no longer the record")

    def test_applying_the_retry_does_not_erase_why_it_differs(self):
        item = self.state.pending_apply_actions(self.run_id)[0]
        self.state.finish_apply_action(item["id"], "failed", error=REJECTED)
        self.state.finish_apply(self.run_id, "apply_failed")
        self.state.resolve_failed_action(
            self.run_id, self.state.failed_apply_actions(self.run_id)[0]["id"],
            "retry", rect=[0.5, 0.5, 0.2, 0.2],
        )
        self.state.finish_apply_action(item["id"], "applied", result={"changed": True})
        review, _ = self.marker(item["id"])
        self.assertEqual(review, "adjusted", "the apply result is not the same field")

    def test_a_marker_written_the_old_way_moves_across_on_upgrade(self):
        item = self.state.pending_apply_actions(self.run_id)[0]
        with self.state.lock:
            self.state.conn.execute(
                """UPDATE run_actions SET review = NULL,
                   result_json = '{"review":"adjusted"}' WHERE id = ?""",
                (item["id"],),
            )
            self.state.conn.execute("ALTER TABLE run_actions DROP COLUMN review")
            self.state.conn.commit()
        self.state.close()

        reopened = StateStore(self.path)
        try:
            row = reopened.conn.execute(
                "SELECT review FROM run_actions WHERE id = ?", (item["id"],)
            ).fetchone()
            self.assertEqual(row["review"], "adjusted")
        finally:
            reopened.close()
            self.state = StateStore(self.path)


class SourceCorrectionTest(unittest.TestCase):
    """Adjusting a rectangle means the box is wrong, not send a different one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.database = root / "digikam4.db"
        make_writable_digikam(self.database)
        self.settings = SettingsStore(root / "config", use_keyring=False)
        self.settings.save({
            "digikam_library": str(root), "digikam_db": str(self.database),
            "nextcloud_url": "https://cloud.test", "nc_user": "keith",
            "nc_photos_path": "Photos",
        }, "secret")
        self.state = StateStore(root / "state.sqlite3")
        self.service = AppService(self.settings, self.state)
        self.run_id = self.state.create_run("all")
        self.state.finish_run(self.run_id, "previewed", {"inserted": 1})
        self.state.initialize_apply(self.run_id, [{
            "target": "memories", "operation": "insert_memories", "action": "insert",
            "path": "2026/photo.jpg", "person": "Angie Galea",
            "rect": [0.1, 0.1, 0.2, 0.2], "digikam_image_id": 3,
        }])
        self.action_id = self.state.pending_apply_actions(self.run_id)[0]["id"]
        self.state.finish_apply_action(self.action_id, "failed", error=REJECTED)
        self.state.finish_apply(self.run_id, "apply_failed")

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def digikam_rect(self):
        from digimem.digikam_writer import DigikamWriter
        with DigikamWriter(self.database) as writer:
            image = writer._image("2026/photo.jpg")
            return writer._face_rows(image)[0]["rect"].as_tuple()

    def resolve(self, **payload):
        with patch("digimem.app_service.digikam_is_running", return_value=False):
            return self.service.resolve_failure(self.run_id, self.action_id, payload)

    def test_the_adjusted_box_is_written_back_to_digikam(self):
        result = self.resolve(decision="retry", rect=[0.5, 0.5, 0.2, 0.2])
        self.assertEqual(result["source_corrected"], 1)
        moved = self.digikam_rect()
        self.assertAlmostEqual(moved[0], 0.5, places=2)
        self.assertAlmostEqual(moved[1], 0.5, places=2)

    def test_leaving_the_box_alone_writes_nothing(self):
        result = self.resolve(decision="retry", rect=[0.1, 0.1, 0.2, 0.2])
        self.assertEqual(result["source_corrected"], 0)
        self.assertAlmostEqual(self.digikam_rect()[0], 0.1, places=2)

    def test_keeping_the_face_writes_nothing(self):
        result = self.resolve(decision="keep_source")
        self.assertEqual(result["source_corrected"], 0)
        self.assertAlmostEqual(self.digikam_rect()[0], 0.1, places=2)

    def test_an_open_digikam_refuses_rather_than_writing_underneath_it(self):
        with patch("digimem.app_service.digikam_is_running", return_value=True):
            with self.assertRaisesRegex(ValueError, "Close digiKam"):
                self.service.resolve_failure(
                    self.run_id, self.action_id,
                    {"decision": "retry", "rect": [0.5, 0.5, 0.2, 0.2]})
        self.assertAlmostEqual(self.digikam_rect()[0], 0.1, places=2)

    def test_the_correction_is_backed_up_first(self):
        self.resolve(decision="retry", rect=[0.5, 0.5, 0.2, 0.2])
        backups = list((self.settings.root / "backups").glob("digikam4-review-*.db"))
        self.assertEqual(len(backups), 1, "local writes are backed up, this one too")


class SupersededWorkTest(unittest.TestCase):
    """A change that failed once and succeeded later must stop being offered."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = StateStore(Path(self.tmp.name) / "state.sqlite3")

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def run_with_pending(self, person="Eli", mode="all"):
        run_id = self.state.create_run(mode, person=person if mode == "person" else "")
        self.state.finish_run(run_id, "previewed", {"inserted": 1})
        self.state.initialize_apply(run_id, [{
            "target": "memories", "operation": "insert_memories", "action": "insert",
            "path": "a.jpg", "person": person, "rect": [0.1, 0.1, 0.05, 0.08],
        }])
        return run_id

    def test_a_later_full_run_retires_what_an_earlier_one_left_waiting(self):
        stale = self.run_with_pending()
        later = self.run_with_pending()
        self.assertEqual(self.state.supersede_pending_actions(later), 1)
        self.assertEqual(self.state.apply_counts(stale)["pending"], 0)
        self.assertEqual(
            self.state.apply_counts(later)["pending"], 1,
            "a run never supersedes its own work")

    def test_a_scoped_run_only_speaks_for_its_own_person(self):
        eli = self.run_with_pending(person="Eli")
        gail = self.run_with_pending(person="Gail")
        later = self.run_with_pending(person="Gail", mode="person")
        self.state.supersede_pending_actions(later, "Gail")
        self.assertEqual(self.state.apply_counts(gail)["pending"], 0)
        self.assertEqual(
            self.state.apply_counts(eli)["pending"], 1,
            "this run never looked at Eli, so it cannot answer for him")

    def test_superseded_work_no_longer_makes_a_run_actionable(self):
        """The symptom: Home offered the same change after every sync."""
        stale = self.run_with_pending()
        self.state.finish_apply(stale, "apply_failed")
        self.assertEqual(self.state.latest_actionable_run()["id"], stale)

        later = self.run_with_pending()
        self.state.supersede_pending_actions(later)
        self.state.finish_apply(later, "applied")
        self.assertIsNone(
            self.state.latest_actionable_run(),
            "nothing is waiting once the newer run has covered the same ground")


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
        with patch("digimem.app_service.digikam_is_running", return_value=False):
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
