"""Tests for the local data fallback: the core folder's data/meridian.db, read read-only, for Projects and Sessions.

Symptom D: with no database file chosen, Projects and Sessions said no local data was found without looking in the
core folder. These tests cover the search, the source order (local server, then hosted when a token is saved, then the
file), the snapshot label, the no-data message, the Sessions file reader wiring, the Sessions re-read when the file
settings change, and the Settings database chooser.

Every database is built in tmp_path (see test_sessions_reader.make_core_db). No test opens the real core database,
reaches the network, reads the real keychain or starts a server. Offscreen Qt, from conftest.
"""

from __future__ import annotations

import dataclasses
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QComboBox, QLabel, QPushButton, QTableWidget

from _fakes import MemoryKeyring
from meridian_desktop import local_client, projects_reader, sessions_client as sc
from meridian_desktop.data_source import (
    NO_LOCAL_DATA_MESSAGE,
    SOURCE_FILE,
    SOURCE_HOSTED,
    SOURCE_SERVER,
    STATE_NO_LOCAL_DATA,
    STATE_OK,
    STATE_READ_FAILED,
    DataSources,
    ProjectsProvider,
    ProjectsUnresolved,
    core_database_path,
    no_local_data_message,
    resolve,
    snapshot_note,
)
from meridian_desktop.local_client import LocalClientError
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.tools_status import ToolsStatus
from meridian_desktop.ui import sessions_panel
from meridian_desktop.ui.main_window import MainWindow
from meridian_desktop.ui.sessions_panel import PROJECTS_FAILED_TEXT, SessionsPanel, status_text
from meridian_desktop.ui.settings_tab import NOT_CHOSEN_TEXT, USING_CORE_FILE_TEXT, SettingsTab
from test_sessions_reader import OTHER_PID, PID, folder_state, make_core_db

LOCAL_URL = "http://127.0.0.1:7999"
HOSTED_URL = "https://hosted.test"
FAKE_TOKEN = "sk_meridian_fake-fallback-0099"
SERVER_ROWS = [{"id": "srv-1", "name": "From server"}]
HOSTED_ROWS = [{"id": "hosted-1", "name": "From hosted"}]
# 12 October 2026 at noon, local time, so the date reads the same in every time zone.
WRITTEN = datetime(2026, 10, 12, 12, 0, 0)
WRITTEN_NOTE = "Read-only copy from the core folder, last written 12 October 2026. The local server is not running."


class Recorder:
    """Fake DataSources. The file reader is the real read-only reader. Each call is recorded by name."""

    def __init__(self, *, probe_state=local_client.HEALTH_NOT_RUNNING, token=None, hosted_error=None):
        self.probe_state = probe_state
        self.token = token
        self.hosted_error = hosted_error
        self.calls: list[str] = []
        self.read_paths: list[Path] = []

    def sources(self) -> DataSources:
        return DataSources(
            probe=self._probe,
            read_server=self._read_server,
            read_file=self._read_file,
            get_token=self._get_token,
            list_hosted=self._list_hosted,
        )

    def _probe(self, url):
        self.calls.append("probe")
        return self.probe_state

    def _read_server(self, url):
        self.calls.append("read_server")
        return list(SERVER_ROWS)

    def _read_file(self, path):
        self.calls.append("read_file")
        self.read_paths.append(path)
        return projects_reader.list_projects(path)

    def _get_token(self):
        self.calls.append("get_token")
        return self.token

    def _list_hosted(self, url, token):
        self.calls.append("list_hosted")
        if self.hosted_error is not None:
            raise self.hosted_error
        return list(HOSTED_ROWS)


def core_folder(tmp_path: Path, *, keep=("-wal", "-shm"), now=None) -> Path:
    """A core checkout in tmp_path with a WAL snapshot at data/meridian.db, last written on WRITTEN."""
    core = tmp_path / "core"
    (core / "meridian").mkdir(parents=True)
    (core / "meridian" / "__init__.py").write_text("", encoding="utf-8")
    db = make_core_db(core / "data", keep=keep, **({"now": now} if now is not None else {}))
    stamp = WRITTEN.timestamp()
    for path in (db, db.with_name("meridian.db-wal")):
        if path.exists():
            os.utime(path, (stamp, stamp))
    return core


def local(core: Path | None = None, db_path: Path | None = None, **changes) -> Settings:
    return Settings(
        mode="local",
        local_url=LOCAL_URL,
        hosted_url=HOSTED_URL,
        core_checkout="" if core is None else str(core),
        db_path=None if db_path is None else str(db_path),
        **changes,
    )


