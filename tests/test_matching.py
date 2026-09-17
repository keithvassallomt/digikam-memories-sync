import unittest

from digimem.constants import ASK
from digimem.models import (
    DigikamImage,
    FaceRegion,
    FileMatch,
    NextcloudFile,
    Rect,
    SyncReport,
)
from digimem.sync import _process_match


class DuplicateFacePreviewTests(unittest.TestCase):
    def test_overlapping_duplicate_in_digikam_is_not_proposed_for_memories(self):
        exact = FaceRegion(
            person="Gail Vassallo",
            rect=Rect(0.1, 0.1, 0.2, 0.2),
            source="digikam",
            digikam_image_id=1,
            digikam_tag_id=34,
        )
        duplicate = FaceRegion(
            person="Gail Vassallo",
            rect=Rect(0.12, 0.1, 0.2, 0.2),
            source="digikam",
            digikam_image_id=1,
            digikam_tag_id=34,
        )
        memories_face = FaceRegion(
            person="Gail Vassallo",
            rect=Rect(0.1, 0.1, 0.2, 0.2),
            source="nextcloud",
            nc_file_id=10,
            nc_detection_id=20,
            nc_cluster_id=30,
        )
        image = DigikamImage(
            image_id=1,
            name="photo.jpg",
            relative_path="2026/photo.jpg",
            full_path="/photos/2026/photo.jpg",
            width=1000,
            height=800,
            file_size=1,
            unique_hash="",
            faces=[exact, duplicate],
        )
        nc_file = NextcloudFile(
            file_id=10,
            path="Photos/2026/photo.jpg",
            name="photo.jpg",
            size=1,
        )
        report = SyncReport()

        _process_match(
            FileMatch(digikam=image, nextcloud=nc_file, method="path"),
            [memories_face],
            object(),
            report,
            apply=False,
            insert_missing=True,
            conflict_policy=ASK,
            iou_threshold=0.4,
            cluster_cache={},
            max_actions=10,
            max_conflicts=10,
            max_warnings=10,
        )

        self.assertEqual(report.inserted, 0)
        self.assertEqual(report.skipped, 2)
        self.assertEqual([action.action for action in report.actions], ["skip", "skip"])
        self.assertIn("already represented", report.actions[1].detail)


if __name__ == "__main__":
    unittest.main()
