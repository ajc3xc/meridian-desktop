"""Home reads its cards again after a runner action or a client setup change, and when it is shown again.

The reported bug: "N need attention" on the Tools card never updated. A tunnel action re-read only the Tunnel card, a
local Start or Stop in Settings and a client setup change told Home nothing, a check asked while a read was busy was
dropped, and showing Home again within five minutes read nothing. These tests drive Home with fakes only: no socket
opens, no process starts, and the settings file is in pytest's temp directory.
"""

from __future__ import annotations

import json
import subprocess
import threading
from collections import Counter
from datetime import datetime
from pathlib import Path

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFrame, QLabel, QPushButton

from meridian_desktop import local_client, runner_client
from meridian_desktop.client_setup_runner import COMMAND_TIMEOUT_SECONDS, ClientSetupRunner, CommandOutput
from meridian_desktop.latex_client import ENGINE_NOT_RUNNING, EngineStatus
from meridian_desktop.runner_client import RunnerResult
from meridian_desktop.runner_controls import RunnerControl, build_argv, confirmation_text
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.slot_ports import SlotPortResult
from meridian_desktop.tools_status import YES, ToolRow, ToolsStatus
from meridian_desktop.ui import home_tab, scope_controls
from meridian_desktop.ui.client_setup_section import ClientSetupSection
from meridian_desktop.ui.home_tab import HomeDeps, HomeTab
from meridian_desktop.ui.runner_actions import LaunchStarts, RunnerActions
from meridian_desktop.ui.runner_section import RunnerSection
from meridian_desktop.zotero_client import API_NOT_RUNNING, ApiCheck, ZoteroStatus

QUICK = ("local", "tunnel", "services", "zotero")


# ----------------------------------------------------------------- fakes


class Reads:
    """Every card's read, counted. A tools read can be held until a gate opens."""

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self._lock = threading.Lock()
        self.tools_gates: list[threading.Event | None] = []
        self.tools_answers: list[ToolsStatus] = []

    def _count(self, key: str) -> int:
        with self._lock:
            self.counts[key] += 1
            return self.counts[key]

    def snapshot(self) -> Counter[str]:
        with self._lock:
            return Counter(self.counts)

    def probe(self, url):
        self._count("local")
        return local_client.HEALTH_NOT_RUNNING

    def tunnel(self, scope):
        self._count("tunnel")
        return RunnerResult(verb="status", ok=True, payload={"scope": "tunnel", "child": {"state": "not_started"}}, exit_code=0)

    def engine(self, url):
        self._count("services")
        return EngineStatus(state=ENGINE_NOT_RUNNING, message="Not running (test stand-in).")

    def ports(self):
        return [SlotPortResult("test slot", 1, False)]

    def zotero_status(self):
        self._count("zotero")
        return ZoteroStatus(exe=None, process_running=False, api=ApiCheck(API_NOT_RUNNING, "not answering"))

    def tools(self):
        number = self._count("tools")
        with self._lock:
            gate = self.tools_gates[number - 1] if number <= len(self.tools_gates) else None
            answer = self.tools_answers[number - 1] if number <= len(self.tools_answers) else _tools_ready()
        if gate is not None:
            gate.wait(10)
        return answer


class FakeZotero:
    def __init__(self, reads: Reads) -> None:
        self._reads = reads

    def status(self):
        return self._reads.zotero_status()

    def probe_api(self):
        return ApiCheck(API_NOT_RUNNING, "not answering")

    def start_zotero(self, exe):
        raise AssertionError("no Zotero start in these tests")


def _tools_ready():
    rows = (ToolRow("This computer", "Local Meridian server", YES, YES, "answers"),)
    return ToolsStatus(rows=rows, message="Checked.", checked_at=datetime(2026, 10, 23, 14, 5))


def _tools_one_missing():
    rows = (
        ToolRow("Codex", "Meridian entries", "no", "unchecked", "Not in Codex's settings yet."),
        ToolRow("This computer", "Local Meridian server", YES, YES, "answers"),
    )
    return ToolsStatus(rows=rows, message="Checked.", checked_at=datetime(2026, 10, 23, 14, 5))


def _done(code, payload=None):
    body = json.dumps(payload).encode("utf-8") if payload is not None else b""
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=body, stderr=b"")


