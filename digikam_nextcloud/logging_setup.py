"""Logging configuration."""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


class _FlushStreamHandler(logging.StreamHandler):
    """StreamHandler that flushes after every record (visible progress under load)."""

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self.flush()


def setup_logging(verbose: bool = False) -> None:
    """Configure root + package loggers. Safe to call multiple times.

    Always prints timestamps so long phases (COUNT, PROPFIND, assign) show activity
    even without ``-v``. Verbose adds logger names and DEBUG detail.
    """
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    handler = _FlushStreamHandler(sys.stderr)
    handler.setLevel(level)
    if verbose:
        fmt = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    else:
        # Still show time + level so "what is it doing?" is answerable without -v
        fmt = "%(asctime)s %(levelname)s %(message)s"
    handler.setFormatter(logging.Formatter(fmt, datefmt="%H:%M:%S"))
    root.addHandler(handler)

    # Ensure package loggers are not filtered by a parent set higher
    for name in (
        "digikam_nextcloud",
        "digikam_nextcloud.sync",
        "digikam_nextcloud.digikam",
        "digikam_nextcloud.nextcloud_http",
        "digikam_nextcloud.nextcloud_db",
        "sync_faces",
    ):
        logging.getLogger(name).setLevel(level)


PACKAGE_LOGGERS = (
    "digikam_nextcloud",
    "digikam_nextcloud.sync",
    "digikam_nextcloud.digikam",
    "digikam_nextcloud.nextcloud_http",
    "digikam_nextcloud.nextcloud_db",
    "sync_faces",
)

LOG_FILE_BYTES = 5 * 1024 * 1024
LOG_FILE_COPIES = 5


def setup_service_logging(
    root: str | Path,
    state: Any,
    *,
    verbose: bool = False,
) -> list[logging.Handler]:
    """Install the background service's log destinations.

    Records go to a rotating file for support, and to the state database so
    the interface can show them without reading files. Returns the handlers so
    the caller can close them on shutdown.
    """
    from .log_store import SQLiteLogHandler

    level = logging.DEBUG if verbose else logging.INFO
    directory = Path(root) / "logs"
    directory.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger()
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)

    installed: list[logging.Handler] = []

    file_handler = RotatingFileHandler(
        directory / "face-sync.log",
        maxBytes=LOG_FILE_BYTES,
        backupCount=LOG_FILE_COPIES,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s [%(name)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        )
    )
    logger.addHandler(file_handler)
    installed.append(file_handler)

    database_handler = SQLiteLogHandler(state, level=level)
    database_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(database_handler)
    installed.append(database_handler)

    console = _FlushStreamHandler(sys.stderr)
    console.setLevel(level)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(console)
    installed.append(console)

    for name in PACKAGE_LOGGERS:
        logging.getLogger(name).setLevel(level)

    # urllib and http.server are noisy at DEBUG and say nothing a user needs.
    for name in ("urllib3", "http.server", "asyncio"):
        logging.getLogger(name).setLevel(logging.WARNING)

    return installed
