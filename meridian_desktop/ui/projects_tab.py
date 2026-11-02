"""Projects tab: a list of projects and a detail pane for the selected one.

This is Qt code and sits in ui/, so the boundary check allows it. The tab never writes to projects. Its rows come from
data_source.resolve, and every outside read is an injected callable in DataSources. Its one control is the button
"Back up hosted projects to this computer", shown only when a hosted token is saved. The button reads the hosted project
list once, in one run, and writes it to one backup file in the desktop backups folder (projects_backup). The tab states
that folder before the run and names the file after it. Nothing is written to the hosted service.

The resolve runs on the thread pool, so a slow probe never blocks the window. Each load has a
generation number, and a result from an older load is dropped once a newer load has started.

A pool worker never touches a Qt object. It emits its result to one shared delivery object, which
lives on the GUI thread and is never deleted. Each tab receives on the GUI thread and keeps only
its own results, so a tab deleted while its load runs is never touched by the worker.

Only the fields in projects_reader.PROJECT_COLUMNS reach the table or the detail pane. The
detail pane is built from those fields alone, so executor_config and agent_instructions never
appear, even if a source returned them. Every label and cell is plain text.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from PySide6.QtCore import QRunnable, Qt, QThreadPool, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from meridian_desktop import hosted_client, local_client, projects_backup, projects_reader, token_store
from meridian_desktop.data_source import (
    SOURCE_FILE,
    SOURCE_HOSTED,
    SOURCE_SERVER,
    STATE_OK,
    STATE_READ_FAILED,
    DataSources,
    Resolution,
    resolve,
)
from meridian_desktop.projects_reader import PROJECT_COLUMNS
from meridian_desktop.secret_store import SecretStore
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.ui.delivery import Delivered, Delivery, shared_delivery
from meridian_desktop.ui.text import enable_table_copy, make_selectable

PROBE_TIMEOUT = 2.0
LOADING_TEXT = "Loading projects..."
DETAIL_EMPTY_TEXT = "Select a project to see its details."
NONE_TEXT = "(none)"
BACKUP_BUTTON_TEXT = "Back up hosted projects to this computer"
BACKUP_RUNNING_TEXT = "Backing up hosted projects..."
BACKUP_FAILED_TEXT = "Projects could not be backed up"

FIELD_LABELS = {
    "id": "ID",
    "name": "Name",
    "creator_human_id": "Creator",
    "icon": "Icon",
    "status": "Status",
    "priority": "Priority",
    "parent_project_id": "Parent project",
    "created_at": "Created at",
}
# The table columns, in display order. Each one is in PROJECT_COLUMNS.
TABLE_FIELDS = ("name", "status", "priority", "created_at")

SOURCE_TEXT = {
    SOURCE_SERVER: "the local Meridian server",
    SOURCE_FILE: "the database file",
    SOURCE_HOSTED: "hosted Meridian",
}


def default_sources(secrets: SecretStore, *, server_transport: local_client.Transport | None = None) -> DataSources:
    """The data sources the app uses. Every one is real.

    The probe is loopback only (local_client.probe_health). The running-server reader is
    local_client.list_projects: one loopback GET /projects, with no token. The file reader opens
    the database read-only. The token comes from the keychain through token_store, and only in
    hosted mode. The hosted reader is hosted_client.list_projects: one GET /projects with the bearer
    token. ``server_transport`` is for tests: when given, the server read sends through it, not urllib.
    """
    return DataSources(
        probe=lambda url: local_client.probe_health(url, PROBE_TIMEOUT),
        read_server=lambda url: local_client.list_projects(url, transport=server_transport),
        read_file=projects_reader.list_projects,
        get_token=lambda: token_store.get_token(store=secrets),
        list_hosted=hosted_client.list_projects,
    )


def visible_row(row: dict[str, Any]) -> dict[str, Any]:
    """The listed fields of one row, in PROJECT_COLUMNS order. Every other key is dropped."""
    return {key: row.get(key) for key in PROJECT_COLUMNS}


def _table_text(value: Any) -> str:
    return "" if value is None else str(value)


def detail_text(row: dict[str, Any]) -> str:
    """The detail pane for one visible row: a labelled line per listed field."""
    lines = []
    for key in PROJECT_COLUMNS:
        value = row.get(key)
        text = NONE_TEXT if value is None or value == "" else str(value)
        lines.append(f"{FIELD_LABELS[key]}: {text}")
    return "\n".join(lines)


@dataclass(frozen=True)
class _Loaded:
    """One finished load: the number it was started with, what the resolver returned, and whether a hosted token is saved.

    ``token_saved`` is None when the load failed before it could tell, so the backup button keeps what it shows. It is
    the value of a Delivered whose owner is the tab's token, so it holds no Qt object.
    """

    generation: int
    outcome: Resolution
    token_saved: bool | None = None


@dataclass(frozen=True)
class _Backed:
    """One finished backup: the status line for it. It holds no Qt object and no token."""

    message: str


def _usable_token(token: object) -> bool:
    return isinstance(token, str) and bool(token.strip())


def _load(store: SettingsStore, sources: DataSources) -> tuple[Resolution, bool]:
    """Runs on a worker: reads the saved settings, resolves the one source the mode uses, and says whether a hosted
    token is saved.

    The token is read at most once. The resolve's own read is reused. A load whose resolve never reads it reads it here.
    """
    read: list[object] = []

    def get_token() -> object:
        if not read:
            read.append(sources.get_token())
        return read[0]

    outcome = resolve(store.load().settings, replace(sources, get_token=get_token))
    try:
        token = get_token()
    except Exception:  # noqa: BLE001 - an unreadable token only hides the backup button
        token = None
    return outcome, _usable_token(token)


def _backup_work(store: SettingsStore, sources: DataSources) -> _Backed:
    """Runs on a worker: backs up all the hosted projects in one run, through the same hosted reader the tab uses."""
    settings = store.load().settings
    token = sources.get_token()
    if not _usable_token(token):
        return _Backed(projects_backup.NO_TOKEN_TEXT)
    try:
        written = projects_backup.backup_hosted(
            sources.list_hosted,
            settings.hosted_url,
            str(token),
            folder=projects_backup.backup_dir(),
            taken_at=projects_backup.utc_now(),
        )
    except projects_backup.NoUsableProjects:
        # An empty answer writes nothing, so the backups already on disk are still there.
        return _Backed(projects_backup.NO_PROJECTS_TEXT)
    return _Backed(written.message)


class _LoadJob(QRunnable):
    """Run one load on the thread pool and deliver its _Loaded to the shared delivery object.

    The job holds plain data only: an owner token, a generation number, the work, and the shared delivery object.
    It never holds the tab, so the worker never touches a tab, even one that was deleted while the load ran. A failure
    becomes a read_failed Resolution that names the exception class only, so a raw exception, and any value inside it,
    never reaches the UI thread.
    """

    def __init__(
        self,
        owner: object,
        generation: int,
        work: Callable[[], tuple[Resolution, bool]],
        delivery: Delivery,
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._owner = owner
        self._generation = generation
        self._work = work
        self._delivery = delivery

    def run(self) -> None:
        token_saved: bool | None
        try:
            outcome, token_saved = self._work()
        except Exception as exc:  # noqa: BLE001 - a worker must never raise into the pool
            failed = f"Projects could not be loaded ({exc.__class__.__name__})."
            outcome = Resolution(STATE_READ_FAILED, None, message=failed)
            token_saved = None
        try:
            self._delivery.delivered.emit(Delivered(self._owner, _Loaded(self._generation, outcome, token_saved)))
        except RuntimeError:
            pass  # the shared delivery object is gone, for example at shutdown


class _BackupJob(QRunnable):
    """Run one backup on the thread pool and deliver its _Backed to the shared delivery object.

    Like _LoadJob, it holds plain data only and never the tab. A failure is named in plain words: the hosted client's own
    fixed text, or the exception class only. No token, response body or path reaches the UI as an error.
    """

    def __init__(self, owner: object, work: Callable[[], _Backed], delivery: Delivery) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._owner = owner
        self._work = work
        self._delivery = delivery

    def run(self) -> None:
        try:
            report = self._work()
        except hosted_client.HostedError as exc:
            report = _Backed(f"{BACKUP_FAILED_TEXT}. {exc}")
        except Exception as exc:  # noqa: BLE001 - a worker must never raise into the pool
            report = _Backed(f"{BACKUP_FAILED_TEXT} ({exc.__class__.__name__}).")
        try:
            self._delivery.delivered.emit(Delivered(self._owner, report))
        except RuntimeError:
            pass  # the shared delivery object is gone, for example at shutdown


class ProjectsTab(QWidget):
    """Projects from the resolver, and the detail of the selected row. Read-only."""

    def __init__(self, settings_store: SettingsStore, sources: DataSources, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("projects-tab")
        self._settings = settings_store
        self._sources = sources
        self._generation = 0
        # Identifies this tab's loads. A plain object, so a job can hold it without holding the tab.
        self._owner = object()
        self._rows: list[dict[str, Any]] = []
        # The backup controls exist only once a hosted token has been seen saved, so a tab without a token has none.
        # A backup runs one at a time.
        self._backup_widgets: tuple[QPushButton, QLabel, QLabel] | None = None
        self._backup_running = False
        shared_delivery().delivered.connect(self._on_delivered)
        self._build()

    # ----------------------------------------------------------- layout

    def _build(self) -> None:
        layout = QVBoxLayout(self)

        self._status = make_selectable(QLabel(LOADING_TEXT, self))
        self._status.setObjectName("projects-status")
        self._status.setTextFormat(Qt.TextFormat.PlainText)
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        self._table = QTableWidget(0, len(TABLE_FIELDS), self)
        self._table.setObjectName("projects-table")
        self._table.setHorizontalHeaderLabels([FIELD_LABELS[field] for field in TABLE_FIELDS])
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.verticalHeader().setVisible(False)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        self._table.itemSelectionChanged.connect(self._show_detail)
        enable_table_copy(self._table)

        self._detail = make_selectable(QLabel(DETAIL_EMPTY_TEXT))
        self._detail.setObjectName("projects-detail")
        self._detail.setTextFormat(Qt.TextFormat.PlainText)
        self._detail.setWordWrap(True)
        self._detail.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        detail_scroll = QScrollArea(self)
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setWidget(self._detail)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.addWidget(self._table)
        splitter.addWidget(detail_scroll)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)

    # ----------------------------------------------------------- loading

    def refresh(self) -> None:
        """Start a load of the rows. A load still running is superseded, and its result is dropped."""
        self._generation += 1
        self._status.setText(LOADING_TEXT)
        # The job gets the store, the sources and the shared delivery object, not self: a worker never holds the tab.
        work = functools.partial(_load, self._settings, self._sources)
        QThreadPool.globalInstance().start(_LoadJob(self._owner, self._generation, work, shared_delivery()))

    @Slot(object)
    def _on_delivered(self, item: object) -> None:
        # Runs on the GUI thread. Another tab's result, or a load a newer one has superseded, is dropped.
        if not isinstance(item, Delivered) or item.owner is not self._owner:
            return
        if isinstance(item.value, _Backed):
            self._show_backup(item.value)
            return
        loaded = item.value
        if not isinstance(loaded, _Loaded) or loaded.generation != self._generation:
            return
        if loaded.token_saved is not None:
            self._offer_backup(loaded.token_saved)
        self._show(loaded.outcome)

    def _show(self, outcome: Resolution) -> None:
        if outcome.state != STATE_OK:
            # Every non-ok state shows the resolver's message, and no rows.
            self._rows = []
            self._fill()
            self._status.setText(outcome.message)
            return
        self._rows = [visible_row(row) for row in outcome.rows]
        self._fill()
        where = SOURCE_TEXT.get(outcome.source or "", "Meridian")
        if self._rows:
            self._status.setText(f"Showing {len(self._rows)} project(s) from {where}.")
        else:
            self._status.setText(f"No projects found in {where}.")

    def _fill(self) -> None:
        self._table.setRowCount(len(self._rows))
        for row_index, row in enumerate(self._rows):
            for column, field in enumerate(TABLE_FIELDS):
                self._table.setItem(row_index, column, QTableWidgetItem(_table_text(row.get(field))))
        if self._rows:
            self._table.selectRow(0)
        self._show_detail()

    # ----------------------------------------------------------- detail

    @Slot()
    def _show_detail(self) -> None:
        selected = sorted({item.row() for item in self._table.selectedItems()})
        if not selected or selected[0] >= len(self._rows):
            self._detail.setText(DETAIL_EMPTY_TEXT)
            return
        self._detail.setText(detail_text(self._rows[selected[0]]))

    # ----------------------------------------------------------- backup

    def _offer_backup(self, saved: bool) -> None:
        """Show the backup controls while a hosted token is saved, and hide them when none is.

        The widgets are built the first time a token is seen saved. A tab that never sees one never builds them. The
        destination line is set on each call, so it names the folder the backup would be written to now.
        """
        if self._backup_widgets is None:
            if not saved:
                return
            self._backup_widgets = self._build_backup_row()
        button, destination, status = self._backup_widgets
        destination.setText(projects_backup.destination_text(projects_backup.backup_dir()))
        for widget in (button, destination, status):
            widget.setVisible(saved)

    def _build_backup_row(self) -> tuple[QPushButton, QLabel, QLabel]:
        column = QVBoxLayout()
        row = QHBoxLayout()
        button = QPushButton(BACKUP_BUTTON_TEXT, self)
        button.setObjectName("projects-backup-button")
        button.clicked.connect(self._start_backup)
        row.addWidget(button)
        row.addStretch(1)
        destination = make_selectable(QLabel("", self))
        destination.setObjectName("projects-backup-destination")
        destination.setTextFormat(Qt.TextFormat.PlainText)
        destination.setWordWrap(True)
        status = make_selectable(QLabel("", self))
        status.setObjectName("projects-backup-status")
        status.setTextFormat(Qt.TextFormat.PlainText)
        status.setWordWrap(True)
        column.addLayout(row)
        column.addWidget(destination)
        column.addWidget(status)
        # Below the status line, above the table.
        self.layout().insertLayout(1, column)
        return button, destination, status

    def _start_backup(self, _checked: bool = False) -> None:
        """Start the one backup, which reads every hosted project in one run. A press while one runs does nothing."""
        if self._backup_running or self._backup_widgets is None:
            return
        button, _destination, status = self._backup_widgets
        self._backup_running = True
        button.setEnabled(False)
        status.setText(BACKUP_RUNNING_TEXT)
        # The job gets the store and the sources, not self: a worker never holds the tab.
        work = functools.partial(_backup_work, self._settings, self._sources)
        QThreadPool.globalInstance().start(_BackupJob(self._owner, work, shared_delivery()))

    def _show_backup(self, report: _Backed) -> None:
        self._backup_running = False
        if self._backup_widgets is None:
            return
        button, _destination, status = self._backup_widgets
        button.setEnabled(True)
        status.setText(report.message)

    # ----------------------------------------------------------- first show

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        # Each time the tab is shown, reload, so a server or file that started since then is seen.
        self.refresh()


__all__ = [
    "FIELD_LABELS",
    "TABLE_FIELDS",
    "ProjectsTab",
    "default_sources",
    "detail_text",
    "visible_row",
]
