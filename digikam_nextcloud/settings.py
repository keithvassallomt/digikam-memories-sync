"""Persistent desktop settings and credentials."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

APP_ID = "digikam-memories-sync"
KEYRING_SERVICE = "digiKam Memories Face Sync"


def default_config_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / APP_ID


class SettingsStore:
    def __init__(self, root: Path | None = None, *, use_keyring: bool = True):
        self.root = Path(root) if root else default_config_dir()
        self.settings_path = self.root / "settings.json"
        self.secrets_path = self.root / "credentials.json"
        self.use_keyring = use_keyring

    def load(self) -> dict[str, Any]:
        if not self.settings_path.is_file():
            return {}
        data = json.loads(self.settings_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}

    def save(self, settings: dict[str, Any], password: str | None = None) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        public = dict(settings)
        public.pop("password", None)
        self._write_private_json(self.settings_path, public)
        if password:
            self._save_password(str(public.get("nc_user", "")), password)

    def password(self, user_id: str) -> str:
        if self.use_keyring:
            try:
                import keyring

                value = keyring.get_password(KEYRING_SERVICE, user_id)
                if value:
                    return value
            except Exception:
                pass
        if self.secrets_path.is_file():
            data = json.loads(self.secrets_path.read_text(encoding="utf-8"))
            return str(data.get(user_id, ""))
        return ""

    def public_settings(self) -> dict[str, Any]:
        settings = self.load()
        settings["has_password"] = bool(self.password(str(settings.get("nc_user", ""))))
        return settings

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
            existing = json.loads(self.secrets_path.read_text(encoding="utf-8"))
        existing[user_id] = password
        self._write_private_json(self.secrets_path, existing)

    @staticmethod
    def _write_private_json(path: Path, data: dict[str, Any]) -> None:
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
