"""Start, Stop and Restart for one runner scope: the tunnel on the Tunnel tab, the local server in Settings. Qt code.

Every button asks the owner first, in a dialog that names the exact command, the scope and the working folder
(rule 2). Cancel changes nothing. The call runs on the thread pool, and its result comes back through the shared
delivery object with this widget's owner token and a generation number, so another widget's result, or an older
call's, is dropped.

Restart is a stop, then a start, and the owner is asked before each one (rule 9). A stop that is not confirmed
ends the restart, so nothing is started after a stop the runner did not confirm.

The buttons are enabled only when the gate says so. For the tunnel the gate also needs tunnel_control_enabled
(rule 10). Both gates need the Meridian core checkout and its Python to be set. The core is looked for at start, and
again by "Find it again" in Settings. The gate's text is shown when the buttons are off, so the owner sees why, and a
button next to the text does the one thing that turns them on: Choose the Meridian core folder, or Turn on the tunnel
controls. The buttons read the setup again whenever RunnerActions.coreChanged is emitted. After each finished call,
actionFinished and RunnerActions.runnerStateChanged are emitted on the GUI thread, so the owner and Home read the
state again.

A confirmed start is read from the status the runner prints as it starts the child, before the child can exit. So the
widget reads the status again about 3 and 20 seconds later (runner_controls.START_CHECK_DELAYS_SECONDS). A child that
is no longer running replaces the success line with "Started, but it stopped straight after. Check the control log.",
and the later check is dropped. A state that cannot be read takes the success line down too. These checks only read.
Any newer Start or Stop on the same scope, from this widget or another one, cancels them.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QTimer, Signal, Slot
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from meridian_desktop.runner_controls import (
    SCOPE_LABELS,
    START,
    START_CHECK_DELAYS_SECONDS,
    START_CHECK_UNKNOWN_MESSAGE,
    STOP,
    STOPPED_AFTER_START,
    STOPPED_AFTER_START_MESSAGE,
    ControlResult,
    confirmation_text,
)
from meridian_desktop.tunnel_status import RUNNING
from meridian_desktop.ui.delivery import Delivered, shared_delivery
from meridian_desktop.ui.runner_actions import CoreSetup, RunnerActions, submit
from meridian_desktop.ui.text import TextBlock

RESTART = "restart"
TUNNEL_OFF_TEXT = (
    "The tunnel controls are off. Turn them on with the button here. Each one still asks first, "
    "and the desktop never starts or stops the tunnel by itself."
)
CORE_NOT_SET_TEXT = (
    "The Meridian core was not found on this computer, so these buttons are off. "
    "Choose the Meridian core folder, and the desktop finds its Python."
)
# The button that does the one thing that turns the controls on, for each gate note that has one.
CHOOSE_CORE_BUTTON = "Choose the Meridian core folder"
TURN_ON_TUNNEL_BUTTON = "Turn on the tunnel controls"
FIX_CHOOSE_CORE = "choose-core"
FIX_TURN_ON_TUNNEL = "turn-on-tunnel"
FIX_BY_NOTE = {
    CORE_NOT_SET_TEXT: (FIX_CHOOSE_CORE, CHOOSE_CORE_BUTTON),
    TUNNEL_OFF_TEXT: (FIX_TURN_ON_TUNNEL, TURN_ON_TUNNEL_BUTTON),
}
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


class ScopeControls(QWidget):
    """The Start, Stop and Restart buttons for one scope, with the gate note and the last result. It runs one call at a time.

    ``actionFinished`` is emitted on the GUI thread after each finished call, a start check included. The owner connects
    to it, so the owner can read the state again. A signal, not a callback, keeps this widget from holding its owner in
    memory. RunnerActions.runnerStateChanged is emitted at the same moments, for views that do not hold this widget.
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
        # The button that turns the controls on, when the gate is off: FIX_CHOOSE_CORE, FIX_TURN_ON_TUNNEL, or None.
        self._fix_kind: str | None = None
        # The call in flight: (python, working folder, "start" when a restart still has its start to do, or None, and the
        # step's number from RunnerActions.begin_action).
        self._pending: tuple[str, str, str | None, int] | None = None
        # Identifies this widget's calls. A plain object, so a job can hold it without holding the widget.
        self._owner = object()
        self._generation = 0
        shared_delivery().delivered.connect(self._on_delivered)
        # The status checks after a confirmed start: their own owner token and generation, the Python and folder the start
        # used, the start's step number, its success line, and one single-shot timer per check still to run.
        self._check_owner = object()
        self._check_generation = 0
        self._check_with: tuple[str, str] | None = None
        self._check_serial = 0
        self._started_text = ""
        self._check_timers: list[QTimer] = []
        shared_delivery().delivered.connect(self._on_check_delivered)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self._start = QPushButton(BUTTON_TEXT[START].format(label=self._label), self)
        self._start.setObjectName(f"{prefix}-start")
        self._stop = QPushButton(BUTTON_TEXT[STOP].format(label=self._label), self)
        self._stop.setObjectName(f"{prefix}-stop")
        self._restart = QPushButton(BUTTON_TEXT[RESTART].format(label=self._label), self)
        self._restart.setObjectName(f"{prefix}-restart")
        # The one-step fix. It is shown only while the gate is off, next to the greyed controls, and says what it does.
        self._fix = QPushButton(self)
        self._fix.setObjectName(f"{prefix}-fix")
        self._fix.setVisible(False)
        for button in (self._start, self._stop, self._restart, self._fix):
            row.addWidget(button)
        row.addStretch(1)
        layout.addLayout(row)
        # The gate note and the last result are one block under the buttons, so one drag over them selects both.
        self._text_block = TextBlock(self, f"{prefix}-control-text", spacing=layout.spacing())
        self._note = self._text_block.line(f"{prefix}-control-note", "")
        self._result = self._text_block.line(f"{prefix}-control-result", "")
        self._result.setVisible(False)
        layout.addWidget(self._text_block.label)

        self._start.clicked.connect(self._on_start_clicked)
        self._stop.clicked.connect(self._on_stop_clicked)
        self._restart.clicked.connect(self._on_restart_clicked)
        self._fix.clicked.connect(self._on_fix_clicked)
        # The core search and a chosen core folder change the gate, so the buttons read the setup again then.
        actions.coreChanged.connect(self.refresh_enabled)
        self.refresh_enabled()

    # ----------------------------------------------------------- state

    def refresh_enabled(self) -> None:
        """Read the saved setup again and set the buttons, the gate note and the fix button. Starts nothing."""
        enabled, note = self._gate(self._actions.core())
        self._enabled_by_gate = enabled
        self._note.setText(note)
        self._note.setVisible(bool(note))
        fix = FIX_BY_NOTE.get(note) if not enabled else None
        self._fix_kind = fix[0] if fix else None
        self._fix.setText(fix[1] if fix else "")
        self._set_buttons()

    def _set_buttons(self) -> None:
        on = self._enabled_by_gate and not self._busy
        for button in (self._start, self._stop, self._restart):
            button.setEnabled(on)
        self._fix.setVisible(self._fix_kind is not None)
        self._fix.setEnabled(self._fix_kind is not None and not self._busy)

    def _show(self, text: str) -> None:
        self._result.setText(text)
        self._result.setVisible(bool(text))

    def _finish(self) -> None:
        self._set_buttons()
        self._changed()

    def _changed(self) -> None:
        """Tell the owner and every other view that a call finished, so each one reads the state again."""
        self.actionFinished.emit()
        self._actions.notify_runner_state_changed()

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

    @Slot()
    def _on_fix_clicked(self) -> None:
        """Do the one thing the gate note names: choose the core folder, or turn the tunnel controls on. Starts nothing."""
        if self._busy or self._fix_kind is None:
            return
        if self._fix_kind == FIX_CHOOSE_CORE:
            message = self._actions.choose_core_folder(self)
        else:
            message = self._actions.enable_tunnel_controls()
        self._show(message)
        self.refresh_enabled()

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
        # A new step makes any check of an earlier start meaningless, here and in every other control of this scope.
        self._cancel_start_checks()
        serial = self._actions.begin_action(self._scope)
        self._busy = True
        self._pending = (python, cwd, then, serial)
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
        python, cwd, then, serial = self._pending
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
        if result.action == START and result.confirmed:
            self._schedule_start_checks(python, cwd, serial, result.message)
        self._finish()

    # ----------------------------------------------------------- the checks after a confirmed start

    def _schedule_start_checks(self, python: str, cwd: str, serial: int, started_text: str) -> None:
        """Read the status again at each of START_CHECK_DELAYS_SECONDS after the start. Each check only reads."""
        self._cancel_start_checks()
        self._check_with = (python, cwd)
        self._check_serial = serial
        self._started_text = started_text
        for seconds in START_CHECK_DELAYS_SECONDS:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(int(seconds * 1000))
            timer.timeout.connect(self._run_start_check)
            timer.start()
            self._check_timers.append(timer)

    def _cancel_start_checks(self) -> None:
        """Stop every check still to run, and drop the answer of one still running."""
        for timer in self._check_timers:
            timer.stop()
            timer.deleteLater()
        self._check_timers = []
        self._check_with = None
        self._check_generation += 1

    def _check_superseded(self) -> bool:
        """True when a newer Start or Stop on this scope has begun since the start being checked, from any control."""
        return self._actions.action_serial(self._scope) != self._check_serial

    @Slot()
    def _run_start_check(self) -> None:
        if self._check_with is None or self._busy:
            return
        if self._check_superseded():
            self._cancel_start_checks()
            return
        python, cwd = self._check_with
        self._check_generation += 1
        generation = self._check_generation
        control = self._actions.control(python, cwd)
        scope = self._scope
        submit(self._check_owner, generation, lambda: control.check_start(scope))

    @Slot(object)
    def _on_check_delivered(self, item: object) -> None:
        # Runs on the GUI thread. A check for another widget, a superseded check, or one a newer step cancelled is dropped.
        if not isinstance(item, Delivered) or item.owner is not self._check_owner:
            return
        generation, (result, failure) = item.value
        if generation != self._check_generation or self._check_with is None or self._busy:
            return
        if self._check_superseded():
            self._cancel_start_checks()
            return
        readable = failure is None and isinstance(result, ControlResult)
        if readable and result.outcome == STOPPED_AFTER_START:
            # The child is gone, so a later check has nothing to add.
            self._show(STOPPED_AFTER_START_MESSAGE)
            self._cancel_start_checks()
        elif readable and result.confirmed and result.state == RUNNING:
            self._show(self._started_text)
        else:
            self._show(START_CHECK_UNKNOWN_MESSAGE)
        if not any(timer.isActive() for timer in self._check_timers):
            self._cancel_start_checks()
        self._changed()


__all__ = [
    "CORE_NOT_SET_TEXT",
    "RESTART",
    "TUNNEL_OFF_TEXT",
    "ScopeControls",
    "local_gate",
    "tunnel_gate",
]
