import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

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

    def list_named_people(self):
        return ["Gail Vassallo", "April Vassallo"]

    def list_named_faces(self, person=None, **kwargs):
        callback = kwargs.get("progress_callback")
        if callback:
            callback(0)
        return []

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

    def image_ids_for_person(self, person):
        return list(range(10))

    def count_images_with_faces(self):
        return 10

    def images_for_relative_paths(self, paths):
        return {}


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

    def test_conflict_choices_are_persisted_and_can_resolve_all_remaining(self):
        state = StateStore(self.root / "state.sqlite3")
        try:
            run_id = state.create_run("person")
            state.save_conflicts(
                run_id,
                [
                    {"path": "2026/one.jpg", "digikam_person": "Gail", "nextcloud_person": "Angie"},
                    {"path": "2026/two.jpg", "digikam_person": "Gail", "nextcloud_person": "April"},
                ],
            )
            conflicts = state.conflicts_for_run(run_id)
            first_id = conflicts["conflicts"][0]["id"]

            resolved = state.resolve_conflict(
                run_id,
                first_id,
                "digikam",
                apply_to_remaining=True,
            )

            self.assertEqual(resolved["resolved"], 2)
            self.assertEqual(resolved["remaining"], 0)
            self.assertTrue(
                all(item["resolution"] == "digikam" for item in resolved["conflicts"])
            )
        finally:
            state.close()

    def test_conflict_photo_is_limited_to_the_configured_library(self):
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
        photo_dir = self.library / "2026"
        photo_dir.mkdir()
        photo = photo_dir / "photo.jpg"
        photo.write_bytes(b"test-image")
        outside = self.root / "outside.jpg"
        outside.write_bytes(b"private")
        state = StateStore(self.root / "state.sqlite3")
        service = AppService(settings, state)
        try:
            run_id = state.create_run("person")
            state.save_conflicts(run_id, [{"path": "2026/photo.jpg"}])
            conflict_id = state.conflicts_for_run(run_id)["conflicts"][0]["id"]
            body, content_type = service.conflict_photo(run_id, conflict_id)
            self.assertEqual(body, b"test-image")
            self.assertEqual(content_type, "image/jpeg")

            other_run = state.create_run("person")
            state.save_conflicts(other_run, [{"path": "../outside.jpg"}])
            other_id = state.conflicts_for_run(other_run)["conflicts"][0]["id"]
            with self.assertRaisesRegex(ValueError, "path is invalid"):
                service.conflict_photo(other_run, other_id)

            heic = photo_dir / "photo.heic"
            heic.write_bytes(b"heic-original")
            heic_run = state.create_run("person")
            state.save_conflicts(
                heic_run,
                [{"path": "2026/photo.heic", "nc_file_id": 77}],
            )
            heic_id = state.conflicts_for_run(heic_run)["conflicts"][0]["id"]
            with patch(
                "digikam_nextcloud.app_service.fetch_file_preview",
                return_value=(b"jpeg-preview", "image/jpeg"),
            ) as fetch_preview:
                body, content_type = service.conflict_photo(heic_run, heic_id)
            self.assertEqual(body, b"jpeg-preview")
            self.assertEqual(content_type, "image/jpeg")
            fetch_preview.assert_called_once_with(
                "https://cloud.test", "keith", "secret", 77
            )
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

    def test_local_api_lists_resolves_and_serves_conflict_photos(self):
        settings = SettingsStore(self.root / "config", use_keyring=False)
        settings.save(
            {
                "digikam_library": str(self.library),
                "digikam_db": str(self.library / "digikam4.db"),
                "nextcloud_url": "https://cloud.test",
                "nc_user": "keith",
            },
            "secret",
        )
        photo_dir = self.library / "2026"
        photo_dir.mkdir()
        (photo_dir / "photo.jpg").write_bytes(b"photo-bytes")
        state = StateStore(self.root / "state.sqlite3")
        run_id = state.create_run("person")
        state.save_conflicts(
            run_id,
            [
                {
                    "path": "2026/photo.jpg",
                    "digikam_person": "Gail Vassallo",
                    "nextcloud_person": "Angie Galea",
                    "digikam_rect": [0.1, 0.2, 0.3, 0.4],
                    "nextcloud_rect": [0.1, 0.2, 0.3, 0.4],
                    "iou": 1.0,
                }
            ],
        )
        service = AppService(settings, state)
        server = FaceSyncHTTPServer(("127.0.0.1", 0), service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}/api/runs/{run_id}/conflicts"
        headers = {"X-Face-Sync-Token": server.api_token}
        try:
            request = urllib.request.Request(base, headers=headers)
            with urllib.request.urlopen(request) as response:
                listing = json.load(response)
            conflict_id = listing["conflicts"][0]["id"]

            payload = json.dumps(
                {"resolution": "memories", "apply_to_remaining": False}
            ).encode()
            request = urllib.request.Request(
                f"{base}/{conflict_id}",
                data=payload,
                method="POST",
                headers={**headers, "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request) as response:
                resolved = json.load(response)
            self.assertEqual(resolved["remaining"], 0)
            self.assertEqual(resolved["conflicts"][0]["resolution"], "memories")

            request = urllib.request.Request(
                f"{base}/{conflict_id}/photo", headers=headers
            )
            with urllib.request.urlopen(request) as response:
                self.assertEqual(response.read(), b"photo-bytes")
                self.assertEqual(response.headers.get_content_type(), "image/jpeg")
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()
            state.close()

    def test_local_api_reviews_and_remembers_failed_faces(self):
        settings = SettingsStore(self.root / "config", use_keyring=False)
        settings.save(
            {
                "digikam_library": str(self.library),
                "digikam_db": str(self.library / "digikam4.db"),
                "nextcloud_url": "https://cloud.test",
                "nc_user": "keith",
            },
            "secret",
        )
        photo_dir = self.library / "2026"
        photo_dir.mkdir()
        (photo_dir / "photo.jpg").write_bytes(b"photo-bytes")
        action = {
            "target": "memories",
            "operation": "insert_memories",
            "action": "insert",
            "path": "2026/photo.jpg",
            "person": "Gail Vassallo",
            "rect": [0.1, 0.2, 0.3, 0.4],
            "nc_file_id": 7,
        }
        state = StateStore(self.root / "state.sqlite3")
        run_id = state.create_run("person")
        state.initialize_apply(run_id, [action])
        failed = state.pending_apply_actions(run_id)[0]
        state.finish_apply_action(failed["id"], "failed", error="No face found")
        state.finish_apply(run_id, "apply_failed")
        service = AppService(settings, state)
        server = FaceSyncHTTPServer(("127.0.0.1", 0), service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}/api/runs/{run_id}/failures"
        headers = {"X-Face-Sync-Token": server.api_token}
        try:
            with urllib.request.urlopen(urllib.request.Request(base, headers=headers)) as response:
                listing = json.load(response)
            self.assertEqual(listing["remaining"], 1)
            self.assertEqual(listing["failures"][0]["source"], "digikam")

            photo_url = f"{base}/{failed['id']}/photo"
            with urllib.request.urlopen(urllib.request.Request(photo_url, headers=headers)) as response:
                self.assertEqual(response.read(), b"photo-bytes")

            request = urllib.request.Request(
                f"{base}/{failed['id']}",
                data=json.dumps({"decision": "keep_source"}).encode(),
                method="POST",
                headers={**headers, "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request) as response:
                resolved = json.load(response)
            self.assertEqual(resolved["status"], "applied")
            self.assertEqual(resolved["ignored"], 1)
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
            self.assertEqual(result["direction"], "two_way")
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
