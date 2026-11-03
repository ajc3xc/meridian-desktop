"""Zotero tab: whether Zotero is installed, running and answering, a Start Zotero button, and a library search.

This is Qt code and sits in ui/, so the boundary check allows it. The tab makes no request when it
is built. It checks Zotero when first shown and when Refresh is pressed. Each check, each search and
the start action run on the thread pool, so the UI never blocks. Zotero starts only after the owner
picks Start Zotero in a dialog whose default button is Cancel. Results are plain text (PlainText
labels and plain table cells), so Zotero's text is never read as HTML. The results table is read-only
and has the shared Copy action (Ctrl+C or the right-click menu). The save section at the foot sends lines of
reading-list items through the Meridian Zotero bridge. The bridge check runs with the other checks, and Save runs on the
thread pool as well. A line whose outcome is unknown is held back until the person checks Zotero and presses I checked Zotero.
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
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from meridian_desktop import zotero_bridge, zotero_client, zotero_journal
from meridian_desktop.ui.delivery import Delivered, shared_delivery
from meridian_desktop.ui.text import enable_table_copy, make_selectable
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
SAVE_HEADING = "Save to Zotero"
SAVE_INTRO = (
    "One item per line, as Title | URL. The items go into your Zotero library through the Meridian Zotero bridge. "
    "Nothing is deleted."
)
SAVE_PLACEHOLDER = "Title | https://example.org/page"
SAVE_BUTTON_TEXT = "Save to Zotero"
SAVING_TEXT = "Saving to Zotero through the Meridian bridge..."
SAVE_NO_RESULT_TEXT = (
    "The save ended without a result, so every line is held back. Check Zotero before you save again."
)
SAVE_HELD_TEXT = "Save is off until you check Zotero for the held lines and press I checked Zotero."
HELD_HEADING = (
    "Held back: these may already be in Zotero, so they are not retried. Check Zotero, then press I checked Zotero."
)
HELD_BUTTON_TEXT = "I checked Zotero"
HELD_RELEASED_TEXT = "Released. Nothing was retried."
BRIDGE_CHECKING_TEXT = "Bridge: checking..."
# The kinds of read the panel runs. Each kind has at most one read in flight.
STATUS = "status"
SEARCH = "search"
ACTION = "action"
BRIDGE = "bridge"
SAVE = "save"


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


class _JournalProgress:
    """Writes each item's outcome to the journal as it happens. It runs on the save's worker thread.

    An item is recorded as writing before its POST, and as landed, unknown or refused once its outcome is known. A quit in
    between leaves the item writing, and the next launch holds it.
    """

    def __init__(self, journal: zotero_journal.SaveJournal, lines: list[str]) -> None:
        self._journal = journal
        self._lines = lines
        self._ids: dict[int, str] = {}

    def started(self, index: int) -> None:
        self._ids[index] = self._journal.begin(self._lines[index])

    def finished(self, index: int, outcome: str) -> None:
        self._journal.finish(self._ids[index], outcome)


class ZoteroPanel(QWidget):
    """The Zotero tab: status, Start Zotero behind a confirmation, and a read-only library search."""

    def __init__(
        self,
        client: Any = zotero_client,
        confirm: Callable[[QWidget, str, str], bool] | None = None,
        parent: QWidget | None = None,
        bridge: Any = zotero_bridge,
        journal_path: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("zotero-panel")
        self._client = client
        self._bridge = bridge
        self._saving = False
        self._pending_lines: list[str] = []
        # The save journal: the held lines (outcome unknown) and whether saving is possible at all.
        self._journal: zotero_journal.SaveJournal | None = None
        self._journal_error: str | None = None
        self._held_ids: list[str] = []
        path = zotero_journal.default_path() if journal_path is None else journal_path
        try:
            self._journal = zotero_journal.SaveJournal(path)
        except zotero_journal.JournalError as exc:
            self._journal_error = exc.message
        self._confirm = ask_start if confirm is None else confirm
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
        self._refresh_held()
        if self._journal_error is not None:
            self._save_result.setText(self._journal_error)

    # ----------------------------------------------------------- layout

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.addWidget(_label(self, "zotero-intro", INTRO_TEXT))

        self._installed = _label(self, "zotero-installed", NOT_CHECKED_TEXT)
        self._process = _label(self, "zotero-process", "")
        self._api = _label(self, "zotero-api", "")
        self._api_detail = _label(self, "zotero-api-detail", "")
        self._start_hint = _label(self, "zotero-start-hint", "")
        for label in (self._installed, self._process, self._api, self._api_detail, self._start_hint):
            layout.addWidget(label)

        actions = QHBoxLayout()
        self._refresh_button = QPushButton("Refresh", self)
        self._refresh_button.setObjectName("zotero-refresh")
        self._start_button = QPushButton("Start Zotero...", self)
        self._start_button.setObjectName("zotero-start")
        self._start_button.setEnabled(False)
        actions.addWidget(self._refresh_button)
        actions.addWidget(self._start_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self._start_result = _label(self, "zotero-start-result", "")
        layout.addWidget(self._start_result)

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
        self._results.horizontalHeader().setStretchLastSection(True)
        enable_table_copy(self._results)
        layout.addWidget(self._results, 1)

        save_box = QVBoxLayout()
        save_box.addWidget(_label(self, "zotero-save-heading", SAVE_HEADING))
        save_box.addWidget(_label(self, "zotero-save-intro", SAVE_INTRO))
        self._save_input = QPlainTextEdit(self)
        self._save_input.setObjectName("zotero-save-input")
        self._save_input.setPlaceholderText(SAVE_PLACEHOLDER)
        self._save_input.setMinimumHeight(90)
        save_box.addWidget(self._save_input)
        save_row = QHBoxLayout()
        self._save_button = QPushButton(SAVE_BUTTON_TEXT, self)
        self._save_button.setObjectName("zotero-save-button")
        self._bridge_state = _label(self, "zotero-bridge-state", BRIDGE_CHECKING_TEXT)
        save_row.addWidget(self._save_button)
        save_row.addWidget(self._bridge_state, 1)
        save_box.addLayout(save_row)
        self._save_result = _label(self, "zotero-save-result", "")
        save_box.addWidget(self._save_result)
        self._held_heading = _label(self, "zotero-held-heading", HELD_HEADING)
        self._held_lines = QPlainTextEdit(self)
        self._held_lines.setObjectName("zotero-held-lines")
        self._held_lines.setReadOnly(True)
        make_selectable(self._held_lines)
        self._held_lines.setMinimumHeight(60)
        self._held_button = QPushButton(HELD_BUTTON_TEXT, self)
        self._held_button.setObjectName("zotero-held-checked")
        for widget in (self._held_heading, self._held_lines, self._held_button):
            save_box.addWidget(widget)
            widget.setVisible(False)
        layout.addLayout(save_box)

        self._refresh_button.clicked.connect(self._on_refresh_clicked)
        self._start_button.clicked.connect(self._on_start_clicked)
        self._search_button.clicked.connect(self._on_search_clicked)
        self._search_input.returnPressed.connect(self._on_search_clicked)
        self._save_button.clicked.connect(self._on_save_clicked)
        self._held_button.clicked.connect(self._on_held_checked_clicked)

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
        self._start_bridge_check()

    def _start_bridge_check(self) -> None:
        self._bridge_state.setText(BRIDGE_CHECKING_TEXT)
        self._start_read(BRIDGE, self._bridge.bridge_status)

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
        elif done.kind == BRIDGE:
            self._on_bridge_ready(done.payload)
        elif done.kind == SAVE:
            self._on_save_ready(done.payload)

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

    # ----------------------------------------------------------- bridge and save

    def _on_bridge_ready(self, payload: tuple) -> None:
        result, failure = payload
        if failure is not None or not isinstance(result, zotero_bridge.BridgeStatus):
            self._bridge_state.setText(failure or CHECK_FAILED_TEXT.format("NoResult"))
            return
        self._bridge_state.setText(result.message)

    def _refresh_held(self) -> None:
        """Show the held lines from the journal. They survive quitting, so they are shown again on every launch."""
        held = self._journal.held() if self._journal is not None else []
        self._held_ids = [entry.id for entry in held]
        self._held_lines.setPlainText("\n".join(entry.line for entry in held))
        for widget in (self._held_heading, self._held_lines, self._held_button):
            widget.setVisible(bool(held))
        self._update_save_enabled()

    def _update_save_enabled(self) -> None:
        """Save is off while the journal cannot be used, while any line is held, and while a save is running."""
        self._save_button.setEnabled(self._journal is not None and not self._held_ids and not self._saving)

    def _held_contains(self, line: str) -> bool:
        if self._journal is None:
            return False
        return any(entry.line == line for entry in self._journal.held())

    @Slot()
    def _on_held_checked_clicked(self) -> None:
        if self._journal is None:
            return
        try:
            self._journal.clear(self._held_ids)
        except zotero_journal.JournalError as exc:
            self._save_result.setText(exc.message)
            return
        self._refresh_held()
        self._save_result.setText(HELD_RELEASED_TEXT)

    @Slot()
    def _on_save_clicked(self) -> None:
        if self._saving:
            return
        if self._journal is None:
            self._save_result.setText(self._journal_error or SAVE_HELD_TEXT)
            return
        if self._held_ids:
            self._save_result.setText(SAVE_HELD_TEXT)
            return
        text = self._save_input.toPlainText()
        try:
            items = self._bridge.parse_lines(text)
        except zotero_bridge.BridgeInputError as exc:
            self._save_result.setText(exc.message)
            return
        # parse_lines makes one item per non-blank line, in order, so these lines line up with the items.
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        self._pending_lines = lines
        self._saving = True
        self._save_input.setReadOnly(True)
        self._update_save_enabled()
        self._save_result.setText(SAVING_TEXT)
        progress = _JournalProgress(self._journal, lines)
        self._start_read(SAVE, functools.partial(self._bridge.save_items, items, progress=progress))

    def _on_save_ready(self, payload: tuple) -> None:
        result, failure = payload
        lines, self._pending_lines = self._pending_lines, []
        self._saving = False
        self._save_input.setReadOnly(False)
        if failure is None and isinstance(result, zotero_bridge.SaveResult):
            # The first `saved` lines landed in Zotero, so they leave the box. An unknown line is held in the journal and
            # leaves the box too. The lines after it were not sent, so they stay.
            held_index = result.saved if result.state == zotero_bridge.UNKNOWN and result.saved < len(lines) else None
            rest = lines[result.saved + (1 if held_index is not None else 0) :]
            if held_index is not None and not self._held_contains(lines[held_index]):
                self._record_unknown([lines[held_index]])
            self._save_input.setPlainText("\n".join(rest))
            self._save_result.setText(result.message)
        else:
            # No result came back, so nothing is known about any line. Every line is held back, and the box is cleared.
            self._record_unknown(lines)
            self._save_input.setPlainText("")
            self._save_result.setText(SAVE_NO_RESULT_TEXT)
        self._refresh_held()
        self._start_bridge_check()

    def _record_unknown(self, lines: list[str]) -> None:
        if self._journal is None:
            return
        try:
            self._journal.record_unknown(lines)
        except zotero_journal.JournalError as exc:
            self._save_result.setText(exc.message)


__all__ = [
    "API_TEXT",
    "CHECKING_TEXT",
    "NOT_CHECKED_TEXT",
    "START_CANCELLED_TEXT",
    "ZoteroPanel",
    "api_text",
    "ask_start",
    "process_text",
    "results_text",
    "start_dialog",
]
