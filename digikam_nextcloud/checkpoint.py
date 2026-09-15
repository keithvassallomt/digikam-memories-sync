"""Writing a long run's progress down as it goes.

A preview over a large library takes minutes. A laptop lid, a service restart
or a dropped connection should cost the remaining work, not the work already
done, so each batch records where it got to and hands its findings to storage.
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any, Protocol

LOG = logging.getLogger(__name__)

# The phases a preview moves through, in order.
LOADING_MEMORIES = "loading_memories"
SCANNING_DIGIKAM = "scanning_digikam"
SCANNING_MEMORIES = "scanning_memories"
FINISHED = "completed"

# Counters restored so a resumed run reports the same totals as an unbroken one.
COUNTERS = (
    "files_digikam",
    "files_nextcloud",
    "files_matched",
    "files_unmatched_digikam",
    "faces_digikam",
    "faces_nextcloud",
    "assigned",
    "inserted",
    "created_in_digikam",
    "reassigned_in_digikam",
    "skipped",
    "files_memories",
    "faces_memories",
    "files_unmatched_nextcloud",
    "actions_total",
)


def counters_of(report: Any) -> dict[str, int]:
    values = {name: int(getattr(report, name, 0) or 0) for name in COUNTERS}
    values["conflicts"] = int(report.conflict_count)
    return values


def restore(report: Any, counters: dict[str, Any]) -> None:
    """Seed a fresh report with what an earlier segment already counted."""
    for name in COUNTERS:
        if name in counters:
            setattr(report, name, int(counters[name] or 0))
    report.prior_conflicts = int(counters.get("conflicts") or 0)


def _as_action(item: dict[str, Any]) -> Any:
    from .models import RegionAction

    return RegionAction(**item)


class Checkpoint(Protocol):
    def batch_done(self, phase: str, cursor: dict[str, Any], report: Any) -> None: ...


class NullCheckpoint:
    """Keep everything in memory. What the command line does."""

    def batch_done(self, phase: str, cursor: dict[str, Any], report: Any) -> None:
        return None


class StateCheckpoint:
    """Drain each batch into the state database and record how far we read."""

    def __init__(self, state: Any, run_id: int):
        self.state = state
        self.run_id = run_id
        self.actions_written = 0
        self.conflicts_written = 0

    def batch_done(self, phase: str, cursor: dict[str, Any], report: Any) -> None:
        actions = [asdict(action) for action in report.actions]
        report.actions.clear()
        conflicts = [asdict(conflict) for conflict in report.conflicts]
        # Conflicts counted here must survive being cleared from memory, or a
        # resumed run would under-report them.
        report.prior_conflicts += len(conflicts)
        report.conflicts.clear()
        try:
            self.state.record_preview_batch(
                self.run_id, actions, conflicts, phase, cursor, counters_of(report)
            )
            self.actions_written += len(actions)
            self.conflicts_written += len(conflicts)
        except Exception:
            LOG.exception("Could not record progress for run %s", self.run_id)
            # Put them back so the next batch tries again rather than losing them.
            report.actions[:0] = [_as_action(item) for item in actions]
            report.prior_conflicts -= len(conflicts)
