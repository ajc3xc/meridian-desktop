"""Zotero tab: whether Zotero is installed, running and answering, a Start Zotero button, and a library search.

This is Qt code and sits in ui/, so the boundary check allows it. The tab makes no request when it
is built. It checks Zotero when first shown and when Refresh is pressed. Each check, each search and
the start action run on the thread pool, so the UI never blocks. Zotero starts only after the owner
picks Start Zotero in a dialog whose default button is Cancel. Results are plain text (PlainText
labels and plain table cells), so Zotero's text is never read as HTML. The results table is read-only
and has the shared Copy action (Ctrl+C or the right-click menu).

The bridge line and the Update bridge button are read from the local server only when the panel is given a bridge and
a Local URL reader. Without them the card shows no bridge line and makes no bridge request. Update bridge sends nothing
until the owner picks one of three choices in a dialog whose default is Cancel: close Zotero politely, force close
Zotero, or wait until Zotero restarts. A force close needs a second Yes, whose default is No. A polite close that leaves
Zotero open is followed by a dialog that offers force, retry or cancel.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QRunnable, Qt, QThreadPool, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from meridian_desktop import zotero_client
from meridian_desktop.ui.confirm import ask_yes_no
from meridian_desktop.ui.delivery import Delivered, shared_delivery
from meridian_desktop.ui.text import TextBlock, enable_table_copy, make_selectable
from meridian_desktop.zotero_bridge_client import (
    CHOICE_FORCE,
    CHOICE_POLITE,
    CHOICE_WAIT,
    CHOICES,
    STATE_CURRENT,
    STATE_ERROR,
    STATE_NOT_INSTALLED,
    STATE_UPDATE_READY,
    STATE_ZOTERO_RUNNING,
    BridgeResult,
    BridgeStatus,
)
from meridian_desktop.zotero_client import (
    API_DISABLED,
    API_NOT_RUNNING,
    API_ON,
    SEARCH_LIMIT,
    ZoteroError,
    ZoteroItem,
    ZoteroStatus,
    command_line,
)

INTRO_TEXT = (
    "Zotero is a separate app that runs on this computer. This tab reads its local API "
    "on 127.0.0.1:23119 and nothing else."
)
NOT_CHECKED_TEXT = "Zotero: not checked yet. Press Refresh to check."
CHECKING_TEXT = "Checking Zotero..."
CHECK_FAILED_TEXT = "The check failed unexpectedly ({})."
INSTALLED_TEXT = "Installed: yes, at {}"
NOT_INSTALLED_TEXT = (
    "Installed: not found in the usual install folders. Install Zotero from zotero.org, then press Refresh."
)
PROCESS_RUNNING_TEXT = "Zotero process: running"
PROCESS_STOPPED_TEXT = "Zotero process: not running"
PROCESS_UNKNOWN_TEXT = "Zotero process: could not be checked on this computer"
API_TEXT = {
    API_ON: "Local API: on",
    API_DISABLED: "Local API: off (Zotero has it turned off)",
    API_NOT_RUNNING: "Local API: not answering (Zotero is not running)",
}
API_ERROR_TEXT = "Local API: could not be checked"
START_HINT_READY = (
    "Start Zotero opens Zotero on this computer after you confirm. The desktop changes no Zotero setting."
)
START_HINT_RUNNING = (
    "Zotero is already running, so there is nothing to start. If the local API is off, "
    "turn it on in Zotero under Settings, then Advanced."
)
START_HINT_NOT_INSTALLED = (
    "Zotero is not installed in a usual folder, so the desktop cannot start it. "
    "Install Zotero from zotero.org, then press Refresh."
)
START_CANCELLED_TEXT = "Start cancelled. Nothing was started and no file was written."
# The Zotero bridge: its line on the card, the Update bridge button, and the dialog behind it.
BRIDGE_NOT_CHECKED_TEXT = "Bridge: not checked yet."
BRIDGE_CHECKING_TEXT = "Bridge: checking..."
BRIDGE_ERROR_TEXT = "Bridge: could not be checked. {}"
BRIDGE_CURRENT_TEXT = "Bridge {installed} installed; expected {expected}."
BRIDGE_UPDATE_READY_TEXT = "Bridge {installed} installed; expected {expected}. An update is ready."
BRIDGE_ZOTERO_OPEN_TEXT = (
    "Bridge {installed} installed; expected {expected}. An update is ready. "
    "Zotero is open, so it needs Zotero to close and restart."
)
BRIDGE_NOT_INSTALLED_TEXT = "Bridge not installed; expected {expected}."
WAIT_RESULT_TEXT = "Waiting: the bridge will update the next time Zotero starts."
# A recorded "wait" choice reads as the waiting sentence, in every state that has a bridge line.
BRIDGE_WAIT_PENDING_TEXT = "Bridge {installed} installed; expected {expected}. " + WAIT_RESULT_TEXT
BRIDGE_NOT_INSTALLED_WAIT_TEXT = "Bridge not installed; expected {expected}. " + WAIT_RESULT_TEXT
BRIDGE_SENDING_TEXT = "Sending the bridge update..."
BRIDGE_DONE_TEXT = "The bridge update was sent. Zotero may take up to a minute to close and restart."
BRIDGE_UPDATE_UNCHECKED_TEXT = "The bridge update could not be completed."
UPDATE_BUTTON_TEXT = "Update bridge"
POLITE_BUTTON_TEXT = "Close Zotero politely, then install"
FORCE_BUTTON_TEXT = "Force close Zotero, then install"
WAIT_BUTTON_TEXT = "Wait until Zotero restarts"
CANCEL_BUTTON_TEXT = "Cancel"
RETRY_BUTTON_TEXT = "I will close Zotero myself, then try again"
BRIDGE_DIALOG_TITLE = "Update the Zotero bridge"
BRIDGE_DIALOG_TEXT = "Choose how the Zotero bridge is updated. Close Zotero politely is recommended."
BRIDGE_DIALOG_DETAIL = (
    "Close Zotero politely, then install (recommended): this is a normal close request, the same as closing Zotero "
    "yourself. It can take up to a minute. If Zotero does not close, nothing is installed and you are offered the "
    "next steps.\n\n"
    "Force close Zotero, then install: force ends Zotero at once. Anything unsaved in Zotero is lost. "
    "You are asked once more before anything is forced.\n\n"
    "Wait until Zotero restarts: this closes nothing. The bridge is updated the next time Zotero starts.\n\n"
    "Cancel: nothing is closed, nothing is updated and nothing is sent."
)
FORCE_CONFIRM_TITLE = "Force close Zotero"
FORCE_CONFIRM_TEXT = "Force close ends Zotero immediately. Anything unsaved in Zotero is lost. Continue?"
AFTER_POLITE_TITLE = "Zotero did not close"
AFTER_POLITE_TEXT = "Zotero did not close, so the bridge was not installed. Choose what to do next."
AFTER_POLITE_DETAIL = (
    "Force close Zotero, then install: force ends Zotero at once. Anything unsaved in Zotero is lost. "
    "You are asked once more before anything is forced.\n\n"
    "I will close Zotero myself, then try again: close Zotero yourself, then choose this. The desktop then asks "
    "Zotero to close normally and installs.\n\n"
    "Cancel: nothing more is closed, updated or sent."
)
# What the follow-up dialog answers. Retry sends the polite choice again. Force goes through the second question.
FOLLOW_FORCE = "force"
FOLLOW_RETRY = "retry"
STARTING_TEXT = "Starting Zotero..."
API_READY_TEXT = "Local API answering: yes"
API_NOT_READY_TEXT = "Local API answering: not yet. Zotero can take a little while to open. Press Refresh in a moment."
SEARCH_PLACEHOLDER = "Search your Zotero library by title, author or year"
SEARCH_HINT = "Type a search term, then press Search or Enter."
BLANK_TEXT = "Type a search term first. Nothing was sent to Zotero."
SEARCHING_TEXT = "Searching your Zotero library..."
NO_RESULTS_TEXT = "No items in your Zotero library match \"{}\"."
RESULTS_TEXT = "{} result(s) for \"{}\"."
LIMIT_TEXT = " Showing the first {}; narrow the search to see fewer."
RESULT_COLUMNS = ("Title", "Authors", "Year", "Zotero key")
RESULT_TITLE_COLUMN = 0
# The kinds of read the panel runs. Each kind has at most one read in flight.
STATUS = "status"
SEARCH = "search"
ACTION = "action"
BRIDGE_STATUS = "bridge_status"
BRIDGE_UPDATE = "bridge_update"


def _label(parent: QWidget, name: str, text: str) -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return make_selectable(label)


def process_text(running: bool | None) -> str:
    if running is True:
        return PROCESS_RUNNING_TEXT
    if running is False:
        return PROCESS_STOPPED_TEXT
    return PROCESS_UNKNOWN_TEXT


def api_text(state: str) -> str:
    return API_TEXT.get(state, API_ERROR_TEXT)


def results_text(query: str, count: int) -> str:
    """The line above the results. It names the query and says when the list is cut at the limit."""
    if count == 0:
        return NO_RESULTS_TEXT.format(query)
    text = RESULTS_TEXT.format(count, query)
    if count >= SEARCH_LIMIT:
        text += LIMIT_TEXT.format(SEARCH_LIMIT)
    return text


def start_dialog(parent: QWidget | None, command: str, folder: str) -> tuple[QMessageBox, QPushButton, QPushButton]:
    """The Start Zotero confirmation, built but not shown. Its default button is Cancel, so Enter does not start Zotero.

    The command and the working folder are shown as plain text, so the owner sees exactly what will run.
    """
    box = QMessageBox(parent)
    box.setWindowTitle("Start Zotero")
    box.setIcon(QMessageBox.Icon.Question)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setText("Start Zotero on this computer?")
    box.setInformativeText(
        f"The desktop will run this command:\n\n{command}\n\n"
        f"Working folder: {folder}\n\n"
        "Zotero opens in its own window. The desktop changes no Zotero setting."
    )
    start = box.addButton("Start Zotero", QMessageBox.ButtonRole.AcceptRole)
    cancel = box.addButton(QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(cancel)
    box.setEscapeButton(cancel)
    for child in box.findChildren(QLabel):
        child.setTextFormat(Qt.TextFormat.PlainText)
        make_selectable(child)
    return box, start, cancel


def ask_start(parent: QWidget | None, command: str, folder: str) -> bool:
    """Show the confirmation. True only when Start Zotero is chosen. Closing the dialog is a no."""
    box, start, _cancel = start_dialog(parent, command, folder)
    box.exec()
    return box.clickedButton() is start


def bridge_status_text(status: BridgeStatus | None) -> str:
    """The bridge line on the card. A version that is not known reads as "unknown". A recorded wait reads as the
    waiting sentence, whatever the state. An update ready while Zotero is open says so in plain words."""
    if status is None:
        return BRIDGE_NOT_CHECKED_TEXT
    if status.state == STATE_ERROR:
        return BRIDGE_ERROR_TEXT.format(status.reason)
    installed = status.installed or "unknown"
    expected = status.expected or "unknown"
    if status.state == STATE_NOT_INSTALLED:
        template = BRIDGE_NOT_INSTALLED_WAIT_TEXT if status.pending_install else BRIDGE_NOT_INSTALLED_TEXT
        return template.format(expected=expected)
    if status.pending_install:
        return BRIDGE_WAIT_PENDING_TEXT.format(installed=installed, expected=expected)
    if status.state == STATE_UPDATE_READY:
        return BRIDGE_UPDATE_READY_TEXT.format(installed=installed, expected=expected)
    if status.state == STATE_ZOTERO_RUNNING:
        return BRIDGE_ZOTERO_OPEN_TEXT.format(installed=installed, expected=expected)
    assert status.state == STATE_CURRENT
    return BRIDGE_CURRENT_TEXT.format(installed=installed, expected=expected)


def bridge_update_dialog(
    parent: QWidget | None,
) -> tuple[QMessageBox, QPushButton, QPushButton, QPushButton, QPushButton]:
    """The Update bridge dialog, built but not shown. Its buttons are, in order, Close Zotero politely, Force close
    Zotero, Wait until Zotero restarts and Cancel. All four are action buttons, which keeps this order in buttons().
    Cancel is the default and the Escape button, so Enter and Escape change nothing.
    """
    box = QMessageBox(parent)
    box.setWindowTitle(BRIDGE_DIALOG_TITLE)
    box.setIcon(QMessageBox.Icon.Question)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setText(BRIDGE_DIALOG_TEXT)
    box.setInformativeText(BRIDGE_DIALOG_DETAIL)
    polite = box.addButton(POLITE_BUTTON_TEXT, QMessageBox.ButtonRole.ActionRole)
    force = box.addButton(FORCE_BUTTON_TEXT, QMessageBox.ButtonRole.ActionRole)
    wait = box.addButton(WAIT_BUTTON_TEXT, QMessageBox.ButtonRole.ActionRole)
    cancel = box.addButton(CANCEL_BUTTON_TEXT, QMessageBox.ButtonRole.ActionRole)
    box.setDefaultButton(cancel)
    box.setEscapeButton(cancel)
    for child in box.findChildren(QLabel):
        child.setTextFormat(Qt.TextFormat.PlainText)
        make_selectable(child)
    return box, polite, force, wait, cancel


def ask_bridge_update(parent: QWidget | None) -> str | None:
    """Show the first dialog. Return the choice for its button, or None for Cancel or a closed dialog.
    Nothing is sent here.
    """
    box, polite, force, wait, _cancel = bridge_update_dialog(parent)
    box.exec()
    clicked = box.clickedButton()
    box.deleteLater()
    if clicked is polite:
        return CHOICE_POLITE
    if clicked is force:
        return CHOICE_FORCE
    if clicked is wait:
        return CHOICE_WAIT
    return None


def after_polite_dialog(parent: QWidget | None) -> tuple[QMessageBox, QPushButton, QPushButton, QPushButton]:
    """The dialog offered after a polite close left Zotero open, built but not shown. Its buttons are, in order, Force
    close Zotero, I will close Zotero myself, then try again, and Cancel. Cancel is the default and the Escape button.
    """
    box = QMessageBox(parent)
    box.setWindowTitle(AFTER_POLITE_TITLE)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setText(AFTER_POLITE_TEXT)
    box.setInformativeText(AFTER_POLITE_DETAIL)
    force = box.addButton(FORCE_BUTTON_TEXT, QMessageBox.ButtonRole.ActionRole)
    retry = box.addButton(RETRY_BUTTON_TEXT, QMessageBox.ButtonRole.ActionRole)
    cancel = box.addButton(CANCEL_BUTTON_TEXT, QMessageBox.ButtonRole.ActionRole)
    box.setDefaultButton(cancel)
    box.setEscapeButton(cancel)
    for child in box.findChildren(QLabel):
        child.setTextFormat(Qt.TextFormat.PlainText)
        make_selectable(child)
    return box, force, retry, cancel


def ask_after_polite_failure(parent: QWidget | None) -> str | None:
    """Show the follow-up. FOLLOW_FORCE or FOLLOW_RETRY for that button, and None for Cancel or a closed dialog."""
    box, force, retry, _cancel = after_polite_dialog(parent)
    box.exec()
    clicked = box.clickedButton()
    box.deleteLater()
    if clicked is force:
        return FOLLOW_FORCE
    if clicked is retry:
        return FOLLOW_RETRY
    return None


def ask_force_close(parent: QWidget) -> bool:
    """The second question before a force close. Its default and Escape are No, so only a click on Yes returns True."""
    return ask_yes_no(parent, FORCE_CONFIRM_TITLE, FORCE_CONFIRM_TEXT)


def update_result_text(choice: str, result: BridgeResult) -> str:
    """The card's line after a successful update. A wait always says it is waiting. Polite and force show the server's
    own line."""
    if choice == CHOICE_WAIT:
        return WAIT_RESULT_TEXT
    return result.line or BRIDGE_DONE_TEXT


def _bridge_status_read(bridge: Any, local_url: Callable[[], str]) -> BridgeStatus:
    """Runs on the pool. The Local URL is read here, so the UI thread never reads Settings."""
    return bridge.status(local_url())


def _bridge_update_read(bridge: Any, local_url: Callable[[], str], choice: str) -> BridgeResult:
    """Runs on the pool, and only after the owner has picked a choice. A force choice has also passed its second
    question."""
    return bridge.update(local_url(), choice)


@dataclass(frozen=True)
class _Done:
    """One finished read: its kind, the number it was started with, and the (result, failure_text) its job made."""

    kind: str
    generation: int
    payload: tuple


def _launch(client: Any, exe: str) -> tuple[str, bool]:
    """Start Zotero, then ask the local API once. Runs on a worker thread, and only after a yes."""
    line = client.start_zotero(exe)
    check = client.probe_api()
    return line, check.state == API_ON


class _Job(QRunnable):
    """Run ``work`` on the thread pool and deliver (result, failure_text).

    A ZoteroError keeps its fixed message. Any other exception is reduced to its class name, so
    no traceback reaches the UI.
    """

    def __init__(self, work: Callable[[], Any], deliver: Callable[[tuple], None]) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._work = work
        self._deliver = deliver

    def run(self) -> None:
        result: Any = None
        failure: str | None = None
        try:
            result = self._work()
        except ZoteroError as exc:
            failure = exc.message
        except Exception as exc:  # noqa: BLE001 - a worker must never raise into the pool
            failure = CHECK_FAILED_TEXT.format(exc.__class__.__name__)
        try:
            self._deliver((result, failure))
        except RuntimeError:
            pass  # the shared delivery object is gone, for example at shutdown


class ZoteroPanel(QWidget):
    """The Zotero tab: status, Start Zotero behind a confirmation, and a read-only library search."""

    def __init__(
        self,
        client: Any = zotero_client,
        confirm: Callable[[QWidget, str, str], bool] | None = None,
        parent: QWidget | None = None,
        *,
        bridge: Any | None = None,
        local_url: Callable[[], str] | None = None,
        ask_update: Callable[[QWidget], str | None] | None = None,
        ask_force: Callable[[QWidget], bool] | None = None,
        ask_after_polite: Callable[[QWidget], str | None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("zotero-panel")
        self._client = client
        self._confirm = ask_start if confirm is None else confirm
        # The bridge is on only when a bridge and a Local URL reader are both given. When it is off, the card shows no
        # bridge line, makes no bridge request and keeps Update bridge disabled. ``bridge`` has status(base_url) and
        # update(base_url, choice), and both run on the pool.
        self._bridge = bridge
        self._local_url = local_url
        self._bridge_on = bridge is not None and local_url is not None
        self._ask_update = ask_bridge_update if ask_update is None else ask_update
        self._ask_force = ask_force_close if ask_force is None else ask_force
        self._ask_after_polite = ask_after_polite_failure if ask_after_polite is None else ask_after_polite
        self._bridge_status: BridgeStatus | None = None
        self._bridge_acting = False
        self._bridge_choice = ""
        self._exe: str | None = None
        self._running: bool | None = None
        self._checking = False
        self._searching = False
        self._acting = False
        self._probed_on_show = False
        self._pending_query = ""
        # Identifies this panel's reads. A plain object, so a job can hold it without holding the panel.
        self._owner = object()
        # Each read takes the next number, and each kind keeps the number of its latest read. A result from an older
        # read of the same kind is dropped. Reads of different kinds never supersede each other.
        self._generation = 0
        self._latest: dict[str, int] = {}
        shared_delivery().delivered.connect(self._on_delivered)
        self._build()

    # ----------------------------------------------------------- layout

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        # The intro and the status lines are one block, so one drag over them selects them all.
        # The buttons stay below it.
        status = TextBlock(self, "zotero-status-text", spacing=layout.spacing())
        status.line("zotero-intro", INTRO_TEXT)
        self._installed = status.line("zotero-installed", NOT_CHECKED_TEXT)
        self._process = status.line("zotero-process", "")
        self._api = status.line("zotero-api", "")
        self._api_detail = status.line("zotero-api-detail", "")
        self._bridge_line = status.line("zotero-bridge", BRIDGE_NOT_CHECKED_TEXT if self._bridge_on else "")
        self._start_hint = status.line("zotero-start-hint", "")
        layout.addWidget(status.label)

        actions = QHBoxLayout()
        self._refresh_button = QPushButton("Refresh", self)
        self._refresh_button.setObjectName("zotero-refresh")
        self._start_button = QPushButton("Start Zotero...", self)
        self._start_button.setObjectName("zotero-start")
        self._start_button.setEnabled(False)
        actions.addWidget(self._refresh_button)
        actions.addWidget(self._start_button)
        # The bridge controls are built only when a bridge is given. Without one, the card is the same as before the
        # bridge.
        self._bridge_button: QPushButton | None = None
        self._bridge_result: QLabel | None = None
        if self._bridge_on:
            button = QPushButton(UPDATE_BUTTON_TEXT, self)
            button.setObjectName("zotero-bridge-update")
            button.setEnabled(False)
            button.clicked.connect(self._on_bridge_update_clicked)
            self._bridge_button = button
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self._start_result = _label(self, "zotero-start-result", "")
        layout.addWidget(self._start_result)
        if self._bridge_on:
            self._bridge_result = _label(self, "zotero-bridge-result", "")
            layout.addWidget(self._bridge_result)

        search_row = QHBoxLayout()
        self._search_input = QLineEdit(self)
        self._search_input.setObjectName("zotero-search-input")
        self._search_input.setPlaceholderText(SEARCH_PLACEHOLDER)
        self._search_button = QPushButton("Search", self)
        self._search_button.setObjectName("zotero-search-button")
        search_row.addWidget(self._search_input, 1)
        search_row.addWidget(self._search_button)
        layout.addLayout(search_row)

        self._search_status = _label(self, "zotero-search-status", SEARCH_HINT)
        layout.addWidget(self._search_status)

        self._results = QTableWidget(0, len(RESULT_COLUMNS), self)
        self._results.setObjectName("zotero-results")
        self._results.setHorizontalHeaderLabels(list(RESULT_COLUMNS))
        self._results.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._results.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self._results.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._results.verticalHeader().setVisible(False)
        # Title takes the spare width, so long titles have room. The other columns fit their contents, so the key is
        # never stretched wider than it needs to be.
        header = self._results.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(RESULT_TITLE_COLUMN, QHeaderView.ResizeMode.Stretch)
        for column in range(len(RESULT_COLUMNS)):
            if column != RESULT_TITLE_COLUMN:
                header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        enable_table_copy(self._results)
        layout.addWidget(self._results, 1)

        self._refresh_button.clicked.connect(self._on_refresh_clicked)
        self._start_button.clicked.connect(self._on_start_clicked)
        self._search_button.clicked.connect(self._on_search_clicked)
        self._search_input.returnPressed.connect(self._on_search_clicked)

    # ----------------------------------------------------------- status

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        if not self._probed_on_show:
            self._probed_on_show = True
            self._refresh()

    @Slot()
    def _on_refresh_clicked(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        if self._checking:
            return
        self._checking = True
        self._refresh_button.setEnabled(False)
        self._start_button.setEnabled(False)
        self._installed.setText(CHECKING_TEXT)
        self._process.setText("")
        self._api.setText("")
        self._api_detail.setText("")
        self._start_hint.setText("")
        self._start_read(STATUS, self._client.status)
        self._start_bridge_status()

    def _start_read(self, kind: str, work: Callable[[], Any]) -> None:
        """Start one read on the thread pool. Its result comes back to _on_delivered, tagged with kind and number."""
        self._generation += 1
        generation = self._generation
        self._latest[kind] = generation
        # The deliver closure captures only the owner, the kind, the number and the delivery object, never the panel.
        owner = self._owner
        delivery = shared_delivery()

        def deliver(payload: tuple) -> None:
            delivery.delivered.emit(Delivered(owner, _Done(kind, generation, payload)))

        QThreadPool.globalInstance().start(_Job(work, deliver))

    @Slot(object)
    def _on_delivered(self, item: object) -> None:
        # Runs on the GUI thread. A result for another panel is dropped here, and so is one a newer read of its kind
        # has superseded.
        if not isinstance(item, Delivered) or item.owner is not self._owner:
            return
        done = item.value
        if not isinstance(done, _Done) or done.generation != self._latest.get(done.kind):
            return
        if done.kind == STATUS:
            self._on_status_ready(done.payload)
        elif done.kind == SEARCH:
            self._on_search_ready(done.payload)
        elif done.kind == ACTION:
            self._on_action_ready(done.payload)
        elif done.kind == BRIDGE_STATUS:
            self._on_bridge_status_ready(done.payload)
        elif done.kind == BRIDGE_UPDATE:
            self._on_bridge_update_ready(done.payload)

    def _on_status_ready(self, payload: tuple) -> None:
        result, failure = payload
        self._checking = False
        self._refresh_button.setEnabled(True)
        if failure is not None or not isinstance(result, ZoteroStatus):
            self._exe = None
            self._running = None
            self._installed.setText(NOT_CHECKED_TEXT)
            self._process.setText("")
            self._api.setText(API_ERROR_TEXT)
            self._api_detail.setText(failure or CHECK_FAILED_TEXT.format("NoResult"))
            self._start_hint.setText("")
            self._start_button.setEnabled(False)
            return
        self._show_status(result)

    def _show_status(self, status: ZoteroStatus) -> None:
        self._exe = status.exe
        self._running = status.process_running
        self._installed.setText(NOT_INSTALLED_TEXT if status.exe is None else INSTALLED_TEXT.format(status.exe))
        self._process.setText(process_text(status.process_running))
        self._api.setText(api_text(status.api.state))
        self._api_detail.setText(status.api.message)
        self._update_start_button()

    def _update_start_button(self) -> None:
        if self._exe is None:
            hint, enabled = START_HINT_NOT_INSTALLED, False
        elif self._running is True:
            hint, enabled = START_HINT_RUNNING, False
        else:
            hint, enabled = START_HINT_READY, True
        self._start_hint.setText(hint)
        self._start_button.setEnabled(enabled and not self._acting)

    # ----------------------------------------------------------- start

    @Slot()
    def _on_start_clicked(self) -> None:
        exe = self._exe
        if exe is None or self._acting or self._running is True:
            return
        command = command_line(exe)
        folder = str(Path(exe).parent)
        if not self._confirm(self, command, folder):
            self._start_result.setText(START_CANCELLED_TEXT)
            return
        self._acting = True
        self._start_button.setEnabled(False)
        self._start_result.setText(STARTING_TEXT)
        self._start_read(ACTION, functools.partial(_launch, self._client, exe))

    def _on_action_ready(self, payload: tuple) -> None:
        result, failure = payload
        self._acting = False
        if failure is not None or not isinstance(result, tuple):
            self._start_result.setText(failure or CHECK_FAILED_TEXT.format("NoResult"))
            self._update_start_button()
            return
        line, ready = result
        ready_text = API_READY_TEXT if ready else API_NOT_READY_TEXT
        self._start_result.setText(f"{line}\n{ready_text}")
        self._refresh()

    # ----------------------------------------------------------- bridge

    def _start_bridge_status(self) -> None:
        """Check the bridge on the same schedule as the card. The check runs on the pool, and the line says so until
        it ends."""
        if not self._bridge_on:
            return
        self._bridge_status = None
        self._bridge_line.setText(BRIDGE_CHECKING_TEXT)
        self._update_bridge_button()
        self._start_read(BRIDGE_STATUS, functools.partial(_bridge_status_read, self._bridge, self._local_url))

    def _update_bridge_button(self) -> None:
        button = self._bridge_button
        if button is None:
            return
        status = self._bridge_status
        button.setEnabled(status is not None and status.can_update and not self._bridge_acting)

    def _on_bridge_status_ready(self, payload: tuple) -> None:
        result, failure = payload
        if failure is not None or not isinstance(result, BridgeStatus):
            self._bridge_status = BridgeStatus(STATE_ERROR, reason=failure or CHECK_FAILED_TEXT.format("NoResult"))
        else:
            self._bridge_status = result
        self._bridge_line.setText(bridge_status_text(self._bridge_status))
        self._update_bridge_button()

    @Slot()
    def _on_bridge_update_clicked(self) -> None:
        status = self._bridge_status
        if not self._bridge_on or self._bridge_acting or status is None or not status.can_update:
            return
        choice = self._ask_update(self)
        if choice not in CHOICES:
            return  # Cancel, or a dialog closed without a choice: nothing is sent and nothing changes
        self._send_bridge_choice(choice)

    def _send_bridge_choice(self, choice: str) -> None:
        """Send one choice to the bridge. A force close asks the second question first, and No sends nothing."""
        if choice == CHOICE_FORCE and not self._ask_force(self):
            return
        self._bridge_acting = True
        self._bridge_choice = choice
        self._update_bridge_button()
        self._bridge_result.setText(BRIDGE_SENDING_TEXT)
        self._start_read(BRIDGE_UPDATE, functools.partial(_bridge_update_read, self._bridge, self._local_url, choice))

    def _offer_after_polite_failure(self) -> None:
        """A polite close left Zotero open. Offer force, retry or cancel. Retry sends the polite choice again."""
        follow = self._ask_after_polite(self)
        if follow == FOLLOW_FORCE:
            self._send_bridge_choice(CHOICE_FORCE)
        elif follow == FOLLOW_RETRY:
            self._send_bridge_choice(CHOICE_POLITE)

    def _on_bridge_update_ready(self, payload: tuple) -> None:
        result, failure = payload
        self._bridge_acting = False
        if failure is not None or not isinstance(result, BridgeResult):
            self._bridge_result.setText(failure or BRIDGE_UPDATE_UNCHECKED_TEXT)
        elif not result.ok:
            self._bridge_result.setText(result.reason or BRIDGE_UPDATE_UNCHECKED_TEXT)
            if result.still_open and self._bridge_choice == CHOICE_POLITE:
                self._offer_after_polite_failure()
        else:
            self._bridge_result.setText(update_result_text(self._bridge_choice, result))
            self._refresh()
        self._update_bridge_button()

    # ----------------------------------------------------------- search

    @Slot()
    def _on_search_clicked(self) -> None:
        query = self._search_input.text().strip()
        if not query:
            self._search_status.setText(BLANK_TEXT)
            return
        if self._searching:
            return
        self._searching = True
        self._pending_query = query
        self._search_button.setEnabled(False)
        self._search_status.setText(SEARCHING_TEXT)
        self._results.setRowCount(0)
        self._start_read(SEARCH, functools.partial(self._client.search_items, query))

    def _on_search_ready(self, payload: tuple) -> None:
        result, failure = payload
        self._searching = False
        self._search_button.setEnabled(True)
        if failure is not None:
            self._results.setRowCount(0)
            self._search_status.setText(failure)
            return
        items: list[ZoteroItem] = list(result or [])
        self._fill_results(items)
        self._search_status.setText(results_text(self._pending_query, len(items)))

    def _fill_results(self, items: list[ZoteroItem]) -> None:
        self._results.setRowCount(len(items))
        for row, item in enumerate(items):
            for column, value in enumerate((item.title, item.authors, item.year, item.key)):
                cell = QTableWidgetItem(value)
                cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._results.setItem(row, column, cell)


__all__ = [
    "AFTER_POLITE_TEXT",
    "API_TEXT",
    "BRIDGE_DIALOG_TEXT",
    "BRIDGE_NOT_CHECKED_TEXT",
    "CANCEL_BUTTON_TEXT",
    "CHECKING_TEXT",
    "FOLLOW_FORCE",
    "FOLLOW_RETRY",
    "FORCE_BUTTON_TEXT",
    "FORCE_CONFIRM_TEXT",
    "NOT_CHECKED_TEXT",
    "POLITE_BUTTON_TEXT",
    "RETRY_BUTTON_TEXT",
    "START_CANCELLED_TEXT",
    "WAIT_BUTTON_TEXT",
    "WAIT_RESULT_TEXT",
    "ZoteroPanel",
    "api_text",
    "after_polite_dialog",
    "ask_after_polite_failure",
    "ask_bridge_update",
    "ask_force_close",
    "ask_start",
    "bridge_status_text",
    "bridge_update_dialog",
    "process_text",
    "results_text",
    "start_dialog",
    "update_result_text",
]
