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
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from digikam_nextcloud import autostart, shortcuts
from digikam_nextcloud.log_store import SQLiteLogHandler, run_context
from digikam_nextcloud.service import (
    EXIT_ALREADY_RUNNING,
    ServiceLock,
    read_service_info,
    remove_service_info,
    run_service,
    service_is_live,
    write_service_info,
)
from digikam_nextcloud.state_store import StateStore


def make_digikam_database(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection:
        for table in ("Images", "Tags", "TagProperties", "ImageTagProperties"):
            connection.execute(f"CREATE TABLE {table} (id INTEGER)")
        connection.commit()


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
            headers={"X-Face-Sync-Token": self.info()["token"]},
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
        logging.getLogger("digikam_nextcloud.test").info("a line for the interface")
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
        self.log = logging.getLogger("digikam_nextcloud.logtest")
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
            "digikam_nextcloud.autostart._has_systemd", return_value=False
        ), patch("sys.platform", "linux"):
            self.assertEqual(autostart.mechanism(), "xdg")
            result = autostart.enable(self.home / "config")
            path = Path(result["path"])
            self.assertTrue(path.is_file())
            body = path.read_text()
            self.assertIn("digikam_nextcloud", body)
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
            "digikam_nextcloud.autostart._has_systemd", return_value=True
        ), patch("sys.platform", "linux"), patch(
            "digikam_nextcloud.autostart._systemctl", side_effect=record
        ):
            result = autostart.enable(None)
            unit = Path(result["path"]).read_text()
            self.assertIn("WantedBy=default.target", unit)
            self.assertIn("Restart=on-failure", unit)
            self.assertIn(("enable", "face-sync.service"), calls)
            self.assertIn(("start", "face-sync.service"), calls)

    def test_a_refusing_systemd_is_reported(self) -> None:
        with patch.dict("os.environ", {"XDG_CONFIG_HOME": str(self.home)}), patch(
            "digikam_nextcloud.autostart._has_systemd", return_value=True
        ), patch("sys.platform", "linux"), patch(
            "digikam_nextcloud.autostart._systemctl", return_value=(1, "no session")
        ):
            with self.assertRaises(RuntimeError):
                autostart.enable(None)


class ShortcutTest(unittest.TestCase):
    def test_desktop_shortcut_opens_the_interface(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ", {"XDG_DATA_HOME": tmp}
        ), patch("sys.platform", "linux"), patch(
            "digikam_nextcloud.shortcuts.subprocess.run"
        ):
            result = shortcuts.install(None)
            path = Path(result["path"])
            body = path.read_text()
            self.assertIn("Name=Face Sync", body)
            self.assertIn('"ui"', body, "the shortcut opens the interface, not the service")
            self.assertNotIn('"service"', body)
            self.assertTrue(shortcuts.status()["installed"])
            shortcuts.remove()
            self.assertFalse(shortcuts.status()["installed"])


if __name__ == "__main__":
    unittest.main()
