"""Preview named Memories/Recognize faces that are missing from digiKam."""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable, Iterable, Optional

from . import checkpoint as checkpoint_module
from . import ledger as ledger_module
from .constants import ASK, DEFAULT_SKIP_PERSONS
from .digikam import DigikamDB
from .matching import match_regions, overlapping_same_person
from .models import (
    NextcloudNamedFace,
    RegionAction,
    RegionConflict,
    SyncReport,
)
from .names import person_names_match, sanitize_person_name
from .paths import normalize_path


def _library_relative_path(path: str, nextcloud_photos_path: str) -> Optional[str]:
    remote = normalize_path(path).strip("/")
    prefix = normalize_path(nextcloud_photos_path).strip("/")
    if not prefix:
        return remote or None
    if remote.lower() == prefix.lower():
        return None
    marker = prefix + "/"
    if not remote.lower().startswith(marker.lower()):
        return None
    return remote[len(marker) :]


def selected_memories_faces(
    faces: Iterable[NextcloudNamedFace],
    *,
    nextcloud_photos_path: str,
    only_person: Optional[str],
) -> list[tuple[str, NextcloudNamedFace]]:
    """Return ``(digiKam-relative path, face)`` pairs inside the configured folder."""
    wanted_person = sanitize_person_name(only_person or "").strip()
    skipped_people = {person.lower() for person in DEFAULT_SKIP_PERSONS}
    selected: list[tuple[str, NextcloudNamedFace]] = []
    for named_face in faces:
        person = sanitize_person_name(named_face.face.person).strip()
        if not person or person.lower() in skipped_people:
            continue
        if wanted_person and not person_names_match(person, wanted_person):
            continue
        relative = _library_relative_path(
            named_face.file.webdav_path or named_face.file.path,
            nextcloud_photos_path,
        )
        if relative:
            selected.append((relative, named_face))
    return selected


