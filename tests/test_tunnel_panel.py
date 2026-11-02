"""Tests for the Tunnel panel: meridian_desktop/tunnel_panel_model.py (the words and rows) and ui/tunnel_panel.py.

Every outside effect is a fake. The status call is a fake runner reply, the Start and Stop runner is a fake spawn, and
the confirmation dialog is a fake that answers in order. The settings file is a temp file. No process starts, no
socket opens, and no dialog opens. The widget tests run under pytest-qt on the offscreen platform.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess

import pytest
from PySide6.QtWidgets import QCheckBox, QLabel, QPushButton, QWidget

from meridian_desktop import tool_catalog
from meridian_desktop import tunnel_panel_model as model
from meridian_desktop.runner_client import RunnerResult
from meridian_desktop.runner_controls import RunnerControl
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.tunnel_status import NOT_RUNNING, RUNNING, UNKNOWN, TunnelStatus
from meridian_desktop.ui.runner_actions import RunnerActions
from meridian_desktop.ui.scope_controls import TUNNEL_OFF_TEXT
from meridian_desktop.ui.tunnel_panel import PROBLEM_TEXT, TunnelPanel

PY = "C:/Users/tester/core-env/python.exe"
CWD = "C:/Users/tester/Documents/Meridian/repository"
STAMP = dt.datetime(2026, 10, 22, 17, 0, 0, tzinfo=dt.timezone.utc)
STAMP_TEXT = "2026-10-22 17:00:00"
CONNECTOR = {
    "name": "notes", "kind": "copy", "transport": "stdio", "enabled": True, "tunnel_exposed": False,
    "command": "notes-cli", "args": ["--serve"],
}


# ------------------------------------------------------------------ fakes


class Spawn:
    """A fake for the runner's spawn. It records each call and answers with a benign status reply."""

    def __init__(self):
        self.calls: list[list] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        body = json.dumps({"scope": "tunnel", "generated_at": 1.0, "child": {"state": "stopped", "pid": None}})
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=body.encode("utf-8"), stderr=b"")


class Confirm:
    """A fake for the confirmation dialog. It answers in order and records each question."""

    def __init__(self, *answers):
        self._answers = list(answers)
        self.asked: list[tuple[str, str, str]] = []

    def __call__(self, parent, title, body, confirm_text):
        self.asked.append((title, body, confirm_text))
        return self._answers.pop(0) if self._answers else False


def runner_reply(state: str):
    """A status call that answers with the given child state for the tunnel scope, as the runner does."""

    def call(scope: str) -> RunnerResult:
        payload = {"scope": scope, "generated_at": 1.0, "child": {"state": state, "pid": 808}}
        return RunnerResult(verb="status", ok=True, payload=payload)

    return call


def raising_call(scope: str) -> RunnerResult:
    raise RuntimeError("the runner could not be reached")


def _status(state: str, reason: str = "The runner reports the tunnel process as running.") -> TunnelStatus:
    return TunnelStatus(state=state, reason=reason, checked_at=STAMP)


def _settings(settings_path, **overrides):
    values = dict(core_python=PY, core_checkout=CWD, tunnel_control_enabled=True)
    values.update(overrides)
    SettingsStore(settings_path).save(Settings(**values))


def _actions(settings_path, spawn, confirm, tmp_path):
    return RunnerActions(
        SettingsStore(settings_path),
        make_control=lambda python, cwd: RunnerControl(
            python, cwd, spawn=spawn, log_path=tmp_path / "runner-control.log", clock=lambda: STAMP,
        ),
        confirm=confirm,
    )


def _panel(qtbot, settings_path, tmp_path, status_call=None, spawn=None, confirm=None):
    spawn = spawn if spawn is not None else Spawn()
    confirm = confirm if confirm is not None else Confirm()
    actions = _actions(settings_path, spawn, confirm, tmp_path)
    panel = TunnelPanel(
        SettingsStore(settings_path),
        actions,
        status_call if status_call is not None else runner_reply("running"),
    )
    qtbot.addWidget(panel)
    return panel, spawn, confirm


def _label(panel, name) -> QLabel:
    return panel.findChild(QLabel, name)


def _box(panel, name) -> QCheckBox:
    return panel.findChild(QCheckBox, name)


def _button(panel, name) -> QPushButton:
    return panel.findChild(QPushButton, name)


