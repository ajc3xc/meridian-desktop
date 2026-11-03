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
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QRunnable, Qt, QThreadPool, QTimer, Slot
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
STILL_WRITING_TEXT = (
    "Released the lines Zotero confirmed. Lines still being written in another window stay held until they finish."
)
BRIDGE_CHECKING_TEXT = "Bridge: checking..."
JOURNAL_LOADING_TEXT = "The save journal is still loading. Save is off until it is read."
JOURNAL_CHECK_FAILED_TEXT = "The save journal could not be checked, so saving is off."
WRITING_ELSEWHERE_TEXT = (
    "Another window is still writing a line, so it stays held until that write ends. "
    "If no other window is open, restart the desktop."
)
# A busy journal is tried again after this long, so a window that opened while another held the lock recovers.
JOURNAL_RETRY_MS = 2000
# The kinds of read the panel runs. Each kind has at most one read in flight.
STATUS = "status"
SEARCH = "search"
ACTION = "action"
BRIDGE = "bridge"
SAVE = "save"
JOURNAL = "journal"
CLEAR = "clear"


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


@dataclass(frozen=True)
class _JournalState:
    """What a read or a release of the journal found: the open journal, the held entries if the read finished, and any
    problem. ``held_lines`` names the lines a backup holds, when the journal cannot be read."""

    journal: zotero_journal.SaveJournal | None
    held: tuple[zotero_journal.JournalEntry, ...] | None
    problem: str | None = None
    busy: bool = False
    held_lines: tuple[str, ...] = ()


@dataclass(frozen=True)
class _SaveDone:
    """What a save run decided, on the worker thread: the result text, how much of the box to keep, the held entries once
    the run has written them, and any journal problem."""

    message: str
    keep_from: int
    held: tuple[zotero_journal.JournalEntry, ...] | None
    problem: str | None = None
    busy: bool = False


def _journal_state(path: Path, journal: zotero_journal.SaveJournal | None) -> _JournalState:
    """Open the journal if it is not open yet, and read what is held. Runs on a worker thread."""
    try:
        if journal is None:
            journal = zotero_journal.SaveJournal(path, read_timeout=zotero_journal.UI_READ_TIMEOUT_SECONDS)
        held = journal.held()
    except zotero_journal.JournalError as exc:
        return _JournalState(journal, None, exc.message, exc.busy, exc.held_lines)
    return _JournalState(journal, tuple(held))


def _clear_held(journal: zotero_journal.SaveJournal, entry_ids: list[str]) -> _JournalState:
    """Release the unknown entries the person checked, then read what is still held. Runs on a worker thread."""
    try:
        journal.clear(entry_ids)
        held = journal.held()
    except zotero_journal.JournalError as exc:
        return _JournalState(journal, None, exc.message, exc.busy, exc.held_lines)
    return _JournalState(journal, tuple(held))


def _hold_line(journal: zotero_journal.SaveJournal, line: str) -> tuple[str | None, bool]:
    """Make sure ``line`` is held. Returns the problem and whether it was busy; both empty when the line is held."""
    try:
        if any(entry.line == line for entry in journal.held()):
            return None, False
        journal.record_unknown([line])
    except zotero_journal.JournalError as exc:
        return exc.message, exc.busy
    return None, False


def _hold_lines(journal: zotero_journal.SaveJournal, lines: list[str]) -> tuple[str | None, bool]:
    """Hold every line of a save that produced no result. Returns the problem and whether it was busy."""
    try:
        journal.record_unknown(lines)
    except zotero_journal.JournalError as exc:
        return exc.message, exc.busy
    return None, False


