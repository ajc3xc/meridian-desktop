#!/usr/bin/env python3
"""Build the MeridianDesktop ONEDIR bundle with PyInstaller.

ONEDIR (not onefile) keeps Qt and PySide6 as separate files so the LGPL
libraries can be replaced by the user. Qt modules this app does not use are
excluded so they are not bundled. After the build the LICENSES/, NOTICE.md,
OFFER-SOURCE.md and EULA.txt files are copied into dist/MeridianDesktop/.

Next step: python packaging/verify_bundle.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ENTRY = REPO / "meridian_desktop" / "__main__.py"
APP_NAME = "MeridianDesktop"
DIST_DIR = REPO / "dist"
BUILD_DIR = REPO / "build"
BUNDLE_DIR = DIST_DIR / APP_NAME

# Qt modules the skeleton does not use. Excluding them keeps their DLLs and
# plugins out of the bundle. verify_bundle.py fails the build if any of them
# appear anyway.
UNUSED_PYSIDE_MODULES = [
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DExtras",
    "PySide6.Qt3DAnimation",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQuickWidgets",
    "PySide6.QtQml",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
    "PySide6.QtSql",
    "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtPositioning",
    "PySide6.QtLocation",
    "PySide6.QtSerialPort",
    "PySide6.QtSensors",
    "PySide6.QtRemoteObjects",
    "PySide6.QtScxml",
    "PySide6.QtStateMachine",
    "PySide6.QtNetworkAuth",
    "PySide6.QtHttpServer",
    "PySide6.QtGraphs",
    "PySide6.QtTest",
    "PySide6.QtXml",
]

# Files and directories copied into the bundle next to the executable.
NOTICE_FILES = ["NOTICE.md", "OFFER-SOURCE.md", "EULA.txt"]
NOTICE_DIRS = ["LICENSES"]


def pyinstaller_command() -> list[str]:
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--windowed",
        "--name",
        APP_NAME,
        "--distpath",
        str(DIST_DIR),
        "--workpath",
        str(BUILD_DIR / "pyinstaller"),
        "--specpath",
        str(BUILD_DIR),
        "--paths",
        str(REPO),
    ]
    for module in UNUSED_PYSIDE_MODULES:
        cmd += ["--exclude-module", module]
    cmd.append(str(ENTRY))
    return cmd


def copy_notices() -> None:
    for name in NOTICE_FILES:
        src = REPO / name
        if not src.is_file():
            raise SystemExit(f"missing notice file: {src}")
        shutil.copy2(src, BUNDLE_DIR / name)
    for name in NOTICE_DIRS:
        src = REPO / name
        if not src.is_dir():
            raise SystemExit(f"missing notice directory: {src}")
        shutil.copytree(src, BUNDLE_DIR / name, dirs_exist_ok=True)


def main() -> int:
    if not ENTRY.is_file():
        raise SystemExit(f"entry point not found: {ENTRY}")
    cmd = pyinstaller_command()
    print("running:", " ".join(cmd))
    subprocess.run(cmd, cwd=REPO, check=True)
    if not (BUNDLE_DIR / f"{APP_NAME}.exe").is_file():
        raise SystemExit("PyInstaller did not produce the executable")
    copy_notices()
    print(f"bundle ready: {BUNDLE_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
