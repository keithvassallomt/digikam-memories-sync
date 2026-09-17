"""Which library changed, and therefore which one wins.

Without a record of what the two libraries last agreed on, any disagreement
looks the same as any other and every rename becomes a question for the user.
The ledger remembers the agreed name for each paired face, so a later
disagreement can be read as "one side changed" instead of "the sides differ".

A face is identified by its Memories detection. That identifier survives a
rename; the digiKam tag id does not, because the tag *is* the person.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Protocol

from .names import person_names_match

LOG = logging.getLogger(__name__)

AGREED = "agreed"
DIGIKAM_CHANGED = "digikam_changed"
MEMORIES_CHANGED = "memories_changed"
BOTH_CHANGED = "both_changed"
UNKNOWN = "unknown"


def attribute(digikam_name: str, memories_name: str, agreed_name: str | None) -> str:
    """Decide what a pair of names means, given what they last agreed on.

    Returning ``UNKNOWN`` or ``BOTH_CHANGED`` means a person has to choose.
    Everything else can be applied without asking.
    """
    if person_names_match(digikam_name, memories_name):
        return AGREED
    if not agreed_name:
        # No history, so there is no way to tell which side moved.
        return UNKNOWN
    digikam_matches = person_names_match(digikam_name, agreed_name)
    memories_matches = person_names_match(memories_name, agreed_name)
    if digikam_matches and not memories_matches:
        return MEMORIES_CHANGED
    if memories_matches and not digikam_matches:
        return DIGIKAM_CHANGED
    return BOTH_CHANGED


def key(file_id: Any, detection_id: Any) -> tuple[int, int] | None:
    """The stable identity of a paired face, or None when it has none."""
    if file_id is None or detection_id is None:
        return None
    try:
        return int(file_id), int(detection_id)
    except (TypeError, ValueError):
        return None


class Ledger(Protocol):
    def agreed_name(self, file_id: Any, detection_id: Any) -> str | None: ...

    def record(self, entries: Iterable[dict[str, Any]]) -> None: ...

    def flush(self) -> None: ...


class NullLedger:
    """No history at all, so every disagreement stays a question.

    This is what the command line uses, and what the engine falls back to, so
    behaviour without a state database is unchanged.
    """

    def agreed_name(self, file_id: Any, detection_id: Any) -> str | None:
        return None

    def record(self, entries: Iterable[dict[str, Any]]) -> None:
        return None

    def flush(self) -> None:
        return None


class StateLedger:
    """The ledger held in the application's state database.

    Names are read once into memory at the start of a run. A library with half
    a million faces is a few megabytes of names, and that beats one query per
    face by a wide margin.
    """

    BATCH = 500

    def __init__(self, state: Any):
        self.state = state
        self.names: dict[tuple[int, int], str] = {}
        self.pending: list[dict[str, Any]] = []
        self.written = 0

    def load(self) -> "StateLedger":
        self.names = self.state.load_ledger()
        LOG.info("Loaded %s remembered face names", len(self.names))
        return self

    def agreed_name(self, file_id: Any, detection_id: Any) -> str | None:
        identity = key(file_id, detection_id)
        return self.names.get(identity) if identity else None

    def record(self, entries: Iterable[dict[str, Any]]) -> None:
        for entry in entries:
            identity = key(entry.get("nextcloud_file_id"), entry.get("nextcloud_detection_id"))
            if identity is None:
                continue
            self.pending.append(entry)
            self.names[identity] = str(entry.get("synced_name") or "")
        if len(self.pending) >= self.BATCH:
            self.flush()

    def flush(self) -> None:
        if not self.pending:
            return
        batch, self.pending = self.pending, []
        try:
            self.state.record_agreements(batch)
            self.written += len(batch)
        except Exception:
            LOG.exception("Could not record %s remembered face names", len(batch))
