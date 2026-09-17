"""Desktop notifications for a closed window, and keeping storage in check."""
from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from digimem import desktop, notify
from digimem.app_service import AppService
from digimem.settings import SettingsStore
from digimem.state_store import StateStore

NOW = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)


class DesktopSenderTest(unittest.TestCase):
    """Every send is mocked. Nothing is ever shown on a real desktop."""

    def sent(self, platform):
        with patch("sys.platform", platform), \
             patch("digimem.desktop.shutil.which", return_value="/usr/bin/tool"), \
             patch("digimem.desktop.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stderr = ""
            ok = desktop.send("12 faces need a decision", "Two names disagree.")
        self.assertTrue(ok)
        return run.call_args

    def test_linux_uses_notify_send_with_the_app_name(self):
        command = self.sent("linux").args[0]
        self.assertEqual(command[0], "notify-send")
        self.assertIn("--app-name", command)
        self.assertIn("DigiMem", command)
        self.assertEqual(command[-2:], ["12 faces need a decision", "Two names disagree."])

    def test_macos_uses_osascript(self):
        command = self.sent("darwin").args[0]
        self.assertEqual(command[0], "osascript")
        self.assertIn("display notification", command[2])
        self.assertIn("12 faces need a decision", command[2])

    def test_windows_uses_a_toast(self):
        call = self.sent("win32")
        self.assertIn("-NoProfile", call.args[0])
        self.assertIn("ToastNotification", call.kwargs["input"])

    def test_quotes_in_a_name_cannot_break_the_command(self):
        with patch("sys.platform", "darwin"), \
             patch("digimem.desktop.shutil.which", return_value="/usr/bin/osascript"), \
             patch("digimem.desktop.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stderr = ""
            desktop.send('He said "hi"', 'back\\slash')
        script = run.call_args.args[0][2]
        self.assertIn('\\"hi\\"', script)
        self.assertNotIn('said "hi"', script)

    def test_a_refusing_platform_is_reported_not_raised(self):
        with patch("sys.platform", "linux"), \
             patch("digimem.desktop.shutil.which", return_value="/usr/bin/notify-send"), \
             patch("digimem.desktop.subprocess.run", side_effect=OSError("no bus")):
            self.assertFalse(desktop.send("title", "body"))

    def test_an_empty_title_is_not_shown(self):
        self.assertFalse(desktop.send("", "body"))

    def test_a_connection_problem_is_marked_urgent(self):
        with patch("sys.platform", "linux"), \
             patch("digimem.desktop.shutil.which", return_value="/usr/bin/notify-send"), \
             patch("digimem.desktop.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stderr = ""
            desktop.send_notification({
                "title": "Can't reach Nextcloud", "message": "Retrying.",
                "event_type": "connection.action_required",
            })
        self.assertIn("critical", run.call_args.args[0])


class ServiceHousekeepingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.settings = SettingsStore(self.root / "config", use_keyring=False)
        self.state = StateStore(self.root / "state.sqlite3")
        self.service = AppService(self.settings, self.state)

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def deliver(self):
        with patch("digimem.desktop.send_notification", return_value=True) as sender:
            count = self.service.deliver_desktop_notifications()
        return count, sender

    # -------------------------------------------------------- notifications

    def test_nothing_is_shown_while_a_window_is_polling(self):
        self.state.create_notification("conflicts.created", "12 faces", "…", "/attention")
        self.service._client_seen = time.monotonic()
        count, sender = self.deliver()
        self.assertEqual(count, 0)
        sender.assert_not_called()
        self.assertEqual(len(self.state.undelivered_notifications()), 1,
                         "left for the page to raise")

    def test_a_closed_window_gets_a_desktop_notification(self):
        self.state.create_notification("conflicts.created", "12 faces", "…", "/attention")
        count, sender = self.deliver()
        self.assertEqual(count, 1)
        self.assertEqual(sender.call_args.args[0]["title"], "12 faces")
        self.assertEqual(self.state.undelivered_notifications(), [])

    def test_the_same_event_is_not_shown_twice(self):
        self.state.create_notification("conflicts.created", "12 faces", "…", "/attention")
        self.deliver()
        count, _ = self.deliver()
        self.assertEqual(count, 0)

    def test_a_switched_off_category_is_filed_not_shown(self):
        self.settings.update_group("notifications", {"decisions": False})
        self.state.create_notification("conflicts.created", "12 faces", "…", "/attention")
        count, sender = self.deliver()
        self.assertEqual(count, 0)
        sender.assert_not_called()
        self.assertEqual(self.state.undelivered_notifications(), [],
                         "settled, so it never comes round again")

    def test_context_events_are_never_shown(self):
        self.state.create_notification("automation.paused", "Paused", "…", "/")
        count, sender = self.deliver()
        self.assertEqual(count, 0)
        sender.assert_not_called()

    def test_a_desktop_that_cannot_show_one_does_not_make_it_repeat(self):
        self.state.create_notification("conflicts.created", "12 faces", "…", "/attention")
        with patch("digimem.desktop.send_notification", return_value=False):
            self.service.deliver_desktop_notifications()
        self.assertEqual(self.state.undelivered_notifications(), [])
        self.assertEqual(len(self.state.unread_notifications()), 1,
                         "and the inbox still has it")

    # -------------------------------------------------------------- backups

    def backup(self, name, age_days=0):
        folder = self.settings.root / "backups"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_bytes(b"x")
        stamp = time.time() - age_days * 86400
        import os
        os.utime(path, (stamp, stamp))
        return path

    def test_only_the_newest_backups_are_kept(self):
        paths = [self.backup(f"digikam4-run-{n}.db", age_days=n) for n in range(6)]
        self.assertEqual(self.service.prune_backups(keep=3), 3)
        remaining = {path.name for path in (self.settings.root / "backups").glob("*.db")}
        self.assertEqual(remaining, {path.name for path in paths[:3]})

    def test_a_backup_a_run_still_needs_is_never_removed(self):
        old = self.backup("digikam4-run-99.db", age_days=100)
        for n in range(5):
            self.backup(f"digikam4-run-{n}.db", age_days=n)
        run_id = self.state.create_run("all")
        self.state.initialize_apply(run_id, [
            {"target": "digikam", "operation": "create_digikam", "path": "a.jpg",
             "person": "Gail", "rect": [0, 0, 1, 1]},
        ])
        self.state.set_apply_backup(run_id, str(old))
        self.state.set_deferred(run_id, "digikam")

        self.service.prune_backups(keep=2)
        self.assertTrue(old.exists(), "its run has not finished with it")

    def test_a_missing_backup_folder_is_not_a_problem(self):
        self.assertEqual(self.service.prune_backups(keep=5), 0)

    # ------------------------------------------------------------ retention

    def test_retention_trims_logs_backups_and_stored_changes(self):
        self.state.append_logs([("2000-01-01T00:00:00", "INFO", "old", None, "ancient")])
        for n in range(15):
            self.backup(f"digikam4-run-{n}.db", age_days=n)
        old_run = self.state.create_run("all")
        self.state.append_preview_actions(old_run, [{"action": "assign"}])
        with self.state.lock:
            self.state.conn.execute(
                "UPDATE runs SET status = 'applied', finished_at = datetime('now', '-200 days')"
                " WHERE id = ?", (old_run,))
            self.state.conn.commit()

        result = self.service.apply_retention()
        self.assertEqual(result["logs"], 1)
        self.assertEqual(result["backups"], 5, "default keeps ten")
        self.assertEqual(result["preview_actions"], 1)

    def test_a_live_runs_changes_are_never_pruned(self):
        run_id = self.state.create_run("all")
        self.state.finish_run(run_id, "previewed", {})
        self.state.append_preview_actions(run_id, [{"action": "assign"}])
        with self.state.lock:
            self.state.conn.execute(
                "UPDATE runs SET finished_at = datetime('now', '-200 days') WHERE id = ?",
                (run_id,))
            self.state.conn.commit()
        self.assertEqual(self.state.prune_preview_actions(days=90), 0)
        self.assertEqual(len(self.state.preview_actions(run_id)), 1)


class CoordinatorHousekeepingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = StateStore(Path(self.tmp.name) / "state.sqlite3")

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def coordinator(self, now):
        from digimem.coordinator import Coordinator

        class App:
            def __init__(self, state):
                self.state = state
                self.runs = 0

            def apply_retention(self):
                self.runs += 1
                return {}

        self.app = App(self.state)
        return Coordinator(self.app, wall=lambda: now[0])

    def test_nothing_is_deleted_in_the_first_day_of_running(self):
        now = [NOW]
        engine = self.coordinator(now)
        self.assertFalse(engine.run_housekeeping())
        self.assertEqual(self.app.runs, 0)

    def test_it_runs_about_once_a_day_after_that(self):
        now = [NOW]
        engine = self.coordinator(now)
        engine.run_housekeeping()

        now[0] = NOW + timedelta(hours=2)
        self.assertFalse(engine.run_housekeeping(), "not yet")

        now[0] = NOW + timedelta(hours=25)
        self.assertTrue(engine.run_housekeeping())
        self.assertEqual(self.app.runs, 1)

    def test_a_failure_does_not_stop_the_coordinator(self):
        now = [NOW]
        engine = self.coordinator(now)
        engine.run_housekeeping()
        now[0] = NOW + timedelta(hours=25)
        with patch.object(self.app, "apply_retention", side_effect=RuntimeError("full disk")):
            self.assertTrue(engine.run_housekeeping())


if __name__ == "__main__":
    unittest.main()
