"""Open the Face Sync interface, starting the service first when needed.

This is what desktop shortcuts run. Opening the interface must never depend on
the user knowing whether a background service is already there.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

from .service import (
    read_service_info,
    run_service,
    service_is_live,
    service_url,
)

LOG = logging.getLogger(__name__)

START_TIMEOUT = 20.0
POLL_INTERVAL = 0.25


def service_command(config_dir: str | Path | None = None) -> list[str]:
    """The command that starts a service using this same installation."""
    command = [sys.executable, "-m", "digikam_nextcloud", "service"]
    if config_dir:
        command += ["--config-dir", str(config_dir)]
    return command


def start_service_detached(config_dir: str | Path | None = None) -> subprocess.Popen[bytes]:
    """Start a service that outlives this process."""
    kwargs: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        creation = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
        kwargs["creationflags"] = creation
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(service_command(config_dir), **kwargs)  # type: ignore[arg-type]


def wait_for_service(
    config_dir: str | Path | None = None, timeout: float = START_TIMEOUT
) -> dict | None:
    """Poll until the service publishes itself and answers, or time out."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = read_service_info(config_dir)
        if service_is_live(info):
            return info
        time.sleep(POLL_INTERVAL)
    return None


def open_ui(
    config_dir: str | Path | None = None,
    *,
    no_browser: bool = False,
    target: str = "",
) -> int:
    """Point a browser at the running service, starting one if necessary."""
    info = read_service_info(config_dir)
    if not service_is_live(info):
        start_service_detached(config_dir)
        info = wait_for_service(config_dir)
        if info is None:
            print(
                "Face Sync could not start. Run 'face-sync service' to see why.",
                file=sys.stderr,
            )
            return 1

    url = service_url(info)
    if target:
        url = url.rstrip("/") + "/#" + target.lstrip("#/")
    print(f"Face Sync is running at {url}")
    if not no_browser:
        webbrowser.open(url)
    return 0


def run_foreground(
    config_dir: str | Path | None = None,
    port: int | None = None,
    *,
    no_browser: bool = False,
    verbose: bool = False,
) -> int:
    """Run a service in this terminal and open the interface once it is up.

    This keeps the old ``face-sync`` behaviour for anyone who prefers a window
    they can close, rather than a service that starts at login.
    """
    import threading

    def opened(server: object) -> None:
        if no_browser:
            return
        url = f"http://127.0.0.1:{server.server_port}/"  # type: ignore[attr-defined]
        threading.Timer(0.2, webbrowser.open, args=(url,)).start()

    return run_service(config_dir, port, verbose=verbose, on_start=opened)