class Spawn:
    """A fake for subprocess.run. Every start reports running, every stop and status not running."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self._lock = threading.Lock()

    def __call__(self, argv, **kwargs):
        with self._lock:
            self.calls.append(list(argv))
        verb, scope = argv[3], argv[5]
        state = "running" if verb == "start" else "stopped"
        return _done(0, {"scope": scope, "child": {"state": state, "pid": 77}})

    @property
    def verbs(self):
        with self._lock:
            return [argv[3] for argv in self.calls]


class Confirm:
    def __init__(self, *answers) -> None:
        self._answers = list(answers)
        self.asked: list[tuple[str, str, str]] = []

    def __call__(self, parent, title, body, confirm_text):
        self.asked.append((title, body, confirm_text))
        return self._answers.pop(0) if self._answers else False


def _core(tmp_path: Path) -> tuple[str, str]:
    """A core checkout and Python that exist, so the client setup section accepts them. Neither is ever run."""
    checkout = tmp_path / "core"
    (checkout / "meridian").mkdir(parents=True)
    (checkout / "meridian" / "__init__.py").write_text("", encoding="utf-8")
    python = tmp_path / "venv" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_text("not a real interpreter", encoding="utf-8")
    return str(python), str(checkout)


def _store(settings_path: Path, tmp_path: Path) -> SettingsStore:
    python, checkout = _core(tmp_path)
    store = SettingsStore(settings_path)
    store.save(Settings(core_python=python, core_checkout=checkout, tunnel_control_enabled=True))
    return store


def _actions(store, spawn, confirm, tmp_path):
    return RunnerActions(
        store,
        make_control=lambda python, cwd: RunnerControl(python, cwd, spawn=spawn, log_path=tmp_path / "control.log"),
        confirm=confirm,
    )


def _deps(store, reads, actions):
    return HomeDeps(
        store=store,
        probe=reads.probe,
        engine=reads.engine,
        ports=reads.ports,
        tunnel=reads.tunnel,
        actions=actions,
        zotero=FakeZotero(reads),
        tools=reads.tools,
        current_version="0.2.0",
    )


def _shown(qtbot, deps):
    tab = HomeTab(deps)
    qtbot.addWidget(tab)
    tab.show()
    qtbot.waitExposed(tab)
    return tab


def _headline(tab, key):
    card = tab.findChild(QFrame, f"home-card-{key}")
    return card.findChild(QLabel, f"home-{key}-headline").text()


def _settle(qtbot, tab, keys=QUICK + ("tools",)):
    for key in keys:
        qtbot.waitUntil(lambda key=key: _headline(tab, key) not in ("Checking...", ""), timeout=5000)


def _grew(reads, before, keys, by=1):
    now = reads.snapshot()
    return all(now[key] >= before[key] + by for key in keys)


@pytest.fixture
def no_start_checks(monkeypatch):
    """The checks after a start would read the cards again too. These tests count only the action's own re-read."""
    monkeypatch.setattr(scope_controls, "START_CHECK_DELAYS_SECONDS", (60.0, 60.0))


@pytest.fixture
def setup(qtbot, settings_path, tmp_path, no_start_checks):
    store = _store(settings_path, tmp_path)
    reads, spawn = Reads(), Spawn()
    confirm = Confirm(True, True, True)
    actions = _actions(store, spawn, confirm, tmp_path)
    return store, reads, spawn, confirm, actions


# ----------------------------------------------------------------- the stall time (F)


def test_a_normal_slow_tools_read_does_not_show_still_checking_early():
    """The Tools read runs the core's plan, then the local probe, then the tunnel status call, one after the other."""
    slowest = COMMAND_TIMEOUT_SECONDS + local_client.DEFAULT_TIMEOUT + runner_client.DEFAULT_TIMEOUT
    assert home_tab.STALL_SECONDS > slowest


def test_each_card_stalls_only_after_stall_seconds(qtbot, setup):
    store, reads, _spawn, _confirm, actions = setup
    tab = _shown(qtbot, _deps(store, reads, actions))
    card = tab.findChild(QFrame, "home-card-tools")
    intervals = [timer.interval() for timer in card.findChildren(QTimer) if timer.isSingleShot()]
    assert int(home_tab.STALL_SECONDS * 1000) in intervals


