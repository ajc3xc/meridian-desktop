"""Tests for the hosted-projects backup: the backup file, the Projects source order, and the tab's backup button.

Every outside call is a fake that records its name. Nothing here reaches the hosted service, the OS keychain, a real
server or a real port. Each test that writes a backup points LOCALAPPDATA at its own temp folder, so no backup is left
in the session's folder for another test to read. FAKE_TOKEN is an obviously fake value, so a leak check can search
for it.
"""

from __future__ import annotations

import dataclasses
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PySide6.QtWidgets import QLabel, QPushButton, QTableWidget

from _fakes import MemoryKeyring
from meridian_desktop import data_source, hosted_client, local_client, projects_backup
from meridian_desktop.claude_processes import LISTED, ClaudeProcessesResult
from meridian_desktop.data_source import (
    SOURCE_BACKUP,
    SOURCE_FILE,
    SOURCE_HOSTED,
    SOURCE_SERVER,
    STATE_NO_LOCAL_DATA,
    STATE_NOT_SIGNED_IN,
    STATE_OK,
    STATE_READ_FAILED,
    DataSources,
    ProjectsProvider,
    ProjectsUnresolved,
    no_local_data_message,
    read_in_order,
    resolve,
)
from meridian_desktop.projects_backup import Backup
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.ui import sessions_panel
from meridian_desktop.ui.projects_tab import LOADING_TEXT, ProjectsTab, default_sources

FAKE_TOKEN = "sk_meridian_fake-backup-0005"
LOCAL_URL = "http://127.0.0.1:7878"
HOSTED_URL = "https://hosted.test"
TAKEN = datetime(2026, 10, 24, 23, 54, 24, tzinfo=timezone.utc)
LATER = datetime(2026, 10, 25, 8, 0, 0, tzinfo=timezone.utc)
LABEL = "Backup from 24 October 2026. Hosted did not answer."
BUTTON_TEXT = "Back up hosted projects to this computer"

# The hosted reply carries fields the backup must never keep: an executor config, agent notes and a secret-like value.
HOSTED_ROWS = [
    {
        "id": "h-1",
        "name": "Hosted one",
        "created_at": "2026-03-02T17:30:00",
        "status": "parked",
        "executor_config": {"secret": "FAKE-EXEC-VALUE"},
        "agent_instructions": "FAKE-AGENT-NOTE",
    },
    {"id": "h-2", "name": "Hosted two", "created_at": "2026-04-01T09:00:00"},
]
BACKUP_ROWS = [{"id": "b-1", "name": "Backed up one", "created_at": "2026-02-01T00:00:00"}]
FILE_ROWS = [{"id": "f-1", "name": "From file"}]
SERVER_ROWS = [{"id": "s-1", "name": "From server"}]


class Fake:
    """Canned callables for DataSources. Each one records its name in ``calls``. An ``*_error`` is raised instead."""

    def __init__(
        self,
        *,
        probe_state: str = local_client.HEALTH_NOT_RUNNING,
        token: str | None = None,
        hosted_error: BaseException | None = None,
        backup: Backup | None = None,
        backup_error: BaseException | None = None,
    ) -> None:
        self.probe_state = probe_state
        self.token = token
        self.hosted_error = hosted_error
        self.backup = backup
        self.backup_error = backup_error
        self.calls: list[str] = []
        self.hosted_args: list[tuple[str, str]] = []

    def sources(self) -> DataSources:
        return DataSources(
            probe=self._probe,
            read_server=self._read_server,
            read_file=self._read_file,
            get_token=self._get_token,
            list_hosted=self._list_hosted,
            read_backup=self._read_backup,
        )

    def _probe(self, url: str) -> str:
        self.calls.append("probe")
        return self.probe_state

    def _read_server(self, url: str) -> list:
        self.calls.append("read_server")
        return list(SERVER_ROWS)

    def _read_file(self, path: Path) -> list:
        self.calls.append("read_file")
        return list(FILE_ROWS)

    def _get_token(self) -> str | None:
        self.calls.append("get_token")
        return self.token

    def _list_hosted(self, base_url: str, token: str) -> list:
        self.calls.append("list_hosted")
        self.hosted_args.append((base_url, token))
        if self.hosted_error is not None:
            raise self.hosted_error
        return list(HOSTED_ROWS)

    def _read_backup(self) -> Backup | None:
        self.calls.append("read_backup")
        if self.backup_error is not None:
            raise self.backup_error
        return self.backup


