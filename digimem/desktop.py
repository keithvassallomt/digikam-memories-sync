"""Raising a notification when no DigiMem window is open.

The page raises its own when it can (see notify.py), which is the better
channel because clicking it lands on the right screen. This one is the fallback
for a closed window: a plain one-way message, deliberately not relied on for
click-to-open, because no platform offers that without more machinery than it
is worth.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from typing import Any

LOG = logging.getLogger(__name__)

APP_NAME = "DigiMem"
TIMEOUT_SECONDS = 15


def available() -> bool:
    """Whether this machine can show one at all."""
    if sys.platform == "darwin":
        return shutil.which("osascript") is not None
    if sys.platform == "win32":
        return shutil.which("powershell") is not None or shutil.which("pwsh") is not None
    return shutil.which("notify-send") is not None


def _run(command: list[str], *, stdin: str | None = None) -> bool:
    try:
        finished = subprocess.run(
            command,
            input=stdin,
            text=True,
            capture_output=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        LOG.debug("Desktop notification failed: %s", error)
        return False
    if finished.returncode != 0:
        LOG.debug(
            "Desktop notification refused (%s): %s",
            finished.returncode, (finished.stderr or "").strip(),
        )
        return False
    return True


def _escape_applescript(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _escape_powershell(value: str) -> str:
    return value.replace("'", "''")


def send(title: str, body: str, *, urgent: bool = False) -> bool:
    """Show one notification. Returns whether the platform accepted it."""
    if not title:
        return False
    if sys.platform == "darwin":
        script = (
            f'display notification "{_escape_applescript(body)}" '
            f'with title "{_escape_applescript(APP_NAME)}" '
            f'subtitle "{_escape_applescript(title)}"'
        )
        return _run(["osascript", "-e", script])

    if sys.platform == "win32":
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if shell is None:
            return False
        script = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
    [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$texts = $template.GetElementsByTagName('text')
$texts.Item(0).AppendChild($template.CreateTextNode('{_escape_powershell(title)}')) > $null
$texts.Item(1).AppendChild($template.CreateTextNode('{_escape_powershell(body)}')) > $null
$toast = [Windows.UI.Notifications.ToastNotification]::new($template)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{APP_NAME}').Show($toast)
"""
        return _run([shell, "-NoProfile", "-NonInteractive", "-Command", "-"], stdin=script)

    command = ["notify-send", "--app-name", APP_NAME]
    if urgent:
        command += ["--urgency", "critical"]
    command += [title, body]
    return _run(command)


def send_notification(row: dict[str, Any]) -> bool:
    """Show a stored notification row."""
    from .notify import category

    return send(
        str(row.get("title", "")),
        str(row.get("message", "")),
        urgent=category(str(row.get("event_type", ""))) == "connection",
    )
