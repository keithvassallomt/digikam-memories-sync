"""Posting a notification macOS can act on, from inside the application bundle.

``display notification`` carries no buttons, and its click activates whichever
application posted it, which from a Python service is osascript. A notification
this application can act on has to be posted by the bundle itself, and the
answer to it arrives on a run loop, which a headless HTTP service has not got.

So this is a process of its own. It posts one notification, waits on a run loop
until somebody presses the button or the notification goes stale, prints the
action it was given, and exits. That is the contract ``notify-send --action``
keeps on Linux, which is why desktop.py can drive the two the same way.

It only works inside DigiMem.app: a notification belongs to an application, and
an interpreter running from a checkout is not one. Everywhere else it exits
non-zero and the caller falls back to a message with nothing to press.
"""
from __future__ import annotations

import argparse
import sys
import uuid

try:  # Only the macOS build has these, and only it can use them.
    import Foundation
    import UserNotifications
except ImportError:  # pragma: no cover - every platform but the macOS build
    Foundation = None  # type: ignore[assignment]
    UserNotifications = None  # type: ignore[assignment]

#: Printed when the button is pressed, matching what notify-send prints.
OPEN_ACTION = "open"
#: Ours alone, so a category registered here cannot collide with another app's.
CATEGORY_ID = "com.keithvassallo.digimem.open"
#: How long to wait for permission before giving up and falling back.
AUTHORIZATION_SECONDS = 30.0
#: How often the loop looks at what has arrived.
POLL_SECONDS = 0.25


class NotificationCentreUnavailable(RuntimeError):
    """This is not a macOS application, so it has no notifications to post."""


def _centre():
    """The notification centre for this bundle, or a refusal saying why."""
    if UserNotifications is None or Foundation is None:
        raise NotificationCentreUnavailable(
            "This DigiMem was built without the macOS notification frameworks."
        )
    if Foundation.NSBundle.mainBundle().bundleIdentifier() is None:
        raise NotificationCentreUnavailable(
            "Notifications with a button need DigiMem.app, not a bare interpreter."
        )
    centre = UserNotifications.UNUserNotificationCenter.currentNotificationCenter()
    if centre is None:  # pragma: no cover - defensive; the call raises instead
        raise NotificationCentreUnavailable("macOS refused a notification centre.")
    return centre


def _pump(until, seconds: float) -> bool:
    """Run the loop until ``until()`` is true, or the time runs out."""
    loop = Foundation.NSRunLoop.currentRunLoop()
    deadline = Foundation.NSDate.dateWithTimeIntervalSinceNow_(seconds)
    while not until():
        if Foundation.NSDate.date().compare_(deadline) >= 0:
            return False
        loop.runMode_beforeDate_(
            Foundation.NSDefaultRunLoopMode,
            Foundation.NSDate.dateWithTimeIntervalSinceNow_(POLL_SECONDS),
        )
    return True


def _authorize(centre) -> None:
    """Ask once for permission to show anything at all."""
    answer: list[bool] = []

    def decided(granted, error) -> None:
        del error
        answer.append(bool(granted))

    options = (
        UserNotifications.UNAuthorizationOptionAlert
        | UserNotifications.UNAuthorizationOptionSound
    )
    centre.requestAuthorizationWithOptions_completionHandler_(options, decided)
    if not _pump(lambda: bool(answer), AUTHORIZATION_SECONDS):
        raise NotificationCentreUnavailable("macOS did not answer the permission request.")
    if not answer[0]:
        raise NotificationCentreUnavailable("Notifications are turned off for DigiMem.")


def _register_button(centre, label: str) -> None:
    """Declare the one button this notification carries."""
    action = UserNotifications.UNNotificationAction.actionWithIdentifier_title_options_(
        OPEN_ACTION, label, UserNotifications.UNNotificationActionOptionForeground
    )
    category = (
        UserNotifications.UNNotificationCategory
        .categoryWithIdentifier_actions_intentIdentifiers_options_(
            # 0 is UNNotificationCategoryOptionNone, named here as a number
            # because which pyobjc versions export the name has moved about.
            CATEGORY_ID, [action], [], 0
        )
    )
    centre.setNotificationCategories_({category})


#: Delegates macOS refers to weakly. Dropping one crashes the callback.
_KEEP_ALIVE: list[object] = []


def _listen(centre) -> list[str]:
    """Attach a delegate, and hand back the list its answers land in."""
    pressed: list[str] = []
    default = UserNotifications.UNNotificationDefaultActionIdentifier

    class Delegate(Foundation.NSObject):
        def userNotificationCenter_didReceiveNotificationResponse_withCompletionHandler_(
            self, notification_centre, response, finished
        ):
            del notification_centre
            chosen = str(response.actionIdentifier())
            # Pressing the button and clicking the message itself both mean
            # the same thing here, so both come back as the one action.
            pressed.append(OPEN_ACTION if chosen == default else chosen)
            finished()

    delegate = Delegate.alloc().init()
    centre.setDelegate_(delegate)
    _KEEP_ALIVE.append(delegate)
    return pressed


def post(title: str, body: str, *, label: str, timeout: float) -> str:
    """Show one notification, and return the action pressed, or "" for none."""
    centre = _centre()
    _authorize(centre)
    if label:
        _register_button(centre, label)
    content = UserNotifications.UNMutableNotificationContent.alloc().init()
    content.setTitle_(title)
    content.setBody_(body)
    if label:
        content.setCategoryIdentifier_(CATEGORY_ID)
    request = UserNotifications.UNNotificationRequest.requestWithIdentifier_content_trigger_(
        str(uuid.uuid4()), content, None
    )
    failure: list[str] = []
    centre.addNotificationRequest_withCompletionHandler_(
        request, lambda error: failure.append(str(error)) if error else None
    )
    if not label:
        # Nothing to wait for. Give the request a moment to leave.
        _pump(lambda: bool(failure), 1.0)
        if failure:
            raise NotificationCentreUnavailable(failure[0])
        return ""
    pressed = _listen(centre)
    _pump(lambda: bool(pressed) or bool(failure), timeout)
    if failure:
        raise NotificationCentreUnavailable(failure[0])
    return pressed[0] if pressed else ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="digimem notify",
        description="Show one macOS notification and wait for its button.",
    )
    parser.add_argument("--title", default="")
    parser.add_argument("--body", default="")
    parser.add_argument("--action", default="", help="Button label; omit for no button")
    parser.add_argument("--timeout", type=float, default=6 * 60 * 60)
    parser.add_argument(
        "--probe",
        action="store_true",
        help="Say whether a notification could be posted, and show nothing",
    )
    args = parser.parse_args(argv)

    try:
        if args.probe:
            _centre()
            return 0
        if not args.title:
            parser.error("--title is required unless probing")
        chosen = post(args.title, args.body, label=args.action, timeout=args.timeout)
    except NotificationCentreUnavailable as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception as error:  # pragma: no cover - whatever AppKit throws
        print(f"The notification could not be shown: {error}", file=sys.stderr)
        return 1
    if chosen:
        print(chosen)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
