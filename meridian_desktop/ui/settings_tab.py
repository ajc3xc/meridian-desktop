"""Connection settings on the Settings tab: the local server URL and the database file, and the saved mode.

This is Qt code and sits in ui/, so the boundary check allows it. The main window places it
above the RunPod panel, in that panel's own layout, so the Settings tab's page stays the RunPod
panel.

The local URL is checked with local_client.assert_loopback before anything is written. A value
that is not an http loopback URL is refused inline, and the settings file is not touched. The
database file is picked with a file dialog, and only its path is saved. The hosted token is not
shown or entered here, and this module does not import token_store.

The mode is shown, not edited. The top-bar selector is the one control that changes it, and the
main window calls refresh() after each save, so both always show the same saved mode.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from meridian_desktop.local_client import LocalClientError, assert_loopback
from meridian_desktop.settings_store import DEFAULT_LOCAL_URL, SettingsStore

# Mode names in display order: (settings value, label). The top-bar selector uses this list too.
MODE_ITEMS = (("hosted", "Hosted"), ("local", "Local"))
MODE_LABELS = dict(MODE_ITEMS)

DB_DIALOG_TITLE = "Choose the Meridian database file"
DB_FILE_FILTER = "SQLite database (*.db *.sqlite *.sqlite3);;All files (*)"
NOT_CHOSEN_TEXT = "No database file chosen yet."
LOCAL_URL_SAVED_TEXT = "Local URL saved."
DB_SAVED_TEXT = "Database file saved."
NOT_WRITTEN_TEXT = "Not saved: the settings file could not be written"

# A file chooser takes (parent, start directory) and returns the chosen path, or "" when cancelled.
FileChooser = Callable[[QWidget, str], str]


def mode_text(mode: str) -> str:
    """The saved mode as this tab shows it."""
    return f"Mode: {MODE_LABELS.get(mode, mode)} (set by the Mode selector above)"


def _choose_database_file(parent: QWidget, start_dir: str) -> str:
    """Ask for an existing file. Return its path, or "" when the dialog is cancelled."""
    path, _selected_filter = QFileDialog.getOpenFileName(parent, DB_DIALOG_TITLE, start_dir, DB_FILE_FILTER)
    return path


def _label(parent: QWidget, name: str, text: str = "") -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


def _set_message(label: QLabel, text: str) -> None:
    """Show a message line, or hide the line when the text is empty."""
    label.setText(text)
    label.setVisible(bool(text))


class SettingsTab(QWidget):
    """Edit the local server URL and the database file, and show the saved mode."""

    def __init__(
        self,
        settings_store: SettingsStore,
        parent: QWidget | None = None,
        choose_database: FileChooser = _choose_database_file,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("settings-tab")
        self._store = settings_store
        self._choose_database = choose_database
        self._build()
        self.refresh()

    # ----------------------------------------------------------- layout

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._mode = _label(self, "settings-mode")
        layout.addWidget(self._mode)

        url_row = QHBoxLayout()
        url_row.addWidget(QLabel("Local URL:", self))
        self._local_url = QLineEdit(self._store.load().settings.local_url, self)
        self._local_url.setObjectName("settings-local-url")
        self._local_url.setPlaceholderText(DEFAULT_LOCAL_URL)
        url_row.addWidget(self._local_url, 1)
        self._save_url = QPushButton("Save local URL", self)
        self._save_url.setObjectName("settings-save-local-url")
        url_row.addWidget(self._save_url)
        layout.addLayout(url_row)

        self._url_message = _label(self, "settings-local-url-message")
        self._url_message.setVisible(False)
        layout.addWidget(self._url_message)

        db_row = QHBoxLayout()
        db_row.addWidget(QLabel("Database file:", self))
        self._db_path = _label(self, "settings-db-path")
        db_row.addWidget(self._db_path, 1)
        self._choose_db = QPushButton("Choose database file...", self)
        self._choose_db.setObjectName("settings-choose-db")
        db_row.addWidget(self._choose_db)
        layout.addLayout(db_row)

        self._db_message = _label(self, "settings-db-message")
        self._db_message.setVisible(False)
        layout.addWidget(self._db_message)

        self._save_url.clicked.connect(self._on_save_url_clicked)
        self._local_url.returnPressed.connect(self._on_save_url_clicked)
        self._choose_db.clicked.connect(self._on_choose_db_clicked)

    def refresh(self) -> None:
        """Show the saved mode and database file. The main window calls this after a mode change."""
        settings = self._store.load().settings
        self._mode.setText(mode_text(settings.mode))
        self._db_path.setText(settings.db_path or NOT_CHOSEN_TEXT)

    # ----------------------------------------------------------- saving

    def _update(self, **changes: object) -> None:
        """Save changed fields through the store. Every other field in the saved file is kept."""
        self._store.save(replace(self._store.load().settings, **changes))

    @Slot()
    def _on_save_url_clicked(self) -> None:
        url = self._local_url.text().strip()
        try:
            assert_loopback(url)
        except LocalClientError as exc:
            # The text is fixed wording from local_client. The value typed is not repeated.
            _set_message(self._url_message, f"Not saved. {exc}")
            return
        try:
            self._update(local_url=url)
        except (OSError, ValueError):
            _set_message(self._url_message, f"{NOT_WRITTEN_TEXT}.")
            return
        _set_message(self._url_message, LOCAL_URL_SAVED_TEXT)

    @Slot()
    def _on_choose_db_clicked(self) -> None:
        current = self._store.load().settings.db_path
        start_dir = str(Path(current).parent) if current else ""
        chosen = self._choose_database(self, start_dir)
        if not chosen:
            return  # the dialog was cancelled, so nothing changes
        try:
            self._update(db_path=chosen)
        except (OSError, ValueError):
            _set_message(self._db_message, f"{NOT_WRITTEN_TEXT}.")
            return
        self.refresh()  # show the path back from the saved file
        _set_message(self._db_message, DB_SAVED_TEXT)


__all__ = [
    "DB_FILE_FILTER",
    "DB_SAVED_TEXT",
    "LOCAL_URL_SAVED_TEXT",
    "MODE_ITEMS",
    "NOT_CHOSEN_TEXT",
    "NOT_WRITTEN_TEXT",
    "SettingsTab",
    "mode_text",
]
