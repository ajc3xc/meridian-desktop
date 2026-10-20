"""Smoke tests for the window and its tabs. Offscreen Qt, no network.

The Projects tab reads through fake data sources here, so no test probes a server, opens
the real home directory or reads the keychain. The one real database is a temp file.
"""

import dataclasses
import sqlite3
from pathlib import Path

import pytest
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QToolButton,
)

from _fakes import MemoryKeyring
from meridian_desktop import __main__ as entry
from meridian_desktop import local_client, projects_reader, settings_store
from meridian_desktop.data_source import (
    NO_LOCAL_DATA_MESSAGE,
    NOT_SIGNED_IN_MESSAGE,
    DataSources,
)
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.token_store import HOSTED_TOKEN_ACCOUNT, TOKEN_SERVICE
from meridian_desktop.ui.latex_panel import LatexPanel
from meridian_desktop.ui.logs_page import INTRO_UNREAD_TEXT, LogsPage
from meridian_desktop.ui.main_window import TAB_NAMES, WRITING_SECTIONS, MainWindow
from meridian_desktop.ui.projects_tab import default_sources
from meridian_desktop.ui.services_page import NOT_CHECKED, UNCHECKED_TEXT, ServicesPage
from meridian_desktop.ui.settings_tab import (
    DB_SAVED_TEXT,
    LOCAL_URL_SAVED_TEXT,
    NOT_CHOSEN_TEXT,
    NOT_WRITTEN_TEXT,
    SettingsTab,
    mode_text,
)
from meridian_desktop.ui.tunnel_page import NOT_AVAILABLE_TEXT, TunnelPage

EXPECTED_TABS = (
    "Projects", "Writing", "Zotero", "Tunnel", "Services", "Code", "Outputs", "Research", "Logs", "Settings",
)
# The tabs that are still placeholders, with what each one says is missing. Only Writing is left:
# its DOCX and Google Docs sections are not built. Zotero, Code, Outputs and Research are working tabs.
EXPECTED_PLACEHOLDERS = {
    "Writing": "DOCX and Google Docs writing are not in this build yet. The LaTeX section below works.",
}
# The wave-3 tabs, each with its page's object name and class.
WAVE3_PAGES = {
    "Tunnel": ("tunnel-page", TunnelPage),
    "Services": ("services-page", ServicesPage),
    "Logs": ("logs-page", LogsPage),
}
STATUS = "No services checked yet. Press Check services on the Services tab."

FAKE_TOKEN = "sk_meridian_fake-projects-tab"

# The core projects table. executor_config and agent_instructions are present so a test can
# prove the Projects tab never shows them. Inserted oldest first, so the expected order differs.
PROJECT_SCHEMA = """
CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    creator_human_id TEXT,
    icon TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    priority TEXT NOT NULL DEFAULT 'P2',
    parent_project_id TEXT,
    created_at TEXT NOT NULL,
    executor_config TEXT,
    agent_instructions TEXT
)
"""
PROJECT_ROWS = [
    ("p-old", "Oldest", "human-1", "book", "active", "P2", None,
     "2026-01-05 09:00:00", "EXEC-SECRET-OLD", "AGENT-NOTE-OLD"),
    ("p-new", "Newest", "human-1", "rocket", "parked", "P0", "p-old",
     "2026-03-02 17:30:00", "EXEC-SECRET-NEW", "AGENT-NOTE-NEW"),
    ("p-mid", "Middle", None, None, "archived", "P1", None,
     "2026-02-14 12:00:00", "EXEC-SECRET-MID", "AGENT-NOTE-MID"),
]
WRITE_CONTROLS = (
    QPushButton,
    QToolButton,
    QCheckBox,
    QRadioButton,
    QLineEdit,
    QComboBox,
    QAbstractSpinBox,
)


def _unexpected(*args):
    raise AssertionError("this data source should not be read in this test")


