"""RunPod panel for the Settings tab: key storage, a key check, the pod list and the server state.

This is Qt code, and the only new Qt module. It sits in ui/, so the boundary check allows it.

The panel never shows the key. It stores the key through the secret store, and Meridian
reads it from the keychain on each check. Every keychain call and every loopback call
runs on the thread pool, so the UI never blocks. Results and errors are plain text
(PlainText labels, plain table cells), so server text is never read as HTML.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QRunnable, Qt, QThreadPool, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from meridian_desktop import local_client
from meridian_desktop.local_client import LocalClientError
from meridian_desktop.secret_store import RUNPOD_ACCOUNT, RUNPOD_SERVICE, SecretStore, SecretStoreError
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.ui.delivery import Delivered, Delivery, shared_delivery
from meridian_desktop.ui.text import enable_table_copy, make_selectable

PROBE_TIMEOUT = 2.0
RUNPOD_TIMEOUT = 30.0
POD_COLUMNS = ("id", "name", "status", "gpu", "costPerHour", "uptimeSeconds")
MAX_SERVER_TEXT = 300
# The pods table keeps room for a few rows inside the Keys section, so the card never collapses to a sliver.
PODS_MIN_HEIGHT = 96
PODS_MAX_HEIGHT = 160

KEY_STORED_TEXT = "A RunPod key is stored in the keychain."
KEY_MISSING_TEXT = "No RunPod key is stored."
SERVER_UNCHECKED_TEXT = "Local Meridian server: not checked yet"


def _truncate(text: str, limit: int = MAX_SERVER_TEXT) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def describe_local_error(error: LocalClientError) -> str:
    """Plain text for a failed local request. It uses this module's wording only."""
    code = error.code
    if code == "not_running":
        return "The local Meridian server is not running. Start Meridian on this computer, then try again."
    if code == "timeout":
        return "The local Meridian server did not answer in time."
    if code == "unreachable":
        return "The local Meridian server could not be reached."
    if code == "not_loopback":
        return "The local server address in Settings is not a loopback address, so no request was sent."
    if code == "local_only":
        return "Meridian accepts RunPod requests only from this computer, and it refused this one."
    if code == "hosted_refused":
        return "This Meridian server is hosted. RunPod checks run only on a self-hosted Meridian."
    if code == "http_404":
        return "This Meridian server has no RunPod routes (HTTP 404). Update Meridian to a build that includes them."
    if code == "redirect_refused":
        return "The local Meridian server sent a redirect, which the desktop does not follow."
    if code == "too_large":
        return "The local Meridian server sent a response that is too large to read."
    if code == "bad_json":
        return "The local Meridian server sent a response the desktop could not read."
    if code.startswith("http_"):
        return f"The local Meridian server returned HTTP {code[len('http_'):]}."
    return "The local Meridian server returned an error."


def not_sent_text(state: str | None) -> str:
    """The result line when the local server is not running as Meridian, so nothing was sent."""
    if state == local_client.HEALTH_NOT_RUNNING:
        return "Request not sent: the local Meridian server is not running."
    if state == local_client.HEALTH_NOT_MERIDIAN:
        return "Request not sent: the server at the local address is not Meridian."
    return "Request not sent: the local Meridian server could not be checked."


def server_state_text(state: str | None, url: str) -> str:
    """The one-line local server status, from a probe_health result."""
    if state == local_client.HEALTH_RUNNING:
        return f"Local Meridian server: running at {url}"
    if state == local_client.HEALTH_NOT_RUNNING:
        return f"Local Meridian server: not running at {url}. Start Meridian on this computer."
    if state == local_client.HEALTH_NOT_MERIDIAN:
        return f"Local Meridian server: something answers at {url}, but it is not Meridian."
    if state == local_client.HEALTH_ERROR:
        return f"Local Meridian server: could not be checked at {url}."
    return SERVER_UNCHECKED_TEXT


def _cell(value: Any) -> str:
    return "" if value is None else str(value)


def pod_row(pod: dict[str, Any]) -> tuple[str, ...]:
    """Cells for one pod, in POD_COLUMNS order. A missing value is an empty cell."""
    gpu = pod.get("gpu")
    gpu_text = ""
    if isinstance(gpu, dict) and gpu.get("id") is not None:
        gpu_text = str(gpu["id"]) if gpu.get("count") is None else f"{gpu['id']} x{gpu['count']}"
    return (
        _cell(pod.get("id")),
        _cell(pod.get("name")),
        _cell(pod.get("status")),
        gpu_text,
        _cell(pod.get("costPerHour")),
        _cell(pod.get("uptimeSeconds")),
    )


