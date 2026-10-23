"""Tunnel tab: whether the local tunnel is running, then the slot table. Qt code, so it sits in ui/.

The top of the tab says whether the tunnel is running. That line comes from one read-only runner
status call for the tunnel scope (see meridian_desktop/tunnel_status.py). The call runs on the thread
pool each time the tab is shown, so the UI thread never waits on it. The page starts no timer and
makes no request until it is shown. Below the status line are Start, Stop and Restart for the tunnel
(see meridian_desktop/ui/scope_controls.py). They are on while the tunnel_control_enabled setting is true, which is
its default (owner decision, 2026-10-23), and each one asks the owner first, in a dialog that names the exact command
(docs/local-trust-contract.md).
The same top section names the tools that connect through the tunnel, and tells the owner how to start
the tunnel by hand in a terminal.

Below that is the slot table. It shows a snapshot the caller hands in (see meridian_desktop/tunnel_view.py
for the rows). Until a snapshot is given, the slot line says the slot states are not available, because
this build has no local source for them. That line names the missing core item (MS-104). Every cell is
plain text (QTableWidgetItem and PlainText labels), so a slot name or note is never read as HTML.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from PySide6.QtCore import QRunnable, Qt, QThreadPool, Slot
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from meridian_desktop.runner_client import RunnerClient, RunnerResult
from meridian_desktop.runner_controls import SCOPE_TUNNEL
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.tunnel_status import (
    CHECK_FAILED_REASON,
    CHECKED_TEMPLATE,
    DEPENDENCY_TEXT,
    NO_FAILURE_NAME,
    NO_TIME_TEXT,
    NOT_RUNNING,
    RUNNING,
    START_TEXT,
    TIME_FORMAT,
    UNKNOWN,
    TunnelStatus,
    read_tunnel_status,
)
from meridian_desktop.tunnel_view import slot_details, slots_of
from meridian_desktop.ui.delivery import Delivered, shared_delivery
from meridian_desktop.ui.runner_actions import RunnerActions
from meridian_desktop.ui.scope_controls import ScopeControls, tunnel_gate
from meridian_desktop.ui.text import enable_table_copy, make_selectable

NOT_AVAILABLE_TEXT = (
    "Tunnel slot states are not available. The core does not write a local tunnel slot status "
    "file that the desktop can read. What is missing: core item MS-104, which makes the core write "
    "that file. Nothing to do in the desktop until it ships."
)
NO_SLOTS_TEXT = "The tunnel snapshot lists no slots."
SHOWN_TEXT = "Slot states from the tunnel snapshot."
# The health columns come after Note, so the first three keep their positions.
SLOT_COLUMNS = ("Slot", "State", "Note", "Health basis", "Health age (s)")

# The running line. Until a check has finished, it never says running.
NOT_CHECKED_TEXT = "Tunnel: not checked yet. The check runs when this tab is shown."
CHECKING_TEXT = "Tunnel: checking..."
HEADLINES = {
    RUNNING: "Tunnel: running.",
    NOT_RUNNING: "Tunnel: not running.",
    UNKNOWN: "Tunnel: unknown.",
}


def _label(parent: QWidget, name: str, text: str) -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return make_selectable(label)


class TunnelCheckJob(QRunnable):
    """Run one tunnel check on the thread pool and deliver (status, failure_name).

    The job holds only the work and the deliver callable its caller builds. The caller's deliver emits to the shared
    delivery object, never to a window or page, so the worker never holds one (see TunnelPage.refresh and
    ui/main_window.py). A check that raises is reduced to its class name here, so a raw exception never reaches the UI.
    """

    def __init__(self, work: Callable[[], TunnelStatus], deliver: Callable[[tuple], None]) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._work = work
        self._deliver = deliver

    def run(self) -> None:
        status: TunnelStatus | None = None
        failure: str | None = None
        try:
            status = self._work()
        except Exception as exc:  # noqa: BLE001 - a worker must never raise into the pool
            failure = exc.__class__.__name__
        try:
            self._deliver((status, failure))
        except RuntimeError:
            pass  # the shared delivery object is gone, for example at shutdown


class TunnelPage(QWidget):
    """The Tunnel tab: the tunnel's running state, the Start, Stop and Restart controls, then the slot table.

    The controls are off only when the tunnel_control_enabled setting is false or the core is not set, and each one asks
    the owner first.
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        status: Callable[[str], RunnerResult] | None = None,
        clock: Callable[[], datetime] = datetime.now,
        actions: RunnerActions | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("tunnel-page")
        # With no argument the check uses a RunnerClient that has no interpreter. It reports unknown and
        # starts nothing. The app can pass a configured client once the desktop has a setting for one.
        self._check_runner = status if status is not None else RunnerClient().status
        self._clock = clock
        self._actions = actions if actions is not None else RunnerActions(SettingsStore())
        self._busy = False
        # Identifies this page's checks. A plain object, so a job can hold it without holding the page.
        self._owner = object()
        # Each check takes the next number. A result with an older number is dropped.
        self._generation = 0
        shared_delivery().delivered.connect(self._on_delivered)
        layout = QVBoxLayout(self)

        self._run_state = _label(self, "tunnel-run-state", NOT_CHECKED_TEXT)
        layout.addWidget(self._run_state)
        self._run_reason = _label(self, "tunnel-run-reason", "")
        layout.addWidget(self._run_reason)
        self._run_checked = _label(self, "tunnel-run-checked", "")
        layout.addWidget(self._run_checked)
        self._run_dependency = _label(self, "tunnel-run-dependency", DEPENDENCY_TEXT)
        layout.addWidget(self._run_dependency)
        self._run_start = _label(self, "tunnel-run-start", START_TEXT)
        layout.addWidget(self._run_start)

        self._controls = ScopeControls(SCOPE_TUNNEL, self._actions, tunnel_gate, prefix="tunnel", parent=self)
        self._controls.actionFinished.connect(self._after_control)
        layout.addWidget(self._controls)

        self._status = _label(self, "tunnel-status", NOT_AVAILABLE_TEXT)
        layout.addWidget(self._status)

        self._table = QTableWidget(0, len(SLOT_COLUMNS), self)
        self._table.setObjectName("tunnel-slot-table")
        self._table.setHorizontalHeaderLabels(list(SLOT_COLUMNS))
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        enable_table_copy(self._table)
        layout.addWidget(self._table, 1)

    def refresh(self) -> None:
        """Run one read-only tunnel status check on the thread pool. A check already running is not doubled."""
        if self._busy:
            return
        self._busy = True
        self._generation += 1
        generation = self._generation
        self._run_state.setText(CHECKING_TEXT)
        check, clock = self._check_runner, self._clock
        # The job holds the check, the clock, this page's owner token and the shared delivery object. It never holds
        # the page, and the deliver closure captures only the owner, the generation and the delivery object.
        owner = self._owner
        delivery = shared_delivery()

        def deliver(payload: tuple) -> None:
            delivery.delivered.emit(Delivered(owner, (generation, payload)))

        QThreadPool.globalInstance().start(TunnelCheckJob(lambda: read_tunnel_status(check, clock=clock), deliver))

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._controls.refresh_enabled()
        self.refresh()

    @Slot()
    def _after_control(self) -> None:
        """After a Start, Stop or Restart finishes, read the state again. An older status check's result is dropped."""
        self._busy = False
        self.refresh()

    @Slot(object)
    def _on_delivered(self, item: object) -> None:
        # Runs on the GUI thread. A result for another page or window, or for an older check, is dropped here.
        if not isinstance(item, Delivered) or item.owner is not self._owner:
            return
        generation, payload = item.value
        if generation != self._generation:
            return
        self._on_check_finished(payload)

    def _on_check_finished(self, payload: tuple) -> None:
        status, failure = payload
        self._busy = False
        if status is None:
            # The check raised before it could give an answer, so it has no time to show.
            self._run_state.setText(HEADLINES[UNKNOWN])
            self._run_reason.setText(CHECK_FAILED_REASON.format(failure or NO_FAILURE_NAME))
            self._run_checked.setText(NO_TIME_TEXT)
            return
        self._show_status(status)

    def _show_status(self, status: TunnelStatus) -> None:
        self._run_state.setText(HEADLINES.get(status.state, HEADLINES[UNKNOWN]))
        self._run_reason.setText(status.reason)
        self._run_checked.setText(CHECKED_TEMPLATE.format(status.checked_at.strftime(TIME_FORMAT)))

    def set_snapshot(self, snapshot: Any) -> None:
        """Show one row per slot in ``snapshot``. None, or a snapshot with no slots dict, says not available.

        The page has no start, stop or restart control. It only shows what it is given.
        """
        details = slot_details(snapshot)
        if slots_of(snapshot) is None:
            self._status.setText(NOT_AVAILABLE_TEXT)
        elif not details:
            self._status.setText(NO_SLOTS_TEXT)
        else:
            self._status.setText(SHOWN_TEXT)

        self._table.setRowCount(len(details))
        for index, detail in enumerate(details):
            row = detail.row
            cells = (row.slot, row.state, row.note, detail.health_basis, detail.health_age)
            for column, text in enumerate(cells):
                self._table.setItem(index, column, QTableWidgetItem(text))


__all__ = [
    "CHECKED_TEMPLATE",
    "CHECKING_TEXT",
    "HEADLINES",
    "NOT_AVAILABLE_TEXT",
    "NOT_CHECKED_TEXT",
    "NO_SLOTS_TEXT",
    "NO_TIME_TEXT",
    "SHOWN_TEXT",
    "SLOT_COLUMNS",
    "TunnelPage",
]