def _sources(*, probe_state=local_client.HEALTH_NOT_RUNNING, token=None, read_file=None, list_hosted=None):
    """Fake data sources. The probe answers from probe_state. Nothing here touches the network or the disk."""
    return DataSources(
        probe=lambda url: probe_state,
        read_server=_unexpected,
        read_file=read_file or projects_reader.list_projects,
        get_token=lambda: token,
        list_hosted=list_hosted or _unexpected,
    )


def _window(qtbot, settings_path, sources=None):
    window = MainWindow(SettingsStore(settings_path), data_sources=sources if sources is not None else _sources())
    qtbot.addWidget(window)
    return window


def _shown_window(qtbot, settings_path, sources=None):
    window = _window(qtbot, settings_path, sources)
    window.show()
    qtbot.waitExposed(window)
    return window


def _projects_db(tmp_path) -> Path:
    path = tmp_path / "meridian.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(PROJECT_SCHEMA)
        conn.executemany("INSERT INTO projects VALUES (?,?,?,?,?,?,?,?,?,?)", PROJECT_ROWS)
        conn.commit()
    finally:
        conn.close()
    return path


def _empty_db(tmp_path) -> Path:
    """A file at a database path. A fake reader stands in for it, so its bytes do not matter."""
    path = tmp_path / "meridian.db"
    path.write_bytes(b"")
    return path


def _all_text(widget):
    """Every string a user can read on this widget: labels, table headers and table cells."""
    chunks = [label.text() for label in widget.findChildren(QLabel)]
    for table in widget.findChildren(QTableWidget):
        for column in range(table.columnCount()):
            header = table.horizontalHeaderItem(column)
            if header is not None:
                chunks.append(header.text())
        for row in range(table.rowCount()):
            for column in range(table.columnCount()):
                item = table.item(row, column)
                if item is not None:
                    chunks.append(item.text())
    return "\n".join(chunks)


def _tab_names(window):
    return tuple(window.tabs.tabText(i) for i in range(window.tabs.count()))


def _page(window, name):
    return window.tabs.widget(_tab_names(window).index(name))


def _label_texts(widget):
    return [label.text() for label in widget.findChildren(QLabel)]


def test_tab_names_and_order(qtbot, settings_path):
    window = _window(qtbot, settings_path)
    assert _tab_names(window) == EXPECTED_TABS
    assert TAB_NAMES == EXPECTED_TABS
    assert entry.TAB_NAMES == EXPECTED_TABS


def test_placeholders_say_what_is_missing(qtbot, settings_path):
    window = _window(qtbot, settings_path)
    for name, text in EXPECTED_PLACEHOLDERS.items():
        assert text in _label_texts(_page(window, name)), name
        assert not any("Not built yet" in label for label in _label_texts(_page(window, name))), name


