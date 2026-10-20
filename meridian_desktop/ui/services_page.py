"""Services tab: a read-only look at the local server, the runner and the tunnel slot ports.

This is Qt code and sits in ui/, so the boundary check allows it. The page makes no request,
starts no timer and polls nothing when it is built or shown. It checks only when Check services
is pressed, and the checks run on the thread pool, so the UI thread never blocks. The three checks:

- the local server health: one GET of /health at the local_url setting (local_client.probe_health);
- the runner status: one read-only runner call (runner_client passes only status and tail-log,
  never a mutating verb);
- the slot ports: a connect-only listening check on 127.0.0.1 (slot_ports.probe_slot_ports).

The page has one button, and it runs those checks. It has no start, stop, restart, preflight,
doctor or run-script control.

The values shown are the probe's and the runner's own text, unchanged (a state token is shown as
printed, and a slot's listening label is the probe's label). Every label is plain text (PlainText
labels and plain table cells), so a value from a probe is never read as HTML.

Degraded in this build, and shown as "not available": local_client.probe_health reports the
server's state only, so the engine version and git SHA stay unavailable until the health probe
reports them. The desktop has no setting for the core's interpreter or runner scope yet, so the
runner check reports not available and starts nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from PySide6.QtCore import QRunnable, Qt, QThreadPool, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from meridian_desktop import local_client
from meridian_desktop.runner_client import ERR_NOT_AVAILABLE, NOT_AVAILABLE_MESSAGE, RunnerResult
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.slot_ports import PROBE_HOST, SlotPortResult, probe_slot_ports
from meridian_desktop.ui.text import enable_table_copy, make_selectable

HEALTH_TIMEOUT = 2.0
NOT_AVAILABLE = "not available"
NOT_CHECKED = "not checked yet"
CHECK_BUTTON_TEXT = "Check services"
CHECKING_TEXT = "Checking..."
UNCHECKED_TEXT = "Press Check services to look at the local server, the runner and the slot ports. Nothing is checked until then."
FAILED_TEXT = "The {} check failed unexpectedly ({})."
NO_CHILD_STATE_TEXT = "The runner reply did not include a child state."
PORTS_NOTE = "A listening port only shows that something accepts a connection. It does not show that Meridian is healthy."
PORT_COLUMNS = ("Slot", "Port", "Listening state")


@dataclass(frozen=True)
class HealthFacts:
    """The health check's result. The version and git SHA are None when the probe does not report them."""

    state: str
    engine_version: str | None = None
    git_sha: str | None = None


@dataclass(frozen=True)
class ServicesOutcome:
    """One pass of the three checks. A check that raised is None here, and its class name is in ``failures``."""

    local_url: str
    health: HealthFacts | None = None
    runner: RunnerResult | None = None
    ports: tuple[SlotPortResult, ...] | None = None
    failures: dict[str, str] = field(default_factory=dict)


def probe_health_facts(base_url: str, timeout: float = HEALTH_TIMEOUT) -> HealthFacts:
    """The local server's state from local_client.probe_health.

    That probe returns the state only. It does not read the version or git SHA from the /health
    body, so both stay None and read not available until the health probe reports them.
    """
    return HealthFacts(state=local_client.probe_health(base_url, timeout))


def runner_not_configured() -> RunnerResult:
    """The runner check for this build. The desktop sets no core interpreter or runner scope,
    so no runner command is started. The result is the runner client's own not_available result."""
    return RunnerResult(
        verb="status", ok=False, error_code=ERR_NOT_AVAILABLE, error_message=NOT_AVAILABLE_MESSAGE
    )


def child_state_text(result: RunnerResult) -> str:
    """The runner's child state as the runner printed it, or the runner client's own message."""
    if not result.ok:
        return f"{NOT_AVAILABLE}. {result.error_message}"
    child = (result.payload or {}).get("child")
    state = child.get("state") if isinstance(child, dict) else None
    if isinstance(state, str) and state:
        return state
    return f"{NOT_AVAILABLE}. {NO_CHILD_STATE_TEXT}"


