"""The single state that the home screen shows.

Kept as a pure function over gathered facts so the precedence rules can be
tested without a server, a database or a browser.
"""
from __future__ import annotations

from typing import Any

SETUP = "setup"
SYNCING = "syncing"
ATTENTION = "attention"
WAITING_DIGIKAM = "waiting_digikam"
READY = "ready"
PAUSED = "paused"
OFF = "off"
IN_SYNC = "in_sync"

# Runs that are still doing something.
ACTIVE_STATUSES = frozenset({"queued", "previewing", "applying"})
# Runs that have produced changes nobody has applied yet.
READY_STATUSES = frozenset({"previewed"})


def choose_state(
    *,
    configured: bool,
    run: dict[str, Any] | None,
    attention_total: int,
    paused: bool,
    automation_enabled: bool,
    digikam_blocks_apply: bool,
) -> str:
    """Return the one headline state, highest priority first.

    Attention outranks a ready preview because conflicts have to be settled
    before the rest of that preview can be applied.
    """
    if not configured:
        return SETUP
    status = str(run["status"]) if run else ""
    if status in ACTIVE_STATUSES:
        return SYNCING
    if attention_total:
        return ATTENTION
    if status in READY_STATUSES:
        return WAITING_DIGIKAM if digikam_blocks_apply else READY
    if paused:
        return PAUSED
    if not automation_enabled:
        return OFF
    return IN_SYNC


def compute(
    *,
    configured: bool,
    settings: dict[str, Any],
    run: dict[str, Any] | None,
    attention: dict[str, int],
    last_completed: dict[str, Any] | None,
    paused_until: str | None,
    paused: bool,
    digikam_running: bool,
    digikam_blocks_apply: bool,
    service: dict[str, Any],
    unread: int,
    raise_notifications: list[dict[str, Any]],
    now: str,
) -> dict[str, Any]:
    """Assemble everything the home screen polls for, as facts not sentences."""
    automation = dict(settings.get("automation") or {})
    state = choose_state(
        configured=configured,
        run=run,
        attention_total=attention.get("total", 0),
        paused=paused,
        automation_enabled=bool(automation.get("enabled")),
        digikam_blocks_apply=digikam_blocks_apply,
    )
    return {
        "state": state,
        "configured": configured,
        "now": now,
        "automation": {
            "enabled": bool(automation.get("enabled")),
            "apply_automatically": bool(automation.get("apply_automatically", True)),
            "paused": paused,
            "paused_until": paused_until,
        },
        "run": run,
        "attention": attention,
        "last_completed": last_completed,
        "libraries": {
            "digikam": {
                "path": settings.get("digikam_library"),
                "database": settings.get("digikam_db"),
                "running": digikam_running,
            },
            "nextcloud": {
                "url": settings.get("nextcloud_url"),
                "user": settings.get("nc_user"),
                "photos_path": settings.get("nc_photos_path"),
            },
        },
        "service": service,
        "notifications": {"unread": unread, "raise": raise_notifications},
    }
