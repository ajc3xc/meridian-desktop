"""Tests for the Tunnel tab: ui/tunnel_page.py.

The top of the tab says whether the tunnel is running. That answer comes from one read-only runner
status call, made each time the tab is shown, and every such call here is a fake. The slot line
below it still says the slot states are not available, because this build has no local source for
them. That line names the missing core item (MS-104). Nothing here makes a request, starts a
process or reads a real file. The page runs under pytest-qt on the offscreen platform (conftest).
"""

import gc
import threading
import weakref
from datetime import datetime
from pathlib import Path

import pytest
import shiboken6
from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtWidgets import QLabel, QPushButton, QTableWidget, QWidget

from meridian_desktop.core_locator import FOUND, NOT_FOUND, SOURCE_SEARCHED, LocatedCore
from meridian_desktop.runner_client import (
    ERR_TIMEOUT,
    NOT_AVAILABLE_MESSAGE,
    RunnerClient,
    RunnerResult,
)
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.tunnel_status import (
    CHECK_FAILED_REASON,
    DEPENDENCY_TEXT,
    NOT_AVAILABLE_REASON,
    NOT_RUNNING,
    RUNNING,
    START_COMMAND,
    START_TEXT,
    UNKNOWN,
    UNREAD_REASON,
    TunnelStatus,
)
from meridian_desktop.ui.delivery import Delivered, shared_delivery
from meridian_desktop.ui.runner_actions import RunnerActions
from meridian_desktop.ui.tunnel_page import (
    CHECKED_TEMPLATE,
    HEADLINES,
    NOT_AVAILABLE_TEXT,
    NOT_CHECKED_TEXT,
    NO_TIME_TEXT,
    TunnelPage,
)
from meridian_desktop.ui.text import SELECTABLE_TEXT

REPO = Path(__file__).resolve().parents[1]
TUNNEL_SOURCES = (
    REPO / "meridian_desktop" / "ui" / "tunnel_page.py",
    REPO / "meridian_desktop" / "tunnel_view.py",
    REPO / "meridian_desktop" / "tunnel_status.py",
)
RUN_LABELS = ("tunnel-run-state", "tunnel-run-reason", "tunnel-run-checked", "tunnel-run-dependency", "tunnel-run-start")
CHECKED = datetime(2026, 10, 21, 10, 15, 0)
PY = "C:/Users/tester/core-env/python.exe"
CWD = "C:/Users/tester/Documents/Meridian/repository"


def _clock():
    return CHECKED


class Fake:
    """A fake runner status call. It records each scope asked for, and returns or raises as set.

    ``gate``, when given, holds the call until the test sets it, so a check can be seen while it runs.
    It is called on a thread-pool thread, so the count is kept under a lock.
    """

    def __init__(self, reply=None, *, raises=None, gate=None):
        self.reply = reply
        self.raises = raises
        self.gate = gate
        self._lock = threading.Lock()
        self._scopes = []

    def __call__(self, scope):
        with self._lock:
            self._scopes.append(scope)
        if self.gate is not None:
            self.gate.wait(5)
        if self.raises is not None:
            raise self.raises
        return self.reply

    @property
    def scopes(self):
        with self._lock:
            return list(self._scopes)


def _reply(child_state, *, scope="tunnel"):
    payload = {"scope": scope, "generated_at": 1_800_000_000.0, "child": {"state": child_state}}
    return RunnerResult(verb="status", ok=True, payload=payload, exit_code=0)


def _page(qtbot, status=None, clock=_clock):
    page = TunnelPage(status=status, clock=clock)
    qtbot.addWidget(page)
    return page


def _no_confirm(parent, title, body, confirm_text):
    raise AssertionError("no confirmation dialog may open in this test")


def _page_with_actions(qtbot, settings_path, status=None, *, confirm=_no_confirm):
    """A page whose controls read the settings file at ``settings_path``. The confirmation is a fake, never a dialog."""
    actions = RunnerActions(SettingsStore(settings_path), confirm=confirm)
    page = TunnelPage(status=status, clock=_clock, actions=actions)
    qtbot.addWidget(page)
    return page


def _label(page, name):
    return page.findChild(QLabel, name)


