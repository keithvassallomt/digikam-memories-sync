"""UI-facing application operations, independent of HTTP presentation."""
from __future__ import annotations

import os
import sqlite3
import threading
import logging
import mimetypes
from datetime import datetime
from contextlib import closing
from pathlib import Path
from typing import Any, Callable

from .digikam import DigikamDB
from .digikam_writer import (
    DigikamWriter,
    create_sqlite_backup,
    digikam_is_running,
    terminate_digikam,
)
from .apply import ApplyExecutor, build_apply_plan, plan_summary
from .nextcloud_http import NextcloudConnectionError, NextcloudHTTP, fetch_file_preview
from .reverse import compare_memories_to_digikam, selected_memories_faces
from .settings import SettingsStore
from .state_store import StateStore
from .sync import sync

REQUIRED_DIGIKAM_TABLES = {"Images", "Tags", "TagProperties", "ImageTagProperties"}
BROWSER_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif"}
LOG = logging.getLogger(__name__)


class InvalidDigikamLibrary(ValueError):
    pass


def is_systemic_apply_failure(error: Exception) -> bool:
    """Return true when retrying every remaining action is likely to fail."""
    if isinstance(error, (NextcloudConnectionError, ConnectionError, TimeoutError, OSError)):
        return True
    message = str(error).lower()
    if "failed after retries" in message:
        return True
    return any(
        f"http {status}" in message
        for status in (401, 403, 408, 429, 500, 502, 503, 504)
    )


def resolve_digikam_database(value: str | Path) -> Path:
    selected = Path(value).expanduser()
    database = selected if selected.is_file() else selected / "digikam4.db"
    if not database.is_file():
        raise InvalidDigikamLibrary("No digikam4.db was found in that location.")
    try:
        uri = f"file:{database.resolve()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            tables = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
    except sqlite3.Error as error:
        raise InvalidDigikamLibrary("The digiKam database could not be read.") from error
    if not REQUIRED_DIGIKAM_TABLES.issubset(tables):
        raise InvalidDigikamLibrary("That file is not a compatible digiKam database.")
    return database.resolve()


def discover_digikam_databases() -> list[str]:
    home = Path.home()
    candidates = [
        os.environ.get("DIGIKAM_DB", ""),
        home / "Photos" / "digikam4.db",
        home / "Pictures" / "digikam4.db",
        home / ".local" / "share" / "digikam" / "digikam4.db",
        home / "Library" / "Application Support" / "digikam" / "digikam4.db",
    ]
    found: list[str] = []
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if path.is_file() and str(path.resolve()) not in found:
            found.append(str(path.resolve()))
    return found