def _check_and_wait(qtbot, panel, until_text: str, name: str = "tunnelpanel-headline") -> None:
    panel.check()
    qtbot.waitUntil(lambda: _label(panel, name).text() == until_text, timeout=5000)


# ------------------------------------------------------------------ the status words (no Qt)


def test_a_running_tunnel_reads_connected_with_its_check_time():
    shown = model.panel_status(_status(RUNNING))
    assert shown.headline == "Connected"
    assert shown.checked == f"Checked at {STAMP_TEXT}."
    assert shown.detail == "The runner reports the tunnel process as running."


def test_a_stopped_tunnel_reads_not_connected_and_says_how_to_start_it():
    shown = model.panel_status(_status(NOT_RUNNING, "The runner reports the tunnel process as stopped."))
    assert shown.headline == "Not connected"
    assert shown.checked == f"Checked at {STAMP_TEXT}."
    assert shown.detail.startswith("The runner reports the tunnel process as stopped.")
    assert model.PANEL_START_HINT in shown.detail


def test_an_unknown_or_failed_check_never_reads_as_connected():
    unknown = model.panel_status(_status(UNKNOWN, "The runner could not verify whether the tunnel process is running."))
    assert unknown.headline == model.UNKNOWN_HEADLINE
    assert unknown.headline != "Connected"

    failed = model.panel_status(None, "RuntimeError")
    assert failed.headline == model.UNKNOWN_HEADLINE
    assert failed.checked == "The check did not finish, so no time was recorded."
    assert "RuntimeError" in failed.detail
    assert failed.headline != model.CONNECTED_HEADLINE


def test_the_tunnel_lane_is_live_only_while_the_tunnel_runs():
    _direct, tunnel = model.lanes(RUNNING)
    assert tunnel.live == model.TUNNEL_LANE_LIVE
    _direct, tunnel = model.lanes(NOT_RUNNING)
    assert tunnel.live == model.TUNNEL_LANE_NOT_LIVE
    _direct, tunnel = model.lanes(UNKNOWN)
    assert tunnel.live == model.TUNNEL_LANE_UNKNOWN
    _direct, tunnel = model.lanes(None)
    assert tunnel.live == model.TUNNEL_LANE_UNKNOWN


def test_the_direct_lane_is_never_shown_as_live_by_this_panel():
    for state in (RUNNING, NOT_RUNNING, UNKNOWN, None):
        direct, _tunnel = model.lanes(state)
        assert direct.live == model.DIRECT_LANE_LIVE
        assert "Live" not in direct.live


def test_each_lane_says_what_it_needs():
    direct, tunnel = model.lanes(RUNNING)
    assert direct.name == "Direct lane"
    assert "does not need this desktop or the tunnel" in direct.needs
    assert tunnel.name == "Tunnel lane"
    assert "needs this desktop or the tunnel running" in tunnel.needs


def test_a_group_is_on_only_when_every_tool_in_it_is_on():
    rows = {row.id: row for row in model.group_rows(())}
    assert all(row.on for row in rows.values())
    code = tool_catalog.group_keys("code")
    assert rows["code"].count == f"{len(code)} of {len(code)} on"


def test_a_group_with_some_tools_off_is_unchecked_and_counts_them():
    keys = tool_catalog.group_keys("debug")
    rows = {row.id: row for row in model.group_rows(keys[:2])}
    assert rows["debug"].on is False
    assert rows["debug"].count == f"{len(keys) - 2} of {len(keys)} on"
    assert rows["code"].on is True


def test_the_tunnel_opt_in_is_off_by_default():
    assert model.OPT_IN_DEFAULT is False
    assert Settings().custom_tunnel_opt_in is False


def test_a_connector_says_why_claude_ai_cannot_reach_it_through_the_tunnel():
    assert model.connector_text(True, False, opt_in=False) == model.NOT_REACHABLE_OPT_IN_OFF
    assert model.connector_text(True, False, opt_in=True) == model.NOT_REACHABLE_NOT_MARKED
    assert model.connector_text(True, True, opt_in=False) == model.NOT_REACHABLE_OPT_IN_OFF
    assert model.connector_text(True, True, opt_in=True) == model.REACHABLE_TEXT


def test_an_unusable_connector_is_listed_without_a_tunnel_switch():
    rows = model.connector_rows([CONNECTOR, {"name": "bad name!", "kind": "copy"}], opt_in=False)
    assert [row.name for row in rows] == ["notes", "bad name!"]
    assert rows[0].usable is True
    assert rows[1].usable is False
    assert rows[1].tunnel_exposed is False
    assert rows[1].note


