"""Desktop notifications for a closed window, and keeping storage in check."""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from digimem import desktop, macos_notify, notify
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


class NotificationButtonTest(unittest.TestCase):
    """A notification about a screen carries a button that opens it."""

    def setUp(self):
        desktop._notify_send_takes_actions.cache_clear()
        self.addCleanup(desktop._notify_send_takes_actions.cache_clear)

    def send(self, help_text="  -A, --action=[NAME=]Text", **extra):
        opened = []
        with patch("sys.platform", "linux"), \
             patch("digimem.desktop.shutil.which", return_value="/usr/bin/notify-send"), \
             patch("digimem.desktop.subprocess.run") as run, \
             patch("digimem.desktop.subprocess.Popen") as popen:
            run.return_value.returncode = 0
            run.return_value.stdout = help_text
            run.return_value.stderr = ""
            popen.return_value.communicate.return_value = ("open", "")
            popen.return_value.returncode = 0
            ok = desktop.send(
                "54 changes need attention", "Open DigiMem to retry.",
                open_action=desktop.OpenAction(
                    url="http://127.0.0.1:47818/#/runs/26",
                    show=lambda: opened.append(True),
                ),
                **extra,
            )
        return ok, run, popen, opened

    def test_the_button_is_asked_for_and_the_message_still_comes_last(self):
        ok, _, popen, _ = self.send()
        self.assertTrue(ok)
        command = popen.call_args.args[0]
        self.assertIn("--action", command)
        self.assertIn("open=Open", command)
        self.assertEqual(command[-2:], ["54 changes need attention", "Open DigiMem to retry."])

    def test_pressing_it_opens_the_screen(self):
        _, _, _, opened = self.send()
        for thread in threading.enumerate():
            if thread.name == "digimem-notification":
                thread.join(timeout=5)
        self.assertEqual(opened, [True])

    def test_an_urgent_one_keeps_its_urgency(self):
        _, _, popen, _ = self.send(urgent=True)
        self.assertIn("critical", popen.call_args.args[0])

    def test_a_notify_send_too_old_for_buttons_still_shows_the_message(self):
        ok, run, popen, _ = self.send(help_text="  -u, --urgency=LEVEL")
        self.assertTrue(ok)
        popen.assert_not_called()
        self.assertEqual(run.call_args.args[0][-2:],
                         ["54 changes need attention", "Open DigiMem to retry."])

    def test_a_notification_about_nothing_in_particular_has_no_button(self):
        service = AppService.__new__(AppService)
        self.assertIsNone(service._open_action({"target": ""}))

    def test_the_button_opens_the_screen_the_message_names(self):
        service = AppService.__new__(AppService)
        service.settings = SettingsStore(Path("/tmp/digimem-test-config"), use_keyring=False)
        with patch("digimem.launcher.screen_url", return_value="http://127.0.0.1:1/#/runs/26"):
            action = service._open_action({"target": "/runs/26"})
        self.assertEqual(action.url, "http://127.0.0.1:1/#/runs/26")
        with patch("digimem.launcher.open_ui") as open_ui:
            action.show()
        self.assertEqual(open_ui.call_args.kwargs["target"], "/runs/26")


