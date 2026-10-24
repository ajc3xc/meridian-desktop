"""Tests for the local data fallback: the core folder's data/meridian.db, read read-only, for Projects and Sessions.

Symptom D: with no database file chosen, Projects and Sessions said no local data was found without looking in the
core folder. These tests cover the search, the source order both tabs share (local server, then hosted when a token is
saved, then the file only when no token is saved; hosted and tunnel modes read hosted only), the snapshot label on
both tabs, the hosted errors that a file must never hide, the no-data message, the Sessions file reader wiring, the
Sessions re-read when the file settings change, and the Settings database chooser.

Every database is built in tmp_path (see test_sessions_reader.make_core_db). No test opens the real core database,
reaches the network or reads the real keychain. The one server is a stub on an ephemeral 127.0.0.1 port, never a real
Meridian port, used to check that Projects and Sessions decide "did the local server answer?" with the same check.
Offscreen Qt, from conftest.
"""

from __future__ import annotations

import dataclasses
import json
import os
import socket
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QComboBox, QLabel, QPushButton, QTableWidget

from _fakes import MemoryKeyring
from meridian_desktop import hosted_client, local_client, projects_reader, sessions_client as sc, sessions_reader
from meridian_desktop import token_store
from meridian_desktop.claude_processes import LISTED, ClaudeProcessesResult
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
from meridian_desktop.ui import main_window, projects_tab, sessions_panel
from meridian_desktop.ui.main_window import MainWindow
from meridian_desktop.ui.projects_tab import ProjectsTab
from meridian_desktop.ui.sessions_panel import PROJECTS_FAILED_TEXT, SessionsPanel, status_text
from meridian_desktop.ui.settings_tab import NOT_CHOSEN_TEXT, USING_CORE_FILE_TEXT, SettingsTab
from test_sessions_reader import OTHER_PID, PID, folder_state, make_core_db

LOCAL_URL = "http://127.0.0.1:7999"
HOSTED_URL = "https://hosted.test"
FAKE_TOKEN = "sk_meridian_fake-fallback-0099"
SERVER_ROWS = [{"id": "srv-1", "name": "From server"}]
HOSTED_ROWS = [{"id": "hosted-1", "name": "From hosted"}]
# Hosted projects whose ids the sessions client accepts, so the Sessions tab asks hosted for their sessions.
HOSTED_PROJECT_ROWS = [{"id": PID, "name": "Alpha"}]
# 12 October 2026 at noon, local time, so the date reads the same in every time zone.
WRITTEN = datetime(2026, 10, 12, 12, 0, 0)
WRITTEN_NOTE = "Read-only copy from the core folder, last written 12 October 2026. The local server is not running."
# The hosted failures a saved token can meet: refused (401), forbidden (403), no route (404), down (503), and a
# connection that is refused before any reply.
HOSTED_FAILURES = [401, 403, 404, 503, "refused"]


class Recorder:
    """Fake DataSources. The file reader is the real read-only reader. Each call is recorded by name."""

    def __init__(self, *, probe_state=local_client.HEALTH_NOT_RUNNING, token=None, hosted_error=None,
                 hosted_rows=None):
        self.probe_state = probe_state
        self.token = token
        self.hosted_error = hosted_error
        self.hosted_rows = HOSTED_ROWS if hosted_rows is None else hosted_rows
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
        return [dict(row) for row in self.hosted_rows]


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
    ("case", "expected_state", "expected_source", "expected_calls"),
    [
        ("server", STATE_OK, SOURCE_SERVER, ["probe", "read_server"]),
        ("hosted", STATE_OK, SOURCE_HOSTED, ["probe", "get_token", "list_hosted"]),
        # A saved token means hosted is in use: its failure is the answer, and the file is never read over it.
        ("hosted_fails", STATE_READ_FAILED, SOURCE_HOSTED, ["probe", "get_token", "list_hosted"]),
        ("no_token", STATE_OK, SOURCE_FILE, ["probe", "get_token", "read_file"]),
    ],
)
def test_the_source_order_is_server_then_hosted_with_a_token_else_the_file(tmp_path, case, expected_state,
                                                                         expected_source, expected_calls):
    core = core_folder(tmp_path)
    fakes = Recorder(
        probe_state=local_client.HEALTH_RUNNING if case == "server" else local_client.HEALTH_NOT_RUNNING,
        token=None if case == "no_token" else FAKE_TOKEN,
        hosted_error=RuntimeError("offline") if case == "hosted_fails" else None,
    )

    result = resolve(local(core), fakes.sources())

    assert result.state == expected_state
    assert result.source == expected_source
    assert fakes.calls == expected_calls
    assert FAKE_TOKEN not in repr(result)