# ----------------------------------------------------------------- after a runner action (A, B, C)


def test_a_tunnel_start_on_home_reads_the_tunnel_local_and_tools_cards_again(qtbot, setup):
    store, reads, spawn, _confirm, actions = setup
    tab = _shown(qtbot, _deps(store, reads, actions))
    _settle(qtbot, tab)
    before = reads.snapshot()

    tab.findChild(QPushButton, "tunnel-start").click()

    qtbot.waitUntil(lambda: _grew(reads, before, ("tunnel", "local", "tools")), timeout=5000)
    assert spawn.verbs == ["start"]


def test_a_change_replaces_a_tools_read_that_began_before_it(qtbot, setup):
    """The read that was running when the start finished began before it, so its answer is dropped."""
    store, reads, _spawn, _confirm, actions = setup
    held = threading.Event()
    reads.tools_gates = [held]
    reads.tools_answers = [_tools_one_missing(), _tools_ready()]
    tab = _shown(qtbot, _deps(store, reads, actions))
    _settle(qtbot, tab, QUICK)
    assert _headline(tab, "tools") == "Checking..."

    tab.findChild(QPushButton, "tunnel-start").click()

    qtbot.waitUntil(lambda: reads.snapshot()["tools"] == 2, timeout=5000)
    qtbot.waitUntil(lambda: _headline(tab, "tools") == "All set up", timeout=5000)
    held.set()
    qtbot.wait(150)
    assert _headline(tab, "tools") == "All set up"


def test_home_has_a_local_start_and_stop_that_ask_first_with_the_tunnel_confirmation(qtbot, settings_path, tmp_path):
    store = _store(settings_path, tmp_path)
    settings = store.load().settings
    reads, spawn, confirm = Reads(), Spawn(), Confirm(False, False)
    tab = _shown(qtbot, _deps(store, reads, _actions(store, spawn, confirm, tmp_path)))
    card = tab.findChild(QFrame, "home-card-local")
    start = card.findChild(QPushButton, "home-local-start")
    stop = card.findChild(QPushButton, "home-local-stop")
    assert start is not None and stop is not None
    assert start.isEnabled() and stop.isEnabled()

    start.click()
    stop.click()

    assert [(title, text) for title, _body, text in confirm.asked] == [
        ("Start the local server?", "Start"),
        ("Stop the local server?", "Stop"),
    ]
    assert confirm.asked[0][1] == confirmation_text(settings.core_python, "start", "local", settings.core_checkout)
    assert confirm.asked[1][1] == confirmation_text(settings.core_python, "stop", "local", settings.core_checkout)
    assert spawn.calls == []
    assert card.findChild(QLabel, "home-local-control-result").text() == "Stop cancelled. Nothing was stopped."


def test_a_confirmed_local_start_on_home_runs_the_fixed_command_and_reads_the_cards(qtbot, setup):
    store, reads, spawn, _confirm, actions = setup
    settings = store.load().settings
    tab = _shown(qtbot, _deps(store, reads, actions))
    _settle(qtbot, tab)
    before = reads.snapshot()

    tab.findChild(QPushButton, "home-local-start").click()

    qtbot.waitUntil(lambda: _grew(reads, before, ("tunnel", "local", "tools")), timeout=5000)
    assert spawn.calls == [build_argv(settings.core_python, "start", "local")]


def test_home_local_controls_are_off_with_no_core_set(qtbot, settings_path, tmp_path):
    store = SettingsStore(settings_path)
    reads = Reads()
    tab = _shown(qtbot, _deps(store, reads, _actions(store, Spawn(), Confirm(), tmp_path)))
    for name in ("home-local-start", "home-local-stop", "home-local-restart"):
        assert not tab.findChild(QPushButton, name).isEnabled(), name


def test_a_local_start_in_settings_makes_home_read_its_cards_again(qtbot, setup):
    store, reads, spawn, _confirm, actions = setup
    tab = _shown(qtbot, _deps(store, reads, actions))
    _settle(qtbot, tab)
    section = RunnerSection(actions)
    qtbot.addWidget(section)
    before = reads.snapshot()

    section.findChild(QPushButton, "local-start").click()

    qtbot.waitUntil(lambda: _grew(reads, before, ("tunnel", "local", "tools")), timeout=5000)
    assert "start" in spawn.verbs


