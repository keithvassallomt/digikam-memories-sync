"""The long-running DigiMem process.

One service owns a configuration directory. It holds an exclusive lock so a
second copy cannot start, and publishes where it is listening so the launcher
and the command line can find it.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import socket
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .coordinator import Coordinator
from .digikam_writer import digikam_probe_supported
from .local_server import create_server
from .logging_setup import setup_service_logging
from .settings import SettingsStore

LOG = logging.getLogger(__name__)

LOCK_NAME = "service.lock"
INFO_NAME = "service.json"
DEFAULT_PORT = 47818
EXIT_ALREADY_RUNNING = 3


class ServiceLock:
    """An exclusive lock on one configuration directory.

    The lock is the authority on whether a service is running. A leftover
    ``service.json`` from a crash is therefore never mistaken for a live
    process, and no process id has to be trusted.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.handle: Any = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            if sys.platform == "win32":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()).encode())
        handle.flush()
        self.handle = handle
        return True

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self.handle.close()
            self.handle = None


def config_root(config_dir: str | Path | None = None) -> Path:
    return SettingsStore(Path(config_dir) if config_dir else None).root


def info_path(config_dir: str | Path | None = None) -> Path:
    return config_root(config_dir) / INFO_NAME


def read_service_info(config_dir: str | Path | None = None) -> dict[str, Any] | None:
    """Return the published connection details, or None when absent or unreadable."""
    path = info_path(config_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not data.get("port") or not data.get("token"):
        return None
    return data


def write_service_info(config_dir: str | Path | None, payload: dict[str, Any]) -> Path:
    path = info_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    temporary.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def remove_service_info(config_dir: str | Path | None = None) -> None:
    try:
        info_path(config_dir).unlink(missing_ok=True)
    except OSError:
        pass


def service_url(info: dict[str, Any]) -> str:
    return f"http://127.0.0.1:{int(info['port'])}/"


def service_is_live(info: dict[str, Any] | None, timeout: float = 1.5) -> bool:
    """Confirm that something is actually answering as DigiMem."""
    if not info:
        return False
    request = urllib.request.Request(
        service_url(info) + "api/health",
        headers={"X-DigiMem-Token": str(info.get("token", ""))},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read() or b"{}").get("status") == "ok"
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return False


def _port_is_free(port: int) -> bool:
    if port == 0:
        return True
    with socket.socket() as probe:
        # Match the server's own reuse behaviour so a socket in TIME_WAIT
        # does not read as occupied.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _bind(config_dir: Path | None, preferred: int) -> Any:
    """Bind the preferred port, falling back to any free one.

    The port is probed first so the server, and the state database it opens,
    are only constructed once.
    """
    if not _port_is_free(preferred):
        LOG.warning("Port %s is in use; listening on a free port instead", preferred)
        preferred = 0
    return create_server(config_dir, preferred)


def run_service(
    config_dir: str | Path | None = None,
    port: int | None = None,
    *,
    verbose: bool = False,
    once: bool = False,
    on_start: Callable[[Any], None] | None = None,
) -> int:
    """Run until stopped. Returns 3 when another service already holds the lock."""
    root = config_root(config_dir)
    root.mkdir(parents=True, exist_ok=True)
    lock = ServiceLock(root / LOCK_NAME)
    if not lock.acquire():
        existing = read_service_info(config_dir)
        if existing:
            print(f"DigiMem is already running at {service_url(existing)}", file=sys.stderr)
        else:
            print("DigiMem is already running.", file=sys.stderr)
        return EXIT_ALREADY_RUNNING

    directory = Path(config_dir) if config_dir else None
    settings = SettingsStore(directory)
    saved = settings.load().get("service")
    configured = saved.get("port") if isinstance(saved, dict) else None
    wanted = int(port if port is not None else configured or DEFAULT_PORT)
    server = None
    coordinator = None
    handlers: list[logging.Handler] = []
    try:
        server = _bind(directory, wanted)
        handlers = setup_service_logging(root, server.app.state, verbose=verbose)
        from . import __version__

        write_service_info(
            config_dir,
            {
                "pid": os.getpid(),
                "port": server.server_port,
                "token": server.api_token,
                "started_at": _now(),
                "version": __version__,
            },
        )
        LOG.info("DigiMem service listening on %s", service_url({"port": server.server_port}))
        if not digikam_probe_supported():
            LOG.warning(
                "This machine cannot tell whether digiKam is open, so runs will "
                "not wait for it. Install the desktop extra (psutil) to fix that."
            )

        def stop(signum: int, _frame: Any) -> None:
            LOG.info("Stopping on signal %s", signum)
            threading.Thread(target=server.shutdown, daemon=True).start()

        for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
            value = getattr(signal, name, None)
            if value is not None:
                try:
                    signal.signal(value, stop)
                except (ValueError, OSError):  # not the main thread, or unsupported
                    pass

        coordinator = Coordinator(server.app)
        if once:
            # One pass and out. Used by the smoke test, so a change that breaks
            # startup or the coordinator fails a build rather than a user.
            LOG.info("Running a single coordinator pass")
            coordinator.tick()
            LOG.info("Single pass complete")
            return 0
        coordinator.start()

        if on_start is not None:
            on_start(server)
        server.serve_forever(poll_interval=0.2)
        LOG.info("DigiMem service stopped")
        return 0
    finally:
        if coordinator is not None:
            coordinator.stop()
        remove_service_info(config_dir)
        for handler in handlers:
            try:
                handler.close()
            except Exception:
                pass
            logging.getLogger().removeHandler(handler)
        if server is not None:
            try:
                server.app.state.close()
            finally:
                server.server_close()
        lock.release()


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def free_port() -> int:
    """Return a port that is free right now. Used by tests."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
