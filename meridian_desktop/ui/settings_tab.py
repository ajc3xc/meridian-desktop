"""The Settings tab: five collapsible groups in one scrolling column, in this order.

1. Connection: the mode (shown, set by the top-bar selector), the hosted URL, the hosted token status and its
   Save and Remove, the Test connection button, the local URL and the database file.
2. Local server and startup: the runner block (ui/runner_section.py), its state and Start, Stop and Restart, and the two
   launch-time switches. Each control asks the owner first.
3. Keys: the RunPod entry card (ui/runpod_panel.py) and one card per named key (ui/keys_section.py).
4. Client setup: the Meridian core checkout and its Python, then one row per client (ui/client_setup_section.py).
5. Diagnostics (logs): the settings file path, any note from loading it, and the Logs view (ui/logs_page.py).
   It starts folded. The Logs view reads nothing until its Refresh is pressed.

The hosted URL is checked with hosted_client.validate_base_url before anything is written. The hosted token is typed into
a password field, saved to the keychain through token_store, and never written to the settings file. The tab shows only
whether a token is set. Test connection makes one GET /projects with the saved URL and token, on a worker thread, and
shows the result in plain words.

The local URL is checked with local_client.assert_loopback before anything is written. A value that is not an http
loopback URL is refused inline, and the settings file is not touched. The database file is picked with a file dialog, and
only its path is saved.

The mode is shown, not edited. The top-bar selector is the one control that changes it, and the main window calls
refresh() after each save, so both always show the same saved mode.

The main window builds this tab with the RunPod panel as its parent, so the Settings page is still the RunPod panel. The
tab then takes the panel's entry card into the Keys group, so the RunPod panel is shown once. Built with any other
parent, the tab has no RunPod card, and the Keys group lists only the names the owner adds.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from PySide6.QtCore import QRunnable, Qt, QThreadPool, Slot
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QGridLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from meridian_desktop import hosted_client, token_store
from meridian_desktop.api_keys import ApiKeys
from meridian_desktop.hosted_client import BadBaseUrl, HostedError
from meridian_desktop.local_client import LocalClientError, assert_loopback
from meridian_desktop.secret_store import KeyringSecretStore, SecretStore
from meridian_desktop.settings_store import DEFAULT_HOSTED_URL, DEFAULT_LOCAL_URL, SettingsStore
from meridian_desktop.token_store import TokenStoreError
from meridian_desktop.ui.client_setup_section import ClientSetupSection
from meridian_desktop.ui.delivery import Delivered, Delivery, shared_delivery
from meridian_desktop.ui.keys_section import KeysSection
from meridian_desktop.ui.logs_page import LogsPage
from meridian_desktop.ui.runner_actions import RunnerActions
from meridian_desktop.ui.runner_section import RunnerSection
from meridian_desktop.ui.runpod_panel import RunPodPanel
from meridian_desktop.ui.settings_group import CollapsibleGroup
from meridian_desktop.ui.text import make_selectable

# Mode names in display order: (settings value, label). The top-bar selector uses this list too.
MODE_ITEMS = (("hosted", "Hosted"), ("local", "Local"), ("tunnel", "Tunnel"))
MODE_LABELS = dict(MODE_ITEMS)

# The width the Settings column never shrinks below, so a label wraps instead of being cut off.
CONTENT_MIN_WIDTH = 640
CONNECTION_TITLE = "Connection"
LOCAL_SERVER_TITLE = "Local server and startup"
KEYS_TITLE = "Keys"
CLIENT_SETUP_TITLE = "Client setup"
DIAGNOSTICS_TITLE = "Diagnostics (logs)"
SETTINGS_FILE_TEXT = "Settings file: {path}"
LOAD_WARNING_TEXT = "Note from the settings file: {warning}"

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


def _caption(parent: QWidget, text: str) -> QLabel:
    """A short caption at the start of a row. It never wraps, so the column is as wide as the caption."""
    label = QLabel(text, parent)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(False)
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
    """The five Settings groups: connection, local server and startup, keys, client setup, and diagnostics."""

    def __init__(
        self,
        settings_store: SettingsStore,
        parent: QWidget | None = None,
        choose_database: FileChooser = _choose_database_file,
        secrets: SecretStore | None = None,
        list_hosted: ListHostedFn | None = None,
        actions: RunnerActions | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("settings-tab")
        self._store = settings_store
        self._choose_database = choose_database
        self._secrets = secrets if secrets is not None else KeyringSecretStore()
        self._list_hosted = list_hosted if list_hosted is not None else hosted_client.list_projects
        self._actions = actions if actions is not None else RunnerActions(settings_store)
        self._check = _ConnectionCheck(settings_store, self._secrets, self._list_hosted)
        self._keys_store = ApiKeys(settings_store, self._secrets)
        # The RunPod panel is the parent when the main window builds this tab, so its Settings page stays the RunPod panel
        # and the card moves into the Keys group. A tab built with any other parent has no RunPod card.
        self._runpod: RunPodPanel | None = parent if isinstance(parent, RunPodPanel) else None
        # Identifies this tab's connection tests. A plain object, so a job can hold it without holding the tab.
        self._owner = object()
        self._generation = 0
        shared_delivery().delivered.connect(self._on_delivered)
        self._build()
        self.refresh()

    # ----------------------------------------------------------- layout

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(self)
        scroll.setObjectName("settings-scroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(scroll)

        content = QWidget(scroll)
        content.setObjectName("settings-content")
        content.setMinimumWidth(CONTENT_MIN_WIDTH)
        column = QVBoxLayout(content)
        column.setContentsMargins(4, 4, 12, 4)
        column.setSpacing(6)
        scroll.setWidget(content)

        # The RunPod entry card moves into the Keys group. The page panel stays as the page, empty of rows.
        runpod_card = self._runpod.take_card() if self._runpod is not None else None

        column.addWidget(self._build_connection(content))
        column.addWidget(self._build_local_server(content))
        column.addWidget(self._build_keys(content, runpod_card))
        column.addWidget(self._build_client_setup(content))
        column.addWidget(self._build_diagnostics(content))
        column.addStretch(1)

        self._wire()

    def _build_connection(self, parent: QWidget) -> CollapsibleGroup:
        group = CollapsibleGroup("connection", CONNECTION_TITLE, parent)
        body = group.body()
        settings = self._store.load().settings

        self._mode = _label(body, "settings-mode")
        group.add_widget(self._mode)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)

        # Hosted URL: the URL, then its message.
        grid.addWidget(_caption(body, "Hosted URL:"), 0, 0)
        self._hosted_url = QLineEdit(settings.hosted_url, body)
        self._hosted_url.setObjectName("settings-hosted-url")
        self._hosted_url.setPlaceholderText(DEFAULT_HOSTED_URL)
        grid.addWidget(self._hosted_url, 0, 1)
        self._save_hosted_url = QPushButton("Save hosted URL", body)
        self._save_hosted_url.setObjectName("settings-save-hosted-url")
        grid.addWidget(self._save_hosted_url, 0, 2)
        self._hosted_url_message = _label(body, "settings-hosted-url-message")
        self._hosted_url_message.setVisible(False)
        grid.addWidget(self._hosted_url_message, 1, 0, 1, 3)

        # Hosted token: the status line, then the box with Save and Remove. The value is never shown.
        self._token_status = _label(body, "settings-token-status")
        grid.addWidget(self._token_status, 2, 0, 1, 4)
        grid.addWidget(_caption(body, "Token:"), 3, 0)
        self._token_input = QLineEdit(body)
        self._token_input.setObjectName("settings-token-input")
        self._token_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._token_input.setPlaceholderText("Paste the hosted token")
        grid.addWidget(self._token_input, 3, 1)
        self._save_token = QPushButton("Save token", body)
        self._save_token.setObjectName("settings-save-token")
        grid.addWidget(self._save_token, 3, 2)
        self._remove_token = QPushButton("Remove token", body)
        self._remove_token.setObjectName("settings-remove-token")
        grid.addWidget(self._remove_token, 3, 3)
        self._token_message = _label(body, "settings-token-message")
        self._token_message.setVisible(False)
        grid.addWidget(self._token_message, 4, 0, 1, 4)

        # Test connection, and its result.
        self._test_button = QPushButton("Test connection", body)
        self._test_button.setObjectName("settings-test-connection")
        grid.addWidget(self._test_button, 5, 0, 1, 2, Qt.AlignmentFlag.AlignLeft)
        self._test_result = _label(body, "settings-test-result")
        self._test_result.setVisible(False)
        grid.addWidget(self._test_result, 6, 0, 1, 4)

        # Local URL.
        grid.addWidget(_caption(body, "Local URL:"), 7, 0)
        self._local_url = QLineEdit(settings.local_url, body)
        self._local_url.setObjectName("settings-local-url")
        self._local_url.setPlaceholderText(DEFAULT_LOCAL_URL)
        grid.addWidget(self._local_url, 7, 1)
        self._save_url = QPushButton("Save local URL", body)
        self._save_url.setObjectName("settings-save-local-url")
        grid.addWidget(self._save_url, 7, 2)
        self._url_message = _label(body, "settings-local-url-message")
        self._url_message.setVisible(False)
        grid.addWidget(self._url_message, 8, 0, 1, 4)

        # Database file.
        grid.addWidget(_caption(body, "Database file:"), 9, 0)
        self._db_path = _label(body, "settings-db-path")
        grid.addWidget(self._db_path, 9, 1)
        self._choose_db = QPushButton("Choose database file...", body)
        self._choose_db.setObjectName("settings-choose-db")
        grid.addWidget(self._choose_db, 9, 2)
        self._db_message = _label(body, "settings-db-message")
        self._db_message.setVisible(False)
        grid.addWidget(self._db_message, 10, 0, 1, 4)

        group.add_layout(grid)
        return group

    def _build_local_server(self, parent: QWidget) -> CollapsibleGroup:
        group = CollapsibleGroup("local-server", LOCAL_SERVER_TITLE, parent)
        # The runner block. Its own heading repeats the group title, so the heading is hidden here.
        self._runner = RunnerSection(self._actions, group.body())
        heading = self._runner.findChild(QLabel, "runner-heading")
        if heading is not None:
            heading.setVisible(False)
        group.add_widget(self._runner)
        return group

    def _build_keys(self, parent: QWidget, runpod_card: QWidget | None) -> CollapsibleGroup:
        group = CollapsibleGroup("keys", KEYS_TITLE, parent)
        self._keys = KeysSection(self._keys_store, runpod_card, group.body())
        group.add_widget(self._keys)
        return group

    def _build_client_setup(self, parent: QWidget) -> CollapsibleGroup:
        group = CollapsibleGroup("client-setup", CLIENT_SETUP_TITLE, parent)
        # The client setup block has its own heading, which repeats the group title, so the heading is hidden here.
        self._client_setup = ClientSetupSection(self._store, group.body())
        heading = self._client_setup.findChild(QLabel, "client-setup-heading")
        if heading is not None:
            heading.setVisible(False)
        group.add_widget(self._client_setup)
        return group

    def _build_diagnostics(self, parent: QWidget) -> CollapsibleGroup:
        group = CollapsibleGroup("diagnostics", DIAGNOSTICS_TITLE, parent, expanded=False)
        self._settings_file = _label(group.body(), "settings-file-path")
        group.add_widget(self._settings_file)
        self._load_warning = _label(group.body(), "settings-load-warning")
        self._load_warning.setVisible(False)
        group.add_widget(self._load_warning)
        # The Logs view reads nothing until its own Refresh is pressed, so folding this group costs nothing.
        self._logs = LogsPage(parent=group.body())
        group.add_widget(self._logs)
        return group

    def _wire(self) -> None:
        self._save_hosted_url.clicked.connect(self._on_save_hosted_url_clicked)
        self._hosted_url.returnPressed.connect(self._on_save_hosted_url_clicked)
        self._save_token.clicked.connect(self._on_save_token_clicked)
        self._token_input.returnPressed.connect(self._on_save_token_clicked)
        self._remove_token.clicked.connect(self._on_remove_token_clicked)
        self._test_button.clicked.connect(self._on_test_clicked)
        self._save_url.clicked.connect(self._on_save_url_clicked)
        self._local_url.returnPressed.connect(self._on_save_url_clicked)
        self._choose_db.clicked.connect(self._on_choose_db_clicked)
        # A saved core path turns the launch switches off, so the runner block on this tab shows them as saved now.
        self._client_setup.coreChanged.connect(self._runner.refresh_flags)

    def refresh(self) -> None:
        """Show the saved mode, the database file, whether a hosted token is set, and the keys.

        The main window calls this after a mode change, so the top bar and this tab always show the same saved mode.
        """
        result = self._store.load()
        settings = result.settings
        self._mode.setText(mode_text(settings.mode))
        self._db_path.setText(settings.db_path or NOT_CHOSEN_TEXT)
        self._refresh_token_status()
        self._runner.refresh_flags()
        self._keys.refresh()
        self._settings_file.setText(SETTINGS_FILE_TEXT.format(path=self._store.path))
        _set_message(self._load_warning, LOAD_WARNING_TEXT.format(warning=result.warning) if result.warning else "")

    def _refresh_token_status(self) -> None:
        """Show only whether a token is saved. The value is read here and never shown.

        A keychain read that fails reads as not set, the same as the resolver reads it.
        """
        stored = bool(token_store.get_token(store=self._secrets))
        self._token_status.setText(TOKEN_SET_TEXT if stored else TOKEN_NOT_SET_TEXT)

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        # The RunPod probe runs once, on the first show of this tab, whichever panel holds the card.
        if self._runpod is not None:
            self._runpod.ensure_probed()

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
