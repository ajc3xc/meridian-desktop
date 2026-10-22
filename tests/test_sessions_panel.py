"""Tests for the Sessions tab (meridian_desktop/ui/sessions_panel.py), with pytest-qt. Offscreen Qt, no network.

Both transports are fakes, so no socket is opened and no keychain is read. Each fake records its calls and the
thread it ran on, so a test can prove that a refresh runs off the UI thread, that nothing is sent before the tab is
shown, and that a refused address sends nothing. The token is checked against every widget's visible text.
"""

import json
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QAbstractItemView, QComboBox, QLabel, QPushButton, QTableWidget

from meridian_desktop import hosted_client, sessions_client as sc
from meridian_desktop.local_client import LocalClientError
from meridian_desktop.ui.sessions_panel import (
    COLUMNS,
    LOADING_TEXT,
    NO_PROJECTS_TEXT,
    REFRESH_MS,
    SessionsPanel,
    choose_project,
    row_cells,
    status_text,
)
from meridian_desktop.ui.text import SELECTABLE_TEXT

LOCAL = "http://127.0.0.1:7878"
HOSTED = "https://usemeridian.us"
PID = "5787cc92-ba7d-4788-b17c-28ab7938b839"
PID2 = "11111111-2222-4333-8444-555555555555"
SENTINEL_TOKEN = "sk_meridian_PANELSENTINEL0123456789"
SECRET_SUMMARY = "SECRET-SUMMARY-TEXT-do-not-show"


def ago(**delta):
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


def session(**overrides):
    row = {
        "id": "sess-1",
        "project_id": PID,
        "name": "auth-refactor",
        "status": "active",
        "last_seen": ago(hours=2),
        "created_at": ago(hours=3),
        "session_summary": {"notes": SECRET_SUMMARY},
        "agent_framework": "claude_code",
        "client_type": "claude-code",
    }
    row.update(overrides)
    return row


def body_of(rows):
    return json.dumps(rows).encode("utf-8")


class FakeLocal:
    """A local transport. It records the base, the path and the thread. It can block until released."""

    def __init__(self, status=200, body=b"[]", error=None, release=None):
        self.status = status
        self.body = body
        self.error = error
        self.release = release
        self.calls = []
        self.threads = []

    def __call__(self, base_url, path, timeout):
        self.calls.append((base_url, path, timeout))
        self.threads.append(threading.current_thread())
        if self.release is not None:
            self.release.wait(5)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(status=self.status, body=self.body)


class FakeHosted:
    """A hosted transport. It records each request and the thread it ran on."""

    def __init__(self, status=200, body=b"[]"):
        self.status = status
        self.body = body
        self.calls = []
        self.threads = []

    def __call__(self, request, timeout):
        self.calls.append((request, timeout))
        self.threads.append(threading.current_thread())
        return hosted_client.Reply(self.status, self.body)


class ScriptedLocal:
    """A local transport that returns one body per call. The first call can be held until released."""

    def __init__(self, bodies, gate=None):
        self.bodies = list(bodies)
        self.gate = gate
        self.paths = []

    def __call__(self, base_url, path, timeout):
        self.paths.append(path)
        index = len(self.paths)
        if index == 1 and self.gate is not None:
            self.gate.wait(5)
        return SimpleNamespace(status=200, body=self.bodies[index - 1])


def make_panel(qtbot, *, local, hosted=None, local_url=LOCAL, hosted_value=(HOSTED, None),
               projects=((PID, "Alpha"),), project_provider=None):
    hosted = hosted if hosted is not None else FakeHosted()
    panel = SessionsPanel(
        lambda: local_url,
        lambda: hosted_value,
        project_provider if project_provider is not None else (lambda: list(projects)),
        local_transport=local,
        hosted_transport=hosted,
    )
    qtbot.addWidget(panel)
    panel.hide()
    return panel, hosted


def settle(qtbot, panel):
    """Wait until no refresh is running. The panel clears its in-flight marker when the latest result lands."""
    qtbot.waitUntil(lambda: panel._in_flight is None, timeout=5000)  # noqa: SLF001 - test hook


def table_of(panel):
    return panel.findChild(QTableWidget, "sessions-table")


def status_of(panel):
    return panel.findChild(QLabel, "sessions-status").text()


def picker_of(panel):
    return panel.findChild(QComboBox, "sessions-project-picker")


def all_visible_text(panel):
    """Every string the user can read in the panel: labels, the status line, the picker and every table cell."""
    parts = [label.text() for label in panel.findChildren(QLabel)]
    combo = picker_of(panel)
    parts.extend(combo.itemText(index) for index in range(combo.count()))
    table = table_of(panel)
    for row in range(table.rowCount()):
        for column in range(table.columnCount()):
            item = table.item(row, column)
            if item is not None:
                parts.append(item.text())
    return "\n".join(parts)


