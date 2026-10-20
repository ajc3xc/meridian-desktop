"""Logs tab: a read-only look at the tunnel log and the runner's log tail.

This is Qt code and sits in ui/, so the boundary check allows it. The page reads nothing when it
is built or shown, starts no timer and polls nothing. It reads only when Refresh is pressed, and
the reads run on the thread pool, so the UI thread never blocks. The two reads are:

- the tunnel log: the bounded, redacted tail that log_tail.read_tunnel_log_tail returns;
- the runner log: the tail-log reply of the runner client. runner_client passes only the read
  verbs, so no mutating verb is ever run from this page. Each line of the runner's output goes
  through the same redaction pass as the tunnel log.

The page has one button, Refresh. It has no clear, delete, rotate or truncate control, and it never
writes to either log. Both panes are read-only plain text (QPlainTextEdit with setPlainText), and
every label is PlainText, so a log line is never read as HTML.

A missing log shows "No log yet". A log that cannot be read shows fixed text. The desktop has no
setting for the core's interpreter or runner scope yet, so the runner pane reads "not available"
and starts nothing, the same as the Services tab.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from PySide6.QtCore import QRunnable, Qt, QThreadPool, Signal, Slot
from PySide6.QtWidgets import QLabel, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget

from meridian_desktop.log_tail import (
    STATUS_OK,
    UNREADABLE_TEXT,
    LogTail,
    read_tunnel_log_tail,
    redact_line,
)
from meridian_desktop.runner_client import ERR_NOT_AVAILABLE, NOT_AVAILABLE_MESSAGE, RunnerResult
from meridian_desktop.ui.text import make_selectable

REFRESH_BUTTON_TEXT = "Refresh"
INTRO_UNREAD_TEXT = "Press Refresh to read the last part of both logs. Nothing is read until then."
INTRO_READING_TEXT = "Reading the logs..."
INTRO_SHOWN_TEXT = "Both logs are shown read-only, with secret-shaped values redacted. Press Refresh to read them again."
TUNNEL_HEADING = "Tunnel log:"
TUNNEL_HEADING_PATH = "Tunnel log: {}"
RUNNER_HEADING = "Runner log (tail-log):"
READING_TEXT = "reading..."
EMPTY_TUNNEL_TEXT = "The tunnel log is empty."
RUNNER_EMPTY_TEXT = "No log yet. The runner has not written a log file for this scope."
RUNNER_NOT_AVAILABLE_PREFIX = "Runner log not available."
NO_LOG_TAIL_TEXT = "The runner reply did not include a log tail."
FAILED_TEXT = "The {} check failed unexpectedly ({})."
NO_RESULT = "NoResult"


@dataclass(frozen=True)
class LogsOutcome:
    """One pass of the two reads. A read that raised is None here, and its class name is in ``failures``."""

    tunnel: LogTail | None = None
    runner: RunnerResult | None = None
    failures: dict[str, str] = field(default_factory=dict)


def runner_tail_not_configured() -> RunnerResult:
    """The runner read for this build. The desktop sets no core interpreter or runner scope,
    so no runner command is started. The result is the runner client's own not_available result."""
    return RunnerResult(
        verb="tail-log", ok=False, error_code=ERR_NOT_AVAILABLE, error_message=NOT_AVAILABLE_MESSAGE
    )


