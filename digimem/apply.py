"""Build and execute the exact change plan saved by a UI preview."""
from __future__ import annotations

from typing import Any

from .digikam_writer import DigikamChangedError, DigikamWriter
from .models import FaceRegion, NextcloudFile, Rect
from .names import person_names_match, sanitize_person_name
from .paths import normalize_path


MUTATION_ACTIONS = {"assign", "insert", "create_digikam", "reassign_digikam"}


def build_apply_plan(result: dict[str, Any], conflicts: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn a frozen preview and its decisions into explicit target operations.

    Conflicts nobody has settled yet are left out rather than refused, so one
    undecided face cannot hold back every other change in the run.
    """
    summary = result.get("summary") or {}
    source_actions = result.get("actions") or []
    expected = sum(
        int(summary.get(name, 0))
        for name in ("assigned", "inserted", "created_in_digikam", "reassigned_in_digikam")
    )
    actual = sum(action.get("action") in MUTATION_ACTIONS for action in source_actions)
    if actual != expected:
        raise ValueError(
            "This preview does not contain every proposed change. Run a new preview before applying."
        )
    plan: list[dict[str, Any]] = []
    for source in source_actions:
        kind = source.get("action")
        if kind == "assign":
            target, operation = "memories", "assign_memories"
        elif kind == "insert":
            target, operation = "memories", "insert_memories"
        elif kind == "create_digikam":
            target, operation = "digikam", "create_digikam"
        elif kind == "reassign_digikam":
            target, operation = "digikam", "reassign_digikam"
        else:
            continue
        plan.append({"target": target, "operation": operation, **source})

    plan.extend(conflict_plan_entries(conflicts.get("conflicts", [])))
    return plan


def conflict_plan_entries(conflicts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn settled decisions into target operations.

    An undecided conflict is left out rather than refused, so one open
    question cannot hold back every other change.
    """
    entries: list[dict[str, Any]] = []
    for conflict in conflicts:
        resolution = conflict.get("resolution")
        if resolution not in {"digikam", "memories"}:
            # Not settled yet. Leave it out and read nothing else from it.
            continue
        shared = {
            "source": "conflict",
            "path": conflict["path"],
            "nc_file_id": conflict.get("nc_file_id"),
            "nc_detection_id": conflict.get("nc_detection_id"),
            "digikam_image_id": conflict.get("digikam_image_id"),
            "digikam_tag_id": conflict.get("digikam_tag_id"),
        }
        if resolution == "digikam":
            entries.append({
                **shared,
                "target": "memories",
                "operation": "assign_memories",
                "person": conflict["digikam_person"],
                "old_person": conflict["nextcloud_person"],
                "rect": conflict["nextcloud_rect"],
                "digikam_rect": conflict["digikam_rect"],
            })
        else:
            entries.append({
                **shared,
                "target": "digikam",
                "operation": "reassign_digikam",
                "person": conflict["nextcloud_person"],
                "old_person": conflict["digikam_person"],
                "rect": conflict["digikam_rect"],
                "nextcloud_rect": conflict["nextcloud_rect"],
            })
    return entries


def conflict_preview_actions(conflicts: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a frozen preview containing only settled decisions.

    A decision made after its own run finished cannot be added to that run's
    journal, so it becomes a small follow-up run of its own.
    """
    actions: list[dict[str, Any]] = []
    assigned = reassigned = 0
    for conflict in conflicts:
        if conflict.get("resolution") == "digikam":
            assigned += 1
            actions.append({
                "action": "assign",
                "path": conflict["path"],
                "person": conflict["digikam_person"],
                "old_person": conflict["nextcloud_person"],
                "rect": conflict["nextcloud_rect"],
                "digikam_rect": conflict["digikam_rect"],
                "detail": "your decision: keep the digiKam name",
                "nc_file_id": conflict.get("nc_file_id"),
                "nc_detection_id": conflict.get("nc_detection_id"),
                "digikam_image_id": conflict.get("digikam_image_id"),
                "digikam_tag_id": conflict.get("digikam_tag_id"),
            })
        elif conflict.get("resolution") == "memories":
            reassigned += 1
            actions.append({
                "action": "reassign_digikam",
                "path": conflict["path"],
                "person": conflict["nextcloud_person"],
                "old_person": conflict["digikam_person"],
                "rect": conflict["digikam_rect"],
                "nextcloud_rect": conflict["nextcloud_rect"],
                "detail": "your decision: keep the Memories name",
                "nc_file_id": conflict.get("nc_file_id"),
                "nc_detection_id": conflict.get("nc_detection_id"),
                "digikam_image_id": conflict.get("digikam_image_id"),
                "digikam_tag_id": conflict.get("digikam_tag_id"),
            })
    return {
        "summary": {
            "assigned": assigned,
            "inserted": 0,
            "created_in_digikam": 0,
            "reassigned_in_digikam": reassigned,
            "skipped": 0,
            "conflicts": 0,
        },
        "actions": actions,
        "conflicts": [],
        "warnings": [],
    }


def plan_summary(plan: list[dict[str, Any]]) -> dict[str, int]:
    memories = sum(action["target"] == "memories" for action in plan)
    digikam = sum(action["target"] == "digikam" for action in plan)
    return {"total": len(plan), "memories": memories, "digikam": digikam}


class ApplyExecutor:
    """Executes idempotent plan entries and checks their current targets first."""

    def __init__(
        self,
        *,
        backend: Any | None,
        digikam: DigikamWriter,
        nextcloud_photos_path: str,
    ):
        self.backend = backend
        self.digikam = digikam
        self.photos_path = normalize_path(nextcloud_photos_path).strip("/")
        self.remote_cache: dict[str, tuple[NextcloudFile, list[FaceRegion]]] = {}

    def _remote(self, action: dict[str, Any]) -> tuple[NextcloudFile, list[FaceRegion]]:
        relative = normalize_path(str(action["path"])).strip("/")
        webdav_path = normalize_path(f"{self.photos_path}/{relative}") if self.photos_path else relative
        if webdav_path not in self.remote_cache:
            if self.backend is None:
                raise RuntimeError("The Nextcloud connection is unavailable.")
            nc_file, faces = self.backend.resolve_file_with_faces(webdav_path)
            if nc_file is None:
                raise RuntimeError(f"Photo is no longer available in Nextcloud: {relative}")
            expected_file = action.get("nc_file_id")
            if expected_file is not None and int(expected_file) != nc_file.file_id:
                raise RuntimeError(f"The Nextcloud photo changed after the preview: {relative}")
            self.remote_cache[webdav_path] = (nc_file, faces)
        return self.remote_cache[webdav_path]

    @staticmethod
    def _overlap(faces: list[FaceRegion], rect: Rect) -> list[FaceRegion]:
        return [face for face in faces if face.rect.iou(rect) >= 0.4]

    def _assign_memories(self, action: dict[str, Any]) -> dict[str, Any]:
        nc_file, faces = self._remote(action)
        person = sanitize_person_name(str(action["person"])).strip()
        detection_id = action.get("nc_detection_id")
        face = next(
            (candidate for candidate in faces if candidate.nc_detection_id == detection_id),
            None,
        )
        wanted = Rect(*map(float, action["rect"])).clamp()
        if face is None:
            same = [candidate for candidate in self._overlap(faces, wanted) if person_names_match(candidate.person, person)]
            if same:
                face = same[0]
                return {
                    "changed": False,
                    "nc_file_id": nc_file.file_id,
                    "nc_detection_id": face.nc_detection_id,
                }
            raise RuntimeError(f"The Memories face changed after the preview: {action['path']}")
        if person_names_match(face.person, person):
            return {
                "changed": False,
                "nc_file_id": nc_file.file_id,
                "nc_detection_id": face.nc_detection_id,
            }
        expected_person = sanitize_person_name(str(action.get("old_person") or "")).strip()
        if expected_person:
            if not person_names_match(face.person, expected_person):
                raise RuntimeError(f"The Memories face name changed after the preview: {action['path']}")
        elif face.person:
            raise RuntimeError(f"The Memories face was named after the preview: {action['path']}")
        if self.backend is None:
            raise RuntimeError("The Nextcloud connection is unavailable.")
        cluster = (
            0
            if getattr(self.backend, "supports_assign", False)
            else self.backend.get_or_create_cluster(person)
        )
        self.backend.assign_person(face, person, nc_file, cluster)
        return {
            "changed": True,
            "nc_file_id": nc_file.file_id,
            "nc_detection_id": face.nc_detection_id,
        }

    def _insert_memories(self, action: dict[str, Any]) -> dict[str, Any]:
        nc_file, faces = self._remote(action)
        person = sanitize_person_name(str(action["person"])).strip()
        wanted = Rect(*map(float, action["rect"])).clamp()
        overlaps = self._overlap(faces, wanted)
        same = [face for face in overlaps if person_names_match(face.person, person)]
        if same:
            return {
                "changed": False,
                "nc_file_id": nc_file.file_id,
                "nc_detection_id": same[0].nc_detection_id,
            }
        if overlaps:
            raise RuntimeError(f"A different Memories face now overlaps the approved region in {action['path']}.")
        if self.backend is None:
            raise RuntimeError("The Nextcloud connection is unavailable.")
        cluster = self.backend.get_or_create_cluster(person)
        # Every box digiKam drew is offered as the detection. Recognize's own
        # detector still gets first refusal, so this only changes what happens
        # to the faces it finds nothing in, which are the small ones in group
        # shots that a review said were right anyway.
        reviewed = action.get("confirmed_face") is True
        confirmed = reviewed or bool(
            getattr(self.backend, "supports_confirmed_insert", False)
        )
        detection_id = self.backend.insert_detection(
            file_id=nc_file.file_id,
            rect=wanted,
            cluster_id=cluster,
            person=person,
            confirmed=confirmed,
        )
        faces.append(
            FaceRegion(
                person=person,
                rect=wanted,
                source="nextcloud",
                nc_file_id=nc_file.file_id,
                nc_detection_id=detection_id,
                nc_cluster_id=cluster,
                dav_parent=person,
                file_name=nc_file.name,
            )
        )
        result = {
            "changed": True,
            "nc_file_id": nc_file.file_id,
            "nc_detection_id": detection_id,
        }
        score = getattr(self.backend, "last_insert_score", None)
        if score is not None:
            # Kept so the cost of confirming by default stays answerable from
            # the journal: a zero here is a descriptor taken from the box.
            result["score"] = score
        return result

    def execute(self, action: dict[str, Any]) -> dict[str, Any]:
        operation = action["operation"]
        if operation == "assign_memories":
            result = self._assign_memories(action)
        elif operation == "insert_memories":
            result = self._insert_memories(action)
        elif operation == "create_digikam":
            result = self.digikam.create_face(
                str(action["path"]), str(action["person"]), tuple(action["rect"]),
                image_id=action.get("digikam_image_id"),
            )
        elif operation == "reassign_digikam":
            result = self.digikam.reassign_face(
                str(action["path"]), str(action["old_person"]), str(action["person"]),
                tuple(action["rect"]), image_id=action.get("digikam_image_id"),
                old_tag_id=action.get("digikam_tag_id"),
            )
        else:
            raise ValueError(f"Unknown apply operation: {operation}")
        return {**result, "target": action["target"], "operation": operation}

    def link_for(self, action: dict[str, Any], result: dict[str, Any]) -> dict[str, Any] | None:
        detection_id = result.get("nc_detection_id", action.get("nc_detection_id"))
        file_id = result.get("nc_file_id", action.get("nc_file_id"))
        local = {
            "digikam_image_id": result.get("digikam_image_id", action.get("digikam_image_id")),
            "digikam_tag_id": result.get("digikam_tag_id", action.get("digikam_tag_id")),
        }
        if local["digikam_image_id"] is None or local["digikam_tag_id"] is None:
            try:
                local = self.digikam.locate_face(
                    str(action["path"]), str(action["person"]),
                    tuple(action.get("digikam_rect") or action["rect"]),
                    image_id=action.get("digikam_image_id"),
                ) or local
            except (DigikamChangedError, ValueError):
                pass
        if None in (file_id, detection_id, local.get("digikam_image_id"), local.get("digikam_tag_id")):
            return None
        local_rect = action.get("digikam_rect") or action["rect"]
        remote_rect = action.get("nextcloud_rect") or action["rect"]
        return {
            **local,
            "nextcloud_file_id": int(file_id),
            "nextcloud_detection_id": int(detection_id),
            "digikam_name": str(action["person"]),
            "nextcloud_name": str(action["person"]),
            "digikam_rect": local_rect,
            "nextcloud_rect": remote_rect,
        }
