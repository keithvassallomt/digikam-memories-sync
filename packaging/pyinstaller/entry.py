"""The entry point a frozen build starts at.

A frozen DigiMem is its own interpreter: ``sys.executable`` is the bundle, so
the subcommand arrives as the first argument rather than after ``-m digimem``.
``digimem.relaunch`` is what knows that, and what every self-launch goes
through, so nothing else here has to care.
"""
import multiprocessing
import sys

from digimem.entrypoint import main

if __name__ == "__main__":
    # Without this a bundled child process re-runs the whole application
    # instead of the worker, on Windows and on macOS spawn starts.
    multiprocessing.freeze_support()
    sys.exit(main())