# ----------------------------------------------------------------- the resolver


def test_with_no_file_chosen_the_core_folder_file_is_read_and_labelled_a_snapshot(tmp_path):
    core = core_folder(tmp_path)
    before = folder_state(core / "data")
    fakes = Recorder()

    result = resolve(local(core), fakes.sources())

    assert result.state == STATE_OK
    assert result.source == SOURCE_FILE
    assert sorted(row["name"] for row in result.rows) == ["Alpha", "Beta"]
    assert fakes.read_paths == [core / "data" / "meridian.db"]
    assert core_database_path(local(core)) == core / "data" / "meridian.db"
    assert result.message == WRITTEN_NOTE
    # Read-only: no -wal or -shm was created and no byte changed.
    assert folder_state(core / "data") == before


@pytest.mark.parametrize("keep", [("-wal",), ()], ids=["wal-only", "clean"])
def test_the_core_file_is_read_without_creating_sidecars_in_each_snapshot_state(tmp_path, keep):
    core = core_folder(tmp_path, keep=keep)
    before = folder_state(core / "data")

    result = resolve(local(core), Recorder().sources())

    assert result.state == STATE_OK
    assert len(result.rows) == 2
    assert folder_state(core / "data") == before


def test_a_chosen_database_file_always_wins_over_the_core_folder_file(tmp_path):
    core = core_folder(tmp_path)
    chosen = make_core_db(tmp_path / "chosen")
    fakes = Recorder()

    result = resolve(local(core, chosen), fakes.sources())

    assert result.source == SOURCE_FILE
    assert fakes.read_paths == [chosen]
    assert result.message == ""  # a file the owner chose is not called a core folder copy


def test_a_chosen_file_that_is_gone_does_not_fall_back_to_the_core_folder_file(tmp_path):
    core = core_folder(tmp_path)
    gone = tmp_path / "gone.db"
    fakes = Recorder()

    result = resolve(local(core, gone), fakes.sources())

    assert result.state == STATE_NO_LOCAL_DATA
    assert "read_file" not in fakes.calls
    assert str(gone) in result.message


@pytest.mark.parametrize(
    ("case", "expected_source", "expected_calls"),
    [
        ("server", SOURCE_SERVER, ["probe", "read_server"]),
        ("hosted", SOURCE_HOSTED, ["probe", "get_token", "list_hosted"]),
        ("hosted_fails", SOURCE_FILE, ["probe", "get_token", "list_hosted", "read_file"]),
        ("no_token", SOURCE_FILE, ["probe", "get_token", "read_file"]),
    ],
)
def test_the_source_order_is_server_then_hosted_with_a_token_then_the_file(tmp_path, case, expected_source,
                                                                         expected_calls):
    core = core_folder(tmp_path)
    fakes = Recorder(
        probe_state=local_client.HEALTH_RUNNING if case == "server" else local_client.HEALTH_NOT_RUNNING,
        token=None if case == "no_token" else FAKE_TOKEN,
        hosted_error=RuntimeError("offline") if case == "hosted_fails" else None,
    )

    result = resolve(local(core), fakes.sources())

    assert result.state == STATE_OK
    assert result.source == expected_source
    assert fakes.calls == expected_calls
    assert FAKE_TOKEN not in repr(result)


def test_a_failed_hosted_read_with_no_file_reports_the_hosted_failure(tmp_path):
    fakes = Recorder(token=FAKE_TOKEN, hosted_error=RuntimeError("offline"))

    result = resolve(local(), fakes.sources())

    assert result.state == STATE_READ_FAILED
    assert result.source == SOURCE_HOSTED
    assert FAKE_TOKEN not in result.message


@pytest.mark.parametrize("mode", ["hosted", "tunnel"])
def test_hosted_and_tunnel_modes_never_read_the_core_folder_file(tmp_path, mode):
    core = core_folder(tmp_path)
    fakes = Recorder(token=None)

    result = resolve(dataclasses.replace(local(core), mode=mode), fakes.sources())

    assert result.source is None
    assert fakes.calls == ["get_token"]


