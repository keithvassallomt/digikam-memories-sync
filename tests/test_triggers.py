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

    # ------------------------------------------------------- who changed

    def people(self):
        return fingerprint.read_digikam(self.path).people

    def test_each_person_is_hashed_on_their_own(self):
        self.change("INSERT INTO Tags VALUES (35, 'April')")
        self.change("INSERT INTO TagProperties VALUES (35, 'person', 'April')")
        self.change(
            "INSERT INTO ImageTagProperties VALUES (1, 35, 'tagRegion', '<rect x=\"4\"/>')")
        before = self.people()
        self.assertEqual(set(before), {"Gail", "April"})

        self.change("UPDATE ImageTagProperties SET value = ? WHERE tagid = 35", '<rect x="9"/>')
        self.assertEqual(
            fingerprint.changed_people(before, self.people()), {"April"},
            "only the person whose face moved")

    def test_a_new_face_names_the_person_it_belongs_to(self):
        before = self.people()
        self.change(
            "INSERT INTO ImageTagProperties VALUES (2, 34, 'tagRegion', '<rect x=\"9\"/>')")
        self.assertEqual(fingerprint.changed_people(before, self.people()), {"Gail"})

    def test_a_rename_is_two_people(self):
        """The old name loses its faces and the new one gains them, which is
        the only honest reading without tracking tag ids across a rename."""
        before = self.people()
        self.change("UPDATE Tags SET name = 'Abigail' WHERE id = 34")
        self.assertEqual(
            fingerprint.changed_people(before, self.people()), {"Gail", "Abigail"})

    def test_a_face_on_a_tag_that_is_not_a_person_belongs_to_nobody(self):
        """So the whole-library value moves and nobody is named, which is what
        makes a sync look at everyone rather than guess."""
        self.change("INSERT INTO Tags VALUES (90, 'Holiday')")
        before = fingerprint.read_digikam(self.path)
        self.change(
            "INSERT INTO ImageTagProperties VALUES (3, 90, 'tagRegion', '<rect x=\"7\"/>')")
        after = fingerprint.read_digikam(self.path)
        self.assertNotEqual(after.value, before.value)
        self.assertEqual(fingerprint.changed_people(before.people, after.people), set())

    def test_a_change_of_case_alone_is_not_a_change(self):
        self.assertEqual(
            fingerprint.changed_people({"Gail": "aa"}, {"gail": "aa"}), set())

    def test_the_name_returned_is_how_it_is_spelled_now(self):
        self.assertEqual(
            fingerprint.changed_people({"gail": "aa"}, {"Gail Vassallo": "bb"}),
            {"Gail Vassallo", "gail"})


