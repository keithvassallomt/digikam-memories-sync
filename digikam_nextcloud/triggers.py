"""Noticing that a library changed, and waiting for the changes to stop.

Someone renaming twenty faces should get one sync afterwards, not twenty while
they work. So a change does not start a run; it sets a time, and further
changes push that time out, up to a limit.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

LOG = logging.getLogger(__name__)

DIGIKAM_CHANGED = "digikam_changed"
DIGIKAM_CLOSED = "digikam_closed"
MEMORIES_CHANGED = "memories_changed"
INTERVAL = "interval"

# However busy a library is, it still syncs about this often.
QUIET_CAP_MINUTES = 60

# Where the watcher keeps what it knows between ticks.
PENDING = "pending_trigger"
DIGIKAM_FINGERPRINT = "last_synced_digikam_fingerprint"
MEMORIES_FINGERPRINT = "last_synced_memories_fingerprint"
DIGIKAM_MTIME = "last_digikam_mtime"
MEMORIES_CHECKED_AT = "last_memories_check_at"
IN_FLIGHT = "in_flight_fingerprints"
# What the last poll saw, as opposed to what was last synced. Without this, a
# change that is already waiting would be re-noticed on every poll and keep
# pushing its own start time further away.
DIGIKAM_SEEN = "last_seen_digikam_fingerprint"
MEMORIES_SEEN = "last_seen_memories_fingerprint"
LAST_COMPLETED_AT = "last_completed_at"

# A run that reached one of these told us the truth about both libraries.
SETTLED_STATUSES = frozenset(
    {"applied", "applied_with_issues", "no_changes", "previewed", "deferred", "apply_failed"}
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


@dataclass
class Pending:
    """Changes seen but not yet acted on."""

    reasons: list[str] = field(default_factory=list)
    first_change_at: datetime | None = None
    start_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reasons": list(self.reasons),
            "first_change_at": self.first_change_at.isoformat() if self.first_change_at else None,
            "start_at": self.start_at.isoformat() if self.start_at else None,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Pending":
        if not isinstance(data, dict):
            return cls()
        reasons = data.get("reasons")
        return cls(
            reasons=[str(item) for item in reasons] if isinstance(reasons, list) else [],
            first_change_at=_parse(data.get("first_change_at")),
            start_at=_parse(data.get("start_at")),
        )

    @property
    def waiting(self) -> bool:
        return bool(self.reasons)


def note_change(
    pending: Pending,
    reason: str,
    now: datetime,
    quiet_minutes: float,
    cap_minutes: float = QUIET_CAP_MINUTES,
) -> Pending:
    """Record a change and work out when the sync should start.

    Each new change pushes the start out by the quiet period, but never past
    the cap measured from the first change, so an afternoon of editing still
    syncs hourly.
    """
    first = pending.first_change_at or now
    reasons = list(pending.reasons)
    if reason not in reasons:
        reasons.append(reason)
    latest = first + timedelta(minutes=max(0.0, cap_minutes))
    start = min(now + timedelta(minutes=max(0.0, quiet_minutes)), latest)
    return Pending(reasons=reasons, first_change_at=first, start_at=start)


def is_due(pending: Pending, now: datetime) -> bool:
    return bool(pending.reasons) and pending.start_at is not None and pending.start_at <= now


def primary_reason(pending: Pending) -> str:
    """The reason to show. The first one seen started the wait."""
    return pending.reasons[0] if pending.reasons else INTERVAL


class TriggerWatcher:
    """Polls both libraries and decides when a sync is due."""

    def __init__(self, app: Any):
        self.app = app
        self.state = app.state

    # ------------------------------------------------------------- settings

    def automation(self) -> dict[str, Any]:
        return dict(self.app.settings.load().get("automation") or {})

    # --------------------------------------------------------------- memory

    def pending(self) -> Pending:
        return Pending.from_dict(self.state.get_state(PENDING))

    def save_pending(self, pending: Pending) -> None:
        if pending.waiting:
            self.state.set_state(PENDING, pending.to_dict())
        else:
            self.state.clear_state(PENDING)

    def clear(self) -> None:
        self.state.clear_state(PENDING)

    def observe(
        self,
        pending: Pending,
        reason: str,
        current: str,
        seen_key: str,
        synced_key: str,
        now: datetime,
    ) -> Pending:
        """Fold one observation into the pending state.

        A value that differs from the last sync is work to do. A value that
        also differs from the previous poll is a *fresh* change, and only that
        extends the quiet period.
        """
        previously_seen = self.state.get_state(seen_key)
        self.state.set_state(seen_key, current)
        if current == self.state.get_state(synced_key):
            return pending
        if current == previously_seen and reason in pending.reasons:
            return pending
        LOG.info("%s since the last sync", reason.replace("_", " ").capitalize())
        return note_change(
            pending, reason, now,
            float(self.automation().get("quiet_period_minutes") or 10),
        )

    # ---------------------------------------------------------- the checks

    def check_digikam(self, pending: Pending, now: datetime, digikam_running: bool) -> Pending:
        """Look at digiKam, but never while it is open and mid-write."""
        if digikam_running:
            return pending
        database = self.app.settings.load().get("digikam_db")
        if not database:
            return pending
        from . import fingerprint as fingerprint_module

        mtime = fingerprint_module.source_mtime(database)
        if mtime and mtime == self.state.get_state(DIGIKAM_MTIME):
            return pending
        self.state.set_state(DIGIKAM_MTIME, mtime)
        try:
            current = fingerprint_module.digikam_fingerprint(database)
        except Exception:
            LOG.exception("Could not read the digiKam library")
            return pending
        return self.observe(
            pending, DIGIKAM_CHANGED, current, DIGIKAM_SEEN, DIGIKAM_FINGERPRINT, now
        )

    def check_memories(self, pending: Pending, now: datetime) -> Pending:
        """Ask Nextcloud whether anything moved, no more often than configured."""
        interval = float(self.automation().get("check_interval_minutes") or 5)
        last = _parse(self.state.get_state(MEMORIES_CHECKED_AT))
        if last is not None and (now - last) < timedelta(minutes=interval):
            return pending
        self.state.set_state(MEMORIES_CHECKED_AT, now.isoformat())
        try:
            current = self.app.memories_fingerprint()
        except Exception as error:
            LOG.debug("Could not read the Memories fingerprint: %s", error)
            return pending
        if current is None:
            return pending
        return self.observe(
            pending, MEMORIES_CHANGED, current, MEMORIES_SEEN, MEMORIES_FINGERPRINT, now
        )

    def check_interval(self, pending: Pending, now: datetime) -> Pending:
        """The safety net for anything the fingerprints miss."""
        hours = float(self.automation().get("fallback_interval_hours") or 24)
        last = _parse(self.state.get_state(LAST_COMPLETED_AT))
        if last is None:
            self.state.set_state(LAST_COMPLETED_AT, now.isoformat())
            return pending
        if (now - last) < timedelta(hours=hours):
            return pending
        if INTERVAL in pending.reasons:
            return pending
        LOG.info("The fallback interval has elapsed since the last sync")
        # No quiet period: nothing is changing, so there is nothing to wait for.
        return note_change(pending, INTERVAL, now, 0.0)

    # --------------------------------------------------- fingerprint bookkeeping

    def stash_for_run(self, run_id: int) -> None:
        """Remember both fingerprints as they were when this run started.

        Promoting these on completion, rather than measuring again afterwards,
        means a change made during the run is still noticed next time.
        """
        database = self.app.settings.load().get("digikam_db")
        from . import fingerprint as fingerprint_module

        digikam = None
        if database:
            try:
                digikam = fingerprint_module.digikam_fingerprint(database)
            except Exception:
                digikam = None
        try:
            memories = self.app.memories_fingerprint()
        except Exception:
            memories = None
        self.state.set_state(
            IN_FLIGHT, {"run_id": int(run_id), "digikam": digikam, "memories": memories}
        )

    def promote_finished(self) -> bool:
        """Once a run settles, treat what it saw as the synced state."""
        stashed = self.state.get_state(IN_FLIGHT)
        if not isinstance(stashed, dict):
            return False
        run = self.state.run(int(stashed.get("run_id") or 0))
        if run is None or str(run.get("status")) not in SETTLED_STATUSES:
            return False
        if stashed.get("digikam"):
            self.state.set_state(DIGIKAM_FINGERPRINT, stashed["digikam"])
        if stashed.get("memories"):
            self.state.set_state(MEMORIES_FINGERPRINT, stashed["memories"])
        self.state.set_state(LAST_COMPLETED_AT, _now().isoformat())
        self.state.clear_state(IN_FLIGHT)
        return True

    # ------------------------------------------------------------------ tick

    def poll(self, now: datetime | None = None, *, digikam_running: bool = False) -> str | None:
        """Return the reason a sync should start now, or None."""
        now = now or _now()
        self.promote_finished()
        automation = self.automation()
        if not automation.get("enabled"):
            self.clear()
            return None

        pending = self.pending()
        pending = self.check_digikam(pending, now, digikam_running)
        pending = self.check_memories(pending, now)
        pending = self.check_interval(pending, now)
        self.save_pending(pending)

        if not is_due(pending, now):
            return None
        # Reported, not consumed. A gate may still refuse, and the trigger has
        # to survive that rather than being silently dropped.
        return primary_reason(pending)

    def consume(self) -> None:
        """Called once a run has actually started for the reported reason."""
        self.clear()
