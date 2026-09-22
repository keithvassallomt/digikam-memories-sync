"""Start DigiMem when the user logs in.

Each platform gets the mechanism a user would expect there. The unit or entry
always names the interpreter that installed it, so a virtual environment keeps
working.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import relaunch
from .service import EXIT_ALREADY_RUNNING

LOG = logging.getLogger(__name__)

SERVICE_NAME = "digimem"
LAUNCH_AGENT_ID = "com.keithvassallo.digimem"
WINDOWS_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
WINDOWS_VALUE = "DigiMem"


def _command(config_dir: str | Path | None = None) -> list[str]:
    return relaunch.command("service", config_dir)


def _quote(value: str) -> str:
    return '"' + value.replace('"', '\\"') + '"'


def _has_systemd() -> bool:
    if not shutil.which("systemctl"):
        return False
    if not os.environ.get("XDG_RUNTIME_DIR"):
        return False
    return Path("/run/systemd/system").exists()


def mechanism() -> str:
    """Which autostart mechanism applies on this machine."""
    if sys.platform == "win32":
        return "registry"
    if sys.platform == "darwin":
        return "launchd"
    return "systemd" if _has_systemd() else "xdg"


# --------------------------------------------------------------------- paths

def systemd_unit_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "systemd" / "user" / f"{SERVICE_NAME}.service"


def xdg_autostart_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "autostart" / f"{SERVICE_NAME}.desktop"


def launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_ID}.plist"


def entry_path() -> Path | None:
    kind = mechanism()
    if kind == "systemd":
        return systemd_unit_path()
    if kind == "xdg":
        return xdg_autostart_path()
    if kind == "launchd":
        return launch_agent_path()
    return None


# ------------------------------------------------------------------ contents

def _systemd_unit(config_dir: str | Path | None) -> str:
    executable = " ".join(_quote(part) for part in _command(config_dir))
    # systemd starts a service in the home directory, which is the one place
    # `-m digimem` cannot find a source checkout from. Without this the unit
    # exits 1 on every start and Restart=on-failure turns that into a loop.
    root = relaunch.working_directory()
    directory = f"\nWorkingDirectory={root}" if root else ""
    return f"""[Unit]
Description=DigiMem between digiKam and Nextcloud Memories
After=graphical-session.target

[Service]
Type=simple
ExecStart={executable}{directory}
# Exit 3 is "a service is already running", which is the normal outcome when
# the interface was opened before the login item ran. It is a success as far as
# a login item is concerned; without this, Restart=on-failure retries it every
# ten seconds for the rest of the session.
SuccessExitStatus={EXIT_ALREADY_RUNNING}
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
"""


def _xdg_desktop(config_dir: str | Path | None) -> str:
    executable = " ".join(_quote(part) for part in _command(config_dir))
    root = relaunch.working_directory()
    directory = f"\nPath={root}" if root else ""
    return f"""[Desktop Entry]
Type=Application
Name=DigiMem
Comment=Keep digiKam and Nextcloud Memories faces in sync
Exec={executable}{directory}
Terminal=false
X-GNOME-Autostart-enabled=true
"""


def _launch_agent(config_dir: str | Path | None) -> str:
    arguments = "".join(f"\t\t<string>{part}</string>\n" for part in _command(config_dir))
    root = relaunch.working_directory()
    directory = (
        f"\t<key>WorkingDirectory</key>\n\t<string>{root}</string>\n" if root else ""
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
\t<key>Label</key>
\t<string>{LAUNCH_AGENT_ID}</string>
\t<key>ProgramArguments</key>
\t<array>
{arguments}\t</array>
{directory}\t<key>RunAtLoad</key>
\t<true/>
\t<key>KeepAlive</key>
\t<dict>
\t\t<key>SuccessfulExit</key>
\t\t<false/>
\t</dict>
</dict>
</plist>
"""


# ------------------------------------------------------------------- actions

def _systemctl(*arguments: str) -> tuple[int, str]:
    try:
        finished = subprocess.run(
            ["systemctl", "--user", *arguments],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return 1, str(error)
    return finished.returncode, (finished.stderr or finished.stdout or "").strip()


def supervisor() -> tuple[str, str] | None:
    """The login supervisor that is running this very process, if one is.

    A supervised service that stops itself and starts its own replacement
    leaves the supervisor believing the job is down: it would not bring it back
    after a later crash, and its status would contradict what is really
    running. Knowing who is in charge is what lets a restart ask them instead.
    """
    if sys.platform == "darwin":
        # launchd names the job it started in the process's own environment.
        label = os.environ.get("XPC_SERVICE_NAME", "")
        return ("launchd", LAUNCH_AGENT_ID) if label == LAUNCH_AGENT_ID else None
    if sys.platform == "win32" or not _has_systemd():
        # The registry and an XDG entry start a process and then forget it.
        return None
    unit = f"{SERVICE_NAME}.service"
    code, answer = _systemctl("show", "-p", "MainPID", "--value", unit)
    if code != 0:
        return None
    try:
        main_pid = int(answer.strip() or 0)
    except ValueError:
        return None
    return ("systemd", unit) if main_pid == os.getpid() else None


def restart_supervised(found: tuple[str, str]) -> None:
    """Ask the supervisor to restart the job this process is.

    Neither call waits. The request is with the supervisor the moment it is
    made, and this process is about to be stopped to carry it out, so waiting
    would only mean waiting to be killed.
    """
    kind, name = found
    if kind == "systemd":
        code, answer = _systemctl("restart", "--no-block", name)
        if code != 0:
            raise RuntimeError(f"systemd refused the restart: {answer}")
        return
    try:
        finished = subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{name}"],
            capture_output=True, text=True, check=False, timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"launchd could not be reached: {error}") from error
    if finished.returncode != 0:
        raise RuntimeError(
            f"launchd refused the restart: {(finished.stderr or '').strip()}"
        )