def _lines(text: str) -> list[str]:
    """The lines of ``text``, with line endings removed. A final newline does not add an empty line."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def runner_log_text(result: RunnerResult) -> str:
    """The runner's log tail as the pane shows it, or the reason it is not available.

    Each line goes through redact_line, the same pass the tunnel log gets. An empty tail is the
    runner's "no log yet". A reply with no text tail reads as a missing piece, not as an empty log.
    """
    if not result.ok:
        return f"{RUNNER_NOT_AVAILABLE_PREFIX} {result.error_message}"
    tail = (result.payload or {}).get("log_tail")
    if not isinstance(tail, str):
        return f"{RUNNER_NOT_AVAILABLE_PREFIX} {NO_LOG_TAIL_TEXT}"
    if not tail:
        return RUNNER_EMPTY_TEXT
    return "\n".join(redact_line(line) for line in _lines(tail))


def _guarded(name: str, failures: dict[str, str], work: Callable[[], Any]) -> Any:
    """Run one read. If it raises, record only its class name, and return None so the other read still runs."""
    try:
        return work()
    except Exception as exc:  # noqa: BLE001 - one failed read must not hide the other
        failures[name] = exc.__class__.__name__
        return None


def _heading(parent: QWidget, name: str, text: str) -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return make_selectable(label)


def _pane(parent: QWidget, name: str) -> QPlainTextEdit:
    pane = QPlainTextEdit(parent)
    pane.setObjectName(name)
    pane.setReadOnly(True)
    make_selectable(pane)  # after setReadOnly, which otherwise allows mouse selection only
    pane.setUndoRedoEnabled(False)
    return pane


class _ReadJob(QRunnable):
    """Run both reads on the thread pool and deliver (outcome, failure_name).

    Any exception that gets past the per-read guards is reduced to its class name here, so a
    raw exception never reaches the UI thread.
    """

    def __init__(self, work: Callable[[], LogsOutcome], deliver: Callable[[tuple], None]) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._work = work
        self._deliver = deliver

    def run(self) -> None:
        outcome: LogsOutcome | None = None
        failure: str | None = None
        try:
            outcome = self._work()
        except Exception as exc:  # noqa: BLE001 - a worker must never raise into the pool
            failure = exc.__class__.__name__
        try:
            self._deliver((outcome, failure))
        except RuntimeError:
            pass  # the page was deleted while the reads ran


class LogsPage(QWidget):
    """The Logs tab: one Refresh button, the tunnel log tail and the runner log tail.

    The two reads are injected. The app passes the defaults above. Tests pass fakes or temp files.
    """

    _reads_finished = Signal(object)

    def __init__(
        self,
        *,
        tunnel_log: Callable[[], LogTail] = read_tunnel_log_tail,
        runner: Callable[[], RunnerResult] = runner_tail_not_configured,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("logs-page")
        self._tunnel_log = tunnel_log
        self._runner = runner
        self._busy = False
        self._reads_finished.connect(self._on_reads_finished)
        self._build()

    @property
    def busy(self) -> bool:
        return self._busy

    # ----------------------------------------------------------- layout

    def _build(self) -> None:
        layout = QVBoxLayout(self)

        self._intro = _heading(self, "logs-intro", INTRO_UNREAD_TEXT)
        layout.addWidget(self._intro)

        self._tunnel_heading = _heading(self, "logs-tunnel-heading", f"{TUNNEL_HEADING} not read yet.")
        layout.addWidget(self._tunnel_heading)
        self._tunnel_text = _pane(self, "logs-tunnel-text")
        layout.addWidget(self._tunnel_text, 1)

        self._runner_heading = _heading(self, "logs-runner-heading", f"{RUNNER_HEADING} not read yet.")
        layout.addWidget(self._runner_heading)
        self._runner_text = _pane(self, "logs-runner-text")
        layout.addWidget(self._runner_text, 1)

        self._refresh_button = QPushButton(REFRESH_BUTTON_TEXT, self)
        self._refresh_button.setObjectName("logs-refresh")
        layout.addWidget(self._refresh_button)

        self._refresh_button.clicked.connect(self._on_refresh_clicked)

    # ----------------------------------------------------------- reads

    @Slot()
    def _on_refresh_clicked(self) -> None:
        if self._busy:
            return
        self._busy = True
        self._refresh_button.setEnabled(False)
        self._show_reading()
        QThreadPool.globalInstance().start(_ReadJob(self._collect, self._reads_finished.emit))

    def _collect(self) -> LogsOutcome:
        """Run the two reads in order. Runs on a worker thread, and never raises for one failed read."""
        failures: dict[str, str] = {}
        tunnel = _guarded("tunnel", failures, self._tunnel_log)
        runner = _guarded("runner", failures, self._runner)
        return LogsOutcome(tunnel=tunnel, runner=runner, failures=failures)

    @Slot(object)
    def _on_reads_finished(self, payload: tuple) -> None:
        outcome, failure = payload
        self._busy = False
        self._refresh_button.setEnabled(True)
        if failure is not None or not isinstance(outcome, LogsOutcome):
            self._show_all_failed(failure or NO_RESULT)
            return
        self._show(outcome)

    # ----------------------------------------------------------- display

    def _show_reading(self) -> None:
        self._intro.setText(INTRO_READING_TEXT)
        self._tunnel_heading.setText(f"{TUNNEL_HEADING} {READING_TEXT}")
        self._runner_heading.setText(f"{RUNNER_HEADING} {READING_TEXT}")
        self._tunnel_text.setPlainText("")
        self._runner_text.setPlainText("")

    def _show_all_failed(self, failure: str) -> None:
        text = FAILED_TEXT.format("logs", failure)
        self._intro.setText(INTRO_SHOWN_TEXT)
        self._tunnel_heading.setText(TUNNEL_HEADING)
        self._runner_heading.setText(RUNNER_HEADING)
        self._tunnel_text.setPlainText(text)
        self._runner_text.setPlainText(text)

    def _show(self, outcome: LogsOutcome) -> None:
        self._intro.setText(INTRO_SHOWN_TEXT)
        self._show_tunnel(outcome)
        self._show_runner(outcome)

    def _show_tunnel(self, outcome: LogsOutcome) -> None:
        tail = outcome.tunnel
        if not isinstance(tail, LogTail):
            self._tunnel_heading.setText(TUNNEL_HEADING)
            self._tunnel_text.setPlainText(
                FAILED_TEXT.format("tunnel log", outcome.failures.get("tunnel", NO_RESULT))
            )
            return
        self._tunnel_heading.setText(TUNNEL_HEADING_PATH.format(tail.path))
        if tail.status == STATUS_OK:
            # The reader already redacts. The page does it again, so no source can show a raw secret.
            lines = [redact_line(line) for line in tail.lines]
            self._tunnel_text.setPlainText("\n".join(lines) if lines else EMPTY_TUNNEL_TEXT)
        else:
            self._tunnel_text.setPlainText(tail.message or UNREADABLE_TEXT)

    def _show_runner(self, outcome: LogsOutcome) -> None:
        result = outcome.runner
        self._runner_heading.setText(RUNNER_HEADING)
        if not isinstance(result, RunnerResult):
            self._runner_text.setPlainText(
                FAILED_TEXT.format("runner log", outcome.failures.get("runner", NO_RESULT))
            )
            return
        self._runner_text.setPlainText(runner_log_text(result))


__all__ = [
    "EMPTY_TUNNEL_TEXT",
    "LogsOutcome",
    "LogsPage",
    "REFRESH_BUTTON_TEXT",
    "RUNNER_EMPTY_TEXT",
    "RUNNER_NOT_AVAILABLE_PREFIX",
    "runner_log_text",
    "runner_tail_not_configured",
]