def test_wave3_tabs_replace_placeholders_and_keep_others(qtbot, settings_path, monkeypatch):
    # The health probe records its calls, so the test can show that showing the window makes none.
    health_calls = []

    def recording_probe(url, timeout=None):
        health_calls.append(url)
        return local_client.HEALTH_NOT_RUNNING

    monkeypatch.setattr(local_client, "probe_health", recording_probe)
    window = _shown_window(qtbot, settings_path)
    assert _tab_names(window) == EXPECTED_TABS

    # Tunnel, Services and Logs host their own pages. No placeholder and no wave-3 text is left on them.
    for name, (object_name, page_type) in WAVE3_PAGES.items():
        page = _page(window, name)
        assert isinstance(page, page_type), name
        assert page.objectName() == object_name, name
        assert "Not built yet (wave 3)" not in _all_text(page), name
        assert not any("Not built yet" in text for text in _label_texts(page)), name

    # Tunnel says the slot states are not available, and shows no rows. This build has no slot-state source.
    tunnel = _page(window, "Tunnel")
    assert tunnel.findChild(QLabel, "tunnel-status").text() == NOT_AVAILABLE_TEXT
    assert tunnel.findChild(QTableWidget, "tunnel-slot-table").rowCount() == 0

    # Services and Logs show their unchecked state. Showing the window made no health request and checked no port.
    services = _page(window, "Services")
    assert services.findChild(QLabel, "services-intro").text() == UNCHECKED_TEXT
    assert f"Local server health: {NOT_CHECKED}" in _label_texts(services)
    assert services.findChild(QLabel, "services-ports-status").text() == f"Slot ports: {NOT_CHECKED}"
    assert services.findChild(QTableWidget, "services-ports-table").rowCount() == 0
    assert health_calls == []

    logs = _page(window, "Logs")
    assert logs.findChild(QLabel, "logs-intro").text() == INTRO_UNREAD_TEXT
    assert logs.findChild(QPlainTextEdit, "logs-tunnel-text").toPlainText() == ""
    assert logs.findChild(QPlainTextEdit, "logs-runner-text").toPlainText() == ""

    # The other tabs keep their state. Projects and Settings are built, Zotero is a working panel,
    # Writing keeps its LaTeX section, and Code is still a placeholder that says what is missing.
    assert _page(window, "Projects").objectName() == "projects-tab"
    assert _page(window, "Settings").objectName() == "runpod-panel"
    writing = _page(window, "Writing")
    assert writing.findChild(LatexPanel) is not None
    for section in WRITING_SECTIONS:
        assert section in _label_texts(writing)
    for name, text in EXPECTED_PLACEHOLDERS.items():
        page = _page(window, name)
        assert page.objectName() == f"placeholder-{name.lower()}", name
        assert text in _label_texts(page), name


def test_settings_tab_holds_runpod_panel_not_placeholder(qtbot, settings_path):
    window = _window(qtbot, settings_path)
    page = _page(window, "Settings")
    assert page.objectName() == "runpod-panel"
    assert not any("Not built yet" in text for text in _label_texts(page))


def test_writing_lists_three_named_sections(qtbot, settings_path):
    window = _window(qtbot, settings_path)
    assert WRITING_SECTIONS == (
        "DOCX (meridian-docs)",
        "LaTeX (local engine on 127.0.0.1:8471)",
        "Google Docs (approach not decided)",
    )
    texts = _label_texts(_page(window, "Writing"))
    for section in WRITING_SECTIONS:
        assert section in texts


def test_mode_selector_offers_hosted_and_local(qtbot, settings_path):
    window = _window(qtbot, settings_path)
    combo = window.mode_selector
    assert [combo.itemData(i) for i in range(combo.count())] == ["hosted", "local"]


def test_mode_selector_writes_to_temp_settings_path(qtbot, settings_path):
    window = _window(qtbot, settings_path)
    combo = window.mode_selector
    assert combo.currentData() == "local"
    assert not settings_path.exists()

    combo.setCurrentIndex(combo.findData("hosted"))

    assert settings_path.is_file()
    assert SettingsStore(settings_path).load().settings.mode == "hosted"


def test_mode_selector_reads_saved_mode(qtbot, settings_path):
    SettingsStore(settings_path).set_mode("hosted")
    window = _window(qtbot, settings_path)
    assert window.mode_selector.currentData() == "hosted"


def test_failed_mode_save_reverts_selector(qtbot, settings_path, monkeypatch):
    SettingsStore(settings_path).set_mode("hosted")
    before = settings_path.read_bytes()
    window = _window(qtbot, settings_path)
    window.show()
    qtbot.waitExposed(window)
    combo = window.mode_selector
    assert combo.currentData() == "hosted"

    attempts = []
    real_save = SettingsStore.save

    def counting_save(self, settings):
        attempts.append(settings.mode)
        return real_save(self, settings)

    def failing_replace(src, dst):
        # The last step of the atomic save. Failing here leaves settings.json as it was.
        raise OSError("simulated disk failure")

    monkeypatch.setattr(SettingsStore, "save", counting_save)
    monkeypatch.setattr(settings_store.os, "replace", failing_replace)

    combo.setCurrentIndex(combo.findData("local"))

    # One attempt for the user's change. The revert must not trigger a second save.
    assert attempts == ["local"]
    assert combo.currentData() == "hosted"
    assert "could not save mode" in window.settings_warning.text()
    assert window.settings_warning.isVisible()
    assert settings_path.read_bytes() == before
    assert sorted(p.name for p in settings_path.parent.iterdir()) == ["settings.json"]


