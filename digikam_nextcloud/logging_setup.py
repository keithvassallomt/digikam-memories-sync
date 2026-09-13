"""Logging configuration."""
from __future__ import annotations

import logging
import sys


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