# ------------------------------------------------------------------ the widget


def test_status_shows_connected_when_the_runner_reports_the_tunnel_running(qtbot, settings_path, tmp_path):
    panel, _spawn, _confirm = _panel(qtbot, settings_path, tmp_path, status_call=runner_reply("running"))
    _check_and_wait(qtbot, panel, "Connected")
    assert _label(panel, "tunnelpanel-checked").text().startswith("Checked at ")
    assert "Live." in _label(panel, "tunnelpanel-lane-tunnel").text()


def test_status_shows_not_connected_when_the_runner_reports_the_tunnel_stopped(qtbot, settings_path, tmp_path):
    panel, _spawn, _confirm = _panel(qtbot, settings_path, tmp_path, status_call=runner_reply("stopped"))
    _check_and_wait(qtbot, panel, "Not connected")
    assert model.PANEL_START_HINT in _label(panel, "tunnelpanel-detail").text()
    assert "Not live" in _label(panel, "tunnelpanel-lane-tunnel").text()


def test_a_status_call_that_raises_is_not_shown_as_connected(qtbot, settings_path, tmp_path):
    panel, _spawn, _confirm = _panel(qtbot, settings_path, tmp_path, status_call=raising_call)
    _check_and_wait(qtbot, panel, model.UNKNOWN_HEADLINE)
    assert _label(panel, "tunnelpanel-headline").text() != "Connected"


def test_start_stop_and_restart_are_the_tunnel_controls(qtbot, settings_path, tmp_path):
    panel, _spawn, _confirm = _panel(qtbot, settings_path, tmp_path)
    _settings(settings_path)  # the core is set and the switch is on, so the buttons are on
    panel.refresh()
    for name in ("tunnel-start", "tunnel-stop", "tunnel-restart"):
        assert _button(panel, name).isEnabled() is True, name
        assert _button(panel, name).isHidden() is False, name


def test_the_tunnel_controls_are_the_only_start_and_stop_buttons_in_the_panel(qtbot, settings_path, tmp_path):
    panel, _spawn, _confirm = _panel(qtbot, settings_path, tmp_path)
    controls = [b for b in panel.findChildren(QPushButton) if b.text().split()[:1] in (["Start"], ["Stop"], ["Restart"])]
    assert sorted(b.objectName() for b in controls) == ["tunnel-restart", "tunnel-start", "tunnel-stop"]


def test_start_asks_first_and_runs_nothing_when_the_owner_says_no(qtbot, settings_path, tmp_path):
    spawn, confirm = Spawn(), Confirm(False)
    panel, _spawn, _confirm = _panel(qtbot, settings_path, tmp_path, spawn=spawn, confirm=confirm)
    _settings(settings_path)
    panel.refresh()

    _button(panel, "tunnel-start").click()

    assert len(confirm.asked) == 1
    title, body, confirm_text = confirm.asked[0]
    assert "tunnel" in title.lower()
    assert confirm_text == "Start"
    assert spawn.calls == []


def test_the_start_button_is_off_while_the_tunnel_switch_is_off(qtbot, settings_path, tmp_path):
    _settings(settings_path, tunnel_control_enabled=False)
    panel, spawn, confirm = _panel(qtbot, settings_path, tmp_path)
    assert _button(panel, "tunnel-start").isEnabled() is False
    assert _label(panel, "tunnel-control-note").text() == TUNNEL_OFF_TEXT
    assert confirm.asked == [] and spawn.calls == []


def test_the_group_switch_turns_every_tool_of_the_group_off_and_on(qtbot, settings_path, tmp_path):
    panel, _spawn, _confirm = _panel(qtbot, settings_path, tmp_path)
    code_keys = set(tool_catalog.group_keys("code"))
    box = _box(panel, "tunnelpanel-group-code")
    assert box.isChecked() is True

    box.click()  # off
    assert set(SettingsStore(settings_path).load().settings.disabled_tools) == code_keys
    assert _box(panel, "tunnelpanel-group-code").isChecked() is False
    assert _label(panel, "tunnelpanel-group-code-count").text() == f"0 of {len(code_keys)} on"

    _box(panel, "tunnelpanel-group-code").click()  # on again
    assert SettingsStore(settings_path).load().settings.disabled_tools == ()
    assert _box(panel, "tunnelpanel-group-code").isChecked() is True


