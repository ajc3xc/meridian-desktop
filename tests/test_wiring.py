"""Wiring tests for the Sessions and Tools tabs and the update notice. Offscreen Qt, pytest-qt, fakes only.

No test reaches the network, the OS keychain or the core. Each update check gets a fake transport that records its
URLs, and each opener is a fake that records the URLs it is given. The Sessions reads are fakes at the module seam
(sessions_panel.local_sessions and sessions_panel.hosted_sessions), so no socket is opened. The Tools read is a fake
status provider, so no core process starts.
"""

import gc
import json
import threading
import weakref
from datetime import datetime

import pytest
import shiboken6
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QComboBox, QFrame, QLabel, QPushButton, QTableWidget, QWidget

from _fakes import MemoryKeyring
from _shared_delivery import recording_deliveries, visible_state
from meridian_desktop import sessions_client, updater
from meridian_desktop.data_source import DataSources
from meridian_desktop.local_client import HEALTH_NOT_RUNNING
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.token_store import HOSTED_TOKEN_ACCOUNT, TOKEN_SERVICE
from meridian_desktop.tools_status import ENTRY_IN_PLACE_TEXT, UNCHECKED, YES, ToolRow, ToolsStatus
from meridian_desktop.ui import sessions_panel
from meridian_desktop.ui.main_window import MainWindow
from meridian_desktop.ui.sessions_panel import SessionsPanel
from meridian_desktop.ui.update_notice import UpdateNoticeBar, notice_text
from meridian_desktop.version import APP_VERSION

EXPECTED_TABS = ("Home", "Projects", "Sessions", "Search", "Settings")
LOCAL = "http://127.0.0.1:7999"
HOSTED = "https://usemeridian.us"
PID = "5787cc92-ba7d-4788-b17c-28ab7938b839"
PROJECT_ROW = {
    "id": PID,
    "name": "Alpha",
    "status": "active",
    "priority": "P1",
    "created_at": "2026-01-05 09:00:00",
    "executor_config": "EXEC-SECRET-WIRING",
    "agent_instructions": "AGENT-NOTE-WIRING",
}
FAKE_TOKEN = "sk_meridian_fake-wiring-0042"
NEWER = "99.0.0"
NEWER_TAG = f"v{NEWER}"
CLOCK = datetime(2026, 10, 22, 9, 30, 0)


class FakeTransport:
    """A transport for the release JSON. It answers with one tag, or raises. It records each URL it is asked for."""

    def __init__(self, tag=None, error=None):
        self.tag = tag
        self.error = error
        self.urls = []

    def __call__(self, url, max_bytes, dest):
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        body = json.dumps({"tag_name": self.tag, "draft": False, "assets": []}).encode("utf-8")
        return updater.Reply(200, {}, body)


class FakeOpener:
    """Stands in for QDesktopServices.openUrl. It records each URL it is asked to open."""

    def __init__(self):
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        return True


def _unused(*args):
    raise AssertionError("this data source should not be read in this test")


def _sources(rows=()):
    """Fake data sources: the local server is not running, and the database file returns the given rows."""
    return DataSources(
        probe=lambda url: HEALTH_NOT_RUNNING,
        read_server=_unused,
        read_file=lambda path: [dict(row) for row in rows],
        get_token=lambda: None,
        list_hosted=_unused,
    )


def _no_tools() -> ToolsStatus:
    return ToolsStatus(rows=())


def _window(qtbot, settings_path, *, sources=None, secrets=None, tools=None, transport=None, shown=False):
    window = MainWindow(
        SettingsStore(settings_path),
        secret_store=secrets if secrets is not None else KeyringSecretStore(backend=MemoryKeyring()),
        data_sources=sources if sources is not None else _sources(),
        tools_status=tools if tools is not None else _no_tools,
        update_transport=transport,
    )
    qtbot.addWidget(window)
    if shown:
        window.show()
        qtbot.waitExposed(window)
    return window


def _tab_names(window):
    return tuple(window.tabs.tabText(index) for index in range(window.tabs.count()))


def _page(window, name):
    return window.tabs.widget(_tab_names(window).index(name))


def _select(window, name):
    window.tabs.setCurrentIndex(_tab_names(window).index(name))


def _label_texts(widget):
    return [label.text() for label in widget.findChildren(QLabel)]


def _all_text(widget):
    """Every string a user can read on this widget: labels, combo items and table cells."""
    chunks = _label_texts(widget)
    for combo in widget.findChildren(QComboBox):
        chunks.extend(combo.itemText(index) for index in range(combo.count()))
    for table in widget.findChildren(QTableWidget):
        for row in range(table.rowCount()):
            for column in range(table.columnCount()):
                item = table.item(row, column)
                if item is not None:
                    chunks.append(item.text())
    return "\n".join(chunks)


