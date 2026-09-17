"""Persistent desktop settings and credentials."""
from __future__ import annotations

import copy
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

APP_ID = "digimem"
KEYRING_SERVICE = "DigiMem"

# What the application was called before it was DigiMem. Both are read once, so
# an existing installation keeps its settings, its history and its saved
# password rather than looking like a fresh one.
LEGACY_APP_ID = "digikam-memories-sync"
LEGACY_KEYRING_SERVICE = "digiKam Memories DigiMem"
SETTINGS_VERSION = 2
DEFAULT_PORT = 47818

LOG = logging.getLogger(__name__)

# Groups added in version 2. Automation stays off until somebody turns it on,
# so upgrading an existing installation never starts changing libraries by
# itself.
DEFAULTS: dict[str, Any] = {
    "sync": {
        # "ask", "digikam" or "memories". Asking is the default because
        # trusting a library rewrites names in bulk with no review step.
        "conflict_policy": "ask",
        "create_in_memories": True,
        "create_in_digikam": True,
    },
    "automation": {
        "enabled": False,
        "apply_automatically": True,
        "quiet_period_minutes": 10,
        "check_interval_minutes": 5,
        "fallback_interval_hours": 24,
    },
    "notifications": {
        "decisions": True,
        "connection": True,
        "completed": False,
    },
    "service": {
        "autostart": False,
        "port": DEFAULT_PORT,
    },
    "retention": {
        "backups_keep": 10,
        "log_days": 30,
    },
}

LIBRARY_KEYS = (
    "digikam_library",
    "digikam_db",
    "nextcloud_url",
    "nc_user",
    "nc_photos_path",
)


def _config_base() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


def adopt_legacy_config_dir() -> Path | None:
    """Move an installation made under the old name, once.

    Everything that matters lives in this directory: the settings, the state
    database, the saved credentials and the digiKam backups. Starting under a
    new name without it would look like a fresh install and would lose the
    history that stops faces being re-proposed.
    """
    base = _config_base()
    current, legacy = base / APP_ID, base / LEGACY_APP_ID
    if current.exists() or not legacy.is_dir():
        return None
    try:
        current.parent.mkdir(parents=True, exist_ok=True)
        legacy.rename(current)
    except OSError:
        LOG.warning("Could not move %s to %s; starting fresh", legacy, current)
        return None
    LOG.info("Adopted the settings and history from %s", legacy)
    return current


def default_config_dir() -> Path:
    return _config_base() / APP_ID


def _merge_defaults(settings: dict[str, Any]) -> dict[str, Any]:
    """Fill in missing groups and keys without touching what is already set."""
    merged = copy.deepcopy(settings)
    for group, values in DEFAULTS.items():
        current = merged.get(group)
        if not isinstance(current, dict):
            merged[group] = copy.deepcopy(values)
            continue
        for key, value in values.items():
            current.setdefault(key, value)
    return merged


def migrate(settings: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Bring a settings file up to the current version.

    Returns the settings and whether anything changed, so the caller can
    decide whether a write is needed.
    """
    version = int(settings.get("version") or 1)
    upgraded = _merge_defaults(settings)
    upgraded["version"] = SETTINGS_VERSION
    changed = upgraded != settings
    if changed and version < SETTINGS_VERSION:
        LOG.info("Upgraded settings from version %s to %s", version, SETTINGS_VERSION)
    return upgraded, changed


class SettingsStore:
    def __init__(self, root: Path | None = None, *, use_keyring: bool = True):
        self.root = Path(root) if root else default_config_dir()
        self.settings_path = self.root / "settings.json"
        self.secrets_path = self.root / "credentials.json"
        self.use_keyring = use_keyring

    # ------------------------------------------------------------------ read

    def raw(self) -> dict[str, Any]:
        """The file exactly as stored, with no defaults applied."""
        if not self.settings_path.is_file():
            return {}
        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            LOG.exception("The settings file could not be read")
            return {}
        return data if isinstance(data, dict) else {}

    def load(self) -> dict[str, Any]:
        """Settings with every group present, migrating the file if needed."""
        stored = self.raw()
        if not stored:
            return _merge_defaults({"version": SETTINGS_VERSION})
        settings, changed = migrate(stored)
        if changed:
            try:
                self._write_private_json(self.settings_path, settings)
            except OSError:
                LOG.exception("The upgraded settings could not be saved")
        return settings

    def is_configured(self) -> bool:
        """True when a library and an account have been set up and kept."""
        settings = self.load()
        return bool(
            settings.get("digikam_db")
            and settings.get("nextcloud_url")
            and settings.get("nc_user")
            and self.password(str(settings.get("nc_user", "")))
        )

    def public_settings(self) -> dict[str, Any]:
        settings = self.load()
        settings["has_password"] = bool(self.password(str(settings.get("nc_user", ""))))
        settings["config_dir"] = str(self.root)
        return settings

    # ----------------------------------------------------------------- write

    def save(self, settings: dict[str, Any], password: str | None = None) -> None:
        """Replace the library settings, keeping every other group intact."""
        merged = self.load()
        public = dict(settings)
        public.pop("password", None)
        merged.update(public)
        merged["version"] = SETTINGS_VERSION
        self._persist(merged)
        if password:
            self._save_password(str(merged.get("nc_user", "")), password)

    def update_group(self, group: str, values: dict[str, Any]) -> dict[str, Any]:
        """Merge values into one settings group and save."""
        if group not in DEFAULTS:
            raise ValueError(f"Unknown settings group: {group}")
        settings = self.load()
        current = dict(settings.get(group) or {})
        current.update(values)
        settings[group] = current
        self._persist(settings)
        return settings

    def _persist(self, settings: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._write_private_json(self.settings_path, settings)

    # ------------------------------------------------------------- passwords

    def password(self, user_id: str) -> str:
        if not user_id:
            return ""
        if self.use_keyring:
            try:
                import keyring

                value = keyring.get_password(KEYRING_SERVICE, user_id)
                if value:
                    return value
                value = keyring.get_password(LEGACY_KEYRING_SERVICE, user_id)
                if value:
                    # Carry it over, so this only happens once.
                    keyring.set_password(KEYRING_SERVICE, user_id, value)
                    return value
            except Exception:
                pass
        if self.secrets_path.is_file():
            try:
                data = json.loads(self.secrets_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return ""
            return str(data.get(user_id, ""))
        return ""

    def _save_password(self, user_id: str, password: str) -> None:
        if self.use_keyring:
            try:
                import keyring

                keyring.set_password(KEYRING_SERVICE, user_id, password)
                return
            except Exception:
                pass
        existing: dict[str, str] = {}
        if self.secrets_path.is_file():
            try:
                existing = json.loads(self.secrets_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = {}
        existing[user_id] = password
        self._write_private_json(self.secrets_path, existing)

    @staticmethod
    def _write_private_json(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        temporary.replace(path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
