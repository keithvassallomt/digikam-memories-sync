"""Raising a notification when no DigiMem window is open.

The page raises its own when it can (see notify.py), which is the better
channel because clicking it lands on the right screen. This one is the fallback
for a closed window.

Where the desktop can act on a notification, it carries a button that opens the
screen the message is about, so a closed window is one press away rather than a
hunt through the application menu.

The two desktops that can do it do it differently. Linux waits on notify-send
and runs code when the button comes back, which can start a stopped service
before opening it. Windows hands a URL to the shell through protocol
activation, which is the one kind of toast action that needs no registered COM
server; nothing runs in this process, so a stopped service stays stopped.
macOS needs a process of its own, because a notification belongs to an
application and its answer arrives on a run loop: see macos_notify.py, which
keeps the same contract notify-send does. Only DigiMem.app can post one, so
anywhere else the message falls back to having nothing to press.
"""
from __future__ import annotations

import functools
import html
import logging
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Any, Callable

LOG = logging.getLogger(__name__)

APP_NAME = "DigiMem"
TIMEOUT_SECONDS = 15
# The action key notify-send prints when the button is pressed.
OPEN_ACTION = "open"
OPEN_LABEL = "Open"
# ``--action`` implies ``--wait``, so notify-send lives as long as its
# notification does. This is the point past which nobody is going to press it
# and the process is better reclaimed than kept.
ACTION_WAIT_SECONDS = 6 * 60 * 60


@dataclass(frozen=True)
class OpenAction:
    """Where a notification's button goes, in the two forms platforms need."""

    #: The address of the screen, for a desktop that can only open a URL.
    url: str
    #: What to do about it here, for a desktop that can run code instead.
    show: Callable[[], None]


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


def _toast_xml(title: str, body: str, open_action: "OpenAction | None") -> str:
    """One Windows toast, with the button the shell can act on by itself.

    ``activationType="protocol"`` asks the shell to open the address, which is
    the only activation a toast gets without the application registering a COM
    server for the notification platform to call back into.
    """
    text = (
        f"<text>{html.escape(title)}</text>"
        f"<text>{html.escape(body)}</text>"
    )
    actions = ""
    launch = ""
    if open_action is not None and open_action.url:
        address = html.escape(open_action.url)
        launch = f' activationType="protocol" launch="{address}"'
        actions = (
            f"<actions><action content=\"{html.escape(OPEN_LABEL)}\" "
            f'activationType="protocol" arguments="{address}"/></actions>'
        )
    return (
        f"<toast{launch}><visual><binding template=\"ToastGeneric\">"
        f"{text}</binding></visual>{actions}</toast>"
    )


@functools.cache
def _notify_send_takes_actions() -> bool:
    """Whether this notify-send is new enough to put a button on one."""
    try:
        finished = subprocess.run(
            ["notify-send", "--help"],
            text=True, capture_output=True, timeout=TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "--action" in (finished.stdout or "") + (finished.stderr or "")


@functools.cache
def _macos_notifier() -> list[str] | None:
    """The command that posts a notification this application can act on.

    Only DigiMem.app can: a notification belongs to an application, and an
    interpreter running from a checkout is not one. The probe shows nothing
    and asks for no permission, so a machine that cannot do this is never
    interrupted to be told so.
    """
    from . import relaunch

    if not relaunch.frozen():
        return None
    command = relaunch.command("notify")
    try:
        finished = subprocess.run(
            command + ["--probe"],
            text=True, capture_output=True, timeout=TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if finished.returncode != 0:
        LOG.debug("No macOS notification centre: %s", (finished.stderr or "").strip())
        return None
    return command


def _send_with_action(
    command: list[str],
    on_open: Callable[[], None],
    on_failure: Callable[[], None] | None = None,
) -> bool:
    """Show a notification carrying one button, and act when it is pressed.

    The wait happens on a thread of its own, so the caller learns only that
    the desktop took the request, which is all it could learn anyway. A helper
    that turns out not to be able to show one runs ``on_failure``, which is
    how a message still arrives when the button could not.
    """
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError) as error:
        LOG.debug("Desktop notification failed: %s", error)
        return False

    def wait() -> None:
        try:
            chosen, problem = process.communicate(timeout=ACTION_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            return
        if process.returncode != 0:
            LOG.debug("Notification with a button refused: %s", (problem or "").strip())
            if on_failure is not None:
                on_failure()
            return
        if chosen.strip() != OPEN_ACTION:
            return
        try:
            on_open()
        except Exception:
            LOG.exception("Could not open DigiMem from a notification")

    threading.Thread(target=wait, name="digimem-notification", daemon=True).start()
    return True


def send(
    title: str,
    body: str,
    *,
    urgent: bool = False,
    open_action: OpenAction | None = None,
) -> bool:
    """Show one notification. Returns whether the platform accepted it."""
    if not title:
        return False
    if sys.platform == "darwin":
        script = (
            f'display notification "{_escape_applescript(body)}" '
            f'with title "{_escape_applescript(APP_NAME)}" '
            f'subtitle "{_escape_applescript(title)}"'
        )
        plain = ["osascript", "-e", script]
        helper = _macos_notifier() if open_action is not None else None
        if helper is not None:
            return _send_with_action(
                helper + [
                    "--title", title, "--body", body,
                    "--action", OPEN_LABEL, "--timeout", str(ACTION_WAIT_SECONDS),
                ],
                open_action.show,
                on_failure=lambda: _run(plain),
            )
        return _run(plain)

    if sys.platform == "win32":
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if shell is None:
            return False
        xml = _escape_powershell(_toast_xml(title, body, open_action))
        script = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml('{xml}')
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{APP_NAME}').Show($toast)
"""
        return _run([shell, "-NoProfile", "-NonInteractive", "-Command", "-"], stdin=script)

    command = ["notify-send", "--app-name", APP_NAME]
    if urgent:
        command += ["--urgency", "critical"]
    if open_action is not None and _notify_send_takes_actions():
        return _send_with_action(
            command + ["--action", f"{OPEN_ACTION}={OPEN_LABEL}", title, body],
            open_action.show,
        )
    command += [title, body]
    return _run(command)


def send_notification(
    row: dict[str, Any], *, open_action: OpenAction | None = None
) -> bool:
    """Show a stored notification row."""
    from .notify import category

    return send(
        str(row.get("title", "")),
        str(row.get("message", "")),
        urgent=category(str(row.get("event_type", ""))) == "connection",
        open_action=open_action,
    )
