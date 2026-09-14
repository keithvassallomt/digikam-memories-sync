"""UI-facing application operations, independent of HTTP presentation."""
from __future__ import annotations

import os
import sqlite3
import threading
import logging
import mimetypes
from contextlib import closing
from pathlib import Path
from typing import Any, Callable

from .digikam import DigikamDB
from .nextcloud_http import NextcloudHTTP, fetch_file_preview
from .reverse import compare_memories_to_digikam, selected_memories_faces
from .settings import SettingsStore
from .state_store import StateStore
from .sync import sync

REQUIRED_DIGIKAM_TABLES = {"Images", "Tags", "TagProperties", "ImageTagProperties"}
BROWSER_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif"}
LOG = logging.getLogger(__name__)


class InvalidDigikamLibrary(ValueError):
    pass


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

    def conflicts(self, run_id: int) -> dict[str, Any]:
        return self.state.conflicts_for_run(run_id)

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

    def conflict_photo(self, run_id: int, conflict_id: int) -> tuple[bytes, str]:
        conflict = self.state.conflict_for_run(run_id, conflict_id)
        settings = self.settings.load()
        library_value = settings.get("digikam_library")
        if not library_value:
            raise ValueError("The digiKam library location is unavailable.")
        library = Path(str(library_value)).expanduser().resolve()
        photo = (library / str(conflict.get("path", ""))).resolve()
        try:
            photo.relative_to(library)
        except ValueError as error:
            raise ValueError("The conflict photo path is invalid.") from error
        if not photo.is_file():
            raise ValueError("The conflict photo is not available locally.")
        if photo.suffix.lower() not in BROWSER_IMAGE_EXTENSIONS:
            file_id = conflict.get("nc_file_id")
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
                    max_actions=5000,
                    session=None,
                    progress_callback=forward_progress,
                )
                compare_memories_to_digikam(
                    digikam,
                    selected_faces,
                    report,
                    batch_size=250,
                    max_actions=5000,
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
