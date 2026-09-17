"""Application-menu shortcuts that open the DigiMem interface.

The shortcut runs the launcher, not the service, so clicking it opens a window
whether or not a background service is already running.
"""
from __future__ import annotations

import logging
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

LOG = logging.getLogger(__name__)

APP_NAME = "DigiMem"
FILE_STEM = "digimem"
ICONS = Path(__file__).with_name("icons")


def icon_path(suffix: str) -> Path:
    """The packaged icon for one platform's idea of an icon file."""
    return ICONS / f"{FILE_STEM}{suffix}"


def _command(config_dir: str | Path | None = None) -> list[str]:
    command = [sys.executable, "-m", "digimem", "ui"]
    if config_dir:
        command += ["--config-dir", str(config_dir)]
    return command


def _quote(value: str) -> str:
    return '"' + value.replace('"', '\\"') + '"'


def _start_menu() -> Path:
    base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def shortcut_paths() -> list[Path]:
    """Everywhere a shortcut may be, the preferred shape first.

    Windows has two: a .lnk, which can carry an icon, and the .cmd that is
    written when there is no PowerShell to create one. Both are listed so that
    installing again replaces the other rather than leaving two menu entries.
    """
    if sys.platform == "win32":
        base = _start_menu()
        return [base / f"{APP_NAME}.lnk", base / f"{APP_NAME}.cmd"]
    if sys.platform == "darwin":
        return [Path.home() / "Applications" / f"{APP_NAME}.app"]
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return [base / "applications" / f"{FILE_STEM}.desktop"]


def shortcut_path() -> Path:
    return shortcut_paths()[0]


def _desktop_entry(config_dir: str | Path | None) -> str:
    executable = " ".join(_quote(part) for part in _command(config_dir))
    return f"""[Desktop Entry]
Type=Application
Name={APP_NAME}
GenericName=Photo face synchronisation
Comment=Keep digiKam and Nextcloud Memories faces in sync
Exec={executable}
Icon={icon_path(".png")}
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
    resources = bundle / "Contents" / "Resources"
    resources.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(icon_path(".icns"), resources / f"{FILE_STEM}.icns")
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
\t<key>CFBundleIconFile</key>
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


def _windows_executable() -> str:
    """Prefer the interpreter that opens no console window."""
    windowless = Path(sys.executable).with_name("pythonw.exe")
    return str(windowless) if windowless.is_file() else sys.executable


def _windows_arguments(config_dir: str | Path | None) -> str:
    parts = ["-m", "digimem", "ui"]
    if config_dir:
        parts += ["--config-dir", str(config_dir)]
    return " ".join(_quote(part) for part in parts)


def _ps_quote(value: str) -> str:
    """A PowerShell single-quoted string, where '' is a literal quote."""
    return "'" + str(value).replace("'", "''") + "'"


def shortcut_script(target: Path, config_dir: str | Path | None) -> str:
    """The PowerShell that writes the .lnk.

    A .lnk is the only Start Menu entry that can carry an icon, and creating
    one needs COM. PowerShell has COM and is already required on Windows for
    notifications, so this costs no new dependency.
    """
    return "; ".join([
        "$s = (New-Object -ComObject WScript.Shell)"
        f".CreateShortcut({_ps_quote(target)})",
        f"$s.TargetPath = {_ps_quote(_windows_executable())}",
        f"$s.Arguments = {_ps_quote(_windows_arguments(config_dir))}",
        f"$s.IconLocation = {_ps_quote(icon_path('.ico'))}",
        f"$s.Description = {_ps_quote('Keep digiKam and Nextcloud Memories faces in sync')}",
        "$s.Save()",
    ])


def _install_windows(config_dir: str | Path | None, path: Path) -> Path:
    """Write a .lnk, or a .cmd on a machine with no PowerShell.

    The fallback is searchable and clickable in the same way; it simply shows
    the batch-file icon, because nothing can attach one to a .cmd.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is not None:
        finished = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-Command",
             shortcut_script(path, config_dir)],
            capture_output=True, text=True, check=False, timeout=60,
        )
        if finished.returncode == 0 and path.is_file():
            return path
        LOG.warning(
            "Could not create the Start Menu shortcut (%s); falling back to a .cmd",
            (finished.stderr or "").strip() or finished.returncode,
        )

    fallback = path.with_suffix(".cmd")
    command = " ".join(
        [_quote(_windows_executable()), _windows_arguments(config_dir)]
    )
    fallback.write_text(
        "@echo off\r\nstart \"\" " + command + "\r\n", encoding="utf-8"
    )
    return fallback


def install(config_dir: str | Path | None = None) -> dict[str, Any]:
    """Create the shortcut for this platform and return where it went."""
    path = shortcut_path()
    if sys.platform == "win32":
        written = _install_windows(config_dir, path)
        # Whichever shape was not written is a leftover from before, and two
        # entries for one application is worse than none.
        _remove(*[other for other in shortcut_paths() if other != written])
        path = written
    elif sys.platform == "darwin":
        _install_macos(config_dir, path)
    else:
        _install_linux(config_dir, path)
    LOG.info("Installed the %s shortcut at %s", APP_NAME, path)
    return {"installed": True, "path": str(path)}


def _remove(*paths: Path) -> None:
    for path in paths:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)


def remove() -> dict[str, Any]:
    _remove(*shortcut_paths())
    return {"installed": False, "path": str(shortcut_path())}


def status() -> dict[str, Any]:
    for path in shortcut_paths():
        if path.exists():
            return {"installed": True, "path": str(path)}
    return {"installed": False, "path": str(shortcut_path())}
