"""digiKam photos are found in Nextcloud by their path inside the collection.

Issue #1: on Windows every photo was skipped. The app mapped the folder holding
digikam4.db, drive letter and all, onto paths that digiKam stores without one,
so no photo ever fell inside the map.
"""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from digimem.digikam import DigikamDB
from digimem.matching import digikam_path_candidates
from digimem.models import DigikamImage
from digimem.paths import COLLECTION
from digimem.sync import sync


def image(image_id: int, full_path: str, relative_path: str) -> DigikamImage:
    return DigikamImage(
        image_id=image_id,
        name=Path(relative_path).name,
        relative_path=relative_path,
        full_path=full_path,
        width=100,
        height=100,
        file_size=10,
        unique_hash="",
    )


class CollectionCandidateTests(unittest.TestCase):
    def test_a_windows_collection_is_found_under_the_photos_folder(self):
        # digiKam stores C:\Users\keith\Pictures as "/Users/keith/Pictures".
        photo = image(1, "Users/keith/Pictures/2026/photo.jpg", "2026/photo.jpg")
        self.assertEqual(
            digikam_path_candidates(photo, [(COLLECTION, "Photos")]),
            ["Photos/2026/photo.jpg"],
        )

    def test_a_network_share_collection_is_found_under_the_photos_folder(self):
        # A network share is stored as "/", so the full path is the relative one.
        photo = image(1, "2026/photo.jpg", "2026/photo.jpg")
        self.assertEqual(
            digikam_path_candidates(photo, [(COLLECTION, "Photos")]),
            ["Photos/2026/photo.jpg"],
        )

    def test_a_photo_at_the_top_of_the_collection(self):
        photo = image(1, "Users/keith/Pictures/photo.jpg", "photo.jpg")
        self.assertEqual(
            digikam_path_candidates(photo, [(COLLECTION, "Photos/")]),
            ["Photos/photo.jpg"],
        )

    def test_folder_maps_still_skip_photos_outside_them(self):
        photo = image(1, "mnt/archive/2026/photo.jpg", "2026/photo.jpg")
        self.assertEqual(
            digikam_path_candidates(photo, [("/home/keith/Photos", "Photos")]),
            [],
        )

    def test_a_folder_map_is_tried_before_the_collection(self):
        photo = image(1, "home/keith/Photos/Family/photo.jpg", "Family/photo.jpg")
        self.assertEqual(
            digikam_path_candidates(
                photo,
                [(COLLECTION, "Photos"), ("/home/keith/Photos/Family", "Shared/Family")],
            ),
            ["Shared/Family/photo.jpg", "Photos/Family/photo.jpg"],
        )


def make_two_collection_database(path: Path) -> None:
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
            INSERT INTO AlbumRoots VALUES (1, '/Users/keith/Pictures');
            INSERT INTO AlbumRoots VALUES (2, '/');
            INSERT INTO Albums VALUES (10, '/2026', 1);
            INSERT INTO Albums VALUES (11, '/', 1);
            INSERT INTO Albums VALUES (20, '/2026', 2);
            INSERT INTO Albums VALUES (21, '/', 2);
            -- Same path in both collections, once with different case.
            INSERT INTO Images VALUES (1, 'IMG_0001.jpg', 10, '', 10, 1);
            INSERT INTO Images VALUES (2, 'img_0001.JPG', 10, '', 20, 1);
            -- Same name at the top of both collections.
            INSERT INTO Images VALUES (3, 'cover.jpg', 10, '', 11, 1);
            INSERT INTO Images VALUES (4, 'cover.jpg', 10, '', 21, 1);
            -- Only in one collection.
            INSERT INTO Images VALUES (5, 'IMG_0002.jpg', 10, '', 10, 1);
            -- The other copy was removed from digiKam.
            INSERT INTO Images VALUES (6, 'IMG_0003.jpg', 10, '', 10, 1);
            INSERT INTO Images VALUES (7, 'IMG_0003.jpg', 10, '', 20, 3);
            """
        )
        connection.commit()


class SharedPathTests(unittest.TestCase):
    def test_paths_in_more_than_one_collection_are_found(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "digikam4.db"
            make_two_collection_database(path)
            with DigikamDB(path) as digikam:
                shared = digikam.shared_relative_paths()
        self.assertEqual(shared, {"2026/img_0001.jpg", "cover.jpg"})


class FakeDigikam:
    def __init__(self, images, shared):
        self.images = images
        self.shared = shared
        self.asked_for_shared = False

    def count_images_with_faces(self):
        return len(self.images)

    def count_face_regions(self):
        return 0

    def iter_image_batches(self, **kwargs):
        yield list(self.images)

    def shared_relative_paths(self):
        self.asked_for_shared = True
        return self.shared


class FakeBackend:
    supports_insert = True

    def __init__(self):
        self.resolved = []

    def list_face_clusters(self):
        return []

    def resolve_digikam_images(self, images, path_maps):
        self.resolved.extend(images)
        return [], list(images), {}

    def rollback(self):
        pass


class SharedPathSyncTests(unittest.TestCase):
    def setUp(self):
        self.images = [
            image(1, "Users/keith/Pictures/2026/IMG_0001.jpg", "2026/IMG_0001.jpg"),
            image(5, "Users/keith/Pictures/2026/IMG_0002.jpg", "2026/IMG_0002.jpg"),
        ]

    def test_a_photo_another_collection_shares_a_path_with_is_left_out(self):
        digikam = FakeDigikam(self.images, frozenset({"2026/img_0001.jpg"}))
        backend = FakeBackend()
        report = sync(digikam, backend, path_maps=[(COLLECTION, "Photos")])
        self.assertEqual([im.image_id for im in backend.resolved], [5])
        self.assertEqual(report.files_digikam, 2)
        self.assertEqual(report.files_unmatched_digikam, 2, "left out, and one not found")
        self.assertTrue(any("same path" in w for w in report.warnings))

    def test_folder_maps_do_not_look_for_shared_paths(self):
        digikam = FakeDigikam(self.images, frozenset({"2026/img_0001.jpg"}))
        backend = FakeBackend()
        sync(digikam, backend, path_maps=[("/Users/keith/Pictures", "Photos")])
        self.assertFalse(digikam.asked_for_shared)
        self.assertEqual([im.image_id for im in backend.resolved], [1, 5])


if __name__ == "__main__":
    unittest.main()