def test_status_bar_text(qtbot, settings_path):
    window = _window(qtbot, settings_path)
    assert window.statusBar().currentMessage() == STATUS


def test_smoke_flag_returns_zero_and_prints_ok(settings_path, capsys):
    code = entry.main(["--smoke"], settings_path=settings_path)
    assert code == 0
    assert "smoke ok" in capsys.readouterr().out


# ----------------------------------------------------------- Projects tab (read-only)


def test_projects_tab_is_built_not_a_placeholder(qtbot, settings_path):
    window = _window(qtbot, settings_path)
    page = _page(window, "Projects")
    assert page.objectName() == "projects-tab"
    assert not any("Not built yet" in text for text in _label_texts(page))


def test_projects_tab_lists_rows_and_has_no_write_controls(qtbot, settings_path, tmp_path):
    db = _projects_db(tmp_path)
    SettingsStore(settings_path).save(Settings(mode="local", db_path=str(db)))
    window = _shown_window(qtbot, settings_path)
    page = _page(window, "Projects")
    table = page.findChild(QTableWidget, "projects-table")
    detail = page.findChild(QLabel, "projects-detail")

    qtbot.waitUntil(lambda: table.rowCount() == 3, timeout=5000)
    headers = tuple(table.horizontalHeaderItem(column).text() for column in range(table.columnCount()))
    assert headers == ("Name", "Status", "Priority", "Created at")
    # Newest first, as the reader orders the rows.
    assert [table.item(row, 0).text() for row in range(3)] == ["Newest", "Middle", "Oldest"]
    assert [table.item(0, column).text() for column in range(1, 4)] == ["parked", "P0", "2026-03-02 17:30:00"]

    # The first row is selected on load, so its detail shows.
    qtbot.waitUntil(lambda: "Name: Newest" in detail.text(), timeout=5000)
    assert "ID: p-new" in detail.text()
    assert "Icon: rocket" in detail.text()
    assert "Parent project: p-old" in detail.text()
    # Selecting another row changes the detail, and an empty field reads as (none).
    table.selectRow(1)
    qtbot.waitUntil(lambda: "Name: Middle" in detail.text(), timeout=5000)
    assert "Parent project: (none)" in detail.text()

    # No write control on the tab, and the table cannot be edited.
    for widget_type in WRITE_CONTROLS:
        assert page.findChildren(widget_type) == [], widget_type.__name__
    assert table.editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers

    # The fields the desktop never reads do not appear anywhere on the tab.
    text = _all_text(page) + "\n" + detail.text()
    assert "EXEC-SECRET" not in text
    assert "AGENT-NOTE" not in text
    assert "executor_config" not in text
    assert "agent_instructions" not in text


def _raise_os_error(path):
    raise OSError(f"cannot open {path}")