def enable(config_dir: str | Path | None = None, *, start: bool = True) -> dict[str, Any]:
    """Install the login entry. Returns what was written and whether it started."""
    kind = mechanism()
    if kind == "registry":
        return _enable_registry(config_dir)

    path = entry_path()
    assert path is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "systemd":
        path.write_text(_systemd_unit(config_dir), encoding="utf-8")
        _systemctl("daemon-reload")
        code, message = _systemctl("enable", f"{SERVICE_NAME}.service")
        if code != 0:
            raise RuntimeError(f"systemd refused to enable the service: {message}")
        if start:
            _systemctl("start", f"{SERVICE_NAME}.service")
    elif kind == "xdg":
        path.write_text(_xdg_desktop(config_dir), encoding="utf-8")
    else:
        path.write_text(_launch_agent(config_dir), encoding="utf-8")
        subprocess.run(
            ["launchctl", "unload", str(path)], capture_output=True, check=False, timeout=20
        )
        finished = subprocess.run(
            ["launchctl", "load", str(path)], capture_output=True, text=True, check=False, timeout=20
        )
        if finished.returncode != 0:
            raise RuntimeError(
                f"launchctl refused to load the agent: {(finished.stderr or '').strip()}"
            )
    return {"enabled": True, "mechanism": kind, "path": str(path)}


def disable(config_dir: str | Path | None = None) -> dict[str, Any]:
    """Remove the login entry. Leaves any running service alone."""
    kind = mechanism()
    if kind == "registry":
        return _disable_registry()

    path = entry_path()
    assert path is not None
    if kind == "systemd":
        _systemctl("disable", f"{SERVICE_NAME}.service")
        path.unlink(missing_ok=True)
        _systemctl("daemon-reload")
    elif kind == "launchd":
        subprocess.run(
            ["launchctl", "unload", str(path)], capture_output=True, check=False, timeout=20
        )
        path.unlink(missing_ok=True)
    else:
        path.unlink(missing_ok=True)
    return {"enabled": False, "mechanism": kind, "path": str(path)}


def status() -> dict[str, Any]:
    kind = mechanism()
    if kind == "registry":
        return _status_registry()
    path = entry_path()
    return {
        "supported": True,
        "mechanism": kind,
        "path": str(path) if path else None,
        "enabled": bool(path and path.is_file()),
    }


# ------------------------------------------------------------------- Windows

def _open_run_key(write: bool):  # type: ignore[no-untyped-def]
    import winreg  # type: ignore[import-not-found]

    access = winreg.KEY_SET_VALUE if write else winreg.KEY_READ
    return winreg.OpenKey(winreg.HKEY_CURRENT_USER, WINDOWS_RUN_KEY, 0, access)


def _windows_command(config_dir: str | Path | None) -> str:
    # A login item wants the form that opens no console window.
    parts = relaunch.command("service", config_dir, windowless=True)
    command = " ".join(_quote(part) for part in parts)
    root = relaunch.working_directory()
    if root is None:
        return command
    # A Run value is a command line with no working directory of its own, so a
    # source checkout has to be moved into first. Only a checkout needs this;
    # an installed or frozen DigiMem finds itself without help.
    return f'cmd /c cd /d {_quote(str(root))} && {command}'


def _enable_registry(config_dir: str | Path | None) -> dict[str, Any]:
    import winreg  # type: ignore[import-not-found]

    with _open_run_key(write=True) as key:
        winreg.SetValueEx(key, WINDOWS_VALUE, 0, winreg.REG_SZ, _windows_command(config_dir))
    return {"enabled": True, "mechanism": "registry", "path": f"HKCU\\{WINDOWS_RUN_KEY}"}


def _disable_registry() -> dict[str, Any]:
    import winreg  # type: ignore[import-not-found]

    try:
        with _open_run_key(write=True) as key:
            winreg.DeleteValue(key, WINDOWS_VALUE)
    except FileNotFoundError:
        pass
    return {"enabled": False, "mechanism": "registry", "path": f"HKCU\\{WINDOWS_RUN_KEY}"}


def _status_registry() -> dict[str, Any]:
    import winreg  # type: ignore[import-not-found]

    enabled = False
    try:
        with _open_run_key(write=False) as key:
            winreg.QueryValueEx(key, WINDOWS_VALUE)
            enabled = True
    except FileNotFoundError:
        enabled = False
    except OSError:
        enabled = False
    return {
        "supported": True,
        "mechanism": "registry",
        "path": f"HKCU\\{WINDOWS_RUN_KEY}",
        "enabled": enabled,
    }
