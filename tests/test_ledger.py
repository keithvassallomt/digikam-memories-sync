"""Tests for change attribution: which library moved, and what follows."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from digimem import ledger as ledger_module
from digimem.constants import ASK
from digimem.apply import build_apply_plan, conflict_preview_actions
from digimem.ledger import NullLedger, StateLedger
from digimem.models import (
    DigikamImage,
    FaceRegion,
    FileMatch,
    NextcloudFile,
    Rect,
    SyncReport,
)
from digimem.state_store import StateStore
from digimem.sync import _process_match

BOX = Rect(0.1, 0.1, 0.2, 0.2)


def digikam_face(person, tag_id=34):
    return FaceRegion(
        person=person, rect=BOX, source="digikam",
        digikam_image_id=1, digikam_tag_id=tag_id,
    )


def memories_face(person, detection_id=20):
    return FaceRegion(
        person=person, rect=BOX, source="nextcloud",
        nc_file_id=10, nc_detection_id=detection_id, nc_cluster_id=30,
    )


def photo(faces):
    return DigikamImage(
        image_id=1, name="photo.jpg", relative_path="2026/photo.jpg",
        full_path="/photos/2026/photo.jpg", width=1000, height=800,
        file_size=1, unique_hash="", faces=faces,
    )


class FakeBackend:
    """Enough of a Nextcloud backend for a preview, which writes nothing."""

    supports_insert = True
    generates_face_vectors = True

    def get_or_create_cluster(self, title):
        return 1


def run_match(digikam_faces, remote_faces, ledger=None):
    report = SyncReport()
    _process_match(
        FileMatch(
            digikam=photo(digikam_faces),
            nextcloud=NextcloudFile(file_id=10, path="Photos/2026/photo.jpg",
                                    name="photo.jpg", size=1),
            method="path",
        ),
        remote_faces,
        FakeBackend(),
        report,
        apply=False,
        insert_missing=True,
        conflict_policy=ASK,
        iou_threshold=0.4,
        cluster_cache={},
        max_actions=50,
        max_conflicts=50,
        max_warnings=10,
        ledger=ledger,
    )
    return report


class RememberedName:
    """A ledger that remembers exactly one agreement."""

    def __init__(self, name):
        self.name = name
        self.recorded = []

    def agreed_name(self, file_id, detection_id):
        return self.name

    def record(self, entries):
        self.recorded.extend(entries)

    def flush(self):
        return None


class AttributionRuleTest(unittest.TestCase):
    """The five cases from the design, as a table."""

    def test_matching_names_are_an_agreement(self):
        self.assertEqual(
            ledger_module.attribute("Gail", "Gail", None), ledger_module.AGREED)

    def test_no_history_means_nobody_can_tell(self):
        self.assertEqual(
            ledger_module.attribute("Gail", "Angie", None), ledger_module.UNKNOWN)

    def test_memories_moved_when_digikam_still_matches(self):
        self.assertEqual(
            ledger_module.attribute("Gail", "Angie", "Gail"),
            ledger_module.MEMORIES_CHANGED)

    def test_digikam_moved_when_memories_still_matches(self):
        self.assertEqual(
            ledger_module.attribute("Angie", "Gail", "Gail"),
            ledger_module.DIGIKAM_CHANGED)

    def test_both_moving_is_a_question_for_a_person(self):
        self.assertEqual(
            ledger_module.attribute("Angie", "Eli", "Gail"),
            ledger_module.BOTH_CHANGED)

    def test_names_are_compared_the_way_the_rest_of_the_engine_does(self):
        self.assertEqual(
            ledger_module.attribute("gail vassallo", "Gail Vassallo", None),
            ledger_module.AGREED)


class EngineAttributionTest(unittest.TestCase):
    def test_without_history_a_disagreement_asks(self):
        report = run_match([digikam_face("Gail")], [memories_face("Angie")])
        self.assertEqual(report.conflict_count, 1)
        self.assertEqual(report.reassigned_in_digikam, 0)
        self.assertEqual(report.assigned, 0)

    def test_a_rename_in_memories_renames_digikam_without_asking(self):
        report = run_match(
            [digikam_face("Gail")], [memories_face("Angie")], RememberedName("Gail"))
        self.assertEqual(report.conflict_count, 0)
        self.assertEqual(report.reassigned_in_digikam, 1)
        action = report.actions[0]
        self.assertEqual(action.action, "reassign_digikam")
        self.assertEqual(action.person, "Angie")
        self.assertEqual(action.old_person, "Gail")
        self.assertEqual(action.digikam_tag_id, 34)

    def test_a_rename_in_digikam_renames_memories_without_asking(self):
        report = run_match(
            [digikam_face("Angie")], [memories_face("Gail")], RememberedName("Gail"))
        self.assertEqual(report.conflict_count, 0)
        self.assertEqual(report.assigned, 1)
        action = report.actions[0]
        self.assertEqual(action.action, "assign")
        self.assertEqual(action.person, "Angie")
        self.assertEqual(
            action.old_person, "Gail",
            "Apply re-checks the previous name before overwriting it")

    def test_a_rename_on_both_sides_still_asks(self):
        report = run_match(
            [digikam_face("Angie")], [memories_face("Eli")], RememberedName("Gail"))
        self.assertEqual(report.conflict_count, 1)
        self.assertEqual(report.reassigned_in_digikam, 0)

    def test_an_unnamed_memories_face_is_assigned_not_attributed(self):
        report = run_match(
            [digikam_face("Gail")], [memories_face("")], RememberedName("Gail"))
        self.assertEqual(report.conflict_count, 0)
        self.assertEqual(report.assigned, 1)
        self.assertEqual(report.actions[0].old_person, "")

    def test_an_agreement_is_remembered(self):
        remembered = RememberedName(None)
        report = run_match([digikam_face("Gail")], [memories_face("Gail")], remembered)
        self.assertEqual(report.skipped, 1)
        self.assertEqual(len(remembered.recorded), 1)
        entry = remembered.recorded[0]
        self.assertEqual(entry["synced_name"], "Gail")
        self.assertEqual(entry["nextcloud_detection_id"], 20)
        self.assertEqual(entry["nextcloud_file_id"], 10)

    def test_the_command_line_keeps_asking_about_every_disagreement(self):
        report = run_match([digikam_face("Gail")], [memories_face("Angie")], NullLedger())
        self.assertEqual(report.conflict_count, 1)


class LedgerStorageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = StateStore(Path(self.tmp.name) / "state.sqlite3")

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def entry(self, name, tag_id=34, detection_id=20):
        return {
            "nextcloud_file_id": 10, "nextcloud_detection_id": detection_id,
            "digikam_image_id": 1, "digikam_tag_id": tag_id,
            "synced_name": name,
            "digikam_rect": [0.1, 0.1, 0.2, 0.2],
            "nextcloud_rect": [0.1, 0.1, 0.2, 0.2],
        }

    def test_agreements_round_trip(self):
        self.state.record_agreements([self.entry("Gail")])
        self.assertEqual(self.state.load_ledger(), {(10, 20): "Gail"})

    def test_a_rename_replaces_the_row_rather_than_adding_one(self):
        self.state.record_agreements([self.entry("Gail", tag_id=34)])
        # The tag id changes with the person, which is why the detection is
        # the key and not the tag.
        self.state.record_agreements([self.entry("Angie", tag_id=77)])
        self.assertEqual(self.state.load_ledger(), {(10, 20): "Angie"})
        self.assertEqual(self.state.ledger_size(), 1)

    def test_rebuilding_forgets_everything(self):
        self.state.record_agreements([self.entry("Gail"), self.entry("Eli", detection_id=21)])
        self.assertEqual(self.state.clear_ledger(), 2)
        self.assertEqual(self.state.load_ledger(), {})

    def test_the_loaded_ledger_answers_by_detection(self):
        self.state.record_agreements([self.entry("Gail")])
        loaded = StateLedger(self.state).load()
        self.assertEqual(loaded.agreed_name(10, 20), "Gail")
        self.assertIsNone(loaded.agreed_name(10, 999))
        self.assertIsNone(loaded.agreed_name(None, None))

    def test_writes_are_batched_and_flushed(self):
        loaded = StateLedger(self.state).load()
        loaded.record([self.entry(f"Person {index}", detection_id=index) for index in range(5)])
        self.assertEqual(self.state.ledger_size(), 0, "not written until flushed")
        loaded.flush()
        self.assertEqual(self.state.ledger_size(), 5)


class AcrossRunsTest(unittest.TestCase):
    """The behaviour milestone three exists for, proved end to end."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = StateStore(Path(self.tmp.name) / "state.sqlite3")

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def preview(self, digikam_name, memories_name):
        """One run against a freshly loaded ledger, as the service does."""
        face_ledger = StateLedger(self.state).load()
        report = run_match(
            [digikam_face(digikam_name)], [memories_face(memories_name)], face_ledger)
        face_ledger.flush()
        return report

    def test_a_rename_in_memories_needs_no_decision_on_the_next_run(self):
        agreed = self.preview("Gail", "Gail")
        self.assertEqual(agreed.skipped, 1)
        self.assertEqual(self.state.ledger_size(), 1, "the agreement was remembered")

        renamed = self.preview("Gail", "Angie")
        self.assertEqual(renamed.conflict_count, 0, "nobody should be asked")
        self.assertEqual(renamed.reassigned_in_digikam, 1)
        self.assertEqual(renamed.actions[0].person, "Angie")

    def test_a_rename_in_digikam_needs_no_decision_on_the_next_run(self):
        self.preview("Gail", "Gail")
        renamed = self.preview("Angie", "Gail")
        self.assertEqual(renamed.conflict_count, 0)
        self.assertEqual(renamed.assigned, 1)
        self.assertEqual(renamed.actions[0].person, "Angie")

    def test_without_a_first_agreement_the_same_rename_asks(self):
        renamed = self.preview("Gail", "Angie")
        self.assertEqual(
            renamed.conflict_count, 1,
            "with no history there is nothing to attribute the change to")

    def test_forgetting_the_ledger_brings_the_question_back(self):
        self.preview("Gail", "Gail")
        self.state.clear_ledger()
        renamed = self.preview("Gail", "Angie")
        self.assertEqual(renamed.conflict_count, 1)

    def test_agreeing_again_updates_what_is_remembered(self):
        self.preview("Gail", "Gail")
        self.preview("Angie", "Angie")
        self.assertEqual(self.state.load_ledger(), {(10, 20): "Angie"})
        # And the new name is now the baseline for the next disagreement.
        renamed = self.preview("Angie", "Eli")
        self.assertEqual(renamed.reassigned_in_digikam, 1)


class ConflictIdentityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = StateStore(Path(self.tmp.name) / "state.sqlite3")

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def conflict(self, detection_id=20, digikam="Gail", memories="Angie"):
        return {
            "path": "2026/photo.jpg",
            "digikam_person": digikam, "nextcloud_person": memories,
            "digikam_rect": [0.1, 0.1, 0.2, 0.2],
            "nextcloud_rect": [0.1, 0.1, 0.2, 0.2],
            "iou": 0.9, "nc_file_id": 10, "nc_detection_id": detection_id,
            "digikam_image_id": 1, "digikam_tag_id": 34,
        }

    def previewed_run(self):
        run_id = self.state.create_run("all")
        self.state.finish_run(run_id, "previewed", {})
        return run_id

    def test_the_same_face_is_not_asked_about_twice(self):
        first = self.previewed_run()
        self.assertEqual(
            self.state.save_conflicts(first, [self.conflict()])["added"], 1)
        second = self.previewed_run()
        result = self.state.save_conflicts(second, [self.conflict()])
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["refreshed"], 1)
        self.assertEqual(self.state.attention_counts()["conflicts"], 1)

    def test_a_refreshed_conflict_shows_the_newest_names(self):
        run_id = self.previewed_run()
        self.state.save_conflicts(run_id, [self.conflict(memories="Angie")])
        later = self.previewed_run()
        self.state.save_conflicts(later, [self.conflict(memories="Eli")])
        open_now = self.state.open_conflicts()
        self.assertEqual(len(open_now), 1)
        self.assertEqual(open_now[0]["nextcloud_person"], "Eli")

    def test_a_full_run_closes_a_question_that_has_gone_away(self):
        run_id = self.previewed_run()
        self.state.save_conflicts(run_id, [self.conflict()])
        later = self.previewed_run()
        result = self.state.save_conflicts(later, [], close_unseen=True)
        self.assertEqual(result["closed"], 1)
        self.assertEqual(self.state.attention_counts()["conflicts"], 0)

    def test_a_single_person_run_never_closes_anyone_elses_question(self):
        run_id = self.previewed_run()
        self.state.save_conflicts(run_id, [self.conflict()])
        later = self.previewed_run()
        self.state.save_conflicts(later, [], close_unseen=False)
        self.assertEqual(self.state.attention_counts()["conflicts"], 1)

    def test_faces_with_no_detection_fall_back_to_their_rectangles(self):
        without = {**self.conflict(), "nc_file_id": None, "nc_detection_id": None}
        first = self.previewed_run()
        self.state.save_conflicts(first, [without])
        second = self.previewed_run()
        self.assertEqual(self.state.save_conflicts(second, [without])["added"], 0)


