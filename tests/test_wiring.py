"""Wiring tests for the Sessions and Tools tabs and the update notice. Offscreen Qt, pytest-qt, fakes only.

No test reaches the network, the OS keychain or the core. Each update check gets a fake transport that records its
URLs, and each opener is a fake that records the URLs it is given. The Sessions reads are fakes at the module seam
(sessions_panel.local_sessions and sessions_panel.hosted_sessions), so no socket is opened. The Tools read is a fake
status provider, so no core process starts.
"""

import json
from datetime import datetime

import pytest
from PySide6.QtWidgets import QComboBox, QLabel, QPushButton, QTableWidget

from _fakes import MemoryKeyring
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
from meridian_desktop.ui.tools_panel import ToolsPanel
from meridian_desktop.ui.update_notice import UpdateNoticeBar
from meridian_desktop.version import APP_VERSION

EXPECTED_TABS = (
    "Projects", "Sessions", "Tools", "Writing", "Zotero", "Tunnel", "Services", "Code", "Outputs", "Research", "Logs",
    "Settings",
)
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


def test_sessions_and_tools_tabs_sit_after_projects(qtbot, settings_path):
    window = _window(qtbot, settings_path)
    assert _tab_names(window) == EXPECTED_TABS

    sessions = _page(window, "Sessions")
    tools = _page(window, "Tools")
    assert isinstance(sessions, SessionsPanel)
    assert isinstance(tools, ToolsPanel)
    assert sessions.objectName() == "sessions-panel"
    assert tools.objectName() == "tools-panel"
    for page in (sessions, tools):
        assert not any("Not built yet" in text for text in _label_texts(page))


# ----------------------------------------------------------- Sessions


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


# ----------------------------------------------------------- Tools


def test_tools_tab_shows_rows_from_a_fake_status(qtbot, settings_path):
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
    assert reads == []  # showing the window does not read the tools

    _select(window, "Tools")
    page = _page(window, "Tools")
    table = page.findChild(QTableWidget, "tools-table")
    qtbot.waitUntil(lambda: table.rowCount() == len(rows), timeout=5000)
    for index, row in enumerate(rows):
        cells = tuple(table.item(index, column).text() for column in range(table.columnCount()))
        assert cells == row.cells()
    assert reads == [1]
    assert "Checked. No tool is started or stopped." in page.findChild(QLabel, "tools-status").text()


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
