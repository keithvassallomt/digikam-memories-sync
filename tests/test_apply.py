import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from digikam_nextcloud.apply import ApplyExecutor, build_apply_plan, plan_summary
from digikam_nextcloud.app_service import AppService
from digikam_nextcloud.digikam_writer import (
    DigikamWriter,
    create_sqlite_backup,
    terminate_digikam,
)
from digikam_nextcloud.models import FaceRegion, NextcloudFile, NextcloudRequirements, Rect
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
    def __init__(self, faces=None):
        self.file = NextcloudFile(
            file_id=7, path="Photos/2026/photo.jpg", name="photo.jpg", size=10,
            webdav_path="Photos/2026/photo.jpg",
        )
        self.faces = list(faces or [])
        self.closed = False

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
                })
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
