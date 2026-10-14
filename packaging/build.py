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
import tomllib
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


MANIFEST = REPO / "packaging" / "lgpl_manifest.toml"
PRUNE_REPORT = BUILD_DIR / "pruned.txt"


def prune_forbidden(bundle_root: Path, qt: dict) -> list[str]:
    """Delete bundle files the manifest forbids, and plugin files not on the allowlist.

    PyInstaller collects Qt plugins and their DLL dependencies through hooks, so a
    clean build still contains forbidden files (verified on 2026-10-14: 53 problems
    before pruning). This removes them after collection. verify_bundle then checks
    what is left, so pruning cannot hide a violation. Every key is required: a
    missing key raises KeyError and the build fails.
    """
    forbidden = set(qt["forbidden_dlls"])
    forbidden_other = set(qt["forbidden_other_dlls"])
    allowed = set(qt["allowed_dlls"])
    allowed_plugin_files = set(qt["allowed_plugin_files"])
    removed: list[str] = []
    for path in sorted(bundle_root.rglob("*")):
        if not path.is_file():
            continue
        parts = path.relative_to(bundle_root).parts
        suffix = path.suffix.lower()
        stem = path.stem
        drop = False
        if suffix == ".dll" and (stem in forbidden or stem in forbidden_other):
            drop = True
        elif suffix == ".pyd" and "PySide6" in parts and stem.startswith("Qt"):
            dll = "Qt6" + stem[2:]
            drop = dll not in allowed
        elif "PySide6" in parts and "plugins" in parts:
            idx = parts.index("plugins")
            if len(parts) > idx + 2 and "/".join(parts[idx + 1 :]) not in allowed_plugin_files:
                drop = True
        if drop:
            path.unlink()
            removed.append(path.relative_to(bundle_root).as_posix())
    return removed


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
    qt = tomllib.loads(MANIFEST.read_text(encoding="utf-8"))["qt"]
    removed = prune_forbidden(BUNDLE_DIR, qt)
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    PRUNE_REPORT.write_text("\n".join(removed) + "\n", encoding="utf-8")
    print(f"pruned {len(removed)} forbidden or unlisted files (list: {PRUNE_REPORT})")
    copy_notices()
    print(f"bundle ready: {BUNDLE_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
