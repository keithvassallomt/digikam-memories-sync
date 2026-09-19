"""Tests for the background service, its discovery files and its log store."""
from __future__ import annotations

import json
import logging
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import sys
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch

from digimem import autostart, relaunch, shortcuts
from digimem.log_store import SQLiteLogHandler, run_context
from digimem.service import (
    EXIT_ALREADY_RUNNING,
    ServiceLock,
    read_service_info,
    remove_service_info,
    run_service,
    service_is_live,
    write_service_info,
)
from digimem.state_store import StateStore


def make_digikam_database(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection:
        for table in ("Images", "Tags", "TagProperties", "ImageTagProperties"):
            connection.execute(f"CREATE TABLE {table} (id INTEGER)")
        connection.commit()



@contextmanager
def source_checkout():
    """Pin the installation shape these tests are about.

    What a shortcut or login item has to say depends on how DigiMem was
    installed, and a test environment may or may not have a console script on
    the path — `pip install -e .` puts one there, a bare checkout does not.
    Left to chance, these tests assert whatever the machine running them
    happens to be, which is how they passed locally and failed in CI.
    """
    with patch("digimem.relaunch.frozen", return_value=False), patch(
        "digimem.relaunch.console_script", return_value=None
    ):
        yield


class ServiceLockTest(unittest.TestCase):
    def test_second_lock_is_refused_while_the_first_is_held(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "service.lock"
            first, second = ServiceLock(path), ServiceLock(path)
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.release()
            self.assertTrue(second.acquire())
            second.release()

    def test_lock_records_the_owning_process(self) -> None:
        import os

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "service.lock"
            lock = ServiceLock(path)
            self.assertTrue(lock.acquire())
            self.assertEqual(path.read_text().strip(), str(os.getpid()))
            lock.release()


class ServiceInfoTest(unittest.TestCase):
    def test_roundtrip_and_removal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = {"pid": 1, "port": 47818, "token": "abc", "started_at": "now"}
            write_service_info(tmp, payload)
            self.assertEqual(read_service_info(tmp), payload)
            remove_service_info(tmp)
            self.assertIsNone(read_service_info(tmp))

    def test_incomplete_or_corrupt_files_read_as_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "service.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertIsNone(read_service_info(tmp))
            path.write_text(json.dumps({"port": 1}), encoding="utf-8")
            self.assertIsNone(read_service_info(tmp), "a file with no token is unusable")

    def test_a_dead_service_is_not_reported_live(self) -> None:
        self.assertFalse(service_is_live(None))
        self.assertFalse(service_is_live({"port": 1, "token": "x"}, timeout=0.3))


class RunningServiceTest(unittest.TestCase):
    """Start a real service on a real socket and talk to it."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.server: object | None = None
        self.started = threading.Event()
        self.result: int | None = None

        def capture(server: object) -> None:
            self.server = server
            self.started.set()

        self.thread = threading.Thread(
            target=self._run, args=(capture,), daemon=True
        )
        self.thread.start()
        if not self.started.wait(20):
            raise AssertionError("The service did not start")

    def _run(self, capture) -> None:  # type: ignore[no-untyped-def]
        self.result = run_service(self.root, port=0, on_start=capture)

    def tearDown(self) -> None:
        if self.server is not None:
            self.server.shutdown()  # type: ignore[attr-defined]
        self.thread.join(timeout=20)
        logging.getLogger().handlers.clear()
        self.tmp.cleanup()

    def info(self) -> dict:
        found = read_service_info(self.root)
        assert found is not None
        return found

    def get(self, path: str) -> dict:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.info()['port']}{path}",
            headers={"X-DigiMem-Token": self.info()["token"]},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read())

    def test_it_publishes_itself_and_answers(self) -> None:
        self.assertTrue(service_is_live(self.info()))
        self.assertEqual(self.get("/api/health"), {"status": "ok"})

    def test_a_second_service_refuses_to_start(self) -> None:
        self.assertEqual(run_service(self.root, port=0), EXIT_ALREADY_RUNNING)

    def test_the_token_is_required(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(
                f"http://127.0.0.1:{self.info()['port']}/api/logs", timeout=5
            )
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()

    def test_logs_reach_the_interface(self) -> None:
        logging.getLogger("digimem.test").info("a line for the interface")
        for handler in logging.getLogger().handlers:
            if isinstance(handler, SQLiteLogHandler):
                self.assertTrue(handler.flush(), "the log writer did not drain")
        messages = [entry["message"] for entry in self.get("/api/logs")["entries"]]
        self.assertIn("a line for the interface", messages)

    def test_service_details_are_reported(self) -> None:
        details = self.get("/api/service")
        self.assertTrue(details["running"])
        self.assertEqual(details["port"], self.info()["port"])
        self.assertIn("autostart", details)
        self.assertIn("shortcut", details)

    def test_shutdown_clears_the_published_file(self) -> None:
        self.server.shutdown()  # type: ignore[attr-defined]
        self.thread.join(timeout=20)
        self.server = None
        self.assertIsNone(read_service_info(self.root))
        self.assertEqual(self.result, 0)


class LogStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.tmp.name) / "state.sqlite3")
        self.handler = SQLiteLogHandler(self.store)
        self.handler.setFormatter(logging.Formatter("%(message)s"))
        self.log = logging.getLogger("digimem.logtest")
        self.log.handlers = [self.handler]
        self.log.propagate = False
        self.log.setLevel(logging.DEBUG)

    def tearDown(self) -> None:
        self.handler.close()
        self.log.handlers = []
        self.store.close()
        self.tmp.cleanup()

    def drain(self) -> None:
        self.assertTrue(self.handler.flush(), "the log writer did not drain")

    def test_records_carry_the_run_they_belong_to(self) -> None:
        self.log.info("no run here")
        with run_context(7):
            self.log.info("inside a run")
        self.log.info("no run again")
        self.drain()
        self.assertEqual(
            [entry["message"] for entry in self.store.logs(run_id=7)["entries"]],
            ["inside a run"],
        )

    def test_level_filter_includes_everything_more_severe(self) -> None:
        self.log.debug("quiet")
        self.log.info("normal")
        self.log.warning("odd")
        self.log.error("bad")
        self.drain()
        self.assertEqual(
            sorted(e["level"] for e in self.store.logs(level="WARNING")["entries"]),
            ["ERROR", "WARNING"],
        )
        self.assertEqual(len(self.store.logs(level="DEBUG")["entries"]), 4)

    def test_unknown_level_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.logs(level="CHATTY")

    def test_search_treats_wildcards_as_text(self) -> None:
        self.log.info("progress is 50% done")
        self.log.info("nothing to see")
        self.drain()
        found = [e["message"] for e in self.store.logs(query="50%")["entries"]]
        self.assertEqual(found, ["progress is 50% done"])

    def test_following_returns_only_newer_lines(self) -> None:
        self.log.info("first")
        self.drain()
        newest = self.store.logs()["newest_id"]
        self.log.info("second")
        self.drain()
        self.assertEqual(
            [e["message"] for e in self.store.logs(after_id=newest)["entries"]], ["second"]
        )

    def test_paging_reports_more(self) -> None:
        for index in range(5):
            self.log.info("line %s", index)
        self.drain()
        page = self.store.logs(limit=2)
        self.assertTrue(page["has_more"])
        self.assertEqual(len(page["entries"]), 2)

    def test_retention_removes_old_lines(self) -> None:
        self.store.append_logs([("2000-01-01T00:00:00", "INFO", "old", None, "ancient")])
        self.log.info("recent")
        self.drain()
        self.assertEqual(self.store.delete_old_logs(days=30), 1)
        self.assertEqual(
            [e["message"] for e in self.store.logs()["entries"]], ["recent"]
        )

    def test_a_broken_database_never_breaks_the_caller(self) -> None:
        self.store.close()
        self.log.info("written after the database closed")
        self.handler.flush(timeout=2.0)  # must not raise


class AutostartTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_desktop_entry_is_written_when_systemd_is_absent(self) -> None:
        with patch.dict("os.environ", {"XDG_CONFIG_HOME": str(self.home)}), patch(
            "digimem.autostart._has_systemd", return_value=False
        ), patch("sys.platform", "linux"):
            self.assertEqual(autostart.mechanism(), "xdg")
            result = autostart.enable(self.home / "config")
            path = Path(result["path"])
            self.assertTrue(path.is_file())
            body = path.read_text()
            self.assertIn("digimem", body)
            self.assertIn("service", body)
            self.assertTrue(autostart.status()["enabled"])

            autostart.disable(self.home / "config")
            self.assertFalse(path.exists())
            self.assertFalse(autostart.status()["enabled"])

    def test_systemd_unit_is_written_and_enabled(self) -> None:
        calls: list[tuple[str, ...]] = []

        def record(*arguments: str) -> tuple[int, str]:
            calls.append(arguments)
            return 0, ""

        with patch.dict("os.environ", {"XDG_CONFIG_HOME": str(self.home)}), patch(
            "digimem.autostart._has_systemd", return_value=True
        ), patch("sys.platform", "linux"), patch(
            "digimem.autostart._systemctl", side_effect=record
        ):
            result = autostart.enable(None)
            unit = Path(result["path"]).read_text()
            self.assertIn("WantedBy=default.target", unit)
            self.assertIn("Restart=on-failure", unit)
            self.assertIn(("enable", "digimem.service"), calls)
            self.assertIn(("start", "digimem.service"), calls)

    def test_a_refusing_systemd_is_reported(self) -> None:
        with patch.dict("os.environ", {"XDG_CONFIG_HOME": str(self.home)}), patch(
            "digimem.autostart._has_systemd", return_value=True
        ), patch("sys.platform", "linux"), patch(
            "digimem.autostart._systemctl", return_value=(1, "no session")
        ):
            with self.assertRaises(RuntimeError):
                autostart.enable(None)


class ShortcutTest(unittest.TestCase):
    def test_desktop_shortcut_opens_the_interface(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ", {"XDG_DATA_HOME": tmp}
        ), patch("sys.platform", "linux"), patch(
            "digimem.shortcuts.subprocess.run"
        ):
            result = shortcuts.install(None)
            path = Path(result["path"])
            body = path.read_text()
            self.assertIn("Name=DigiMem", body)
            self.assertIn('"ui"', body, "the shortcut opens the interface, not the service")
            self.assertNotIn('"service"', body)
            self.assertTrue(shortcuts.status()["installed"])
            shortcuts.remove()
            self.assertFalse(shortcuts.status()["installed"])

    @source_checkout()
    def test_the_entry_says_where_to_run_from_a_checkout(self) -> None:
        """Recording the interpreter is half the story. Run from a source
        checkout the package is not installed for it, and is found through the
        working directory. Without that the entry launches, fails to import and
        exits with no window, which looks exactly like nothing happening."""
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ", {"XDG_DATA_HOME": tmp}
        ), patch("sys.platform", "linux"), patch("digimem.shortcuts.subprocess.run"):
            body = Path(shortcuts.install(None)["path"]).read_text()
        line = next(l for l in body.splitlines() if l.startswith("Path="))
        root = Path(line.removeprefix("Path="))
        self.assertTrue(
            (root / "digimem" / "__init__.py").is_file(),
            "the recorded directory has to be the one that makes -m work")

    def test_a_properly_installed_package_needs_no_working_directory(self) -> None:
        root = str(Path(shortcuts.__file__).resolve().parent.parent)
        with patch(
            "digimem.relaunch.sysconfig.get_paths",
            return_value={"purelib": root, "platlib": root},
        ), patch("digimem.relaunch.console_script", return_value=None):
            self.assertIsNone(shortcuts.working_directory())

    @source_checkout()
    def test_the_macos_runner_moves_before_it_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "DigiMem.app"
            shortcuts._install_macos(None, bundle)
            runner = (bundle / "Contents" / "MacOS" / "digimem").read_text()
        self.assertIn("cd ", runner)
        self.assertLess(
            runner.index("cd "), runner.index("exec "), "moving after is too late")

    @source_checkout()
    def test_the_windows_shortcut_records_where_to_run(self) -> None:
        script = shortcuts.shortcut_script(Path("C:/DigiMem.lnk"), None)
        self.assertIn("$s.WorkingDirectory = '", script)

    def test_every_platform_ships_the_icon_it_needs(self) -> None:
        for suffix in (".png", ".ico", ".icns"):
            with self.subTest(suffix=suffix):
                icon = shortcuts.icon_path(suffix)
                self.assertTrue(icon.is_file(), f"{icon} is not packaged")
                self.assertGreater(icon.stat().st_size, 0)

    def test_the_desktop_entry_points_at_a_real_icon(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ", {"XDG_DATA_HOME": tmp}
        ), patch("sys.platform", "linux"), patch("digimem.shortcuts.subprocess.run"):
            body = Path(shortcuts.install(None)["path"]).read_text()
        line = next(l for l in body.splitlines() if l.startswith("Icon="))
        self.assertTrue(
            Path(line.removeprefix("Icon=")).is_file(),
            "an absolute Icon= that does not exist shows as a blank entry")

    def test_the_macos_bundle_carries_its_icon(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "DigiMem.app"
            shortcuts._install_macos(None, bundle)
            plist = (bundle / "Contents" / "Info.plist").read_text()
            self.assertIn("<key>CFBundleIconFile</key>", plist)
            self.assertIn("<string>digimem</string>", plist)
            self.assertTrue(
                (bundle / "Contents" / "Resources" / "digimem.icns").is_file(),
                "the plist names an icon, so the icon has to be in there")

    @source_checkout()
    def test_the_windows_shortcut_is_a_lnk_that_carries_an_icon(self) -> None:
        """A .cmd cannot have an icon. A .lnk can, and PowerShell makes one
        without adding a dependency, since Windows already needs it."""
        target = Path("C:/Start Menu/DigiMem.lnk")
        script = shortcuts.shortcut_script(target, None)
        self.assertIn("CreateShortcut('C:/Start Menu/DigiMem.lnk')", script)
        self.assertIn("$s.IconLocation = '", script)
        self.assertIn("digimem.ico", script)
        self.assertIn('"-m" "digimem" "ui"', script)
        self.assertIn("$s.Save()", script)

    def test_a_windows_path_with_a_quote_cannot_break_out_of_the_script(self) -> None:
        script = shortcuts.shortcut_script(Path("C:/it's here/DigiMem.lnk"), None)
        self.assertIn("'C:/it''s here/DigiMem.lnk'", script)

    @source_checkout()
    def test_windows_falls_back_to_a_cmd_with_no_powershell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
            "digimem.shortcuts.shutil.which", return_value=None
        ):
            written = shortcuts._install_windows(None, Path(tmp) / "DigiMem.lnk")
            self.assertEqual(written.suffix, ".cmd")
            self.assertIn('"-m" "digimem" "ui"', written.read_text())

    def test_installing_one_windows_shape_clears_the_other(self) -> None:
        """Two menu entries for one application is worse than none."""
        with tempfile.TemporaryDirectory() as tmp, patch(
            "sys.platform", "win32"
        ), patch.dict("os.environ", {"APPDATA": tmp}), patch(
            "digimem.shortcuts.shutil.which", return_value=None
        ):
            stale = shortcuts.shortcut_paths()[0]
            stale.parent.mkdir(parents=True, exist_ok=True)
            stale.write_text("a .lnk from an earlier install")
            result = shortcuts.install(None)
            self.assertTrue(result["path"].endswith(".cmd"))
            self.assertFalse(stale.exists(), "the .lnk should have been cleared")
            self.assertTrue(shortcuts.status()["installed"])
            shortcuts.remove()
            self.assertFalse(shortcuts.status()["installed"])


if __name__ == "__main__":
    unittest.main()


class RelaunchTest(unittest.TestCase):
    """How DigiMem starts another copy of itself, in each shape it ships in.

    Getting this wrong does not raise anything: the entry launches, fails to
    import itself, and exits without a window, which looks exactly like
    clicking it did nothing.
    """

    def test_a_checkout_runs_the_interpreter_with_dash_m(self) -> None:
        with source_checkout():
            command = relaunch.command("service")
            self.assertEqual(command[1:], ["-m", "digimem", "service"])
            self.assertEqual(
                relaunch.working_directory(), relaunch.package_root(),
                "a checkout is only importable from its own directory")

    def test_an_installed_copy_names_its_script(self) -> None:
        """A distribution package finds itself through a wrapper that sets
        PYTHONPATH, and the wrapper is not what runs at login. Naming the
        script rather than the interpreter is what survives that."""
        script = Path("/usr/bin/digimem")
        with patch("digimem.relaunch.frozen", return_value=False), patch(
            "digimem.relaunch.console_script", return_value=script
        ):
            self.assertEqual(
                relaunch.command("service"), ["/usr/bin/digimem", "service"])
            self.assertIsNone(
                relaunch.working_directory(),
                "an installed package is found by import, not by directory")

    def test_a_frozen_bundle_is_its_own_executable(self) -> None:
        """A bundle has no interpreter to hand "-m digimem" to."""
        bundle = "/Applications/DigiMem.app/Contents/MacOS/digimem"
        with patch("digimem.relaunch.frozen", return_value=True), patch.object(
            sys, "executable", bundle
        ):
            self.assertEqual(relaunch.command("service"), [bundle, "service"])
            self.assertIsNone(relaunch.working_directory())

    def test_the_configuration_directory_is_carried_through(self) -> None:
        with source_checkout():
            command = relaunch.command("service", "/tmp/somewhere")
        self.assertEqual(command[-2:], ["--config-dir", "/tmp/somewhere"])


class AutostartDirectoryTest(unittest.TestCase):
    """A login item has to say where to run, not just what to run.

    Every mechanism here starts a service in the home directory, which is the
    one place ``-m digimem`` cannot find a source checkout from. Getting this
    wrong costs nothing at install time and fails on every login afterwards,
    which with Restart=on-failure is a crash loop rather than a crash.
    """

    @source_checkout()
    def test_the_systemd_unit_says_where_to_run(self) -> None:
        unit = autostart._systemd_unit(None)
        line = next(l for l in unit.splitlines() if l.startswith("WorkingDirectory="))
        root = Path(line.removeprefix("WorkingDirectory="))
        self.assertTrue((root / "digimem" / "__init__.py").is_file())
        self.assertLess(
            unit.index("[Service]"), unit.index("WorkingDirectory="),
            "WorkingDirectory only means anything inside [Service]")

    @source_checkout()
    def test_the_xdg_entry_says_where_to_run(self) -> None:
        entry = autostart._xdg_desktop(None)
        line = next(l for l in entry.splitlines() if l.startswith("Path="))
        self.assertTrue(
            (Path(line.removeprefix("Path=")) / "digimem" / "__init__.py").is_file())

    @source_checkout()
    def test_the_launch_agent_says_where_to_run(self) -> None:
        plist = autostart._launch_agent(None)
        self.assertIn("<key>WorkingDirectory</key>", plist)

    @source_checkout()
    def test_the_windows_login_item_moves_before_it_runs(self) -> None:
        command = autostart._windows_command(None)
        self.assertLess(
            command.index("cd /d"), command.index("digimem"),
            "moving after the command has already started is too late")

    def test_an_installed_copy_needs_no_directory(self) -> None:
        """Only a checkout does. Naming one for an installed package would
        pin the service to a directory that may not survive an upgrade."""
        with patch("digimem.relaunch.frozen", return_value=False), patch(
            "digimem.relaunch.console_script", return_value=Path("/usr/bin/digimem")
        ):
            self.assertNotIn("WorkingDirectory=", autostart._systemd_unit(None))
            self.assertNotIn("Path=", autostart._xdg_desktop(None))
            self.assertNotIn("WorkingDirectory", autostart._launch_agent(None))
            self.assertNotIn("cd /d", autostart._windows_command(None))
