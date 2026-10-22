"""Read-only Projects tab: a list of projects and a detail pane for the selected one.

This is Qt code and sits in ui/, so the boundary check allows it. The tab has no write
control: no button, line edit, combo box or spin box. Its rows come from data_source.resolve,
and every outside read is an injected callable in DataSources.

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
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from meridian_desktop import hosted_client, local_client, projects_reader, token_store
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
from meridian_desktop.ui.text import enable_table_copy, make_selectable

PROBE_TIMEOUT = 2.0
LOADING_TEXT = "Loading projects..."
DETAIL_EMPTY_TEXT = "Select a project to see its details."
NONE_TEXT = "(none)"

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
    """One finished load: the tab that started it, its generation, and what the resolver returned.

    ``owner`` is a plain token object, not the tab, so a _Loaded holds no Qt object.
    """

    owner: object
    generation: int
    outcome: Resolution


def _settings_resolution(store: SettingsStore, sources: DataSources) -> Resolution:
    """Runs on a worker: reads the saved settings, then resolves the one source the mode uses."""
    return resolve(store.load().settings, sources)


class _Delivery(QObject):
    """The one object a pool job emits to. It is never deleted, so an emit always has a live target."""

    delivered = Signal(object)


_DELIVERY: _Delivery | None = None


def _delivery() -> _Delivery:
    """The shared delivery object. The first tab creates it on the GUI thread, and it lives until the process ends."""
    global _DELIVERY
    if _DELIVERY is None:
        _DELIVERY = _Delivery()
    return _DELIVERY


class _LoadJob(QRunnable):
    """Run one resolve on the thread pool and emit its _Loaded to the shared delivery object.

    The job holds plain data only: an owner token, a generation number, the work, and the emit of
    the delivery object. It never holds the tab, so the worker never touches a tab, even one that
    was deleted while the load ran. A failure becomes a read_failed Resolution that names the
    exception class only, so a raw exception, and any value inside it, never reaches the UI thread.
    """

    def __init__(
        self,
        owner: object,
        generation: int,
        work: Callable[[], Resolution],
        deliver: Callable[[_Loaded], None],
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._owner = owner
        self._generation = generation
        self._work = work
        self._deliver = deliver

    def run(self) -> None:
        try:
            outcome = self._work()
        except Exception as exc:  # noqa: BLE001 - a worker must never raise into the pool
            outcome = Resolution(STATE_READ_FAILED, None, message=f"Projects could not be loaded ({exc.__class__.__name__}).")
        self._deliver(_Loaded(self._owner, self._generation, outcome))


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
        _delivery().delivered.connect(self._on_delivered)
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
        # The job gets the store and the sources, not self: a worker never holds the tab.
        work = functools.partial(_settings_resolution, self._settings, self._sources)
        QThreadPool.globalInstance().start(_LoadJob(self._owner, self._generation, work, _delivery().delivered.emit))

    @Slot(object)
    def _on_delivered(self, loaded: _Loaded) -> None:
        # Runs on the GUI thread. Another tab's load, or a load a newer one has superseded, is dropped.
        if loaded.owner is not self._owner or loaded.generation != self._generation:
            return
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