def test_the_no_data_message_names_the_address_the_saved_file_and_the_core_file(tmp_path):
    core = tmp_path / "empty-core"
    core.mkdir()

    nothing = resolve(local(), Recorder().sources())
    no_core_file = resolve(local(core), Recorder().sources())
    gone = resolve(local(core, tmp_path / "gone.db"), Recorder().sources())

    for result in (nothing, no_core_file, gone):
        assert result.state == STATE_NO_LOCAL_DATA
        assert result.message.startswith("No local Meridian data is available. Checked: ")
        assert f"no Meridian server answered at {LOCAL_URL}" in result.message
        assert result.message.endswith("Start Meridian on this computer, or choose a database file in Settings.")
    assert nothing.message == no_local_data_message(local())
    assert "no database file is chosen in Settings" in nothing.message
    assert "no core folder is set" in nothing.message
    assert f"no data file at {core / 'data' / 'meridian.db'}" in no_core_file.message
    assert f"the chosen database file {tmp_path / 'gone.db'} was not found" in gone.message
    assert "{" not in NO_LOCAL_DATA_MESSAGE.format(server="a", saved="b", core="c")


def test_snapshot_note_without_a_date_still_says_read_only_and_not_running():
    assert snapshot_note(None) == "Read-only copy from the core folder. The local server is not running."
    assert snapshot_note(WRITTEN) == WRITTEN_NOTE


# ----------------------------------------------------------------- the projects provider the Sessions tab uses


def test_the_projects_provider_lists_pairs_and_names_the_file_for_the_sessions_read(tmp_path):
    core = core_folder(tmp_path)
    settings = [local(core)]
    provider = ProjectsProvider(lambda: settings[0], Recorder().sources())

    assert sorted(provider()) == sorted([(PID, "Alpha"), (OTHER_PID, "Beta")])
    assert provider.local_file() == (core / "data" / "meridian.db", WRITTEN_NOTE)
    assert provider.watch_key() == ("", str(core))

    chosen = make_core_db(tmp_path / "chosen")
    settings[0] = local(core, chosen)
    assert provider.local_file() == (chosen, "")
    assert provider.watch_key() == (str(chosen), str(core))

    settings[0] = local()
    assert provider.local_file() is None
    with pytest.raises(ProjectsUnresolved) as info:
        provider()
    assert info.value.message == no_local_data_message(local())


# ----------------------------------------------------------------- the Sessions tab


def _not_running(base_url, path, timeout):
    raise LocalClientError("not_running", "nothing listens")


class FakeHosted:
    def __init__(self, status=200, body=b"[]"):
        self.status = status
        self.body = body
        self.calls = []

    def __call__(self, request, timeout):
        from meridian_desktop import hosted_client

        self.calls.append(request)
        return hosted_client.Reply(self.status, self.body)


def _panel(qtbot, settings_box, *, token=None, hosted=None, sources=None):
    provider = ProjectsProvider(lambda: settings_box[0], sources if sources is not None else Recorder().sources())
    calls = SimpleNamespace(projects=0)

    class Counting:
        """The provider, with each project read counted. The file methods pass through."""

        def __call__(self):
            calls.projects += 1
            return provider()

        local_file = staticmethod(provider.local_file)
        watch_key = staticmethod(provider.watch_key)

    hosted = hosted if hosted is not None else FakeHosted()
    panel = SessionsPanel(
        lambda: LOCAL_URL,
        lambda: (HOSTED_URL, token),
        Counting(),
        lambda: settings_box[0].mode,
        local_transport=_not_running,
        hosted_transport=hosted,
        process_lister=lambda: sessions_panel.ClaudeProcessesResult(sessions_panel.claude_processes.LISTED, "", ()),
    )
    qtbot.addWidget(panel)
    panel.hide()
    return panel, calls, hosted


def settle(qtbot, panel):
    qtbot.waitUntil(lambda: panel._in_flight is None, timeout=5000)  # noqa: SLF001 - test hook


def status_of(panel):
    return panel.findChild(QLabel, "sessions-status").text()


def rows_of(panel):
    table = panel.findChild(QTableWidget, "sessions-table")
    return [tuple(table.item(r, c).text() for c in (0, 1, 3)) for r in range(table.rowCount())]


def test_sessions_read_the_file_when_the_server_and_hosted_are_unavailable(qtbot, tmp_path):
    core = core_folder(tmp_path, now=datetime.now(timezone.utc))
    panel, _calls, hosted = _panel(qtbot, [local(core)])

    panel.show()
    settle(qtbot, panel)

    assert panel.findChild(QComboBox, "sessions-project-picker").currentData() == PID
    assert rows_of(panel) == [
        ("fresh-active", "active", "claude-code"),
        ("stale-active", "idle (expired)", "codex"),
        ("already-idle", "idle", "claude-desktop"),
    ]
    status = status_of(panel)
    # The status line carries the snapshot note, and the time it was checked is on its own line below it.
    assert status.startswith(f"3 session(s) from the database file. {WRITTEN_NOTE}")
    assert "Checked at" not in status
    assert panel.findChild(QLabel, "sessions-checked").text().startswith("Checked at ")
    assert hosted.calls == []