class Steps:
    """The four steps read_in_order is given. Each records its name and answers as the test sets it. None is no answer."""

    def __init__(self, *, server=None, hosted=None, backup=None, file=None, token=None) -> None:
        self.answers = {"server": server, "hosted": hosted, "backup": backup, "file": file}
        self.token = token
        self.calls: list[str] = []

    def get_token(self):
        self.calls.append("get_token")
        return self.token

    def server(self):
        self.calls.append("server")
        return self.answers["server"]

    def hosted(self, token):
        self.calls.append(f"hosted:{token}")
        return self.answers["hosted"]

    def backup(self):
        self.calls.append("backup")
        return self.answers["backup"]

    def file(self):
        self.calls.append("file")
        return self.answers["file"]

    def run(self, mode):
        return read_in_order(
            mode, self.get_token, server=self.server, hosted=self.hosted, file=self.file, backup=self.backup
        )


@pytest.fixture
def db_file(tmp_path) -> Path:
    path = tmp_path / "meridian.db"
    path.write_bytes(b"")
    return path


@pytest.fixture
def local_folder(tmp_path, monkeypatch) -> Path:
    """A LOCALAPPDATA of this test's own, so the backup folder is this test's and goes away with it."""
    root = tmp_path / "local-app-data"
    monkeypatch.setenv("LOCALAPPDATA", str(root))
    return root / "meridian-desktop" / "backups"


def local(db_path: Path | None) -> Settings:
    return Settings(
        mode="local",
        local_url=LOCAL_URL,
        hosted_url=HOSTED_URL,
        db_path=None if db_path is None else str(db_path),
    )


def hosted_mode(db_path: Path | None) -> Settings:
    return dataclasses.replace(local(db_path), mode="hosted")


def write_backup_at(folder: Path, when: datetime, rows=BACKUP_ROWS) -> projects_backup.BackupWritten:
    return projects_backup.write_backup(rows, folder=folder, taken_at=when)


def write_raw_backup(folder: Path, when: datetime, projects: list) -> Path:
    """A backup file as an older build left it, written without the write path, so it may hold no projects at all."""
    folder.mkdir(parents=True, exist_ok=True)
    document = {"version": 1, "taken_at": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "projects": projects}
    path = folder / projects_backup.backup_name(when)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def hosted_answering(answer):
    """A hosted reader whose transport gives ``answer``: a hosted Reply, or an exception the transport raises."""

    def transport(request, timeout):
        if isinstance(answer, BaseException):
            raise answer
        return answer

    return lambda url, token: hosted_client.list_projects(url, token, transport=transport)


# ----------------------------------------------------------------- the backup file


def test_backup_file_holds_only_names_ids_and_dates_and_never_the_token(local_folder):
    seen: list[tuple[str, str]] = []

    def list_hosted(url, token):
        seen.append((url, token))
        return list(HOSTED_ROWS)

    written = projects_backup.backup_hosted(list_hosted, HOSTED_URL, FAKE_TOKEN, folder=local_folder, taken_at=TAKEN)

    assert seen == [(HOSTED_URL, FAKE_TOKEN)]
    data = json.loads(written.path.read_text(encoding="utf-8"))
    assert set(data) == {"version", "taken_at", "projects"}
    assert data["taken_at"] == "2026-10-24T23:54:24Z"
    assert [set(project) for project in data["projects"]] == [{"id", "name", "created_at"}] * 2
    assert data["projects"][0] == {"id": "h-1", "name": "Hosted one", "created_at": "2026-03-02T17:30:00"}
    text = written.path.read_text(encoding="utf-8")
    secrets = (FAKE_TOKEN, "FAKE-EXEC-VALUE", "FAKE-AGENT-NOTE", "executor_config", "agent_instructions", HOSTED_URL)
    for secret in secrets:
        assert secret not in text
    assert written.count == 2


def test_backup_is_named_by_its_utc_time_in_the_desktop_data_folder(tmp_path, monkeypatch):
    assert projects_backup.backup_name(TAKEN) == "projects-20261024T235424Z.json"

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    assert projects_backup.backup_dir() == tmp_path / "local-app-data" / "meridian-desktop" / "backups"
    # Without LOCALAPPDATA the folder sits beside settings.json in the home folder, as settings does.
    assert projects_backup.backup_dir(env={}, home=tmp_path) == tmp_path / ".meridian-desktop" / "backups"


