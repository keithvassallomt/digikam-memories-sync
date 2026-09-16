import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from digikam_nextcloud.apply import ApplyExecutor, build_apply_plan, plan_summary
from digikam_nextcloud.app_service import AppService, is_systemic_apply_failure
from digikam_nextcloud.digikam_writer import (
    DigikamWriter,
    create_sqlite_backup,
    digikam_probe_supported,
    digikam_process_ids,
    terminate_digikam,
)
from digikam_nextcloud.models import FaceRegion, NextcloudFile, NextcloudRequirements, Rect
from digikam_nextcloud.nextcloud_http import NextcloudConnectionError
from digikam_nextcloud.settings import SettingsStore
from digikam_nextcloud.state_store import StateStore


def make_writable_digikam(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE Images (
                id INTEGER PRIMARY KEY, album INTEGER, name TEXT NOT NULL,
                status INTEGER NOT NULL, category INTEGER NOT NULL DEFAULT 1,
                fileSize INTEGER, uniqueHash TEXT, UNIQUE(album, name)
            );
            CREATE TABLE Albums (
                id INTEGER PRIMARY KEY, albumRoot INTEGER NOT NULL,
                relativePath TEXT NOT NULL
            );
            CREATE TABLE AlbumRoots (id INTEGER PRIMARY KEY, specificPath TEXT);
            CREATE TABLE ImageInformation (
                imageid INTEGER PRIMARY KEY, orientation INTEGER, width INTEGER, height INTEGER
            );
            CREATE TABLE Tags (
                id INTEGER PRIMARY KEY, pid INTEGER, name TEXT NOT NULL,
                icon INTEGER, iconkde TEXT, UNIQUE(name, pid)
            );
            CREATE TABLE TagsTree (id INTEGER NOT NULL, pid INTEGER NOT NULL, UNIQUE(id, pid));
            CREATE TABLE TagProperties (tagid INTEGER, property TEXT, value TEXT);
            CREATE TABLE ImageTags (imageid INTEGER NOT NULL, tagid INTEGER NOT NULL, UNIQUE(imageid, tagid));
            CREATE TABLE ImageTagProperties (imageid INTEGER, tagid INTEGER, property TEXT, value TEXT);
            CREATE TRIGGER insert_tagstree AFTER INSERT ON Tags BEGIN
                INSERT INTO TagsTree SELECT NEW.id, NEW.pid
                UNION SELECT NEW.id, pid FROM TagsTree WHERE id=NEW.pid;
            END;

            INSERT INTO AlbumRoots VALUES (1, '/library');
            INSERT INTO Albums VALUES (2, 1, '/2026');
            INSERT INTO Images(id, album, name, status, fileSize, uniqueHash)
                VALUES (3, 2, 'photo.jpg', 1, 10, 'hash');
            INSERT INTO ImageInformation VALUES (3, 1, 1000, 800);
            INSERT INTO Tags VALUES (4, 0, 'People', NULL, NULL);
            INSERT INTO Tags VALUES (34, 4, 'Gail Vassallo', NULL, NULL);
            INSERT INTO Tags VALUES (44, 4, 'Angie Galea', NULL, NULL);
            INSERT INTO TagProperties VALUES (34, 'person', 'Gail Vassallo');
            INSERT INTO TagProperties VALUES (34, 'faceEngineId', 'Gail Vassallo');
            INSERT INTO TagProperties VALUES (34, 'faceEngineUuid', '{gail}');
            INSERT INTO TagProperties VALUES (44, 'person', 'Angie Galea');
            INSERT INTO TagProperties VALUES (44, 'faceEngineId', 'Angie Galea');
            INSERT INTO TagProperties VALUES (44, 'faceEngineUuid', '{angie}');
            INSERT INTO ImageTags VALUES (3, 44);
            INSERT INTO ImageTagProperties VALUES
                (3, 44, 'tagRegion', '<rect x="100" y="80" width="200" height="160"/>'),
                (3, 44, 'faceToTrain', '<rect x="100" y="80" width="200" height="160"/>');
            """
        )
        connection.commit()


class FakeApplyBackend:
    supports_assign = False

    def __init__(self, faces=None):
        self.file = NextcloudFile(
            file_id=7, path="Photos/2026/photo.jpg", name="photo.jpg", size=10,
            webdav_path="Photos/2026/photo.jpg",
        )
        self.faces = list(faces or [])
        self.closed = False
        self.last_insert = None
        self.last_insert_score = None

    def connection_requirements(self):
        return NextcloudRequirements(True, True, "https://install.test")

    def resolve_file_with_faces(self, path):
        return (self.file, self.faces) if path == self.file.webdav_path else (None, [])

    def get_or_create_cluster(self, person):
        return 5

    def assign_person(self, face, person, nc_file, cluster):
        face.person = person
        face.dav_parent = person

    def insert_detection(self, **kwargs):
        self.last_insert = kwargs
        return 99

    def close(self):
        self.closed = True


class DigikamWriterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "digikam4.db"
        make_writable_digikam(self.database)

    def tearDown(self):
        self.temp.cleanup()

    def test_creates_person_and_face_idempotently(self):
        with DigikamWriter(self.database) as writer:
            first = writer.create_face(
                "2026/photo.jpg", "April Vassallo", (0.6, 0.2, 0.2, 0.3), image_id=3
            )
            second = writer.create_face(
                "2026/photo.jpg", "April Vassallo", (0.6, 0.2, 0.2, 0.3), image_id=3
            )
        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])
        with closing(sqlite3.connect(self.database)) as connection:
            tag = connection.execute("SELECT id FROM Tags WHERE name='April Vassallo'").fetchone()[0]
            props = connection.execute(
                "SELECT property,value FROM ImageTagProperties WHERE imageid=3 AND tagid=? ORDER BY property",
                (tag,),
            ).fetchall()
        self.assertEqual(props, [
            ("faceToTrain", '<rect x="600" y="160" width="200" height="240"/>'),
            ("tagRegion", '<rect x="600" y="160" width="200" height="240"/>'),
        ])

    def test_reassigns_only_the_approved_face_region(self):
        with DigikamWriter(self.database) as writer:
            result = writer.reassign_face(
                "2026/photo.jpg", "Angie Galea", "Gail Vassallo",
                (0.1, 0.1, 0.2, 0.2), image_id=3, old_tag_id=44,
            )
            repeated = writer.reassign_face(
                "2026/photo.jpg", "Angie Galea", "Gail Vassallo",
                (0.1, 0.1, 0.2, 0.2), image_id=3, old_tag_id=44,
            )
        self.assertTrue(result["changed"])
        self.assertFalse(repeated["changed"])
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM ImageTagProperties WHERE imageid=3 AND tagid=34").fetchone()[0],
                2,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM ImageTags WHERE imageid=3 AND tagid=44").fetchone()[0],
                0,
            )

    def test_sqlite_backup_includes_wal_content(self):
        backup = create_sqlite_backup(self.database, self.root / "backups" / "copy.db")
        with closing(sqlite3.connect(backup)) as connection:
            self.assertEqual(connection.execute("SELECT name FROM Images WHERE id=3").fetchone()[0], "photo.jpg")

    def test_close_request_uses_sigterm_and_waits_for_exit(self):
        with (
            patch(
                "digikam_nextcloud.digikam_writer.digikam_process_ids",
                side_effect=[[42], []],
            ),
            patch("digikam_nextcloud.digikam_writer.os.kill") as send_signal,
            patch("digikam_nextcloud.digikam_writer.time.sleep"),
        ):
            result = terminate_digikam()
        self.assertTrue(result["closed"])
        send_signal.assert_called_once()


class ApplyPlanTests(unittest.TestCase):
    def test_plan_includes_both_directions_and_conflict_decisions(self):
        result = {
            "summary": {"assigned": 1, "inserted": 1, "created_in_digikam": 1},
            "actions": [
                {"action": "assign", "path": "a.jpg", "person": "A", "rect": [0, 0, .1, .1]},
                {"action": "insert", "path": "b.jpg", "person": "B", "rect": [0, 0, .1, .1]},
                {"action": "create_digikam", "path": "c.jpg", "person": "C", "rect": [0, 0, .1, .1]},
            ],
        }
        conflicts = {
            "remaining": 0,
            "conflicts": [
                {"resolution": "memories", "path": "d.jpg", "digikam_person": "D", "nextcloud_person": "E", "digikam_rect": [0, 0, .1, .1], "nextcloud_rect": [0, 0, .1, .1]},
                {"resolution": "digikam", "path": "e.jpg", "digikam_person": "F", "nextcloud_person": "G", "digikam_rect": [0, 0, .1, .1], "nextcloud_rect": [0, 0, .1, .1]},
            ],
        }
        plan = build_apply_plan(result, conflicts)
        self.assertEqual(plan_summary(plan), {"total": 5, "memories": 3, "digikam": 2})
        self.assertEqual(plan[-2]["operation"], "reassign_digikam")
        self.assertEqual(plan[-1]["operation"], "assign_memories")

    def test_refuses_a_truncated_preview(self):
        with self.assertRaisesRegex(ValueError, "does not contain every"):
            build_apply_plan(
                {"summary": {"assigned": 2}, "actions": [{"action": "assign"}]},
                {"remaining": 0, "conflicts": []},
            )

    def test_remote_executor_renames_and_creates_memories_faces(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "digikam4.db"
            make_writable_digikam(database)
            existing = FaceRegion(
                person="Angie Galea", rect=Rect(0.1, 0.1, 0.2, 0.2),
                source="nextcloud", nc_file_id=7, nc_detection_id=8,
                nc_cluster_id=4, dav_parent="Angie Galea", file_name="photo.jpg",
            )
            backend = FakeApplyBackend([existing])
            with DigikamWriter(database) as writer:
                executor = ApplyExecutor(
                    backend=backend, digikam=writer, nextcloud_photos_path="Photos"
                )
                renamed = executor.execute({
                    "target": "memories", "operation": "assign_memories",
                    "path": "2026/photo.jpg", "person": "Gail Vassallo",
                    "old_person": "Angie Galea",
                    "rect": [0.1, 0.1, 0.2, 0.2], "nc_file_id": 7,
                    "nc_detection_id": 8,
                })
                created = executor.execute({
                    "target": "memories", "operation": "insert_memories",
                    "path": "2026/photo.jpg", "person": "April Vassallo",
                    "rect": [0.6, 0.2, 0.2, 0.2], "nc_file_id": 7,
                    "confirmed_face": True,
                })
                self.assertTrue(backend.last_insert["confirmed"])
                repeated = executor.execute({
                    "target": "memories", "operation": "insert_memories",
                    "path": "2026/photo.jpg", "person": "April Vassallo",
                    "rect": [0.6, 0.2, 0.2, 0.2], "nc_file_id": 7,
                })
            self.assertTrue(renamed["changed"])
            self.assertEqual(existing.person, "Gail Vassallo")
            self.assertTrue(created["changed"])
            self.assertEqual(created["nc_detection_id"], 99)
            self.assertFalse(repeated["changed"])


class ApplyServiceTests(unittest.TestCase):
    @staticmethod
    def failed_face_action() -> dict:
        return {
            "target": "memories",
            "operation": "insert_memories",
            "action": "insert",
            "path": "2026/photo.jpg",
            "person": "Gail Vassallo",
            "rect": [0.1, 0.2, 0.3, 0.4],
            "nc_file_id": 7,
        }

    def test_keep_source_suppresses_the_same_face_in_future_previews(self):
        with tempfile.TemporaryDirectory() as temp:
            state = StateStore(Path(temp) / "state.sqlite3")
            try:
                run_id = state.create_run("person")
                action = self.failed_face_action()
                state.initialize_apply(run_id, [action])
                pending = state.pending_apply_actions(run_id)[0]
                state.finish_apply_action(
                    pending["id"], "failed", error="No face found"
                )
                state.finish_apply(run_id, "apply_failed")

                review = state.resolve_failed_action(
                    run_id, pending["id"], "keep_source"
                )

                self.assertEqual(review["remaining"], 0)
                self.assertEqual(review["ignored"], 1)
                kept, ignored = state.filter_ignored_actions([action])
                self.assertEqual(kept, [])
                self.assertEqual(ignored, [action])
            finally:
                state.close()

    def test_adjusted_face_returns_to_the_pending_apply_queue(self):
        with tempfile.TemporaryDirectory() as temp:
            state = StateStore(Path(temp) / "state.sqlite3")
            try:
                run_id = state.create_run("person")
                state.initialize_apply(run_id, [self.failed_face_action()])
                pending = state.pending_apply_actions(run_id)[0]
                state.finish_apply_action(
                    pending["id"], "failed", error="No face found"
                )
                state.finish_apply(run_id, "apply_failed")

                review = state.resolve_failed_action(
                    run_id,
                    pending["id"],
                    "retry",
                    rect=[0.12, 0.18, 0.34, 0.46],
                )

                self.assertEqual(review["remaining"], 0)
                self.assertEqual(review["pending"], 1)
                with state.lock:
                    row = state.conn.execute(
                        "SELECT action_json FROM run_actions WHERE id = ?",
                        (pending["id"],),
                    ).fetchone()
                    old_action = json.loads(row["action_json"])
                    old_action.pop("confirmed_face")
                    state.conn.execute(
                        "UPDATE run_actions SET action_json = ? WHERE id = ?",
                        (json.dumps(old_action), pending["id"]),
                    )
                    state.conn.commit()
                state.initialize_apply(run_id, [self.failed_face_action()])
                queued = state.pending_apply_actions(run_id)[0]["action"]
                self.assertEqual(queued["rect"], [0.12, 0.18, 0.34, 0.46])
                self.assertEqual(queued["digikam_rect"], [0.1, 0.2, 0.3, 0.4])
                self.assertTrue(queued["confirmed_face"])
                state.finish_apply_action(
                    pending["id"], "failed", error="Still no face found"
                )
                state.finish_apply(run_id, "apply_failed")
                state.resolve_failed_action(run_id, pending["id"], "keep_source")
                kept, ignored = state.filter_ignored_actions(
                    [self.failed_face_action()]
                )
                self.assertEqual(kept, [])
                self.assertEqual(len(ignored), 1)
            finally:
                state.close()

    def test_resolving_the_last_failure_finishes_the_run(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = StateStore(root / "state.sqlite3")
            service = AppService(
                SettingsStore(root / "config", use_keyring=False), state
            )
            try:
                run_id = state.create_run("person")
                state.initialize_apply(run_id, [self.failed_face_action()])
                pending = state.pending_apply_actions(run_id)[0]
                state.finish_apply_action(
                    pending["id"], "failed", error="No face found"
                )
                state.finish_apply(run_id, "apply_failed")

                result = service.resolve_failure(
                    run_id,
                    pending["id"],
                    {"decision": "keep_source"},
                )

                self.assertEqual(result["status"], "applied")
                self.assertEqual(state.run(run_id)["status"], "applied")
                self.assertEqual(state.run(run_id)["apply"]["ignored"], 1)
            finally:
                state.close()

    def test_only_systemic_failures_trigger_the_apply_safety_stop(self):
        self.assertFalse(is_systemic_apply_failure(RuntimeError(
            "Recognize face-import failed (HTTP 422): No face found inside the supplied rectangle"
        )))
        self.assertFalse(is_systemic_apply_failure(RuntimeError(
            "This Memories face has not been clustered yet."
        )))
        self.assertTrue(is_systemic_apply_failure(
            NextcloudConnectionError("Nextcloud connection failed")
        ))
        self.assertTrue(is_systemic_apply_failure(RuntimeError(
            "HTTP POST failed after retries: connection reset"
        )))

    def test_interrupted_apply_is_recovered_and_remains_latest(self):
        with tempfile.TemporaryDirectory() as temp:
            state = StateStore(Path(temp) / "state.sqlite3")
            try:
                run_id = state.create_run("person")
                state.save_result(run_id, {"run_id": run_id, "summary": {}, "actions": []})
                state.initialize_apply(run_id, [])
                self.assertEqual(state.recover_interrupted_applies(), 1)
                latest = state.latest_actionable_run()
                self.assertEqual(latest["id"], run_id)
                self.assertEqual(latest["status"], "apply_failed")
                self.assertIn("stopped", latest["error"])
            finally:
                state.close()

    def test_running_preview_is_restored_instead_of_an_older_result(self):
        with tempfile.TemporaryDirectory() as temp:
            state = StateStore(Path(temp) / "state.sqlite3")
            try:
                older = state.create_run("person")
                state.save_result(
                    older,
                    {"run_id": older, "summary": {}, "actions": []},
                )
                running = state.create_run("person")

                latest = state.latest_actionable_run()

                self.assertEqual(latest["id"], running)
                self.assertEqual(latest["status"], "previewing")
                self.assertIsNone(latest["result"])
            finally:
                state.close()

    def test_background_apply_backs_up_writes_and_journals(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            library = root / "Photos"
            library.mkdir()
            database = library / "digikam4.db"
            make_writable_digikam(database)
            settings = SettingsStore(root / "config", use_keyring=False)
            settings.save(
                {
                    "digikam_library": str(library), "digikam_db": str(database),
                    "nextcloud_url": "https://cloud.test", "nc_user": "keith",
                    "nc_photos_path": "Photos",
                },
                "secret",
            )
            state = StateStore(root / "state.sqlite3")
            service = AppService(settings, state)
            try:
                run_id = state.create_run("person")
                result = {
                    "run_id": run_id,
                    "summary": {"assigned": 0, "inserted": 0, "created_in_digikam": 1},
                    "actions": [{
                        "action": "create_digikam", "path": "2026/photo.jpg",
                        "person": "April Vassallo", "rect": [0.6, 0.2, 0.2, 0.3],
                        "digikam_image_id": 3, "nc_file_id": 7, "nc_detection_id": 9,
                    }],
                }
                state.save_result(run_id, result)
                state.finish_run(run_id, "previewed", result["summary"])
                with patch("digikam_nextcloud.app_service.digikam_is_running", return_value=False):
                    service.start_apply(run_id, {"digikam_closed": True})
                service._jobs[run_id].join(timeout=3)
                finished = state.run(run_id)
                self.assertEqual(finished["status"], "applied")
                self.assertEqual(finished["apply"]["applied"], 1)
                self.assertTrue(Path(finished["apply"]["backup_path"]).is_file())
                with closing(sqlite3.connect(database)) as connection:
                    self.assertEqual(
                        connection.execute("SELECT COUNT(*) FROM Tags WHERE name='April Vassallo'").fetchone()[0], 1
                    )
                self.assertEqual(state.unread_notifications()[0]["event_type"], "apply.completed")
            finally:
                state.close()


if __name__ == "__main__":
    unittest.main()


class FakePsutil:
    """Just enough psutil for the process probe."""

    class NoSuchProcess(Exception):
        pass

    class AccessDenied(Exception):
        pass

    class _Found:
        def __init__(self, info):
            self.info = info

    def __init__(self, names_by_pid):
        self.names_by_pid = names_by_pid
        self.terminated = []

    def process_iter(self, _attrs=None):
        return [self._Found({"pid": pid, "name": name}) for pid, name in self.names_by_pid.items()]

    def Process(self, pid):  # noqa: N802 - mirrors the psutil name
        outer = self

        class Handle:
            def terminate(self):
                outer.terminated.append(pid)

        return Handle()


class ConfirmedByDefaultTests(unittest.TestCase):
    """Every box digiKam drew is offered to Recognize as the detection."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "digikam4.db"
        make_writable_digikam(self.database)

    def tearDown(self):
        self.temp.cleanup()

    def insert(self, backend, **extra):
        with DigikamWriter(self.database) as writer:
            executor = ApplyExecutor(
                backend=backend, digikam=writer, nextcloud_photos_path="Photos"
            )
            return executor.execute({
                "target": "memories", "operation": "insert_memories",
                "path": "2026/photo.jpg", "person": "April Vassallo",
                "rect": [0.6, 0.2, 0.2, 0.2], "nc_file_id": 7,
                **extra,
            })

    def test_an_ordinary_insert_now_offers_the_box_as_the_detection(self):
        backend = FakeApplyBackend()
        backend.supports_confirmed_insert = True
        self.insert(backend)
        self.assertTrue(backend.last_insert["confirmed"])

    def test_an_older_companion_app_still_gets_a_plain_insert(self):
        """Asking a server that cannot do it is an error, so an unreviewed
        face quietly settles for the detector rather than failing the run."""
        backend = FakeApplyBackend()
        backend.supports_confirmed_insert = False
        self.insert(backend)
        self.assertFalse(backend.last_insert["confirmed"])

    def test_a_reviewed_face_still_insists(self):
        """Falling back would put it through the detector that rejected it."""
        backend = FakeApplyBackend()
        backend.supports_confirmed_insert = False
        self.insert(backend, confirmed_face=True)
        self.assertTrue(backend.last_insert["confirmed"])

    def test_the_descriptor_confidence_is_kept_with_the_result(self):
        backend = FakeApplyBackend()
        backend.supports_confirmed_insert = True
        backend.last_insert_score = 0.0
        result = self.insert(backend)
        self.assertEqual(result["score"], 0.0)

    def test_a_backend_that_reports_no_score_records_none(self):
        backend = FakeApplyBackend()
        result = self.insert(backend)
        self.assertNotIn("score", result)


class DigikamProbeTests(unittest.TestCase):
    def test_psutil_finds_digikam_where_there_is_no_proc(self):
        fake = FakePsutil({11: "Finder", 22: "digiKam"})
        with (
            patch("digikam_nextcloud.digikam_writer.sys.platform", "darwin"),
            patch("digikam_nextcloud.digikam_writer.psutil", fake),
        ):
            self.assertTrue(digikam_probe_supported())
            self.assertEqual(digikam_process_ids(), [22])

    def test_windows_executable_name_matches(self):
        fake = FakePsutil({7: "explorer.exe", 8: "digikam.exe"})
        with (
            patch("digikam_nextcloud.digikam_writer.sys.platform", "win32"),
            patch("digikam_nextcloud.digikam_writer.psutil", fake),
        ):
            self.assertEqual(digikam_process_ids(), [8])

    def test_windows_close_goes_through_psutil(self):
        fake = FakePsutil({8: "digikam.exe"})
        with (
            patch("digikam_nextcloud.digikam_writer.sys.platform", "win32"),
            patch("digikam_nextcloud.digikam_writer.psutil", fake),
            patch(
                "digikam_nextcloud.digikam_writer.digikam_process_ids",
                side_effect=[[8], []],
            ),
            patch("digikam_nextcloud.digikam_writer.time.sleep"),
        ):
            result = terminate_digikam()
        self.assertTrue(result["closed"])
        self.assertEqual(fake.terminated, [8])

    def test_linux_still_reads_proc_without_psutil(self):
        with (
            patch("digikam_nextcloud.digikam_writer.psutil", None),
            patch(
                "digikam_nextcloud.digikam_writer._process_ids_from_proc",
                return_value=[5],
            ),
        ):
            self.assertTrue(digikam_probe_supported())
            self.assertEqual(digikam_process_ids(), [5])

    def test_probe_reports_itself_unavailable_rather_than_answering_no(self):
        with (
            patch("digikam_nextcloud.digikam_writer.sys.platform", "darwin"),
            patch("digikam_nextcloud.digikam_writer.psutil", None),
        ):
            self.assertFalse(digikam_probe_supported())
            self.assertEqual(digikam_process_ids(), [])
            self.assertEqual(
                terminate_digikam(),
                {"supported": False, "closed": False, "remaining": []},
            )
