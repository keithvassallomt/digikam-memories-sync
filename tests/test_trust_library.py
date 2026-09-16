"""Trusting one library, and leaving new face boxes out of a sync."""
from __future__ import annotations

import unittest

from digikam_nextcloud.constants import ASK, TRUST_DIGIKAM, TRUST_MEMORIES
from digikam_nextcloud.models import (
    DigikamImage,
    FaceRegion,
    FileMatch,
    NextcloudFile,
    Rect,
    SyncReport,
)
from digikam_nextcloud.sync import _process_match


def disagreeing_pair():
    """One face both libraries know, under two different names."""
    digikam_face = FaceRegion(
        person="Gail Vassallo",
        rect=Rect(0.1, 0.1, 0.2, 0.2),
        source="digikam",
        digikam_image_id=1,
        digikam_tag_id=34,
    )
    memories_face = FaceRegion(
        person="Abigail Vassallo",
        rect=Rect(0.1, 0.1, 0.2, 0.2),
        source="nextcloud",
        nc_file_id=10,
        nc_detection_id=20,
        nc_cluster_id=30,
    )
    image = DigikamImage(
        image_id=1, name="photo.jpg", relative_path="2026/photo.jpg",
        full_path="/photos/2026/photo.jpg", width=1000, height=800,
        file_size=1, unique_hash="", faces=[digikam_face],
    )
    nc_file = NextcloudFile(file_id=10, path="Photos/2026/photo.jpg", name="photo.jpg", size=1)
    return FileMatch(digikam=image, nextcloud=nc_file, method="path"), [memories_face]


class Backend:
    """Enough of a Nextcloud backend for a read-only preview."""

    supports_insert = True
    generates_face_vectors = True


def run(policy, *, insert_missing=True):
    match, memories_faces = disagreeing_pair()
    report = SyncReport()
    _process_match(
        match, memories_faces, Backend(), report,
        apply=False,
        insert_missing=insert_missing,
        conflict_policy=policy,
        iou_threshold=0.4,
        cluster_cache={},
        max_actions=10,
        max_conflicts=10,
        max_warnings=10,
    )
    return report


class TrustOneLibraryTest(unittest.TestCase):
    """No ledger history, so nothing can say which side moved."""

    def test_asking_is_still_what_happens_by_default(self):
        report = run(ASK)
        self.assertEqual(report.conflict_count, 1)
        self.assertEqual([action.action for action in report.actions], ["conflict"])
        self.assertEqual(report.assigned, 0)
        self.assertEqual(report.reassigned_in_digikam, 0)

    def test_trusting_digikam_renames_the_memories_face(self):
        report = run(TRUST_DIGIKAM)
        self.assertEqual(report.assigned, 1)
        action = report.actions[0]
        self.assertEqual(action.action, "assign")
        self.assertEqual(action.person, "Gail Vassallo")
        self.assertEqual(action.old_person, "Abigail Vassallo")

    def test_trusting_memories_renames_the_digikam_face(self):
        report = run(TRUST_MEMORIES)
        self.assertEqual(report.reassigned_in_digikam, 1)
        action = report.actions[0]
        self.assertEqual(action.action, "reassign_digikam")
        self.assertEqual(action.person, "Abigail Vassallo")
        self.assertEqual(action.old_person, "Gail Vassallo")

    def test_a_trusted_library_asks_nothing(self):
        """The point of trusting one. Nothing reaches Needs attention."""
        for policy in (TRUST_DIGIKAM, TRUST_MEMORIES):
            with self.subTest(policy=policy):
                report = run(policy)
                self.assertEqual(report.conflict_count, 0)
                self.assertEqual(report.conflicts, [])

    def test_the_summary_still_adds_up(self):
        """build_apply_plan refuses a preview whose counters and actions
        disagree, so each policy has to count what it recorded."""
        from digikam_nextcloud.apply import MUTATION_ACTIONS
        for policy in (ASK, TRUST_DIGIKAM, TRUST_MEMORIES):
            with self.subTest(policy=policy):
                report = run(policy)
                summary = report.to_dict()["summary"]
                expected = sum(
                    summary[name] for name in
                    ("assigned", "inserted", "created_in_digikam", "reassigned_in_digikam")
                )
                actual = sum(a.action in MUTATION_ACTIONS for a in report.actions)
                self.assertEqual(actual, expected)


class NamesOnlyTest(unittest.TestCase):
    def test_leaving_boxes_out_names_what_is_already_there(self):
        """A face digiKam knows and Memories has never detected."""
        match, _ = disagreeing_pair()
        report = SyncReport()
        _process_match(
            match, [], Backend(), report,
            apply=False,
            insert_missing=False,
            conflict_policy=ASK,
            iou_threshold=0.4,
            cluster_cache={},
            max_actions=10,
            max_conflicts=10,
            max_warnings=10,
        )
        self.assertEqual(report.inserted, 0)
        self.assertEqual(report.skipped, 1)
        self.assertEqual(report.actions, [])


if __name__ == "__main__":
    unittest.main()
