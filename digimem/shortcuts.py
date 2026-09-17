"""Application-menu shortcuts that open the DigiMem interface.

The shortcut runs the launcher, not the service, so clicking it opens a window
whether or not a background service is already running.
"""
from __future__ import annotations

import logging
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

LOG = logging.getLogger(__name__)

APP_NAME = "DigiMem"
FILE_STEM = "digimem"


def _command(config_dir: str | Path | None = None) -> list[str]:
    command = [sys.executable, "-m", "digimem", "ui"]
    if config_dir:
        command += ["--config-dir", str(config_dir)]
    return command


def _quote(value: str) -> str:
    return '"' + value.replace('"', '\\"') + '"'


def shortcut_path() -> Path:
    if sys.platform == "win32":
        base = Path(
            os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")
        ) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        return base / f"{APP_NAME}.cmd"
    if sys.platform == "darwin":
        return Path.home() / "Applications" / f"{APP_NAME}.app"
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "applications" / f"{FILE_STEM}.desktop"


def _desktop_entry(config_dir: str | Path | None) -> str:
    executable = " ".join(_quote(part) for part in _command(config_dir))
    return f"""[Desktop Entry]
Type=Application
Name={APP_NAME}
GenericName=Photo face synchronisation
Comment=Keep digiKam and Nextcloud Memories faces in sync
Exec={executable}
Terminal=false
Categories=Graphics;Photography;
Keywords=digikam;nextcloud;memories;faces;
StartupNotify=false
"""


def _install_linux(config_dir: str | Path | None, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_desktop_entry(config_dir), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    # Best effort: a missing or failing update is not a problem worth reporting.
    subprocess.run(
        ["update-desktop-database", str(path.parent)],
        capture_output=True,
        check=False,
        timeout=20,
    )


def _install_macos(config_dir: str | Path | None, bundle: Path) -> None:
    binaries = bundle / "Contents" / "MacOS"
    binaries.mkdir(parents=True, exist_ok=True)
    arguments = " ".join(_quote(part) for part in _command(config_dir))
    runner = binaries / FILE_STEM
    runner.write_text(f"#!/bin/sh\nexec {arguments}\n", encoding="utf-8")
    runner.chmod(runner.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    (bundle / "Contents" / "Info.plist").write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
\t<key>CFBundleName</key>
\t<string>{APP_NAME}</string>
\t<key>CFBundleDisplayName</key>
\t<string>{APP_NAME}</string>
\t<key>CFBundleIdentifier</key>
\t<string>com.keithvassallo.digimem</string>
\t<key>CFBundleExecutable</key>
\t<string>{FILE_STEM}</string>
\t<key>CFBundlePackageType</key>
\t<string>APPL</string>
\t<key>LSUIElement</key>
\t<false/>
</dict>
</plist>
""",
        encoding="utf-8",
    )


def _install_windows(config_dir: str | Path | None, path: Path) -> None:
    # A .lnk needs COM, which would mean a new dependency. A .cmd in the Start
    # Menu is searchable and clickable in exactly the same way.
    path.parent.mkdir(parents=True, exist_ok=True)
    executable = sys.executable
    windowless = Path(executable).with_name("pythonw.exe")
    if windowless.is_file():
        executable = str(windowless)
    parts = [executable, "-m", "digimem", "ui"]
    if config_dir:
        parts += ["--config-dir", str(config_dir)]
    path.write_text(
        "@echo off\r\nstart \"\" " + " ".join(_quote(part) for part in parts) + "\r\n",
        encoding="utf-8",
    )


def install(config_dir: str | Path | None = None) -> dict[str, Any]:
    """Create the shortcut for this platform and return where it went."""
    path = shortcut_path()
    if sys.platform == "win32":
        _install_windows(config_dir, path)
    elif sys.platform == "darwin":
        _install_macos(config_dir, path)
    else:
        _install_linux(config_dir, path)
    LOG.info("Installed the %s shortcut at %s", APP_NAME, path)
    return {"installed": True, "path": str(path)}


def remove() -> dict[str, Any]:
    path = shortcut_path()
    if path.is_dir():
        import shutil

        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)
    return {"installed": False, "path": str(path)}


def status() -> dict[str, Any]:
    path = shortcut_path()
    return {"installed": path.exists(), "path": str(path)}
