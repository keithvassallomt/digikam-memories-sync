"""Top-level command dispatcher for desktop and command-line modes."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# "notify" is DigiMem talking to itself: the macOS notification helper runs as
# a process of its own, so it needs a command, but nobody types it.
COMMANDS = ("ui", "run", "service", "autostart", "shortcuts", "notify")

USAGE = """digimem [command]

  ui                  Open the interface, starting the service if needed
  service             Run the background service in this terminal
  run                 One-off command-line sync
  autostart           enable | disable | status
  shortcuts           install | remove | status

Without a command, 'ui' is assumed.
"""


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in {"-h", "--help"} :
        print(USAGE)
        return 0
    command = args.pop(0) if args and args[0] in COMMANDS else "ui"

    # Before anything reads the configuration directory, take over the one the
    # old name left behind.
    from .settings import adopt_legacy_config_dir

    adopt_legacy_config_dir()

    if command == "run":
        from .cli import main as run_main

        return run_main(args)
    if command == "ui":
        return _ui(args)
    if command == "service":
        return _service(args)
    if command == "autostart":
        return _autostart(args)
    if command == "notify":
        from .macos_notify import main as notify_main

        return notify_main(args)
    return _shortcuts(args)


def _ui(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="digimem ui", description="Open the interface")
    parser.add_argument("--config-dir", type=Path)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--target", default="", help="Screen to open, for example attention")
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="Run the service in this terminal instead of in the background",
    )
    parser.add_argument("--port", type=int)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    from .launcher import open_ui, run_foreground

    if args.foreground:
        return run_foreground(
            args.config_dir, args.port, no_browser=args.no_browser, verbose=args.verbose
        )
    return open_ui(args.config_dir, no_browser=args.no_browser, target=args.target)


def _service(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="digimem service", description="Run the DigiMem background service"
    )
    parser.add_argument("--config-dir", type=Path)
    parser.add_argument("--port", type=int)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Start up, do one pass of work, and exit. For checking a build.",
    )
    args = parser.parse_args(argv)

    from .service import run_service

    return run_service(args.config_dir, args.port, verbose=args.verbose, once=args.once)


def _autostart(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="digimem autostart", description="Start DigiMem when you log in"
    )
    parser.add_argument("action", choices=("enable", "disable", "status"))
    parser.add_argument("--config-dir", type=Path)
    args = parser.parse_args(argv)

    from . import autostart

    try:
        if args.action == "enable":
            result = autostart.enable(args.config_dir)
        elif args.action == "disable":
            result = autostart.disable(args.config_dir)
        else:
            result = autostart.status()
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 1
    state = "on" if result.get("enabled") else "off"
    print(f"Start at login is {state} ({result.get('mechanism')}: {result.get('path')})")
    return 0


def _shortcuts(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="digimem shortcuts", description="Application-menu shortcut"
    )
    parser.add_argument("action", choices=("install", "remove", "status"))
    parser.add_argument("--config-dir", type=Path)
    args = parser.parse_args(argv)

    from . import shortcuts

    if args.action == "install":
        result = shortcuts.install(args.config_dir)
    elif args.action == "remove":
        result = shortcuts.remove()
    else:
        result = shortcuts.status()
    state = "installed" if result.get("installed") else "not installed"
    print(f"Shortcut is {state} ({result.get('path')})")
    return 0
