"""The Qt side of the owner-confirmed runner controls: the confirmation dialog, the off-thread job, the launch-time starts.

This is Qt code, so it sits in ui/. Its pure parts are in meridian_desktop/runner_controls.py and
meridian_desktop/launch_starts.py, and the contract is docs/local-trust-contract.md.

- RunnerActions holds every outside effect the controls use: the saved core setup, the RunnerControl factory, the
  confirmation dialog, and the Zotero and local-URL calls. The app builds it from the settings file. Tests pass fakes.
- ask_control builds the confirmation dialog. Its default button is Cancel, Escape is Cancel, and it names the exact
  command, the scope and the working folder. Only the confirm button returns True (rule 2).
- submit runs one call on the thread pool. Its result comes back through shared_delivery() with the owner token and
  a generation number. The job never holds a widget, so a widget that is deleted while a call runs is never touched.
- LaunchStarts runs the two launch-time starts (amendments A and B) once, after the window shows, through the same
  delivery path. It starts nothing when both settings are off.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Slot
from PySide6.QtWidgets import QLabel, QMessageBox, QPushButton, QWidget

from meridian_desktop import local_client, zotero_client
from meridian_desktop.launch_starts import LaunchDeps, LaunchNote, run_launch_starts
from meridian_desktop.runner_controls import SCOPE_LOCAL, RunnerControl, default_log_path
from meridian_desktop.settings_store import CONTROL_FLAG_KEYS, SettingsStore
from meridian_desktop.ui.delivery import Delivered, shared_delivery
from meridian_desktop.ui.text import make_selectable

LAUNCH_FAILED_TEXT = "A launch-time start could not be run ({})."
# The one generation a launch run uses. It runs once, so it never needs to be superseded.
_LAUNCH_GENERATION = 1


@dataclass(frozen=True)
class CoreSetup:
    """The settings the controls read: the core checkout and its Python, the local URL, and the three switches."""

    python: str
    cwd: str
    local_url: str
    tunnel_control_enabled: bool
    zotero_autostart: bool
    local_server_autostart: bool

    @property
    def core_ready(self) -> bool:
        return bool(self.python.strip() and self.cwd.strip())


ConfirmFn = Callable[[QWidget | None, str, str, str], bool]


def control_box(parent: QWidget | None, title: str, body: str, confirm_text: str) -> tuple[QMessageBox, QPushButton]:
    """The confirmation dialog, built but not shown. Its default and Escape button is Cancel, so Enter never confirms.

    The title and the body are plain text, so a command or path is never read as HTML.
    """
    box = QMessageBox(parent)
    box.setWindowTitle("Meridian Desktop")
    box.setIcon(QMessageBox.Icon.Question)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setText(title)
    box.setInformativeText(body)
    confirm = box.addButton(confirm_text, QMessageBox.ButtonRole.AcceptRole)
    cancel = box.addButton(QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(cancel)
    box.setEscapeButton(cancel)
    for child in box.findChildren(QLabel):
        child.setTextFormat(Qt.TextFormat.PlainText)
        make_selectable(child)
    return box, confirm


def ask_control(parent: QWidget | None, title: str, body: str, confirm_text: str) -> bool:
    """Show the confirmation. True only when the confirm button is chosen. Cancel, Escape and closing are all no."""
    box, confirm = control_box(parent, title, body, confirm_text)
    box.exec()
    return box.clickedButton() is confirm


class RunnerActions:
    """The desktop's runner controls: the saved setup, the control factory, the dialog and the launch-time calls.

    Every outside effect is a constructor argument, so a test passes fakes and never starts a process, opens a socket
    or reads the real settings file. The defaults are the real calls, and only the app uses them.
    """

    def __init__(
        self,
        store: SettingsStore,
        *,
        make_control: Callable[[str, str], RunnerControl] | None = None,
        confirm: ConfirmFn | None = None,
        find_zotero: Callable[[], str | None] | None = None,
        probe_zotero: Callable[[], Any] | None = None,
        start_zotero: Callable[..., str] | None = None,
        probe_local: Callable[[str], str] | None = None,
        log_path: Path | None = None,
    ) -> None:
        self._store = store
        self._log_path = log_path
        self._make_control = make_control if make_control is not None else self._default_control
        self._confirm = confirm if confirm is not None else ask_control
        self._find_zotero = find_zotero if find_zotero is not None else zotero_client.find_zotero_exe
        self._probe_zotero = probe_zotero if probe_zotero is not None else zotero_client.probe_api
        self._start_zotero = start_zotero if start_zotero is not None else zotero_client.start_zotero
        self._probe_local = probe_local if probe_local is not None else local_client.probe_health

    def _log(self) -> Path:
        return self._log_path if self._log_path is not None else default_log_path()

    def _default_control(self, python: str, cwd: str) -> RunnerControl:
        return RunnerControl(python, cwd, log_path=self._log())

    def core(self) -> CoreSetup:
        """The settings as they are saved now. Read on the calling thread, and cheap (one small file)."""
        settings = self._store.load().settings
        return CoreSetup(
            python=settings.core_python,
            cwd=settings.core_checkout,
            local_url=settings.local_url,
            tunnel_control_enabled=settings.tunnel_control_enabled,
            zotero_autostart=settings.zotero_autostart,
            local_server_autostart=settings.local_server_autostart,
        )

    def control(self, python: str, cwd: str) -> RunnerControl:
        return self._make_control(python, cwd)

    def ask(self, parent: QWidget | None, title: str, body: str, confirm_text: str) -> bool:
        return self._confirm(parent, title, body, confirm_text)

    def set_flag(self, name: str, value: bool) -> None:
        """Save one runner switch. Every other field in the saved file is kept. Raises OSError or ValueError when it cannot."""
        if name not in CONTROL_FLAG_KEYS:
            raise ValueError(f"unknown runner switch: {name}")
        self._store.save(replace(self._store.load().settings, **{name: value}))

    def find_zotero(self) -> str | None:
        return self._find_zotero()

    def launch_deps(self, core: CoreSetup) -> LaunchDeps:
        """The calls the launch-time starts make, bound to the setup read at launch. Each runs on the worker."""
        control = self.control(core.python, core.cwd)
        log = self._log()
        probe_zotero = self._probe_zotero
        find_zotero = self._find_zotero
        start_zotero = self._start_zotero
        probe_local = self._probe_local
        local_url = core.local_url
        return LaunchDeps(
            zotero_probe=probe_zotero,
            zotero_exe=find_zotero,
            zotero_start=lambda exe: start_zotero(exe, log_path=log),
            local_health=lambda: probe_local(local_url),
            local_status=lambda: control.status(SCOPE_LOCAL),
            local_start=lambda: control.start(SCOPE_LOCAL),
            core_ready=core.core_ready,
        )


class ActionJob(QRunnable):
    """Run ``work`` on the thread pool and deliver (value, failure_class_name).

    A raised exception is reduced to its class name, so no traceback or message reaches the UI.
    """

    def __init__(self, work: Callable[[], Any], deliver: Callable[[tuple], None]) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._work = work
        self._deliver = deliver

    def run(self) -> None:
        value: Any = None
        failure: str | None = None
        try:
            value = self._work()
        except Exception as exc:  # noqa: BLE001 - a worker must never raise into the pool
            failure = exc.__class__.__name__
        try:
            self._deliver((value, failure))
        except RuntimeError:
            pass  # the shared delivery object is gone, for example at shutdown


def submit(owner: object, generation: int, work: Callable[[], Any]) -> None:
    """Run ``work`` on the thread pool. Its result is delivered as Delivered(owner, (generation, (value, failure))).

    The deliver closure captures only the owner, the generation and the shared delivery object, never a widget.
    """
    delivery = shared_delivery()

    def deliver(payload: tuple) -> None:
        delivery.delivered.emit(Delivered(owner, (generation, payload)))

    QThreadPool.globalInstance().start(ActionJob(work, deliver))


class LaunchStarts(QObject):
    """Run the launch-time starts once, on the thread pool, and report what they did through ``report``.

    Call start() after the window shows. It does nothing when neither switch is on, and it never runs twice.
    """

    def __init__(
        self,
        actions: RunnerActions,
        *,
        report: Callable[[str], None] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._actions = actions
        self._report = report
        # Identifies this object's launch results. A plain object, so a job can hold it without holding the object.
        self._owner = object()
        self._started = False
        shared_delivery().delivered.connect(self._on_delivered)

    @Slot()
    def start(self) -> None:
        if self._started:
            return
        self._started = True
        core = self._actions.core()
        zotero_on, local_on = core.zotero_autostart, core.local_server_autostart
        if not (zotero_on or local_on):
            return
        deps = self._actions.launch_deps(core)
        submit(self._owner, _LAUNCH_GENERATION, lambda: run_launch_starts(zotero_on, local_on, deps))

    @Slot(object)
    def _on_delivered(self, item: object) -> None:
        # Runs on the GUI thread. A result for another object is dropped here.
        if not isinstance(item, Delivered) or item.owner is not self._owner:
            return
        _generation, (notes, failure) = item.value
        if failure is not None:
            text = LAUNCH_FAILED_TEXT.format(failure)
        else:
            text = "\n".join(note.message for note in notes if isinstance(note, LaunchNote))
        if text and self._report is not None:
            self._report(text)


__all__ = [
    "ActionJob",
    "ConfirmFn",
    "CoreSetup",
    "LaunchStarts",
    "RunnerActions",
    "ask_control",
    "control_box",
    "submit",
]