# ----------------------------------------------------------------- nothing sent before the tab is shown


def test_nothing_is_sent_when_the_panel_is_built_but_not_shown(qtbot):
    local = FakeLocal()
    panel, hosted = make_panel(qtbot, local=local)
    assert local.calls == []
    assert hosted.calls == []
    assert not panel._timer.isActive()  # noqa: SLF001


def test_showing_the_tab_refreshes_once_and_starts_the_timer(qtbot):
    local = FakeLocal(body=body_of([session()]))
    panel, _hosted = make_panel(qtbot, local=local)
    panel.show()
    qtbot.waitUntil(lambda: len(local.calls) == 1)
    settle(qtbot, panel)
    assert panel._timer.isActive()  # noqa: SLF001
    assert panel._timer.interval() == REFRESH_MS == 5000  # noqa: SLF001


def test_hiding_the_tab_stops_the_timer_and_a_tick_while_hidden_sends_nothing(qtbot):
    local = FakeLocal()
    panel, _hosted = make_panel(qtbot, local=local)
    panel.show()
    settle(qtbot, panel)
    panel.hide()
    assert not panel._timer.isActive()  # noqa: SLF001
    count = len(local.calls)
    qtbot.wait(50)
    assert len(local.calls) == count  # the stopped timer sends nothing while the tab is hidden


def test_a_tick_is_skipped_while_a_refresh_is_still_running(qtbot):
    release = threading.Event()
    local = FakeLocal(release=release)
    panel, _hosted = make_panel(qtbot, local=local)
    panel.show()
    qtbot.waitUntil(lambda: len(local.calls) == 1)
    panel._on_tick()  # noqa: SLF001
    panel._on_tick()  # noqa: SLF001
    qtbot.wait(50)
    assert len(local.calls) == 1
    release.set()
    settle(qtbot, panel)
    panel._on_tick()  # noqa: SLF001
    qtbot.waitUntil(lambda: len(local.calls) == 2)


# ----------------------------------------------------------------- off the UI thread


def test_refresh_runs_off_the_ui_thread(qtbot):
    local = FakeLocal(body=body_of([session()]))
    panel, _hosted = make_panel(qtbot, local=local)
    panel.show()
    qtbot.waitUntil(lambda: len(local.threads) == 1)
    settle(qtbot, panel)
    assert local.threads[0] is not threading.main_thread()


# ----------------------------------------------------------------- rows and the status line


def test_rows_show_name_status_age_and_client(qtbot):
    local = FakeLocal(body=body_of([session(name="auth-refactor", status="active", client_type="claude-code")]))
    panel, _hosted = make_panel(qtbot, local=local)
    panel.show()
    settle(qtbot, panel)
    table = table_of(panel)
    assert [table.horizontalHeaderItem(i).text() for i in range(table.columnCount())] == list(COLUMNS)
    assert table.rowCount() == 1
    cells = [table.item(0, column).text() for column in range(table.columnCount())]
    assert cells == ["auth-refactor", "active", "2 h ago", "claude-code"]
    assert "1 session(s) from this computer's Meridian server" in status_of(panel)


def test_session_summary_is_never_shown(qtbot):
    local = FakeLocal(body=body_of([session()]))
    panel, _hosted = make_panel(qtbot, local=local)
    panel.show()
    settle(qtbot, panel)
    assert SECRET_SUMMARY not in all_visible_text(panel)


def test_empty_list_says_no_sessions_right_now(qtbot):
    panel, _hosted = make_panel(qtbot, local=FakeLocal(body=b"[]"))
    panel.show()
    settle(qtbot, panel)
    assert table_of(panel).rowCount() == 0
    assert "No sessions from this computer's Meridian server right now." in status_of(panel)


def test_unnamed_row_reads_as_unnamed(qtbot):
    panel, _hosted = make_panel(qtbot, local=FakeLocal(body=body_of([session(name="")])))
    panel.show()
    settle(qtbot, panel)
    assert table_of(panel).item(0, 0).text() == "(unnamed)"


def test_table_is_read_only_and_has_no_action_buttons(qtbot):
    panel, _hosted = make_panel(qtbot, local=FakeLocal(body=body_of([session()])))
    panel.show()
    settle(qtbot, panel)
    assert table_of(panel).editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers
    assert panel.findChildren(QPushButton) == []


