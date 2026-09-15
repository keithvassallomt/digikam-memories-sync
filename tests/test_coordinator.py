"""Tests for the gates, the backoff schedule and picking work back up."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from digikam_nextcloud import coordinator as coord
from digikam_nextcloud.coordinator import Coordinator, Snapshot, choose
from digikam_nextcloud.state_store import StateStore

NOW = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)


def snapshot(**overrides):
    defaults = dict(
        now=NOW, paused=False, job_running=False,
        digikam_running=False, digikam_free_since=NOW - timedelta(minutes=5),
        connection_ready=True,
    )
    defaults.update(overrides)
    return Snapshot(**defaults)


def run(run_id=1, status="waiting", reason=None, next_attempt=None):
    return {
        "id": run_id, "status": status, "waiting_reason": reason,
        "next_attempt_at": next_attempt, "attempts": 0,
    }


class BackoffTest(unittest.TestCase):
    def test_the_wait_grows_then_levels_off(self):
        minutes = [coord.backoff_delay(n).total_seconds() / 60 for n in range(8)]
        self.assertEqual(minutes, [1, 2, 5, 15, 30, 60, 60, 60])

    def test_a_negative_count_is_treated_as_the_first_attempt(self):
        self.assertEqual(coord.backoff_delay(-3), timedelta(minutes=1))

    def test_the_next_attempt_is_in_the_future(self):
        when = datetime.fromisoformat(coord.next_attempt_at(0, NOW))
        self.assertEqual(when, NOW + timedelta(minutes=1))


class DigikamGateTest(unittest.TestCase):
    def test_an_open_digikam_keeps_the_gate_shut(self):
        self.assertFalse(coord.digikam_gate_open(snapshot(digikam_running=True)))

    def test_the_gate_waits_a_moment_after_digikam_closes(self):
        just_closed = snapshot(digikam_free_since=NOW - timedelta(seconds=2))
        self.assertFalse(
            coord.digikam_gate_open(just_closed),
            "digiKam needs a moment to release its database")

    def test_the_gate_opens_once_it_has_settled(self):
        self.assertTrue(coord.digikam_gate_open(snapshot()))

    def test_a_gate_with_no_closing_time_stays_shut(self):
        self.assertFalse(coord.digikam_gate_open(snapshot(digikam_free_since=None)))


class ChoosingWorkTest(unittest.TestCase):
    def test_nothing_starts_while_a_job_is_running(self):
        decision = choose([run()], snapshot(job_running=True))
        self.assertFalse(decision.should_start)
        self.assertEqual(decision.blocked[1], coord.WAITING_JOB)

    def test_nothing_starts_while_paused(self):
        decision = choose([run()], snapshot(paused=True))
        self.assertFalse(decision.should_start)
        self.assertEqual(decision.blocked[1], coord.WAITING_PAUSED)

    def test_a_deferred_run_waits_for_digikam(self):
        decision = choose([run(status="deferred")], snapshot(digikam_running=True))
        self.assertFalse(decision.should_start)
        self.assertEqual(decision.blocked[1], coord.WAITING_DIGIKAM)

    def test_a_deferred_run_goes_once_digikam_has_closed(self):
        decision = choose([run(status="deferred")], snapshot())
        self.assertTrue(decision.should_start)
        self.assertEqual(decision.run_id, 1)

    def test_a_backed_off_run_waits_for_its_turn(self):
        later = (NOW + timedelta(minutes=5)).isoformat()
        decision = choose([run(next_attempt=later)], snapshot())
        self.assertEqual(decision.blocked[1], coord.WAITING_ATTEMPT)

    def test_a_backed_off_run_goes_when_its_time_arrives(self):
        past = (NOW - timedelta(seconds=1)).isoformat()
        self.assertTrue(choose([run(next_attempt=past)], snapshot()).should_start)

    def test_an_unreadable_attempt_time_does_not_block_forever(self):
        self.assertTrue(choose([run(next_attempt="soon")], snapshot()).should_start)

    def test_the_oldest_runnable_job_goes_first(self):
        runs = [run(3), run(1, status="deferred"), run(2)]
        self.assertEqual(choose(runs, snapshot()).run_id, 1)

    def test_a_blocked_run_does_not_hold_up_a_later_one(self):
        runs = [run(1, status="deferred"), run(2)]
        decision = choose(runs, snapshot(digikam_running=True))
        self.assertEqual(decision.run_id, 2)
        self.assertEqual(decision.blocked[1], coord.WAITING_DIGIKAM)

    def test_nothing_waiting_means_nothing_to_do(self):
        self.assertFalse(choose([], snapshot()).should_start)


class FakeSettings:
    def __init__(self, automation=None):
        self._automation = automation or {"enabled": False}

    def load(self):
        return {"automation": dict(self._automation), "digikam_db": None}


class FakeApp:
    def __init__(self, state, digikam_running=False, paused=False, automation=None):
        self.state = state
        self.settings = FakeSettings(automation)
        self._digikam = digikam_running
        self._paused = paused
        self.resumed = []
        self.invalidated = 0
        self.started = []
        self.applied = []
        self.busy = False
        self.desktop_notifications = 0
        self.retentions = 0

    def deliver_desktop_notifications(self):
        self.desktop_notifications += 1
        return 0

    def apply_retention(self):
        self.retentions += 1
        return {}

    def recognize_busy(self):
        return self.busy

    def memories_fingerprint(self):
        return None

    def start_sync(self, payload):
        self.started.append(payload)
        return {"run_id": 99, "status": "previewing"}

    def start_apply(self, run_id, payload):
        self.applied.append((run_id, payload))
        return {"run_id": run_id, "status": "applying"}

    def digikam_running(self):
        return self._digikam

    def _paused_until(self):
        return self._paused, None

    def active_run_id(self):
        return None

    def invalidate_probes(self):
        self.invalidated += 1

    def resume_run(self, run_id):
        self.resumed.append(run_id)


class CoordinatorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = StateStore(Path(self.tmp.name) / "state.sqlite3")
        self.app = FakeApp(self.state)

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def coordinator(self, **kwargs):
        return Coordinator(self.app, **kwargs)

    def test_a_clock_jump_with_no_elapsed_time_reads_as_sleep(self):
        wall = [NOW]
        monotonic = [100.0]
        engine = self.coordinator(
            monotonic=lambda: monotonic[0], wall=lambda: wall[0])
        wall[0] = NOW + timedelta(hours=2)
        monotonic[0] = 101.0
        self.assertTrue(engine.detect_sleep())

    def test_normal_time_passing_is_not_sleep(self):
        wall = [NOW]
        monotonic = [100.0]
        engine = self.coordinator(
            monotonic=lambda: monotonic[0], wall=lambda: wall[0])
        wall[0] = NOW + timedelta(seconds=10)
        monotonic[0] = 110.0
        self.assertFalse(engine.detect_sleep())

    def test_waking_up_throws_away_cached_answers(self):
        wall = [NOW]
        monotonic = [100.0]
        engine = self.coordinator(
            monotonic=lambda: monotonic[0], wall=lambda: wall[0])
        wall[0] = NOW + timedelta(hours=2)
        monotonic[0] = 101.0
        engine.tick()
        self.assertEqual(self.app.invalidated, 1)
        self.assertTrue(engine.slept)

    def test_a_waiting_run_is_picked_up(self):
        run_id = self.state.create_run("all")
        self.state.set_waiting(run_id, "resume")
        engine = self.coordinator()
        engine._digikam_free_since = NOW - timedelta(minutes=10)
        engine.tick()
        self.assertEqual(self.app.resumed, [run_id])

    def test_an_open_digikam_holds_a_deferred_run_back(self):
        run_id = self.state.create_run("all")
        self.state.set_deferred(run_id, "digikam")
        self.app._digikam = True
        engine = self.coordinator()
        engine.tick()
        self.assertEqual(self.app.resumed, [])

    def test_closing_digikam_releases_it_after_the_settle_period(self):
        run_id = self.state.create_run("all")
        self.state.set_deferred(run_id, "digikam")
        engine = self.coordinator()
        engine.tick()
        self.assertEqual(self.app.resumed, [], "the settle period has not passed")
        engine._digikam_free_since = engine.wall() - timedelta(minutes=1)
        engine.tick()
        self.assertEqual(self.app.resumed, [run_id])

    def test_every_tick_offers_to_show_waiting_notifications(self):
        engine = self.coordinator()
        engine.tick()
        engine.tick()
        self.assertEqual(self.app.desktop_notifications, 2)

    def test_a_failing_resume_does_not_stop_the_coordinator(self):
        run_id = self.state.create_run("all")
        self.state.set_waiting(run_id, "resume")
        engine = self.coordinator()
        engine._digikam_free_since = NOW - timedelta(minutes=10)
        with patch.object(self.app, "resume_run", side_effect=RuntimeError("boom")):
            engine.tick()
        engine.tick()
        self.assertEqual(self.app.resumed, [run_id], "the next tick tried again")


class StartingNewWorkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = StateStore(Path(self.tmp.name) / "state.sqlite3")

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def coordinator(self, **app_kwargs):
        self.app = FakeApp(self.state, **app_kwargs)
        engine = Coordinator(self.app)
        engine._digikam_free_since = NOW - timedelta(minutes=10)
        return engine

    def due_trigger(self):
        """A change seen long enough ago that its quiet period has passed."""
        self.state.set_state("pending_trigger", {
            "reasons": ["memories_changed"],
            "first_change_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
            "start_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        })

    def test_a_settled_change_starts_a_sync(self):
        engine = self.coordinator(automation={"enabled": True})
        self.due_trigger()
        engine.tick()
        self.assertEqual(len(self.app.started), 1)
        self.assertEqual(self.app.started[0]["trigger"], "memories_changed")
        self.assertIsNone(
            self.state.get_state("pending_trigger"), "the trigger was used up")

    def test_a_busy_recognize_holds_the_sync_but_keeps_the_trigger(self):
        engine = self.coordinator(automation={"enabled": True})
        self.app.busy = True
        self.due_trigger()
        engine.tick()
        self.assertEqual(self.app.started, [])
        self.assertIsNotNone(
            self.state.get_state("pending_trigger"),
            "the change must not be forgotten while Recognize works")

    def test_automation_off_means_nothing_starts(self):
        engine = self.coordinator(automation={"enabled": False})
        self.due_trigger()
        engine.tick()
        self.assertEqual(self.app.started, [])

    def test_a_pause_stops_new_work_as_well_as_resumed_work(self):
        engine = self.coordinator(automation={"enabled": True}, paused=True)
        self.due_trigger()
        engine.tick()
        self.assertEqual(self.app.started, [])

    def test_a_preview_told_to_apply_itself_is_applied(self):
        engine = self.coordinator(automation={"enabled": True})
        run_id = self.state.create_run("all", auto_apply=True)
        self.state.finish_run(run_id, "previewed", {})
        engine.tick()
        self.assertEqual(self.app.applied, [(run_id, {"automatic": True})])

    def test_a_preview_awaiting_a_person_is_left_alone(self):
        engine = self.coordinator(automation={"enabled": True})
        run_id = self.state.create_run("all", auto_apply=False)
        self.state.finish_run(run_id, "previewed", {})
        engine.tick()
        self.assertEqual(self.app.applied, [])

    def test_resuming_existing_work_comes_before_starting_new_work(self):
        engine = self.coordinator(automation={"enabled": True})
        run_id = self.state.create_run("all")
        self.state.set_waiting(run_id, "resume")
        self.due_trigger()
        engine.tick()
        self.assertEqual(self.app.resumed, [run_id])
        self.assertEqual(self.app.started, [], "one job at a time")


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = StateStore(Path(self.tmp.name) / "state.sqlite3")

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def test_everything_in_flight_is_re_queued_not_failed(self):
        previewing = self.state.create_run("all")
        applying = self.state.create_run("all")
        self.state.finish_run(applying, "applying", {})
        deferred = self.state.create_run("all")
        self.state.set_deferred(deferred, "digikam")
        finished = self.state.create_run("all")
        self.state.finish_run(finished, "applied", {})

        recovered = self.state.recover_unfinished_runs()
        self.assertEqual(recovered["total"], 3)

        waiting = {int(item["id"]) for item in self.state.runs_awaiting_work()}
        self.assertEqual(waiting, {previewing, applying, deferred})
        self.assertEqual(self.state.run(finished)["status"], "applied")
        self.assertEqual(self.state.run(previewing)["waiting_reason"], "resume")

    def test_recovery_says_what_it_found(self):
        self.state.create_run("all")
        counts = self.state.recover_unfinished_runs()
        self.assertEqual(counts["previewing"], 1)
        self.assertEqual(counts["total"], 1)

    def test_a_clean_start_recovers_nothing(self):
        self.assertEqual(self.state.recover_unfinished_runs()["total"], 0)


if __name__ == "__main__":
    unittest.main()
