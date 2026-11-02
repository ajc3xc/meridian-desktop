"""A confirmed start is checked again a few seconds later, so a child that dies at once is not left shown as started.

The runner's start prints a status taken before its child can exit, so a start can be "confirmed" while the child is
already dead. After a confirmed start the controls read the status again (runner_controls.START_CHECK_DELAYS_SECONDS,
about 3 and 20 seconds). A child that is not running then replaces the success line with a plain sentence, and the
check is logged.

Every runner call is a fake spawn. No process starts, no socket opens, and every log goes to pytest's temp directory.
The delays are shortened here, so no test waits 20 seconds.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import threading

import pytest
from PySide6.QtWidgets import QLabel, QPushButton

from meridian_desktop import runner_controls as rc
from meridian_desktop.runner_controls import (
    CONFIRMED,
    NOT_CONFIRMED,
    REFUSED,
    START_CHECK,
    STOPPED_AFTER_START,
    STOPPED_AFTER_START_MESSAGE,
    RunnerControl,
    build_argv,
)
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.tunnel_status import NOT_RUNNING, RUNNING, UNKNOWN
from meridian_desktop.ui import scope_controls
from meridian_desktop.ui.runner_actions import RunnerActions
from meridian_desktop.ui.scope_controls import ScopeControls, local_gate, tunnel_gate

PY = "C:/Users/tester/core-env/python.exe"
CWD = "C:/Users/tester/Documents/Meridian/repository"
STAMP = dt.datetime(2026, 10, 23, 9, 0, 0, tzinfo=dt.timezone.utc)
# Short stand-ins for the 3 and 20 second checks.
FAST_DELAYS = (0.05, 0.3)


def _done(code, payload=None, *, stderr=b""):
    body = json.dumps(payload).encode("utf-8") if payload is not None else b""
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=body, stderr=stderr)


def _status(scope, state, pid=808):
    return {"scope": scope, "generated_at": 1.0, "child": {"state": state, "pid": pid}}


class Spawn:
    """A fake for subprocess.run. It answers by subcommand: the start replies in order, and each status in order."""

    def __init__(self, *, starts=(), statuses=(), stops=()):
        self._replies = {"start": list(starts), "status": list(statuses), "stop": list(stops)}
        self._lock = threading.Lock()
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        with self._lock:
            self.calls.append(list(argv))
            verb, scope = argv[3], argv[5]
            queue = self._replies[verb]
            reply = queue.pop(0) if queue else _done(0, _status(scope, "running"))
        if isinstance(reply, BaseException):
            raise reply
        return reply

    @property
    def verbs(self) -> list[str]:
        with self._lock:
            return [argv[3] for argv in self.calls]


class Confirm:
    def __init__(self, *answers):
        self._answers = list(answers)
        self.asked: list[tuple[str, str, str]] = []

    def __call__(self, parent, title, body, confirm_text):
        self.asked.append((title, body, confirm_text))
        return self._answers.pop(0) if self._answers else False


def _control(tmp_path, spawn):
    log = tmp_path / "settings" / rc.LOG_NAME
    return RunnerControl(PY, CWD, spawn=spawn, log_path=log, clock=lambda: STAMP), log


def _settings(settings_path):
    SettingsStore(settings_path).save(Settings(core_python=PY, core_checkout=CWD, tunnel_control_enabled=True))


def _actions(settings_path, spawn, confirm, log_path):
    return RunnerActions(
        SettingsStore(settings_path),
        make_control=lambda python, cwd: RunnerControl(python, cwd, spawn=spawn, log_path=log_path, clock=lambda: STAMP),
        confirm=confirm,
    )


def _widget(qtbot, actions, *, scope="tunnel", prefix="tunnel", gate=tunnel_gate):
    widget = ScopeControls(scope, actions, gate, prefix=prefix)
    qtbot.addWidget(widget)
    return widget


def _result(widget, prefix="tunnel") -> str:
    return widget.findChild(QLabel, f"{prefix}-control-result").text()


def _button(widget, name) -> QPushButton:
    return widget.findChild(QPushButton, name)


@pytest.fixture
def fast_checks(monkeypatch):
    monkeypatch.setattr(scope_controls, "START_CHECK_DELAYS_SECONDS", FAST_DELAYS)


# ------------------------------------------------------------------ the pure check (runner_controls)


def test_the_checks_after_a_start_are_at_about_3_and_20_seconds():
    assert rc.START_CHECK_DELAYS_SECONDS == (3.0, 20.0)
    assert STOPPED_AFTER_START_MESSAGE == "Started, but it stopped straight after. Check the control log."


@pytest.mark.parametrize("state", ["stopped", "crashed", "not_started"])
def test_a_start_check_that_finds_the_child_gone_says_so_and_logs_it(tmp_path, state):
    spawn = Spawn(statuses=[_done(0, _status("tunnel", state, pid=41))])
    control, log = _control(tmp_path, spawn)

    result = control.check_start("tunnel")

    assert result.outcome == STOPPED_AFTER_START
    assert result.state == NOT_RUNNING
    assert result.message == STOPPED_AFTER_START_MESSAGE
    assert not result.confirmed
    # Only a read: the one call is the allowlisted status command.
    assert spawn.calls == [build_argv(PY, "status", "tunnel")]
    assert log.read_text(encoding="utf-8").splitlines() == [
        f"{STAMP.isoformat(timespec='seconds')} {START_CHECK} scope=tunnel outcome={STOPPED_AFTER_START} pid=41",
    ]


def test_a_start_check_that_finds_the_child_running_is_confirmed(tmp_path):
    spawn = Spawn(statuses=[_done(0, _status("local", "running", pid=7))])
    control, log = _control(tmp_path, spawn)

    result = control.check_start("local")

    assert result.outcome == CONFIRMED
    assert result.state == RUNNING
    assert result.pid == 7
    assert "outcome=confirmed pid=7" in log.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "reply",
    [
        _done(0, _status("tunnel", "unknown")),
        _done(0, _status("local", "running")),  # a reply for another scope
        _done(2),
        subprocess.TimeoutExpired(cmd="x", timeout=10),
        OSError("no such file"),
    ],
)
def test_a_start_check_that_cannot_read_the_state_is_never_shown_as_running(tmp_path, reply):
    control, log = _control(tmp_path, Spawn(statuses=[reply]))

    result = control.check_start("tunnel")

    assert result.outcome == NOT_CONFIRMED
    assert result.state == UNKNOWN
    assert "running" not in result.message.replace("not shown as running", "")
    assert f"{START_CHECK} scope=tunnel outcome=not_confirmed" in log.read_text(encoding="utf-8")


def test_a_start_check_for_a_scope_outside_the_allowlist_runs_and_logs_nothing(tmp_path):
    spawn = Spawn()
    control, log = _control(tmp_path, spawn)

    result = control.check_start("all")

    assert result.outcome == REFUSED
    assert spawn.calls == []
    assert not log.exists()


# ------------------------------------------------------------------ the controls (ui/scope_controls.py)


def test_a_start_whose_child_dies_straight_after_replaces_the_success_line(qtbot, settings_path, tmp_path, fast_checks):
    """The reported bug: the runner said running, so the desktop said started, while the child had already exited."""
    _settings(settings_path)
    log = tmp_path / "log.txt"
    spawn = Spawn(
        starts=[_done(0, _status("tunnel", "running", pid=909))],
        statuses=[_done(0, _status("tunnel", "crashed", pid=909))],
    )
    widget = _widget(qtbot, _actions(settings_path, spawn, Confirm(True), log))

    _button(widget, "tunnel-start").click()
    qtbot.waitUntil(lambda: "pid 909" in _result(widget), timeout=5000)
    qtbot.waitUntil(lambda: _result(widget) == STOPPED_AFTER_START_MESSAGE, timeout=5000)

    # The second check is not run once the child is known to be gone.
    qtbot.wait(int(FAST_DELAYS[1] * 1000) + 200)
    assert spawn.verbs == ["start", "status"]
    assert _result(widget) == STOPPED_AFTER_START_MESSAGE
    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines[0].endswith("start scope=tunnel outcome=confirmed pid=909")
    assert lines[1].endswith(f"{START_CHECK} scope=tunnel outcome={STOPPED_AFTER_START} pid=909")


def test_a_start_that_stays_running_keeps_its_line_and_is_checked_twice(qtbot, settings_path, tmp_path, fast_checks):
    _settings(settings_path)
    spawn = Spawn(
        starts=[_done(0, _status("tunnel", "running", pid=5))],
        statuses=[_done(0, _status("tunnel", "running", pid=5)), _done(0, _status("tunnel", "running", pid=5))],
    )
    widget = _widget(qtbot, _actions(settings_path, spawn, Confirm(True), tmp_path / "log.txt"))

    _button(widget, "tunnel-start").click()
    qtbot.waitUntil(lambda: spawn.verbs == ["start", "status", "status"], timeout=5000)
    qtbot.wait(100)

    assert "started" in _result(widget) and "pid 5" in _result(widget)


def test_a_child_that_dies_before_the_second_check_is_caught_then(qtbot, settings_path, tmp_path, fast_checks):
    _settings(settings_path)
    spawn = Spawn(
        starts=[_done(0, _status("local", "running", pid=6))],
        statuses=[_done(0, _status("local", "running", pid=6)), _done(0, _status("local", "stopped", pid=6))],
    )
    widget = _widget(qtbot, _actions(settings_path, spawn, Confirm(True), tmp_path / "log.txt"),
                     scope="local", prefix="local", gate=local_gate)

    _button(widget, "local-start").click()
    qtbot.waitUntil(lambda: _result(widget, "local") == STOPPED_AFTER_START_MESSAGE, timeout=5000)
    assert spawn.verbs == ["start", "status", "status"]


def test_an_unreadable_check_takes_the_success_line_down_and_a_later_running_check_puts_it_back(
    qtbot, settings_path, tmp_path, monkeypatch,
):
    monkeypatch.setattr(scope_controls, "START_CHECK_DELAYS_SECONDS", (0.05, 0.8))
    _settings(settings_path)
    spawn = Spawn(
        starts=[_done(0, _status("tunnel", "running", pid=8))],
        statuses=[_done(2), _done(0, _status("tunnel", "running", pid=8))],
    )
    widget = _widget(qtbot, _actions(settings_path, spawn, Confirm(True), tmp_path / "log.txt"))

    _button(widget, "tunnel-start").click()
    qtbot.waitUntil(lambda: _result(widget) == rc.START_CHECK_UNKNOWN_MESSAGE, timeout=5000)
    assert spawn.verbs == ["start", "status"]
    assert "pid 8" not in _result(widget)
    qtbot.waitUntil(lambda: "pid 8" in _result(widget), timeout=5000)
    assert spawn.verbs == ["start", "status", "status"]


def test_a_cancelled_start_schedules_no_check(qtbot, settings_path, tmp_path, fast_checks):
    _settings(settings_path)
    spawn = Spawn()
    widget = _widget(qtbot, _actions(settings_path, spawn, Confirm(False), tmp_path / "log.txt"))

    _button(widget, "tunnel-start").click()
    qtbot.wait(int(FAST_DELAYS[1] * 1000) + 200)

    assert spawn.calls == []
    assert _result(widget) == "Start cancelled. Nothing was started."


def test_a_start_that_is_not_confirmed_schedules_no_check(qtbot, settings_path, tmp_path, fast_checks):
    _settings(settings_path)
    spawn = Spawn(starts=[_done(0, _status("tunnel", "stopped"))])
    widget = _widget(qtbot, _actions(settings_path, spawn, Confirm(True), tmp_path / "log.txt"))

    _button(widget, "tunnel-start").click()
    qtbot.waitUntil(lambda: "did not stay running" in _result(widget), timeout=5000)
    qtbot.wait(int(FAST_DELAYS[1] * 1000) + 200)

    assert spawn.verbs == ["start"]


def test_a_stop_pressed_before_the_checks_cancels_them(qtbot, settings_path, tmp_path, monkeypatch):
    monkeypatch.setattr(scope_controls, "START_CHECK_DELAYS_SECONDS", (0.4, 0.6))
    _settings(settings_path)
    spawn = Spawn(
        starts=[_done(0, _status("tunnel", "running", pid=3))],
        stops=[_done(0, _status("tunnel", "stopped", pid=3))],
        statuses=[_done(0, _status("tunnel", "stopped", pid=3))] * 2,
    )
    widget = _widget(qtbot, _actions(settings_path, spawn, Confirm(True, True), tmp_path / "log.txt"))

    _button(widget, "tunnel-start").click()
    qtbot.waitUntil(lambda: "pid 3" in _result(widget), timeout=5000)
    _button(widget, "tunnel-stop").click()
    qtbot.waitUntil(lambda: _result(widget) == "The tunnel is stopped.", timeout=5000)
    qtbot.wait(900)

    assert spawn.verbs == ["start", "stop"]
    assert _result(widget) == "The tunnel is stopped."


def test_a_stop_from_another_control_of_the_same_scope_cancels_the_checks(qtbot, settings_path, tmp_path, monkeypatch):
    """Home and Settings each have local controls. A stop from one is not reported as a crash by the other's check."""
    monkeypatch.setattr(scope_controls, "START_CHECK_DELAYS_SECONDS", (0.4, 0.6))
    _settings(settings_path)
    spawn = Spawn(
        starts=[_done(0, _status("local", "running", pid=4))],
        stops=[_done(0, _status("local", "stopped", pid=4))],
        statuses=[_done(0, _status("local", "stopped", pid=4))] * 2,
    )
    actions = _actions(settings_path, spawn, Confirm(True, True), tmp_path / "log.txt")
    home = _widget(qtbot, actions, scope="local", prefix="home-local", gate=local_gate)
    settings = _widget(qtbot, actions, scope="local", prefix="local", gate=local_gate)

    _button(home, "home-local-start").click()
    qtbot.waitUntil(lambda: "pid 4" in _result(home, "home-local"), timeout=5000)
    _button(settings, "local-stop").click()
    qtbot.waitUntil(lambda: _result(settings, "local") == "The local server is stopped.", timeout=5000)
    qtbot.wait(900)

    assert spawn.verbs == ["start", "stop"]
    assert _result(home, "home-local") != STOPPED_AFTER_START_MESSAGE