def _headline(page):
    return _label(page, "tunnel-run-state").text()


def _wait_for(qtbot, page, headline):
    qtbot.waitUntil(lambda: _headline(page) == headline, timeout=5000)


def _show_and_wait(qtbot, page, headline):
    page.show()
    _wait_for(qtbot, page, headline)


# ------------------------------------------------------------------ the existing slot-line checks


def test_not_available_text_names_the_missing_core_item():
    assert "MS-104" in NOT_AVAILABLE_TEXT
    assert "local tunnel slot status file" in NOT_AVAILABLE_TEXT


def test_not_available_text_says_what_is_missing_and_what_to_do():
    assert "What is missing:" in NOT_AVAILABLE_TEXT
    assert "Nothing to do in the desktop until it ships." in NOT_AVAILABLE_TEXT


def test_no_tunnel_tab_source_says_not_built_yet():
    for path in TUNNEL_SOURCES:
        source = path.read_text(encoding="utf-8").lower()
        assert "not built yet" not in source, path.name
    assert "not built yet" not in NOT_AVAILABLE_TEXT.lower()


def test_page_without_a_snapshot_shows_the_missing_core_item(qtbot):
    page = TunnelPage()
    qtbot.addWidget(page)

    status = page.findChild(QLabel, "tunnel-status")
    table = page.findChild(QTableWidget, "tunnel-slot-table")
    assert status.text() == NOT_AVAILABLE_TEXT
    assert "MS-104" in status.text()
    assert table.rowCount() == 0


def test_not_available_label_is_plain_text_and_selectable(qtbot):
    page = TunnelPage()
    qtbot.addWidget(page)

    status = page.findChild(QLabel, "tunnel-status")
    assert status.textFormat() == Qt.TextFormat.PlainText
    assert status.textInteractionFlags() == SELECTABLE_TEXT


# ------------------------------------------------------------------ when the check runs


def test_no_check_runs_until_the_tab_is_shown(qtbot):
    fake = Fake(_reply("running"))
    page = _page(qtbot, fake)

    assert _headline(page) == NOT_CHECKED_TEXT
    assert "running" not in _headline(page).lower()
    assert fake.scopes == []


def test_showing_the_tab_runs_one_check_and_shows_running(qtbot):
    fake = Fake(_reply("running"))
    page = _page(qtbot, fake)
    _show_and_wait(qtbot, page, HEADLINES[RUNNING])

    assert fake.scopes == ["tunnel"]
    assert _label(page, "tunnel-run-reason").text() == "The runner reports the tunnel process as running."
    assert _label(page, "tunnel-run-checked").text() == CHECKED_TEMPLATE.format("2026-10-21 10:15:00")


def test_each_time_the_tab_is_shown_it_checks_again(qtbot):
    fake = Fake(_reply("running"))
    page = _page(qtbot, fake)
    _show_and_wait(qtbot, page, HEADLINES[RUNNING])

    page.hide()
    page.show()
    qtbot.waitUntil(lambda: len(fake.scopes) == 2 and _headline(page) == HEADLINES[RUNNING], timeout=5000)
    assert fake.scopes == ["tunnel", "tunnel"]


def test_a_check_still_running_is_not_started_again(qtbot):
    gate = threading.Event()
    fake = Fake(_reply("running"), gate=gate)
    page = _page(qtbot, fake)
    try:
        page.show()
        qtbot.waitUntil(lambda: len(fake.scopes) == 1, timeout=5000)
        page.refresh()
        page.refresh()
        assert len(fake.scopes) == 1
    finally:
        gate.set()
    _wait_for(qtbot, page, HEADLINES[RUNNING])
    assert len(fake.scopes) == 1


# ------------------------------------------------------------------ the three answers on the page


@pytest.mark.parametrize("child_state", ["not_started", "stopped", "crashed"])
def test_a_child_that_is_not_running_shows_not_running(qtbot, child_state):
    page = _page(qtbot, Fake(_reply(child_state)))
    _show_and_wait(qtbot, page, HEADLINES[NOT_RUNNING])

    assert _headline(page) == "Tunnel: not running."


