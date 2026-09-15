"""Interrupting a run, and picking it up where it stopped."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from digikam_nextcloud import checkpoint as checkpoint_module
from digikam_nextcloud.app_service import AppService
from digikam_nextcloud.checkpoint import StateCheckpoint
from digikam_nextcloud.models import RegionAction, RegionConflict, Rect, SyncReport
from digikam_nextcloud.settings import SettingsStore
from digikam_nextcloud.state_store import StateStore


def make_digikam_database(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection:
        for table in ("Images", "Tags", "TagProperties", "ImageTagProperties"):
            connection.execute(f"CREATE TABLE {table} (id INTEGER)")
        connection.commit()


def action(name, person="Gail"):
    return RegionAction(
        action=name, path=f"{person}.jpg", person=person, rect=(0.1, 0.1, 0.2, 0.2),
        nc_file_id=1, nc_detection_id=2, digikam_image_id=3, digikam_tag_id=4,
    )


class CheckpointTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = StateStore(Path(self.tmp.name) / "state.sqlite3")
        self.run_id = self.state.create_run("all")

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def test_a_batch_is_written_out_and_cleared_from_memory(self):
        report = SyncReport()
        report.actions.append(action("assign"))
        report.assigned = 1
        sink = StateCheckpoint(self.state, self.run_id)

        sink.batch_done("scanning_digikam", {"after_image_id": 120}, report)

        self.assertEqual(report.actions, [], "memory does not keep growing")
        self.assertEqual(len(self.state.preview_actions(self.run_id)), 1)
        saved = self.state.checkpoint(self.run_id)
        self.assertEqual(saved["cursor"], {"after_image_id": 120})
        self.assertEqual(saved["counters"]["assigned"], 1)

    def test_conflicts_survive_being_cleared(self):
        report = SyncReport()
        report.conflicts.append(
            RegionConflict(
                path="a.jpg", digikam_person="Gail", digikam_rect=(0, 0, 1, 1),
                nextcloud_person="Angie", nextcloud_rect=(0, 0, 1, 1),
                iou=0.9, nc_detection_id=2, nc_file_id=1,
            )
        )
        sink = StateCheckpoint(self.state, self.run_id)
        sink.batch_done("scanning_digikam", {}, report)

        self.assertEqual(report.conflicts, [])
        self.assertEqual(
            report.conflict_count, 1, "the count must not drop when memory is freed")
        self.assertEqual(self.state.attention_counts()["conflicts"], 0,
                         "still previewing, so not yet in the inbox")

    def test_counters_restore_onto_a_fresh_report(self):
        first = SyncReport()
        first.assigned = 7
        first.skipped = 3
        first.actions_total = 10
        first.conflicts.append(
            RegionConflict(
                path="a.jpg", digikam_person="A", digikam_rect=(0, 0, 1, 1),
                nextcloud_person="B", nextcloud_rect=(0, 0, 1, 1),
                iou=0.5, nc_detection_id=2, nc_file_id=1,
            )
        )
        StateCheckpoint(self.state, self.run_id).batch_done("scanning_digikam", {}, first)

        second = SyncReport()
        checkpoint_module.restore(second, self.state.checkpoint(self.run_id)["counters"])
        self.assertEqual(second.assigned, 7)
        self.assertEqual(second.skipped, 3)
        self.assertEqual(second.actions_total, 10)
        self.assertEqual(second.conflict_count, 1)

    def test_a_failed_write_keeps_the_batch_for_the_next_try(self):
        report = SyncReport()
        report.actions.append(action("assign"))
        sink = StateCheckpoint(self.state, self.run_id)
        with patch.object(self.state, "record_preview_batch", side_effect=RuntimeError):
            sink.batch_done("scanning_digikam", {}, report)
        self.assertEqual(len(report.actions), 1, "nothing was silently dropped")

    def test_the_cap_counts_the_whole_run_not_the_list_in_memory(self):
        from digikam_nextcloud.sync import _record_action

        report = SyncReport()
        sink = StateCheckpoint(self.state, self.run_id)
        for index in range(3):
            _record_action(report, action("assign", f"P{index}"), max_actions=4)
            sink.batch_done("scanning_digikam", {}, report)
        for index in range(3, 6):
            _record_action(report, action("assign", f"P{index}"), max_actions=4)
        # Four real actions, then one notice that the rest were truncated.
        written = self.state.preview_actions(self.run_id)
        self.assertEqual(len(written) + len(report.actions), 5)
        self.assertEqual(report.actions_total, 6)


class ResumePreviewTest(unittest.TestCase):
    """The application layer's part: which half to redo, and what to assemble."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        library = self.root / "Photos"
        library.mkdir()
        make_digikam_database(library / "digikam4.db")
        self.settings = SettingsStore(self.root / "config", use_keyring=False)
        self.settings.save(
            {
                "digikam_library": str(library),
                "digikam_db": str(library / "digikam4.db"),
                "nextcloud_url": "https://cloud.test",
                "nc_user": "keith",
                "nc_photos_path": "Photos",
            },
            "secret",
        )
        self.state = StateStore(self.root / "state.sqlite3")
        self.forward_calls = []

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def service(self):
        test = self

        class FakeBackend:
            def __init__(self, *args, **kwargs):
                pass

            def connection_requirements(self):
                from digikam_nextcloud.models import NextcloudRequirements
                return NextcloudRequirements(True, True, "")

            def list_named_faces(self, person=None, **kwargs):
                return []

            def close(self):
                pass

        class FakeDigikam:
            def __init__(self, path):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def count_images_with_faces(self):
                return 2

            def image_ids_for_person(self, person):
                return [1, 2]

            def images_for_relative_paths(self, paths):
                return {}

        def fake_sync(digikam, backend, *, checkpoint=None, report=None,
                      start_after_image_id=None, **kwargs):
            test.forward_calls.append(start_after_image_id)
            result = report if report is not None else SyncReport()
            result.assigned += 1
            result.actions.append(action("assign", "FromForward"))
            if checkpoint is not None:
                checkpoint.batch_done(
                    checkpoint_module.SCANNING_DIGIKAM, {"after_image_id": 99}, result)
            return result

        return AppService(
            self.settings, self.state,
            backend_factory=FakeBackend,
            digikam_factory=FakeDigikam,
            sync_function=fake_sync,
        )

    def test_a_first_run_scans_both_halves_and_saves_what_it_found(self):
        service = self.service()
        result = service.preview({"scope": "all"})
        self.assertEqual(self.forward_calls, [None])
        self.assertEqual(len(result["actions"]), 1)
        self.assertEqual(result["actions"][0]["person"], "FromForward")
        self.assertIsNone(
            self.state.checkpoint(result["run_id"]),
            "a finished run leaves no checkpoint behind")

    def test_a_run_stopped_in_the_digikam_half_resumes_from_its_cursor(self):
        run_id = self.state.create_run("all")
        self.state.save_checkpoint(
            run_id, checkpoint_module.SCANNING_DIGIKAM, {"after_image_id": 42},
            {"assigned": 5, "skipped": 2},
        )
        self.service().preview({"scope": "all"}, run_id=run_id)
        self.assertEqual(self.forward_calls, [42], "it carried on from image 42")

    def test_a_run_stopped_in_the_memories_half_does_not_redo_the_first(self):
        run_id = self.state.create_run("all")
        self.state.append_preview_actions(run_id, [{"action": "assign", "person": "Earlier"}])
        self.state.save_checkpoint(
            run_id, checkpoint_module.SCANNING_MEMORIES, {"path_index": 8},
            {"assigned": 5},
        )
        result = self.service().preview({"scope": "all"}, run_id=run_id)
        self.assertEqual(self.forward_calls, [], "the digiKam half was already done")
        self.assertEqual(result["summary"]["assigned"], 5, "its counters carried over")
        self.assertEqual(
            [item["person"] for item in result["actions"]], ["Earlier"],
            "and so did what it had already found")

    def test_a_resumed_run_keeps_the_scope_it_started_with(self):
        service = self.service()
        run_id = self.state.create_run("person", person="Gail Vassallo")
        with patch.object(service, "_preview_job") as job:
            service.resume_run(run_id)
        self.assertEqual(
            job.call_args.args[1], {"scope": "person", "person": "Gail Vassallo"})


class DeferApplyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        library = self.root / "Photos"
        library.mkdir()
        make_digikam_database(library / "digikam4.db")
        self.settings = SettingsStore(self.root / "config", use_keyring=False)
        self.settings.save(
            {
                "digikam_library": str(library),
                "digikam_db": str(library / "digikam4.db"),
                "nextcloud_url": "https://cloud.test",
                "nc_user": "keith",
                "nc_photos_path": "Photos",
            },
            "secret",
        )
        self.state = StateStore(self.root / "state.sqlite3")
        self.service = AppService(self.settings, self.state)
        self.run_id = self.state.create_run("all")
        self.state.finish_run(self.run_id, "previewed", {"created_in_digikam": 2})
        self.state.initialize_apply(self.run_id, [
            {
                "target": "digikam", "operation": "create_digikam",
                "path": "a.jpg", "person": "Gail", "rect": [0.1, 0.1, 0.2, 0.2],
            },
            {
                "target": "digikam", "operation": "create_digikam",
                "path": "b.jpg", "person": "Eli", "rect": [0.1, 0.1, 0.2, 0.2],
            },
        ])

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def apply_with(self, digikam_running, database_free=True):
        class FakeWriter:
            def __init__(self, path):
                self.calls = []

            def create_face(self, path, person, rect, image_id=None):
                self.calls.append(path)
                return {"changed": True, "digikam_image_id": 1, "digikam_tag_id": 2}

            def locate_face(self, *args, **kwargs):
                return None

            def close(self):
                pass

        with patch("digikam_nextcloud.app_service.DigikamWriter", FakeWriter), \
             patch("digikam_nextcloud.app_service.digikam_is_running",
                   return_value=digikam_running), \
             patch("digikam_nextcloud.app_service.digikam_database_is_free",
                   return_value=database_free), \
             patch("digikam_nextcloud.app_service.create_sqlite_backup",
                   return_value=self.root / "backup.db"):
            self.service._cache.clear()
            self.service._apply_job(self.run_id)

    def test_an_open_digikam_defers_rather_than_fails(self):
        self.apply_with(digikam_running=True)
        run = self.state.run(self.run_id)
        self.assertEqual(run["status"], "deferred")
        self.assertEqual(run["waiting_reason"], "digikam")
        counts = self.state.apply_counts(self.run_id)
        self.assertEqual(counts["pending"], 2, "nothing was written")
        self.assertEqual(counts["failed"], 0, "and nothing is treated as broken")

    def test_no_backup_is_taken_for_changes_that_were_not_made(self):
        self.apply_with(digikam_running=True)
        self.assertIsNone(self.state.apply_backup(self.run_id))

    def test_a_locked_database_defers_even_when_no_process_is_seen(self):
        self.apply_with(digikam_running=False, database_free=False)
        self.assertEqual(self.state.run(self.run_id)["status"], "deferred")

    def test_closing_digikam_lets_the_deferred_run_finish(self):
        self.apply_with(digikam_running=True)
        self.assertEqual(self.state.run(self.run_id)["status"], "deferred")

        self.apply_with(digikam_running=False)
        run = self.state.run(self.run_id)
        self.assertEqual(run["status"], "applied")
        self.assertEqual(self.state.apply_counts(self.run_id)["applied"], 2)
        self.assertIsNotNone(
            self.state.apply_backup(self.run_id),
            "a backup is taken before the first real change")

    def test_the_user_is_told_it_is_waiting(self):
        self.apply_with(digikam_running=True)
        events = [item["event_type"] for item in self.state.unread_notifications()]
        self.assertIn("sync.deferred", events)


if __name__ == "__main__":
    unittest.main()
