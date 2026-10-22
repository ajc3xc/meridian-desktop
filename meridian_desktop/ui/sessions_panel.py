"""Read-only Sessions tab: a project picker, a table of the project's sessions, and a status line.

This is Qt code and sits in ui/, so the boundary check allows it. The panel has no write control: no start,
stop, restart, send or edit button. Its only input is the project picker.

Each refresh runs on the thread pool, so a slow server never blocks the window. Each refresh has a generation
number, and a result from an older refresh is dropped once a newer one has started. The panel refreshes every
5 seconds while the tab is visible, and stops its timer when the tab is hidden. A tick is skipped while a
refresh is still running.

Source order: the local server first. When it is not running, the hosted host is asked, but only when a hosted
token is saved. The status line names the source the rows came from.

The three providers are called on the worker thread, never on the UI thread:
- local_url_provider() -> str: the local Meridian server address from Settings.
- hosted_provider() -> (hosted_url, token or None): the hosted address from Settings and the saved token from the
  keychain. The panel passes the token to the client for one request and keeps no copy. It is never shown.
- project_provider() -> sequence of (project_id, name) pairs: fills the picker.

Only names, status, last seen (as an age) and the client name reach the table. Session summaries, task log bodies
and notes are never read by the client, so they cannot reach this panel.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from PySide6.QtCore import QRunnable, Qt, QThreadPool, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
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
from meridian_desktop.ui.text import enable_table_copy, make_selectable

REFRESH_MS = 5000
COLUMNS = ("Name", "Status", "Last seen", "Client")
INTRO_TEXT = "Sessions for the chosen project. Read-only: names, status, last seen and client only."
PROJECT_CAPTION = "Project"
LOADING_TEXT = "Loading sessions..."
PICK_TEXT = "Pick a project to see its sessions."
NO_PROJECTS_TEXT = "No projects are available to pick. Create a project in Meridian first."
PROJECTS_FAILED_TEXT = "Projects could not be read ({})."
READ_FAILED_TEXT = "Sessions could not be read ({})."
UNNAMED_TEXT = "(unnamed)"
SOURCE_TEXT = {SOURCE_LOCAL: "this computer's Meridian server", SOURCE_HOSTED: "hosted Meridian"}


@dataclass(frozen=True)
class _Snapshot:
    """One finished refresh. ``choices`` is None when the project list could not be read, so the picker is left alone."""

    generation: int
    choices: tuple[tuple[str, str], ...] | None
    project_id: str
    result: SessionsResult


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
    """Run one refresh on the thread pool and deliver a _Snapshot.

    A failure becomes an error result that names the exception class only, so a raw exception, and any
    value inside it, never reaches the UI thread.
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
        except Exception as exc:  # noqa: BLE001 - a worker must never raise into the pool
            failed = SessionsResult(ERROR, READ_FAILED_TEXT.format(exc.__class__.__name__))
            snapshot = _Snapshot(self._generation, None, "", failed)
        try:
            self._deliver(snapshot)
        except RuntimeError:
            pass  # the panel was deleted while the refresh ran


# ----------------------------------------------------------------- panel


class SessionsPanel(QWidget):
    """The Sessions tab: the sessions of the picked project, refreshed while the tab is visible. Read-only."""

    _finished = Signal(object)

    def __init__(
        self,
        local_url_provider: Callable[[], str],
        hosted_provider: Callable[[], tuple[str, str | None]],
        project_provider: Callable[[], Sequence[tuple[str, str]]],
        parent: QWidget | None = None,
        *,
        local_transport: Any = None,
        hosted_transport: hosted_client.Transport | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("sessions-panel")
        self._local_url = local_url_provider
        self._hosted = hosted_provider
        self._projects = project_provider
        self._local_transport = local_transport
        self._hosted_transport = hosted_transport
        self._generation = 0
        self._in_flight: int | None = None
        self._finished.connect(self._on_finished)
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
        layout.addLayout(picker_row)

        self._status = _label(self, "sessions-status", LOADING_TEXT)
        layout.addWidget(self._status)

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

    # ----------------------------------------------------------- refresh

    def refresh(self) -> None:
        """Start a refresh for the picked project. A refresh still running is superseded, and its result is dropped."""
        self._generation += 1
        generation = self._generation
        self._in_flight = generation
        selected = str(self._combo.currentData() or "")
        job = _RefreshJob(generation, lambda: self._collect(generation, selected), self._finished.emit)
        QThreadPool.globalInstance().start(job)

    def _collect(self, generation: int, selected: str) -> _Snapshot:
        """Runs on a worker. Reads the project list, then the sessions of the chosen project."""
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

    def _read_projects(self) -> tuple[tuple[tuple[str, str], ...] | None, str]:
        """(choices, "") on success. (None, message) when the project list could not be read."""
        try:
            pairs = [(pid, name) for pid, name in self._projects() if isinstance(pid, str) and pid.strip()]
        except Exception as exc:  # noqa: BLE001 - a failed read is named by class only
            return None, PROJECTS_FAILED_TEXT.format(exc.__class__.__name__)
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

    @Slot(object)
    def _on_finished(self, snapshot: _Snapshot) -> None:
        if snapshot.generation != self._generation:
            return  # a newer refresh started after this one
        self._in_flight = None
        if snapshot.choices is not None:
            self._fill_picker(snapshot.choices, snapshot.project_id)
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
        self._status.setText(LOADING_TEXT)
        self.refresh()

    @Slot()
    def _on_tick(self) -> None:
        if self._in_flight is None:
            self.refresh()

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        self._timer.start()
        self.refresh()

    def hideEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        self._timer.stop()
        super().hideEvent(event)


__all__ = [
    "COLUMNS",
    "REFRESH_MS",
    "SessionsPanel",
    "choose_project",
    "row_cells",
    "status_text",
]
