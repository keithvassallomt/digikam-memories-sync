import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path

from digikam_nextcloud.app_service import AppService, resolve_digikam_database
from digikam_nextcloud.local_server import FaceSyncHTTPServer
from digikam_nextcloud.models import NextcloudRequirements
from digikam_nextcloud.models import RegionConflict, SyncReport
from digikam_nextcloud.settings import SettingsStore
from digikam_nextcloud.state_store import StateStore


def make_digikam_database(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection:
        for table in ("Images", "Tags", "TagProperties", "ImageTagProperties"):
            connection.execute(f"CREATE TABLE {table} (id INTEGER)")
        connection.commit()


class FakeBackend:
    def __init__(self, requirements: NextcloudRequirements):
        self.requirements = requirements
        self.closed = False

    def connection_requirements(self):
        return self.requirements

    def close(self):
        self.closed = True


class FakeDigikam:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def person_tag_ids(self):
        return {1: "Gail Vassallo", 2: "Keith Vassallo"}


class AppFoundationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.library = self.root / "Photos"
        self.library.mkdir()
        make_digikam_database(self.library / "digikam4.db")

    def tearDown(self):
        self.temporary.cleanup()

    def test_resolves_a_digikam_folder_or_database(self):
        expected = (self.library / "digikam4.db").resolve()
        self.assertEqual(resolve_digikam_database(self.library), expected)
        self.assertEqual(resolve_digikam_database(expected), expected)

    def test_settings_and_fallback_credential_are_private_and_persistent(self):
        store = SettingsStore(self.root / "config", use_keyring=False)
        store.save({"nc_user": "keith", "nextcloud_url": "https://cloud.test"}, "secret")

        self.assertEqual(store.password("keith"), "secret")
        self.assertTrue(store.public_settings()["has_password"])
        self.assertNotIn("password", store.settings_path.read_text())
        self.assertEqual(store.settings_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(store.secrets_path.stat().st_mode & 0o777, 0o600)

    def test_state_store_persists_notifications_for_the_ui_inbox(self):
        state = StateStore(self.root / "state.sqlite3")
        try:
            notification_id = state.create_notification(
                "conflicts.created",
                "12 conflicts",
                "Click to resolve",
                "/runs/7/conflicts",
                run_id=None,
            )
            unread = state.unread_notifications()
            self.assertEqual(unread[0]["id"], notification_id)
            self.assertEqual(unread[0]["target"], "/runs/7/conflicts")
        finally:
            state.close()

    def test_connection_result_guides_missing_companion_app(self):
        settings = SettingsStore(self.root / "config", use_keyring=False)
        state = StateStore(self.root / "state.sqlite3")
        backend = FakeBackend(
            NextcloudRequirements(True, False, "https://keithvassallo.com")
        )
        service = AppService(settings, state, backend_factory=lambda *a, **k: backend)
        try:
            result = service.test_connection(
                {
                    "digikam_library": str(self.library),
                    "nextcloud_url": "https://cloud.test",
                    "nc_user": "keith",
                    "password": "secret",
                }
            )
            self.assertFalse(result["ready"])
            self.assertEqual(result["install_url"], "https://keithvassallo.com")
            self.assertTrue(backend.closed)
        finally:
            state.close()

    def test_local_api_requires_the_per_process_token(self):
        settings = SettingsStore(self.root / "config", use_keyring=False)
        state = StateStore(self.root / "state.sqlite3")
        service = AppService(settings, state)
        server = FaceSyncHTTPServer(("127.0.0.1", 0), service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}/api/health"
        try:
            with self.assertRaises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(url)
            self.assertEqual(denied.exception.code, 403)
            denied.exception.close()

            request = urllib.request.Request(
                url, headers={"X-Face-Sync-Token": server.api_token}
            )
            with urllib.request.urlopen(request) as response:
                self.assertEqual(json.load(response), {"status": "ok"})
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()
            state.close()

    def test_preview_records_run_conflicts_and_notification(self):
        settings = SettingsStore(self.root / "config", use_keyring=False)
        settings.save(
            {
                "digikam_library": str(self.library),
                "digikam_db": str(self.library / "digikam4.db"),
                "nextcloud_url": "https://cloud.test",
                "nc_user": "keith",
                "nc_photos_path": "Photos",
            },
            "secret",
        )
        state = StateStore(self.root / "state.sqlite3")
        backend = FakeBackend(NextcloudRequirements(True, True, "https://install.test"))
        report = SyncReport(
            files_digikam=10,
            files_matched=10,
            faces_digikam=12,
            faces_nextcloud=11,
            assigned=2,
            inserted=3,
            skipped=6,
            conflicts=[
                RegionConflict(
                    path="2026/photo.jpg",
                    digikam_person="Gail Vassallo",
                    digikam_rect=(0.1, 0.2, 0.3, 0.4),
                    nextcloud_person="Angie Galea",
                    nextcloud_rect=(0.1, 0.2, 0.3, 0.4),
                    iou=1.0,
                    nc_detection_id=9,
                    nc_file_id=7,
                )
            ],
        )
        captured = {}

        def fake_sync(*args, **kwargs):
            captured.update(kwargs)
            return report

        service = AppService(
            settings,
            state,
            backend_factory=lambda *a, **k: backend,
            digikam_factory=FakeDigikam,
            sync_function=fake_sync,
        )
        try:
            result = service.preview({"scope": "person", "person": "Gail Vassallo"})
            self.assertEqual(result["summary"]["conflicts"], 1)
            self.assertEqual(result["direction"], "digikam_to_memories")
            self.assertEqual(captured["only_person"], "Gail Vassallo")
            self.assertFalse(captured["apply"])
            self.assertEqual(state.run(result["run_id"])["status"], "previewed")
            self.assertEqual(
                state.unread_notifications()[0]["event_type"], "conflicts.created"
            )
            conflict_count = state.conn.execute("SELECT COUNT(*) FROM conflicts").fetchone()[0]
            self.assertEqual(conflict_count, 1)
        finally:
            state.close()

    def test_background_preview_exposes_measured_progress(self):
        settings = SettingsStore(self.root / "config", use_keyring=False)
        settings.save(
            {
                "digikam_library": str(self.library),
                "digikam_db": str(self.library / "digikam4.db"),
                "nextcloud_url": "https://cloud.test",
                "nc_user": "keith",
                "nc_photos_path": "Photos",
            },
            "secret",
        )
        state = StateStore(self.root / "state.sqlite3")
        backend = FakeBackend(NextcloudRequirements(True, True, "https://install.test"))
        reached_first_batch = threading.Event()
        finish = threading.Event()

        def fake_sync(*args, **kwargs):
            kwargs["progress_callback"](
                {
                    "phase": "scanning",
                    "current": 4,
                    "total": 10,
                    "matched": 4,
                    "assigned": 2,
                    "inserted": 1,
                    "skipped": 1,
                    "conflicts": 0,
                }
            )
            reached_first_batch.set()
            finish.wait(timeout=2)
            return SyncReport(files_digikam=10, files_matched=10, skipped=10)

        service = AppService(
            settings,
            state,
            backend_factory=lambda *a, **k: backend,
            digikam_factory=FakeDigikam,
            sync_function=fake_sync,
        )
        try:
            job = service.start_preview({"scope": "person", "person": "Gail Vassallo"})
            self.assertTrue(reached_first_batch.wait(timeout=2))
            running = service.preview_status(job["run_id"])
            self.assertEqual(running["progress"]["current"], 4)
            self.assertEqual(running["progress"]["total"], 10)
            self.assertEqual(running["progress"]["matched"], 4)
            thread = service._jobs[job["run_id"]]
            finish.set()
            thread.join(timeout=2)
            completed = service.preview_status(job["run_id"])
            self.assertEqual(completed["status"], "previewed")
            self.assertEqual(completed["progress"]["current"], 10)
            self.assertEqual(completed["progress"]["total"], 10)
        finally:
            finish.set()
            state.close()


if __name__ == "__main__":
    unittest.main()
