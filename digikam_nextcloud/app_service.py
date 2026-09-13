"""UI-facing application operations, independent of HTTP presentation."""
from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Callable

from .digikam import DigikamDB
from .nextcloud_http import NextcloudHTTP
from .settings import SettingsStore
from .state_store import StateStore
from .sync import sync

REQUIRED_DIGIKAM_TABLES = {"Images", "Tags", "TagProperties", "ImageTagProperties"}


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
        with self.digikam_factory(database) as digikam:
            return sorted(set(digikam.person_tag_ids().values()), key=str.casefold)

    def preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        settings = self.settings.load()
        if not settings.get("digikam_db"):
            raise ValueError("Complete the connection setup first.")
        scope = str(payload.get("scope", "person"))
        if scope not in {"all", "person"}:
            raise ValueError("Choose All faces or One person.")
        person = str(payload.get("person", "")).strip() if scope == "person" else ""
        if scope == "person" and not person:
            raise ValueError("Choose a person.")

        user_id = str(settings["nc_user"])
        password = self.settings.password(user_id)
        if not password:
            raise ValueError("The saved Nextcloud app password is unavailable.")
        run_id = self.state.create_run(scope)
        backend = None
        try:
            backend = self.backend_factory(
                str(settings["nextcloud_url"]), user_id, password, http_workers=16
            )
            with self.digikam_factory(str(settings["digikam_db"])) as digikam:
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
                )
            data = report.to_dict()
            summary = data["summary"]
            self.state.finish_run(run_id, "previewed", summary)
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
                changes = summary["assigned"] + summary["inserted"]
                self.state.create_notification(
                    "run.completed" if changes else "run.no_changes",
                    "Preview complete",
                    f"{changes} face changes are ready to review.",
                    f"/runs/{run_id}",
                    run_id=run_id,
                )
            return {
                "run_id": run_id,
                "scope": scope,
                "person": person or None,
                "direction": "digikam_to_memories",
                **data,
            }
        except Exception:
            self.state.finish_run(run_id, "failed", {})
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