def _server_error_text(payload: dict[str, Any]) -> str:
    error = payload.get("error")
    message = error.get("message") if isinstance(error, dict) else None
    if isinstance(message, str) and message:
        return _truncate(message)
    return "Meridian did not say why the check failed."


def describe_check(payload: dict[str, Any]) -> str:
    if payload.get("ok") is not True:
        return f"RunPod key check failed: {_server_error_text(payload)}"
    source = payload.get("credential_source") or "not reported"
    billing = "available" if payload.get("billing_available") else "not available"
    return (
        f"RunPod key works. Key source: {source}. Billing: {billing}. "
        f"Pods on the first page: {payload.get('pod_count', 0)}."
    )


def describe_pod_list(payload: dict[str, Any], pods: list[dict[str, Any]]) -> str:
    text = f"Listed {len(pods)} pod(s)."
    if payload.get("has_more_pods") is True:
        text += " More pods exist than this page shows."
    return text


@dataclass
class _Outcome:
    """What one local job found: the probed state, a payload, or a failure text."""

    url: str
    state: str | None = None
    payload: dict[str, Any] | None = None
    failure: str | None = None


@dataclass(frozen=True)
class _JobDone:
    """One finished job, as it is handed back: its job id, the callable's result, and the error text if it failed."""

    job_id: int
    result: Any
    error: str | None


class _Work:
    """The panel's work on the thread pool: the keychain calls and the loopback calls, through the stores and client it was given.

    It holds no widget. A job that holds one of these methods never keeps the panel alive, and never touches it. The
    panel holds one of these and hands its methods to the thread pool.
    """

    def __init__(self, settings_store: SettingsStore, secret_store: SecretStore, client: Any) -> None:
        self._settings = settings_store
        self._secrets = secret_store
        self._client = client

    def read_key_present(self) -> bool:
        """True when a key is stored. The value stays in the worker and is never returned."""
        return self._secrets.get_secret(RUNPOD_SERVICE, RUNPOD_ACCOUNT) is not None

    def set_key(self, value: str) -> Any:
        return self._secrets.set_secret(RUNPOD_SERVICE, RUNPOD_ACCOUNT, value)

    def delete_key(self) -> Any:
        return self._secrets.delete_secret(RUNPOD_SERVICE, RUNPOD_ACCOUNT)

    def local(self, fetch: Callable[[str, float], dict[str, Any]] | None) -> _Outcome:
        """Probe the local server, then run ``fetch`` only when it is running. Runs on a worker."""
        url = self._settings.load().settings.local_url
        state = self._client.probe_health(url, PROBE_TIMEOUT)
        outcome = _Outcome(url=url, state=state)
        if fetch is not None and state == local_client.HEALTH_RUNNING:
            try:
                outcome.payload = fetch(url, RUNPOD_TIMEOUT)
            except LocalClientError as exc:
                outcome.failure = describe_local_error(exc)
        return outcome

    def probe_only(self) -> _Outcome:
        return self.local(None)


class _Job(QRunnable):
    """Run one callable on the thread pool and deliver a _JobDone to the shared delivery object.

    The job holds only the callable, the owner token, its job id and the shared delivery object. It never holds the
    panel. The error text is produced here, in the worker, so a raw exception never reaches the UI thread. An
    exception that could hold a value is reduced to its class name.
    """

    def __init__(self, job_id: int, work: Callable[[], Any], owner: object, delivery: Delivery) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._job_id = job_id
        self._work = work
        self._owner = owner
        self._delivery = delivery

    def run(self) -> None:
        result: Any = None
        error: str | None = None
        try:
            result = self._work()
        except LocalClientError as exc:
            error = describe_local_error(exc)
        except SecretStoreError as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 - a worker must never raise into the pool
            error = f"Unexpected {exc.__class__.__name__}. Details were not shown."
        self._delivery.delivered.emit(Delivered(self._owner, _JobDone(self._job_id, result, error)))


def _label(parent: QWidget, name: str, text: str) -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return make_selectable(label)


