"""How DigiMem starts another copy of itself.

Three places need this: the launcher starting a service, autostart writing a
login item, and shortcuts writing a menu entry. What has to be written differs
by how DigiMem was installed, and writing the wrong one looks exactly like
clicking the entry did nothing — the process starts, fails to import itself,
and exits without a window.

There are three shapes:

* **Frozen.** In a PyInstaller bundle ``sys.executable`` is DigiMem itself, so
  the subcommand is simply its first argument. ``-m digimem`` means nothing
  here, because there is no interpreter to hand it to.
* **Installed.** A console script sits beside the interpreter, and a distro
  package's wrapper sits in ``/usr/bin``. Naming that script is what lets a
  login item work without inheriting the environment the installer happened to
  have — a ``PYTHONPATH`` set by a wrapper is not there at login.
* **A source checkout.** The interpreter plus ``-m digimem``, run from the
  directory that puts the package on the path.
"""
from __future__ import annotations

import os
import shutil
import sys
import sysconfig
from pathlib import Path

SCRIPT_NAME = "digimem"
#: The windowless twin installed by ``[project.gui-scripts]``. Windows only.
GUI_SCRIPT_NAME = "digimemw"


def frozen() -> bool:
    """Whether this is a PyInstaller bundle rather than an interpreter."""
    return bool(getattr(sys, "frozen", False))


def _script(name: str) -> Path | None:
    """An installed launcher of that name, as an absolute path.

    The scripts directory is asked first because it is where *this*
    installation's script is, which matters in a virtual environment whose
    ``bin`` is not on the PATH of the session that will run the entry. PATH is
    the fallback, and is what finds a distro package's ``/usr/bin`` wrapper.
    """
    candidates = [name, f"{name}.exe"] if sys.platform == "win32" else [name]
    directories = []
    for key in ("scripts", "purelib"):
        try:
            directories.append(Path(sysconfig.get_path(key)).parent if key == "purelib"
                               else Path(sysconfig.get_path(key)))
        except (KeyError, TypeError):  # pragma: no cover - exotic layouts
            continue
    for directory in directories:
        for candidate in candidates:
            found = directory / candidate
            if found.is_file() and os.access(found, os.X_OK):
                return found
    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            return Path(found)
    return None


def console_script() -> Path | None:
    """The installed ``digimem`` launcher, if this installation has one."""
    return None if frozen() else _script(SCRIPT_NAME)


def _windowless_script() -> Path | None:
    """The launcher that opens no console window, on Windows."""
    if frozen() or sys.platform != "win32":
        return None
    return _script(GUI_SCRIPT_NAME)


def _interpreter(*, windowless: bool) -> str:
    """The interpreter to name, preferring one that opens no console window."""
    if windowless and sys.platform == "win32":
        quiet = Path(sys.executable).with_name("pythonw.exe")
        if quiet.is_file():
            return str(quiet)
    return sys.executable


def package_root() -> Path:
    """The directory that has to be on the path for ``-m digimem`` to work."""
    return Path(__file__).resolve().parent.parent


def working_directory() -> Path | None:
    """Where a written entry must run, or None if it can run anywhere.

    Only a source checkout needs one. A frozen bundle carries its own code, and
    an installed script finds the package the way any other import does.
    """
    if frozen() or console_script() is not None:
        return None
    root = package_root()
    installed = set()
    for key in ("purelib", "platlib"):
        try:
            installed.add(Path(sysconfig.get_paths()[key]).resolve())
        except KeyError:  # pragma: no cover - exotic layouts
            continue
    return None if root in installed else root


def command(
    subcommand: str,
    config_dir: str | Path | None = None,
    *,
    windowless: bool = False,
) -> list[str]:
    """The command that runs ``subcommand`` using this same installation.

    ``windowless`` asks for the form that opens no console window, which is
    what a login item and a menu entry want on Windows. It is ignored
    everywhere else, where nothing pops up either way.
    """
    if frozen():
        command = [sys.executable, subcommand]
    else:
        script = _windowless_script() if windowless else None
        script = script or console_script()
        if script is not None:
            command = [str(script), subcommand]
        else:
            command = [_interpreter(windowless=windowless), "-m", "digimem", subcommand]
    if config_dir:
        command += ["--config-dir", str(config_dir)]
    return command