def test_sessions_prefer_hosted_when_a_token_is_saved_and_fall_back_to_the_file_when_it_fails(qtbot, tmp_path):
    core = core_folder(tmp_path, now=datetime.now(timezone.utc))
    # The projects come from the file (no token on the resolver), the sessions try hosted with the saved token.
    refused = FakeHosted(status=401)
    panel, _calls, hosted = _panel(qtbot, [local(core)], token=FAKE_TOKEN, hosted=refused)

    panel.show()
    settle(qtbot, panel)

    assert len(hosted.calls) == 1
    assert [row[0] for row in rows_of(panel)] == ["fresh-active", "stale-active", "already-idle"]
    assert FAKE_TOKEN not in status_of(panel)

    answering = FakeHosted(body=b'[{"name": "from-hosted", "status": "active", "last_seen": ""}]')
    panel2, _calls2, hosted2 = _panel(qtbot, [local(core)], token=FAKE_TOKEN, hosted=answering)
    panel2.show()
    settle(qtbot, panel2)
    assert [row[0] for row in rows_of(panel2)] == ["from-hosted"]
    assert status_of(panel2).startswith("1 session(s) from hosted Meridian.")


def test_another_project_picked_then_refresh_reads_that_project_from_the_file(qtbot, tmp_path):
    core = core_folder(tmp_path, now=datetime.now(timezone.utc))
    panel, _calls, _hosted = _panel(qtbot, [local(core)])
    panel.show()
    settle(qtbot, panel)
    picker = panel.findChild(QComboBox, "sessions-project-picker")
    picker.setCurrentIndex(picker.findData(OTHER_PID))
    panel.refresh()
    settle(qtbot, panel)
    assert rows_of(panel) == [("other-project", "active", "claude-code")]


def test_an_empty_active_list_from_the_file_says_so_with_the_snapshot_label():
    empty = sc.SessionsResult(sc.SESSIONS, WRITTEN_NOTE, "file", ())
    assert status_text(empty, "12:00:00") == (
        f"No active sessions in the database file. {WRITTEN_NOTE} Checked at 12:00:00."
    )
    # A server result with no rows reads as before.
    server = sc.SessionsResult(sc.SESSIONS, "", sc.SOURCE_LOCAL, ())
    assert status_text(server, "12:00:00") == (
        "No sessions from this computer's Meridian server right now. Checked at 12:00:00."
    )


def test_the_sessions_tab_shows_the_resolver_message_not_the_generic_failure(qtbot, tmp_path):
    settings = local()
    panel, _calls, _hosted = _panel(qtbot, [settings])

    panel.show()
    settle(qtbot, panel)

    assert status_of(panel) == no_local_data_message(settings)
    assert status_of(panel) != PROJECTS_FAILED_TEXT


def test_the_sessions_tab_reads_again_on_show_when_the_file_settings_change(qtbot, tmp_path):
    core = core_folder(tmp_path, now=datetime.now(timezone.utc))
    box = [local()]
    panel, calls, _hosted = _panel(qtbot, box)
    panel.show()
    settle(qtbot, panel)
    assert calls.projects == 1
    assert status_of(panel) == no_local_data_message(box[0])

    # Shown again with nothing changed: no new read.
    panel.hide()
    panel.show()
    qtbot.wait(50)
    settle(qtbot, panel)
    assert calls.projects == 1

    # The core folder is set while the tab is hidden. The next show reads again and finds the file.
    panel.hide()
    box[0] = local(core)
    panel.show()
    qtbot.waitUntil(lambda: calls.projects == 2, timeout=5000)
    settle(qtbot, panel)
    assert [row[0] for row in rows_of(panel)] == ["fresh-active", "stale-active", "already-idle"]

    # A database file chosen in Settings is a change too.
    panel.hide()
    box[0] = local(core, make_core_db(tmp_path / "chosen"))
    panel.show()
    qtbot.waitUntil(lambda: calls.projects == 3, timeout=5000)
    settle(qtbot, panel)


# ----------------------------------------------------------------- the Settings database chooser


def _settings_tab(qtbot, settings_path, chooser):
    tab = SettingsTab(
        SettingsStore(settings_path),
        choose_database=chooser,
        secrets=KeyringSecretStore(backend=MemoryKeyring()),
        list_hosted=lambda url, token: [],
    )
    qtbot.addWidget(tab)
    return tab


