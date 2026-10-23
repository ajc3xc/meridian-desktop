"""Read-only Sessions tab: a project picker, a Refresh button, a table of the project's sessions, and a status line.

This is Qt code and sits in ui/, so the boundary check allows it. The panel has no start, stop, restart, send or
edit control. Its inputs are the project picker and the Refresh button.

Every read runs on the thread pool, so a slow server never blocks the window. Each read has a generation number,
and a result from an older read is dropped once a newer one has started. There are two kinds of read:

- A full read lists the projects, then reads the sessions of the picked project. It runs the first time the tab is
  shown, and when Refresh is pressed. The sessions come from the local server first. The hosted host is asked only
  when the local server is not running and a hosted token is saved. The Refresh button is disabled while a full
  read runs.
- A local read asks the local server only, for the picked project, and only in local mode. It never lists the
  projects and never calls the hosted host. The 5-second timer starts one on each tick while the tab is visible,
  and a change of project starts one. A local server that does not answer gives nothing new, so the table and the
  status line keep what they show.

Outside local mode a change of project clears the table and says to press Refresh. It reads nothing. The status
line names the source the rows came from.

The four providers are called on the worker thread, never on the UI thread:
- local_url_provider() -> str: the local Meridian server address from Settings.
- hosted_provider() -> (hosted_url, token or None): the hosted address from Settings and the saved token from the
  keychain. The panel passes the token to the client for one request and keeps no copy. It is never shown.
- project_provider() -> sequence of (project_id, name) pairs: fills the picker. Only a full read calls it.
- mode_provider() -> str: the mode saved in Settings. Only "local" allows a local read.

Only names, status, last seen (as an age) and the client name reach the table. Session summaries, task log bodies
and notes are never read by the client, so they cannot reach this panel.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from PySide6.QtCore import QRunnable, Qt, QThreadPool, QTimer, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from meridian_desktop import hosted_client
from meridian_desktop.sessions_client import (
    ERROR,
    NO_PROJECT,
    NOT_RUNNING,
    SESSIONS,
    SOURCE_HOSTED,
    SOURCE_LOCAL,
    SessionRow,
    SessionsResult,
    hosted_sessions,
    last_seen_age,
    local_sessions,
)
from meridian_desktop.ui.delivery import Delivered, shared_delivery
from meridian_desktop.ui.text import enable_table_copy, make_selectable

REFRESH_MS = 5000
LOCAL_MODE = "local"
COLUMNS = ("Name", "Status", "Last seen", "Client")
INTRO_TEXT = "Sessions for the chosen project. Read-only: names, status, last seen and client only."
PROJECT_CAPTION = "Project"
REFRESH_TEXT = "Refresh"
LOADING_TEXT = "Loading sessions..."
PICK_TEXT = "Pick a project to see its sessions."
PRESS_REFRESH_TEXT = "Press Refresh to read this project's sessions."
NO_PROJECTS_TEXT = "No projects are available to pick. Create a project in Meridian first."
PROJECTS_FAILED_TEXT = "The projects could not be read. Check the local server or the database file in Settings."
READ_FAILED_TEXT = "The sessions could not be read. Check the local server or the hosted token in Settings."
# The desktop has no read-only process check for Claude Desktop or Claude Code yet, so this line says so and lists none.
CLAUDE_PROCESSES_TEXT = (
    "Running Claude Desktop and Claude Code processes on this computer: Not connected. "
    "This desktop has no read-only check for them yet, so none are listed here."
)
UNNAMED_TEXT = "(unnamed)"
SOURCE_TEXT = {SOURCE_LOCAL: "this computer's Meridian server", SOURCE_HOSTED: "hosted Meridian"}


@dataclass(frozen=True)
class _Snapshot:
    """One finished read. ``choices`` is None when the picker is left alone. ``result`` is None when there is nothing
    new to show: a local read whose server did not answer, or one that ran outside local mode."""

    generation: int
    choices: tuple[tuple[str, str], ...] | None
    project_id: str
    result: SessionsResult | None


# ----------------------------------------------------------------- pure helpers


def choose_project(selected: str, choices: Sequence[tuple[str, str]]) -> str:
    """Keep the selected project when it is still listed. Otherwise take the first one. Empty when none is listed."""
    ids = [pid for pid, _name in choices]
    if selected in ids:
        return selected
    return ids[0] if ids else ""


def status_text(result: SessionsResult, checked_at: str) -> str:
    """The status line for one result. ``checked_at`` is the local time the result arrived."""
    if result.state == NO_PROJECT:
        return result.message
    if result.state == SESSIONS:
        where = SOURCE_TEXT.get(result.source, "Meridian")
        if result.rows:
            return f"{len(result.rows)} session(s) from {where}. Checked at {checked_at}."
        return f"No sessions from {where} right now. Checked at {checked_at}."
    return f"{result.message} Checked at {checked_at}."


def row_cells(row: SessionRow, now: datetime) -> tuple[str, str, str, str]:
    """The four table cells for one session, in COLUMNS order. Last seen is shown as an age, not a timestamp."""
    return (row.name or UNNAMED_TEXT, row.status, last_seen_age(row.last_seen, now), row.client)


def _plain(value: Any) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _label(parent: QWidget, name: str, text: str) -> QLabel:
    label = make_selectable(QLabel(text, parent))
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


# ----------------------------------------------------------------- worker


class _RefreshJob(QRunnable):
    """Run one read on the thread pool and deliver a _Snapshot.

    A failure becomes an error result with fixed text. The text names no exception class and no value, so a raw
    exception never reaches the UI thread.
    """

    def __init__(self, generation: int, work: Callable[[], _Snapshot], deliver: Callable[[_Snapshot], None]) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._generation = generation
        self._work = work
        self._deliver = deliver

    def run(self) -> None:
        try:
            snapshot = self._work()
        except Exception:  # noqa: BLE001 - a worker must never raise into the pool
            failed = SessionsResult(ERROR, READ_FAILED_TEXT)
            snapshot = _Snapshot(self._generation, None, "", failed)
        try:
            self._deliver(snapshot)
        except RuntimeError:
            pass  # the panel was deleted while the read ran


# ----------------------------------------------------------------- reads


class _Reads:
    """The reads the Sessions tab runs on the worker thread, through the providers Settings gives it.

    It holds no widget. A read that is still running when the tab is deleted therefore never keeps the tab
    alive, and never touches it. The tab holds one of these and hands its reads to the thread pool.
    """

    def __init__(
        self,
        local_url_provider: Callable[[], str],
        hosted_provider: Callable[[], tuple[str, str | None]],
        project_provider: Callable[[], Sequence[tuple[str, str]]],
        mode_provider: Callable[[], str],
        local_transport: Any,
        hosted_transport: hosted_client.Transport | None,
    ) -> None:
        self._local_url = local_url_provider
        self._hosted = hosted_provider
        self._projects = project_provider
        self._mode = mode_provider
        self._local_transport = local_transport
        self._hosted_transport = hosted_transport

    def collect_full(self, generation: int, selected: str) -> _Snapshot:
        """Runs on a worker. Lists the projects once, then reads the sessions of the chosen project."""
        choices, failure = self._read_projects()
        chosen = selected if choices is None else choose_project(selected, choices)
        if not chosen:
            if failure:
                message = failure
            elif choices == ():
                message = NO_PROJECTS_TEXT
            else:
                message = PICK_TEXT
            return _Snapshot(generation, choices, "", SessionsResult(NO_PROJECT, message))
        return _Snapshot(generation, choices, chosen, self._read_sessions(chosen))

    def collect_local(self, generation: int, selected: str) -> _Snapshot:
        """Runs on a worker. A local read: the local server only, for the picked project, and only in local mode.

        It never lists the projects and never calls the hosted host. When the local server does not answer, there is
        nothing new to show, so the result is None and the table and status line keep what they show.
        """
        if not selected or self._mode() != LOCAL_MODE:
            return _Snapshot(generation, None, "", None)
        result = local_sessions(self._local_url(), selected, transport=self._local_transport)
        if result.state == NOT_RUNNING:
            return _Snapshot(generation, None, selected, None)
        return _Snapshot(generation, None, selected, result)

    def _read_projects(self) -> tuple[tuple[tuple[str, str], ...] | None, str]:
        """(choices, "") on success. (None, message) when the project list could not be read."""
        try:
            pairs = [(pid, name) for pid, name in self._projects() if isinstance(pid, str) and pid.strip()]
        except Exception:  # noqa: BLE001 - a failed read is named in plain words, never by class or value
            return None, PROJECTS_FAILED_TEXT
        return tuple((pid.strip(), _plain(name) or pid.strip()) for pid, name in pairs), ""

    def _read_sessions(self, project_id: str) -> SessionsResult:
        """The local server first. Hosted only when the local server is not running and a token is saved."""
        local = local_sessions(self._local_url(), project_id, transport=self._local_transport)
        if local.state != NOT_RUNNING:
            return local
        hosted_url, token = self._hosted()
        if not isinstance(token, str) or not token.strip():
            return local
        return hosted_sessions(hosted_url, project_id, token, transport=self._hosted_transport)


# ----------------------------------------------------------------- panel


class SessionsPanel(QWidget):
    """The Sessions tab: the picked project's sessions. Read on first show and on Refresh, and locally on a timer."""

    def __init__(
        self,
        local_url_provider: Callable[[], str],
        hosted_provider: Callable[[], tuple[str, str | None]],
        project_provider: Callable[[], Sequence[tuple[str, str]]],
        mode_provider: Callable[[], str],
        parent: QWidget | None = None,
        *,
        local_transport: Any = None,
        hosted_transport: hosted_client.Transport | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("sessions-panel")
        self._reads = _Reads(
            local_url_provider,
            hosted_provider,
            project_provider,
            mode_provider,
            local_transport,
            hosted_transport,
        )
        # Identifies this tab's reads. A plain object, so a job can hold it without holding the tab.
        self._owner = object()
        self._generation = 0
        self._in_flight: int | None = None
        self._read_once = False
        shared_delivery().delivered.connect(self._on_delivered)
        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self._on_tick)
        self._build()

    # ----------------------------------------------------------- layout

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.addWidget(_label(self, "sessions-intro", INTRO_TEXT))

        picker_row = QHBoxLayout()
        picker_row.addWidget(_label(self, "sessions-project-caption", PROJECT_CAPTION))
        self._combo = QComboBox(self)
        self._combo.setObjectName("sessions-project-picker")
        self._combo.currentIndexChanged.connect(self._on_project_changed)
        picker_row.addWidget(self._combo, 1)
        self._refresh = QPushButton(REFRESH_TEXT, self)
        self._refresh.setObjectName("sessions-refresh")
        self._refresh.clicked.connect(lambda _checked=False: self.refresh())
        picker_row.addWidget(self._refresh)
        layout.addLayout(picker_row)

        self._status = _label(self, "sessions-status", LOADING_TEXT)
        layout.addWidget(self._status)
        self._claude_processes = _label(self, "sessions-claude-processes", CLAUDE_PROCESSES_TEXT)
        layout.addWidget(self._claude_processes)

        self._table = QTableWidget(0, len(COLUMNS), self)
        self._table.setObjectName("sessions-table")
        self._table.setHorizontalHeaderLabels(list(COLUMNS))
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.verticalHeader().setVisible(False)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        enable_table_copy(self._table)
        layout.addWidget(self._table, 1)

    # ----------------------------------------------------------- reads

    def refresh(self) -> None:
        """A full read now: the project list, then the picked project's sessions, through the mode chosen in Settings.

        A read still running is superseded, and its result is dropped. The Refresh button is disabled until this
        read ends.
        """
        self._start(full=True)

    def _start(self, *, full: bool) -> None:
        """Start one read on the thread pool. It supersedes a read still running, whose result is then dropped."""
        self._generation += 1
        generation = self._generation
        self._in_flight = generation
        selected = str(self._combo.currentData() or "")
        if full:
            self._refresh.setEnabled(False)

        # The job holds the reads, the owner token and the shared delivery object. It never holds this tab.
        reads = self._reads
        owner = self._owner
        delivery = shared_delivery()

        def work() -> _Snapshot:
            if full:
                return reads.collect_full(generation, selected)
            return reads.collect_local(generation, selected)

        def deliver(snapshot: _Snapshot) -> None:
            delivery.delivered.emit(Delivered(owner, snapshot))

        QThreadPool.globalInstance().start(_RefreshJob(generation, work, deliver))

    @Slot(object)
    def _on_delivered(self, item: object) -> None:
        # Runs on the GUI thread. A result for another tab is dropped here, and so is one a newer read has superseded.
        if not isinstance(item, Delivered) or item.owner is not self._owner:
            return
        snapshot = item.value
        if snapshot.generation != self._generation:
            return  # a newer read started after this one
        self._in_flight = None
        self._refresh.setEnabled(True)
        if snapshot.choices is not None:
            self._fill_picker(snapshot.choices, snapshot.project_id)
        if snapshot.result is not None:
            self._show(snapshot.result, datetime.now(timezone.utc))

    def _fill_picker(self, choices: tuple[tuple[str, str], ...], chosen: str) -> None:
        wanted = [pid for pid, _name in choices]
        self._combo.blockSignals(True)
        try:
            current = [self._combo.itemData(index) for index in range(self._combo.count())]
            if current != wanted:
                self._combo.clear()
                for pid, name in choices:
                    self._combo.addItem(name, pid)
            self._combo.setCurrentIndex(self._combo.findData(chosen) if chosen else -1)
        finally:
            self._combo.blockSignals(False)

    def _show(self, result: SessionsResult, now: datetime) -> None:
        rows = result.rows if result.state == SESSIONS else ()
        self._table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for column, text in enumerate(row_cells(row, now)):
                self._table.setItem(row_index, column, QTableWidgetItem(text))
        self._status.setText(status_text(result, now.astimezone().strftime("%H:%M:%S")))

    # ----------------------------------------------------------- events

    @Slot(int)
    def _on_project_changed(self, _index: int) -> None:
        self._table.setRowCount(0)
        self._status.setText(PRESS_REFRESH_TEXT)
        self._start(full=False)

    @Slot()
    def _on_tick(self) -> None:
        if self._in_flight is None:
            self._start(full=False)

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        self._timer.start()
        if not self._read_once:
            self._read_once = True
            self.refresh()

    def hideEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        self._timer.stop()
        super().hideEvent(event)


__all__ = [
    "COLUMNS",
    "LOCAL_MODE",
    "REFRESH_MS",
    "SessionsPanel",
    "choose_project",
    "row_cells",
    "status_text",
]
