"""LaTeX engine panel for the Writing tab: one probe of the local engine, with both versions shown.

This is Qt code and sits in ui/, so the boundary check allows it. The panel makes no
request when it is built. It probes when it is first shown and again when Check again
is pressed. The probe runs on the thread pool, and the engine address comes from the
settings file, so the UI thread never blocks. Results are plain text (PlainText labels),
so engine text is never read as HTML.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QRunnable, Qt, QThreadPool, Signal, Slot
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from meridian_desktop import latex_client
from meridian_desktop.latex_client import PROBE_TIMEOUT, EngineStatus
from meridian_desktop.settings_store import SettingsStore

UNCHECKED_TEXT = "LaTeX engine: not checked yet"
CHECKING_TEXT = "LaTeX engine: checking..."
CONNECTED_TEXT = "LaTeX engine: connected."
NOT_CONNECTED_TEXT = "LaTeX engine: not connected."
VERSION_UNAVAILABLE = "not available"
FAILED_TEXT = "The check failed unexpectedly ({})."


def _label(parent: QWidget, name: str, text: str) -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


def engine_version_text(status: EngineStatus | None) -> str:
    """The engine version line. Versions are shown only for a connected engine."""
    if status is None or not status.connected:
        return f"Engine version: {VERSION_UNAVAILABLE}"
    return f"Engine version: {status.engine_version}"


def extension_version_text(status: EngineStatus | None) -> str:
    """The extension version line. Versions are shown only for a connected engine."""
    if status is None or not status.connected:
        return f"Extension version: {VERSION_UNAVAILABLE}"
    return f"Extension version: {status.extension_version}"


class _ProbeJob(QRunnable):
    """Run the probe on the thread pool and deliver (status, failure_text).

    The probe maps its own loopback errors to a status. Any other exception is reduced
    to its class name here, in the worker, so a raw exception never reaches the UI thread.
    """

    def __init__(self, work: Callable[[], EngineStatus], deliver: Callable[[tuple], None]) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._work = work
        self._deliver = deliver

    def run(self) -> None:
        status: EngineStatus | None = None
        failure: str | None = None
        try:
            status = self._work()
        except Exception as exc:  # noqa: BLE001 - a worker must never raise into the pool
            failure = FAILED_TEXT.format(exc.__class__.__name__)
        try:
            self._deliver((status, failure))
        except RuntimeError:
            pass  # the panel was deleted while the probe ran


class LatexPanel(QWidget):
    """The LaTeX section of the Writing tab: connected or not, with both versions side by side."""

    _probe_finished = Signal(object)

    def __init__(
        self,
        settings_store: SettingsStore,
        client: Any = latex_client,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("latex-panel")
        self._settings = settings_store
        self._client = client
        self._probed_on_show = False
        self._probing = False
        self._probe_finished.connect(self._on_probe_finished)
        self._build()

    # ----------------------------------------------------------- layout

    def _build(self) -> None:
        layout = QVBoxLayout(self)

        self._status = _label(self, "latex-status", UNCHECKED_TEXT)
        layout.addWidget(self._status)

        self._detail = _label(self, "latex-detail", "")
        layout.addWidget(self._detail)

        versions = QHBoxLayout()
        self._engine_version = _label(self, "latex-engine-version", engine_version_text(None))
        self._extension_version = _label(self, "latex-extension-version", extension_version_text(None))
        versions.addWidget(self._engine_version)
        versions.addWidget(self._extension_version)
        versions.addStretch(1)
        layout.addLayout(versions)

        action_row = QHBoxLayout()
        self._check_button = QPushButton("Check again", self)
        self._check_button.setObjectName("latex-check")
        action_row.addWidget(self._check_button)
        action_row.addStretch(1)
        layout.addLayout(action_row)

        self._check_button.clicked.connect(self._on_check_clicked)

    # ----------------------------------------------------------- probe

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        if not self._probed_on_show:
            self._probed_on_show = True
            self._start_probe()

    @Slot()
    def _on_check_clicked(self) -> None:
        self._start_probe()

    def _start_probe(self) -> None:
        if self._probing:
            return
        self._probing = True
        self._check_button.setEnabled(False)
        self._status.setText(CHECKING_TEXT)
        self._detail.setText("")
        QThreadPool.globalInstance().start(_ProbeJob(self._probe_work, self._probe_finished.emit))

    def _probe_work(self) -> EngineStatus:
        """Read the engine address from settings, then probe it. Runs on a worker thread."""
        url = self._settings.load().settings.engine_url
        return self._client.probe_engine(url, PROBE_TIMEOUT)

    @Slot(object)
    def _on_probe_finished(self, payload: tuple) -> None:
        status, failure = payload
        self._probing = False
        self._check_button.setEnabled(True)
        if failure is not None or not isinstance(status, EngineStatus):
            self._show_result(None, failure or FAILED_TEXT.format("NoResult"))
            return
        self._show_result(status, status.message)

    def _show_result(self, status: EngineStatus | None, detail: str) -> None:
        connected = status is not None and status.connected
        self._status.setText(CONNECTED_TEXT if connected else NOT_CONNECTED_TEXT)
        self._detail.setText(detail)
        self._engine_version.setText(engine_version_text(status))
        self._extension_version.setText(extension_version_text(status))


__all__ = [
    "CONNECTED_TEXT",
    "NOT_CONNECTED_TEXT",
    "UNCHECKED_TEXT",
    "LatexPanel",
    "engine_version_text",
    "extension_version_text",
]