def _bar(window):
    bar = window.findChild(UpdateNoticeBar, "update-notice")
    assert bar is not None
    return bar


# ----------------------------------------------------------- tabs


def test_sessions_sits_after_projects_and_tools_is_no_longer_a_tab(qtbot, settings_path):
    window = _window(qtbot, settings_path)
    assert _tab_names(window) == EXPECTED_TABS

    sessions = _page(window, "Sessions")
    assert isinstance(sessions, SessionsPanel)
    assert sessions.objectName() == "sessions-panel"
    assert not any("Not built yet" in text for text in _label_texts(sessions))
    # The Tools panel is no longer a tab. Home's AI clients card shows its rows, and Settings has the full table.
    assert "Tools" not in _tab_names(window)


def test_sessions_tab_uses_its_providers_and_never_shows_the_token(qtbot, settings_path, tmp_path, monkeypatch):
    db = tmp_path / "meridian.db"
    db.write_bytes(b"")
    SettingsStore(settings_path).save(Settings(mode="local", local_url=LOCAL, hosted_url=HOSTED, db_path=str(db)))
    secrets = KeyringSecretStore(backend=MemoryKeyring())
    secrets.set_secret(TOKEN_SERVICE, HOSTED_TOKEN_ACCOUNT, FAKE_TOKEN)
    local_calls = []
    hosted_calls = []

    def fake_local(base_url, project_id, *, transport=None, timeout=None):
        local_calls.append((base_url, project_id))
        return sessions_client.SessionsResult(
            sessions_client.NOT_RUNNING, sessions_client.NOT_RUNNING_MESSAGE, sessions_client.SOURCE_LOCAL
        )

    def fake_hosted(base_url, project_id, token, *, transport=None, timeout=None):
        hosted_calls.append((base_url, project_id, token))
        row = sessions_client.SessionRow(name="auth-refactor", status="active", last_seen="", client="claude-code")
        return sessions_client.SessionsResult(
            sessions_client.SESSIONS, "", sessions_client.SOURCE_HOSTED, (row,)
        )

    monkeypatch.setattr(sessions_panel, "local_sessions", fake_local)
    monkeypatch.setattr(sessions_panel, "hosted_sessions", fake_hosted)
    window = _window(qtbot, settings_path, sources=_sources([PROJECT_ROW]), secrets=secrets, shown=True)

    # Showing the window reads no session: the Sessions tab is not the one on screen.
    assert local_calls == []
    assert hosted_calls == []

    _select(window, "Sessions")
    page = _page(window, "Sessions")
    table = page.findChild(QTableWidget, "sessions-table")
    qtbot.waitUntil(lambda: table.rowCount() == 1, timeout=5000)

    # The local URL comes from Settings, and the project id from the Projects resolver. The hosted fallback
    # gets the Settings URL and the token from the store. The list may refresh while the test runs, so
    # every recorded call must match.
    assert set(local_calls) == {(LOCAL, PID)}
    assert set(hosted_calls) == {(HOSTED, PID, FAKE_TOKEN)}
    picker = page.findChild(QComboBox, "sessions-project-picker")
    assert picker.itemText(0) == "Alpha"
    assert picker.itemData(0) == PID
    assert table.item(0, 0).text() == "auth-refactor"
    assert page.findChild(QLabel, "sessions-status").text().startswith("1 session(s) from hosted Meridian.")

    # The token reaches the client only. No label, picker entry or table cell shows it, and no project field
    # the Projects tab never shows reaches the page either.
    text = _all_text(window)
    assert FAKE_TOKEN not in text
    assert "EXEC-SECRET-WIRING" not in text
    assert "AGENT-NOTE-WIRING" not in text


