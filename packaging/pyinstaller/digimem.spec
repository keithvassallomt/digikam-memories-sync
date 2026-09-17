# -*- mode: python ; coding: utf-8 -*-
"""One spec for every frozen build: the AppImage, the Windows exe, the macOS app.

Deliberately onedir, never onefile. A onefile bundle unpacks its shared
libraries to a temporary directory and loads them from there, so the binaries
actually loaded are not the ones that were signed, and macOS library validation
refuses them under the hardened runtime. The app then signs, notarises, and
dies on launch. onedir keeps every library where it was signed.
"""
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve().parent.parent
ICONS = ROOT / "digimem" / "icons"

# The database drivers are imported inside functions, so static analysis never
# sees them. Named here or the direct-database connection breaks only at run
# time, on the user's machine, in the one configuration that uses it.
hidden = [
    "pymysql",
    "pymysql.cursors",
    "psycopg2",
    "psycopg2.extras",
    "keyring.backends.fail",
    "keyring.backends.chainer",
]
if sys.platform == "darwin":
    hidden += ["keyring.backends.macOS"]
elif sys.platform == "win32":
    hidden += ["keyring.backends.Windows"]
else:
    hidden += ["keyring.backends.SecretService", "keyring.backends.libsecret"]
hidden += collect_submodules("digimem")

analysis = Analysis(
    [str(ROOT / "packaging" / "pyinstaller" / "entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    # The interface is read off disk at run time, so it has to land beside the
    # package exactly as it sits in the source tree.
    datas=[
        (str(ROOT / "digimem" / "web"), "digimem/web"),
        (str(ICONS), "digimem/icons"),
    ],
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pydoc_data", "test"],
    noarchive=False,
)
pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="digimem",
    debug=False,
    strip=False,
    upx=False,
    # No console window: this opens a browser, it is not a terminal program.
    # `digimem run` on Windows therefore needs to be started from a terminal.
    console=False,
    icon=str(ICONS / "digimem.ico") if sys.platform == "win32" else None,
)

collected = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="digimem",
)

if sys.platform == "darwin":
    app = BUNDLE(
        collected,
        name="DigiMem.app",
        icon=str(ICONS / "digimem.icns"),
        # Matches autostart's LAUNCH_AGENT_ID, so the login item and the bundle
        # are the same application as far as launchd is concerned.
        bundle_identifier="com.keithvassallo.digimem",
        info_plist={
            "CFBundleName": "DigiMem",
            "CFBundleDisplayName": "DigiMem",
            "CFBundleShortVersionString": os.environ.get("VERSION", "0.0.0"),
            "CFBundleVersion": os.environ.get("VERSION", "0.0.0"),
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
            "NSHumanReadableCopyright": "Copyright © 2026 Keith Vassallo. Licensed under the GNU GPL v3 or later.",
        },
    )