def test_the_status_says_how_many_projects_and_the_day(local_folder):
    written = write_backup_at(local_folder, TAKEN, rows=HOSTED_ROWS)

    assert written.count == 2
    assert written.message == "Backed up 2 projects on 24 October 2026."
    assert projects_backup.backed_up_text(1, TAKEN) == "Backed up 1 project on 24 October 2026."
    assert projects_backup.backup_label(TAKEN) == LABEL


def test_the_newest_five_backups_are_kept_and_older_ones_are_removed(local_folder):
    # Seven writes, a day apart: the last five stay, and the two oldest are removed.
    times = [TAKEN + timedelta(days=day) for day in range(7)]
    for when in times:
        write_backup_at(local_folder, when)

    names = sorted(path.name for path in local_folder.iterdir())
    assert names == sorted(projects_backup.backup_name(when) for when in times[-5:])
    found = projects_backup.newest_backup(local_folder)
    assert found is not None and found.taken_at == times[-1]


@pytest.mark.parametrize(
    "answer",
    [[], [{"name": "no id"}, {"id": "   ", "name": "blank id"}, "not a row"]],
    ids=["empty-list", "no-usable-id"],
)
def test_an_empty_hosted_answer_writes_no_backup_and_keeps_the_good_one(local_folder, answer):
    good = write_backup_at(local_folder, TAKEN)

    with pytest.raises(projects_backup.NoUsableProjects):
        projects_backup.backup_hosted(lambda url, token: answer, HOSTED_URL, FAKE_TOKEN, folder=local_folder,
                                      taken_at=LATER)

    assert [path.name for path in local_folder.iterdir()] == [good.path.name]
    found = projects_backup.newest_backup(local_folder)
    assert found is not None and found.projects == BACKUP_ROWS


def test_an_empty_hosted_answer_on_the_backup_button_says_so_and_keeps_the_good_backup(qtbot, settings_path,
                                                                                     local_folder):
    good = write_backup_at(local_folder, TAKEN)
    fake = Fake(token=FAKE_TOKEN)
    sources = dataclasses.replace(fake.sources(), list_hosted=lambda url, token: [])
    tab, _status = _tab(qtbot, settings_path, sources)
    button = tab.findChild(QPushButton, "projects-backup-button")
    backup_status = tab.findChild(QLabel, "projects-backup-status")

    button.click()
    qtbot.waitUntil(lambda: backup_status.text() == projects_backup.NO_PROJECTS_TEXT, timeout=5000)

    assert [path.name for path in local_folder.iterdir()] == [good.path.name]
    assert button.isEnabled()


def test_retention_keeps_the_newest_backup_with_projects_behind_five_newer_empty_ones(local_folder):
    good = write_backup_at(local_folder, TAKEN)
    for hours in range(1, 6):
        write_raw_backup(local_folder, TAKEN + timedelta(hours=hours), [])

    projects_backup.prune_backups(local_folder)

    names = sorted(path.name for path in local_folder.iterdir())
    assert good.path.name in names
    assert len(names) == 6


def test_the_newest_backup_read_skips_newer_files_that_hold_no_projects(local_folder):
    write_backup_at(local_folder, TAKEN)
    for hours in range(1, 6):
        write_raw_backup(local_folder, TAKEN + timedelta(hours=hours), [])

    found = projects_backup.newest_backup(local_folder)

    assert found is not None
    assert found.taken_at == TAKEN
    assert found.projects == BACKUP_ROWS


def test_a_failed_hosted_read_writes_nothing_and_keeps_the_newest_backup(local_folder):
    kept = write_backup_at(local_folder, TAKEN)

    def unreachable(url, token):
        raise hosted_client.HostedUnreachable()

    with pytest.raises(hosted_client.HostedError):
        projects_backup.backup_hosted(unreachable, HOSTED_URL, FAKE_TOKEN, folder=local_folder, taken_at=LATER)

    assert [path.name for path in local_folder.iterdir()] == [kept.path.name]


def test_the_newest_backup_reads_back_its_names_and_dates_and_skips_a_damaged_newer_file(local_folder):
    write_backup_at(local_folder, TAKEN)
    (local_folder / "projects-20261025T080000Z.json").write_text("{not json", encoding="utf-8")

    found = projects_backup.newest_backup(local_folder)

    assert found is not None
    assert found.taken_at == TAKEN
    assert found.projects == BACKUP_ROWS