class UpgradeTest(unittest.TestCase):
    """What happens to a database written before any of this existed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def old_database(self):
        """A store with the phase one shape, then the new columns dropped."""
        import sqlite3

        state = StateStore(self.path)
        run_id = state.create_run("all")
        state.finish_run(run_id, "applied", {})
        state.conn.execute(
            """INSERT INTO conflicts(run_id, status, resolution, detail_json)
               VALUES (?, 'resolved', 'digikam', '{}')""",
            (run_id,),
        )
        state.conn.execute(
            """INSERT INTO face_links(
                   profile_id, digikam_image_id, digikam_tag_id,
                   nextcloud_file_id, nextcloud_detection_id,
                   digikam_name, nextcloud_name,
                   digikam_rect_json, nextcloud_rect_json)
               VALUES (1, 1, 34, 10, 20, 'Gail', 'Gail', '[]', '[]')"""
        )
        state.conn.execute(
            """INSERT INTO face_links(
                   profile_id, digikam_image_id, digikam_tag_id,
                   nextcloud_file_id, nextcloud_detection_id,
                   digikam_name, nextcloud_name,
                   digikam_rect_json, nextcloud_rect_json)
               VALUES (1, 2, 35, 11, 21, 'Eli', 'Elias', '[]', '[]')"""
        )
        state.conn.commit()
        state.close()

        # Drop the columns this milestone added, so reopening has to add them.
        connection = sqlite3.connect(self.path)
        connection.execute("ALTER TABLE conflicts DROP COLUMN decided_run_id")
        connection.execute("ALTER TABLE face_links DROP COLUMN synced_name")
        connection.commit()
        connection.close()
        return run_id

    def test_links_from_applied_changes_seed_the_ledger(self):
        self.old_database()
        state = StateStore(self.path)
        try:
            self.assertEqual(
                state.load_ledger(), {(10, 20): "Gail"},
                "only links whose two names already agree are an agreement")
        finally:
            state.close()

    def test_decisions_already_applied_are_not_gathered_again(self):
        self.old_database()
        state = StateStore(self.path)
        try:
            self.assertEqual(
                state.pending_decisions(), [],
                "a decision from before this column existed was carried by its run")
        finally:
            state.close()

    def test_the_backfill_runs_only_once(self):
        self.old_database()
        first = StateStore(self.path)
        first.record_agreements([{
            "nextcloud_file_id": 10, "nextcloud_detection_id": 20,
            "digikam_image_id": 1, "digikam_tag_id": 34,
            "synced_name": "Angie", "digikam_rect": [], "nextcloud_rect": [],
        }])
        first.close()

        second = StateStore(self.path)
        try:
            self.assertEqual(
                second.load_ledger(), {(10, 20): "Angie"},
                "reopening must not overwrite what has been learned since")
        finally:
            second.close()


class WhoCarriesADecisionTest(unittest.TestCase):
    """A sync that can still apply its own decisions must keep them."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = StateStore(Path(self.tmp.name) / "state.sqlite3")
        self.run_id = self.state.create_run("all")
        self.state.finish_run(self.run_id, "previewed", {})
        self.state.save_conflicts(self.run_id, [{
            "path": "a.jpg", "digikam_person": "Martina Muscat",
            "nextcloud_person": "Eli Vassallo",
            "digikam_rect": [0, 0, 1, 1], "nextcloud_rect": [0, 0, 1, 1],
            "iou": 0.62, "nc_file_id": 1, "nc_detection_id": 2,
            "digikam_image_id": 3, "digikam_tag_id": 4,
        }])
        self.conflict_id = self.state.open_conflicts()[0]["id"]

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def resolve(self):
        self.state.resolve_conflict(self.run_id, self.conflict_id, "digikam")

    def test_a_waiting_preview_keeps_its_own_decisions(self):
        self.resolve()
        self.assertEqual(
            self.state.pending_decisions(), [],
            "nothing needs a follow-up run; this preview will carry it")
        self.assertEqual(self.state.runs_carrying_decisions(), [self.run_id])

    def test_the_decision_is_in_that_preview_plan(self):
        self.resolve()
        self.assertEqual(self.state.plan_conflicts(self.run_id)["total"], 1)

    def test_a_decision_made_after_the_plan_was_written_needs_its_own_run(self):
        self.state.initialize_apply(self.run_id, [{
            "target": "memories", "operation": "assign_memories",
            "path": "b.jpg", "person": "Gail", "rect": [0, 0, 1, 1],
        }])
        self.resolve()
        self.assertEqual(
            len(self.state.pending_decisions()), 1,
            "the plan is frozen, so this needs carrying separately")
        self.assertEqual(self.state.runs_carrying_decisions(), [])

    def test_a_decision_on_a_finished_run_needs_its_own_run(self):
        self.state.finish_run(self.run_id, "apply_failed", {})
        self.resolve()
        self.assertEqual(len(self.state.pending_decisions()), 1)
        self.assertEqual(self.state.runs_carrying_decisions(), [])


class DiscardingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = StateStore(Path(self.tmp.name) / "state.sqlite3")

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def previewed(self, mode="all"):
        run_id = self.state.create_run(mode)
        self.state.finish_run(run_id, "previewed", {})
        self.state.append_preview_actions(run_id, [{"action": "assign"}])
        return run_id

    def test_discarding_a_full_preview_retires_the_older_ones(self):
        older = self.previewed()
        newer = self.previewed()
        self.state.discard_preview(newer)
        self.assertEqual(self.state.run(older)["status"], "discarded")
        self.assertEqual(self.state.run(newer)["status"], "discarded")

    def test_discarding_a_decisions_run_leaves_the_real_sync_alone(self):
        big = self.previewed("all")
        decisions = self.previewed("decisions")
        self.state.discard_preview(decisions)
        self.assertEqual(self.state.run(decisions)["status"], "discarded")
        self.assertEqual(
            self.state.run(big)["status"], "previewed",
            "thousands of proposed changes must not vanish with a small follow-up")
        self.assertEqual(len(self.state.preview_actions(big)), 1)

    def test_a_full_preview_does_not_retire_a_decisions_run(self):
        decisions = self.previewed("decisions")
        big = self.previewed("all")
        self.state.discard_preview(big)
        self.assertEqual(self.state.run(decisions)["status"], "previewed")

    def test_decisions_a_discarded_run_carried_are_asked_again(self):
        run_id = self.previewed("all")
        self.state.save_conflicts(run_id, [{
            "path": "a.jpg", "digikam_person": "A", "nextcloud_person": "B",
            "digikam_rect": [0, 0, 1, 1], "nextcloud_rect": [0, 0, 1, 1],
            "iou": 0.6, "nc_file_id": 1, "nc_detection_id": 2,
        }])
        conflict_id = self.state.open_conflicts()[0]["id"]
        self.state.resolve_conflict(run_id, conflict_id, "digikam")
        carrier = self.state.create_run("decisions")
        self.state.mark_decisions_folded([conflict_id], carrier)
        self.state.finish_run(carrier, "previewed", {})

        self.state.discard_preview(carrier)
        with self.state.lock:
            decided = self.state.conn.execute(
                "SELECT decided_run_id FROM conflicts WHERE id = ?", (conflict_id,)
            ).fetchone()[0]
        self.assertIsNone(decided, "the decision is loose again, not lost")