def test_a_runner_change_while_home_is_hidden_is_read_when_it_is_shown(qtbot, setup):
    store, reads, _spawn, _confirm, actions = setup
    tab = _shown(qtbot, _deps(store, reads, actions))
    _settle(qtbot, tab)
    tab.hide()
    before = reads.snapshot()

    actions.notify_runner_state_changed()
    qtbot.wait(100)
    assert reads.snapshot() == before

    tab.show()
    qtbot.waitUntil(lambda: _grew(reads, before, ("tunnel", "local", "tools")), timeout=5000)


def test_the_start_checks_read_the_cards_again(qtbot, settings_path, tmp_path, monkeypatch):
    monkeypatch.setattr(scope_controls, "START_CHECK_DELAYS_SECONDS", (0.05, 0.3))
    store = _store(settings_path, tmp_path)
    reads, spawn = Reads(), Spawn()
    tab = _shown(qtbot, _deps(store, reads, _actions(store, spawn, Confirm(True), tmp_path)))
    _settle(qtbot, tab)
    before = reads.snapshot()

    tab.findChild(QPushButton, "tunnel-start").click()

    # The fake reports the child stopped at the first check, so the line says so and the cards are read again.
    result = tab.findChild(QLabel, "tunnel-control-result")
    qtbot.waitUntil(lambda: result.text().startswith("Started, but it stopped straight after."), timeout=5000)
    qtbot.waitUntil(lambda: _grew(reads, before, ("tunnel", "local", "tools"), by=2), timeout=5000)


def test_a_launch_time_local_start_makes_home_read_its_cards_again(qtbot, settings_path, tmp_path, no_start_checks):
    python, checkout = _core(tmp_path)
    store = SettingsStore(settings_path)
    store.save(Settings(
        core_python=python, core_checkout=checkout, local_server_autostart=True,
        confirmed_core_python=python, confirmed_core_checkout=checkout,
    ))
    reads, spawn = Reads(), Spawn()
    actions = RunnerActions(
        store,
        make_control=lambda p, c: RunnerControl(p, c, spawn=spawn, log_path=tmp_path / "control.log"),
        confirm=Confirm(),
        probe_local=lambda url: local_client.HEALTH_NOT_RUNNING,
    )
    tab = _shown(qtbot, _deps(store, reads, actions))
    _settle(qtbot, tab)
    before = reads.snapshot()

    launch = LaunchStarts(actions)
    launch.start()

    qtbot.waitUntil(lambda: "start" in spawn.verbs, timeout=5000)
    qtbot.waitUntil(lambda: _grew(reads, before, ("tunnel", "local", "tools")), timeout=5000)


def test_home_never_emits_core_changed_when_it_reads_again(qtbot, setup):
    store, reads, _spawn, _confirm, actions = setup
    emitted = []
    actions.coreChanged.connect(lambda: emitted.append(True))
    tab = _shown(qtbot, _deps(store, reads, actions))
    _settle(qtbot, tab)

    tab.findChild(QPushButton, "tunnel-start").click()
    actions.notify_client_setup_changed()
    tab.hide()
    tab.show()
    qtbot.wait(700)

    assert emitted == []


# ----------------------------------------------------------------- after a client setup change (C, D)


def _apply_flow_run(config: Path):
    """The core's plan and apply, faked: the plan says one entry would be added, and apply writes it."""
    state = {"written": False}

    def entry(action):
        return {
            "client": "claude-desktop",
            "path": str(config),
            "action": action,
            "changes": {"added": ["meridian-docs"], "changed": [], "removed": [], "unchanged": []},
            "re_enabled": [],
            "backup": None,
            "restart_required": False,
        }

    def run(argv, cwd, timeout):
        if "--yes" in argv:
            state["written"] = True
            return CommandOutput(0, json.dumps({"results": [entry("written")], "wrote": True}), "")
        action = "unchanged" if state["written"] and argv[-1] == "all" else "would_write"
        return CommandOutput(0, json.dumps({"results": [entry(action)], "wrote": False}), "")

    return run