def test_no_backup_folder_means_no_backup(tmp_path):
    assert projects_backup.newest_backup(tmp_path / "never-made") is None


# ----------------------------------------------------------------- the source order


@pytest.mark.parametrize(
    ("mode", "steps", "expected", "calls"),
    [
        # 1. The local server answers: it is final, and nothing else is asked.
        ("local", {"server": "server-answer", "token": FAKE_TOKEN, "hosted": "h", "backup": "b", "file": "f"},
         "server-answer", ["server"]),
        # 2. No server, a token, and hosted answers: its answer is final. The backup and the file are not asked.
        ("local", {"token": FAKE_TOKEN, "hosted": "hosted-answer", "backup": "b", "file": "f"},
         "hosted-answer", ["server", "get_token", f"hosted:{FAKE_TOKEN}"]),
        # 3. Hosted does not answer: the backup is the next answer, and the file is not read while a token is saved.
        ("local", {"token": FAKE_TOKEN, "backup": "backup-answer", "file": "f"},
         "backup-answer", ["server", "get_token", f"hosted:{FAKE_TOKEN}", "backup"]),
        # 3, no backup: hosted's failure stands, as it does today, and the file is not read over it.
        ("local", {"token": FAKE_TOKEN, "file": "f"}, None, ["server", "get_token", f"hosted:{FAKE_TOKEN}", "backup"]),
        # 4. No token: the backup is not used, and the file is read as it is today.
        ("local", {"file": "file-answer"}, "file-answer", ["server", "get_token", "file"]),
        ("local", {"token": "   ", "file": "file-answer"}, "file-answer", ["server", "get_token", "file"]),
        ("local", {"file": None}, None, ["server", "get_token", "file"]),
        # Hosted and tunnel: hosted with a token, then the backup. The server and the file are never asked.
        ("hosted", {"token": FAKE_TOKEN, "hosted": "hosted-answer", "backup": "b", "server": "s"},
         "hosted-answer", ["get_token", f"hosted:{FAKE_TOKEN}"]),
        ("hosted", {"token": FAKE_TOKEN, "backup": "backup-answer", "file": "f"},
         "backup-answer", ["get_token", f"hosted:{FAKE_TOKEN}", "backup"]),
        ("tunnel", {"token": FAKE_TOKEN, "backup": "backup-answer", "file": "f", "server": "s"},
         "backup-answer", ["get_token", f"hosted:{FAKE_TOKEN}", "backup"]),
        ("tunnel", {"token": FAKE_TOKEN}, None, ["get_token", f"hosted:{FAKE_TOKEN}", "backup"]),
        # Hosted and tunnel with no token: nothing else is read, the backup included.
        ("hosted", {"backup": "b", "server": "s"}, None, ["get_token"]),
    ],
)
def test_the_source_order_is_the_four_steps(mode, steps, expected, calls):
    order = Steps(**steps)

    assert order.run(mode) == expected
    assert order.calls == calls


def test_read_in_order_still_refuses_an_unknown_mode():
    order = Steps(server="server-answer", token=FAKE_TOKEN)

    with pytest.raises(ValueError):
        order.run("cloud")
    assert order.calls == []


def test_a_running_server_is_answered_first_and_nothing_else_is_read(db_file):
    fake = Fake(probe_state=local_client.HEALTH_RUNNING, token=FAKE_TOKEN, backup=Backup(BACKUP_ROWS, TAKEN))

    result = resolve(local(db_file), fake.sources())

    assert result.source == SOURCE_SERVER
    assert fake.calls == ["probe", "read_server"]


def test_a_working_hosted_read_is_final_and_the_backup_is_not_read(db_file):
    fake = Fake(token=FAKE_TOKEN, backup=Backup(BACKUP_ROWS, TAKEN))

    result = resolve(local(db_file), fake.sources())

    assert result.state == STATE_OK
    assert result.source == SOURCE_HOSTED
    assert fake.calls == ["probe", "get_token", "list_hosted"]


def test_a_hosted_failure_falls_back_to_the_backup_and_the_label_says_so(db_file):
    fake = Fake(token=FAKE_TOKEN, hosted_error=hosted_client.HostedUnreachable(), backup=Backup(BACKUP_ROWS, TAKEN))

    result = resolve(local(db_file), fake.sources())

    assert result.state == STATE_OK
    assert result.source == SOURCE_BACKUP
    assert result.rows == BACKUP_ROWS
    assert result.message == LABEL
    assert fake.calls == ["probe", "get_token", "list_hosted", "read_backup"]
    assert "read_file" not in fake.calls
    assert FAKE_TOKEN not in repr(result)