def test_an_unverifiable_child_shows_unknown(qtbot):
    page = _page(qtbot, Fake(_reply("unknown")))
    _show_and_wait(qtbot, page, HEADLINES[UNKNOWN])

    assert _headline(page) == "Tunnel: unknown."


def test_a_missing_runner_shows_unknown_with_the_runner_message(qtbot):
    # The default client has no interpreter, so it reports the runner as not available and starts nothing.
    page = _page(qtbot)
    _show_and_wait(qtbot, page, HEADLINES[UNKNOWN])

    assert _label(page, "tunnel-run-reason").text() == NOT_AVAILABLE_REASON
    assert NOT_AVAILABLE_MESSAGE in _label(page, "tunnel-run-reason").text()


def test_an_explicit_client_with_no_interpreter_is_a_missing_runner(qtbot):
    page = _page(qtbot, RunnerClient().status)
    _show_and_wait(qtbot, page, HEADLINES[UNKNOWN])

    assert _label(page, "tunnel-run-reason").text() == NOT_AVAILABLE_REASON


def test_a_check_still_running_when_its_page_is_deleted_does_not_keep_the_page(qtbot, monkeypatch):
    """A page deleted while its check still runs is freed on the GUI thread, and no widget of it is touched.

    The check used to hold the page's own signal emit. When the pool ended the check on a worker, the worker dropped
    the last reference, and the page's widgets were deleted off the GUI thread. The check now emits to the shared
    delivery object. The collector is off, so only reference counting can free the page here.
    """
    entered = threading.Event()
    gate = threading.Event()
    touched = []
    monkeypatch.setattr(TunnelPage, "_on_check_finished", lambda self, payload: touched.append(self._owner))

    def held(scope):
        entered.set()
        gate.wait(5)
        return _reply("running")

    gc.disable()
    try:
        container = QWidget()
        page = TunnelPage(container, status=held, clock=_clock)
        page.refresh()
        assert entered.wait(5)  # the check has started and is held at the gate
        owner = page._owner
        ref = weakref.ref(page)
        shiboken6.delete(container)  # the page's widgets are deleted on the GUI thread while the check still runs
        del page
        assert ref() is None
        gate.set()
        assert QThreadPool.globalInstance().waitForDone(5000)
        qtbot.wait(50)  # the late result reaches the event loop, and nothing raises
        assert all(token is not owner for token in touched)  # the deleted page's widgets are never touched
    finally:
        gate.set()
        gc.enable()


def test_a_status_call_that_raises_shows_unknown_and_the_page_still_works(qtbot):
    page = _page(qtbot, Fake(raises=RuntimeError("a message that must not be shown")))
    _show_and_wait(qtbot, page, HEADLINES[UNKNOWN])

    assert _label(page, "tunnel-run-reason").text() == CHECK_FAILED_REASON.format("RuntimeError")
    assert "must not be shown" not in _label(page, "tunnel-run-reason").text()
    assert page.findChild(QTableWidget, "tunnel-slot-table") is not None


def test_a_failed_status_call_shows_unknown_never_running(qtbot):
    failed = RunnerResult(verb="status", ok=False, error_code=ERR_TIMEOUT, error_message="The runner command did not finish in time.")
    page = _page(qtbot, Fake(failed))
    _show_and_wait(qtbot, page, HEADLINES[UNKNOWN])

    assert _headline(page) != HEADLINES[RUNNING]
    assert _label(page, "tunnel-run-reason").text() == "The tunnel state is unknown. The runner command did not finish in time."


def test_a_clock_that_fails_after_the_call_shows_unknown_with_no_time(qtbot):
    def broken_clock():
        raise ValueError("clock broken")

    page = _page(qtbot, Fake(_reply("running")), clock=broken_clock)
    _show_and_wait(qtbot, page, HEADLINES[UNKNOWN])

    assert _label(page, "tunnel-run-reason").text() == CHECK_FAILED_REASON.format("ValueError")
    assert _label(page, "tunnel-run-checked").text() == NO_TIME_TEXT