def _hosted_failure(status) -> FakeHosted:
    """A hosted transport that answers ``status``, or refuses the connection for "refused"."""
    return FakeHosted(refused=True) if status == "refused" else FakeHosted(status=status)


@pytest.mark.parametrize("status", HOSTED_FAILURES)
def test_a_hosted_error_with_a_token_is_shown_on_projects_and_the_core_file_is_not_read(tmp_path, status):
    core = core_folder(tmp_path)
    before = folder_state(core / "data")
    fakes = Recorder(token=FAKE_TOKEN)
    failing = _hosted_failure(status)
    sources = dataclasses.replace(
        fakes.sources(), list_hosted=lambda url, token: hosted_client.list_projects(url, token, transport=failing)
    )
    with pytest.raises(hosted_client.HostedError) as raised:
        hosted_client.list_projects(HOSTED_URL, FAKE_TOKEN, transport=_hosted_failure(status))

    result = resolve(local(core), sources)

    assert result.state == STATE_READ_FAILED
    assert result.source == SOURCE_HOSTED
    assert result.rows == []
    # The hosted client's own words, exactly as a hosted-mode failure shows them.
    assert result.message == f"Hosted Meridian projects could not be loaded. {raised.value}"
    assert len(failing.calls) == 1
    assert "read_file" not in fakes.calls
    assert FAKE_TOKEN not in repr(result)
    assert folder_state(core / "data") == before


def test_with_a_token_saved_and_a_database_file_chosen_the_order_is_server_then_hosted(tmp_path):
    """The answer to "what is the local order with a token and a db_path": the server, then hosted. The chosen file is
    not read while a token is saved, whether hosted answers or fails."""
    chosen = make_core_db(tmp_path / "chosen")
    answered = Recorder(token=FAKE_TOKEN)
    failed = Recorder(token=FAKE_TOKEN, hosted_error=hosted_client.HostedUnreachable())
    running = Recorder(token=FAKE_TOKEN, probe_state=local_client.HEALTH_RUNNING)

    assert resolve(local(db_path=chosen), answered.sources()).source == SOURCE_HOSTED
    assert resolve(local(db_path=chosen), failed.sources()).state == STATE_READ_FAILED
    assert resolve(local(db_path=chosen), running.sources()).source == SOURCE_SERVER

    assert answered.calls == ["probe", "get_token", "list_hosted"]
    assert failed.calls == ["probe", "get_token", "list_hosted"]
    assert running.calls == ["probe", "read_server"]
    assert answered.read_paths == failed.read_paths == running.read_paths == []


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
    """A hosted transport for both hosted clients. It answers one status, or refuses the connection."""

    def __init__(self, status=200, body=b"[]", refused=False):
        self.status = status
        self.body = body
        self.refused = refused
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append(request)
        if self.refused:
            raise ConnectionRefusedError("nothing listens at the hosted address")
        return hosted_client.Reply(self.status, self.body)


@pytest.fixture
def file_session_reads(monkeypatch):
    """Each project id the Sessions tab asked the database file for. The real reader still answers."""
    reads: list[str] = []
    real = sessions_reader.file_sessions

    def recording(path, project_id, **options):
        reads.append(project_id)
        return real(path, project_id, **options)

    monkeypatch.setattr(sessions_reader, "file_sessions", recording)
    return reads


def _panel(qtbot, settings_box, *, token=None, hosted=None, sources=None):
    provider = ProjectsProvider(lambda: settings_box[0], sources if sources is not None else Recorder().sources())
    calls = SimpleNamespace(projects=0)

    class Counting:
        """The provider, with each project read counted. The file methods pass through."""

        def __call__(self):
            calls.projects += 1
            return provider()

        local_server_answers = staticmethod(provider.local_server_answers)
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


