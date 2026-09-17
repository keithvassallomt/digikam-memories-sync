"""Deciding how a notification reaches the user.

Two channels exist. The open interface can raise a browser notification, and
the service can raise an operating-system one. An event uses exactly one, so
nobody is told the same thing twice.
"""
from __future__ import annotations

from typing import Any

# Which Settings switch governs each event.
CATEGORY_BY_EVENT = {
    "conflicts.created": "decisions",
    "apply.failed": "decisions",
    "connection.action_required": "connection",
    "run.failed": "connection",
    "run.completed": "completed",
    "run.no_changes": "completed",
    "apply.completed": "completed",
}

# Recorded in the inbox, never raised. These are context, not interruptions.
SILENT_EVENTS = frozenset({"sync.deferred", "automation.paused", "service.started"})

BROWSER = "browser"
OPERATING_SYSTEM = "os"
SUPPRESSED = "none"

# A client that stopped polling this long ago is treated as gone.
CLIENT_TIMEOUT_SECONDS = 10.0


def category(event_type: str) -> str | None:
    """The Settings switch for an event, or None when it is never raised."""
    if event_type in SILENT_EVENTS:
        return None
    return CATEGORY_BY_EVENT.get(event_type)


def is_enabled(event_type: str, settings: dict[str, Any] | None) -> bool:
    """Whether the user wants to be told about this kind of event."""
    name = category(event_type)
    if name is None:
        return False
    chosen = settings or {}
    return bool(chosen.get(name, True))


def choose_channel(
    *,
    client_attached: bool,
    client_visible: bool,
    client_can_notify: bool,
) -> str:
    """Pick the one channel that carries an event.

    A user looking at DigiMem is told nothing, because the screen in front of
    them already changed. Otherwise the open page raises it when it can, and
    the service falls back to the operating system.
    """
    if client_attached and client_visible:
        return SUPPRESSED
    if client_attached and client_can_notify:
        return BROWSER
    return OPERATING_SYSTEM


def browser_payload(row: dict[str, Any]) -> dict[str, Any]:
    """What the page needs to raise one notification."""
    run_id = row.get("run_id")
    event_type = str(row.get("event_type", ""))
    group = f"run-{run_id}" if run_id else "digimem"
    return {
        "id": int(row["id"]),
        "title": str(row.get("title", "")),
        "body": str(row.get("message", "")),
        "target": str(row.get("target", "")),
        # Grouping key: a later event about the same run and kind replaces its
        # predecessor rather than stacking another copy on the desktop.
        "tag": f"{group}-{category(event_type) or 'other'}",
        "event_type": event_type,
    }
