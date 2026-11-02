"""The GPUs tab: this computer's GPUs and GPU jobs, the SSH hosts saved in Settings, Slurm and LSF, and RunPod pods.

This is Qt code and sits in ui/, so the boundary check allows it. The panel has one control that does anything, the
Refresh button. Every table is read-only. There is no start, stop, kill, submit or edit control, and nothing is read
until the tab is first shown or Refresh is pressed.

Every read runs on the thread pool, never on the UI thread. The reads come back as parts, each one a separate delivery:
this computer's part (GPUs, processes, Slurm, LSF and RunPod) and one part per saved SSH host. A part is shown as soon as
it arrives, so one slow host never holds back the local tables. Each delivery is tagged with this panel's owner token, so a
result that belongs to another tab is dropped. While the tab is visible, a timer starts a local read on each tick.

The panel takes three injected callables. The window passes the real ones, and tests pass fakes:
- hosts_provider() -> sequence of SSH host names, from Settings. It runs on the worker thread.
- runpod_provider() -> the local Meridian server's RunPod pod list, or None when RunPod is not wired. It may raise.
- run: the program runner (gpu_jobs.Runner). Defaults to gpu_jobs.run_command.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from PySide6.QtCore import QRunnable, Qt, QThreadPool, QTimer, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from meridian_desktop import gpu_jobs
from meridian_desktop.gpu_jobs import GpuSnapshot, HostBackoff, Reading, Runner
from meridian_desktop.ui.delivery import Delivered, shared_delivery
from meridian_desktop.ui.text import enable_table_copy, make_selectable

REFRESH_MS = 5000
# A table never grows past this height. A longer list scrolls, so the rest of the page stays in view.
TABLE_MAX_HEIGHT = 220
GPU_COLUMNS = ("Host", "GPU", "Name", "Utilization", "Memory", "Temperature", "Power")
JOB_COLUMNS = ("Source", "Host", "Job", "User", "Name or command", "State", "GPU", "GPU memory")
STATUS_COLUMNS = ("Where", "Part", "Status")
INTRO_TEXT = (
    "GPUs and GPU jobs on this computer, the SSH hosts saved in Settings, Slurm and LSF, and RunPod pods. "
    "Read-only: nothing is started, stopped or changed."
)
REFRESH_TEXT = "Refresh"
NOT_READ_TEXT = "Press Refresh to read the GPUs. Nothing is read until then."
LOADING_TEXT = "Reading GPUs..."
CHECKED_PREFIX = "Checked at"
NO_GPUS_TEXT = "No GPUs to show yet."
NO_JOBS_TEXT = "No GPU jobs or processes to show."
GPUS_HEADING = "GPUs"
JOBS_HEADING = "GPU jobs and processes"
STATUS_HEADING = "Where each part was read"


@dataclass(frozen=True)
class LocalPart:
    """This computer's part, with the time it was read and the saved hosts it was read with."""

    checked_at: datetime
    hosts: tuple[str, ...]
    reading: Reading


@dataclass(frozen=True)
class RemotePart:
    """One saved host's part."""

    alias: str
    reading: Reading


class _ReadJob(QRunnable):
    """Run one read on the thread pool and deliver its part. A failure becomes the fixed failure part, so no raw
    exception reaches the UI thread."""

    def __init__(self, work: Callable[[], object], deliver: Callable[[object], None], failure: Callable[[], object]) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._work = work
        self._deliver = deliver
        self._failure = failure

    def run(self) -> None:
        try:
            part = self._work()
        except Exception:  # noqa: BLE001 - a worker must never raise into the pool
            part = self._failure()
        try:
            self._deliver(part)
        except RuntimeError:
            pass  # the panel was deleted while the read ran


def _table(parent: QWidget, name: str, columns: Sequence[str]) -> QTableWidget:
    table = QTableWidget(0, len(columns), parent)
    table.setObjectName(name)
    table.setHorizontalHeaderLabels(list(columns))
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.verticalHeader().setVisible(False)
    table.horizontalHeader().setStretchLastSection(True)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    table.setMaximumHeight(TABLE_MAX_HEIGHT)
    enable_table_copy(table)
    return table


def _fill(table: QTableWidget, rows: Sequence[Sequence[str]]) -> None:
    table.setRowCount(len(rows))
    for row_index, cells in enumerate(rows):
        for column, text in enumerate(cells):
            item = QTableWidgetItem(text)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            table.setItem(row_index, column, item)


def _label(parent: QWidget, name: str, text: str) -> QLabel:
    label = make_selectable(QLabel(text, parent))
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


