"""Tests for noticing changes and waiting for them to stop."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from digikam_nextcloud import fingerprint, triggers
from digikam_nextcloud.state_store import StateStore
from digikam_nextcloud.triggers import Pending, TriggerWatcher

NOW = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)


class QuietPeriodTest(unittest.TestCase):
    def test_one_change_schedules_a_sync_after_the_quiet_period(self):
        pending = triggers.note_change(Pending(), "digikam_changed", NOW, 10)
        self.assertEqual(pending.start_at, NOW + timedelta(minutes=10))
        self.assertEqual(pending.reasons, ["digikam_changed"])

    def test_a_second_change_pushes_the_start_out(self):
        first = triggers.note_change(Pending(), "digikam_changed", NOW, 10)
        later = NOW + timedelta(minutes=5)
        second = triggers.note_change(first, "digikam_changed", later, 10)
        self.assertEqual(second.start_at, later + timedelta(minutes=10))
        self.assertEqual(second.first_change_at, NOW, "the clock started at the first change")

    def test_constant_changes_still_sync_within_the_cap(self):
        pending = triggers.note_change(Pending(), "digikam_changed", NOW, 10)
        moment = NOW
        for _ in range(30):
            moment += timedelta(minutes=5)
            pending = triggers.note_change(pending, "digikam_changed", moment, 10)
        self.assertEqual(
            pending.start_at, NOW + timedelta(minutes=triggers.QUIET_CAP_MINUTES),
            "an afternoon of editing still syncs hourly")

    def test_different_reasons_are_gathered_not_replaced(self):
        pending = triggers.note_change(Pending(), "digikam_changed", NOW, 10)
        pending = triggers.note_change(pending, "memories_changed", NOW, 10)
        self.assertEqual(pending.reasons, ["digikam_changed", "memories_changed"])
        self.assertEqual(triggers.primary_reason(pending), "digikam_changed")

    def test_a_sync_is_due_only_once_its_time_has_come(self):
        pending = triggers.note_change(Pending(), "digikam_changed", NOW, 10)
        self.assertFalse(triggers.is_due(pending, NOW))
        self.assertTrue(triggers.is_due(pending, NOW + timedelta(minutes=10)))

    def test_nothing_pending_is_never_due(self):
        self.assertFalse(triggers.is_due(Pending(), NOW))

    def test_pending_survives_being_written_down(self):
        pending = triggers.note_change(Pending(), "memories_changed", NOW, 10)
        restored = Pending.from_dict(pending.to_dict())
        self.assertEqual(restored.reasons, pending.reasons)
        self.assertEqual(restored.start_at, pending.start_at)

    def test_rubbish_reads_back_as_nothing_pending(self):
        self.assertFalse(Pending.from_dict("not a record").waiting)
        self.assertFalse(Pending.from_dict({"start_at": "never"}).waiting)


class DigikamFingerprintTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "digikam4.db"
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                "CREATE TABLE ImageTagProperties (imageid INT, tagid INT, property TEXT, value TEXT)")
            connection.execute("CREATE TABLE Tags (id INT, name TEXT)")
            connection.execute("CREATE TABLE TagProperties (tagid INT, property TEXT, value TEXT)")
            connection.execute(
                "INSERT INTO ImageTagProperties VALUES (1, 34, 'tagRegion', '<rect x=\"1\"/>')")
            connection.execute("INSERT INTO Tags VALUES (34, 'Gail')")
            connection.execute("INSERT INTO TagProperties VALUES (34, 'person', 'Gail')")
            connection.commit()

    def tearDown(self):
        self.tmp.cleanup()

    def change(self, statement, *values):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(statement, values)
            connection.commit()

    def test_the_same_library_gives_the_same_answer(self):
        self.assertEqual(
            fingerprint.digikam_fingerprint(self.path),
            fingerprint.digikam_fingerprint(self.path))

    def test_a_moved_rectangle_changes_it(self):
        before = fingerprint.digikam_fingerprint(self.path)
        self.change("UPDATE ImageTagProperties SET value = ? WHERE tagid = 34", '<rect x="2"/>')
        self.assertNotEqual(fingerprint.digikam_fingerprint(self.path), before)

    def test_a_new_face_changes_it(self):
        before = fingerprint.digikam_fingerprint(self.path)
        self.change(
            "INSERT INTO ImageTagProperties VALUES (2, 34, 'tagRegion', '<rect x=\"9\"/>')")
        self.assertNotEqual(fingerprint.digikam_fingerprint(self.path), before)

    def test_a_rename_changes_it(self):
        before = fingerprint.digikam_fingerprint(self.path)
        self.change("UPDATE TagProperties SET value = 'Angie' WHERE tagid = 34")
        self.assertNotEqual(fingerprint.digikam_fingerprint(self.path), before)

    def test_an_unrelated_property_does_not(self):
        before = fingerprint.digikam_fingerprint(self.path)
        self.change("INSERT INTO ImageTagProperties VALUES (1, 34, 'rating', '5')")
        self.assertEqual(
            fingerprint.digikam_fingerprint(self.path), before,
            "only faces and people matter")

    def test_the_face_count_is_part_of_the_answer(self):
        self.assertTrue(fingerprint.digikam_fingerprint(self.path).endswith(":1"))

    def test_a_missing_library_has_no_modification_time(self):
        self.assertEqual(fingerprint.source_mtime(Path(self.tmp.name) / "gone.db"), 0.0)


class FakeApp:
    def __init__(self, state, settings, memories=None):
        self.state = state
        self.settings = settings
        self._memories = memories
        self.fingerprint_calls = 0

    def memories_fingerprint(self):
        self.fingerprint_calls += 1
        if isinstance(self._memories, Exception):
            raise self._memories
        return self._memories


class FakeSettings:
    def __init__(self, automation, database=None):
        self._automation = automation
        self._database = database

    def load(self):
        return {"automation": dict(self._automation), "digikam_db": self._database}


class WatcherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = StateStore(Path(self.tmp.name) / "state.sqlite3")

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def watcher(self, automation=None, memories=None, database=None):
        settings = FakeSettings(
            automation or {"enabled": True, "quiet_period_minutes": 10,
                           "check_interval_minutes": 5, "fallback_interval_hours": 24},
            database,
        )
        self.app = FakeApp(self.state, settings, memories)
        return TriggerWatcher(self.app)

    def test_automation_off_forgets_anything_pending(self):
        watcher = self.watcher(automation={"enabled": False})
        self.state.set_state("pending_trigger", {"reasons": ["memories_changed"]})
        self.assertIsNone(watcher.poll(NOW))
        self.assertIsNone(self.state.get_state("pending_trigger"))

    def test_a_memories_change_starts_a_wait_rather_than_a_sync(self):
        watcher = self.watcher(memories="abc")
        self.assertIsNone(watcher.poll(NOW), "the quiet period has not passed")
        pending = Pending.from_dict(self.state.get_state("pending_trigger"))
        self.assertEqual(pending.reasons, ["memories_changed"])

    def test_the_wait_ends_and_the_reason_is_reported(self):
        watcher = self.watcher(memories="abc")
        watcher.poll(NOW)
        self.assertEqual(watcher.poll(NOW + timedelta(minutes=11)), "memories_changed")

    def test_a_reported_trigger_is_not_forgotten_until_consumed(self):
        watcher = self.watcher(memories="abc")
        watcher.poll(NOW)
        later = NOW + timedelta(minutes=11)
        self.assertEqual(watcher.poll(later), "memories_changed")
        self.assertEqual(watcher.poll(later), "memories_changed", "still standing")
        watcher.consume()
        self.assertIsNone(self.state.get_state("pending_trigger"))

    def test_nextcloud_is_not_asked_more_often_than_configured(self):
        watcher = self.watcher(memories="abc")
        watcher.poll(NOW)
        watcher.poll(NOW + timedelta(minutes=1))
        watcher.poll(NOW + timedelta(minutes=2))
        self.assertEqual(self.app.fingerprint_calls, 1)
        watcher.poll(NOW + timedelta(minutes=6))
        self.assertEqual(self.app.fingerprint_calls, 2)

    def test_an_unanswerable_question_is_not_read_as_a_change(self):
        watcher = self.watcher(memories=RuntimeError("offline"))
        self.assertIsNone(watcher.poll(NOW))
        self.assertIsNone(self.state.get_state("pending_trigger"))

    def test_a_matching_fingerprint_is_not_a_change(self):
        self.state.set_state("last_synced_memories_fingerprint", "abc")
        watcher = self.watcher(memories="abc")
        watcher.poll(NOW)
        self.assertIsNone(self.state.get_state("pending_trigger"))

    def test_digikam_is_not_read_while_it_is_open(self):
        watcher = self.watcher(database="/nonexistent/digikam4.db")
        with patch("digikam_nextcloud.fingerprint.source_mtime") as mtime:
            watcher.poll(NOW, digikam_running=True)
            mtime.assert_not_called()

    def test_an_unchanged_modification_time_skips_the_hash(self):
        watcher = self.watcher(database="/photos/digikam4.db")
        self.state.set_state("last_digikam_mtime", 1234.0)
        with patch("digikam_nextcloud.fingerprint.source_mtime", return_value=1234.0), \
             patch("digikam_nextcloud.fingerprint.digikam_fingerprint") as hashed:
            watcher.poll(NOW)
            hashed.assert_not_called()

    def test_the_fallback_interval_eventually_forces_a_sync(self):
        watcher = self.watcher()
        self.state.set_state(
            "last_completed_at", (NOW - timedelta(hours=30)).isoformat())
        self.assertEqual(watcher.poll(NOW), "interval")

    def test_a_recent_sync_does_not_trip_the_fallback(self):
        watcher = self.watcher()
        self.state.set_state("last_completed_at", (NOW - timedelta(hours=2)).isoformat())
        self.assertIsNone(watcher.poll(NOW))

    def test_what_a_run_saw_becomes_the_synced_state_once_it_settles(self):
        watcher = self.watcher()
        run_id = self.state.create_run("all")
        self.state.set_state("in_flight_fingerprints", {
            "run_id": run_id, "digikam": "dk1", "memories": "nc1"})

        self.assertFalse(watcher.promote_finished(), "still running")
        self.state.finish_run(run_id, "applied", {})
        self.assertTrue(watcher.promote_finished())
        self.assertEqual(self.state.get_state("last_synced_digikam_fingerprint"), "dk1")
        self.assertEqual(self.state.get_state("last_synced_memories_fingerprint"), "nc1")
        self.assertIsNone(self.state.get_state("in_flight_fingerprints"))

    def test_a_failed_run_does_not_claim_its_libraries_are_synced(self):
        watcher = self.watcher()
        run_id = self.state.create_run("all")
        self.state.set_state("in_flight_fingerprints", {
            "run_id": run_id, "digikam": "dk1", "memories": "nc1"})
        self.state.finish_run(run_id, "failed", {})
        self.assertFalse(watcher.promote_finished())
        self.assertIsNone(self.state.get_state("last_synced_digikam_fingerprint"))


if __name__ == "__main__":
    unittest.main()