# A hosted reply that is an answer, not a failure to reach hosted: the token is refused, the page is missing, the
# reply is a redirect or is not the projects list. The backup is not used for any of these: the reason is shown.
ANSWERS_SHOWN = [
    hosted_client.Reply(401, b""),
    hosted_client.Reply(403, b""),
    hosted_client.Reply(404, b""),
    hosted_client.Reply(302, b""),
    hosted_client.Reply(200, b"not json"),
]


@pytest.mark.parametrize("answer", ANSWERS_SHOWN, ids=["401", "403", "404", "redirect", "bad-body"])
def test_a_hosted_answer_that_refuses_is_shown_and_no_backup_is_used(db_file, answer):
    fake = Fake(token=FAKE_TOKEN, backup=Backup(BACKUP_ROWS, TAKEN))
    sources = dataclasses.replace(fake.sources(), list_hosted=hosted_answering(answer))
    with pytest.raises(hosted_client.HostedError) as raised:
        hosted_answering(answer)(HOSTED_URL, FAKE_TOKEN)

    result = resolve(local(db_file), sources)

    assert result.state == STATE_READ_FAILED
    assert result.source == SOURCE_HOSTED
    assert result.rows == []
    assert result.message == f"Hosted Meridian projects could not be loaded. {raised.value}"
    assert "read_backup" not in fake.calls
    assert "read_file" not in fake.calls
    assert FAKE_TOKEN not in repr(result)


def test_a_hosted_401_is_the_auth_message_on_the_hosted_path_too(db_file):
    fake = Fake(token=FAKE_TOKEN, backup=Backup(BACKUP_ROWS, TAKEN))
    sources = dataclasses.replace(fake.sources(), list_hosted=hosted_answering(hosted_client.Reply(401, b"")))

    result = resolve(hosted_mode(db_file), sources)

    assert result.state == STATE_READ_FAILED
    assert result.message == f"Hosted Meridian projects could not be loaded. {hosted_client.REJECTED_TEXT}"
    assert "read_backup" not in fake.calls


# A hosted failure that the backup stands in for: the network failed (refused, timed out, lost) or the server errored.
NO_ANSWER_FROM_HOSTED = [
    ConnectionRefusedError("refused"),
    TimeoutError("slow"),
    hosted_client.Reply(500, b""),
    hosted_client.Reply(503, b""),
]


@pytest.mark.parametrize("answer", NO_ANSWER_FROM_HOSTED, ids=["refused", "timeout", "http-500", "http-503"])
def test_a_refused_connection_or_a_server_error_shows_the_backup_and_its_label(db_file, answer):
    fake = Fake(token=FAKE_TOKEN, backup=Backup(BACKUP_ROWS, TAKEN))
    sources = dataclasses.replace(fake.sources(), list_hosted=hosted_answering(answer))

    result = resolve(local(db_file), sources)

    assert result.state == STATE_OK
    assert result.source == SOURCE_BACKUP
    assert result.rows == BACKUP_ROWS
    assert result.message == LABEL
    assert "read_file" not in fake.calls


def test_a_raw_refused_connection_from_the_hosted_reader_shows_the_backup(db_file):
    # An injected reader may raise the socket error itself, not the hosted client's typed error. It is still a refusal.
    fake = Fake(token=FAKE_TOKEN, hosted_error=ConnectionRefusedError("refused"), backup=Backup(BACKUP_ROWS, TAKEN))

    result = resolve(local(db_file), fake.sources())

    assert result.source == SOURCE_BACKUP
    assert result.message == LABEL
    assert fake.calls == ["probe", "get_token", "list_hosted", "read_backup"]


def test_an_unexpected_error_from_the_hosted_reader_is_shown_and_no_backup_is_used(db_file):
    fake = Fake(token=FAKE_TOKEN, hosted_error=RuntimeError("offline"), backup=Backup(BACKUP_ROWS, TAKEN))

    result = resolve(local(db_file), fake.sources())

    assert result.state == STATE_READ_FAILED
    assert result.source == SOURCE_HOSTED
    assert result.message == "Hosted Meridian projects could not be loaded (RuntimeError)."
    assert "read_backup" not in fake.calls