class GpuJobsPanel(QWidget):
    """The GPUs tab. Read on first show and on Refresh, and while visible on a 5-second timer."""

    def __init__(
        self,
        hosts_provider: Callable[[], Sequence[str]],
        runpod_provider: Callable[[], Any] | None = None,
        parent: QWidget | None = None,
        *,
        run: Runner | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("gpu-jobs-panel")
        self._hosts_provider = hosts_provider
        self._runpod_provider = runpod_provider
        self._run: Runner = run if run is not None else gpu_jobs.run_command
        # Identifies this tab's reads. A plain object, so a job can hold it without holding the tab.
        self._owner = object()
        self._backoff = HostBackoff()
        self._local_in_flight = False
        self._remote_in_flight: set[str] = set()
        self._read_once = False
        # The last local part, and the last part for each saved host. The tables are the sum of these.
        self._checked_at: datetime | None = None
        self._hosts: tuple[str, ...] = ()
        self._local: Reading | None = None
        self._remote: dict[str, Reading] = {}
        shared_delivery().delivered.connect(self._on_delivered)
        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self._on_tick)
        self._build()

    # ----------------------------------------------------------- layout

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.addWidget(_label(self, "gpu-intro", INTRO_TEXT))

        row = QHBoxLayout()
        self._refresh = QPushButton(REFRESH_TEXT, self)
        self._refresh.setObjectName("gpu-refresh")
        self._refresh.clicked.connect(self.refresh)
        row.addWidget(self._refresh)
        self._status = _label(self, "gpu-status", NOT_READ_TEXT)
        row.addWidget(self._status, 1)
        layout.addLayout(row)

        self._summary = _label(self, "gpu-summary", "")
        layout.addWidget(self._summary)

        layout.addWidget(_label(self, "gpu-gpus-heading", GPUS_HEADING))
        self._gpus = _table(self, "gpu-gpus", GPU_COLUMNS)
        layout.addWidget(self._gpus)
        self._gpus_empty = _label(self, "gpu-gpus-empty", NO_GPUS_TEXT)
        layout.addWidget(self._gpus_empty)

        layout.addWidget(_label(self, "gpu-jobs-heading", JOBS_HEADING))
        self._jobs = _table(self, "gpu-jobs", JOB_COLUMNS)
        layout.addWidget(self._jobs)
        self._jobs_empty = _label(self, "gpu-jobs-empty", NO_JOBS_TEXT)
        layout.addWidget(self._jobs_empty)

        layout.addWidget(_label(self, "gpu-statuses-heading", STATUS_HEADING))
        self._statuses = _table(self, "gpu-statuses", STATUS_COLUMNS)
        layout.addWidget(self._statuses)

    # ----------------------------------------------------------- reads

    def refresh(self) -> None:
        """A read now. It does nothing while the local read runs, and the Refresh button is off until it ends."""
        if self._local_in_flight:
            return
        self._start_local()

    def _start_local(self) -> None:
        self._local_in_flight = True
        self._refresh.setEnabled(False)
        self._status.setText(LOADING_TEXT)
        # The job holds the providers, the runner, the owner token and the shared delivery object. It never holds this tab.
        owner = self._owner
        delivery = shared_delivery()
        hosts_provider = self._hosts_provider
        runpod_provider = self._runpod_provider
        run = self._run

        def work() -> LocalPart:
            hosts = tuple(hosts_provider())
            reading = gpu_jobs.read_local(run, runpod_provider)
            return LocalPart(datetime.now(timezone.utc), hosts, reading)

        def failure() -> LocalPart:
            return LocalPart(datetime.now(timezone.utc), (), gpu_jobs.failed_local())

        def deliver(part: object) -> None:
            delivery.delivered.emit(Delivered(owner, part))

        QThreadPool.globalInstance().start(_ReadJob(work, deliver, failure))

    def _start_remote(self, alias: str) -> None:
        if alias in self._remote_in_flight:
            return
        self._remote_in_flight.add(alias)
        owner = self._owner
        delivery = shared_delivery()
        run = self._run
        backoff = self._backoff

        def work() -> RemotePart:
            return RemotePart(alias, gpu_jobs.read_host(run, alias, backoff))

        def failure() -> RemotePart:
            return RemotePart(alias, gpu_jobs.failed_host(alias))

        def deliver(part: object) -> None:
            delivery.delivered.emit(Delivered(owner, part))

        QThreadPool.globalInstance().start(_ReadJob(work, deliver, failure))

    @Slot(object)
    def _on_delivered(self, item: object) -> None:
        # Runs on the GUI thread. A result for another tab, or anything that is not one of this tab's parts, is dropped.
        if not isinstance(item, Delivered) or item.owner is not self._owner:
            return
        if isinstance(item.value, LocalPart):
            self._on_local(item.value)
        elif isinstance(item.value, RemotePart):
            self._on_remote(item.value)

    def _on_local(self, part: LocalPart) -> None:
        self._local_in_flight = False
        self._refresh.setEnabled(True)
        self._checked_at = part.checked_at
        self._hosts = part.hosts
        self._local = part.reading
        for alias in list(self._remote):
            if alias not in part.hosts:
                del self._remote[alias]
        for alias in part.hosts:
            self._start_remote(alias)
        self._render()

    def _on_remote(self, part: RemotePart) -> None:
        self._remote_in_flight.discard(part.alias)
        # A host removed in Settings while its read ran is not shown.
        if part.alias in self._hosts:
            self._remote[part.alias] = part.reading
            self._render()

    def _render(self) -> None:
        if self._checked_at is None:
            return
        readings = [self._local or Reading()]
        readings.extend(self._remote[alias] for alias in self._hosts if alias in self._remote)
        self._show(gpu_jobs.assemble(self._checked_at, readings))

    def _show(self, snapshot: GpuSnapshot) -> None:
        checked = snapshot.checked_at.astimezone().strftime("%H:%M:%S")
        self._status.setText(f"{CHECKED_PREFIX} {checked} local time.")
        self._summary.setText(gpu_jobs.summary_text(snapshot))
        _fill(self._gpus, gpu_jobs.gpu_table_rows(snapshot))
        self._gpus_empty.setVisible(not snapshot.gpus)
        _fill(self._jobs, gpu_jobs.job_table_rows(snapshot))
        self._jobs_empty.setVisible(not snapshot.jobs)
        _fill(self._statuses, gpu_jobs.status_table_rows(snapshot))

    # ----------------------------------------------------------- timer

    def _on_tick(self) -> None:
        if not self._local_in_flight:
            self._start_local()

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
    "GPU_COLUMNS",
    "JOB_COLUMNS",
    "REFRESH_MS",
    "STATUS_COLUMNS",
    "TABLE_MAX_HEIGHT",
    "GpuJobsPanel",
    "LocalPart",
    "RemotePart",
]