@pytest.mark.parametrize("case", ["no_local_data", "not_signed_in", "read_failed", "no_rows"])
def test_each_resolver_state_shows_its_message(qtbot, settings_path, tmp_path, case):
    db = _empty_db(tmp_path)
    if case == "no_local_data":
        SettingsStore(settings_path).save(Settings(mode="local"))
        sources, expected = _sources(), NO_LOCAL_DATA_MESSAGE
    elif case == "not_signed_in":
        SettingsStore(settings_path).save(Settings(mode="hosted"))
        sources, expected = _sources(token=None), NOT_SIGNED_IN_MESSAGE
    elif case == "read_failed":
        SettingsStore(settings_path).save(Settings(mode="local", db_path=str(db)))
        sources, expected = _sources(read_file=_raise_os_error), "The database file could not be read (OSError)."
    else:
        SettingsStore(settings_path).save(Settings(mode="local", db_path=str(db)))
        sources, expected = _sources(read_file=lambda path: []), "No projects found in the database file."
    window = _shown_window(qtbot, settings_path, sources)
    page = _page(window, "Projects")
    status = page.findChild(QLabel, "projects-status")

    qtbot.waitUntil(lambda: status.text() == expected, timeout=5000)
    assert page.findChild(QTableWidget, "projects-table").rowCount() == 0
    # The message never names the database path.
    assert str(db) not in status.text()


def test_hosted_rows_show_without_the_token_or_hidden_fields(qtbot, settings_path):
    SettingsStore(settings_path).save(Settings(mode="hosted"))
    hosted_rows = [
        {
            "id": "h-1",
            "name": "Hosted one",
            "status": "active",
            "priority": "P1",
            "created_at": "2026-05-01 10:00:00",
            "executor_config": "EXEC-SECRET-HOSTED",
            "agent_instructions": "AGENT-NOTE-HOSTED",
        }
    ]
    received = []

    def list_hosted(url, token):
        received.append((url, token))
        return hosted_rows

    window = _shown_window(qtbot, settings_path, _sources(token=FAKE_TOKEN, list_hosted=list_hosted))
    page = _page(window, "Projects")
    table = page.findChild(QTableWidget, "projects-table")
    detail = page.findChild(QLabel, "projects-detail")
    status = page.findChild(QLabel, "projects-status")

    qtbot.waitUntil(lambda: table.rowCount() == 1, timeout=5000)
    assert status.text() == "Showing 1 project(s) from hosted Meridian."
    # The token goes to the hosted client only. It never reaches the tab.
    assert received == [(settings_store.DEFAULT_HOSTED_URL, FAKE_TOKEN)]
    text = _all_text(page) + "\n" + detail.text() + "\n" + status.text()
    assert FAKE_TOKEN not in text
    assert "EXEC-SECRET" not in text
    assert "AGENT-NOTE" not in text


def test_mode_change_reloads_the_projects_tab(qtbot, settings_path):
    SettingsStore(settings_path).save(Settings(mode="local"))
    window = _shown_window(qtbot, settings_path, _sources(token=None))
    status = _page(window, "Projects").findChild(QLabel, "projects-status")
    qtbot.waitUntil(lambda: status.text() == NO_LOCAL_DATA_MESSAGE, timeout=5000)

    combo = window.mode_selector
    combo.setCurrentIndex(combo.findData("hosted"))

    qtbot.waitUntil(lambda: status.text() == NOT_SIGNED_IN_MESSAGE, timeout=5000)


def test_unbuilt_server_reader_says_so(qtbot, settings_path):
    """The running-server read is not built yet. Its state says so, and no network call is made for it."""
    SettingsStore(settings_path).save(Settings(mode="local"))
    real = default_sources(KeyringSecretStore(backend=MemoryKeyring()))
    sources = dataclasses.replace(real, probe=lambda url: local_client.HEALTH_RUNNING)
    window = _shown_window(qtbot, settings_path, sources)
    status = _page(window, "Projects").findChild(QLabel, "projects-status")

    expected = "The local Meridian server could not be read (SourceNotBuilt)."
    qtbot.waitUntil(lambda: status.text() == expected, timeout=5000)


