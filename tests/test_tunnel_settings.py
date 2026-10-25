"""Tests for the owner-round desktop changes: the tunnel sign-in lifetime, the one sentence on the Home tunnel card, the
plain update-check wording, and the count of custom program names dropped on save.

Every outside effect is a fake or a temp file: a spawn fake for the runner (no process starts), a temp settings file and
control log, an in-memory keychain, and a transport fake for GitHub. No test binds a port. Offscreen Qt, from conftest.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

import pytest
from PySide6.QtWidgets import QComboBox, QLabel, QLineEdit, QPushButton

from _fakes import MemoryKeyring
from meridian_desktop import tunnel_status, updater
from meridian_desktop.runner_controls import SCOPE_LOCAL, SCOPE_TUNNEL, RunnerControl
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import (
    DEFAULT_TUNNEL_SIGNIN_DAYS,
    TUNNEL_SIGNIN_DAY_CHOICES,
    Settings,
    SettingsStore,
)
from meridian_desktop.ui.settings_tab import SettingsTab

PY = "C:/Users/tester/core-env/python.exe"
CWD = "C:/Users/tester/meridian"
ENV_KEY = "MERIDIAN_TUNNEL_SIGNIN_DAYS"


class FakeSpawn:
    """Records each runner call and answers with the next canned process result. No real process starts."""

    def __init__(self, *replies: subprocess.CompletedProcess) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), dict(kwargs)))
        return self.replies.pop(0)


def _reply(payload: dict) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(payload).encode("utf-8"), stderr=b"")


def _child(scope: str, state: str) -> dict:
    return {"scope": scope, "child": {"state": state, "pid": 4321}}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _control(tmp_path: Path, spawn: FakeSpawn, **kwargs) -> RunnerControl:
    return RunnerControl(PY, CWD, spawn=spawn, log_path=tmp_path / "control.log", **kwargs)


# ----------------------------------------------------------- tunnel sign-in lifetime: settings


def test_the_lifetime_choices_are_30_90_and_365_days_with_30_the_default():
    assert TUNNEL_SIGNIN_DAY_CHOICES == (30, 90, 365)
    assert DEFAULT_TUNNEL_SIGNIN_DAYS == 30
    assert Settings().tunnel_signin_days == 30


def test_an_old_settings_file_without_the_key_loads_as_30_days_with_no_warning(settings_path):
    _write(settings_path, json.dumps({"mode": "local", "check_updates": True}))
    result = SettingsStore(settings_path).load()
    assert result.settings.tunnel_signin_days == 30
    assert result.warning is None


@pytest.mark.parametrize("value", [7, 60, "90", 90.0, True, None])
def test_an_invalid_lifetime_loads_as_30_days_and_says_so(settings_path, value):
    _write(settings_path, json.dumps({"tunnel_signin_days": value}))
    result = SettingsStore(settings_path).load()
    assert result.settings.tunnel_signin_days == 30
    assert result.warning is not None and "tunnel_signin_days" in result.warning


@pytest.mark.parametrize("days", [90, 365])
def test_a_saved_lifetime_round_trips_and_is_written_under_its_key(settings_path, days):
    store = SettingsStore(settings_path)
    store.save(Settings(tunnel_signin_days=days))
    assert store.load().settings.tunnel_signin_days == days
    assert json.loads(settings_path.read_text(encoding="utf-8"))["tunnel_signin_days"] == days


def test_the_default_lifetime_is_not_written_so_a_file_that_keeps_it_is_unchanged(settings_path):
    SettingsStore(settings_path).save(Settings(tunnel_signin_days=30))
    assert "tunnel_signin_days" not in json.loads(settings_path.read_text(encoding="utf-8"))


def test_saving_another_field_keeps_the_saved_lifetime(settings_path):
    store = SettingsStore(settings_path)
    store.save(Settings(tunnel_signin_days=365))
    store.set_mode("hosted")
    assert store.load().settings.tunnel_signin_days == 365


# ----------------------------------------------------------- tunnel sign-in lifetime: runner environment


def test_a_tunnel_start_passes_the_saved_lifetime_as_the_environment_variable(tmp_path):
    spawn = FakeSpawn(_reply(_child(SCOPE_TUNNEL, "running")))
    result = _control(tmp_path, spawn, signin_days=lambda: 90).start(SCOPE_TUNNEL)
    assert result.confirmed
    assert spawn.calls[0][1]["env"][ENV_KEY] == "90"


def test_a_tunnel_stop_passes_no_lifetime_and_no_environment(tmp_path):
    spawn = FakeSpawn(_reply(_child(SCOPE_TUNNEL, "stopped")))
    _control(tmp_path, spawn, signin_days=lambda: 90).stop(SCOPE_TUNNEL)
    assert "env" not in spawn.calls[0][1]


def test_the_tunnel_start_adds_only_the_lifetime_variable_to_the_desktop_environment(tmp_path):
    spawn = FakeSpawn(_reply(_child(SCOPE_TUNNEL, "running")))
    _control(tmp_path, spawn, signin_days=lambda: 365).start(SCOPE_TUNNEL)
    env = spawn.calls[0][1]["env"]
    added = {key: value for key, value in env.items() if os.environ.get(key) != value}
    assert added == {ENV_KEY: "365"}
    assert set(os.environ) - set(env) == set()


@pytest.mark.parametrize(
    ("method", "scope", "state"),
    [
        ("start", SCOPE_LOCAL, "running"),
        ("stop", SCOPE_LOCAL, "stopped"),
        ("status", SCOPE_TUNNEL, "running"),
        ("status", SCOPE_LOCAL, "running"),
    ],
)
def test_no_other_runner_call_gets_the_lifetime(tmp_path, method, scope, state):
    spawn = FakeSpawn(_reply(_child(scope, state)))
    getattr(_control(tmp_path, spawn, signin_days=lambda: 365), method)(scope)
    assert "env" not in spawn.calls[0][1]


def test_a_tunnel_start_reads_the_saved_lifetime_from_settings_at_each_start(tmp_path, monkeypatch):
    local_app_data = tmp_path / "localappdata"
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    store = SettingsStore(local_app_data / "meridian-desktop" / "settings.json")
    spawn = FakeSpawn(_reply(_child(SCOPE_TUNNEL, "running")), _reply(_child(SCOPE_TUNNEL, "running")))
    control = RunnerControl(PY, CWD, spawn=spawn, log_path=tmp_path / "control.log")
    control.start(SCOPE_TUNNEL)  # no settings file yet, so the default
    store.save(Settings(tunnel_signin_days=90))
    control.start(SCOPE_TUNNEL)
    assert [call[1]["env"][ENV_KEY] for call in spawn.calls] == [str(DEFAULT_TUNNEL_SIGNIN_DAYS), "90"]


# ----------------------------------------------------------- tunnel sign-in lifetime: Settings control


@pytest.fixture
def secrets():
    return KeyringSecretStore(backend=MemoryKeyring())


def _tab(qtbot, settings_path, secrets) -> SettingsTab:
    tab = SettingsTab(SettingsStore(settings_path), secrets=secrets)
    qtbot.addWidget(tab)
    return tab


def _signin_box(tab) -> QComboBox:
    return tab.findChild(QComboBox, "tunnel-signin-days")


def test_the_settings_tab_offers_30_90_and_365_days_and_shows_the_saved_one(qtbot, settings_path, secrets):
    SettingsStore(settings_path).save(Settings(tunnel_signin_days=90))
    tab = _tab(qtbot, settings_path, secrets)
    box = _signin_box(tab)
    assert box is not None
    assert [box.itemText(index) for index in range(box.count())] == ["30 days (default)", "90 days", "365 days"]
    assert [box.itemData(index) for index in range(box.count())] == [30, 90, 365]
    assert box.currentData() == 90


def test_choosing_a_lifetime_saves_it_and_a_new_tab_shows_it(qtbot, settings_path, secrets):
    tab = _tab(qtbot, settings_path, secrets)
    box = _signin_box(tab)
    box.setCurrentIndex(box.findData(365))
    assert SettingsStore(settings_path).load().settings.tunnel_signin_days == 365
    later = _tab(qtbot, settings_path, secrets)
    assert _signin_box(later).currentData() == 365


# ----------------------------------------------------------- Home tunnel card: one sentence


def test_the_start_hint_is_one_plain_sentence_about_what_the_tunnel_is_for():
    hint = tunnel_status.START_HINT
    assert len(re.split(r"(?<=[.!?])\s+", hint.strip())) == 1
    for name in ("claude.ai", "codebase-memory", "Serena", "filesystem", "Zotero", "Search", "Projects", "Sessions"):
        assert name in hint
    assert "without it" in hint
    assert "press Start on the Home tunnel card" in hint


def test_the_start_hint_keeps_the_yes_or_no_before_any_start():
    assert "asks first" in tunnel_status.START_HINT
    assert "without a yes" in tunnel_status.START_HINT


def test_the_not_running_tunnel_detail_carries_the_new_sentence():
    status = tunnel_status.TunnelStatus(state=tunnel_status.NOT_RUNNING, reason="r", checked_at=_when())
    assert tunnel_status.START_HINT in tunnel_status.bar_texts(status)[1]


def _when() -> datetime:
    return datetime(2026, 10, 25, 9, 30, 0)


# ----------------------------------------------------------- update check: plain wording


def _release_transport(asset_names: list[str], tag: str = "v0.1.0"):
    assets = [
        {"name": name, "browser_download_url": f"https://github.com/example/{name}", "size": 10}
        for name in asset_names
    ]
    body = json.dumps({"tag_name": tag, "draft": False, "assets": assets}).encode("utf-8")

    def transport(url, max_bytes, dest):
        assert url == updater.LATEST_RELEASE_URL
        return updater.Reply(200, {}, body)

    return transport


def test_a_release_with_no_zip_says_no_build_is_published_yet_and_that_it_is_not_an_error():
    transport = _release_transport(["SHA256SUMS", "SHA256SUMS.sig"])
    with pytest.raises(updater.Refused) as info:
        updater.fetch_latest(transport)
    assert info.value.reason == (
        "No downloadable build is published for version 0.1.0 yet. This is not an error in the app."
    )


# ----------------------------------------------------------- custom program names: dropped count


def test_saving_custom_program_names_keeps_the_valid_ones_and_returns_only_the_dropped_count(settings_path):
    store = SettingsStore(settings_path)
    store.save(Settings(db_path=str(settings_path.parent / "meridian.db"), tunnel_signin_days=90))
    dropped = store.save_custom_program_names(["myagent.exe", "bad/x.exe", "", "cursor.exe", "myagent.exe"])
    assert dropped == 2
    saved = store.load().settings
    assert saved.agent_process_names == ("myagent.exe",)
    assert saved.tunnel_signin_days == 90
    assert saved.db_path == str(settings_path.parent / "meridian.db")


def _names_line(tab) -> QLabel:
    return tab.findChild(QLabel, "settings-custom-names-message")


def _visible_text(tab) -> str:
    texts = [label.text() for label in tab.findChildren(QLabel)]
    texts += [button.text() for button in tab.findChildren(QPushButton)]
    texts += [field.text() for field in tab.findChildren(QLineEdit)]
    return "\n".join(texts)


def test_a_save_that_drops_invalid_names_shows_the_count_and_never_the_names(qtbot, settings_path, secrets):
    tab = _tab(qtbot, settings_path, secrets)
    tab.save_custom_program_names(["myagent.exe", "bad/x.exe", ""])
    line = _names_line(tab)
    assert line.text() == "Removed 2 invalid name(s) on save."
    assert not line.isHidden()
    assert "bad/x.exe" not in _visible_text(tab)


def test_the_count_line_is_cleared_by_the_next_save_with_no_invalid_names(qtbot, settings_path, secrets):
    tab = _tab(qtbot, settings_path, secrets)
    tab.save_custom_program_names(["bad/x.exe"])
    assert _names_line(tab).text() == "Removed 1 invalid name(s) on save."
    tab.save_custom_program_names(["myagent.exe"])
    assert _names_line(tab).text() == ""
    assert _names_line(tab).isHidden()