def _record_save(journal: zotero_journal.SaveJournal, lines: list[str], result: Any) -> _SaveDone:
    """Hold what the save left unknown, then read what is held. A line leaves the box only when its journal entry was
    written, so a line whose entry failed stays in the box and is not posted again."""
    problem, busy = None, False
    if result is None:
        problem, busy = _hold_lines(journal, lines)
        keep_from = 0 if problem is not None else len(lines)
        base = SAVE_NO_RESULT_TEXT
    elif result.state == zotero_bridge.UNKNOWN and result.saved < len(lines):
        problem, busy = _hold_line(journal, lines[result.saved])
        keep_from = result.saved if problem is not None else result.saved + 1
        base = result.message
    else:
        keep_from = result.saved
        base = result.message
    message = base if problem is None else f"{problem} {base}"
    try:
        held = tuple(journal.held())
    except zotero_journal.JournalError as exc:
        return _SaveDone(message, keep_from, None, problem or exc.message, busy if problem else exc.busy)
    return _SaveDone(message, keep_from, held, problem, busy)


def _run_save(journal: zotero_journal.SaveJournal, bridge: Any, items: list, lines: list[str]) -> _SaveDone:
    """One save, on a worker thread: the guard, the bridge, and the journal entries for what it did.

    The guard reads the journal under its lock, so a window whose list is out of date still refuses a line another window
    is writing. _JournalProgress writes each item's entry as the item starts and as its outcome is known.
    """
    try:
        held = journal.held()
    except zotero_journal.JournalError as exc:
        return _SaveDone(exc.message, 0, None, exc.message, exc.busy)
    if held:
        return _SaveDone(SAVE_HELD_TEXT, 0, tuple(held))
    progress = _JournalProgress(journal, lines)
    try:
        result = bridge.save_items(items, progress=progress)
    except Exception:  # noqa: BLE001 - a save that raises has no result, so every line is held back
        result = None
    return _record_save(journal, lines, result)


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
        # The save journal. It is opened on a worker thread, so until it is open and read, Save is off.
        self._journal: zotero_journal.SaveJournal | None = None
        self._journal_path = zotero_journal.default_path() if journal_path is None else journal_path
        self._journal_loaded = False
        # A journal failure that stays for the rest of this run, so Save stays off. A busy journal is tried again.
        self._journal_problem: str | None = None
        self._journal_busy: str | None = None
        self._journal_retry = QTimer(self)
        self._journal_retry.setSingleShot(True)
        self._journal_retry.timeout.connect(self._start_journal_read)
        # The held entries from the last read that finished. A read that fails keeps them on screen.
        self._held_entries: list[zotero_journal.JournalEntry] = []
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
        self._start_journal_read()

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
        self._held_note = _label(self, "zotero-held-note", "")
        self._held_button = QPushButton(HELD_BUTTON_TEXT, self)
        self._held_button.setObjectName("zotero-held-checked")
        for widget in (self._held_heading, self._held_lines, self._held_note, self._held_button):
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
        elif done.kind == JOURNAL:
            self._on_journal_ready(done.payload)
        elif done.kind == CLEAR:
            self._on_clear_ready(done.payload)

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

    # ----------------------------------------------------------- journal

    def _start_journal_read(self) -> None:
        """Open the journal if it is not open yet, and read what is held. The read runs on a worker thread."""
        self._start_read(JOURNAL, functools.partial(_journal_state, self._journal_path, self._journal))

    def _on_journal_ready(self, payload: tuple) -> None:
        state, failure = payload
        if failure is not None or not isinstance(state, _JournalState):
            state = _JournalState(self._journal, None, failure or JOURNAL_CHECK_FAILED_TEXT)
        self._apply_journal_state(state)

    def _apply_journal_state(self, state: _JournalState) -> None:
        """Show what a read of the journal found. A read that did not finish keeps the held entries of the last one that did."""
        self._journal_loaded = True
        if state.journal is not None:
            self._journal = state.journal
        if state.problem is None:
            self._journal_busy = None
        else:
            self._note_journal_problem(state.problem, state.busy)
            self._save_result.setText(state.problem)
        if state.held is not None:
            self._show_held(state.held)
        elif state.held_lines and self._journal is None:
            # The journal cannot be read, so the lines its backup holds are shown. Nothing can release them from here.
            self._show_held_lines(state.held_lines, can_clear=False)
        self._update_save_enabled()

    def _note_journal_problem(self, problem: str, busy: bool) -> None:
        """A busy journal is tried again in a moment. Any other problem stays for the rest of this run."""
        if busy:
            self._journal_busy = problem
            self._journal_retry.start(JOURNAL_RETRY_MS)
        else:
            self._journal_problem = problem
        self._update_save_enabled()

    def _show_held(self, entries: Sequence[zotero_journal.JournalEntry]) -> None:
        """Show the held lines, read from the journal on disk. They survive quitting, so they show again on every launch."""
        self._held_entries = list(entries)
        self._show_held_lines([entry.line for entry in self._held_entries], can_clear=bool(self._held_entries))
        writing = any(entry.outcome == zotero_journal.WRITING for entry in self._held_entries)
        self._held_note.setText(WRITING_ELSEWHERE_TEXT if writing else "")
        self._held_note.setVisible(writing)
        self._update_save_enabled()

    def _show_held_lines(self, lines: Sequence[str], *, can_clear: bool) -> None:
        self._held_lines.setPlainText("\n".join(lines))
        shown = bool(lines)
        self._held_heading.setVisible(shown)
        self._held_lines.setVisible(shown)
        self._held_button.setVisible(shown and can_clear)

    def _update_save_enabled(self) -> None:
        """Save is off while the journal is not open or cannot be used, while it is busy, while any line is held, and while
        a save is running."""
        self._save_button.setEnabled(
            self._journal is not None
            and self._journal_problem is None
            and self._journal_busy is None
            and not self._held_entries
            and not self._saving
        )

    @Slot()
    def _on_held_checked_clicked(self) -> None:
        if self._journal is None:
            return
        entry_ids = [entry.id for entry in self._held_entries]
        self._start_read(CLEAR, functools.partial(_clear_held, self._journal, entry_ids))

    def _on_clear_ready(self, payload: tuple) -> None:
        state, failure = payload
        if failure is not None or not isinstance(state, _JournalState):
            state = _JournalState(self._journal, None, failure or JOURNAL_CHECK_FAILED_TEXT)
        self._apply_journal_state(state)
        if state.problem is None:
            still_writing = any(entry.outcome == zotero_journal.WRITING for entry in state.held or ())
            self._save_result.setText(STILL_WRITING_TEXT if still_writing else HELD_RELEASED_TEXT)

    @Slot()
    def _on_save_clicked(self) -> None:
        if self._saving:
            return
        if self._journal is None:
            self._save_result.setText(self._journal_problem or self._journal_busy or JOURNAL_LOADING_TEXT)
            return
        if self._journal_problem is not None:
            self._save_result.setText(self._journal_problem)
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
        # The guard, the bridge and every journal write run on the worker, so the window never waits on the journal lock.
        self._start_read(SAVE, functools.partial(_run_save, self._journal, self._bridge, items, lines))

    def _on_save_ready(self, payload: tuple) -> None:
        done, failure = payload
        lines, self._pending_lines = self._pending_lines, []
        self._saving = False
        self._save_input.setReadOnly(False)
        if failure is not None or not isinstance(done, _SaveDone):
            # The save ended without a result the worker could record. Every line stays in the box, and the journal is read
            # again, so that whatever it did record is held.
            self._save_input.setPlainText("\n".join(lines))
            self._save_result.setText(SAVE_NO_RESULT_TEXT)
            self._start_journal_read()
            self._update_save_enabled()
            self._start_bridge_check()
            return
        # The lines before keep_from landed, or their journal entry was written, so they leave the box. The rest stay.
        self._save_input.setPlainText("\n".join(lines[done.keep_from :]))
        self._save_result.setText(done.message)
        if done.problem is not None:
            self._note_journal_problem(done.problem, done.busy)
        if done.held is not None:
            self._show_held(done.held)
        self._update_save_enabled()
        self._start_bridge_check()


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