def test_sessions_timer_in_hosted_mode_makes_no_sessions_call_and_refresh_is_there(
    qtbot, settings_path, monkeypatch
):
    SettingsStore(settings_path).save(Settings(mode="hosted", local_url=LOCAL, hosted_url=HOSTED))
    secrets = KeyringSecretStore(backend=MemoryKeyring())
    secrets.set_secret(TOKEN_SERVICE, HOSTED_TOKEN_ACCOUNT, FAKE_TOKEN)
    local_calls = []
    hosted_calls = []

    def fake_local(base_url, project_id, *, transport=None, timeout=None):
        local_calls.append(project_id)
        return sessions_client.SessionsResult(
            sessions_client.NOT_RUNNING, sessions_client.NOT_RUNNING_MESSAGE, sessions_client.SOURCE_LOCAL
        )

    def fake_hosted(base_url, project_id, token, *, transport=None, timeout=None):
        hosted_calls.append((base_url, project_id))
        row = sessions_client.SessionRow(name="auth-refactor", status="active", last_seen="", client="claude-code")
        return sessions_client.SessionsResult(sessions_client.SESSIONS, "", sessions_client.SOURCE_HOSTED, (row,))

    monkeypatch.setattr(sessions_panel, "local_sessions", fake_local)
    monkeypatch.setattr(sessions_panel, "hosted_sessions", fake_hosted)
    hosted_sources = DataSources(
        probe=lambda url: HEALTH_NOT_RUNNING,
        read_server=_unused,
        read_file=_unused,
        get_token=lambda: FAKE_TOKEN,
        list_hosted=lambda url, token: [dict(PROJECT_ROW)],
    )
    window = _window(qtbot, settings_path, sources=hosted_sources, secrets=secrets, shown=True)
    _select(window, "Sessions")
    page = _page(window, "Sessions")
    table = page.findChild(QTableWidget, "sessions-table")
    qtbot.waitUntil(lambda: table.rowCount() == 1, timeout=5000)
    assert page.findChild(QPushButton, "sessions-refresh") is not None
    assert hosted_calls == [(HOSTED, PID)]  # the first show read the hosted sessions once

    reads = (len(local_calls), len(hosted_calls))
    for _ in range(3):
        page._timer.timeout.emit()  # noqa: SLF001 - the timer's real connection
        qtbot.waitUntil(lambda: page._in_flight is None, timeout=5000)  # noqa: SLF001
    assert (len(local_calls), len(hosted_calls)) == reads  # three hosted-mode ticks read nothing
    assert table.rowCount() == 1


# ----------------------------------------------------------- Tools


def test_home_tools_card_shows_rows_from_a_fake_status_after_boot(qtbot, settings_path):
    rows = (
        ToolRow("Codex", "meridian-docs", YES, UNCHECKED, ENTRY_IN_PLACE_TEXT),
        ToolRow(
            "This computer",
            "Local Meridian server",
            YES,
            YES,
            "The local Meridian server answers on this computer.",
        ),
    )
    reads = []

    def fake_status():
        reads.append(1)
        return ToolsStatus(rows=rows, message="Checked. No tool is started or stopped.", checked_at=CLOCK)

    window = _window(qtbot, settings_path, tools=fake_status, shown=True)
    # Showing the window runs the tools check once, on its own. No tab has to be opened for it.
    qtbot.waitUntil(lambda: reads == [1], timeout=5000)
    card = window.findChild(QFrame, "home-card-tools")
    assert card is not None
    qtbot.waitUntil(lambda: "Codex: meridian-docs." in _all_text(card), timeout=5000)
    assert "Checking the tools" not in _all_text(card)
    assert "This computer: Local Meridian server. Live." in _all_text(card)
    assert reads == [1]


# ----------------------------------------------------------- update notice


def test_notice_shows_for_a_newer_release(qtbot, settings_path):
    transport = FakeTransport(tag=NEWER_TAG)
    window = _window(qtbot, settings_path, transport=transport, shown=True)
    bar = _bar(window)
    qtbot.waitUntil(lambda: bar.check_finished, timeout=5000)

    assert transport.urls == [updater.LATEST_RELEASE_URL]
    assert bar.isVisible()
    assert bar.findChild(QLabel, "update-notice-label").text() == f"Update available: v{NEWER}"
    assert bar.findChild(QPushButton, "update-notice-open").isVisible()


NO_NOTICE_CASES = {
    "up_to_date": lambda: FakeTransport(tag=f"v{APP_VERSION}"),
    "offline": lambda: FakeTransport(error=OSError("no route to host")),
    "refused": lambda: FakeTransport(tag="latest"),
}


@pytest.mark.parametrize("case", sorted(NO_NOTICE_CASES))
def test_notice_stays_hidden_when_up_to_date_offline_or_refused(qtbot, settings_path, case):
    transport = NO_NOTICE_CASES[case]()
    window = _window(qtbot, settings_path, transport=transport, shown=True)
    bar = _bar(window)
    qtbot.waitUntil(lambda: bar.check_finished, timeout=5000)

    assert transport.urls == [updater.LATEST_RELEASE_URL]
    assert not bar.isVisible()
    assert bar.findChild(QLabel, "update-notice-label").text() == ""
    assert bar.release_page_url == ""