def test_a_reply_text_is_never_shown_as_html_or_echoed(qtbot):
    page = _page(qtbot, Fake(_reply("<b>running</b>")))
    _show_and_wait(qtbot, page, HEADLINES[UNKNOWN])

    reason = _label(page, "tunnel-run-reason")
    assert reason.text() == UNREAD_REASON
    assert reason.textFormat() == Qt.TextFormat.PlainText
    assert "<b>" not in reason.text()


# ------------------------------------------------------------------ the static lines and the controls


def test_the_running_lines_are_plain_text_and_selectable(qtbot):
    page = _page(qtbot, Fake(_reply("running")))
    _show_and_wait(qtbot, page, HEADLINES[RUNNING])

    for name in RUN_LABELS:
        label = _label(page, name)
        assert label is not None, name
        assert label.textFormat() == Qt.TextFormat.PlainText, name
        assert label.textInteractionFlags() == SELECTABLE_TEXT, name


def test_the_dependency_and_start_lines_are_on_the_page(qtbot):
    page = _page(qtbot, Fake(_reply("not_started")))
    _show_and_wait(qtbot, page, HEADLINES[NOT_RUNNING])

    dependency = _label(page, "tunnel-run-dependency").text()
    start = _label(page, "tunnel-run-start").text()
    assert dependency == DEPENDENCY_TEXT
    assert "codebase-memory" in dependency and "Serena" in dependency and "filesystem" in dependency
    assert start == START_TEXT
    assert START_COMMAND in start
    assert "The desktop starts or stops the tunnel only when you confirm it" in start


def test_the_slot_line_below_keeps_its_slot_state_text(qtbot):
    page = _page(qtbot, Fake(_reply("running")))
    _show_and_wait(qtbot, page, HEADLINES[RUNNING])

    assert _label(page, "tunnel-status").text() == NOT_AVAILABLE_TEXT


def test_the_tab_has_only_the_three_tunnel_controls_and_no_refresh_button(qtbot, settings_path):
    # With the core set, the tab has its three controls and nothing else. The fix button is shown only while they are off.
    SettingsStore(settings_path).save(Settings(core_python=PY, core_checkout=CWD))
    page = _page_with_actions(qtbot, settings_path, Fake(_reply("running")))
    _show_and_wait(qtbot, page, HEADLINES[RUNNING])

    shown = sorted(button.objectName() for button in page.findChildren(QPushButton) if button.isVisible())
    assert shown == ["tunnel-restart", "tunnel-start", "tunnel-stop"]


def test_with_the_core_not_found_the_only_extra_button_is_the_one_that_enables_the_controls(qtbot):
    page = _page(qtbot, Fake(_reply("running")))
    _show_and_wait(qtbot, page, HEADLINES[RUNNING])

    names = sorted(button.objectName() for button in page.findChildren(QPushButton))
    assert names == ["tunnel-fix", "tunnel-restart", "tunnel-start", "tunnel-stop"]
    fix = page.findChild(QPushButton, "tunnel-fix")
    assert fix.isVisible() and fix.isEnabled()
    assert fix.text() == "Choose the Meridian core folder"


def test_the_tunnel_controls_are_disabled_when_tunnel_control_is_turned_off(qtbot, settings_path):
    # The owner has set tunnel_control_enabled to false, so the buttons are off and the note says why.
    SettingsStore(settings_path).save(Settings(core_python=PY, core_checkout=CWD, tunnel_control_enabled=False))
    page = _page_with_actions(qtbot, settings_path, Fake(_reply("running")))
    _show_and_wait(qtbot, page, HEADLINES[RUNNING])

    for name in ("tunnel-start", "tunnel-stop", "tunnel-restart"):
        assert page.findChild(QPushButton, name).isEnabled() is False, name
    # The note says why, and the one button next to it turns the controls on. Nothing is greyed out without a way to fix it.
    assert "tunnel controls are off" in _label(page, "tunnel-control-note").text()
    fix = page.findChild(QPushButton, "tunnel-fix")
    assert fix.isVisible() and fix.text() == "Turn on the tunnel controls"


