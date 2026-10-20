"""Application runner: QApplication, main window and tray.

``--smoke`` builds the window and tray, checks that the window is visible,
prints "smoke ok" and returns 0 without starting the event loop.

On a real start with no settings file yet, the first-run dialog runs before the window.
``--smoke`` never shows it, and ``first_run=False`` turns it off for callers that must not see it.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox

from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.tray import MeridianTray
from meridian_desktop.ui.first_run import needs_first_run, run_first_run
from meridian_desktop.ui.main_window import MainWindow

SMOKE_FLAG = "--smoke"


def main(argv: list[str] | None = None, *, settings_path: Path | None = None, first_run: bool = True) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    smoke = SMOKE_FLAG in args

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Meridian")
    # The tray keeps the process alive when the window is closed.
    app.setQuitOnLastWindowClosed(False)

    store = SettingsStore(settings_path)
    if first_run and needs_first_run(store, smoke=smoke):
        outcome = run_first_run(store)
        if outcome.notice:
            QMessageBox.information(None, "Meridian Desktop", outcome.notice)

    window = MainWindow(store)
    tray = MeridianTray(window, app)
    window.show()
    tray.show()

    if smoke:
        if not window.isVisible():
            print("meridian_desktop --smoke: window not visible", file=sys.stderr)
            return 1
        print("smoke ok")
        return 0

    return int(app.exec())
