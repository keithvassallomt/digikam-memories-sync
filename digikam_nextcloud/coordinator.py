"""Deciding when work may run, and picking it back up when it may.

Everything that can stop a sync is a gate: someone paused it, digiKam is open,
Nextcloud is unreachable, another job is already going. The decision itself is
a plain function over a snapshot, so every rule can be tested without a thread,
a clock or a network.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .triggers import TriggerWatcher

LOG = logging.getLogger(__name__)

TICK_SECONDS = 10.0
# Housekeeping is not urgent; once a day is plenty.
RETENTION_INTERVAL_HOURS = 24
LAST_RETENTION_AT = "last_retention_at"
# How long after digiKam disappears before its database is treated as free.
DIGIKAM_SETTLE_SECONDS = 15.0
# A wall-clock jump larger than this, with no matching monotonic time, is sleep.
SLEEP_THRESHOLD_SECONDS = 60.0
# Waits between attempts after a connection failure.
BACKOFF_MINUTES = (1, 2, 5, 15, 30, 60)

# Why a run is not starting. These reach the user as sentences, elsewhere.
WAITING_DIGIKAM = "digikam"
WAITING_CONNECTION = "connection"
WAITING_PAUSED = "paused"
WAITING_JOB = "job_running"
WAITING_ATTEMPT = "backoff"
WAITING_RESUME = "resume"
WAITING_RECOGNIZE = "recognize"


def backoff_delay(attempts: int) -> timedelta:
    """How long to wait before the next attempt, levelling off at an hour."""
    index = max(0, min(int(attempts), len(BACKOFF_MINUTES) - 1))
    return timedelta(minutes=BACKOFF_MINUTES[index])


def next_attempt_at(attempts: int, now: datetime | None = None) -> str:
    moment = (now or datetime.now(timezone.utc)) + backoff_delay(attempts)
    return moment.isoformat(timespec="seconds")


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


@dataclass
class Snapshot:
    """Everything the decision needs, gathered once per tick."""

    now: datetime
    paused: bool = False
    job_running: bool = False
    digikam_running: bool = True
    digikam_free_since: datetime | None = None
    connection_ready: bool = True


@dataclass
class Decision:
    run_id: int | None = None
    reason: str = ""
    blocked: dict[int, str] = field(default_factory=dict)

    @property
    def should_start(self) -> bool:
        return self.run_id is not None


def digikam_gate_open(snapshot: Snapshot) -> bool:
    """digiKam writes need it closed, and settled for a moment afterwards."""
    if snapshot.digikam_running:
        return False
    if snapshot.digikam_free_since is None:
        return False
    waited = (snapshot.now - snapshot.digikam_free_since).total_seconds()
    return waited >= DIGIKAM_SETTLE_SECONDS


def blocking_reason(run: dict[str, Any], snapshot: Snapshot) -> str:
    """Why this run cannot start right now, or an empty string if it can."""
    if snapshot.paused:
        return WAITING_PAUSED
    scheduled = _parse(run.get("next_attempt_at"))
    if scheduled is not None and scheduled > snapshot.now:
        return WAITING_ATTEMPT
    reason = str(run.get("waiting_reason") or "")
    if run.get("status") == "deferred" or reason == WAITING_DIGIKAM:
        return "" if digikam_gate_open(snapshot) else WAITING_DIGIKAM
    if reason == WAITING_CONNECTION and not snapshot.connection_ready:
        return WAITING_CONNECTION
    return ""


def choose(runs: list[dict[str, Any]], snapshot: Snapshot) -> Decision:
    """Pick at most one run to continue, oldest first."""
    if snapshot.job_running:
        return Decision(blocked={int(run["id"]): WAITING_JOB for run in runs})
    blocked: dict[int, str] = {}
    for run in sorted(runs, key=lambda item: int(item["id"])):
        reason = blocking_reason(run, snapshot)
        if reason:
            blocked[int(run["id"])] = reason
            continue
        return Decision(
            run_id=int(run["id"]),
            reason=str(run.get("waiting_reason") or run.get("status") or ""),
            blocked=blocked,
        )
    return Decision(blocked=blocked)


class Coordinator:
    """The thread that applies those rules on a timer."""

    def __init__(
        self,
        app: Any,
        *,
        interval: float = TICK_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.app = app
        self.interval = interval
        self.monotonic = monotonic
        self.wall = wall
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._digikam_free_since: datetime | None = None
        self._last_monotonic = monotonic()
        self._last_wall = wall()
        self.watcher = TriggerWatcher(app)
        self.slept = False
        self.ticks = 0
        self.started_by_trigger: list[str] = []
        self.notified = 0

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="face-sync-coordinator", daemon=True
        )
        self._thread.start()
        LOG.info("Coordinator started")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
            LOG.info("Coordinator stopped")

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception:
                LOG.exception("The coordinator tick failed")

    # ----------------------------------------------------------------- work

    def detect_sleep(self) -> bool:
        """A wall clock that moved further than the monotonic one means sleep."""
        monotonic_now = self.monotonic()
        wall_now = self.wall()
        elapsed = monotonic_now - self._last_monotonic
        drifted = (wall_now - self._last_wall).total_seconds()
        self._last_monotonic = monotonic_now
        self._last_wall = wall_now
        return (drifted - elapsed) > SLEEP_THRESHOLD_SECONDS

    def snapshot(self) -> Snapshot:
        now = self.wall()
        running = self.app.digikam_running()
        if running:
            self._digikam_free_since = None
        elif self._digikam_free_since is None:
            self._digikam_free_since = now
        paused, _ = self.app._paused_until()
        return Snapshot(
            now=now,
            paused=paused,
            job_running=self.app.active_run_id() is not None,
            digikam_running=running,
            digikam_free_since=self._digikam_free_since,
            connection_ready=True,
        )

    def tick(self) -> Decision:
        """One pass: notice a sleep, then continue whatever may continue."""
        self.ticks += 1
        if self.detect_sleep():
            self.slept = True
            LOG.info("Resumed after sleep; rechecking before continuing")
            self.app.invalidate_probes()

        # Anything nobody has been told about yet, when no window is open.
        try:
            self.notified += self.app.deliver_desktop_notifications()
        except Exception:
            LOG.exception("Could not show desktop notifications")
        self.run_housekeeping()

        snapshot = self.snapshot()
        runs = self.app.state.runs_awaiting_work()
        decision = choose(runs, snapshot) if runs else Decision()
        if decision.should_start:
            LOG.info("Continuing run %s", decision.run_id)
            try:
                self.app.resume_run(decision.run_id)
            except Exception:
                LOG.exception("Could not continue run %s", decision.run_id)
            return decision

        # Work already under way comes first. Only when nothing is waiting does
        # the coordinator consider starting something new.
        if not snapshot.job_running:
            if self.consider_auto_apply(snapshot):
                return decision
            self.consider_trigger(snapshot)
        return decision

    def run_housekeeping(self) -> bool:
        """Trim logs, backups and stored changes, about once a day."""
        last = _parse(self.app.state.get_state(LAST_RETENTION_AT))
        now = self.wall()
        if last is not None and (now - last) < timedelta(hours=RETENTION_INTERVAL_HOURS):
            return False
        self.app.state.set_state(LAST_RETENTION_AT, now.isoformat())
        if last is None:
            # Not on the very first tick after installing; give the service a
            # day of normal running before it starts deleting anything.
            return False
        try:
            self.app.apply_retention()
        except Exception:
            LOG.exception("Housekeeping failed")
        return True

    def consider_auto_apply(self, snapshot: Snapshot) -> bool:
        """Apply a finished preview that was asked to apply itself.

        digiKam does not gate this: the apply holds its own half back and gets
        on with the Memories half.
        """
        if snapshot.paused:
            return False
        run_id = self.app.state.auto_apply_candidate()
        if run_id is None:
            return False
        try:
            self.app.start_apply(run_id, {"automatic": True})
            LOG.info("Applying run %s without asking, as configured", run_id)
            return True
        except ValueError as error:
            LOG.debug("Run %s is not ready to apply itself: %s", run_id, error)
        except Exception:
            LOG.exception("Could not apply run %s", run_id)
        return False

    def consider_trigger(self, snapshot: Snapshot) -> str | None:
        """Start a sync when a library has changed and settled down."""
        if snapshot.paused:
            return None
        try:
            reason = self.watcher.poll(
                snapshot.now, digikam_running=snapshot.digikam_running
            )
        except Exception:
            LOG.exception("Could not check for changes")
            return None
        if reason is None:
            return None
        if self.app.recognize_busy():
            # The trigger is deliberately left standing, so the sync happens
            # as soon as Recognize is done rather than being forgotten.
            LOG.info("Recognize is busy; holding the %s sync", reason)
            return None
        try:
            started = self.app.start_sync({"scope": "all", "trigger": reason})
        except Exception:
            LOG.exception("Could not start the %s sync", reason)
            return None
        self.watcher.consume()
        self.watcher.stash_for_run(int(started["run_id"]))
        self.started_by_trigger.append(reason)
        LOG.info("Started run %s because %s", started["run_id"], reason)
        return reason
