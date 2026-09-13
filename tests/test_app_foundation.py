import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from digikam_nextcloud.app_service import AppService, resolve_digikam_database
from digikam_nextcloud.local_server import FaceSyncHTTPServer
from digikam_nextcloud.models import NextcloudRequirements
from digikam_nextcloud.settings import SettingsStore
from digikam_nextcloud.state_store import StateStore


def make_digikam_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        for table in ("Images", "Tags", "TagProperties", "ImageTagProperties"):
            connection.execute(f"CREATE TABLE {table} (id INTEGER)")


class FakeBackend:
    def __init__(self, requirements: NextcloudRequirements):
        self.requirements = requirements
        self.closed = False

    def connection_requirements(self):
        return self.requirements

    def close(self):
        self.closed = True


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


if __name__ == "__main__":
    unittest.main()