def test_the_start_of_a_restart_is_checked_too(qtbot, settings_path, tmp_path, fast_checks):
    _settings(settings_path)
    spawn = Spawn(
        stops=[_done(0, _status("tunnel", "stopped", pid=1))],
        starts=[_done(0, _status("tunnel", "running", pid=2))],
        statuses=[_done(0, _status("tunnel", "crashed", pid=2))],
    )
    confirm = Confirm(True, True)
    widget = _widget(qtbot, _actions(settings_path, spawn, confirm, tmp_path / "log.txt"))

    _button(widget, "tunnel-restart").click()
    qtbot.waitUntil(lambda: _result(widget) == STOPPED_AFTER_START_MESSAGE, timeout=5000)
    assert spawn.verbs == ["stop", "start", "status"]
    # Both steps still asked first.
    assert [title for title, _body, _text in confirm.asked] == [
        "Restart the tunnel: stop it first?",
        "Restart the tunnel: start it now?",
    ]


def test_each_finished_action_and_each_check_tells_the_other_views(qtbot, settings_path, tmp_path, fast_checks):
    """RunnerActions.runnerStateChanged fires once for the start and once for each check, so Home can read again."""
    _settings(settings_path)
    spawn = Spawn(
        starts=[_done(0, _status("tunnel", "running", pid=9))],
        statuses=[_done(0, _status("tunnel", "running", pid=9)), _done(0, _status("tunnel", "running", pid=9))],
    )
    actions = _actions(settings_path, spawn, Confirm(True), tmp_path / "log.txt")
    changes, finished = [], []
    actions.runnerStateChanged.connect(lambda: changes.append(True))
    widget = _widget(qtbot, actions)
    widget.actionFinished.connect(lambda: finished.append(True))

    _button(widget, "tunnel-start").click()
    qtbot.waitUntil(lambda: len(changes) == 3, timeout=5000)
    qtbot.wait(100)

    assert len(changes) == 3
    assert len(finished) == 3


def test_a_cancelled_action_tells_no_one(qtbot, settings_path, tmp_path):
    _settings(settings_path)
    actions = _actions(settings_path, Spawn(), Confirm(False), tmp_path / "log.txt")
    changes = []
    actions.runnerStateChanged.connect(lambda: changes.append(True))
    widget = _widget(qtbot, actions)

    _button(widget, "tunnel-stop").click()
    qtbot.wait(50)

    assert changes == []
