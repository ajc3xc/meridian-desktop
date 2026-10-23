"""Start, Stop and Restart for one runner scope: the tunnel on the Tunnel tab, the local server in Settings. Qt code.

Every button asks the owner first, in a dialog that names the exact command, the scope and the working folder
(rule 2). Cancel changes nothing. The call runs on the thread pool, and its result comes back through the shared
delivery object with this widget's owner token and a generation number, so another widget's result, or an older
call's, is dropped.

Restart is a stop, then a start, and the owner is asked before each one (rule 9). A stop that is not confirmed
ends the restart, so nothing is started after a stop the runner did not confirm.

The buttons are enabled only when the gate says so. For the tunnel the gate also needs tunnel_control_enabled
(rule 10). Both gates need the Meridian core checkout and its Python to be set. The gate's text is shown when the
buttons are off, so the owner sees why. After each finished call, after_action runs on the GUI thread, so the owner
can read the state again.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from meridian_desktop.runner_controls import (
    SCOPE_LABELS,
    START,
    STOP,
    ControlResult,
    confirmation_text,
)
from meridian_desktop.ui.delivery import Delivered, shared_delivery
from meridian_desktop.ui.runner_actions import CoreSetup, RunnerActions, submit
from meridian_desktop.ui.text import make_selectable

RESTART = "restart"
TUNNEL_OFF_TEXT = (
    "The tunnel controls are off, because tunnel_control_enabled is false in the desktop settings file. "
    "To turn them on, set it to true. The desktop never starts or stops the tunnel by itself."
)
CORE_NOT_SET_TEXT = (
    "Set the Meridian core checkout and its Python in the client setup section of the Settings tab first."
)
BUTTON_TEXT = {START: "Start {label}", STOP: "Stop {label}", RESTART: "Restart {label}"}
QUESTION_TEXT = {START: "Start the {label}?", STOP: "Stop the {label}?"}
RESTART_STOP_QUESTION = "Restart the {label}: stop it first?"
RESTART_START_QUESTION = "Restart the {label}: start it now?"
CONFIRM_TEXT = {START: "Start", STOP: "Stop"}
CANCELLED_TEXT = {
    START: "Start cancelled. Nothing was started.",
    STOP: "Stop cancelled. Nothing was stopped.",
    RESTART: "Restart cancelled. Nothing was stopped or started.",
}
BUSY_TEXT = {START: "Starting the {label}...", STOP: "Stopping the {label}..."}
RESTART_BUSY_TEXT = "Restarting the {label}: stopping it first..."
START_CANCELLED_TEXT = "Start cancelled. The {label} is stopped."
RESTART_STOPPED_TEXT = "Restart stopped: the stop was not confirmed, so nothing was started."
RESTART_BLOCKED_TEXT = (
    "Restart stopped before the start, so nothing was started. The controls are now off, or the core setup changed."
)
ACTION_FAILED_TEXT = "The {label} action could not be completed ({failure})."

Gate = Callable[[CoreSetup], tuple[bool, str]]


def tunnel_gate(core: CoreSetup) -> tuple[bool, str]:
    """The tunnel buttons are on only when tunnel_control_enabled is true and the core is set. Otherwise the text says why."""
    if not core.tunnel_control_enabled:
        return False, TUNNEL_OFF_TEXT
    if not core.core_ready:
        return False, CORE_NOT_SET_TEXT
    return True, ""


def local_gate(core: CoreSetup) -> tuple[bool, str]:
    """The local buttons are on only when the core checkout and its Python are set."""
    if not core.core_ready:
        return False, CORE_NOT_SET_TEXT
    return True, ""


def _label(parent: QWidget, name: str, text: str = "") -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return make_selectable(label)


class ScopeControls(QWidget):
    """The Start, Stop and Restart buttons for one scope, with the gate note and the last result. It runs one call at a time.

    ``actionFinished`` is emitted on the GUI thread after each finished call. The owner connects to it, so the owner
    can read the state again. A signal, not a callback, keeps this widget from holding its owner in memory.
    """

    actionFinished = Signal()

    def __init__(
        self,
        scope: str,
        actions: RunnerActions,
        gate: Gate,
        *,
        prefix: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName(f"{prefix}-controls")
        self._scope = scope
        self._label = SCOPE_LABELS[scope]
        self._actions = actions
        self._gate = gate
        self._busy = False
        self._enabled_by_gate = False
        # The call in flight: (python, working folder, "start" when a restart still has its start to do, or None).
        self._pending: tuple[str, str, str | None] | None = None
        # Identifies this widget's calls. A plain object, so a job can hold it without holding the widget.
        self._owner = object()
        self._generation = 0
        shared_delivery().delivered.connect(self._on_delivered)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self._start = QPushButton(BUTTON_TEXT[START].format(label=self._label), self)
        self._start.setObjectName(f"{prefix}-start")
        self._stop = QPushButton(BUTTON_TEXT[STOP].format(label=self._label), self)
        self._stop.setObjectName(f"{prefix}-stop")
        self._restart = QPushButton(BUTTON_TEXT[RESTART].format(label=self._label), self)
        self._restart.setObjectName(f"{prefix}-restart")
        for button in (self._start, self._stop, self._restart):
            row.addWidget(button)
        row.addStretch(1)
        layout.addLayout(row)
        self._note = _label(self, f"{prefix}-control-note")
        self._result = _label(self, f"{prefix}-control-result")
        self._result.setVisible(False)
        layout.addWidget(self._note)
        layout.addWidget(self._result)

        self._start.clicked.connect(self._on_start_clicked)
        self._stop.clicked.connect(self._on_stop_clicked)
        self._restart.clicked.connect(self._on_restart_clicked)
        self.refresh_enabled()

    # ----------------------------------------------------------- state

    def refresh_enabled(self) -> None:
        """Read the saved setup again and set the buttons and the gate note. Starts nothing."""
        enabled, note = self._gate(self._actions.core())
        self._enabled_by_gate = enabled
        self._note.setText(note)
        self._note.setVisible(bool(note))
        self._set_buttons()

    def _set_buttons(self) -> None:
        on = self._enabled_by_gate and not self._busy
        for button in (self._start, self._stop, self._restart):
            button.setEnabled(on)

    def _show(self, text: str) -> None:
        self._result.setText(text)
        self._result.setVisible(bool(text))

    def _finish(self) -> None:
        self._set_buttons()
        self.actionFinished.emit()

    def _start_allowed(self, python: str, cwd: str) -> bool:
        """True when the second step of a restart may start with these values.

        The gate must be on now, and the core setup must be the one the owner was shown.
        """
        core = self._actions.core()
        enabled, _note = self._gate(core)
        return enabled and (core.python, core.cwd) == (python, cwd)

    def _stop_restart_before_start(self, stop_text: str) -> None:
        """The restart ends after its stop: nothing starts, and the buttons read the gate again."""
        self._show(f"{stop_text} {RESTART_BLOCKED_TEXT}")
        self.refresh_enabled()
        self._finish()

    # ----------------------------------------------------------- asking and running

    @Slot()
    def _on_start_clicked(self) -> None:
        self._ask_and_run(START, restart=False)

    @Slot()
    def _on_stop_clicked(self) -> None:
        self._ask_and_run(STOP, restart=False)

    @Slot()
    def _on_restart_clicked(self) -> None:
        self._ask_and_run(STOP, restart=True)

    def _ask_and_run(self, subcommand: str, *, restart: bool) -> None:
        if self._busy:
            return
        core = self._actions.core()
        enabled, _note = self._gate(core)
        if not enabled:
            # The buttons are off, so nothing is asked and nothing runs. The gate note already says why.
            self.refresh_enabled()
            return
        first = STOP if restart else subcommand
        if restart:
            question = RESTART_STOP_QUESTION.format(label=self._label)
            cancelled = CANCELLED_TEXT[RESTART]
        else:
            question = QUESTION_TEXT[subcommand].format(label=self._label)
            cancelled = CANCELLED_TEXT[subcommand]
        body = confirmation_text(core.python, first, self._scope, core.cwd)
        if not self._actions.ask(self, question, body, CONFIRM_TEXT[first]):
            self._show(cancelled)
            return
        self._submit(first, core.python, core.cwd, then=START if restart else None, restart=restart)

    def _submit(self, subcommand: str, python: str, cwd: str, *, then: str | None, restart: bool = False) -> None:
        self._busy = True
        self._pending = (python, cwd, then)
        self._generation += 1
        generation = self._generation
        if restart:
            self._show(RESTART_BUSY_TEXT.format(label=self._label))
        else:
            self._show(BUSY_TEXT[subcommand].format(label=self._label))
        self._set_buttons()
        control = self._actions.control(python, cwd)
        scope = self._scope
        call = control.start if subcommand == START else control.stop
        submit(self._owner, generation, lambda: call(scope))

    @Slot(object)
    def _on_delivered(self, item: object) -> None:
        # Runs on the GUI thread. A result for another widget, an older call, or a call that is no longer pending is dropped.
        if not isinstance(item, Delivered) or item.owner is not self._owner:
            return
        generation, (result, failure) = item.value
        if generation != self._generation or not self._busy or self._pending is None:
            return
        python, cwd, then = self._pending
        self._busy = False
        self._pending = None
        if failure is not None or not isinstance(result, ControlResult):
            self._show(ACTION_FAILED_TEXT.format(label=self._label, failure=failure or "no result"))
            self._finish()
            return
        if then == START:
            if not result.confirmed:
                self._show(f"{result.message} {RESTART_STOPPED_TEXT}")
                self._finish()
                return
            # The gate is checked again before the start is asked, so a switch turned off after the stop is not asked.
            if not self._start_allowed(python, cwd):
                self._stop_restart_before_start(result.message)
                return
            # The stop is confirmed, so the owner is asked before the start, as rule 9 requires.
            body = confirmation_text(python, START, self._scope, cwd)
            question = RESTART_START_QUESTION.format(label=self._label)
            if not self._actions.ask(self, question, body, CONFIRM_TEXT[START]):
                self._show(f"{result.message} {START_CANCELLED_TEXT.format(label=self._label)}")
                self._finish()
                return
            # Checked once more after the dialog, just before the start runs. It uses the values the dialog showed.
            if not self._start_allowed(python, cwd):
                self._stop_restart_before_start(result.message)
                return
            self._submit(START, python, cwd, then=None)
            return
        self._show(result.message)
        self._finish()


__all__ = [
    "CORE_NOT_SET_TEXT",
    "RESTART",
    "TUNNEL_OFF_TEXT",
    "ScopeControls",
    "local_gate",
    "tunnel_gate",
]