def test_every_label_is_selectable(qtbot):
    panel, _hosted = make_panel(qtbot, local=FakeLocal(body=body_of([session()])))
    panel.show()
    settle(qtbot, panel)
    labels = panel.findChildren(QLabel)
    assert labels
    for label in labels:
        assert label.textInteractionFlags() & SELECTABLE_TEXT == SELECTABLE_TEXT, label.objectName()


def test_no_text_says_not_built_yet(qtbot):
    panel, _hosted = make_panel(qtbot, local=FakeLocal(error=LocalClientError("not_running", "x")))
    panel.show()
    settle(qtbot, panel)
    assert "not built yet" not in all_visible_text(panel).lower()


# ----------------------------------------------------------------- picker


def test_picker_lists_projects_and_selects_the_first(qtbot):
    local = FakeLocal(body=b"[]")
    panel, _hosted = make_panel(qtbot, local=local, projects=((PID, "Alpha"), (PID2, "Beta")))
    panel.show()
    settle(qtbot, panel)
    combo = picker_of(panel)
    assert [combo.itemText(i) for i in range(combo.count())] == ["Alpha", "Beta"]
    assert combo.currentData() == PID
    assert f"/projects/{PID}/sessions" in local.calls[0][1]


def test_switching_project_refetches_for_the_new_project(qtbot):
    local = FakeLocal(body=b"[]")
    panel, _hosted = make_panel(qtbot, local=local, projects=((PID, "Alpha"), (PID2, "Beta")))
    panel.show()
    settle(qtbot, panel)
    picker_of(panel).setCurrentIndex(1)
    qtbot.waitUntil(lambda: len(local.calls) == 2)
    settle(qtbot, panel)
    assert f"/projects/{PID2}/sessions" in local.calls[-1][1]


def test_no_projects_says_so_and_sends_nothing(qtbot):
    local = FakeLocal()
    panel, _hosted = make_panel(qtbot, local=local, projects=())
    panel.show()
    settle(qtbot, panel)
    assert status_of(panel) == NO_PROJECTS_TEXT
    assert local.calls == []


def test_a_failed_project_list_is_named_by_class_and_sends_nothing(qtbot):
    def broken():
        raise RuntimeError("db path with secret")

    local = FakeLocal()
    panel, _hosted = make_panel(qtbot, local=local, project_provider=broken)
    panel.show()
    settle(qtbot, panel)
    assert status_of(panel) == "Projects could not be read (RuntimeError)."
    assert "secret" not in all_visible_text(panel)
    assert local.calls == []


# ----------------------------------------------------------------- sources and typed states


def test_local_not_running_without_a_token_does_not_touch_hosted(qtbot):
    local = FakeLocal(error=LocalClientError("not_running", "x"))
    panel, hosted = make_panel(qtbot, local=local, hosted_value=(HOSTED, None))
    panel.show()
    settle(qtbot, panel)
    assert "not running on this computer" in status_of(panel)
    assert table_of(panel).rowCount() == 0
    assert hosted.calls == []


def test_local_not_running_with_a_token_reads_hosted_and_names_the_source(qtbot):
    local = FakeLocal(error=LocalClientError("not_running", "x"))
    hosted = FakeHosted(body=body_of([session(name="from-hosted")]))
    panel, hosted = make_panel(qtbot, local=local, hosted=hosted, hosted_value=(HOSTED, SENTINEL_TOKEN))
    panel.show()
    settle(qtbot, panel)
    assert len(hosted.calls) == 1
    request, _timeout = hosted.calls[0]
    assert request.full_url.startswith(HOSTED + "/projects/")
    assert request.get_header("Authorization") == f"Bearer {SENTINEL_TOKEN}"
    assert table_of(panel).item(0, 0).text() == "from-hosted"
    assert "1 session(s) from hosted Meridian" in status_of(panel)


def test_the_token_is_never_shown_in_any_visible_text(qtbot):
    local = FakeLocal(error=LocalClientError("not_running", "x"))
    hosted = FakeHosted(body=body_of([session()]))
    panel, _hosted = make_panel(qtbot, local=local, hosted=hosted, hosted_value=(HOSTED, SENTINEL_TOKEN))
    panel.show()
    settle(qtbot, panel)
    assert SENTINEL_TOKEN not in all_visible_text(panel)


def test_the_token_is_not_shown_when_hosted_refuses_it(qtbot):
    local = FakeLocal(error=LocalClientError("not_running", "x"))
    hosted = FakeHosted(status=401)
    panel, _hosted = make_panel(qtbot, local=local, hosted=hosted, hosted_value=(HOSTED, SENTINEL_TOKEN))
    panel.show()
    settle(qtbot, panel)
    assert "did not accept" in status_of(panel)
    assert SENTINEL_TOKEN not in all_visible_text(panel)


