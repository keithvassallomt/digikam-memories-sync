"""Core digiKam → Nextcloud face sync orchestration (batched for large libraries)."""
from __future__ import annotations

import logging
import signal
import time
from typing import Any, Callable, Optional

from .constants import DEFAULT_SKIP_PERSONS
from .digikam import DigikamDB
from .matching import match_files, match_regions
from .models import (
    NextcloudBackend,
    RegionAction,
    RegionConflict,
    SyncReport,
)
from .names import person_names_match, sanitize_person_name
from .session_state import SessionState

LOG = logging.getLogger(__name__)


class SyncCancelled(Exception):
    """Raised when the operator cancels (SIGINT/SIGTERM) mid-sync."""

# Cap retained detail so 500k faces cannot exhaust RAM on the report alone
DEFAULT_MAX_ACTIONS = 2_000
DEFAULT_MAX_CONFLICTS = 5_000
DEFAULT_MAX_WARNINGS = 100
DEFAULT_MAX_UNMATCHED = 500


def _record_action(
    report: SyncReport,
    action: RegionAction,
    *,
    max_actions: int,
) -> None:
    if len(report.actions) < max_actions:
        report.actions.append(action)
    elif len(report.actions) == max_actions:
        report.actions.append(
            RegionAction(
                action="skip",
                path="…",
                person="",
                rect=(0, 0, 0, 0),
                detail=f"(further actions truncated; max_actions={max_actions})",
            )
        )


def _record_conflict(
    report: SyncReport,
    conflict: RegionConflict,
    *,
    max_conflicts: int,
) -> None:
    if len(report.conflicts) < max_conflicts:
        report.conflicts.append(conflict)


def _record_warning(report: SyncReport, msg: str, *, max_warnings: int) -> None:
    if msg in report.warnings:
        return
    if len(report.warnings) < max_warnings:
        report.warnings.append(msg)
    elif len(report.warnings) == max_warnings:
        report.warnings.append(f"(further warnings truncated; max={max_warnings})")