def test_release_page_opens_only_when_the_button_is_clicked(qtbot):
    transport = FakeTransport(tag=NEWER_TAG)
    opener = FakeOpener()
    bar = UpdateNoticeBar(APP_VERSION, transport, opener=opener)
    qtbot.addWidget(bar)
    bar.start(enabled=True)
    qtbot.waitUntil(lambda: bar.check_finished, timeout=5000)
    assert not bar.isHidden()

    # Finding the release opens nothing. Only the button does.
    assert opener.urls == []
    bar.findChild(QPushButton, "update-notice-open").click()
    assert opener.urls == [updater.release_page_url_for(NEWER)]


def test_no_request_is_made_when_check_updates_is_off(qtbot, settings_path):
    SettingsStore(settings_path).save(Settings(check_updates=False))
    transport = FakeTransport(tag=NEWER_TAG)
    window = _window(qtbot, settings_path, transport=transport, shown=True)
    bar = _bar(window)
    qtbot.wait(100)

    assert transport.urls == []
    assert not bar.check_finished
    assert not bar.isVisible()


def test_the_check_runs_once_per_window(qtbot, settings_path):
    transport = FakeTransport(tag=f"v{APP_VERSION}")
    window = _window(qtbot, settings_path, transport=transport)
    bar = _bar(window)
    qtbot.waitUntil(lambda: bar.check_finished, timeout=5000)

    bar.start(enabled=True)
    bar.start(enabled=True)
    qtbot.wait(50)
    assert transport.urls == [updater.LATEST_RELEASE_URL]


def test_no_transport_means_no_check_and_no_notice(qtbot, settings_path):
    window = _window(qtbot, settings_path)
    bar = _bar(window)
    qtbot.wait(50)

    assert not bar.check_finished
    assert bar.isHidden()


def test_a_notice_from_another_bar_is_dropped(qtbot):
    """Another bar's newer-release notice never reaches this bar's label, its button or its release page.

    Both bars listen on the shared delivery object, so the other bar's notice reaches this one too. The bar keeps no
    generation, because its check runs once, so the owner check is the only guard that applies here. This bar keeps the
    notice of its own check, and the other bar's notice, for a different release, is dropped.
    """
    other_release = "98.0.0"
    own = UpdateNoticeBar(APP_VERSION, FakeTransport(tag=NEWER_TAG))
    qtbot.addWidget(own)
    other = UpdateNoticeBar(APP_VERSION, FakeTransport(tag=f"v{other_release}"))
    qtbot.addWidget(other)
    with recording_deliveries() as emitted:
        own.start(enabled=True)
        qtbot.waitUntil(lambda: own.check_finished, timeout=5000)
        assert len(emitted) == 1
        shown = visible_state(own)
        other.start(enabled=True)
        qtbot.waitUntil(lambda: len(emitted) == 2, timeout=5000)  # wait on the emission, not on the other bar's state
        qtbot.wait(20)  # the other bar's notice has reached every bar that listens on the shared object
    assert own.release_page_url == updater.release_page_url_for(NEWER)
    assert own.findChild(QLabel, "update-notice-label").text() == notice_text(NEWER)
    assert visible_state(own) == shown


def test_a_check_still_running_when_its_bar_is_deleted_does_not_keep_the_bar(qtbot):
    """A bar deleted while its check still runs is freed on the GUI thread, because the check holds no bar.

    The check emits to the shared delivery object, never to the bar's own signal, so a worker never touches a bar
    that the GUI thread may have deleted. This is a guard: a check that captured the bar would keep it alive here.
    The collector is off, so only reference counting can free the bar. The transport answers only after the gate.
    """
    entered = threading.Event()
    gate = threading.Event()
    answer = FakeTransport(tag=NEWER_TAG)

    def held_transport(url, max_bytes, dest):
        entered.set()
        gate.wait(5)
        return answer(url, max_bytes, dest)

    gc.disable()
    try:
        container = QWidget()
        bar = UpdateNoticeBar(APP_VERSION, held_transport, parent=container)
        bar.start(enabled=True)
        assert entered.wait(5)  # the check has started and is held at the gate
        ref = weakref.ref(bar)
        shiboken6.delete(container)  # the bar is deleted on the GUI thread while the check still runs
        del bar
        assert ref() is None
        gate.set()
        assert QThreadPool.globalInstance().waitForDone(5000)
        qtbot.wait(50)  # the late result reaches the event loop, and nothing raises
    finally:
        gate.set()
        gc.enable()