def _value_text(value: object) -> str:
    return value if isinstance(value, str) and value else NOT_AVAILABLE


def _guarded(name: str, failures: dict[str, str], work: Callable[[], Any]) -> Any:
    """Run one check. If it raises, record only its class name, and return None so the others still run."""
    try:
        return work()
    except Exception as exc:  # noqa: BLE001 - one failed check must not hide the others
        failures[name] = exc.__class__.__name__
        return None


def _label(parent: QWidget, name: str, text: str) -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return make_selectable(label)


class _ChecksJob(QRunnable):
    """Run the checks on the thread pool and deliver (outcome, failure_name).

    Any exception that gets past the per-check guards is reduced to its class name here, so a
    raw exception never reaches the UI thread.
    """

    def __init__(self, work: Callable[[], ServicesOutcome], deliver: Callable[[tuple], None]) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._work = work
        self._deliver = deliver

    def run(self) -> None:
        outcome: ServicesOutcome | None = None
        failure: str | None = None
        try:
            outcome = self._work()
        except Exception as exc:  # noqa: BLE001 - a worker must never raise into the pool
            failure = exc.__class__.__name__
        try:
            self._deliver((outcome, failure))
        except RuntimeError:
            pass  # the page was deleted while the checks ran


class ServicesPage(QWidget):
    """The Services tab: one button, three read-only checks, and their results.

    The three checks are injected. The app passes the defaults above. Tests pass fakes.
    """

    _checks_finished = Signal(object)

    def __init__(
        self,
        settings_store: SettingsStore,
        *,
        health: Callable[[str, float], HealthFacts] = probe_health_facts,
        runner: Callable[[], RunnerResult] = runner_not_configured,
        ports: Callable[[], Sequence[SlotPortResult]] = probe_slot_ports,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("services-page")
        self._settings = settings_store
        self._health = health
        self._runner = runner
        self._ports = ports
        self._busy = False
        self._checks_finished.connect(self._on_checks_finished)
        self._build()

    @property
    def busy(self) -> bool:
        return self._busy

    # ----------------------------------------------------------- layout

    def _build(self) -> None:
        layout = QVBoxLayout(self)

        self._intro = _label(self, "services-intro", UNCHECKED_TEXT)
        layout.addWidget(self._intro)

        self._health_label = _label(self, "services-health", f"Local server health: {NOT_CHECKED}")
        self._engine_version = _label(self, "services-engine-version", f"Engine version: {NOT_CHECKED}")
        self._git_sha = _label(self, "services-git-sha", f"Git SHA: {NOT_CHECKED}")
        self._runner_label = _label(self, "services-runner", f"Runner child state: {NOT_CHECKED}")
        for label in (self._health_label, self._engine_version, self._git_sha, self._runner_label):
            layout.addWidget(label)

        self._ports_status = _label(self, "services-ports-status", f"Slot ports: {NOT_CHECKED}")
        layout.addWidget(self._ports_status)

        self._table = QTableWidget(0, len(PORT_COLUMNS), self)
        self._table.setObjectName("services-ports-table")
        self._table.setHorizontalHeaderLabels(list(PORT_COLUMNS))
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        enable_table_copy(self._table)
        layout.addWidget(self._table, 1)

        self._check_button = QPushButton(CHECK_BUTTON_TEXT, self)
        self._check_button.setObjectName("services-check")
        layout.addWidget(self._check_button)

        self._check_button.clicked.connect(self._on_check_clicked)

    # ----------------------------------------------------------- checks

    @Slot()
    def _on_check_clicked(self) -> None:
        if self._busy:
            return
        self._busy = True
        self._check_button.setEnabled(False)
        self._show_checking()
        QThreadPool.globalInstance().start(_ChecksJob(self._collect, self._checks_finished.emit))

    def _collect(self) -> ServicesOutcome:
        """Run the three checks in order. Runs on a worker thread, and never raises for one failed check."""
        url = self._settings.load().settings.local_url
        failures: dict[str, str] = {}
        health = _guarded("health", failures, lambda: self._health(url, HEALTH_TIMEOUT))
        runner = _guarded("runner", failures, self._runner)
        ports_list = _guarded("ports", failures, lambda: tuple(self._ports()))
        return ServicesOutcome(
            local_url=url, health=health, runner=runner, ports=ports_list, failures=failures
        )

    @Slot(object)
    def _on_checks_finished(self, payload: tuple) -> None:
        outcome, failure = payload
        self._busy = False
        self._check_button.setEnabled(True)
        if failure is not None or not isinstance(outcome, ServicesOutcome):
            self._show_all_failed(failure or "NoResult")
            return
        self._show(outcome)

    # ----------------------------------------------------------- display

    def _show_checking(self) -> None:
        self._health_label.setText(f"Local server health: {CHECKING_TEXT}")
        self._engine_version.setText(f"Engine version: {CHECKING_TEXT}")
        self._git_sha.setText(f"Git SHA: {CHECKING_TEXT}")
        self._runner_label.setText(f"Runner child state: {CHECKING_TEXT}")
        self._ports_status.setText(f"Slot ports: {CHECKING_TEXT}")
        self._table.setRowCount(0)

    def _show_all_failed(self, failure: str) -> None:
        text = FAILED_TEXT.format("services", failure)
        self._health_label.setText(text)
        self._engine_version.setText(f"Engine version: {NOT_AVAILABLE}")
        self._git_sha.setText(f"Git SHA: {NOT_AVAILABLE}")
        self._runner_label.setText(text)
        self._ports_status.setText(text)
        self._table.setRowCount(0)

    def _show(self, outcome: ServicesOutcome) -> None:
        failures = outcome.failures
        self._show_health(outcome, failures)
        self._show_runner(outcome, failures)
        self._show_ports(outcome, failures)

    def _show_health(self, outcome: ServicesOutcome, failures: dict[str, str]) -> None:
        if outcome.health is None:
            self._health_label.setText(FAILED_TEXT.format("local server health", failures.get("health", "NoResult")))
            self._engine_version.setText(f"Engine version: {NOT_AVAILABLE}")
            self._git_sha.setText(f"Git SHA: {NOT_AVAILABLE}")
            return
        facts = outcome.health
        self._health_label.setText(f"Local server health ({outcome.local_url}): {_value_text(facts.state)}")
        self._engine_version.setText(f"Engine version: {_value_text(facts.engine_version)}")
        self._git_sha.setText(f"Git SHA: {_value_text(facts.git_sha)}")

    def _show_runner(self, outcome: ServicesOutcome, failures: dict[str, str]) -> None:
        if outcome.runner is None:
            self._runner_label.setText(FAILED_TEXT.format("runner", failures.get("runner", "NoResult")))
            return
        self._runner_label.setText(f"Runner child state: {child_state_text(outcome.runner)}")

    def _show_ports(self, outcome: ServicesOutcome, failures: dict[str, str]) -> None:
        if outcome.ports is None:
            self._ports_status.setText(FAILED_TEXT.format("slot port", failures.get("ports", "NoResult")))
            self._table.setRowCount(0)
            return
        self._table.setRowCount(len(outcome.ports))
        for row, result in enumerate(outcome.ports):
            for column, text in enumerate((result.slot, str(result.port), result.label)):
                self._table.setItem(row, column, QTableWidgetItem(text))
        self._ports_status.setText(f"{len(outcome.ports)} slot ports checked on {PROBE_HOST}. {PORTS_NOTE}")


__all__ = [
    "CHECK_BUTTON_TEXT",
    "HEALTH_TIMEOUT",
    "HealthFacts",
    "NOT_AVAILABLE",
    "PORT_COLUMNS",
    "ServicesOutcome",
    "ServicesPage",
    "child_state_text",
    "probe_health_facts",
    "runner_not_configured",
]