@pytest.mark.parametrize("status", HOSTED_FAILURES)
def test_sessions_with_a_token_show_the_hosted_error_exactly_and_never_the_file_rows(qtbot, tmp_path,
                                                                                    file_session_reads, status):
    core = core_folder(tmp_path, now=datetime.now(timezone.utc))
    # The projects come from hosted with the saved token, as the window wires it. Their sessions read fails.
    sources = Recorder(token=FAKE_TOKEN, hosted_rows=HOSTED_PROJECT_ROWS).sources()
    failing = _hosted_failure(status)
    expected = sc.hosted_sessions(HOSTED_URL, PID, FAKE_TOKEN, transport=_hosted_failure(status))
    assert expected.state != sc.SESSIONS
    panel, _calls, hosted = _panel(qtbot, [local(core)], token=FAKE_TOKEN, hosted=failing, sources=sources)

    panel.show()
    settle(qtbot, panel)

    assert len(hosted.calls) == 1
    # The hosted error is shown as the sessions client words it, as it was before the file reader existed.
    assert status_of(panel) == expected.message
    assert rows_of(panel) == []
    assert file_session_reads == []
    assert "database file" not in status_of(panel)
    assert FAKE_TOKEN not in status_of(panel)


def test_sessions_with_a_token_read_hosted_rows_and_not_the_file(qtbot, tmp_path, file_session_reads):
    core = core_folder(tmp_path, now=datetime.now(timezone.utc))
    sources = Recorder(token=FAKE_TOKEN, hosted_rows=HOSTED_PROJECT_ROWS).sources()
    answering = FakeHosted(body=b'[{"name": "from-hosted", "status": "active", "last_seen": ""}]')
    panel, _calls, hosted = _panel(qtbot, [local(core)], token=FAKE_TOKEN, hosted=answering, sources=sources)

    panel.show()
    settle(qtbot, panel)

    assert len(hosted.calls) == 1
    assert [row[0] for row in rows_of(panel)] == ["from-hosted"]
    assert status_of(panel).startswith("1 session(s) from hosted Meridian.")
    assert file_session_reads == []


@pytest.mark.parametrize("mode", ["hosted", "tunnel"])
def test_hosted_and_tunnel_sessions_never_read_the_file_or_the_local_server(qtbot, tmp_path, file_session_reads,
                                                                            mode):
    core = core_folder(tmp_path, now=datetime.now(timezone.utc))
    sources = Recorder(token=FAKE_TOKEN, hosted_rows=HOSTED_PROJECT_ROWS).sources()
    down = FakeHosted(status=503)
    local_asks: list[str] = []

    def local_server(base_url, path, timeout):
        local_asks.append(path)
        return SimpleNamespace(status=200, body=b'[{"name": "from-local", "status": "active", "last_seen": ""}]')

    provider = ProjectsProvider(lambda: dataclasses.replace(local(core), mode=mode), sources)
    panel = SessionsPanel(
        lambda: LOCAL_URL,
        lambda: (HOSTED_URL, FAKE_TOKEN),
        provider,
        lambda: mode,
        local_transport=local_server,
        hosted_transport=down,
        process_lister=lambda: ClaudeProcessesResult(LISTED, "", ()),
    )
    qtbot.addWidget(panel)
    panel.show()
    settle(qtbot, panel)

    assert len(down.calls) == 1
    assert status_of(panel) == sc.HTTP_STATUS_MESSAGE.format(503)
    assert rows_of(panel) == []
    assert local_asks == []
    assert file_session_reads == []


# ----------------------------------------------------------------- one source order for both tabs

_SESSIONS_SOURCE = {sc.SOURCE_LOCAL: SOURCE_SERVER, sc.SOURCE_HOSTED: SOURCE_HOSTED, sessions_reader.SOURCE_FILE: SOURCE_FILE}


def _expected_order(mode, server_up, token, with_file):
    """The sources a read takes rows from, in order, after the local-server check: the rule both tabs must follow."""
    if mode != "local":
        return [SOURCE_HOSTED] if token else []
    if server_up:
        return [SOURCE_SERVER]
    if token:
        return [SOURCE_HOSTED]
    return [SOURCE_FILE] if with_file else []