def test_the_tunnel_controls_are_on_by_default_when_the_core_is_set(qtbot, settings_path):
    # The settings file is absent, so tunnel_control_enabled is its default, true (contract amendment F). The core
    # is set, so the buttons are on, and each one still asks the owner first.
    SettingsStore(settings_path).save(Settings(core_python=PY, core_checkout=CWD))
    page = _page_with_actions(qtbot, settings_path, Fake(_reply("running")))
    _show_and_wait(qtbot, page, HEADLINES[RUNNING])

    for name in ("tunnel-start", "tunnel-stop", "tunnel-restart"):
        assert page.findChild(QPushButton, name).isEnabled() is True, name


def test_the_tunnel_controls_say_the_core_is_not_set_when_it_is_not_set_by_default(qtbot, settings_path):
    page = _page_with_actions(qtbot, settings_path, Fake(_reply("running")))
    _show_and_wait(qtbot, page, HEADLINES[RUNNING])

    for name in ("tunnel-start", "tunnel-stop", "tunnel-restart"):
        assert page.findChild(QPushButton, name).isEnabled() is False, name
    assert "Meridian core was not found" in _label(page, "tunnel-control-note").text()
    fix = page.findChild(QPushButton, "tunnel-fix")
    assert fix.isVisible() and fix.text() == "Choose the Meridian core folder"


def test_a_delivered_result_with_a_foreign_owner_is_not_shown(qtbot):
    """A result that carries another object's owner token is dropped, even when its generation is the current one."""
    page = _page(qtbot, Fake(_reply("running")))
    _show_and_wait(qtbot, page, HEADLINES[RUNNING])
    stopped = TunnelStatus(NOT_RUNNING, "The runner reports the tunnel process as stopped.", CHECKED)

    shared_delivery().delivered.emit(Delivered(object(), (page._generation, (stopped, None))))
    qtbot.wait(50)

    assert _headline(page) == HEADLINES[RUNNING]
    assert _label(page, "tunnel-run-reason").text() == "The runner reports the tunnel process as running."


def test_a_delivered_result_with_a_stale_generation_is_not_shown(qtbot):
    """A result from a check that a newer check has superseded is dropped, even when its owner token is this page's."""
    page = _page(qtbot, Fake(_reply("running")))
    _show_and_wait(qtbot, page, HEADLINES[RUNNING])
    stopped = TunnelStatus(NOT_RUNNING, "The runner reports the tunnel process as stopped.", CHECKED)
    stale = page._generation - 1

    shared_delivery().delivered.emit(Delivered(page._owner, (stale, (stopped, None))))
    qtbot.wait(50)

    assert _headline(page) == HEADLINES[RUNNING]


def test_when_the_core_search_finishes_the_tunnel_is_checked_again_with_the_core_it_found(qtbot, settings_path):
    status = Fake(_reply("running"))
    actions = RunnerActions(
        SettingsStore(settings_path),
        confirm=_no_confirm,
        locate=lambda store: LocatedCore(FOUND, CWD, PY, SOURCE_SEARCHED),
    )
    page = TunnelPage(status=status, clock=_clock, actions=actions)
    qtbot.addWidget(page)
    _show_and_wait(qtbot, page, HEADLINES[RUNNING])
    before = len(status.scopes)
    assert page.findChild(QPushButton, "tunnel-start").isEnabled() is False  # no core saved yet

    actions.find_core()

    qtbot.waitUntil(lambda: len(status.scopes) > before, timeout=5000)
    qtbot.waitUntil(lambda: page.findChild(QPushButton, "tunnel-start").isEnabled(), timeout=5000)


def test_a_core_search_that_is_still_running_does_not_start_a_tunnel_check(qtbot, settings_path):
    gate = threading.Event()
    status = Fake(_reply("running"))
    actions = RunnerActions(SettingsStore(settings_path), confirm=_no_confirm, locate=lambda store: gate.wait(5) and LocatedCore(NOT_FOUND))
    page = TunnelPage(status=status, clock=_clock, actions=actions)
    qtbot.addWidget(page)
    _show_and_wait(qtbot, page, HEADLINES[RUNNING])
    before = len(status.scopes)

    actions.find_core()  # the search holds on its worker until the gate opens
    qtbot.wait(50)
    gate.set()
    qtbot.waitUntil(lambda: actions.located is not None, timeout=5000)

    # The search's own start changed nothing, so the only new check is the one that follows the finished search.
    assert len(status.scopes) == before + 1
