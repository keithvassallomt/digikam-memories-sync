"""Tests for the home status, activity, attention and automation layer."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from digimem import notify, status as status_module
from digimem.app_service import AppService
from digimem.settings import SettingsStore
from digimem.state_store import StateStore


def make_digikam_database(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection:
        for table in ("Images", "Tags", "TagProperties", "ImageTagProperties"):
            connection.execute(f"CREATE TABLE {table} (id INTEGER)")
        connection.commit()


class StatePrecedenceTest(unittest.TestCase):
    """The home screen shows one state. These are the rules for which."""

    def choose(self, **overrides: object) -> str:
        defaults = dict(
            configured=True,
            run=None,
            attention_total=0,
            paused=False,
            automation_enabled=True,
            digikam_blocks_apply=False,
        )
        defaults.update(overrides)
        return status_module.choose_state(**defaults)  # type: ignore[arg-type]

    def test_setup_outranks_everything(self) -> None:
        self.assertEqual(
            self.choose(configured=False, run={"status": "previewing"}, attention_total=9),
            status_module.SETUP,
        )

    def test_a_running_job_outranks_waiting_work(self) -> None:
        self.assertEqual(
            self.choose(run={"status": "applying"}, attention_total=4), status_module.SYNCING
        )

    def test_decisions_outrank_a_ready_preview(self) -> None:
        self.assertEqual(
            self.choose(run={"status": "previewed"}, attention_total=1),
            status_module.ATTENTION,
            "conflicts must be settled before the rest of that preview applies",
        )

    def test_a_ready_preview_blocked_by_digikam_says_so(self) -> None:
        self.assertEqual(
            self.choose(run={"status": "previewed"}, digikam_blocks_apply=True),
            status_module.WAITING_DIGIKAM,
        )
        self.assertEqual(self.choose(run={"status": "previewed"}), status_module.READY)

    def test_paused_and_off_and_idle(self) -> None:
        self.assertEqual(self.choose(paused=True), status_module.PAUSED)
        self.assertEqual(self.choose(automation_enabled=False), status_module.OFF)
        self.assertEqual(self.choose(), status_module.IN_SYNC)


class NotificationRoutingTest(unittest.TestCase):
    def test_a_watched_window_is_never_interrupted(self) -> None:
        self.assertEqual(
            notify.choose_channel(
                client_attached=True, client_visible=True, client_can_notify=True
            ),
            notify.SUPPRESSED,
        )

    def test_a_background_window_raises_it_itself(self) -> None:
        self.assertEqual(
            notify.choose_channel(
                client_attached=True, client_visible=False, client_can_notify=True
            ),
            notify.BROWSER,
        )

    def test_the_service_covers_a_page_that_cannot_notify(self) -> None:
        self.assertEqual(
            notify.choose_channel(
                client_attached=True, client_visible=False, client_can_notify=False
            ),
            notify.OPERATING_SYSTEM,
        )

    def test_the_service_covers_a_closed_window(self) -> None:
        self.assertEqual(
            notify.choose_channel(
                client_attached=False, client_visible=False, client_can_notify=False
            ),
            notify.OPERATING_SYSTEM,
        )

    def test_categories_follow_the_settings_switches(self) -> None:
        off = {"decisions": False, "connection": True, "completed": False}
        self.assertFalse(notify.is_enabled("conflicts.created", off))
        self.assertTrue(notify.is_enabled("connection.action_required", off))
        self.assertFalse(notify.is_enabled("run.completed", off))

    def test_context_events_are_never_raised(self) -> None:
        for event in notify.SILENT_EVENTS:
            self.assertIsNone(notify.category(event))
            self.assertFalse(notify.is_enabled(event, {"decisions": True}))

    def test_the_grouping_tag_replaces_rather_than_stacks(self) -> None:
        payload = notify.browser_payload(
            {
                "id": 3,
                "run_id": 88,
                "event_type": "conflicts.created",
                "title": "12 faces need a decision",
                "message": "…",
                "target": "/attention",
            }
        )
        self.assertEqual(payload["tag"], "run-88-decisions")


class ServiceLayerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.library = self.root / "Photos"
        self.library.mkdir()
        make_digikam_database(self.library / "digikam4.db")
        self.settings = SettingsStore(self.root / "config", use_keyring=False)
        self.state = StateStore(self.root / "state.sqlite3")
        self.service = AppService(self.settings, self.state)

    def tearDown(self) -> None:
        self.state.close()
        self.temporary.cleanup()

    def configure(self) -> None:
        self.settings.save(
            {
                "digikam_library": str(self.library),
                "digikam_db": str(self.library / "digikam4.db"),
                "nextcloud_url": "https://cloud.test",
                "nc_user": "keith",
                "nc_photos_path": "Photos",
            },
            "secret",
        )

    def status(self, **client: object) -> dict:
        with patch("digimem.app_service.digikam_is_running", return_value=False):
            self.service._cache.clear()
            return self.service.status(client)

    # ------------------------------------------------------------------ state

    def test_an_unconfigured_install_asks_for_setup(self) -> None:
        self.assertEqual(self.status()["state"], status_module.SETUP)
        self.assertFalse(self.status()["configured"])

    def test_a_configured_install_with_nothing_to_do_is_in_sync(self) -> None:
        self.configure()
        self.settings.update_group("automation", {"enabled": True})
        self.assertEqual(self.status()["state"], status_module.IN_SYNC)

    def test_automation_off_is_reported_as_off(self) -> None:
        self.configure()
        self.assertEqual(self.status()["state"], status_module.OFF)

    def test_open_conflicts_reach_the_home_screen(self) -> None:
        self.configure()
        run_id = self.state.create_run("all")
        self.state.finish_run(run_id, "previewed", {})
        self.state.save_conflicts(run_id, [{"path": "a.jpg"}, {"path": "b.jpg"}])
        result = self.status()
        self.assertEqual(result["state"], status_module.ATTENTION)
        self.assertEqual(result["attention"]["conflicts"], 2)

    def test_digikam_blocks_a_preview_that_writes_to_it(self) -> None:
        self.configure()
        run_id = self.state.create_run("all")
        self.state.finish_run(run_id, "previewed", {"created_in_digikam": 31})
        with patch("digimem.app_service.digikam_is_running", return_value=True):
            self.service._cache.clear()
            self.assertEqual(self.service.status({})["state"], status_module.WAITING_DIGIKAM)

    # ----------------------------------------------------------------- pausing

    def test_pause_for_a_period_then_resume(self) -> None:
        self.configure()
        self.settings.update_group("automation", {"enabled": True})
        result = self.service.pause({"minutes": 60})
        self.assertTrue(result["paused"])
        self.assertEqual(self.status()["state"], status_module.PAUSED)
        self.service.resume()
        self.assertEqual(self.status()["state"], status_module.IN_SYNC)

    def test_an_indefinite_pause_has_no_end(self) -> None:
        self.configure()
        result = self.service.pause({"until": "indefinite"})
        self.assertTrue(result["paused"])
        self.assertIsNone(result["paused_until"])

    def test_a_pause_that_has_passed_expires_by_itself(self) -> None:
        self.configure()
        self.settings.update_group("automation", {"enabled": True})
        gone = datetime.now(timezone.utc) - timedelta(minutes=5)
        self.state.set_state("paused_until", gone.isoformat())
        self.assertEqual(self.status()["state"], status_module.IN_SYNC)
        self.assertIsNone(self.state.get_state("paused_until"))

    def test_an_unreadable_pause_is_discarded(self) -> None:
        self.configure()
        self.state.set_state("paused_until", "not a time")
        self.assertFalse(self.service._paused_until()[0])

    def test_a_pause_needs_a_duration(self) -> None:
        with self.assertRaises(ValueError):
            self.service.pause({})

    # --------------------------------------------------- notification routing

    def test_a_watched_window_suppresses_and_files_the_event(self) -> None:
        self.configure()
        self.state.create_notification("conflicts.created", "12", "…", "/attention")
        result = self.status(visible=True, permission="granted")
        self.assertEqual(result["notifications"]["raise"], [])
        self.assertEqual(self.state.undelivered_notifications(), [])

    def test_a_background_window_is_handed_the_notification(self) -> None:
        self.configure()
        self.state.create_notification("conflicts.created", "12", "…", "/attention")
        raised = self.status(visible=False, permission="granted")["notifications"]["raise"]
        self.assertEqual(len(raised), 1)
        self.assertEqual(raised[0]["title"], "12")
        # Not delivered until the page says it showed it.
        self.assertEqual(self.service.notifications_delivered({"ids": [raised[0]["id"]]}),
                         {"delivered": 1})
        self.assertEqual(
            self.status(visible=False, permission="granted")["notifications"]["raise"], []
        )

    def test_a_page_without_permission_leaves_it_to_the_service(self) -> None:
        self.configure()
        self.state.create_notification("conflicts.created", "12", "…", "/attention")
        result = self.status(visible=False, permission="denied")
        self.assertEqual(result["notifications"]["raise"], [])
        with self.state.lock:
            channel = self.state.conn.execute(
                "SELECT channel FROM notifications ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
        self.assertEqual(channel, notify.OPERATING_SYSTEM)

    def test_a_switched_off_category_is_filed_not_raised(self) -> None:
        self.configure()
        self.settings.update_group("notifications", {"decisions": False})
        self.state.create_notification("conflicts.created", "12", "…", "/attention")
        self.assertEqual(
            self.status(visible=False, permission="granted")["notifications"]["raise"], []
        )

    def test_the_session_token_is_never_echoed_in_the_status(self) -> None:
        self.configure()
        published = self.root / "config" / "service.json"
        published.parent.mkdir(parents=True, exist_ok=True)
        published.write_text(
            '{"pid": 1, "port": 47818, "token": "secret-token", "started_at": "now"}',
            encoding="utf-8",
        )
        service = self.status()["service"]
        self.assertEqual(service["port"], 47818)
        self.assertNotIn("token", service)
        self.assertNotIn("secret-token", str(self.status()))

    # ------------------------------------------------------ activity and work

    def test_activity_lists_runs_newest_first_and_pages(self) -> None:
        for _ in range(3):
            self.state.finish_run(self.state.create_run("all"), "applied", {"assigned": 1})
        page = self.service.activity(limit=2)
        self.assertEqual(len(page["runs"]), 2)
        self.assertTrue(page["has_more"])
        self.assertGreater(page["runs"][0]["id"], page["runs"][1]["id"])
        rest = self.service.activity(limit=2, before_id=page["oldest_id"])
        self.assertEqual(len(rest["runs"]), 1)
        self.assertFalse(rest["has_more"])

    def test_a_run_reports_open_decisions_apart_from_the_ones_it_found(self) -> None:
        """Answering a conflict must change what the screen asks of you."""
        run_id = self.state.create_run("all")
        self.state.finish_run(run_id, "previewed", {"conflicts": 3})
        self.state.save_conflicts(run_id, [
            {
                "path": f"{index}.jpg",
                "digikam_person": "Martina Muscat", "nextcloud_person": "Eli Vassallo",
                "digikam_rect": [0, 0, 1, 1], "nextcloud_rect": [0, 0, 1, 1],
                "iou": 0.6, "nc_file_id": 1, "nc_detection_id": index,
            }
            for index in range(3)
        ])

        run = self.state.run(run_id)
        self.assertEqual(run["conflicts_open"], 3)
        self.assertEqual(run["conflicts_resolved"], 0)

        for conflict in self.state.open_conflicts()[:3]:
            self.state.resolve_conflict(run_id, conflict["id"], "digikam")

        run = self.state.run(run_id)
        self.assertEqual(run["conflicts_open"], 0, "nothing is still being asked")
        self.assertEqual(run["conflicts_resolved"], 3)
        self.assertEqual(
            run["summary"]["conflicts"], 3,
            "what the run found does not change; only what is open does")

    def test_the_activity_list_carries_the_same_two_numbers(self) -> None:
        run_id = self.state.create_run("all")
        self.state.finish_run(run_id, "previewed", {"conflicts": 1})
        self.state.save_conflicts(run_id, [{
            "path": "a.jpg", "digikam_person": "A", "nextcloud_person": "B",
            "digikam_rect": [0, 0, 1, 1], "nextcloud_rect": [0, 0, 1, 1],
            "iou": 0.6, "nc_file_id": 1, "nc_detection_id": 2,
        }])
        row = self.service.activity()["runs"][0]
        self.assertEqual(row["conflicts_open"], 1)
        self.state.resolve_conflict(run_id, self.state.open_conflicts()[0]["id"], "digikam")
        self.assertEqual(self.service.activity()["runs"][0]["conflicts_open"], 0)

    def test_a_run_with_no_conflicts_reports_zero_not_nothing(self) -> None:
        run_id = self.state.create_run("all")
        self.state.finish_run(run_id, "previewed", {})
        run = self.state.run(run_id)
        self.assertEqual(run["conflicts_open"], 0)
        self.assertEqual(run["conflicts_resolved"], 0)

    def test_runs_record_what_started_them(self) -> None:
        run_id = self.state.create_run("all", trigger="digikam_closed", auto_apply=True)
        found = self.service.activity()["runs"][0]
        self.assertEqual(found["id"], run_id)
        self.assertEqual(found["trigger"], "digikam_closed")
        self.assertTrue(found["auto_apply"])

    def test_attention_gathers_conflicts_from_every_run(self) -> None:
        first = self.state.create_run("all")
        self.state.finish_run(first, "previewed", {})
        self.state.save_conflicts(first, [{"path": "one.jpg"}])
        second = self.state.create_run("all")
        self.state.finish_run(second, "previewed", {})
        self.state.save_conflicts(second, [{"path": "two.jpg"}])

        inbox = self.service.attention()
        self.assertEqual(inbox["conflict_count"], 2)
        self.assertEqual({item["run_id"] for item in inbox["conflicts"]}, {first, second})

    def test_a_discarded_run_stops_asking_for_decisions(self) -> None:
        run_id = self.state.create_run("all")
        self.state.finish_run(run_id, "previewed", {})
        self.state.save_conflicts(run_id, [{"path": "one.jpg"}])
        self.assertEqual(self.service.attention()["conflict_count"], 1)
        self.state.discard_preview(run_id)
        self.assertEqual(self.service.attention()["conflict_count"], 0)

    def test_sync_attaches_to_a_job_that_is_already_running(self) -> None:
        self.configure()
        with patch.object(self.service, "active_run_id", return_value=42):
            self.assertEqual(
                self.service.start_sync({"scope": "all"}),
                {"run_id": 42, "status": "previewing", "attached": True},
            )

    # ---------------------------------------------------------------- settings

    def test_settings_groups_accept_only_their_own_keys(self) -> None:
        result = self.service.update_automation({"enabled": True, "nonsense": 5})
        self.assertTrue(result["automation"]["enabled"])
        self.assertNotIn("nonsense", result["automation"])

    def test_settings_groups_reject_bad_values(self) -> None:
        with self.assertRaises(ValueError):
            self.service.update_automation({"enabled": "yes"})
        with self.assertRaises(ValueError):
            self.service.update_automation({"quiet_period_minutes": 0})
        with self.assertRaises(ValueError):
            self.service.update_automation({})

    def test_asking_about_disagreements_is_the_default(self) -> None:
        self.assertEqual(self.settings.load()["sync"]["conflict_policy"], "ask")
        self.assertTrue(self.settings.load()["sync"]["create_in_memories"])
        self.assertTrue(self.settings.load()["sync"]["create_in_digikam"])

    def test_a_library_can_be_trusted(self) -> None:
        for policy in ("digikam", "memories", "ask"):
            with self.subTest(policy=policy):
                result = self.service.update_sync({"conflict_policy": policy})
                self.assertEqual(result["sync"]["conflict_policy"], policy)

    def test_only_the_three_choices_are_accepted(self) -> None:
        with self.assertRaises(ValueError):
            self.service.update_sync({"conflict_policy": "whichever"})
        with self.assertRaises(ValueError):
            self.service.update_sync({"conflict_policy": True})
        self.assertEqual(self.settings.load()["sync"]["conflict_policy"], "ask")

    def test_face_box_creation_can_be_turned_off_per_library(self) -> None:
        result = self.service.update_sync({"create_in_memories": False})
        self.assertFalse(result["sync"]["create_in_memories"])
        # The other direction is a separate decision and is untouched.
        self.assertTrue(result["sync"]["create_in_digikam"])

    def test_changing_one_group_leaves_the_libraries_alone(self) -> None:
        self.configure()
        self.service.update_notifications({"completed": True})
        self.assertEqual(self.settings.load()["nc_user"], "keith")
        self.assertTrue(self.settings.load()["notifications"]["completed"])


if __name__ == "__main__":
    unittest.main()
