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
    QLabel,
    QLineEdit,
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
from meridian_desktop.ui.main_window import TAB_NAMES, WRITING_SECTIONS, MainWindow
from meridian_desktop.ui.projects_tab import default_sources

EXPECTED_TABS = ("Projects", "Writing", "Zotero", "Tunnel", "Services", "Code", "Logs", "Settings")
EXPECTED_WAVES = {
    "Writing": 5,
    "Zotero": 4,
    "Tunnel": 3,
    "Services": 3,
    "Code": 6,
    "Logs": 3,
}
STATUS = "No services checked yet"

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


def test_placeholders_name_their_wave(qtbot, settings_path):
    window = _window(qtbot, settings_path)
    for name, wave in EXPECTED_WAVES.items():
        assert f"Not built yet (wave {wave})" in _label_texts(_page(window, name)), name


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


def test_unbuilt_hosted_reader_uses_the_stored_token_and_says_so(qtbot, settings_path):
    """The hosted token comes from the injected keychain, and the hosted reader is not built yet."""
    backend = MemoryKeyring()
    backend.set_password(TOKEN_SERVICE, HOSTED_TOKEN_ACCOUNT, FAKE_TOKEN)
    SettingsStore(settings_path).save(Settings(mode="hosted"))
    window = _shown_window(qtbot, settings_path, default_sources(KeyringSecretStore(backend=backend)))
    status = _page(window, "Projects").findChild(QLabel, "projects-status")

    expected = "Hosted Meridian projects could not be loaded (SourceNotBuilt)."
    qtbot.waitUntil(lambda: status.text() == expected, timeout=5000)
    assert FAKE_TOKEN not in status.text()
