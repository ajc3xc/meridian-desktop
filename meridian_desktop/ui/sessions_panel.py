"""Read-only Sessions tab: a project picker, a Refresh button, a table of the project's sessions, and a status line.

This is Qt code and sits in ui/, so the boundary check allows it. The panel has no start, stop, restart, send or
edit control. Its inputs are the project picker and the Refresh button.

Every read runs on the thread pool, so a slow server never blocks the window. Each read has a generation number,
and a result from an older read is dropped once a newer one has started. There are two kinds of read:

- A full read lists the projects, then reads the sessions of the picked project. It runs the first time the tab is
  shown, when Refresh is pressed, and on a later show when the database file settings (db_path or core_checkout)
  changed since the last full read. The sessions follow data_source.read_in_order, the order the Projects tab uses.
  In local mode they come from the local server first, when it answers. The hosted host is asked only when the local
  server did not answer and a hosted token is saved, and its answer is shown as it is, an error (401, 403, 404, 503,
  refused) included. Only when no token is saved is the database file read, read-only (sessions_reader), and its rows
  carry the snapshot label in the status line. In hosted and tunnel modes the sessions come from hosted only: the
  local server and the database file are not read. The Refresh button is disabled while a full read runs.
- A local read asks the local server only, for the picked project, and only in local mode. It never lists the
  projects and never calls the hosted host. The 5-second timer starts one on each tick while the tab is visible,
  and a change of project starts one. A local server that does not answer gives nothing new, so the table and the
  status line keep what they show.

Whether the local server answered is decided in one place for both tabs: data_source.local_server_answers, the
/health probe the Projects tab's resolve uses. A full read takes that answer from the same resolve that lists the
projects (the provider's resolve_with_local_check), so one full read asks the local server once. A tick asks it once,
through the provider's local_server_answers(url). The tab has no check of its own. When the probe says the server
answered, the sessions route's answer is final, an error included.

Outside local mode a change of project clears the table and says to press Refresh. It reads nothing. The status
line names the source the rows came from.

The four providers are called on the worker thread, never on the UI thread:
- local_url_provider() -> str: the local Meridian server address from Settings.
- hosted_provider() -> (hosted_url, token or None): the hosted address from Settings and the saved token from the
  keychain. The panel passes the token to the client for one request and keeps no copy. It is never shown.
- project_provider() -> sequence of (project_id, name) pairs: fills the picker. Only a full read calls it. When it
  raises data_source.ProjectsUnresolved, the status line shows that resolver message. Any other failure shows
  PROJECTS_FAILED_TEXT. The window passes a data_source.ProjectsProvider, which also has local_server_answers(url)
  (the local-server check), local_file() (the database file and its snapshot label, for the file read) and
  watch_key() (the settings that choose that file). A provider without them reads no file and never reads again on
  show, and with no check to ask, its local server is always asked and its answer is final.
- mode_provider() -> str: the mode saved in Settings. Only "local" allows a local read, and a full read follows the
  source order of that mode.

Only names, status, last seen (as an age) and the client name reach the table. Session summaries, task log bodies
and notes are never read by the client, so they cannot reach this panel.

The running agent programs (Claude, Codex, Cursor and any saved custom names) are listed on the same worker job as the
sessions, on every read: the first show, Refresh, a project change and each local tick, so they stay live while the tab
is visible. Each row is one program: its label, how many of its processes are roots, and how many matching processes it
has in all, read by claude_processes. Nothing else about a process is shown. A failed listing says so in one line and
leaves the sessions table as it is.
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

from meridian_desktop import claude_processes, hosted_client, sessions_reader
from meridian_desktop.claude_processes import ClaudeProcessesResult
from meridian_desktop.data_source import ProjectsUnresolved, read_in_order
from meridian_desktop.sessions_client import (
    ERROR,
    NO_PROJECT,
    NO_TOKEN_MESSAGE,
    NOT_RUNNING,
    NOT_RUNNING_MESSAGE,
    SESSIONS,
    SOURCE_HOSTED,
    SOURCE_LOCAL,
    UNAUTHORISED,
    SessionRow,
    SessionsResult,
    hosted_sessions,
    last_seen_age,
    local_sessions,
)
from meridian_desktop.ui.delivery import Delivered, shared_delivery
from meridian_desktop.ui.text import TextBlock, enable_table_copy, make_selectable, set_label_text

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
CLAUDE_CAPTION = "Agent programs running on this computer. Read-only: each program's label and process counts only."
CLAUDE_CHECKING_TEXT = "Checking the running agent programs..."
CLAUDE_NONE_TEXT = "Not connected. No agent program is running on this computer right now."
CLAUDE_TASKLIST_NOTE = "The process table was not read, so tasklist was used. Each process counts as a root."
CLAUDE_NOTE_TEXT = (
    "A Claude Code session started through Node.js runs as node.exe. That name is shared with other software, "
    "so it is not listed here."
)
CLAUDE_COLUMNS = ("Program", "Root processes", "Total processes")
UNNAMED_TEXT = "(unnamed)"
SOURCE_TEXT = {
    SOURCE_LOCAL: "this computer's Meridian server",
    SOURCE_HOSTED: "hosted Meridian",
    sessions_reader.SOURCE_FILE: "the database file",
}


@dataclass(frozen=True)
class _Snapshot:
    """One finished read. ``choices`` is None when the picker is left alone. ``result`` is None when there is nothing
    new to show: a local read whose server did not answer, or one that ran outside local mode."""

    generation: int
    choices: tuple[tuple[str, str], ...] | None
    project_id: str
    result: SessionsResult | None
    # The Claude processes, read on the same job. None when that read did not finish, so the table keeps what it shows.
    claude: ClaudeProcessesResult | None = None


# ----------------------------------------------------------------- pure helpers


def choose_project(selected: str, choices: Sequence[tuple[str, str]]) -> str:
    """Keep the selected project when it is still listed. Otherwise take the first one. Empty when none is listed."""
    ids = [pid for pid, _name in choices]
    if selected in ids:
        return selected
    return ids[0] if ids else ""


def status_line(result: SessionsResult) -> str:
    """The status line for one result, without the time. It stays the same from one tick to the next.

    A sessions result with a message (the snapshot label of a file read) shows that label after the count.
    """
    if result.state == SESSIONS:
        where = SOURCE_TEXT.get(result.source, "Meridian")
        note = f" {result.message}" if result.message else ""
        if result.rows:
            return f"{len(result.rows)} session(s) from {where}.{note}"
        if note:
            return f"No active sessions in {where}.{note}"
        return f"No sessions from {where} right now."
    return result.message


def checked_line(result: SessionsResult, checked_at: str) -> str:
    """The time line for one result: when it arrived, as local time. Empty when no project was read."""
    return "" if result.state == NO_PROJECT else f"Checked at {checked_at}."


def status_text(result: SessionsResult, checked_at: str) -> str:
    """The status line and the time line together, as one sentence pair. ``checked_at`` is the local arrival time."""
    return " ".join(part for part in (status_line(result), checked_line(result, checked_at)) if part)


def row_cells(row: SessionRow, now: datetime) -> tuple[str, str, str, str]:
    """The four table cells for one session, in COLUMNS order. Last seen is shown as an age, not a timestamp."""
    return (row.name or UNNAMED_TEXT, row.status, last_seen_age(row.last_seen, now), row.client)


def _dropped_text(count: int) -> str:
    """The note for saved custom program names that were not valid and were left out."""
    if count == 1:
        return "1 saved program name is not valid and was left out."
    return f"{count} saved program names are not valid and were left out."


def claude_status_text(result: ClaudeProcessesResult, checked_at: str) -> str:
    """The processes status line for one listing. ``checked_at`` is the local time the listing arrived.

    A failed listing is one plain line: its fixed message, then the time it was checked. A listing with no matching
    process says "Not connected" and nothing else. The time comes before any note, so a listing read through tasklist,
    or with saved names left out, adds its note after the time.
    """
    if result.state != claude_processes.LISTED:
        return f"{result.message} Checked at {checked_at}."
    total = sum(group.total for group in result.groups)
    if total == 0:
        parts = [CLAUDE_NONE_TEXT]
    else:
        noun = "process" if total == 1 else "processes"
        parts = [f"{total} agent {noun} running on this computer."]
    parts.append(f"Checked at {checked_at}.")
    if result.source == claude_processes.SOURCE_TASKLIST:
        parts.append(CLAUDE_TASKLIST_NOTE)
    if result.dropped:
        parts.append(_dropped_text(result.dropped))
    return " ".join(parts)


def _table(parent: QWidget, name: str, columns: tuple[str, ...]) -> QTableWidget:
    """A read-only table: no editing, whole rows selected (several by a drag, Shift or Ctrl), and Ctrl+C copies them."""
    table = QTableWidget(0, len(columns), parent)
    table.setObjectName(name)
    table.setHorizontalHeaderLabels(list(columns))
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    table.verticalHeader().setVisible(False)
    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    header.setStretchLastSection(True)
    enable_table_copy(table)
    return table


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
        process_lister: Callable[[], ClaudeProcessesResult] | None = None,
    ) -> None:
        self._local_url = local_url_provider
        self._hosted = hosted_provider
        self._projects = project_provider
        self._mode = mode_provider
        self._local_transport = local_transport
        self._hosted_transport = hosted_transport
        # None means the real listing. A test passes a fake, so it starts no process.
        self._process_lister = process_lister
        # The window's ProjectsProvider also names the database file and the settings that choose it. A plain
        # callable has neither, so it reads no file and never asks for a read on show.
        self._local_file: Callable[[], tuple[Any, str] | None] | None = getattr(project_provider, "local_file", None)
        self._watch_key: Callable[[], object] | None = getattr(project_provider, "watch_key", None)
        # Whether the local server answered: the Projects tab's own check (data_source.local_server_answers), with
        # the same probe, so both tabs choose the same source. A plain callable has no probe, so its server is asked.
        answers: Callable[[str], bool] | None = getattr(project_provider, "local_server_answers", None)
        self._local_answers: Callable[[str], bool] = answers if answers is not None else (lambda _url: True)
        # The projects and the local-server answer of one resolve. A full read reuses that answer, so it asks the server
        # once. A plain callable gives no answer, so its full read asks the check itself.
        self._resolve_with_check: Callable[[], tuple[Sequence[tuple[str, str]], bool]] | None = getattr(
            project_provider, "resolve_with_local_check", None
        )

    def watch_key(self) -> object:
        """The settings that choose the database file, as saved now, or None when the provider does not say."""
        if self._watch_key is None:
            return None
        try:
            return self._watch_key()
        except Exception:  # noqa: BLE001 - an unreadable key only means no read on show
            return None

    def collect_full(self, generation: int, selected: str) -> _Snapshot:
        """Runs on a worker. Lists the projects once, then reads the sessions of the chosen project.

        The local-server answer of that project list is passed on, so the sessions read does not check the server again.
        """
        claude = self._claude()
        choices, failure, local_answered = self._read_projects()
        chosen = selected if choices is None else choose_project(selected, choices)
        if not chosen:
            if failure:
                message = failure
            elif choices == ():
                message = NO_PROJECTS_TEXT
            else:
                message = PICK_TEXT
            return _Snapshot(generation, choices, "", SessionsResult(NO_PROJECT, message), claude)
        return _Snapshot(generation, choices, chosen, self._read_sessions(chosen, local_answered), claude)

    def collect_local(self, generation: int, selected: str) -> _Snapshot:
        """Runs on a worker. A local read: the local server only, for the picked project, and only in local mode.

        It never lists the projects and never calls the hosted host. When the local server does not answer (the same
        check a full read and the Projects tab use), there is nothing new to show, so the result is None and the table
        and status line keep what they show. The Claude processes are listed on every local read, whatever the mode,
        because they are on this computer.
        """
        claude = self._claude()
        if not selected or self._mode() != LOCAL_MODE:
            return _Snapshot(generation, None, "", None, claude)
        url = self._local_url()
        if not self._local_answers(url):
            return _Snapshot(generation, None, selected, None, claude)
        result = local_sessions(url, selected, transport=self._local_transport)
        return _Snapshot(generation, None, selected, result, claude)

    def _claude(self) -> ClaudeProcessesResult:
        """The running Claude processes, listed on the worker. A listing that raises is shown as unavailable."""
        try:
            lister = claude_processes.list_claude_processes if self._process_lister is None else self._process_lister
            return lister()
        except Exception:  # noqa: BLE001 - a listing must never fail the sessions read
            return ClaudeProcessesResult(claude_processes.UNAVAILABLE, claude_processes.UNAVAILABLE_MESSAGE)

    def _read_projects(self) -> tuple[tuple[tuple[str, str], ...] | None, str, bool | None]:
        """(choices, "", local_answered) on success, or (None, message, local_answered) when the list could not be read.

        local_answered is whether the local server answered in the same resolve, or None when the provider does not say.
        The resolver's own message is shown as it is. Any other failure is named in plain words, never by class or value.
        """
        try:
            if self._resolve_with_check is not None:
                found, local_answered = self._resolve_with_check()
            else:
                found, local_answered = self._projects(), None
            pairs = [(pid, name) for pid, name in found if isinstance(pid, str) and pid.strip()]
        except ProjectsUnresolved as exc:
            return None, exc.message or PROJECTS_FAILED_TEXT, exc.local_answered
        except Exception:  # noqa: BLE001 - a failed read is named in plain words, never by class or value
            return None, PROJECTS_FAILED_TEXT, None
        return tuple((pid.strip(), _plain(name) or pid.strip()) for pid, name in pairs), "", local_answered

    def _read_sessions(self, project_id: str, local_answered: bool | None = None) -> SessionsResult:
        """The picked project's sessions, in data_source.read_in_order: the order the Projects tab uses too.

        Local mode: the local server first, when it answered; its sessions answer is then final, an error included.
        local_answered is the answer the project list read gave in the same refresh, so the check is not asked again.
        None means that read gave no answer, so the check (data_source.local_server_answers) is asked here. Otherwise
        hosted when a token is saved, and its answer is shown as it is, an error included. Only with no token saved is
        the database file read, read-only. Hosted and tunnel modes: hosted only, never the local server or the file.
        """
        saved: list[tuple[str, str | None]] = []
        mode = self._mode()

        def hosted_pair() -> tuple[str, str | None]:
            # The hosted URL and the token, read once per read and only when hosted may be asked.
            if not saved:
                saved.append(self._hosted())
            return saved[0]

        def server() -> SessionsResult | None:
            url = self._local_url()
            answered = self._local_answers(url) if local_answered is None else local_answered
            if not answered:
                return None
            return local_sessions(url, project_id, transport=self._local_transport)

        def hosted(token: str) -> SessionsResult:
            return hosted_sessions(hosted_pair()[0], project_id, token, transport=self._hosted_transport)

        def database_file() -> SessionsResult | None:
            found = self._local_file() if self._local_file is not None else None
            if found is None:
                return None
            path, note = found
            return sessions_reader.file_sessions(path, project_id, note=note)

        result = read_in_order(mode, lambda: hosted_pair()[1], server=server, hosted=hosted, file=database_file)
        if result is not None:
            return result
        # Nothing answered. In local mode, the local server's not-running line. Outside it, no token is saved.
        if mode == LOCAL_MODE:
            return SessionsResult(NOT_RUNNING, NOT_RUNNING_MESSAGE, SOURCE_LOCAL)
        return SessionsResult(UNAUTHORISED, NO_TOKEN_MESSAGE)


# ----------------------------------------------------------------- panel


class SessionsPanel(QWidget):
    """The Sessions tab: the picked project's sessions. Read on first show, on Refresh and on a show after the database
    file settings changed, and locally on a timer."""

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
        process_lister: Callable[[], ClaudeProcessesResult] | None = None,
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
            process_lister,
        )
        # Identifies this tab's reads. A plain object, so a job can hold it without holding the tab.
        self._owner = object()
        self._generation = 0
        self._in_flight: int | None = None
        self._read_once = False
        # The database file settings when the last full read started. A later show reads again when they differ.
        self._read_key: object = None
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

        # The status line and the time it was checked are one block, so one drag selects both. A tick that changes only the
        # time changes the text after the status line, and set_label_text keeps a selection in the status line.
        status = TextBlock(self, "sessions-status-text", spacing=layout.spacing())
        self._status = status.line("sessions-status", LOADING_TEXT)
        self._checked = status.line("sessions-checked", "")
        self._checked.hide()
        layout.addWidget(status.label)

        self._table = _table(self, "sessions-table", COLUMNS)
        layout.addWidget(self._table, 2)

        # The Claude caption and the line that says what was found are one block.
        claude = TextBlock(self, "sessions-claude-text", spacing=layout.spacing())
        claude.line("sessions-claude-caption", CLAUDE_CAPTION)
        self._claude_processes = claude.line("sessions-claude-processes", CLAUDE_CHECKING_TEXT)
        layout.addWidget(claude.label)
        self._claude_table = _table(self, "sessions-claude-table", CLAUDE_COLUMNS)
        layout.addWidget(self._claude_table, 1)
        layout.addWidget(_label(self, "sessions-claude-note", CLAUDE_NOTE_TEXT))

    # ----------------------------------------------------------- reads

    def refresh(self) -> None:
        """A full read now: the project list, then the picked project's sessions, through the mode chosen in Settings.

        A read still running is superseded, and its result is dropped. The Refresh button is disabled until this
        read ends.
        """
        self._read_key = self._reads.watch_key()
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
        now = datetime.now(timezone.utc)
        if snapshot.choices is not None:
            self._fill_picker(snapshot.choices, snapshot.project_id)
        if snapshot.result is not None:
            self._show(snapshot.result, now)
        if snapshot.claude is not None:
            self._show_claude(snapshot.claude, now)

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
        set_label_text(self._status, status_line(result))
        self._show_checked(checked_line(result, now.astimezone().strftime("%H:%M:%S")))

    def _show_checked(self, text: str) -> None:
        set_label_text(self._checked, text)
        self._checked.setVisible(bool(text))

    def _show_claude(self, result: ClaudeProcessesResult, now: datetime) -> None:
        groups = result.groups if result.state == claude_processes.LISTED else ()
        self._claude_table.setRowCount(len(groups))
        for row_index, group in enumerate(groups):
            cells = (group.label, str(group.roots), str(group.total))
            for column, text in enumerate(cells):
                self._claude_table.setItem(row_index, column, QTableWidgetItem(text))
        self._claude_processes.setText(claude_status_text(result, now.astimezone().strftime("%H:%M:%S")))

    # ----------------------------------------------------------- events

    @Slot(int)
    def _on_project_changed(self, _index: int) -> None:
        self._table.setRowCount(0)
        self._status.setText(PRESS_REFRESH_TEXT)
        self._show_checked("")
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
        elif self._read_key != self._reads.watch_key():
            # db_path or core_checkout changed since the last full read, so the file to read may be another one.
            self.refresh()

    def hideEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        self._timer.stop()
        super().hideEvent(event)


__all__ = [
    "CLAUDE_COLUMNS",
    "COLUMNS",
    "LOCAL_MODE",
    "REFRESH_MS",
    "SessionsPanel",
    "checked_line",
    "choose_project",
    "claude_status_text",
    "row_cells",
    "status_line",
    "status_text",
]