def test_a_hosted_failure_in_hosted_mode_also_falls_back_to_the_backup(db_file):
    fake = Fake(token=FAKE_TOKEN, hosted_error=hosted_client.HostedUnreachable(), backup=Backup(BACKUP_ROWS, TAKEN))

    result = resolve(hosted_mode(db_file), fake.sources())

    assert result.source == SOURCE_BACKUP
    assert result.message == LABEL
    assert fake.calls == ["get_token", "list_hosted", "read_backup"]


def test_no_backup_and_a_hosted_failure_gives_the_existing_hosted_message(db_file):
    fake = Fake(token=FAKE_TOKEN, hosted_error=RuntimeError("offline"))

    result = resolve(local(db_file), fake.sources())

    assert result.state == STATE_READ_FAILED
    assert result.source == SOURCE_HOSTED
    assert result.message == "Hosted Meridian projects could not be loaded (RuntimeError)."
    assert "read_file" not in fake.calls


def test_a_backup_that_cannot_be_read_is_no_backup(db_file):
    fake = Fake(
        token=FAKE_TOKEN, hosted_error=hosted_client.HostedUnreachable(), backup_error=OSError("cannot read")
    )

    result = resolve(local(db_file), fake.sources())

    assert result.state == STATE_READ_FAILED
    assert result.source == SOURCE_HOSTED
    assert result.message == f"Hosted Meridian projects could not be loaded. {hosted_client.UNREACHABLE_TEXT}"
    assert "cannot read" not in result.message


def test_no_backup_no_server_and_no_token_gives_the_existing_no_data_message(tmp_path):
    settings = local(None)
    fake = Fake()

    result = resolve(settings, fake.sources())

    assert result.state == STATE_NO_LOCAL_DATA
    assert result.message == no_local_data_message(settings)
    assert fake.calls == ["probe", "get_token"]


def test_hosted_mode_without_a_token_is_not_signed_in_and_reads_no_backup(db_file):
    fake = Fake(token=None, backup=Backup(BACKUP_ROWS, TAKEN))

    result = resolve(hosted_mode(db_file), fake.sources())

    assert result.state == STATE_NOT_SIGNED_IN
    assert fake.calls == ["get_token"]


def test_the_sessions_picker_never_lists_a_backup_project_when_hosted_is_down(db_file):
    fake = Fake(token=FAKE_TOKEN, hosted_error=hosted_client.HostedUnreachable(), backup=Backup(BACKUP_ROWS, TAKEN))
    provider = ProjectsProvider(lambda: local(db_file), fake.sources())

    with pytest.raises(ProjectsUnresolved) as raised:
        provider()

    assert raised.value.message == f"Hosted Meridian projects could not be loaded. {hosted_client.UNREACHABLE_TEXT}"
    assert "read_backup" not in fake.calls


def test_the_sessions_panel_picker_has_no_backup_project_when_hosted_is_down(db_file):
    fake = Fake(token=FAKE_TOKEN, hosted_error=hosted_client.HostedUnreachable(), backup=Backup(BACKUP_ROWS, TAKEN))
    provider = ProjectsProvider(lambda: local(db_file), fake.sources())
    reads = sessions_panel._Reads(  # noqa: SLF001 - the picker's read, as the Sessions tab runs it on its worker
        lambda: LOCAL_URL, lambda: (HOSTED_URL, FAKE_TOKEN), provider, lambda: "local", None, None,
        lambda: ClaudeProcessesResult(LISTED, "", ()),
    )

    choices, message, _local_answered = reads._read_projects()  # noqa: SLF001

    assert choices is None
    assert message == f"Hosted Meridian projects could not be loaded. {hosted_client.UNREACHABLE_TEXT}"
    assert "Backed up one" not in message
    assert "read_backup" not in fake.calls


def test_the_sessions_picker_lists_hosted_projects_when_hosted_answers(db_file):
    fake = Fake(token=FAKE_TOKEN, backup=Backup(BACKUP_ROWS, TAKEN))
    provider = ProjectsProvider(lambda: local(db_file), fake.sources())

    assert provider() == [("h-1", "Hosted one"), ("h-2", "Hosted two")]