class FakeApp:
    def __init__(self, state, settings, memories=None, people=None):
        self.state = state
        self.settings = settings
        self._memories = memories
        self._people = people or {}
        self.fingerprint_calls = 0

    def memories_changes(self):
        self.fingerprint_calls += 1
        if isinstance(self._memories, Exception):
            raise self._memories
        if self._memories is None:
            return None
        return fingerprint.Fingerprint(value=self._memories, people=dict(self._people))

    def memories_fingerprint(self):
        seen = self.memories_changes()
        return None if seen is None else seen.value


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

    def watcher(self, automation=None, memories=None, database=None, people=None):
        settings = FakeSettings(
            automation or {"enabled": True, "quiet_period_minutes": 10,
                           "check_interval_minutes": 5, "fallback_interval_hours": 24},
            database,
        )
        self.app = FakeApp(self.state, settings, memories, people)
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

    # ------------------------------------------------- scoping to one person

    def prime(self, *, digikam=("dk0", "dk1"), memories=("nc0", "nc1"),
              digikam_people=None, memories_people=None):
        """Put the watcher in the state a poll would have left it in."""
        synced, seen = digikam
        self.state.set_state("last_synced_digikam_fingerprint", synced)
        self.state.set_state("last_seen_digikam_fingerprint", seen)
        synced, seen = memories
        self.state.set_state("last_synced_memories_fingerprint", synced)
        self.state.set_state("last_seen_memories_fingerprint", seen)
        before, after = digikam_people or ({}, {})
        self.state.set_state("last_synced_digikam_people", before)
        self.state.set_state("last_seen_digikam_people", after)
        before, after = memories_people or ({}, {})
        self.state.set_state("last_synced_memories_people", before)
        self.state.set_state("last_seen_memories_people", after)

    def test_one_person_changing_scopes_the_sync_to_them(self):
        watcher = self.watcher()
        self.prime(
            memories=("nc0", "nc0"),
            digikam_people=({"Gail": "a", "April": "b"}, {"Gail": "a", "April": "c"}),
        )
        self.assertEqual(watcher.changed_people(), {"April"})
        self.assertEqual(watcher.scope_for("digikam_changed"), "April")

    def test_two_people_changing_means_everyone(self):
        watcher = self.watcher()
        self.prime(
            memories=("nc0", "nc0"),
            digikam_people=({"Gail": "a", "April": "b"}, {"Gail": "z", "April": "c"}),
        )
        self.assertIsNone(watcher.scope_for("digikam_changed"))

    def test_one_person_on_each_side_is_still_one_person(self):
        watcher = self.watcher()
        self.prime(
            digikam_people=({"April": "b"}, {"April": "c"}),
            memories_people=({"April": "x"}, {"April": "y"}),
        )
        self.assertEqual(watcher.scope_for("memories_changed"), "April")

    def test_a_library_that_moved_but_names_nobody_means_everyone(self):
        """An older companion app, or a digiKam change outside any face."""
        watcher = self.watcher()
        self.prime(memories=("nc0", "nc0"))
        self.assertIsNone(watcher.changed_people())
        self.assertIsNone(watcher.scope_for("digikam_changed"))

    def test_the_fallback_sweep_is_never_scoped(self):
        watcher = self.watcher()
        self.prime(
            memories=("nc0", "nc0"),
            digikam_people=({"April": "b"}, {"April": "c"}),
        )
        self.assertIsNone(watcher.scope_for("interval"))

    def test_a_scoped_run_settles_only_its_own_person(self):
        watcher = self.watcher()
        run_id = self.state.create_run("person", person="April")
        self.state.set_state("last_synced_digikam_people", {"Gail": "a", "April": "b"})
        self.state.set_state("last_synced_digikam_fingerprint", "dk0")
        self.state.set_state("in_flight_fingerprints", {
            "run_id": run_id, "digikam": "dk1", "memories": "nc1",
            "digikam_people": {"Gail": "z", "April": "c"},
            "memories_people": {"April": "y"},
        })
        self.state.finish_run(run_id, "applied", {})
        self.assertTrue(watcher.promote_finished())

        self.assertEqual(
            self.state.get_state("last_synced_digikam_people"),
            {"Gail": "a", "April": "c"},
            "Gail changed during the run and has not been looked at")
        self.assertEqual(
            self.state.get_state("last_synced_digikam_fingerprint"), "dk0",
            "the library as a whole is still out of step, so the next poll runs")
        self.assertIsNone(
            self.state.get_state("last_completed_at"),
            "nothing has swept the whole library, so the fallback clock stands")

    def test_a_person_with_no_faces_left_loses_their_entry(self):
        watcher = self.watcher()
        run_id = self.state.create_run("person", person="April")
        self.state.set_state("last_synced_digikam_people", {"April": "b"})
        self.state.set_state("in_flight_fingerprints", {
            "run_id": run_id, "digikam": "dk1", "digikam_people": {}, "memories_people": {},
        })
        self.state.finish_run(run_id, "applied", {})
        watcher.promote_finished()
        self.assertEqual(self.state.get_state("last_synced_digikam_people"), {})

    def test_a_full_run_settles_everyone(self):
        watcher = self.watcher()
        run_id = self.state.create_run("all")
        self.state.set_state("in_flight_fingerprints", {
            "run_id": run_id, "digikam": "dk1", "memories": "nc1",
            "digikam_people": {"Gail": "z"}, "memories_people": {"Gail": "y"},
        })
        self.state.finish_run(run_id, "applied", {})
        watcher.promote_finished()
        self.assertEqual(self.state.get_state("last_synced_digikam_people"), {"Gail": "z"})
        self.assertEqual(self.state.get_state("last_synced_memories_people"), {"Gail": "y"})
        self.assertIsNotNone(self.state.get_state("last_completed_at"))

    # ------------------------------------------- a run judged against itself

    def applied_run(self, mode="all", person="", wrote=("April",)):
        """A settled run that wrote to some people in Memories."""
        run_id = self.state.create_run(mode, person=person)
        self.state.finish_run(run_id, "previewed", {})
        self.state.initialize_apply(run_id, [
            {
                "target": "memories", "operation": "insert_memories",
                "action": "insert", "path": f"{name}.jpg", "person": name,
                "rect": [0.1, 0.1, 0.05, 0.08],
            }
            for name in wrote
        ])
        for item in self.state.pending_apply_actions(run_id):
            self.state.finish_apply_action(item["id"], "applied", result={"changed": True})
        self.state.finish_run(run_id, "applied", {})
        return run_id

    def test_a_runs_own_writes_do_not_chase_it_with_another_run(self):
        """An apply moves the fingerprint the run is about to be judged
        against, which used to start a second sync over the first one's work."""
        watcher = self.watcher(memories="nc_after", people={"April": "after"})
        run_id = self.applied_run()
        self.state.set_state("in_flight_fingerprints", {
            "run_id": run_id, "memories": "nc_before",
            "memories_people": {"April": "before"}, "digikam_people": {},
        })
        self.assertTrue(watcher.promote_finished())
        self.assertEqual(
            self.state.get_state("last_synced_memories_people"), {"April": "after"})
        self.assertIsNone(watcher.poll(NOW), "the run's own writes are not a change")

    def test_a_change_made_while_the_run_worked_is_still_noticed(self):
        """The property the old bookkeeping existed to protect. It has to
        survive the fix, or an edit during a long sync is lost."""
        watcher = self.watcher(
            memories="nc_after", people={"April": "after", "Gail": "moved"})
        run_id = self.applied_run()
        self.state.set_state("last_synced_memories_people",
                             {"April": "before", "Gail": "was"})
        self.state.set_state("last_synced_memories_fingerprint", "nc_before")
        self.state.set_state("in_flight_fingerprints", {
            "run_id": run_id, "memories": "nc_before",
            "memories_people": {"April": "before", "Gail": "was"},
            "digikam_people": {},
        })
        watcher.promote_finished()
        self.assertEqual(
            self.state.get_state("last_synced_memories_people")["Gail"], "was",
            "Gail moved while the run worked and was nothing to do with it")
        self.assertNotEqual(
            self.state.get_state("last_synced_memories_fingerprint"), "nc_after",
            "so the library still reads as changed")

        watcher.poll(NOW)
        self.assertEqual(watcher.changed_people(), {"Gail"})
        self.assertEqual(watcher.scope_for("memories_changed"), "Gail")

    def test_a_scoped_run_that_leaves_nothing_over_brings_the_library_into_step(self):
        """Otherwise the whole-library value stays stale for ever: every poll
        reads the library as changed, nobody is named, and a full run is asked
        for after every scoped one."""
        watcher = self.watcher(memories="nc_after", people={"April": "after"})
        run_id = self.applied_run(mode="person", person="April")
        self.state.set_state("last_synced_memories_people", {"April": "before"})
        self.state.set_state("last_synced_memories_fingerprint", "nc_before")
        self.state.set_state("in_flight_fingerprints", {
            "run_id": run_id, "memories": "nc_before",
            "memories_people": {"April": "before"}, "digikam_people": {},
        })
        watcher.promote_finished()
        self.assertEqual(
            self.state.get_state("last_synced_memories_fingerprint"), "nc_after")
        self.assertIsNone(watcher.poll(NOW), "nothing is left to sync")

    def test_a_scoped_run_still_claims_only_its_own_person(self):
        watcher = self.watcher(
            memories="nc_after", people={"April": "after", "Gail": "moved"})
        run_id = self.applied_run(mode="person", person="April")
        self.state.set_state("last_synced_memories_people",
                             {"April": "before", "Gail": "was"})
        self.state.set_state("in_flight_fingerprints", {
            "run_id": run_id, "memories": "nc_before",
            "memories_people": {"April": "before", "Gail": "was"},
            "digikam_people": {},
        })
        watcher.promote_finished()
        self.assertEqual(
            self.state.get_state("last_synced_memories_people"),
            {"April": "after", "Gail": "was"})

    def test_what_a_run_wrote_is_read_from_its_journal(self):
        run_id = self.state.create_run("all")
        self.state.finish_run(run_id, "previewed", {})
        self.state.initialize_apply(run_id, [
            {"target": "memories", "operation": "insert_memories", "action": "insert",
             "path": "a.jpg", "person": "April", "rect": [0.1, 0.1, 0.05, 0.08]},
            {"target": "digikam", "operation": "reassign_digikam",
             "action": "reassign_digikam", "path": "b.jpg", "person": "Gail",
             "old_person": "Abigail", "rect": [0.1, 0.1, 0.05, 0.08]},
            {"target": "memories", "operation": "insert_memories", "action": "insert",
             "path": "c.jpg", "person": "Never", "rect": [0.1, 0.1, 0.05, 0.08]},
        ])
        pending = self.state.pending_apply_actions(run_id)
        for item in pending[:2]:
            self.state.finish_apply_action(item["id"], "applied", result={"changed": True})
        self.state.finish_apply_action(pending[2]["id"], "failed", error="nope")

        written = self.state.people_written_by(run_id)
        self.assertEqual(written["memories"], {"April"})
        self.assertEqual(
            written["digikam"], {"Gail", "Abigail"},
            "a rename moves a face out of one person's set and into another's")

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