def test_the_chooser_says_not_chosen_and_names_the_core_file_it_uses(qtbot, settings_path, tmp_path):
    core = core_folder(tmp_path)
    SettingsStore(settings_path).save(local(core))
    starts: list[str] = []

    def cancelled(parent, start_dir):
        starts.append(start_dir)
        return ""

    tab = _settings_tab(qtbot, settings_path, cancelled)
    label = tab.findChild(QLabel, "settings-db-path")

    expected = core / "data" / "meridian.db"
    assert label.text() == USING_CORE_FILE_TEXT.format(path=expected)
    assert label.text() == f"Not chosen. Using {expected}."

    tab.findChild(QPushButton, "settings-choose-db").click()
    assert starts == [str(core / "data")]
    assert SettingsStore(settings_path).load().settings.db_path is None


def test_the_chooser_with_no_core_file_says_not_chosen_and_a_chosen_file_is_shown(qtbot, settings_path, tmp_path):
    empty_core = tmp_path / "empty-core"
    empty_core.mkdir()
    SettingsStore(settings_path).save(local(empty_core))
    chosen = make_core_db(tmp_path / "chosen")
    starts: list[str] = []

    def chooser(parent, start_dir):
        starts.append(start_dir)
        return str(chosen)

    tab = _settings_tab(qtbot, settings_path, chooser)
    label = tab.findChild(QLabel, "settings-db-path")
    assert label.text() == NOT_CHOSEN_TEXT

    tab.findChild(QPushButton, "settings-choose-db").click()
    assert label.text() == str(chosen)
    assert starts == [""]  # no data folder in this core folder, so the dialog starts where Qt chooses


# ----------------------------------------------------------------- the window, end to end


def test_the_window_shows_the_core_file_on_projects_and_sessions(qtbot, settings_path, tmp_path, monkeypatch):
    core = core_folder(tmp_path, now=datetime.now(timezone.utc))
    SettingsStore(settings_path).save(local(core))
    local_calls: list[str] = []
    hosted_calls: list[str] = []

    def fake_local(base_url, project_id, *, transport=None, timeout=None):
        local_calls.append(project_id)
        return sc.SessionsResult(sc.NOT_RUNNING, sc.NOT_RUNNING_MESSAGE, sc.SOURCE_LOCAL)

    def fake_hosted(*args, **kwargs):
        hosted_calls.append("hosted")
        raise AssertionError("no hosted token is saved, so hosted is never asked")

    monkeypatch.setattr(sessions_panel, "local_sessions", fake_local)
    monkeypatch.setattr(sessions_panel, "hosted_sessions", fake_hosted)
    monkeypatch.setattr(sessions_panel.claude_processes, "list_claude_processes",
                        lambda run=None: sessions_panel.ClaudeProcessesResult(sessions_panel.claude_processes.LISTED,
                                                                              "", ()))
    sources = DataSources(
        probe=lambda url: local_client.HEALTH_NOT_RUNNING,
        read_server=lambda url: (_ for _ in ()).throw(AssertionError("server not running")),
        read_file=projects_reader.list_projects,
        get_token=lambda: None,
        list_hosted=lambda url, token: (_ for _ in ()).throw(AssertionError("no token")),
    )
    window = MainWindow(
        SettingsStore(settings_path),
        secret_store=KeyringSecretStore(backend=MemoryKeyring()),
        data_sources=sources,
        tools_status=lambda: ToolsStatus(rows=()),
    )
    qtbot.addWidget(window)
    names = [window.tabs.tabText(i) for i in range(window.tabs.count())]
    assert names == ["Home", "Projects", "Sessions", "Search", "Settings"]
    # Projects is in front before the window shows, so Home's checks (Zotero, the tunnel, the slot ports) never run.
    window.tabs.setCurrentIndex(names.index("Projects"))
    window.show()
    qtbot.waitExposed(window)

    projects = window.tabs.widget(names.index("Projects"))
    projects_status = projects.findChild(QLabel, "projects-status")
    qtbot.waitUntil(lambda: projects_status.text().startswith("Showing 2 project(s) from the database file."),
                    timeout=5000)

    window.tabs.setCurrentIndex(names.index("Sessions"))
    sessions = window.tabs.widget(names.index("Sessions"))
    table = sessions.findChild(QTableWidget, "sessions-table")
    qtbot.waitUntil(lambda: table.rowCount() == 3, timeout=5000)
    assert sessions.findChild(QLabel, "sessions-status").text().startswith(
        f"3 session(s) from the database file. {WRITTEN_NOTE}"
    )
    assert local_calls and hosted_calls == []
