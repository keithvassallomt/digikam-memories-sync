"""Top-level command dispatcher for desktop and command-line modes."""
from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    command = args.pop(0) if args and args[0] in {"ui", "run", "service"} else "ui"
    if command == "ui":
        from .local_server import main as ui_main

        return ui_main(args)
    if command == "run":
        from .cli import main as run_main

        return run_main(args)
    print("The background service will be enabled after two-way sync is complete.", file=sys.stderr)
    return 2