@pytest.mark.parametrize("with_file", [True, False], ids=["file", "no-file"])
@pytest.mark.parametrize("hosted_ok", [True, False], ids=["hosted-ok", "hosted-401"])
@pytest.mark.parametrize("token", [FAKE_TOKEN, None], ids=["token", "no-token"])
@pytest.mark.parametrize("server_up", [True, False], ids=["server-up", "server-down"])
@pytest.mark.parametrize("mode", ["local", "hosted", "tunnel"])
def test_projects_and_sessions_ask_the_same_sources_in_the_same_order(tmp_path, file_session_reads, mode,
                                                                      server_up, token, hosted_ok, with_file):
    settings = dataclasses.replace(local(core_folder(tmp_path) if with_file else None), mode=mode)

    projects_asked: list[str] = []
    # Every local-server check, from either tab. Both tabs use this one probe through data_source.local_server_answers.
    probes: list[str] = []

    def probe(url):
        probes.append(url)
        return local_client.HEALTH_RUNNING if server_up else local_client.HEALTH_NOT_RUNNING

    def read_server(url):
        projects_asked.append(SOURCE_SERVER)
        return [dict(row) for row in HOSTED_PROJECT_ROWS]

    def list_hosted(url, saved):
        projects_asked.append(SOURCE_HOSTED)
        if not hosted_ok:
            raise hosted_client.NotSignedIn(hosted_client.REJECTED_TEXT, 401)
        return [dict(row) for row in HOSTED_PROJECT_ROWS]

    def read_file(path):
        projects_asked.append(SOURCE_FILE)
        return projects_reader.list_projects(path)

    sources = DataSources(
        probe=probe,
        read_server=read_server,
        read_file=read_file,
        get_token=lambda: token,
        list_hosted=list_hosted,
    )
    projects = resolve(settings, sources)
    projects_probes = list(probes)

    sessions_asked: list[str] = []

    def local_server(base_url, path, timeout):
        # Asked only when the shared check said the server answered. A stray call would show in sessions_asked.
        sessions_asked.append(SOURCE_SERVER)
        if not server_up:
            raise LocalClientError("not_running", "nothing listens")
        return SimpleNamespace(status=200, body=b"[]")

    def hosted_server(request, timeout):
        sessions_asked.append(SOURCE_HOSTED)
        return hosted_client.Reply(200 if hosted_ok else 401, b"[]")

    reads = sessions_panel._Reads(  # noqa: SLF001 - the read the Sessions tab runs on its worker
        lambda: LOCAL_URL,
        lambda: (HOSTED_URL, token),
        ProjectsProvider(lambda: settings, sources),
        lambda: mode,
        local_server,
        hosted_server,
        lambda: ClaudeProcessesResult(LISTED, "", ()),
    )
    sessions = reads._read_sessions(PID)  # noqa: SLF001
    sessions_asked.extend(SOURCE_FILE for _ in file_session_reads)
    sessions_probes = probes[len(projects_probes):]

    expected = _expected_order(mode, server_up, token, with_file)
    assert projects_asked == expected
    assert sessions_asked == expected
    # One check decides for both tabs: the same probe, asked once by each, at the same address, in local mode only.
    assert projects_probes == sessions_probes == ([LOCAL_URL] if mode == "local" else [])
    # Both tabs show the same source: the one rows came from, or none.
    shown = expected[-1] if expected else None
    assert projects.source == shown
    sessions_source = None if sessions.state == sc.NOT_RUNNING or not sessions.source else _SESSIONS_SOURCE[
        sessions.source]
    assert sessions_source == shown


# ----------------------------------------------------------------- one local-server check, against a stub server
#
# A real HTTP server on an ephemeral loopback port stands in for the local Meridian server, in each state it can be
# in. The app's own wiring reads it over real sockets: projects_tab.default_sources for the Projects tab, and the
# window's providers for the Sessions tab. Only the probe timeout is shortened, and hosted answers through a fake.

STUB_STATES = ["answers", "refuses", "bad_body", "times_out"]
STUB_PROBE_TIMEOUT = 0.5
# The real local ports. A stub must never take one.
REAL_PORTS = {7878, 8471, 23119, *range(8808, 8822)}
SERVER_SESSION = {"name": "from-server", "status": "active", "last_seen": ""}
HOSTED_SESSION = {"name": "from-hosted", "status": "active", "last_seen": ""}