def test_a_group_with_one_tool_off_shows_unchecked_with_its_count(qtbot, settings_path, tmp_path):
    keys = tool_catalog.group_keys("latex")
    _settings(settings_path, disabled_tools=(keys[0],))
    panel, _spawn, _confirm = _panel(qtbot, settings_path, tmp_path)
    panel.refresh()
    assert _box(panel, "tunnelpanel-group-latex").isChecked() is False
    assert _label(panel, "tunnelpanel-group-latex-count").text() == f"{len(keys) - 1} of {len(keys)} on"


def test_the_opt_in_is_off_by_default_and_saves_only_when_turned_on(qtbot, settings_path, tmp_path):
    panel, _spawn, _confirm = _panel(qtbot, settings_path, tmp_path)
    assert _box(panel, "tunnelpanel-opt-in").isChecked() is False
    assert not settings_path.exists() or "custom_tunnel_opt_in" not in settings_path.read_text(encoding="utf-8")

    _box(panel, "tunnelpanel-opt-in").click()
    assert SettingsStore(settings_path).load().settings.custom_tunnel_opt_in is True
    assert _box(panel, "tunnelpanel-opt-in").isChecked() is True

    _box(panel, "tunnelpanel-opt-in").click()
    assert SettingsStore(settings_path).load().settings.custom_tunnel_opt_in is False
    assert "custom_tunnel_opt_in" not in settings_path.read_text(encoding="utf-8")


def test_a_connector_tunnel_flag_saves_and_says_why_it_is_not_reachable(qtbot, settings_path, tmp_path):
    _settings(settings_path, custom_connectors=(CONNECTOR,))
    panel, _spawn, _confirm = _panel(qtbot, settings_path, tmp_path)
    assert _label(panel, "tunnelpanel-connector-0-name").text() == "notes"
    assert _label(panel, "tunnelpanel-connector-0-note").text() == model.NOT_REACHABLE_OPT_IN_OFF
    assert _box(panel, "tunnelpanel-connector-0-exposed").isChecked() is False

    _box(panel, "tunnelpanel-connector-0-exposed").click()

    saved = SettingsStore(settings_path).load().settings.custom_connectors
    assert saved[0]["tunnel_exposed"] is True
    assert _box(panel, "tunnelpanel-connector-0-exposed").isChecked() is True


def test_no_connectors_line_shows_when_none_are_saved(qtbot, settings_path, tmp_path):
    panel, _spawn, _confirm = _panel(qtbot, settings_path, tmp_path)
    assert _label(panel, "tunnelpanel-connectors-empty").isHidden() is False
    assert _label(panel, "tunnelpanel-connectors-empty").text() == model.NO_CONNECTORS_TEXT


def test_a_settings_file_with_a_problem_is_not_written_over(qtbot, settings_path, tmp_path):
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text("{not json", encoding="utf-8")
    before = settings_path.read_text(encoding="utf-8")
    panel, _spawn, _confirm = _panel(qtbot, settings_path, tmp_path)

    _box(panel, "tunnelpanel-group-docs").click()

    assert settings_path.read_text(encoding="utf-8") == before
    assert _label(panel, "tunnelpanel-message").text() == PROBLEM_TEXT


def test_the_tunnel_panel_is_the_first_section_of_settings(qtbot, settings_path, tmp_path):
    from meridian_desktop.ui.main_window import MainWindow

    spawn, confirm = Spawn(), Confirm()
    window = MainWindow(
        SettingsStore(settings_path),
        tunnel_runner=type("Runner", (), {"status": staticmethod(runner_reply("stopped"))})(),
        runner_actions=_actions(settings_path, spawn, confirm, tmp_path),
    )
    qtbot.addWidget(window)
    index = next(i for i in range(window.tabs.count()) if window.tabs.tabText(i) == "Settings")
    page = window.tabs.widget(index)
    body = page.findChild(QWidget, "settings-page")
    first = body.layout().itemAt(0).widget()
    assert isinstance(first, TunnelPanel)
    assert first.objectName() == "tunnel-panel"
    assert window._tunnel_panel is first


@pytest.fixture(autouse=True)
def _no_real_runner(monkeypatch):
    """The panel's only runner calls are the fakes above. A test that reached the real runner would fail here."""
    from meridian_desktop import runner_client

    def refuse(*args, **kwargs):
        raise AssertionError("the real runner was started by a Tunnel panel test")

    monkeypatch.setattr(runner_client.RunnerClient, "run", refuse)