def test_a_non_loopback_local_address_is_refused_and_nothing_is_sent(qtbot):
    local = FakeLocal()
    hosted = FakeHosted()
    panel, hosted = make_panel(qtbot, local=local, hosted=hosted, local_url="http://10.0.0.5:7878",
                               hosted_value=(HOSTED, SENTINEL_TOKEN))
    panel.show()
    settle(qtbot, panel)
    assert local.calls == []
    assert hosted.calls == []
    assert "not on this computer" in status_of(panel)


def test_a_missing_route_shows_its_message_and_no_rows(qtbot):
    panel, _hosted = make_panel(qtbot, local=FakeLocal(status=404))
    panel.show()
    settle(qtbot, panel)
    assert table_of(panel).rowCount() == 0
    assert "no sessions route" in status_of(panel)


def test_a_failed_refresh_keeps_the_tab_usable(qtbot):
    local = FakeLocal(error=RuntimeError("socket blew up"))
    panel, _hosted = make_panel(qtbot, local=local)
    panel.show()
    settle(qtbot, panel)
    assert "could not be checked" in status_of(panel)
    assert "blew up" not in all_visible_text(panel)


# ----------------------------------------------------------------- stale results


def test_a_stale_result_is_dropped_when_a_newer_refresh_has_started(qtbot):
    gate = threading.Event()
    local = ScriptedLocal([body_of([session(name="old-run")]), body_of([session(name="new-run")])], gate=gate)
    panel, _hosted = make_panel(qtbot, local=local)
    panel.show()
    qtbot.waitUntil(lambda: len(local.paths) == 1)
    panel.refresh()
    qtbot.waitUntil(lambda: table_of(panel).rowCount() == 1 and table_of(panel).item(0, 0).text() == "new-run")
    gate.set()
    qtbot.wait(150)
    settle(qtbot, panel)
    assert table_of(panel).item(0, 0).text() == "new-run"


# ----------------------------------------------------------------- pure helpers


def test_choose_project_keeps_a_listed_selection_and_otherwise_takes_the_first():
    choices = ((PID, "Alpha"), (PID2, "Beta"))
    assert choose_project(PID2, choices) == PID2
    assert choose_project("gone", choices) == PID
    assert choose_project("", ()) == ""


def test_status_text_for_each_state():
    rows = (sc.SessionRow(name="a", status="active", last_seen="", client=""),)
    assert status_text(sc.SessionsResult(sc.SESSIONS, "", sc.SOURCE_LOCAL, rows), "12:00:00") == (
        "1 session(s) from this computer's Meridian server. Checked at 12:00:00."
    )
    assert status_text(sc.SessionsResult(sc.SESSIONS, "", sc.SOURCE_HOSTED), "12:00:00") == (
        "No sessions from hosted Meridian right now. Checked at 12:00:00."
    )
    assert status_text(sc.SessionsResult(sc.NO_PROJECT, "Pick one."), "12:00:00") == "Pick one."
    assert status_text(sc.SessionsResult(sc.ERROR, "Broken.", sc.SOURCE_LOCAL), "12:00:00") == (
        "Broken. Checked at 12:00:00."
    )


def test_row_cells_orders_the_four_columns_and_shows_an_age():
    now = datetime(2026, 10, 22, 12, 0, 0, tzinfo=timezone.utc)
    row = sc.SessionRow(name="", status="idle", last_seen="2026-10-22 11:55:00", client="codex")
    assert row_cells(row, now) == ("(unnamed)", "idle", "5 min ago", "codex")


def test_the_table_uses_single_row_selection(qtbot):
    panel, _hosted = make_panel(qtbot, local=FakeLocal())
    table = table_of(panel)
    assert table.selectionBehavior() == QAbstractItemView.SelectionBehavior.SelectRows
    assert table.selectionMode() == QAbstractItemView.SelectionMode.SingleSelection
    assert table.horizontalHeader().stretchLastSection()


def test_loading_text_is_shown_before_the_first_result(qtbot):
    panel, _hosted = make_panel(qtbot, local=FakeLocal())
    assert status_of(panel) == LOADING_TEXT


@pytest.mark.parametrize("base", [None, "", "https://example.com"])
def test_bad_local_address_never_makes_a_request(qtbot, base):
    local = FakeLocal()
    panel, _hosted = make_panel(qtbot, local=local, local_url=base)
    panel.show()
    settle(qtbot, panel)
    assert local.calls == []