class _StubHandler(BaseHTTPRequestHandler):
    """One local server state, chosen by the server's ``state``. Each request path is recorded."""

    def do_GET(self):  # noqa: N802 - http.server naming
        self.server.paths.append(self.path)
        state = self.server.state
        if state == "times_out":
            self.server.release.wait(10)  # no reply until the test ends
            return
        if state == "bad_body":
            self._reply(200, b"<html>Not Meridian</html>", "text/html")
            return
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._reply(200, json.dumps({"service": "meridian", "status": "ok"}).encode("utf-8"))
        elif path == "/projects":
            self._reply(200, json.dumps(HOSTED_PROJECT_ROWS).encode("utf-8"))
        elif path.startswith("/projects/") and path.endswith("/sessions"):
            self._reply(200, json.dumps([SERVER_SESSION]).encode("utf-8"))
        else:
            self._reply(404, b"{}")

    def _reply(self, status, body, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # the test output stays quiet
        pass


@pytest.fixture
def stub_server(monkeypatch):
    """start(state) -> (local URL, paths asked). "refuses" is a loopback port with nothing listening."""
    monkeypatch.setattr(projects_tab, "PROBE_TIMEOUT", STUB_PROBE_TIMEOUT)
    servers = []

    def start(state):
        if state == "refuses":
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            assert port not in REAL_PORTS
            return f"http://127.0.0.1:{port}", []
        server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
        server.daemon_threads = True
        server.state = state
        server.paths = []
        server.release = threading.Event()
        assert server.server_address[1] not in REAL_PORTS
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}", server.paths

    yield start
    for server in servers:
        server.release.set()
        server.shutdown()
        server.server_close()


class RoutedHosted:
    """A hosted transport: the project list for /projects, one session for a sessions path. Each URL is recorded."""

    def __init__(self):
        self.urls = []

    def __call__(self, request, timeout):
        self.urls.append(request.full_url)
        rows = [HOSTED_SESSION] if "/sessions" in request.full_url else HOSTED_PROJECT_ROWS
        return hosted_client.Reply(200, json.dumps(rows).encode("utf-8"))


_EXPECTED_SOURCE = {"answers": SOURCE_SERVER}


@pytest.mark.parametrize("token", [FAKE_TOKEN, None], ids=["token", "no-token"])
@pytest.mark.parametrize("state", STUB_STATES)
def test_projects_and_sessions_choose_the_same_source_for_each_local_server_state(
    stub_server, settings_path, tmp_path, file_session_reads, state, token
):
    url, asked = stub_server(state)
    core = core_folder(tmp_path, now=datetime.now(timezone.utc))
    store = SettingsStore(settings_path)
    store.save(dataclasses.replace(local(core), local_url=url))
    secrets = KeyringSecretStore(backend=MemoryKeyring())
    if token:
        token_store.set_token(token, store=secrets)
    hosted = RoutedHosted()
    # The app's sources, with hosted answered by the fake. The probe and the server read use real sockets.
    sources = dataclasses.replace(
        projects_tab.default_sources(secrets),
        list_hosted=lambda hosted_url, saved: hosted_client.list_projects(hosted_url, saved, transport=hosted),
    )

    # The Projects tab: the resolve its worker runs.
    projects = projects_tab._settings_resolution(store, sources)  # noqa: SLF001
    # The Sessions tab: the reads its worker runs, wired as the window wires them.
    reads = sessions_panel._Reads(  # noqa: SLF001
        main_window._saved_local_url(store),  # noqa: SLF001
        main_window._saved_hosted(store, secrets),  # noqa: SLF001
        main_window._saved_projects(store, sources),  # noqa: SLF001
        main_window._saved_mode(store),  # noqa: SLF001
        None,
        hosted,
        lambda: ClaudeProcessesResult(LISTED, "", ()),
    )
    full = reads.collect_full(1, "")
    tick = reads.collect_local(2, PID)

    expected = _EXPECTED_SOURCE.get(state, SOURCE_HOSTED if token else SOURCE_FILE)
    assert projects.state == STATE_OK
    assert projects.source == expected
    assert full.project_id == PID
    assert full.result.state == sc.SESSIONS
    assert _SESSIONS_SOURCE[full.result.source] == expected
    if expected == SOURCE_SERVER:
        assert [row.name for row in full.result.rows] == ["from-server"]
        assert tick.result is not None and _SESSIONS_SOURCE[tick.result.source] == SOURCE_SERVER
    else:
        # The server did not answer the probe, so no tab asked it for anything else, and the tick shows nothing new.
        assert all(path == "/health" for path in asked)
        assert tick.result is None
    if expected == SOURCE_HOSTED:
        assert [row.name for row in full.result.rows] == ["from-hosted"]
    assert any("/sessions" in sent for sent in hosted.urls) == (expected == SOURCE_HOSTED)
    if expected == SOURCE_FILE:
        # The snapshot label on both tabs, and the file is read only here.
        assert projects.message == WRITTEN_NOTE
        assert full.result.message == WRITTEN_NOTE
        assert file_session_reads == [PID]
    else:
        assert file_session_reads == []