def _process_match(
    m,
    nc_faces: list,
    nextcloud: NextcloudBackend,
    report: SyncReport,
    *,
    apply: bool,
    insert_missing: bool,
    prefer_digikam_on_conflict: bool,
    iou_threshold: float,
    cluster_cache: dict[str, int],
    max_actions: int,
    max_conflicts: int,
    max_warnings: int,
) -> None:
    path = m.digikam.relative_path
    for nf in nc_faces:
        if not nf.file_name:
            nf.file_name = m.nextcloud.name

    pairs, only_dk, _only_nc = match_regions(
        m.digikam.faces, nc_faces, iou_threshold
    )

    def resolve_cluster(person: str) -> int:
        person = sanitize_person_name(person)
        key = person.strip().lower()
        if not key:
            raise ValueError("empty person name after sanitization")
        if key in cluster_cache:
            return cluster_cache[key]
        if not apply:
            return -1
        cid = nextcloud.get_or_create_cluster(person)
        cluster_cache[key] = cid
        return cid

    for df, nf, iou in pairs:
        dk_person = sanitize_person_name(df.person)
        same_person = person_names_match(dk_person, nf.person or "")
        if same_person:
            report.skipped += 1
            _record_action(
                report,
                RegionAction(
                    action="skip",
                    path=path,
                    person=dk_person,
                    rect=df.rect.as_tuple(),
                    detail=f"already assigned (IoU={iou:.2f})",
                    nc_file_id=m.nextcloud.file_id,
                    nc_detection_id=nf.nc_detection_id,
                    nc_cluster_id=nf.nc_cluster_id,
                ),
                max_actions=max_actions,
            )
            continue

        if nf.person and not person_names_match(dk_person, nf.person):
            conflict = RegionConflict(
                path=path,
                digikam_person=dk_person,
                digikam_rect=df.rect.as_tuple(),
                nextcloud_person=nf.person,
                nextcloud_rect=nf.rect.as_tuple(),
                iou=iou,
                nc_detection_id=nf.nc_detection_id,
                nc_file_id=m.nextcloud.file_id,
            )
            _record_conflict(report, conflict, max_conflicts=max_conflicts)
            if not prefer_digikam_on_conflict:
                _record_action(
                    report,
                    RegionAction(
                        action="conflict",
                        path=path,
                        person=dk_person,
                        rect=df.rect.as_tuple(),
                        detail=(
                            f"conflict with NC person {nf.person!r} "
                            f"(IoU={iou:.2f}); left unchanged"
                        ),
                        nc_file_id=m.nextcloud.file_id,
                        nc_detection_id=nf.nc_detection_id,
                    ),
                    max_actions=max_actions,
                )
                continue
            detail = (
                f"CONFLICT resolved → digiKam person {dk_person!r} "
                f"overwrites NC {nf.person!r} (IoU={iou:.2f})"
            )
        else:
            detail = (
                f"assign unclustered/empty NC detection to {dk_person!r} "
                f"(IoU={iou:.2f})"
            )

        if not nextcloud.supports_insert and not nf.dav_parent:
            _record_warning(
                report,
                "Some detections are not DAV-addressable (null cluster); "
                "use DB/SSH backend for those.",
                max_warnings=max_warnings,
            )
            report.skipped += 1
            continue

        cluster_id = resolve_cluster(dk_person)
        if apply and nf.nc_detection_id is not None:
            try:
                nextcloud.assign_person(
                    nf, person=dk_person, nc_file=m.nextcloud, cluster_id=cluster_id
                )
            except Exception as e:
                _record_warning(
                    report,
                    f"assign failed for {path}: {e}",
                    max_warnings=max_warnings,
                )
                report.skipped += 1
                continue

        report.assigned += 1
        _record_action(
            report,
            RegionAction(
                action="assign",
                path=path,
                person=dk_person,
                rect=df.rect.as_tuple(),
                detail=detail,
                nc_file_id=m.nextcloud.file_id,
                nc_detection_id=nf.nc_detection_id,
                nc_cluster_id=cluster_id if cluster_id > 0 else None,
            ),
            max_actions=max_actions,
        )

    for df in only_dk:
        dk_person = sanitize_person_name(df.person)
        if not insert_missing:
            report.skipped += 1
            continue

        cluster_id = resolve_cluster(dk_person)
        server_generates_vector = bool(
            getattr(nextcloud, "generates_face_vectors", False)
        )
        vec = None
        if apply and cluster_id > 0 and not server_generates_vector:
            vec = nextcloud.sample_cluster_vector(cluster_id)
        if vec is None and not server_generates_vector:
            _record_warning(
                report,
                "Inserting faces without embeddings (zero vector). "
                "Prefer Recognize detect first, then --no-insert-missing.",
                max_warnings=max_warnings,
            )

        det_id = None
        if apply:
            try:
                det_id = nextcloud.insert_detection(
                    file_id=m.nextcloud.file_id,
                    rect=df.rect,
                    cluster_id=cluster_id,
                    person=dk_person,
                    face_vector=vec,
                    threshold=0.0,
                )
            except Exception as e:
                _record_warning(
                    report,
                    f"insert failed for {path}: {e}",
                    max_warnings=max_warnings,
                )
                report.skipped += 1
                continue

        report.inserted += 1
        _record_action(
            report,
            RegionAction(
                action="insert",
                path=path,
                person=dk_person,
                rect=df.rect.as_tuple(),
                detail="new detection from digiKam region",
                nc_file_id=m.nextcloud.file_id,
                nc_detection_id=det_id,
                nc_cluster_id=cluster_id if cluster_id > 0 else None,
            ),
            max_actions=max_actions,
        )