def test_the_real_wiring_reads_the_newest_backup_from_the_data_folder(local_folder):
    write_backup_at(local_folder, TAKEN)
    secrets = KeyringSecretStore(backend=MemoryKeyring())

    found = default_sources(secrets).read_backup()

    assert found is not None
    assert found.projects == BACKUP_ROWS
    assert found.taken_at == TAKEN


# ----------------------------------------------------------------- the Projects tab


def _tab(qtbot, settings_path, sources):
    """A Projects tab that has finished its first load. Returns the tab and its status label."""
    SettingsStore(settings_path).save(Settings(mode="local", hosted_url=HOSTED_URL, local_url=LOCAL_URL))
    tab = ProjectsTab(SettingsStore(settings_path), sources)
    qtbot.addWidget(tab)
    tab.refresh()
    status = tab.findChild(QLabel, "projects-status")
    qtbot.waitUntil(lambda: status.text() != LOADING_TEXT, timeout=5000)
    return tab, status


def test_no_saved_token_means_no_backup_button(qtbot, settings_path):
    tab, _status = _tab(qtbot, settings_path, Fake(token=None).sources())

    assert tab.findChildren(QPushButton) == []
    assert tab.findChild(QLabel, "projects-backup-status") is None


def test_a_saved_token_shows_the_backup_button(qtbot, settings_path):
    tab, _status = _tab(qtbot, settings_path, Fake(token=FAKE_TOKEN).sources())

    button = tab.findChild(QPushButton, "projects-backup-button")
    assert button is not None
    assert not button.isHidden()
    assert button.text() == BUTTON_TEXT


def test_the_backup_button_writes_the_file_and_says_how_many(qtbot, settings_path, local_folder):
    fake = Fake(token=FAKE_TOKEN)
    tab, _status = _tab(qtbot, settings_path, fake.sources())
    button = tab.findChild(QPushButton, "projects-backup-button")
    backup_status = tab.findChild(QLabel, "projects-backup-status")

    button.click()
    qtbot.waitUntil(lambda: backup_status.text().startswith("Backed up"), timeout=5000)

    assert re.fullmatch(r"Backed up 2 projects on \d{1,2} [A-Z][a-z]+ \d{4}\.", backup_status.text())
    assert fake.hosted_args[-1] == (HOSTED_URL, FAKE_TOKEN)
    files = sorted(local_folder.glob("projects-*.json"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert FAKE_TOKEN not in text
    assert FAKE_TOKEN not in backup_status.text()
    assert "FAKE-EXEC-VALUE" not in text
    assert button.isEnabled()


def test_a_hosted_failure_shows_the_newest_backup_and_its_label(qtbot, settings_path, local_folder):
    write_backup_at(local_folder, TAKEN)
    # The real backup reader, so the file written to the data folder is the one the tab shows.
    fake = Fake(token=FAKE_TOKEN, hosted_error=hosted_client.HostedUnreachable())
    sources = dataclasses.replace(fake.sources(), read_backup=projects_backup.newest_backup)

    tab, status = _tab(qtbot, settings_path, sources)

    assert status.text() == f"Showing 1 project(s) from the backup on this computer. {LABEL}"
    table = tab.findChild(QTableWidget, "projects-table")
    assert table.rowCount() == 1
    assert table.item(0, 0).text() == "Backed up one"


def test_a_hosted_failure_with_no_backup_shows_the_hosted_message_and_no_rows(qtbot, settings_path, local_folder):
    fake = Fake(token=FAKE_TOKEN, hosted_error=RuntimeError("offline"))

    tab, status = _tab(qtbot, settings_path, fake.sources())

    assert status.text() == "Hosted Meridian projects could not be loaded (RuntimeError)."
    assert tab.findChild(QTableWidget, "projects-table").rowCount() == 0
    assert "read_file" not in fake.calls


def test_a_hosted_401_on_the_projects_tab_shows_the_auth_message_and_no_backup(qtbot, settings_path, local_folder):
    write_backup_at(local_folder, TAKEN)
    fake = Fake(token=FAKE_TOKEN)
    sources = dataclasses.replace(
        fake.sources(),
        list_hosted=hosted_answering(hosted_client.Reply(401, b"")),
        read_backup=projects_backup.newest_backup,
    )

    tab, status = _tab(qtbot, settings_path, sources)

    assert status.text() == f"Hosted Meridian projects could not be loaded. {hosted_client.REJECTED_TEXT}"
    assert LABEL not in status.text()
    assert tab.findChild(QTableWidget, "projects-table").rowCount() == 0