def compare_memories_to_digikam(
    digikam: DigikamDB,
    selected_faces: list[tuple[str, NextcloudNamedFace]],
    report: SyncReport,
    *,
    iou_threshold: float = 0.4,
    batch_size: int = 250,
    max_actions: int = 5000,
    max_conflicts: int = 5000,
    max_unmatched: int = 500,
    conflict_policy: str = ASK,
    create_in_digikam: bool = True,
    progress_callback: Optional[Callable[[dict[str, Any]], None]] = None,
    ledger: Optional[ledger_module.Ledger] = None,
    checkpoint: Optional[checkpoint_module.Checkpoint] = None,
    start_index: int = 0,
) -> SyncReport:
    """Merge the preview of Memories-originating faces into ``report``.

    This function is deliberately read-only. A named Memories face with no
    overlapping digiKam rectangle becomes a proposed ``create_digikam`` action.
    An overlapping rectangle with a different name is read against the ledger,
    exactly as the forward pass does, and only becomes a conflict when neither
    side can be shown to have changed and no library is trusted to win.
    """
    face_ledger = ledger if ledger is not None else ledger_module.NullLedger()
    progress = checkpoint if checkpoint is not None else checkpoint_module.NullCheckpoint()
    grouped: dict[str, list[NextcloudNamedFace]] = defaultdict(list)
    for relative, named_face in selected_faces:
        grouped[normalize_path(relative).strip("/")].append(named_face)

    paths = sorted(grouped, key=str.casefold)
    report.files_memories = len(paths)
    report.faces_memories = sum(len(group) for group in grouped.values())
    if start_index:
        # The path order is deterministic, so a position is enough to resume.
        paths = paths[int(start_index):]
    done = 0
    reverse_matched = 0

    for start in range(0, len(paths), batch_size):
        batch_paths = paths[start : start + batch_size]
        resolved = digikam.images_for_relative_paths(batch_paths)
        for relative in batch_paths:
            key = normalize_path(relative).strip("/").lower()
            image = resolved.get(key)
            remote = grouped[relative]
            if image is None:
                report.files_unmatched_nextcloud += 1
                if len(report.unmatched_nextcloud_paths) < max_unmatched:
                    report.unmatched_nextcloud_paths.append(relative)
                continue
            reverse_matched += 1

            remote_faces = [entry.face for entry in remote]
            pairs, _only_digikam, only_memories = match_regions(
                image.faces,
                remote_faces,
                iou_threshold,
            )
            for digikam_face, memories_face, iou in pairs:
                file_id = int(memories_face.nc_file_id or remote[0].file.file_id)
                if person_names_match(digikam_face.person, memories_face.person):
                    face_ledger.record([
                        {
                            "nextcloud_file_id": file_id,
                            "nextcloud_detection_id": memories_face.nc_detection_id,
                            "digikam_image_id": image.image_id,
                            "digikam_tag_id": digikam_face.digikam_tag_id,
                            "synced_name": sanitize_person_name(digikam_face.person),
                            "digikam_rect": list(digikam_face.rect.as_tuple()),
                            "nextcloud_rect": list(memories_face.rect.as_tuple()),
                        }
                    ])
                    continue
                verdict = ledger_module.attribute(
                    digikam_face.person,
                    memories_face.person,
                    face_ledger.agreed_name(file_id, memories_face.nc_detection_id),
                )
                if verdict in (
                    ledger_module.MEMORIES_CHANGED,
                    ledger_module.DIGIKAM_CHANGED,
                ):
                    # The forward pass owns this pair and has already proposed
                    # the change. Recording it twice would double-count it.
                    continue
                if conflict_policy != ASK:
                    # A trusted library settles this without being asked, and
                    # the forward pass has already proposed what that means.
                    continue
                conflict = RegionConflict(
                    path=relative,
                    digikam_person=digikam_face.person,
                    digikam_rect=digikam_face.rect.as_tuple(),
                    nextcloud_person=memories_face.person,
                    nextcloud_rect=memories_face.rect.as_tuple(),
                    iou=iou,
                    nc_detection_id=memories_face.nc_detection_id,
                    nc_file_id=file_id,
                    digikam_image_id=image.image_id,
                    digikam_tag_id=digikam_face.digikam_tag_id,
                )
                if not report.first_sight_of(conflict):
                    continue
                if len(report.conflicts) < max_conflicts:
                    report.conflicts.append(conflict)

            for memories_face in only_memories:
                represented = overlapping_same_person(
                    memories_face,
                    image.faces,
                    iou_threshold,
                )
                if represented is not None:
                    digikam_face, iou = represented
                    report.skipped += 1
                    report.actions_total += 1
                    if report.actions_total <= max_actions:
                        report.actions.append(
                            RegionAction(
                                action="skip",
                                path=relative,
                                person=memories_face.person,
                                rect=memories_face.rect.as_tuple(),
                                detail=(
                                    "already represented by an overlapping "
                                    "same-person digiKam face "
                                    f"(IoU={iou:.2f})"
                                ),
                                nc_file_id=memories_face.nc_file_id,
                                nc_detection_id=memories_face.nc_detection_id,
                                nc_cluster_id=memories_face.nc_cluster_id,
                                digikam_image_id=image.image_id,
                                digikam_tag_id=digikam_face.digikam_tag_id,
                                nc_file_name=memories_face.file_name,
                            )
                        )
                    continue
                if not create_in_digikam:
                    report.skipped += 1
                    continue
                report.created_in_digikam += 1
                report.actions_total += 1
                if report.actions_total <= max_actions:
                    report.actions.append(
                        RegionAction(
                            action="create_digikam",
                            path=relative,
                            person=memories_face.person,
                            rect=memories_face.rect.as_tuple(),
                            detail="create face in digiKam from Memories",
                            nc_file_id=memories_face.nc_file_id,
                            nc_detection_id=memories_face.nc_detection_id,
                            nc_cluster_id=memories_face.nc_cluster_id,
                            digikam_image_id=image.image_id,
                            nc_file_name=memories_face.file_name,
                        )
                    )

        done += len(batch_paths)
        progress.batch_done(
            checkpoint_module.SCANNING_MEMORIES,
            {"path_index": int(start_index) + done},
            report,
        )
        if progress_callback is not None:
            progress_callback(
                {
                    "phase": "scanning_memories",
                    "current": int(start_index) + done,
                    "total": int(start_index) + len(paths),
                    "matched": report.files_matched + reverse_matched,
                    "assigned": report.assigned,
                    "inserted": report.inserted,
                    "created_in_digikam": report.created_in_digikam,
                    "skipped": report.skipped,
                    "conflicts": report.conflict_count,
                }
            )
    face_ledger.flush()
    return report