def test_hosted_reader_gets_the_stored_token_and_never_shows_it(qtbot, settings_path):
    """The hosted read takes the token from the store and passes it to the hosted reader. The token never appears on screen.
    The reader is a fake here, so the test makes no network call."""
    calls = []

    def list_hosted(url, token):
        calls.append(token)
        return []

    SettingsStore(settings_path).save(Settings(mode="hosted"))
    window = _shown_window(qtbot, settings_path, _sources(token=FAKE_TOKEN, list_hosted=list_hosted))
    status = _page(window, "Projects").findChild(QLabel, "projects-status")

    qtbot.waitUntil(lambda: calls, timeout=5000)
    assert calls == [FAKE_TOKEN]
    assert FAKE_TOKEN not in status.text()
    assert FAKE_TOKEN not in _all_text(window)


# ----------------------------------------------------------- Settings tab (connection settings)

LOOPBACK_OK = "http://localhost:7999"


def _settings_tab(window):
    """The connection section, which sits inside the Settings page above the RunPod rows."""
    tab = _page(window, "Settings").findChild(SettingsTab, "settings-tab")
    assert tab is not None
    return tab


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:7878",  # not http
        "ftp://127.0.0.1:7878",  # not http
        "http://example.com:7878",  # not loopback
        "http://10.0.0.5:7878",  # a private address, not loopback
        "http://127.0.0.1.example.com:7878",  # a lookalike of a loopback address
        "http://someone@127.0.0.1:7878",  # a user name in the address
        "",  # empty
    ],
)
def test_settings_tab_rejects_non_loopback_local_url(qtbot, settings_path, url):
    SettingsStore(settings_path).save(Settings(mode="local"))
    before = settings_path.read_bytes()
    window = _window(qtbot, settings_path)
    tab = _settings_tab(window)
    tab.findChild(QLineEdit, "settings-local-url").setText(url)

    tab.findChild(QPushButton, "settings-save-local-url").click()

    message = tab.findChild(QLabel, "settings-local-url-message")
    assert message.text().startswith("Not saved.")
    assert not message.isHidden()
    # Nothing was written: the file bytes are unchanged.
    assert settings_path.read_bytes() == before


def test_settings_tab_saves_loopback_local_url_and_shows_saved_values(qtbot, settings_path, tmp_path):
    db = _empty_db(tmp_path)
    SettingsStore(settings_path).save(Settings(mode="local", local_url=LOOPBACK_OK, db_path=str(db)))
    window = _window(qtbot, settings_path)
    tab = _settings_tab(window)
    field = tab.findChild(QLineEdit, "settings-local-url")
    assert field.text() == LOOPBACK_OK
    assert tab.findChild(QLabel, "settings-db-path").text() == str(db)

    field.setText("  http://127.0.0.1:7999  ")
    tab.findChild(QPushButton, "settings-save-local-url").click()

    saved = SettingsStore(settings_path).load().settings
    assert saved.local_url == "http://127.0.0.1:7999"
    # Only the local URL changed. The mode and the database file are kept.
    assert saved.mode == "local"
    assert saved.db_path == str(db)
    assert tab.findChild(QLabel, "settings-local-url-message").text() == LOCAL_URL_SAVED_TEXT


def test_settings_tab_local_url_write_failure_is_shown(qtbot, settings_path, monkeypatch):
    SettingsStore(settings_path).save(Settings(mode="local"))
    before = settings_path.read_bytes()

    def failing_replace(src, dst):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(settings_store.os, "replace", failing_replace)
    window = _window(qtbot, settings_path)
    tab = _settings_tab(window)
    tab.findChild(QLineEdit, "settings-local-url").setText(LOOPBACK_OK)

    tab.findChild(QPushButton, "settings-save-local-url").click()

    assert tab.findChild(QLabel, "settings-local-url-message").text() == f"{NOT_WRITTEN_TEXT}."
    assert settings_path.read_bytes() == before
    assert sorted(p.name for p in settings_path.parent.iterdir()) == ["settings.json"]


