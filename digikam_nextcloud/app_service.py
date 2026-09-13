"""UI-facing application operations, independent of HTTP presentation."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Callable

from .nextcloud_http import NextcloudHTTP
from .settings import SettingsStore
from .state_store import StateStore

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
        with sqlite3.connect(uri, uri=True) as connection:
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
    ):
        self.settings = settings
        self.state = state
        self.backend_factory = backend_factory

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