def sync(
    digikam: DigikamDB,
    nextcloud: NextcloudBackend,
    *,
    path_maps: list[tuple[str, str]],
    nc_path_prefix: str = "files/",
    storage_filter: Optional[str] = None,
    iou_threshold: float = 0.4,
    apply: bool = False,
    skip_persons: frozenset[str] = DEFAULT_SKIP_PERSONS,
    only_person: Optional[str] = None,
    insert_missing: bool = True,
    prefer_digikam_on_conflict: bool = True,
    batch_size: int = 500,
    limit_images: Optional[int] = None,
    max_actions: int = DEFAULT_MAX_ACTIONS,
    max_conflicts: int = DEFAULT_MAX_CONFLICTS,
    max_warnings: int = DEFAULT_MAX_WARNINGS,
    http_workers: int = 16,
    session: Optional[SessionState] = None,
    progress_callback: Optional[Callable[[dict[str, Any]], None]] = None,
) -> SyncReport:
    """
    Sync digiKam faces → Nextcloud in image batches.

    Memory stays O(batch_size) for digiKam faces (not O(total faces)).
    Report detail is capped; summary counters always cover the full run.

    When ``only_person`` is set, only digiKam images/faces for that person are
    processed (targeted index — useful for tests and quick single-person updates).

    When ``session`` is provided, progress is persisted after each batch so a
    cancelled run can be resumed with the same session id (already-processed
    digiKam image ids are skipped).
    """
    report = SyncReport()
    t_start = time.monotonic()
    cancelled = False

    def publish_progress(phase: str, current: int, total: int) -> None:
        if progress_callback is None:
            return
        progress_callback(
            {
                "phase": phase,
                "current": current,
                "total": total,
                "matched": report.files_matched,
                "assigned": report.assigned,
                "inserted": report.inserted,
                "skipped": report.skipped,
                "conflicts": report.conflict_count,
            }
        )

    publish_progress("indexing", 0, 0)

    # Seed counters from a previous partial run
    if session is not None:
        session.apply_snapshot_to_report(report)
        LOG.info(
            "Session %s: resuming with %d processed image ids "
            "(status was %s)",
            session.session_id,
            len(session.processed_image_ids),
            session.status,
        )

    only_person_clean: Optional[str] = None
    if only_person:
        only_person_clean = sanitize_person_name(only_person).strip()
        if not only_person_clean:
            report.warnings.append(
                f"only_person {only_person!r} is empty after sanitization; nothing to do."
            )
            if session is not None:
                session.finish("completed", "nothing to do (empty only_person)")
            return report
        if only_person_clean.lower() in {s.lower() for s in skip_persons}:
            report.warnings.append(
                f"only_person {only_person_clean!r} is in skip_persons; nothing to do."
            )
            if session is not None:
                session.finish("completed", "nothing to do (only_person skipped)")
            return report
        LOG.info("=== only_person filter: %r ===", only_person_clean)

    if insert_missing and not nextcloud.supports_insert:
        _record_warning(
            report,
            "Backend does not support inserting new face detections "
            "(HTTP/WebDAV). Only assignment of existing Recognize faces will run.",
            max_warnings=max_warnings,
        )
        insert_missing = False

    t0 = time.monotonic()
    if only_person_clean:
        LOG.info(
            "=== Phase: index digiKam images for person %r "
            "(targeted tag lookup — not a full library scan) ===",
            only_person_clean,
        )
        # Targeted person index — skip full-library tagRegion scan
        person_image_ids = digikam.image_ids_for_person(only_person_clean)
        total_images = len(person_image_ids)
        LOG.info(
            "digiKam person index ready: %s images for %r (%.1fs)",
            f"{total_images:,}",
            only_person_clean,
            time.monotonic() - t0,
        )
    else:
        LOG.info(
            "=== Phase: index digiKam library "
            "(one scan of tagRegion rows — expect progress logs, then batches) ==="
        )
        # Builds faced-image id list with progress; replaces slow COUNT DISTINCT
        total_images = digikam.count_images_with_faces()
        total_faces = digikam.count_face_regions()
        LOG.info(
            "digiKam index ready: %s images with faces, %s tagRegion rows (%.1fs)",
            f"{total_images:,}",
            f"{total_faces:,}",
            time.monotonic() - t0,
        )
    if limit_images is not None:
        LOG.info("Limiting to first %d images (--limit-images)", limit_images)
        total_images = min(total_images, limit_images)

    publish_progress("scanning", 0, total_images)

    if session is not None:
        session.set_total_images(total_images)
        session.save(force=True)

    if total_images == 0:
        if only_person_clean:
            report.warnings.append(
                f"No digiKam images with face regions for person "
                f"{only_person_clean!r}."
            )
        else:
            report.warnings.append("No digiKam images with face regions found.")
        if session is not None:
            session.finish("completed", "no images")
        return report

    already_done = len(session.processed_image_ids) if session else 0
    remaining_images = max(0, total_images - already_done)
    if already_done:
        LOG.info(
            "Resume: %d / %d images already done → %d remaining",
            already_done,
            total_images,
            remaining_images,
        )

    # Cooperative cancel: finish current image, then stop before next batch
    cancel_requested = False
    prev_sigint = None
    prev_sigterm = None

    def _on_signal(signum, frame) -> None:  # type: ignore[no-untyped-def]
        nonlocal cancel_requested
        name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
        if cancel_requested:
            LOG.warning(
                "Second %s received — exiting hard (state may be mid-batch)",
                name,
            )
            raise SystemExit(130)
        cancel_requested = True
        LOG.warning(
            "=== %s received: will cancel after the current image "
            "(session state will be saved for --resume) ===",
            name,
        )

    if session is not None:
        prev_sigint = signal.signal(signal.SIGINT, _on_signal)
        try:
            prev_sigterm = signal.signal(signal.SIGTERM, _on_signal)
        except (ValueError, OSError):
            prev_sigterm = None

    try:
        # Preload person clusters once (thousands of people is fine)
        LOG.info("=== Loading Nextcloud person clusters ===")
        cluster_cache = nextcloud.list_face_clusters()
        LOG.info("Nextcloud person clusters: %d", len(cluster_cache))

        resolve = getattr(nextcloud, "resolve_digikam_images", None)
        supports_resolve = callable(resolve)

        # HTTP tuning: workers + pre-create person folders for digiKam people
        if hasattr(nextcloud, "http_workers"):
            try:
                nextcloud.http_workers = http_workers  # type: ignore[attr-defined]
            except Exception:
                pass
        # Probe Recognize DAV early (HTTP) so missing API key fails once, not 5000×
        ensure_access = getattr(nextcloud, "ensure_recognize_access", None)
        if callable(ensure_access):
            LOG.info("=== Checking Recognize DAV access ===")
            ensure_access()

        ensure_people = getattr(nextcloud, "ensure_people", None)
        if apply and callable(ensure_people):
            skip_l = {s.lower() for s in skip_persons}
            people = []
            seen: set[str] = set()
            if only_person_clean:
                candidates = [only_person_clean]
            else:
                candidates = list(digikam.person_tag_ids().values())
            for n in candidates:
                n = sanitize_person_name(n or "")
                if not n or n.lower() in skip_l:
                    continue
                key = n.lower()
                if key in seen:
                    continue
                seen.add(key)
                people.append(n)
            LOG.info(
                "=== Ensuring up to %d digiKam people exist in Recognize ===",
                len(people),
            )
            ensure_people(people)
            cluster_cache = nextcloud.list_face_clusters()
            LOG.info("Nextcloud person clusters after ensure: %d", len(cluster_cache))

        if supports_resolve and not nextcloud.supports_insert:
            LOG.info(
                "HTTP mode: targeted keep-alive PROPFIND per faced image "
                "(batch_size=%d, workers=%d). Assign-only — Recognize must already "
                "have face boxes.",
                batch_size,
                http_workers,
            )

        # Progress denominators: library total (includes already-done on resume)
        images_done = already_done
        faces_done = session.faces_done if session else 0
        batch_num = 0
        expected_batches = max(
            1, (remaining_images + batch_size - 1) // batch_size
        ) if remaining_images else 0

        LOG.info(
            "=== Phase: process digiKam images "
            "(batch_size=%d, ~%d remaining batches, resolve=%s, apply=%s) ===",
            batch_size,
            expected_batches,
            "targeted-HTTP/DB" if supports_resolve else "list+match",
            apply,
        )
        LOG.info(
            "Next step: open digiKam batches from the in-memory id index "
            "(should be fast), then PROPFIND Nextcloud for each batch…"
        )

        # Full-library list only if backend cannot resolve paths (legacy path)
        nc_files_cache = None
        if not supports_resolve:
            LOG.info("Backend has no targeted resolver; listing Nextcloud images once…")
            nc_files_cache = nextcloud.list_image_files(
                path_prefix=nc_path_prefix, storage_filter=storage_filter
            )
            report.files_nextcloud = len(nc_files_cache)
            LOG.info("Nextcloud image files listed: %d", report.files_nextcloud)

        skip_ids = session.processed_image_ids if session else None
        LOG.info("Starting digiKam batch iterator…")
        for batch in digikam.iter_image_batches(
            batch_size=batch_size,
            skip_persons=skip_persons,
            limit_images=limit_images,
            only_person=only_person_clean,
            skip_image_ids=skip_ids,
        ):
            if cancel_requested:
                cancelled = True
                break

            batch_num += 1
            batch_t0 = time.monotonic()
            batch_faces = sum(len(im.faces) for im in batch)
            report.files_digikam += len(batch)
            report.faces_digikam += batch_faces

            LOG.info(
                "--- Batch %d / ~%d: digiKam loaded %d images / %d faces ---",
                batch_num,
                expected_batches,
                len(batch),
                batch_faces,
            )

            # --- resolve paths + fetch NC faces ---
            t_resolve = time.monotonic()
            if supports_resolve:
                LOG.info(
                    "Batch %d: resolving %d paths on Nextcloud "
                    "(PROPFIND + face-detections)…",
                    batch_num,
                    len(batch),
                )
                matches, unmatched, nc_by_file = resolve(batch, path_maps)
            else:
                LOG.info(
                    "Batch %d: matching against listed Nextcloud files…", batch_num
                )
                matches, unmatched = match_files(
                    batch,
                    nc_files_cache or [],
                    path_maps=path_maps,
                    nc_path_strip=nc_path_prefix,
                )
                files_by_id = {m.nextcloud.file_id: m.nextcloud for m in matches}
                LOG.info(
                    "Batch %d: loading face detections for %d matched files…",
                    batch_num,
                    len(files_by_id),
                )
                nc_by_file = (
                    nextcloud.list_detections_for_files(
                        list(files_by_id.keys()), files_by_id
                    )
                    if matches
                    else {}
                )
            nc_face_n = sum(len(v) for v in nc_by_file.values())
            LOG.info(
                "Batch %d: path resolve done in %.1fs → matched=%d unmatched=%d "
                "NC face boxes=%d",
                batch_num,
                time.monotonic() - t_resolve,
                len(matches),
                len(unmatched),
                nc_face_n,
            )

            report.files_matched += len(matches)
            report.files_unmatched_digikam += len(unmatched)
            report.faces_nextcloud += nc_face_n

            # Keep a sample of unmatched paths only
            if len(report.unmatched_paths) < DEFAULT_MAX_UNMATCHED:
                room = DEFAULT_MAX_UNMATCHED - len(report.unmatched_paths)
                report.unmatched_paths.extend(
                    sorted({u.relative_path for u in unmatched})[:room]
                )

            # --- compare regions / assign ---
            t_cmp = time.monotonic()
            n_matches = len(matches)
            cmp_every = max(1, min(50, n_matches // 10 or 1))
            a0, i0, s0, c0 = (
                report.assigned,
                report.inserted,
                report.skipped,
                report.conflict_count,
            )
            if n_matches:
                LOG.info(
                    "Batch %d: comparing / assigning faces on %d matched files "
                    "(apply=%s)…",
                    batch_num,
                    n_matches,
                    apply,
                )

            processed_ids: list[int] = []
            batch_cancelled_mid = False

            for mi, m in enumerate(matches, start=1):
                if cancel_requested:
                    batch_cancelled_mid = True
                    cancelled = True
                    break
                if mi == 1 or mi % cmp_every == 0 or mi == n_matches:
                    LOG.info(
                        "  Batch %d compare: file %d / %d | "
                        "assign+%d insert+%d skip+%d conflict+%d",
                        batch_num,
                        mi,
                        n_matches,
                        report.assigned - a0,
                        report.inserted - i0,
                        report.skipped - s0,
                        report.conflict_count - c0,
                    )
                _process_match(
                    m,
                    nc_by_file.get(m.nextcloud.file_id, []),
                    nextcloud,
                    report,
                    apply=apply,
                    insert_missing=insert_missing,
                    prefer_digikam_on_conflict=prefer_digikam_on_conflict,
                    iou_threshold=iou_threshold,
                    cluster_cache=cluster_cache,
                    max_actions=max_actions,
                    max_conflicts=max_conflicts,
                    max_warnings=max_warnings,
                )
                processed_ids.append(m.digikam.image_id)

            LOG.info(
                "Batch %d: compare done in %.1fs "
                "(this batch: assign+%d insert+%d skip+%d conflict+%d)%s",
                batch_num,
                time.monotonic() - t_cmp,
                report.assigned - a0,
                report.inserted - i0,
                report.skipped - s0,
                report.conflict_count - c0,
                " [cancelled mid-batch]" if batch_cancelled_mid else "",
            )

            # Commit each batch when applying (limits transaction size / memory)
            if apply and (processed_ids or not batch_cancelled_mid):
                LOG.info("Batch %d: committing…", batch_num)
                nextcloud.commit()

            # Persist progress. Full batch → all digiKam ids (incl. path-map
            # skips / unmatched). Mid-cancel → only images we finished comparing.
            if session is not None:
                if batch_cancelled_mid:
                    done_ids = processed_ids
                    faces_for_done = sum(
                        len(im.faces)
                        for im in batch
                        if im.image_id in set(processed_ids)
                    )
                else:
                    done_ids = [im.image_id for im in batch]
                    faces_for_done = batch_faces
                if done_ids:
                    session.mark_processed(done_ids, faces=faces_for_done)
                    if not batch_cancelled_mid:
                        session.mark_batch_done()
                    session.update_report_from_sync_report(report)
                    session.save(force=True)

            images_done = (
                len(session.processed_image_ids) if session else images_done + len(batch)
            )
            faces_done = session.faces_done if session else faces_done + batch_faces
            elapsed = time.monotonic() - t_start
            batch_elapsed = time.monotonic() - batch_t0
            # Rate based on work done this process (not historical)
            this_run_images = images_done - already_done
            rate = this_run_images / elapsed if elapsed > 0 and this_run_images > 0 else 0
            remaining = max(0, total_images - images_done)
            eta = remaining / rate if rate > 0 else 0
            LOG.info(
                "=== Overall progress: %d / %d images (%.1f%%), ~%d faces | "
                "matched=%d unmatched=%d assign=%d insert=%d skip=%d conflicts=%d | "
                "batch %.1fs, %.1f img/s, ETA %s ===",
                images_done,
                total_images,
                100.0 * images_done / total_images if total_images else 100.0,
                faces_done,
                report.files_matched,
                report.files_unmatched_digikam,
                report.assigned,
                report.inserted,
                report.skipped,
                report.conflict_count,
                batch_elapsed,
                rate,
                f"{eta:.0f}s" if rate > 0 else "?",
            )
            publish_progress("scanning", images_done, total_images)

            if cancelled:
                break

        if cancelled:
            if apply:
                try:
                    nextcloud.commit()
                except Exception as e:
                    LOG.warning("Commit after cancel failed: %s", e)
            if session is not None:
                session.update_report_from_sync_report(report)
                session.finish(
                    "cancelled",
                    f"cancelled after {len(session.processed_image_ids)} images",
                )
                LOG.warning(
                    "=== CANCELLED session %s — resume with: "
                    "--resume %s ===",
                    session.session_id,
                    session.session_id,
                )
            else:
                LOG.warning("=== CANCELLED (no session state) ===")
            raise SyncCancelled(
                f"sync cancelled"
                + (
                    f" (session={session.session_id})"
                    if session is not None
                    else ""
                )
            )

        if apply:
            LOG.info("Final commit…")
            nextcloud.commit()
            LOG.info("Final commit done")
        else:
            nextcloud.rollback()
            LOG.info("Dry-run complete (no changes written)")

        elapsed = time.monotonic() - t_start
        LOG.info(
            "=== DONE in %.1fs: images=%d faces=%d matched=%d assigned=%d "
            "inserted=%d skipped=%d conflicts=%d ===",
            elapsed,
            report.files_digikam,
            report.faces_digikam,
            report.files_matched,
            report.assigned,
            report.inserted,
            report.skipped,
            report.conflict_count,
        )
        if session is not None:
            session.update_report_from_sync_report(report)
            session.finish("completed", "ok")
        publish_progress("completed", total_images, total_images)
        return report

    except SyncCancelled:
        raise
    except Exception as e:
        if session is not None:
            try:
                session.update_report_from_sync_report(report)
                session.finish("failed", f"{type(e).__name__}: {e}")
            except Exception as se:
                LOG.warning("Failed to persist session after error: %s", se)
        raise
    finally:
        if session is not None:
            if prev_sigint is not None:
                signal.signal(signal.SIGINT, prev_sigint)
            if prev_sigterm is not None:
                signal.signal(signal.SIGTERM, prev_sigterm)
