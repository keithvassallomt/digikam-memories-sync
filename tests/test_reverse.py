import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from digikam_nextcloud.digikam import DigikamDB
from digikam_nextcloud.models import (
    DigikamImage,
    FaceRegion,
    NextcloudFile,
    NextcloudNamedFace,
    Rect,
    SyncReport,
)
from digikam_nextcloud.reverse import (
    compare_memories_to_digikam,
    selected_memories_faces,
)


def named_face(
    detection_id: int,
    path: str,
    person: str,
    rect: Rect,
) -> NextcloudNamedFace:
    file_id = 100 + detection_id
    nc_file = NextcloudFile(
        file_id=file_id,
        path=path,
        name=Path(path).name,
        size=0,
        webdav_path=path,
    )
    return NextcloudNamedFace(
        file=nc_file,
        face=FaceRegion(
            person=person,
            rect=rect,
            source="nextcloud",
            nc_file_id=file_id,
            nc_detection_id=detection_id,
            nc_cluster_id=5,
        ),
    )


class FakeDigikam:
    def __init__(self, images):
        self.images = {image.relative_path.lower(): image for image in images}

    def images_for_relative_paths(self, paths):
        return {
            path.lower(): self.images[path.lower()]
            for path in paths
            if path.lower() in self.images
        }


class ReversePreviewTests(unittest.TestCase):
    def test_reverse_preview_creates_missing_faces_and_reports_name_conflicts(self):
        image = DigikamImage(
            image_id=3,
            name="photo.jpg",
            relative_path="2026/photo.jpg",
            full_path="/library/2026/photo.jpg",
            width=1000,
            height=800,
            file_size=10,
            unique_hash="",
            faces=[
                FaceRegion(
                    person="Angie Galea",
                    rect=Rect(0.1, 0.1, 0.2, 0.2),
                    source="digikam",
                    digikam_image_id=3,
                    digikam_tag_id=8,
                )
            ],
        )
        remote = [
            named_face(1, "Photos/2026/photo.jpg", "Gail Vassallo", Rect(0.1, 0.1, 0.2, 0.2)),
            named_face(2, "Photos/2026/photo.jpg", "April Vassallo", Rect(0.6, 0.2, 0.2, 0.2)),
            named_face(3, "Photos/2026/missing.jpg", "Gail Vassallo", Rect(0.2, 0.2, 0.2, 0.2)),
            named_face(4, "Elsewhere/ignored.jpg", "Gail Vassallo", Rect(0.2, 0.2, 0.2, 0.2)),
        ]
        selected = selected_memories_faces(
            remote, nextcloud_photos_path="Photos", only_person=None
        )
        progress = []
        report = compare_memories_to_digikam(
            FakeDigikam([image]),
            selected,
            SyncReport(),
            batch_size=1,
            progress_callback=progress.append,
        )

        self.assertEqual(report.files_memories, 2)
        self.assertEqual(report.faces_memories, 3)
        self.assertEqual(report.files_unmatched_nextcloud, 1)
        self.assertEqual(report.created_in_digikam, 1)
        self.assertEqual(report.actions[0].action, "create_digikam")
        self.assertEqual(report.actions[0].person, "April Vassallo")
        self.assertEqual(len(report.conflicts), 1)
        self.assertEqual(report.conflicts[0].digikam_person, "Angie Galea")
        self.assertEqual(report.conflicts[0].nextcloud_person, "Gail Vassallo")
        self.assertEqual(progress[-1]["current"], 2)
        self.assertEqual(progress[-1]["total"], 2)

    def test_person_selection_starts_from_memories(self):
        remote = [
            named_face(1, "Photos/one.jpg", "Gail Vassallo", Rect(0.1, 0.1, 0.2, 0.2)),
            named_face(2, "Photos/two.jpg", "April Vassallo", Rect(0.1, 0.1, 0.2, 0.2)),
        ]

        selected = selected_memories_faces(
            remote,
            nextcloud_photos_path="Photos",
            only_person="Gail Vassallo",
        )

        self.assertEqual([path for path, _ in selected], ["one.jpg"])

    def test_overlapping_duplicate_in_memories_is_not_proposed_for_digikam(self):
        image = DigikamImage(
            image_id=3,
            name="photo.jpg",
            relative_path="2026/photo.jpg",
            full_path="/library/2026/photo.jpg",
            width=1000,
            height=800,
            file_size=10,
            unique_hash="",
            faces=[
                FaceRegion(
                    person="Gail Vassallo",
                    rect=Rect(0.1, 0.1, 0.2, 0.2),
                    source="digikam",
                    digikam_image_id=3,
                    digikam_tag_id=8,
                )
            ],
        )
        remote = [
            named_face(
                1,
                "Photos/2026/photo.jpg",
                "Gail Vassallo",
                Rect(0.1, 0.1, 0.2, 0.2),
            ),
            named_face(
                2,
                "Photos/2026/photo.jpg",
                "Gail Vassallo",
                Rect(0.12, 0.1, 0.2, 0.2),
            ),
        ]

        report = compare_memories_to_digikam(
            FakeDigikam([image]),
            selected_memories_faces(
                remote,
                nextcloud_photos_path="Photos",
                only_person=None,
            ),
            SyncReport(),
        )

        self.assertEqual(report.created_in_digikam, 0)
        self.assertEqual(report.skipped, 1)
        self.assertEqual(report.actions[0].action, "skip")
        self.assertIn("already represented", report.actions[0].detail)

    def test_digikam_path_lookup_returns_photos_without_existing_faces(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "digikam4.db"
            with closing(sqlite3.connect(path)) as connection:
                connection.executescript(
                    """
                    CREATE TABLE Images (
                        id INTEGER PRIMARY KEY, name TEXT, fileSize INTEGER,
                        uniqueHash TEXT, album INTEGER, status INTEGER
                    );
                    CREATE TABLE Albums (id INTEGER PRIMARY KEY, relativePath TEXT, albumRoot INTEGER);
                    CREATE TABLE AlbumRoots (id INTEGER PRIMARY KEY, specificPath TEXT);
                    CREATE TABLE ImageInformation (imageid INTEGER, width INTEGER, height INTEGER, orientation INTEGER);
                    CREATE TABLE Tags (id INTEGER PRIMARY KEY, name TEXT);
                    CREATE TABLE TagProperties (tagid INTEGER, property TEXT, value TEXT);
                    CREATE TABLE ImageTagProperties (imageid INTEGER, tagid INTEGER, property TEXT, value TEXT);
                    INSERT INTO AlbumRoots VALUES (1, '/library');
                    INSERT INTO Albums VALUES (2, '/2026', 1);
                    INSERT INTO Images VALUES (3, 'photo.jpg', 10, '', 2, 1);
                    INSERT INTO ImageInformation VALUES (3, 1000, 800, 1);
                    """
                )
                connection.commit()

            with DigikamDB(path) as digikam:
                found = digikam.images_for_relative_paths(["2026/photo.jpg"])

            self.assertEqual(found["2026/photo.jpg"].image_id, 3)
            self.assertEqual(found["2026/photo.jpg"].faces, [])


if __name__ == "__main__":
    unittest.main()
