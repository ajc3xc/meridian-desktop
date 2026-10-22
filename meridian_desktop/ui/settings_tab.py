"""Connection settings on the Settings tab: the hosted Meridian URL and token, the local server URL, the database file, and the saved mode.

This is Qt code and sits in ui/, so the boundary check allows it. The main window places it above
the RunPod panel, in that panel's own layout, so the Settings tab's page stays the RunPod panel.

The hosted URL is checked with hosted_client.validate_base_url before anything is written. The
hosted token is typed into a password field, saved to the keychain through token_store, and never
written to the settings file. The tab shows only whether a token is set. Test connection makes one
GET /projects with the saved URL and token, on a worker thread, and shows the result in plain words.

The local URL is checked with local_client.assert_loopback before anything is written. A value that
is not an http loopback URL is refused inline, and the settings file is not touched. The database
file is picked with a file dialog, and only its path is saved.

The mode is shown, not edited. The top-bar selector is the one control that changes it, and the
main window calls refresh() after each save, so both always show the same saved mode.

The client setup section (ui/client_setup_section.py) is the last block on this tab. It manages the
Meridian core checkout and its Python, and one row per client. Nothing in it runs until a button is pressed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from PySide6.QtCore import QRunnable, Qt, QThreadPool, Slot
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from meridian_desktop import hosted_client, token_store
from meridian_desktop.hosted_client import BadBaseUrl, HostedError
from meridian_desktop.local_client import LocalClientError, assert_loopback
from meridian_desktop.secret_store import KeyringSecretStore, SecretStore
from meridian_desktop.settings_store import DEFAULT_HOSTED_URL, DEFAULT_LOCAL_URL, SettingsStore
from meridian_desktop.token_store import TokenStoreError
from meridian_desktop.ui.client_setup_section import ClientSetupSection
from meridian_desktop.ui.delivery import Delivered, Delivery, shared_delivery
from meridian_desktop.ui.text import make_selectable

# Mode names in display order: (settings value, label). The top-bar selector uses this list too.
MODE_ITEMS = (("hosted", "Hosted"), ("local", "Local"), ("tunnel", "Tunnel"))
MODE_LABELS = dict(MODE_ITEMS)

DB_DIALOG_TITLE = "Choose the Meridian database file"
DB_FILE_FILTER = "SQLite database (*.db *.sqlite *.sqlite3);;All files (*)"
NOT_CHOSEN_TEXT = "No database file chosen yet."
LOCAL_URL_SAVED_TEXT = "Local URL saved."
DB_SAVED_TEXT = "Database file saved."
NOT_WRITTEN_TEXT = "Not saved: the settings file could not be written"
HOSTED_URL_SAVED_TEXT = "Hosted URL saved."
TOKEN_SET_TEXT = "Hosted token: set"
TOKEN_NOT_SET_TEXT = "Hosted token: not set"
TOKEN_EMPTY_TEXT = "Paste the hosted token into the box, then press Save token."
TOKEN_SAVED_TEXT = "Hosted token saved in the keychain. The box is cleared."
TOKEN_REMOVED_TEXT = "Hosted token removed from the keychain."
TOKEN_NOTHING_REMOVED_TEXT = "No hosted token was saved, so nothing was removed."
TEST_RUNNING_TEXT = "Testing the saved hosted URL and token..."
TEST_CONNECTED_TEXT = "Connected to {url}. Hosted Meridian returned {count} project(s)."
TEST_UNEXPECTED_TEXT = "Connection test failed ({name})."

# A file chooser takes (parent, start directory) and returns the chosen path, or "" when cancelled.
FileChooser = Callable[[QWidget, str], str]
# The hosted list call: (hosted URL, token or None) -> the project rows. hosted_client.list_projects by default.
ListHostedFn = Callable[[str, str | None], list[Any]]


def mode_text(mode: str) -> str:
    """The saved mode as this tab shows it."""
    return f"Mode: {MODE_LABELS.get(mode, mode)} (set by the Mode selector above)"


def describe_test_failure(exc: BaseException) -> str:
    """Plain words for a failed connection test.

    A HostedError carries fixed text, with no token, body or raw exception. Any other error is
    named by its class only, so its text (which may hold a value) is never shown.
    """
    if isinstance(exc, HostedError):
        return str(exc)
    return TEST_UNEXPECTED_TEXT.format(name=exc.__class__.__name__)


def _choose_database_file(parent: QWidget, start_dir: str) -> str:
    """Ask for an existing file. Return its path, or "" when the dialog is cancelled."""
    path, _selected_filter = QFileDialog.getOpenFileName(parent, DB_DIALOG_TITLE, start_dir, DB_FILE_FILTER)
    return path


def _label(parent: QWidget, name: str, text: str = "") -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return make_selectable(label)


def _set_message(label: QLabel, text: str) -> None:
    """Show a message line, or hide the line when the text is empty."""
    label.setText(text)
    label.setVisible(bool(text))


@dataclass(frozen=True)
class _TestDone:
    """One finished connection test, as its job hands it back: its generation and the plain-words text to show."""

    generation: int
    text: str


class _ConnectionCheck:
    """The hosted connection test, run on the worker thread through the settings store, the keychain and the list call.

    It holds no widget. A test still running when the tab is deleted therefore never keeps the tab alive, and never
    touches it. The tab holds one of these and hands its ``run`` method to the thread pool.
    """

    def __init__(self, settings_store: SettingsStore, secrets: SecretStore, list_hosted: ListHostedFn) -> None:
        self._store = settings_store
        self._secrets = secrets
        self._list_hosted = list_hosted

    def run(self) -> str:
        """Runs on a worker. One GET /projects with the saved URL and token. Returns the text to show."""
        url = self._store.load().settings.hosted_url
        token = token_store.get_token(store=self._secrets)
        try:
            rows = self._list_hosted(url, token)
        except Exception as exc:  # noqa: BLE001 - shown as plain words, never as the raw exception
            return describe_test_failure(exc)
        return TEST_CONNECTED_TEXT.format(url=url, count=len(rows))


class _ConnectionTestJob(QRunnable):
    """Run one connection test on the thread pool and deliver a _TestDone to the shared delivery object.

    The job holds only the work, the owner token, the generation and the shared delivery object. It never holds the tab.
    """

    def __init__(self, generation: int, work: Callable[[], str], owner: object, delivery: Delivery) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._generation = generation
        self._work = work
        self._owner = owner
        self._delivery = delivery

    def run(self) -> None:
        try:
            text = self._work()
        except Exception as exc:  # noqa: BLE001 - a worker must never raise into the pool
            text = describe_test_failure(exc)
        self._delivery.delivered.emit(Delivered(self._owner, _TestDone(self._generation, text)))


class SettingsTab(QWidget):
    """Edit the hosted URL and token, test the hosted connection, edit the local URL and the database file, and show the saved mode."""

    def __init__(
        self,
        settings_store: SettingsStore,
        parent: QWidget | None = None,
        choose_database: FileChooser = _choose_database_file,
        secrets: SecretStore | None = None,
        list_hosted: ListHostedFn | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("settings-tab")
        self._store = settings_store
        self._choose_database = choose_database
        self._secrets = secrets if secrets is not None else KeyringSecretStore()
        self._list_hosted = list_hosted if list_hosted is not None else hosted_client.list_projects
        self._check = _ConnectionCheck(settings_store, self._secrets, self._list_hosted)
        # Identifies this tab's connection tests. A plain object, so a job can hold it without holding the tab.
        self._owner = object()
        self._generation = 0
        shared_delivery().delivered.connect(self._on_delivered)
        self._build()
        self.refresh()

    # ----------------------------------------------------------- layout

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._mode = _label(self, "settings-mode")
        layout.addWidget(self._mode)

        settings = self._store.load().settings

        # Hosted Meridian: the URL, then the token, then the connection test.
        hosted_url_row = QHBoxLayout()
        hosted_url_row.addWidget(make_selectable(QLabel("Hosted URL:", self)))
        self._hosted_url = QLineEdit(settings.hosted_url, self)
        self._hosted_url.setObjectName("settings-hosted-url")
        self._hosted_url.setPlaceholderText(DEFAULT_HOSTED_URL)
        hosted_url_row.addWidget(self._hosted_url, 1)
        self._save_hosted_url = QPushButton("Save hosted URL", self)
        self._save_hosted_url.setObjectName("settings-save-hosted-url")
        hosted_url_row.addWidget(self._save_hosted_url)
        layout.addLayout(hosted_url_row)

        self._hosted_url_message = _label(self, "settings-hosted-url-message")
        self._hosted_url_message.setVisible(False)
        layout.addWidget(self._hosted_url_message)

        token_row = QHBoxLayout()
        self._token_status = _label(self, "settings-token-status")
        token_row.addWidget(self._token_status)
        self._token_input = QLineEdit(self)
        self._token_input.setObjectName("settings-token-input")
        self._token_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._token_input.setPlaceholderText("Paste the hosted token")
        token_row.addWidget(self._token_input, 1)
        self._save_token = QPushButton("Save token", self)
        self._save_token.setObjectName("settings-save-token")
        token_row.addWidget(self._save_token)
        self._remove_token = QPushButton("Remove token", self)
        self._remove_token.setObjectName("settings-remove-token")
        token_row.addWidget(self._remove_token)
        layout.addLayout(token_row)

        self._token_message = _label(self, "settings-token-message")
        self._token_message.setVisible(False)
        layout.addWidget(self._token_message)

        test_row = QHBoxLayout()
        self._test_button = QPushButton("Test connection", self)
        self._test_button.setObjectName("settings-test-connection")
        test_row.addWidget(self._test_button)
        test_row.addStretch(1)
        layout.addLayout(test_row)

        self._test_result = _label(self, "settings-test-result")
        self._test_result.setVisible(False)
        layout.addWidget(self._test_result)

        # Local Meridian server and the database file.
        url_row = QHBoxLayout()
        url_row.addWidget(make_selectable(QLabel("Local URL:", self)))
        self._local_url = QLineEdit(settings.local_url, self)
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
        db_row.addWidget(make_selectable(QLabel("Database file:", self)))
        self._db_path = _label(self, "settings-db-path")
        db_row.addWidget(self._db_path, 1)
        self._choose_db = QPushButton("Choose database file...", self)
        self._choose_db.setObjectName("settings-choose-db")
        db_row.addWidget(self._choose_db)
        layout.addLayout(db_row)

        self._db_message = _label(self, "settings-db-message")
        self._db_message.setVisible(False)
        layout.addWidget(self._db_message)

        # Client setup: the Meridian core checkout, then one row per client with Check, Apply and Remove.
        self._client_setup = ClientSetupSection(self._store, self)
        layout.addWidget(self._client_setup)

        self._save_hosted_url.clicked.connect(self._on_save_hosted_url_clicked)
        self._hosted_url.returnPressed.connect(self._on_save_hosted_url_clicked)
        self._save_token.clicked.connect(self._on_save_token_clicked)
        self._token_input.returnPressed.connect(self._on_save_token_clicked)
        self._remove_token.clicked.connect(self._on_remove_token_clicked)
        self._test_button.clicked.connect(self._on_test_clicked)
        self._save_url.clicked.connect(self._on_save_url_clicked)
        self._local_url.returnPressed.connect(self._on_save_url_clicked)
        self._choose_db.clicked.connect(self._on_choose_db_clicked)

    def refresh(self) -> None:
        """Show the saved mode, the database file and whether a hosted token is set. The main window calls this after a mode change."""
        settings = self._store.load().settings
        self._mode.setText(mode_text(settings.mode))
        self._db_path.setText(settings.db_path or NOT_CHOSEN_TEXT)
        self._refresh_token_status()

    def _refresh_token_status(self) -> None:
        """Show only whether a token is saved. The value is read here and never shown.

        A keychain read that fails reads as not set, the same as the resolver reads it.
        """
        stored = bool(token_store.get_token(store=self._secrets))
        self._token_status.setText(TOKEN_SET_TEXT if stored else TOKEN_NOT_SET_TEXT)

    # ----------------------------------------------------------- saving

    def _update(self, **changes: object) -> None:
        """Save changed fields through the store. Every other field in the saved file is kept."""
        self._store.save(replace(self._store.load().settings, **changes))

    @Slot()
    def _on_save_hosted_url_clicked(self) -> None:
        try:
            url = hosted_client.validate_base_url(self._hosted_url.text())
        except BadBaseUrl as exc:
            # The text is fixed wording from hosted_client. The value typed is not repeated.
            _set_message(self._hosted_url_message, f"Not saved. {exc}")
            return
        try:
            self._update(hosted_url=url)
        except (OSError, ValueError):
            _set_message(self._hosted_url_message, f"{NOT_WRITTEN_TEXT}.")
            return
        self._hosted_url.setText(url)
        _set_message(self._hosted_url_message, HOSTED_URL_SAVED_TEXT)

    @Slot()
    def _on_save_token_clicked(self) -> None:
        value = self._token_input.text()
        if not value.strip():
            _set_message(self._token_message, TOKEN_EMPTY_TEXT)
            return
        try:
            hosted_client.usable_token(value)  # refuses a value with spaces before the keychain sees it
        except HostedError as exc:
            _set_message(self._token_message, f"Not saved. {exc}")
            return
        try:
            token_store.set_token(value, store=self._secrets)
        except TokenStoreError as exc:
            # TokenStoreError text names the step and the error class only, never the token.
            _set_message(self._token_message, f"Not saved. {exc}")
            return
        self._token_input.clear()
        self._refresh_token_status()
        _set_message(self._token_message, TOKEN_SAVED_TEXT)

    @Slot()
    def _on_remove_token_clicked(self) -> None:
        try:
            removed = token_store.delete_token(store=self._secrets)
        except TokenStoreError as exc:
            _set_message(self._token_message, f"Not removed. {exc}")
            return
        self._refresh_token_status()
        _set_message(self._token_message, TOKEN_REMOVED_TEXT if removed else TOKEN_NOTHING_REMOVED_TEXT)

    # ----------------------------------------------------------- connection test

    @Slot()
    def _on_test_clicked(self) -> None:
        self._test_button.setEnabled(False)
        _set_message(self._test_result, TEST_RUNNING_TEXT)
        self._generation += 1
        generation = self._generation
        # The job holds the check, the owner token, the generation and the shared delivery object. It never holds the tab.
        QThreadPool.globalInstance().start(_ConnectionTestJob(generation, self._check.run, self._owner, shared_delivery()))

    @Slot(object)
    def _on_delivered(self, item: object) -> None:
        # Runs on the GUI thread. A result for another tab is dropped here, and so is one from an earlier test.
        if not isinstance(item, Delivered) or item.owner is not self._owner:
            return
        done = item.value
        if not isinstance(done, _TestDone) or done.generation != self._generation:
            return
        self._test_button.setEnabled(True)
        _set_message(self._test_result, done.text)

    # ----------------------------------------------------------- local settings

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
    "HOSTED_URL_SAVED_TEXT",
    "LOCAL_URL_SAVED_TEXT",
    "MODE_ITEMS",
    "NOT_CHOSEN_TEXT",
    "NOT_WRITTEN_TEXT",
    "TEST_CONNECTED_TEXT",
    "TEST_UNEXPECTED_TEXT",
    "TOKEN_NOT_SET_TEXT",
    "TOKEN_SET_TEXT",
    "SettingsTab",
    "describe_test_failure",
    "mode_text",
]