@pytest.mark.parametrize("state", STUB_STATES)
def test_the_window_shows_projects_and_sessions_from_the_same_source_for_each_local_server_state(
    qtbot, stub_server, settings_path, tmp_path, monkeypatch, state
):
    """The whole window with the app's real sources and no token: both screens name the same source."""
    url, _asked = stub_server(state)
    core = core_folder(tmp_path, now=datetime.now(timezone.utc))
    SettingsStore(settings_path).save(dataclasses.replace(local(core), local_url=url))
    monkeypatch.setattr(sessions_panel.claude_processes, "list_claude_processes",
                        lambda run=None: ClaudeProcessesResult(LISTED, "", ()))
    window = MainWindow(
        SettingsStore(settings_path),
        secret_store=KeyringSecretStore(backend=MemoryKeyring()),
        tools_status=lambda: ToolsStatus(rows=()),
    )
    qtbot.addWidget(window)
    names = [window.tabs.tabText(i) for i in range(window.tabs.count())]
    window.tabs.setCurrentIndex(names.index("Projects"))
    window.show()
    qtbot.waitExposed(window)

    if state == "answers":
        projects_line = "Showing 1 project(s) from the local Meridian server."
        sessions_start = "1 session(s) from this computer's Meridian server."
    else:
        projects_line = f"Showing 2 project(s) from the database file. {WRITTEN_NOTE}"
        sessions_start = f"3 session(s) from the database file. {WRITTEN_NOTE}"
    projects_status = window.tabs.widget(names.index("Projects")).findChild(QLabel, "projects-status")
    qtbot.waitUntil(lambda: projects_status.text() == projects_line, timeout=10000)

    window.tabs.setCurrentIndex(names.index("Sessions"))
    sessions = window.tabs.widget(names.index("Sessions"))
    sessions_status = sessions.findChild(QLabel, "sessions-status")
    qtbot.waitUntil(lambda: sessions_status.text().startswith(sessions_start), timeout=10000)


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
    # The Projects screen carries the same snapshot label as Sessions, after the count.
    qtbot.waitUntil(
        lambda: projects_status.text() == f"Showing 2 project(s) from the database file. {WRITTEN_NOTE}", timeout=5000
    )
    assert projects_status.isVisible()

    window.tabs.setCurrentIndex(names.index("Sessions"))
    sessions = window.tabs.widget(names.index("Sessions"))
    table = sessions.findChild(QTableWidget, "sessions-table")
    qtbot.waitUntil(lambda: table.rowCount() == 3, timeout=5000)
    assert sessions.findChild(QLabel, "sessions-status").text().startswith(
        f"3 session(s) from the database file. {WRITTEN_NOTE}"
    )
    # The probe says no local server answered, so, as on Projects, its sessions route is never asked.
    assert local_calls == [] and hosted_calls == []


