#!/usr/bin/env python3
"""Boundary checker for the Meridian desktop app.

Rules:
  (a) Qt imports (PySide6, PySide2, PyQt5, PyQt6, PyQt4, shiboken6, shiboken2)
      appear only under meridian_desktop/ui/ and in the UI files listed in
      UI_FILES. tray.py is listed there on purpose.
  (b) No file in the repo references the meridian-latex engine path. The
      patterns are assembled from pieces so this file does not match itself.

Exit 0 and print "boundary ok" when clean. Exit 1 and print one VIOLATION line
per problem otherwise. The scan root is injectable (--root) so tests can run
the checker on a temporary tree.
"""

from __future__ import annotations

import argparse
import ast
import os
import warnings
from collections.abc import Iterator
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PACKAGE = "meridian_desktop"
UI_DIR = Path(PACKAGE) / "ui"
# tray.py is UI code and stays where it is. It is listed here so the exception is explicit.
UI_FILES = (Path(PACKAGE) / "tray.py",)
QT_ROOTS = ("PySide6", "PySide2", "PyQt5", "PyQt6", "PyQt4", "shiboken6", "shiboken2")
# Built from pieces so that this file does not contain the literal paths.
# The doubled-backslash forms are what a Windows path looks like inside a
# Python or JSON string literal, so they must match too.
FORBIDDEN_REFS = (
    "extensions/" + "meridian-latex",
    "extensions\\" + "meridian-latex",
    "extensions\\\\" + "meridian-latex",
    "meridian-latex/" + "engine",
    "meridian-latex\\" + "engine",
    "meridian-latex\\\\" + "engine",
)
# Generated or dependency trees. They are not repo content.
SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "build",
    "dist",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}


def _is_ui_file(rel: Path) -> bool:
    return rel.parts[:2] == UI_DIR.parts or rel in UI_FILES


def _qt_imports(tree: ast.AST) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.append((node.lineno, node.module))
    return [(line, name) for line, name in found if name.split(".")[0] in QT_ROOTS]


def _repo_files(root: Path) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            yield Path(dirpath) / name


def check(root: Path) -> list[str]:
    root = Path(root)
    package = root / PACKAGE
    if not package.is_dir():
        return [f"{PACKAGE}/ not found under {root} (fail closed)"]

    problems: list[str] = []
    for path in sorted(package.rglob("*.py")):
        rel = path.relative_to(root)
        if "__pycache__" in rel.parts or _is_ui_file(rel):
            continue
        try:
            with warnings.catch_warnings():
                # Escape-sequence warnings in scanned files are not this tool's concern.
                warnings.simplefilter("ignore")
                tree = ast.parse(path.read_bytes(), filename=str(path))
        except SyntaxError as exc:
            problems.append(
                f"{rel.as_posix()}:{exc.lineno}: cannot parse ({exc.msg}); "
                "boundary not checked (fail closed)"
            )
            continue
        for line, name in _qt_imports(tree):
            problems.append(
                f"{rel.as_posix()}:{line}: imports {name} outside {UI_DIR.as_posix()}/ (Qt boundary)"
            )

    for path in _repo_files(root):
        rel = path.relative_to(root).as_posix()
        lowered = path.read_bytes().lower()
        for ref in FORBIDDEN_REFS:
            if ref.lower().encode("utf-8") in lowered:
                problems.append(f"{rel}: references the meridian-latex engine path {ref!r}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the Qt and meridian-latex boundary rules.")
    parser.add_argument("--root", type=Path, default=REPO, help="tree to scan (default: this repo)")
    args = parser.parse_args(argv)

    problems = check(args.root)
    for problem in problems:
        print(f"VIOLATION {problem}")
    if problems:
        print(f"boundary FAILED: {len(problems)} violation(s)")
        return 1
    print("boundary ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