class DecisionsRunTest(unittest.TestCase):
    def test_settled_decisions_become_their_own_preview(self):
        decisions = [
            {
                "path": "a.jpg", "resolution": "digikam",
                "digikam_person": "Gail", "nextcloud_person": "Angie",
                "digikam_rect": [0, 0, 1, 1], "nextcloud_rect": [0, 0, 1, 1],
                "nc_file_id": 1, "nc_detection_id": 2,
                "digikam_image_id": 3, "digikam_tag_id": 4,
            },
            {
                "path": "b.jpg", "resolution": "memories",
                "digikam_person": "Eli", "nextcloud_person": "Joe",
                "digikam_rect": [0, 0, 1, 1], "nextcloud_rect": [0, 0, 1, 1],
                "nc_file_id": 5, "nc_detection_id": 6,
                "digikam_image_id": 7, "digikam_tag_id": 8,
            },
        ]
        result = conflict_preview_actions(decisions)
        self.assertEqual(result["summary"]["assigned"], 1)
        self.assertEqual(result["summary"]["reassigned_in_digikam"], 1)

        # The frozen preview must survive the same check Apply makes.
        plan = build_apply_plan(result, {"conflicts": [], "remaining": 0})
        self.assertEqual(
            [(entry["target"], entry["operation"]) for entry in plan],
            [("memories", "assign_memories"), ("digikam", "reassign_digikam")])
        self.assertEqual(plan[0]["old_person"], "Angie")
        self.assertEqual(plan[1]["old_person"], "Eli")

    def test_an_undecided_conflict_no_longer_blocks_the_rest(self):
        result = {
            "summary": {"assigned": 1, "inserted": 0, "created_in_digikam": 0,
                        "reassigned_in_digikam": 0},
            "actions": [{
                "action": "assign", "path": "a.jpg", "person": "Gail",
                "rect": [0, 0, 1, 1],
            }],
        }
        plan = build_apply_plan(
            result, {"remaining": 1, "conflicts": [{"resolution": None}]})
        self.assertEqual(len(plan), 1, "the unambiguous change still applies")


if __name__ == "__main__":
    unittest.main()