class MacosNotifierTest(unittest.TestCase):
    """The helper that lets a macOS notification be acted on.

    Only the plumbing is here. Whether AppKit puts a button on the screen is
    not something a Linux test runner can be asked.
    """

    def setUp(self):
        desktop._macos_notifier.cache_clear()
        self.addCleanup(desktop._macos_notifier.cache_clear)
        self.action = desktop.OpenAction(
            url="http://127.0.0.1:47818/#/runs/26", show=lambda: self.opened.append(True)
        )
        self.opened = []

    def probe(self, *, frozen=True, returncode=0):
        run = patch("digimem.desktop.subprocess.run").start()
        self.addCleanup(patch.stopall)
        run.return_value.returncode = returncode
        run.return_value.stdout = ""
        run.return_value.stderr = "no bundle"
        patch("digimem.relaunch.frozen", return_value=frozen).start()
        patch("digimem.relaunch.command", return_value=["/DigiMem.app/x", "notify"]).start()
        return run

    def test_an_unfrozen_digimem_has_no_bundle_to_post_from(self):
        self.probe(frozen=False)
        self.assertIsNone(desktop._macos_notifier())

    def test_a_bundle_macos_will_not_talk_to_is_not_used(self):
        self.probe(returncode=1)
        self.assertIsNone(desktop._macos_notifier())

    def test_the_probe_shows_nothing(self):
        run = self.probe()
        self.assertEqual(desktop._macos_notifier(), ["/DigiMem.app/x", "notify"])
        self.assertEqual(run.call_args.args[0][-1], "--probe")

    def test_the_helper_is_asked_for_the_button(self):
        self.probe()
        with patch("sys.platform", "darwin"), \
             patch("digimem.desktop.shutil.which", return_value="/usr/bin/osascript"), \
             patch("digimem.desktop.subprocess.Popen") as popen:
            popen.return_value.communicate.return_value = ("open", "")
            popen.return_value.returncode = 0
            self.assertTrue(desktop.send("54 changes", "Retry.", open_action=self.action))
        command = popen.call_args.args[0]
        self.assertEqual(command[:2], ["/DigiMem.app/x", "notify"])
        self.assertEqual(command[command.index("--title") + 1], "54 changes")
        self.assertEqual(command[command.index("--action") + 1], "Open")

    def test_a_helper_that_cannot_post_still_gets_the_message_shown(self):
        run = self.probe()
        with patch("sys.platform", "darwin"), \
             patch("digimem.desktop.shutil.which", return_value="/usr/bin/osascript"), \
             patch("digimem.desktop.subprocess.Popen") as popen:
            popen.return_value.communicate.return_value = ("", "Notifications are off")
            popen.return_value.returncode = 1
            desktop.send("54 changes", "Retry.", open_action=self.action)
        for thread in threading.enumerate():
            if thread.name == "digimem-notification":
                thread.join(timeout=5)
        self.assertEqual(run.call_args.args[0][0], "osascript")
        self.assertIn("display notification", run.call_args.args[0][2])

    def test_a_message_about_nothing_in_particular_needs_no_helper(self):
        run = self.probe()
        with patch("sys.platform", "darwin"), \
             patch("digimem.desktop.shutil.which", return_value="/usr/bin/osascript"), \
             patch("digimem.desktop.subprocess.Popen") as popen:
            desktop.send("Sync complete", "Nothing to do.")
        popen.assert_not_called()
        self.assertEqual(run.call_args.args[0][0], "osascript")


class MacosHelperTest(unittest.TestCase):
    def test_it_refuses_where_there_is_no_notification_centre(self):
        """Every platform but the macOS build, which is where it falls back."""
        self.assertEqual(macos_notify.main(["--probe"]), 1)
        self.assertEqual(macos_notify.main(["--title", "x", "--action", "Open"]), 1)


class WindowsToastTest(unittest.TestCase):
    """A toast acts through the shell, so it needs no COM server of its own."""

    def toast(self, open_action=None):
        with patch("sys.platform", "win32"), \
             patch("digimem.desktop.shutil.which", return_value="C:/pwsh.exe"), \
             patch("digimem.desktop.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stderr = ""
            desktop.send("54 changes need attention", "Open DigiMem to retry.",
                         open_action=open_action)
        return run.call_args.kwargs["input"]

    @staticmethod
    def xml(script):
        start = script.index("$xml.LoadXml('") + len("$xml.LoadXml('")
        return script[start:script.index("')", start)]

    def test_the_button_hands_the_address_to_the_shell(self):
        action = desktop.OpenAction(url="http://127.0.0.1:47818/#/runs/26", show=lambda: None)
        document = ET.fromstring(self.xml(self.toast(action)))
        self.assertEqual(document.get("activationType"), "protocol")
        self.assertEqual(document.get("launch"), action.url)
        pressed = document.find("actions/action")
        self.assertEqual(pressed.get("activationType"), "protocol")
        self.assertEqual(pressed.get("arguments"), action.url)
        self.assertEqual(pressed.get("content"), "Open")

    def test_a_message_about_nothing_in_particular_gets_no_button(self):
        document = ET.fromstring(self.xml(self.toast()))
        self.assertIsNone(document.find("actions"))
        self.assertIsNone(document.get("launch"))

    def test_markup_in_a_name_cannot_break_the_toast(self):
        with patch("sys.platform", "win32"), \
             patch("digimem.desktop.shutil.which", return_value="C:/pwsh.exe"), \
             patch("digimem.desktop.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stderr = ""
            desktop.send("Anique & <Roel>", "It's done.")
        document = ET.fromstring(self.xml(run.call_args.kwargs["input"]))
        texts = [node.text for node in document.findall("visual/binding/text")]
        self.assertEqual(texts, ["Anique & <Roel>", "It's done."])


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