def test_a_client_setup_change_makes_home_read_the_tools_again_when_it_is_shown(qtbot, setup, tmp_path):
    store, reads, _spawn, _confirm, actions = setup
    tab = _shown(qtbot, _deps(store, reads, actions))
    _settle(qtbot, tab)
    tab.hide()
    config = tmp_path / "desktop.json"
    config.write_text("{}", encoding="utf-8")
    section = ClientSetupSection(
        store, runner=ClientSetupRunner(store, run=_apply_flow_run(config)),
        confirm=lambda parent, title, text: True, actions=actions,
    )
    qtbot.addWidget(section)
    changed = []
    actions.clientSetupChanged.connect(lambda: changed.append(True))
    section.findChild(QPushButton, "client-setup-check").click()
    apply = section.findChild(QPushButton, "client-setup-claude-desktop-apply")
    qtbot.waitUntil(apply.isEnabled, timeout=5000)
    before = reads.snapshot()

    apply.click()
    qtbot.waitUntil(lambda: changed == [True], timeout=5000)
    assert reads.snapshot()["tools"] == before["tools"]  # Home is hidden, so it waits for the next show

    tab.show()
    qtbot.waitUntil(lambda: reads.snapshot()["tools"] == before["tools"] + 1, timeout=5000)


def test_a_client_setup_change_while_home_shows_reads_the_tools_at_once(qtbot, setup):
    store, reads, _spawn, _confirm, actions = setup
    tab = _shown(qtbot, _deps(store, reads, actions))
    _settle(qtbot, tab)
    before = reads.snapshot()

    actions.notify_client_setup_changed()

    qtbot.waitUntil(lambda: reads.snapshot()["tools"] == before["tools"] + 1, timeout=5000)
    qtbot.wait(100)
    after = reads.snapshot()
    assert all(after[key] == before[key] for key in QUICK)


# ----------------------------------------------------------------- showing Home again (D)


def test_showing_home_again_reads_the_quick_cards_but_not_the_tools(qtbot, setup):
    store, reads, _spawn, _confirm, actions = setup
    tab = _shown(qtbot, _deps(store, reads, actions))
    _settle(qtbot, tab)
    before = reads.snapshot()

    tab.hide()
    tab.show()

    qtbot.waitUntil(lambda: _grew(reads, before, QUICK), timeout=5000)
    qtbot.wait(200)
    assert reads.snapshot()["tools"] == before["tools"]


def test_quick_switches_back_to_home_read_the_quick_cards_once(qtbot, setup):
    store, reads, _spawn, _confirm, actions = setup
    tab = _shown(qtbot, _deps(store, reads, actions))
    _settle(qtbot, tab)
    before = reads.snapshot()

    for _ in range(3):
        tab.hide()
        tab.show()
    qtbot.waitUntil(lambda: _grew(reads, before, QUICK), timeout=5000)
    qtbot.wait(home_tab.SHOW_DEBOUNCE_MS + 300)

    after = reads.snapshot()
    assert all(after[key] == before[key] + 1 for key in QUICK), (before, after)


# ----------------------------------------------------------------- a check asked while a read runs (E)


def test_a_check_asked_while_a_read_runs_is_queued_and_runs_after_it(qtbot, setup):
    store, reads, _spawn, _confirm, actions = setup
    held = threading.Event()
    reads.tools_gates = [held]
    reads.tools_answers = [_tools_one_missing(), _tools_ready()]
    tab = _shown(qtbot, _deps(store, reads, actions))
    _settle(qtbot, tab, QUICK)

    tab.findChild(QPushButton, "home-check-now").click()
    qtbot.wait(100)
    assert reads.snapshot()["tools"] == 1  # still the first read: the new one waits for it

    held.set()
    qtbot.waitUntil(lambda: reads.snapshot()["tools"] == 2, timeout=5000)
    qtbot.waitUntil(lambda: _headline(tab, "tools") == "All set up", timeout=5000)
    qtbot.wait(100)
    assert reads.snapshot()["tools"] == 2


def test_the_headline_still_counts_only_the_tools_rows(qtbot, setup):
    store, reads, _spawn, _confirm, actions = setup
    reads.tools_answers = [_tools_one_missing()]
    tab = _shown(qtbot, _deps(store, reads, actions))
    qtbot.waitUntil(lambda: _headline(tab, "tools") == "1 need attention", timeout=5000)
