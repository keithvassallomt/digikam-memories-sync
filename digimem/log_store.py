"""Log capture for the background service.

Two destinations are installed in service mode: a rotating file for support,
and the state database so the interface can show logs without reading files.

The database handler never writes on the calling thread. Records are queued
and drained by one worker, so a slow or locked database cannot stall a sync.
"""
from __future__ import annotations

import logging
import queue
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Iterator

# The run a log record belongs to. Set by the job runner, read by the handler,
# so callers never have to pass a run id into every logging call.
current_run_id: ContextVar[int | None] = ContextVar("current_run_id", default=None)

QUEUE_LIMIT = 10_000
BATCH_LIMIT = 200
DRAIN_INTERVAL = 0.5


@contextmanager
def run_context(run_id: int | None) -> Iterator[None]:
    """Stamp every record emitted inside this block with ``run_id``."""
    token = current_run_id.set(run_id)
    try:
        yield
    finally:
        current_run_id.reset(token)


def _timestamp(record: logging.LogRecord) -> str:
    moment = datetime.fromtimestamp(record.created, tz=timezone.utc)
    return moment.isoformat(timespec="milliseconds")


class SQLiteLogHandler(logging.Handler):
    """Queue log records and write them to the state database in batches."""

    def __init__(self, store: Any, *, level: int = logging.NOTSET):
        super().__init__(level)
        self.store = store
        self.queue: queue.Queue[tuple[str, str, str, int | None, str]] = queue.Queue(
            maxsize=QUEUE_LIMIT
        )
        self.dropped = 0
        self._pending = 0
        self._idle = threading.Condition()
        self._stop = threading.Event()
        self._worker = threading.Thread(
            target=self._drain_forever, name="digimem-log-writer", daemon=True
        )
        self._worker.start()

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(record, "skip_database", False):
            return
        try:
            row = (
                _timestamp(record),
                record.levelname,
                record.name,
                current_run_id.get(),
                self.format(record),
            )
        except Exception:  # pragma: no cover - formatting must never raise here
            self.handleError(record)
            return
        with self._idle:
            try:
                self.queue.put_nowait(row)
            except queue.Full:
                # Losing lines beats blocking a sync on the database.
                self.dropped += 1
                return
            self._pending += 1

    def _next_batch(self) -> list[tuple[str, str, str, int | None, str]]:
        try:
            first = self.queue.get(timeout=DRAIN_INTERVAL)
        except queue.Empty:
            return []
        batch = [first]
        while len(batch) < BATCH_LIMIT:
            try:
                batch.append(self.queue.get_nowait())
            except queue.Empty:
                break
        return batch

    def _drain_forever(self) -> None:
        while not self._stop.is_set():
            batch = self._next_batch()
            if batch:
                self._write(batch)
        # Flush whatever arrived between the stop signal and this point.
        while True:
            batch: list[tuple[str, str, str, int | None, str]] = []
            while len(batch) < BATCH_LIMIT:
                try:
                    batch.append(self.queue.get_nowait())
                except queue.Empty:
                    break
            if not batch:
                return
            self._write(batch)

    def _write(self, batch: list[tuple[str, str, str, int | None, str]]) -> None:
        try:
            self.store.append_logs(batch)
        except Exception:
            # A logging failure must never propagate into the code being logged.
            pass
        finally:
            with self._idle:
                self._pending -= len(batch)
                if self._pending <= 0:
                    self._idle.notify_all()

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until queued records are written. Returns False on timeout."""
        with self._idle:
            return self._idle.wait_for(lambda: self._pending <= 0, timeout=timeout)

    def close(self) -> None:
        self._stop.set()
        self._worker.join(timeout=3.0)
        super().close()

    def __enter__(self) -> "SQLiteLogHandler":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