def test_a_hosted_401_shows_on_projects_and_sessions_while_the_core_file_exists(qtbot, settings_path, tmp_path,
                                                                               monkeypatch, file_session_reads):
    core = core_folder(tmp_path, now=datetime.now(timezone.utc))
    before = folder_state(core / "data")
    SettingsStore(settings_path).save(local(core))
    secrets = KeyringSecretStore(backend=MemoryKeyring())
    token_store.set_token(FAKE_TOKEN, store=secrets)
    refusing = FakeHosted(status=401)
    file_reads: list[Path] = []
    local_calls: list[str] = []

    def read_file(path):
        file_reads.append(path)
        return projects_reader.list_projects(path)

    def fake_local(base_url, project_id, *, transport=None, timeout=None):
        local_calls.append(project_id)
        return sc.SessionsResult(sc.NOT_RUNNING, sc.NOT_RUNNING_MESSAGE, sc.SOURCE_LOCAL)

    def refusing_sessions(base_url, project_id, token, *, transport=None, timeout=None):
        return sc.hosted_sessions(base_url, project_id, token, transport=refusing)

    monkeypatch.setattr(sessions_panel, "local_sessions", fake_local)
    monkeypatch.setattr(sessions_panel, "hosted_sessions", refusing_sessions)
    monkeypatch.setattr(sessions_panel.claude_processes, "list_claude_processes",
                        lambda run=None: ClaudeProcessesResult(LISTED, "", ()))
    # The real wiring of the token and the hosted client, with the hosted host answering 401 to every request.
    sources = DataSources(
        probe=lambda url: local_client.HEALTH_NOT_RUNNING,
        read_server=lambda url: (_ for _ in ()).throw(AssertionError("server not running")),
        read_file=read_file,
        get_token=lambda: token_store.get_token(store=secrets),
        list_hosted=lambda url, token: hosted_client.list_projects(url, token, transport=refusing),
    )
    window = MainWindow(
        SettingsStore(settings_path),
        secret_store=secrets,
        data_sources=sources,
        tools_status=lambda: ToolsStatus(rows=()),
    )
    qtbot.addWidget(window)
    names = [window.tabs.tabText(i) for i in range(window.tabs.count())]
    window.tabs.setCurrentIndex(names.index("Projects"))
    window.show()
    qtbot.waitExposed(window)

    expected = f"Hosted Meridian projects could not be loaded. {hosted_client.REJECTED_TEXT}"
    projects = window.tabs.widget(names.index("Projects"))
    projects_status = projects.findChild(QLabel, "projects-status")
    qtbot.waitUntil(lambda: projects_status.text() == expected, timeout=5000)
    assert projects.findChild(QTableWidget, "projects-table").rowCount() == 0

    window.tabs.setCurrentIndex(names.index("Sessions"))
    sessions = window.tabs.widget(names.index("Sessions"))
    sessions_status = sessions.findChild(QLabel, "sessions-status")
    qtbot.waitUntil(lambda: sessions_status.text() == expected, timeout=5000)
    assert sessions.findChild(QTableWidget, "sessions-table").rowCount() == 0
    assert "HTTP 401" in projects_status.text() and "HTTP 401" in sessions_status.text()

    # The core file is there, and neither tab read it over the hosted refusal.
    assert file_reads == []
    assert file_session_reads == []
    assert local_calls == []
    assert len(refusing.calls) >= 2
    assert folder_state(core / "data") == before
    assert FAKE_TOKEN not in projects_status.text() + sessions_status.text()


def _projects_tab(qtbot, settings_path, sources):
    tab = ProjectsTab(SettingsStore(settings_path), sources)
    qtbot.addWidget(tab)
    tab.show()
    qtbot.waitExposed(tab)
    return tab, tab.findChild(QLabel, "projects-status")


def test_the_projects_screen_shows_the_snapshot_label_for_the_core_file(qtbot, settings_path, tmp_path):
    core = core_folder(tmp_path)
    SettingsStore(settings_path).save(local(core))

    tab, status = _projects_tab(qtbot, settings_path, Recorder().sources())

    expected = f"Showing 2 project(s) from the database file. {WRITTEN_NOTE}"
    qtbot.waitUntil(lambda: status.text() == expected, timeout=5000)
    assert status.isVisible()
    assert tab.findChild(QTableWidget, "projects-table").rowCount() == 2


def test_the_projects_screen_labels_an_empty_core_file_and_not_a_chosen_file(qtbot, settings_path, tmp_path):
    core = core_folder(tmp_path)
    SettingsStore(settings_path).save(local(core))
    empty = dataclasses.replace(Recorder().sources(), read_file=lambda path: [])

    _tab, status = _projects_tab(qtbot, settings_path, empty)
    qtbot.waitUntil(lambda: status.text() == f"No projects found in the database file. {WRITTEN_NOTE}", timeout=5000)

    # A database file the owner chose is not the core folder's copy, so it carries no label.
    SettingsStore(settings_path).save(local(core, make_core_db(tmp_path / "chosen")))
    _tab2, chosen_status = _projects_tab(qtbot, settings_path, Recorder().sources())
    qtbot.waitUntil(lambda: chosen_status.text() == "Showing 2 project(s) from the database file.", timeout=5000)


def test_the_window_has_no_projects_unavailable_path():
    # The Sessions tab gets the resolver's own message through data_source.ProjectsUnresolved. The window's old
    # ProjectsUnavailable exception was never raised, so it is gone.
    assert not hasattr(main_window, "ProjectsUnavailable")
    assert issubclass(ProjectsUnresolved, Exception)
