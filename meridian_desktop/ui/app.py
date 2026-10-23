"""Application runner: QApplication, main window and tray.

``--smoke`` builds the window and tray, checks that the window is visible,
prints "smoke ok" and returns 0 without starting the event loop.

On a real start with no settings file yet, the first-run dialog runs before the window.
``--smoke`` never shows it, and ``first_run=False`` turns it off for callers that must not see it.
A real start asks GitHub once for a newer release, unless check_updates is off. ``--smoke`` does not ask.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from meridian_desktop import token_store, updater
from meridian_desktop.desktop_log import start_desktop_log
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.tray import MeridianTray
from meridian_desktop.ui.first_run import needs_first_run, run_first_run
from meridian_desktop.ui.main_window import MainWindow
from meridian_desktop.ui.runner_actions import LaunchStarts, RunnerActions

SMOKE_FLAG = "--smoke"


def main(argv: list[str] | None = None, *, settings_path: Path | None = None, first_run: bool = True) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    smoke = SMOKE_FLAG in args

    # The desktop's own log starts before anything else is built. --smoke writes no log file.
    if not smoke:
        start_desktop_log()

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Meridian")
    # The tray keeps the process alive when the window is closed.
    app.setQuitOnLastWindowClosed(False)

    store = SettingsStore(settings_path)
    # The mode this run starts in. A mode the file names wins. With none, hosted when a hosted token is saved, and
    # local otherwise. Only whether a token exists is checked here. The token is never read into the window.
    store.resolve_launch_mode(lambda: token_store.has_token(store=KeyringSecretStore()))
    if first_run and needs_first_run(store, smoke=smoke):
        outcome = run_first_run(store)
        if outcome.notice:
            QMessageBox.information(None, "Meridian Desktop", outcome.notice)

    # The update check runs once at a real start, through the urllib transport. A --smoke run passes no transport,
    # so it makes no request. The window still honours the check_updates setting.
    window = MainWindow(store, update_transport=None if smoke else updater.urllib_transport)
    tray = MeridianTray(window, app)
    window.show()
    tray.show()

    if smoke:
        if not window.isVisible():
            print("meridian_desktop --smoke: window not visible", file=sys.stderr)
            return 1
        print("smoke ok")
        return 0

    # The launch-time starts (amendments A and B of docs/local-trust-contract.md) run once, after the window shows.
    # Each runs only when its own switch is on, and never in a smoke run, which returned above. The starter is held
    # by this frame, so it lives as long as the event loop runs.
    launch = LaunchStarts(RunnerActions(store), report=window.show_launch_notes)
    QTimer.singleShot(0, launch.start)
    return int(app.exec())