class AppService:
    def __init__(
        self,
        settings: SettingsStore,
        state: StateStore,
        *,
        backend_factory: Callable[..., NextcloudHTTP] = NextcloudHTTP,
        digikam_factory: Callable[..., DigikamDB] = DigikamDB,
        sync_function: Callable[..., Any] = sync,
    ):
        self.settings = settings
        self.state = state
        self.backend_factory = backend_factory
        self.digikam_factory = digikam_factory
        self.sync_function = sync_function
        self._job_lock = threading.Lock()
        self._jobs: dict[int, threading.Thread] = {}

    def public_settings(self) -> dict[str, Any]:
        return self.settings.public_settings()

    def test_connection(self, payload: dict[str, Any]) -> dict[str, Any]:
        database = resolve_digikam_database(str(payload.get("digikam_library", "")))
        user_id = str(payload.get("nc_user", "")).strip()
        password = str(payload.get("password", "")) or self.settings.password(user_id)
        if not user_id or not password:
            raise ValueError("Enter your Nextcloud username and app password.")
        url = str(payload.get("nextcloud_url", "")).strip()
        if not url:
            raise ValueError("Enter your Nextcloud address.")

        backend = self.backend_factory(url, user_id, password, http_workers=1)
        try:
            requirements = backend.connection_requirements()
            return {
                "digikam_db": str(database),
                "recognize_installed": requirements.recognize_installed,
                "face_sync_installed": requirements.face_sync_installed,
                "install_url": requirements.face_sync_install_url,
                "ready": requirements.ready,
            }
        finally:
            backend.close()

    def save_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = self.test_connection(payload)
        if not result["ready"]:
            return result
        settings = {
            "digikam_library": str(Path(result["digikam_db"]).parent),
            "digikam_db": result["digikam_db"],
            "nextcloud_url": str(payload["nextcloud_url"]).strip().rstrip("/"),
            "nc_user": str(payload["nc_user"]).strip(),
            "nc_photos_path": str(payload.get("nc_photos_path", "Photos")).strip("/"),
        }
        self.settings.save(settings, str(payload.get("password", "")) or None)
        return {**result, "saved": True, "settings": self.settings.public_settings()}

    def people(self) -> list[str]:
        settings = self.settings.load()
        database = settings.get("digikam_db")
        if not database:
            return []
        user_id = str(settings["nc_user"])
        password = self.settings.password(user_id)
        if not password:
            raise ValueError("The saved Nextcloud app password is unavailable.")
        backend = self.backend_factory(
            str(settings["nextcloud_url"]), user_id, password, http_workers=4
        )
        try:
            with self.digikam_factory(database) as digikam:
                digikam_people = set(digikam.person_tag_ids().values())
            memories_people = set(backend.list_named_people())
        finally:
            backend.close()
        return sorted(digikam_people | memories_people, key=str.casefold)

    @staticmethod
    def _preview_selection(payload: dict[str, Any]) -> tuple[str, str]:
        scope = str(payload.get("scope", "person"))
        if scope not in {"all", "person"}:
            raise ValueError("Choose All faces or One person.")
        person = str(payload.get("person", "")).strip() if scope == "person" else ""
        if scope == "person" and not person:
            raise ValueError("Choose a person.")
        return scope, person

    def start_preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.settings.load().get("digikam_db"):
            raise ValueError("Complete the connection setup first.")
        scope, person = self._preview_selection(payload)
        with self._job_lock:
            active = [run_id for run_id, thread in self._jobs.items() if thread.is_alive()]
            if active:
                raise ValueError(f"Preview {active[0]} is already running.")
            run_id = self.state.create_run(scope)
            thread = threading.Thread(
                target=self._preview_job,
                args=(run_id, {"scope": scope, "person": person}),
                name=f"face-sync-preview-{run_id}",
                daemon=True,
            )
            self._jobs[run_id] = thread
            thread.start()
        return {"run_id": run_id, "status": "previewing"}

    def _preview_job(self, run_id: int, payload: dict[str, Any]) -> None:
        try:
            self.preview(payload, run_id=run_id)
        except Exception:
            LOG.exception("Preview %s failed", run_id)
        finally:
            with self._job_lock:
                self._jobs.pop(run_id, None)

    def preview_status(self, run_id: int) -> dict[str, Any]:
        result = self.state.run(run_id)
        if result is None:
            raise ValueError("Preview run not found.")
        return result

    def discard_preview(self, run_id: int) -> dict[str, Any]:
        with self._job_lock:
            thread = self._jobs.get(run_id)
            if thread is not None and thread.is_alive():
                raise ValueError("This preview is still running and cannot be discarded yet.")
        self.state.discard_preview(run_id)
        return {"run_id": run_id, "status": "discarded"}

    def conflicts(self, run_id: int) -> dict[str, Any]:
        return self.state.conflicts_for_run(run_id)

    def apply_review(self, run_id: int) -> dict[str, Any]:
        run = self.state.run(run_id)
        if run is None or run.get("result") is None:
            raise ValueError("Preview run not found.")
        if run["status"] not in {"previewed", "applying", "apply_failed", "applied"}:
            raise ValueError("This preview is not ready to apply.")
        conflicts = self.state.conflicts_for_run(run_id)
        plan = build_apply_plan(run["result"], conflicts)
        summary = (
            self.state.remaining_apply_counts(run_id)
            if run.get("apply") is not None
            else plan_summary(plan)
        )
        return {
            "run_id": run_id,
            **summary,
            "conflicts": conflicts["total"],
            "digikam_running": digikam_is_running(),
            "requires_digikam_closed": summary["digikam"] > 0,
            "status": run["status"],
            "apply": run.get("apply"),
        }

    def close_digikam(self) -> dict[str, Any]:
        result = terminate_digikam()
        if not result["supported"]:
            raise ValueError("Automatic closing is unavailable on this platform. Close digiKam normally.")
        if not result["closed"]:
            raise ValueError("digiKam did not close. Quit it manually, then try again.")
        return {"closed": True}

    def start_apply(self, run_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        review = self.apply_review(run_id)
        if review["status"] == "applied":
            return self.preview_status(run_id)
        if review["total"] == 0:
            raise ValueError("There are no changes to apply.")
        if review["requires_digikam_closed"]:
            if payload.get("digikam_closed") is not True:
                raise ValueError("Confirm that digiKam is closed before applying changes.")
            if digikam_is_running():
                raise ValueError("digiKam is still running. Close it, then try Apply again.")
        run = self.state.run(run_id)
        assert run is not None and run["result"] is not None
        plan = build_apply_plan(run["result"], self.state.conflicts_for_run(run_id))
        with self._job_lock:
            active = [job_id for job_id, thread in self._jobs.items() if thread.is_alive()]
            if active:
                raise ValueError("Another Face Sync job is already running.")
            self.state.initialize_apply(run_id, plan)
            counts = self.state.apply_counts(run_id)
            self.state.update_progress(
                run_id,
                "starting_apply",
                counts["applied"] + counts["ignored"],
                counts["total"],
                counts,
            )
            thread = threading.Thread(
                target=self._apply_job,
                args=(run_id,),
                name=f"face-sync-apply-{run_id}",
                daemon=True,
            )
            self._jobs[run_id] = thread
            thread.start()
        return {"run_id": run_id, "status": "applying"}

    def _apply_job(self, run_id: int) -> None:
        backend = None
        writer = None
        try:
            settings = self.settings.load()
            database = str(settings.get("digikam_db") or "")
            if not database:
                raise ValueError("The saved digiKam database is unavailable.")
            pending = self.state.pending_apply_actions(run_id)
            counts = self.state.apply_counts(run_id)
            if any(item["action"]["target"] == "digikam" for item in pending):
                backup = self.state.apply_backup(run_id)
                if not backup:
                    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                    destination = self.settings.root / "backups" / f"digikam4-run-{run_id}-{timestamp}.db"
                    self.state.update_progress(run_id, "backing_up_digikam", counts["applied"], counts["total"], counts)
                    backup = str(create_sqlite_backup(database, destination))
                    self.state.set_apply_backup(run_id, backup)

            if any(item["action"]["target"] == "memories" for item in pending):
                user_id = str(settings["nc_user"])
                password = self.settings.password(user_id)
                if not password:
                    raise ValueError("The saved Nextcloud app password is unavailable.")
                backend = self.backend_factory(
                    str(settings["nextcloud_url"]), user_id, password, http_workers=4
                )
                requirements = backend.connection_requirements()
                if not requirements.ready:
                    raise ValueError("Nextcloud Recognize or the Face Sync companion app is unavailable.")

            writer = DigikamWriter(database)
            executor = ApplyExecutor(
                backend=backend,
                digikam=writer,
                nextcloud_photos_path=str(settings.get("nc_photos_path", "Photos")),
            )
            consecutive_failures = 0
            for item in pending:
                action = item["action"]
                try:
                    outcome = executor.execute(action)
                    self.state.finish_apply_action(item["id"], "applied", result=outcome)
                    link = executor.link_for(action, outcome)
                    if link is not None:
                        self.state.save_face_link(link)
                    consecutive_failures = 0
                except Exception as error:
                    LOG.exception("Apply action %s failed", item["id"])
                    self.state.finish_apply_action(item["id"], "failed", error=str(error))
                    if is_systemic_apply_failure(error):
                        consecutive_failures += 1
                    else:
                        consecutive_failures = 0
                counts = self.state.apply_counts(run_id)
                self.state.update_progress(
                    run_id,
                    "applying",
                    counts["applied"] + counts["failed"] + counts["ignored"],
                    counts["total"],
                    counts,
                )
                if consecutive_failures >= 3:
                    break

            counts = self.state.apply_counts(run_id)
            final_status = "applied" if not counts["failed"] and not counts["pending"] else "apply_failed"
            final = self.state.finish_apply(run_id, final_status)
            self.state.update_progress(
                run_id,
                "completed" if final_status == "applied" else "apply_failed",
                final["applied"] + final["failed"] + final["ignored"],
                final["total"],
                final,
                error=None if final_status == "applied" else "Some changes could not be applied.",
            )
            if final_status == "applied":
                message = (
                    f"{final['applied']} face changes completed."
                    if not final["ignored"]
                    else f"{final['applied']} face changes completed and {final['ignored']} kept in one library."
                )
                self.state.create_notification(
                    "apply.completed",
                    f"Updated {final['applied']} faces",
                    message,
                    f"/runs/{run_id}",
                    run_id=run_id,
                )
            else:
                self.state.create_notification(
                    "apply.failed",
                    f"{final['failed'] + final['pending']} changes need attention",
                    "Open Face Sync to retry the changes that did not finish.",
                    f"/runs/{run_id}",
                    run_id=run_id,
                )
        except Exception as error:
            LOG.exception("Apply %s failed", run_id)
            final = self.state.finish_apply(run_id, "apply_failed")
            self.state.update_progress(
                run_id, "apply_failed", final["applied"] + final["failed"] + final["ignored"],
                final["total"], final, error=str(error),
            )
            self.state.create_notification(
                "apply.failed", "Apply failed", "Open Face Sync for details.",
                f"/runs/{run_id}", run_id=run_id,
            )
        finally:
            if writer is not None:
                writer.close()
            if backend is not None:
                backend.close()
            with self._job_lock:
                self._jobs.pop(run_id, None)

    def resolve_conflict(
        self, run_id: int, conflict_id: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        resolution = str(payload.get("resolution", ""))
        apply_to_remaining = payload.get("apply_to_remaining") is True
        return self.state.resolve_conflict(
            run_id,
            conflict_id,
            resolution,
            apply_to_remaining=apply_to_remaining,
        )

    def failures(self, run_id: int) -> dict[str, Any]:
        run = self.state.run(run_id)
        if run is None or run["status"] != "apply_failed":
            raise ValueError("This run has no failed changes to review.")
        return self.state.failure_review(run_id)

    def resolve_failure(
        self, run_id: int, action_id: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        decision = str(payload.get("decision", ""))
        rect_value = payload.get("rect")
        rect = [float(value) for value in rect_value] if isinstance(rect_value, list) else None
        result = self.state.resolve_failed_action(
            run_id,
            action_id,
            decision,
            rect=rect,
            apply_to_remaining=payload.get("apply_to_remaining") is True,
        )
        if result["remaining"] == 0 and result["pending"] == 0:
            final = self.state.finish_apply(run_id, "applied")
            self.state.update_progress(
                run_id,
                "completed",
                final["applied"] + final["ignored"],
                final["total"],
                final,
            )
            self.state.create_notification(
                "apply.completed",
                "Face decisions saved",
                f"{final['applied']} changes completed and {final['ignored']} kept in one library.",
                f"/runs/{run_id}",
                run_id=run_id,
            )
            result["status"] = "applied"
        else:
            result["status"] = "apply_failed"
        return result

    def conflict_photo(self, run_id: int, conflict_id: int) -> tuple[bytes, str]:
        conflict = self.state.conflict_for_run(run_id, conflict_id)
        return self._review_photo(conflict, "conflict")

    def failure_photo(self, run_id: int, action_id: int) -> tuple[bytes, str]:
        failure = self.state.failed_apply_action(run_id, action_id)
        return self._review_photo(failure, "failed face")

    def _review_photo(self, item: dict[str, Any], description: str) -> tuple[bytes, str]:
        settings = self.settings.load()
        library_value = settings.get("digikam_library")
        if not library_value:
            raise ValueError("The digiKam library location is unavailable.")
        library = Path(str(library_value)).expanduser().resolve()
        photo = (library / str(item.get("path", ""))).resolve()
        try:
            photo.relative_to(library)
        except ValueError as error:
            raise ValueError(f"The {description} photo path is invalid.") from error
        if not photo.is_file():
            raise ValueError(f"The {description} photo is not available locally.")
        if photo.suffix.lower() not in BROWSER_IMAGE_EXTENSIONS:
            file_id = item.get("nc_file_id")
            user_id = str(settings.get("nc_user", ""))
            password = self.settings.password(user_id)
            if file_id is None or not user_id or not password:
                raise ValueError("A browser-compatible preview is unavailable.")
            return fetch_file_preview(
                str(settings["nextcloud_url"]),
                user_id,
                password,
                int(file_id),
            )
        content_type = mimetypes.guess_type(photo.name)[0] or "application/octet-stream"
        if not content_type.startswith("image/"):
            raise ValueError("The conflict file is not a supported image.")
        return photo.read_bytes(), content_type

    def preview(self, payload: dict[str, Any], *, run_id: int | None = None) -> dict[str, Any]:
        settings = self.settings.load()
        if not settings.get("digikam_db"):
            raise ValueError("Complete the connection setup first.")
        scope, person = self._preview_selection(payload)

        user_id = str(settings["nc_user"])
        password = self.settings.password(user_id)
        if not password:
            raise ValueError("The saved Nextcloud app password is unavailable.")
        if run_id is None:
            run_id = self.state.create_run(scope)
        backend = None
        try:
            backend = self.backend_factory(
                str(settings["nextcloud_url"]), user_id, password, http_workers=16
            )
            with self.digikam_factory(str(settings["digikam_db"])) as digikam:
                self.state.update_progress(run_id, "loading_memories", 0, 0)
                named_faces = backend.list_named_faces(
                    person or None,
                    progress_callback=lambda current: self.state.update_progress(
                        run_id,
                        "loading_memories",
                        current,
                        0,
                        {"loaded_faces": current},
                    ),
                )
                selected_faces = selected_memories_faces(
                    named_faces,
                    nextcloud_photos_path=str(settings.get("nc_photos_path", "Photos")),
                    only_person=person or None,
                )
                memories_files = len({relative.lower() for relative, _ in selected_faces})
                if person:
                    digikam_files = len(digikam.image_ids_for_person(person))
                else:
                    digikam_files = digikam.count_images_with_faces()
                total_work = digikam_files + memories_files

                def forward_progress(progress: dict[str, Any]) -> None:
                    detail = dict(progress)
                    detail["phase"] = "scanning_digikam"
                    detail["current"] = int(progress["current"])
                    detail["total"] = total_work
                    detail["created_in_digikam"] = 0
                    self.state.update_progress(
                        run_id,
                        "scanning_digikam",
                        int(progress["current"]),
                        total_work,
                        detail,
                    )

                report = self.sync_function(
                    digikam,
                    backend,
                    path_maps=[
                        (
                            str(settings["digikam_library"]),
                            str(settings.get("nc_photos_path", "Photos")),
                        )
                    ],
                    apply=False,
                    only_person=person or None,
                    insert_missing=True,
                    prefer_digikam_on_conflict=False,
                    batch_size=250,
                    max_actions=250000,
                    session=None,
                    progress_callback=forward_progress,
                )
                compare_memories_to_digikam(
                    digikam,
                    selected_faces,
                    report,
                    batch_size=250,
                    max_actions=250000,
                    progress_callback=lambda progress: self.state.update_progress(
                        run_id,
                        "scanning_memories",
                        digikam_files + int(progress["current"]),
                        total_work,
                        {
                            **progress,
                            "phase": "scanning_memories",
                            "current": digikam_files + int(progress["current"]),
                            "total": total_work,
                        },
                    ),
                )
            data = report.to_dict()
            actions, ignored_actions = self.state.filter_ignored_actions(data["actions"])
            data["actions"] = actions
            for action in ignored_actions:
                if action.get("action") == "insert":
                    data["summary"]["inserted"] -= 1
                elif action.get("action") == "create_digikam":
                    data["summary"]["created_in_digikam"] -= 1
            data["summary"]["ignored"] = len(ignored_actions)
            summary = data["summary"]
            result = {
                "run_id": run_id,
                "scope": scope,
                "person": person or None,
                "direction": "two_way",
                **data,
            }
            self.state.save_result(run_id, result)
            self.state.finish_run(run_id, "previewed", summary)
            self.state.update_progress(
                run_id, "completed", total_work, total_work, summary
            )
            self.state.save_conflicts(run_id, data["conflicts"])
            if summary["conflicts"]:
                self.state.create_notification(
                    "conflicts.created",
                    f"{summary['conflicts']} conflicts",
                    "Click to resolve the faces that have different names.",
                    f"/runs/{run_id}/conflicts",
                    run_id=run_id,
                )
            else:
                changes = (
                    summary["assigned"]
                    + summary["inserted"]
                    + summary["created_in_digikam"]
                )
                self.state.create_notification(
                    "run.completed" if changes else "run.no_changes",
                    "Preview complete",
                    f"{changes} face changes are ready to review.",
                    f"/runs/{run_id}",
                    run_id=run_id,
                )
            return result
        except Exception as error:
            self.state.finish_run(run_id, "failed", {})
            self.state.update_progress(run_id, "failed", 0, 0, error=str(error))
            self.state.create_notification(
                "run.failed",
                "Preview failed",
                "Open Face Sync for details.",
                f"/runs/{run_id}",
                run_id=run_id,
            )
            raise
        finally:
            if backend is not None:
                backend.close()