class RunPodPanel(QWidget):
    """Settings tab content: key storage, a key check, the pod list and the server state."""

    #: Emitted when no background job is running.
    idle = Signal()

    @property
    def busy(self) -> bool:
        """True while a keychain or loopback job is running."""
        return self._pending > 0

    def __init__(
        self,
        settings_store: SettingsStore,
        secret_store: SecretStore,
        client: Any = local_client,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("runpod-panel")
        self._client = client
        self._work = _Work(settings_store, secret_store, client)
        # Identifies this panel's jobs. A plain object, so a job can hold it without holding the panel.
        self._owner = object()
        self._job_ids = itertools.count(1)
        # Each waiting job's handler, as an unbound function. A bound method would make the panel hold itself.
        self._handlers: dict[int, Callable[[RunPodPanel, Any, str | None], None]] = {}
        self._pending = 0
        self._probed_on_show = False
        shared_delivery().delivered.connect(self._on_delivered)
        self._build()
        self._submit(self._work.read_key_present, RunPodPanel._on_key_checked)

    # ----------------------------------------------------------- layout

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        # Every row of the panel sits in one entry card. The Settings tab's Keys section takes the card with take_card().
        self._card = QWidget(self)
        self._card.setObjectName("runpod-entry-card")
        layout = QVBoxLayout(self._card)
        layout.setContentsMargins(0, 0, 0, 0)

        self._server_status = _label(self, "runpod-server-status", SERVER_UNCHECKED_TEXT)
        layout.addWidget(self._server_status)

        self._key_status = _label(self, "runpod-key-status", "Checking key storage...")
        layout.addWidget(self._key_status)

        key_row = QHBoxLayout()
        self._key_input = QLineEdit(self)
        self._key_input.setObjectName("runpod-key-input")
        self._key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_input.setPlaceholderText("RunPod API key")
        key_row.addWidget(self._key_input, 1)
        self._save_button = QPushButton("Save key", self)
        self._save_button.setObjectName("runpod-save-key")
        key_row.addWidget(self._save_button)
        self._remove_button = QPushButton("Remove key", self)
        self._remove_button.setObjectName("runpod-remove-key")
        key_row.addWidget(self._remove_button)
        layout.addLayout(key_row)

        action_row = QHBoxLayout()
        self._test_button = QPushButton("Test key", self)
        self._test_button.setObjectName("runpod-test-key")
        action_row.addWidget(self._test_button)
        self._refresh_button = QPushButton("Refresh pods", self)
        self._refresh_button.setObjectName("runpod-refresh-pods")
        action_row.addWidget(self._refresh_button)
        action_row.addStretch(1)
        layout.addLayout(action_row)

        self._result = _label(self, "runpod-result", "")
        # The result line takes no room until it has text, so an empty line never leaves a gap above the pods table.
        self._result.setVisible(False)
        layout.addWidget(self._result)

        self._pods = QTableWidget(0, len(POD_COLUMNS), self)
        self._pods.setObjectName("runpod-pods")
        self._pods.setMinimumHeight(PODS_MIN_HEIGHT)
        self._pods.setMaximumHeight(PODS_MAX_HEIGHT)
        self._pods.setHorizontalHeaderLabels(list(POD_COLUMNS))
        self._pods.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._pods.verticalHeader().setVisible(False)
        header = self._pods.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        enable_table_copy(self._pods)
        layout.addWidget(self._pods, 1)
        outer.addWidget(self._card)

        self._save_button.clicked.connect(self._on_save_clicked)
        self._remove_button.clicked.connect(self._on_remove_clicked)
        self._test_button.clicked.connect(self._on_test_clicked)
        self._refresh_button.clicked.connect(self._on_refresh_clicked)

    def _set_busy(self, busy: bool) -> None:
        for widget in (self._save_button, self._remove_button, self._test_button, self._refresh_button, self._key_input):
            widget.setEnabled(not busy)

    # ----------------------------------------------------------- jobs

    def _submit(self, work: Callable[[], Any], handler: Callable[[RunPodPanel, Any, str | None], None]) -> None:
        job_id = next(self._job_ids)
        self._handlers[job_id] = handler
        self._pending += 1
        self._set_busy(True)
        # The job holds the callable, the owner token, its job id and the shared delivery object. It never holds the panel.
        QThreadPool.globalInstance().start(_Job(job_id, work, self._owner, shared_delivery()))

    @Slot(object)
    def _on_delivered(self, item: object) -> None:
        # Runs on the GUI thread. A result for another panel is dropped here, and so is one for a job this panel is not
        # waiting for. Every job has its own id, and each one's handler must run, so no newer job supersedes another.
        if not isinstance(item, Delivered) or item.owner is not self._owner:
            return
        done = item.value
        if not isinstance(done, _JobDone) or done.job_id not in self._handlers:
            return
        handler = self._handlers.pop(done.job_id)
        self._pending -= 1
        handler(self, done.result, done.error)
        self._set_busy(self._pending > 0)
        if self._pending == 0:
            self.idle.emit()

    def _set_result(self, text: str) -> None:
        """Show the result line with this text. An empty text hides the line, so it leaves no gap."""
        self._result.setText(text)
        self._result.setVisible(bool(text))

    def _take(self, result: Any, error: str | None) -> _Outcome | None:
        """Return the job's outcome, or show the error and return None."""
        if error is not None or not isinstance(result, _Outcome):
            self._set_result(error or "No result was produced.")
            return None
        if result.state is not None:
            self._server_status.setText(server_state_text(result.state, result.url))
        return result

    def _on_key_checked(self, result: Any, error: str | None) -> None:
        if error is not None:
            self._key_status.setText(f"Key storage could not be read. {error}")
            return
        self._key_status.setText(KEY_STORED_TEXT if result else KEY_MISSING_TEXT)

    # ----------------------------------------------------------- key actions

    @Slot()
    def _on_save_clicked(self) -> None:
        value = self._key_input.text()
        if not value.strip():
            self._set_result("Type a key first, then press Save key.")
            return
        work = self._work
        self._submit(lambda: work.set_key(value), RunPodPanel._on_save_done)

    def _on_save_done(self, result: Any, error: str | None) -> None:
        if error is not None:
            self._set_result(error)
            return
        self._key_input.clear()
        self._key_status.setText(KEY_STORED_TEXT)
        self._set_result("Key saved in the keychain.")

    @Slot()
    def _on_remove_clicked(self) -> None:
        self._submit(self._work.delete_key, RunPodPanel._on_remove_done)

    def _on_remove_done(self, result: Any, error: str | None) -> None:
        if error is not None:
            self._set_result(error)
            return
        self._key_status.setText(KEY_MISSING_TEXT)
        self._set_result("Key removed from the keychain." if result else "No key was stored, so nothing was removed.")

    # ----------------------------------------------------------- Meridian actions

    @Slot()
    def _on_test_clicked(self) -> None:
        check = self._client.runpod_check
        work = self._work
        self._submit(lambda: work.local(check), RunPodPanel._on_test_done)

    def _on_test_done(self, result: Any, error: str | None) -> None:
        outcome = self._take(result, error)
        if outcome is None:
            return
        if outcome.failure is not None:
            self._set_result(outcome.failure)
        elif outcome.state != local_client.HEALTH_RUNNING:
            self._set_result(not_sent_text(outcome.state))
        else:
            self._set_result(describe_check(outcome.payload or {}))

    @Slot()
    def _on_refresh_clicked(self) -> None:
        listing = self._client.runpod_list_pods
        work = self._work
        self._submit(lambda: work.local(listing), RunPodPanel._on_refresh_done)

    def _on_refresh_done(self, result: Any, error: str | None) -> None:
        outcome = self._take(result, error)
        if outcome is None:
            return
        if outcome.failure is not None:
            self._clear_pods()
            self._set_result(outcome.failure)
            return
        if outcome.state != local_client.HEALTH_RUNNING:
            self._clear_pods()
            self._set_result(not_sent_text(outcome.state))
            return
        payload = outcome.payload or {}
        if payload.get("ok") is not True:
            self._clear_pods()
            self._set_result(f"RunPod pod list failed: {_server_error_text(payload)}")
            return
        raw = payload.get("pods")
        pods = [pod for pod in raw if isinstance(pod, dict)] if isinstance(raw, list) else []
        self._fill_pods(pods)
        self._set_result(describe_pod_list(payload, pods))

    def _fill_pods(self, pods: list[dict[str, Any]]) -> None:
        self._pods.setRowCount(len(pods))
        for row, pod in enumerate(pods):
            for column, cell in enumerate(pod_row(pod)):
                self._pods.setItem(row, column, QTableWidgetItem(cell))

    def _clear_pods(self) -> None:
        self._pods.setRowCount(0)

    # ----------------------------------------------------------- first show

    def take_card(self) -> QWidget:
        """Give the entry card to the host that places it, such as the Keys section. The panel keeps its jobs and signals.

        The panel is left with no rows of its own. Its first-show probe still runs through ensure_probed.
        """
        self.layout().removeWidget(self._card)
        return self._card

    def ensure_probed(self) -> None:
        """Probe the local server once, on the first call. A host that places the card calls this when it is shown."""
        if not self._probed_on_show:
            self._probed_on_show = True
            self._submit(self._work.probe_only, RunPodPanel._take_probe_only)

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        self.ensure_probed()

    def _take_probe_only(self, result: Any, error: str | None) -> None:
        self._take(result, error)


__all__ = [
    "KEY_MISSING_TEXT",
    "KEY_STORED_TEXT",
    "POD_COLUMNS",
    "RunPodPanel",
    "describe_check",
    "describe_local_error",
    "describe_pod_list",
    "pod_row",
    "server_state_text",
]