def test_settings_tab_db_path_chosen_by_dialog_and_shown_back(qtbot, settings_path, tmp_path, monkeypatch):
    db = _empty_db(tmp_path)
    asked = []

    def fake_dialog(parent, caption, directory, file_filter, *args, **kwargs):
        asked.append((caption, file_filter))
        return str(db), "SQLite database (*.db *.sqlite *.sqlite3)"

    monkeypatch.setattr(QFileDialog, "getOpenFileName", fake_dialog)
    SettingsStore(settings_path).save(Settings(mode="local"))
    window = _window(qtbot, settings_path)
    tab = _settings_tab(window)
    label = tab.findChild(QLabel, "settings-db-path")
    assert label.text() == NOT_CHOSEN_TEXT

    tab.findChild(QPushButton, "settings-choose-db").click()

    assert len(asked) == 1
    assert SettingsStore(settings_path).load().settings.db_path == str(db)
    assert label.text() == str(db)
    assert tab.findChild(QLabel, "settings-db-message").text() == DB_SAVED_TEXT


def test_settings_tab_cancelled_dialog_changes_nothing(qtbot, settings_path, tmp_path, monkeypatch):
    previous = _empty_db(tmp_path)
    start_dirs = []

    def cancelled(parent, caption, directory, file_filter, *args, **kwargs):
        start_dirs.append(directory)
        return "", ""

    monkeypatch.setattr(QFileDialog, "getOpenFileName", cancelled)
    SettingsStore(settings_path).save(Settings(mode="local", db_path=str(previous)))
    before = settings_path.read_bytes()
    window = _window(qtbot, settings_path)
    tab = _settings_tab(window)

    tab.findChild(QPushButton, "settings-choose-db").click()

    # The dialog opens in the folder of the file already chosen, and a cancel leaves everything as it was.
    assert start_dirs == [str(previous.parent)]
    assert settings_path.read_bytes() == before
    assert tab.findChild(QLabel, "settings-db-path").text() == str(previous)
    assert tab.findChild(QLabel, "settings-db-message").isHidden()


def test_settings_tab_never_shows_or_takes_the_hosted_token(qtbot, settings_path):
    backend = MemoryKeyring()
    secrets = KeyringSecretStore(backend=backend)
    secrets.set_secret(TOKEN_SERVICE, HOSTED_TOKEN_ACCOUNT, FAKE_TOKEN)
    SettingsStore(settings_path).save(Settings(mode="hosted"))
    window = MainWindow(SettingsStore(settings_path), secret_store=secrets, data_sources=_sources())
    qtbot.addWidget(window)
    tab = _settings_tab(window)

    # The token field is for entering a new token. It never shows the stored one, and the tab reports only "set".
    names = {field.objectName() for field in tab.findChildren(QLineEdit)}
    assert {"settings-local-url", "settings-hosted-url", "settings-token-input"} <= names
    assert tab.findChild(QLineEdit, "settings-token-input").text() == ""
    assert FAKE_TOKEN not in _all_text(window)
    assert FAKE_TOKEN not in tab.findChild(QLabel, "settings-mode").text()


def test_settings_tab_and_top_bar_show_the_same_saved_mode(qtbot, settings_path, monkeypatch):
    SettingsStore(settings_path).save(Settings(mode="hosted"))
    window = _shown_window(qtbot, settings_path)
    tab = _settings_tab(window)
    mode_label = tab.findChild(QLabel, "settings-mode")
    combo = window.mode_selector
    assert combo.currentData() == "hosted"
    assert mode_label.text() == mode_text("hosted")

    combo.setCurrentIndex(combo.findData("local"))
    assert SettingsStore(settings_path).load().settings.mode == "local"
    assert combo.currentData() == "local"
    assert mode_label.text() == mode_text("local")

    # A failed save reverts the selector, and the tab still shows the mode that is saved.
    def failing_replace(src, dst):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(settings_store.os, "replace", failing_replace)
    combo.setCurrentIndex(combo.findData("hosted"))
    assert combo.currentData() == "local"
    assert mode_label.text() == mode_text("local")
