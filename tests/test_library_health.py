"""Finding face boxes that are wrong, rather than merely out of step."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from digimem import health
from digimem.app_service import AppService
from digimem.settings import SettingsStore
from digimem.state_store import StateStore
from tests.test_apply import make_writable_digikam

# The fixture photo is 1000x800 and already carries Angie Galea at
# (100, 80, 200, 160) in pixels.
ANGIE = '<rect x="100" y="80" width="200" height="160"/>'


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "digikam4.db"
        make_writable_digikam(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, tag_id: int, region: str) -> None:
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO ImageTags VALUES (3, ?)", (tag_id,))
            connection.execute(
                "INSERT INTO ImageTagProperties VALUES (3, ?, 'tagRegion', ?)",
                (tag_id, region))
            connection.commit()

    def test_a_tidy_library_reports_nothing(self):
        self.assertEqual(health.scan(self.path), [])

    def test_one_person_tagged_twice_is_reported(self):
        self.add(44, '<rect x="600" y="80" width="200" height="160"/>')
        issues = health.scan(self.path)
        self.assertEqual([issue.kind for issue in issues], [health.PERSON_TWICE])
        self.assertEqual(issues[0].person, "Angie Galea")
        self.assertEqual(len(issues[0].boxes), 2, "both boxes are offered")

    def test_two_people_side_by_side_are_fine(self):
        self.add(34, '<rect x="600" y="80" width="200" height="160"/>')
        self.assertEqual(health.scan(self.path), [])

    def test_a_box_inside_another_persons_is_reported(self):
        """The shape that started this: one box swallowed by another."""
        self.add(34, '<rect x="50" y="40" width="400" height="320"/>')
        issues = health.scan(self.path)
        self.assertEqual([issue.kind for issue in issues], [health.NESTED_BOX])
        self.assertEqual(issues[0].person, "Angie Galea")
        self.assertEqual(issues[0].other_person, "Gail Vassallo")

    def test_boxes_that_merely_overlap_are_left_alone(self):
        self.add(34, '<rect x="250" y="80" width="200" height="160"/>')
        self.assertEqual(health.scan(self.path), [])

    def test_the_same_pair_is_only_reported_once(self):
        self.add(34, '<rect x="50" y="40" width="400" height="320"/>')
        self.add(34, '<rect x="40" y="30" width="420" height="340"/>')
        kinds = sorted(issue.kind for issue in health.scan(self.path))
        self.assertEqual(kinds, [health.NESTED_BOX, health.PERSON_TWICE])

    def test_the_identity_survives_a_box_being_nudged(self):
        """So that dismissing it stays dismissed."""
        self.add(44, '<rect x="600" y="80" width="200" height="160"/>')
        first = health.scan(self.path)[0].identity
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                "UPDATE ImageTagProperties SET value = ? WHERE value = ?",
                ('<rect x="610" y="90" width="200" height="160"/>',
                 '<rect x="600" y="80" width="200" height="160"/>'))
            connection.commit()
        self.assertEqual(health.scan(self.path)[0].identity, first)


class LibraryIssueServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.database = root / "digikam4.db"
        make_writable_digikam(self.database)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "INSERT INTO ImageTagProperties VALUES (3, 44, 'tagRegion', ?)",
                ('<rect x="600" y="80" width="200" height="160"/>',))
            connection.commit()
        self.settings = SettingsStore(root / "config", use_keyring=False)
        self.settings.save({
            "digikam_library": str(root), "digikam_db": str(self.database),
            "nextcloud_url": "https://cloud.test", "nc_user": "keith",
            "nc_photos_path": "Photos",
        }, "secret")
        self.state = StateStore(root / "state.sqlite3")
        self.service = AppService(self.settings, self.state)

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def only_issue(self):
        return self.service.library_issues()["issues"][0]

    def resolve(self, issue_id, **payload):
        with patch("digimem.app_service.digikam_is_running", return_value=False):
            return self.service.resolve_library_issue(issue_id, payload)

    def test_scanning_twice_adds_nothing_the_second_time(self):
        self.assertEqual(self.service.check_library()["added"], 1)
        self.assertEqual(self.service.check_library()["added"], 0)

    def test_a_dismissed_issue_never_comes_back(self):
        self.service.check_library()
        self.resolve(self.only_issue()["id"], decision="dismiss")
        self.service.check_library()
        self.assertEqual(self.service.library_issues()["issues"], [])

    def test_removing_a_box_takes_it_out_of_digikam(self):
        self.service.check_library()
        issue = self.only_issue()
        target = issue["boxes"][1]
        result = self.resolve(
            issue["id"], decision="remove", person=target["person"], rect=target["rect"])
        self.assertTrue(result["removed"])
        self.assertEqual(result["issues"], [], "and the issue goes with it")
        with closing(sqlite3.connect(self.database)) as connection:
            left = connection.execute(
                "SELECT COUNT(*) FROM ImageTagProperties WHERE property='tagRegion'"
            ).fetchone()[0]
        self.assertEqual(left, 1, "the other box is untouched")

    def test_a_box_that_is_not_in_question_is_refused(self):
        """So a stale page cannot delete something nobody was shown."""
        self.service.check_library()
        with self.assertRaisesRegex(ValueError, "not one of"):
            self.resolve(
                self.only_issue()["id"], decision="remove",
                person="Angie Galea", rect=[0.9, 0.9, 0.05, 0.05])

    def test_an_open_digikam_refuses_rather_than_writing_underneath_it(self):
        self.service.check_library()
        issue = self.only_issue()
        target = issue["boxes"][1]
        with patch("digimem.app_service.digikam_is_running", return_value=True):
            with self.assertRaisesRegex(ValueError, "Close digiKam"):
                self.service.resolve_library_issue(issue["id"], {
                    "decision": "remove", "person": target["person"],
                    "rect": target["rect"]})

    def test_removing_a_box_is_backed_up_first(self):
        self.service.check_library()
        issue = self.only_issue()
        target = issue["boxes"][1]
        self.resolve(
            issue["id"], decision="remove", person=target["person"], rect=target["rect"])
        backups = list((self.settings.root / "backups").glob("digikam4-library-*.db"))
        self.assertEqual(len(backups), 1)

    def test_issues_reach_the_attention_inbox(self):
        self.service.check_library()
        inbox = self.service.attention()
        self.assertEqual(inbox["issue_count"], 1)
        self.assertEqual(inbox["total"], 1)
        self.assertEqual(self.state.attention_counts()["issues"], 1)

    def test_an_automatic_sync_says_so_once(self):
        self.service.check_library(notify_on_new=True)
        kinds = [n["event_type"] for n in self.state.undelivered_notifications()]
        self.assertIn("library.issues", kinds)
        self.service.check_library(notify_on_new=True)
        again = [n["event_type"] for n in self.state.undelivered_notifications()]
        self.assertEqual(again.count("library.issues"), 1, "nothing new, nothing said")

    def test_a_manual_sync_says_nothing(self):
        self.service.check_library(notify_on_new=False)
        kinds = [n["event_type"] for n in self.state.undelivered_notifications()]
        self.assertNotIn("library.issues", kinds)


if __name__ == "__main__":
    unittest.main()
