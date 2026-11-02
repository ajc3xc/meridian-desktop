"""Choosing which hosted projects to back up, and stating the backup folder in Settings (board item 2ec36c33).

The Projects tab offers a checklist of the hosted projects: all are checked, and the owner unchecks the ones to leave out.
Only the checked projects are written. "Back up hosted projects" still writes every project in one run. The Settings
connection section states the same backup folder as the Projects tab.

Every outside call is a fake. Nothing reaches the hosted service, the OS keychain or a real port. Each test points
LOCALAPPDATA at its own temp folder. FAKE_TOKEN is an obviously fake value, so a leak check can search for it.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import QLabel, QListWidget, QPushButton, QWidget

from _fakes import MemoryKeyring
from meridian_desktop import hosted_client, local_client, projects_backup, token_store
from meridian_desktop.data_source import DataSources
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.ui.projects_tab import BACKUP_FAILED_TEXT, LOADING_TEXT, ProjectsTab
from meridian_desktop.ui.settings_tab import SettingsTab

FAKE_TOKEN = "sk_meridian_fake-choice-0006"
LOCAL_URL = "http://127.0.0.1:7878"
HOSTED_URL = "https://hosted.test"
TAKEN = datetime(2026, 10, 24, 23, 54, 24, tzinfo=timezone.utc)

# The hosted reply carries a field the backup must never keep.
HOSTED_ROWS = [
    {"id": "h-1", "name": "Hosted one", "created_at": "2026-03-02T17:30:00", "executor_config": {"secret": "FAKE-EXEC-VALUE"}},
    {"id": "h-2", "name": "Hosted two", "created_at": "2026-04-01T09:00:00"},
    {"id": "h-3", "name": "Hosted three", "created_at": "2026-05-01T09:00:00"},
]
CHOOSE_TEXT = "Choose projects to back up"
ACCOUNT_EMAIL = "fake-owner@example.test"
IDENTITY_UNKNOWN_TEXT = "Could not confirm which hosted account is signed in, so no project list was read. Try again."
WRITE_CHECKED_TEXT = "Back up checked projects"


@pytest.fixture
def local_folder(tmp_path, monkeypatch) -> Path:
    """A LOCALAPPDATA of this test's own, so the backup folder is this test's and goes away with it."""
    root = tmp_path / "local-app-data"
    monkeypatch.setenv("LOCALAPPDATA", str(root))
    return root / "meridian-desktop" / "backups"


FULL_GLOB = "projects-*.json"
SUBSET_GLOB = "subset-projects-*.json"


def _written_ids(folder: Path, glob: str = FULL_GLOB) -> list[str]:
    files = sorted(folder.glob(glob))
    assert len(files) == 1
    document = json.loads(files[0].read_text(encoding="utf-8"))
    return [row["id"] for row in document["projects"]]


# ----------------------------------------------------------------- the rules (pure)


def test_kept_projects_keeps_only_the_three_fields_and_drops_a_row_with_no_id():
    rows = HOSTED_ROWS + [{"name": "No id"}, {"id": "  ", "name": "Blank id"}]

    kept = projects_backup.kept_projects(rows)

    assert [row["id"] for row in kept] == ["h-1", "h-2", "h-3"]
    assert all(set(row) == {"id", "name", "created_at"} for row in kept)
    assert "FAKE-EXEC-VALUE" not in repr(kept)


def test_with_no_selection_every_project_is_written(tmp_path):
    written = projects_backup.write_backup(HOSTED_ROWS, folder=tmp_path / "b", taken_at=TAKEN)

    assert written.count == 3
    assert _written_ids(tmp_path / "b") == ["h-1", "h-2", "h-3"]


def test_only_the_chosen_ids_are_written(tmp_path):
    written = projects_backup.write_backup(
        HOSTED_ROWS, folder=tmp_path / "b", taken_at=TAKEN, only=["h-1", "h-3"]
    )

    assert written.count == 2
    assert written.message.startswith("Backed up 2 projects")
    assert _written_ids(tmp_path / "b", SUBSET_GLOB) == ["h-1", "h-3"]


@pytest.mark.parametrize("only", [[], ["not-a-project"]])
def test_an_empty_or_unknown_selection_writes_nothing_and_removes_nothing(tmp_path, only):
    folder = tmp_path / "b"
    for day in range(3):
        projects_backup.write_backup(HOSTED_ROWS, folder=folder, taken_at=TAKEN + timedelta(days=day))
    before = sorted(path.name for path in folder.iterdir())

    with pytest.raises(projects_backup.NoUsableProjects):
        projects_backup.write_backup(HOSTED_ROWS, folder=folder, taken_at=TAKEN, only=only)

    assert len(before) == 3
    assert sorted(path.name for path in folder.iterdir()) == before


def test_subset_writes_never_delete_a_full_backup(tmp_path):
    """A subset holds only some projects. Its writes must not evict the full backups, which may be the only copy."""
    folder = tmp_path / "b"
    full = [
        projects_backup.write_backup(HOSTED_ROWS, folder=folder, taken_at=TAKEN + timedelta(days=day))
        for day in range(2)
    ]
    for step in range(projects_backup.SUBSET_BACKUPS_KEPT + 3):
        projects_backup.write_backup(
            HOSTED_ROWS, folder=folder, taken_at=TAKEN + timedelta(days=10 + step), only=["h-2"]
        )

    names = {path.name for path in folder.iterdir()}
    assert {written.path.name for written in full} <= names
    assert len(list(folder.glob(SUBSET_GLOB))) == projects_backup.SUBSET_BACKUPS_KEPT


def test_full_backups_keep_their_own_cap_beside_subset_backups(tmp_path):
    folder = tmp_path / "b"
    newest_full = []
    for step in range(projects_backup.BACKUPS_KEPT + 2):
        newest_full.append(
            projects_backup.write_backup(HOSTED_ROWS, folder=folder, taken_at=TAKEN + timedelta(days=step)).path.name
        )
        projects_backup.write_backup(
            HOSTED_ROWS, folder=folder, taken_at=TAKEN + timedelta(days=step, hours=1), only=["h-1"]
        )

    kept_full = sorted(path.name for path in folder.glob(FULL_GLOB))
    assert kept_full == sorted(newest_full[-projects_backup.BACKUPS_KEPT:])
    assert len(list(folder.glob(SUBSET_GLOB))) == projects_backup.SUBSET_BACKUPS_KEPT


# ----------------------------------------------------------------- the Projects tab (Qt)


def _sources(*, list_hosted) -> DataSources:
    """Every outside read is a fake. The local server is not running and there is no database file."""
    return DataSources(
        probe=lambda url: local_client.HEALTH_NOT_RUNNING,
        read_server=lambda url: [],
        read_file=lambda path: [],
        get_token=lambda: FAKE_TOKEN,
        list_hosted=list_hosted,
        read_account=lambda url, token: ACCOUNT_EMAIL,
    )


def _tab(qtbot, settings_path, list_hosted):
    """A Projects tab that has finished its first load, with a saved hosted token."""
    SettingsStore(settings_path).save(Settings(mode="local", hosted_url=HOSTED_URL, local_url=LOCAL_URL))
    tab = ProjectsTab(SettingsStore(settings_path), _sources(list_hosted=list_hosted))
    qtbot.addWidget(tab)
    tab.refresh()
    status = tab.findChild(QLabel, "projects-status")
    qtbot.waitUntil(lambda: status.text() != LOADING_TEXT, timeout=5000)
    return tab


def _checklist(tab) -> QListWidget:
    return tab.findChild(QListWidget, "projects-backup-checklist")


def _choice_box(tab) -> QWidget:
    """The box that holds the checklist and its write button. It is hidden until a Choose read has finished."""
    return _checklist(tab).parentWidget()


def _choose(qtbot, tab) -> QListWidget:
    """Press Choose, then wait for the checklist to show. Returns the checklist."""
    tab.findChild(QPushButton, "projects-backup-choose").click()
    qtbot.waitUntil(lambda: not _choice_box(tab).isHidden(), timeout=5000)
    return _checklist(tab)


def test_choose_reads_the_hosted_list_once_and_checks_every_project(qtbot, settings_path, local_folder):
    reads: list[tuple[str, str]] = []

    def list_hosted(url, token):
        reads.append((url, token))
        return list(HOSTED_ROWS)

    tab = _tab(qtbot, settings_path, list_hosted)
    checklist = _choose(qtbot, tab)

    assert reads == [(HOSTED_URL, FAKE_TOKEN)]
    labels = [checklist.item(row).text() for row in range(checklist.count())]
    assert labels == ["Hosted one", "Hosted two", "Hosted three"]
    assert all(checklist.item(row).checkState() == Qt.CheckState.Checked for row in range(checklist.count()))
    assert list(local_folder.glob("projects-*.json")) == []
    choose = tab.findChild(QPushButton, "projects-backup-choose")
    assert choose.text() == CHOOSE_TEXT
    write = tab.findChild(QPushButton, "projects-backup-write-checked")
    assert write.text() == WRITE_CHECKED_TEXT and write.isEnabled()


def test_only_the_checked_projects_are_written(qtbot, settings_path, local_folder):
    tab = _tab(qtbot, settings_path, lambda url, token: list(HOSTED_ROWS))
    checklist = _choose(qtbot, tab)
    checklist.item(1).setCheckState(Qt.CheckState.Unchecked)  # leave out "Hosted two"
    write = tab.findChild(QPushButton, "projects-backup-write-checked")
    backup_status = tab.findChild(QLabel, "projects-backup-status")

    write.click()
    qtbot.waitUntil(lambda: backup_status.text().startswith("Backed up"), timeout=5000)

    assert _written_ids(local_folder, SUBSET_GLOB) == ["h-1", "h-3"]
    assert backup_status.text().startswith("Backed up 2 projects")
    written_text = next(local_folder.glob(SUBSET_GLOB)).read_text(encoding="utf-8")
    assert FAKE_TOKEN not in written_text and "FAKE-EXEC-VALUE" not in written_text
    assert write.isEnabled()


def test_the_write_button_is_off_while_nothing_is_checked(qtbot, settings_path, local_folder):
    tab = _tab(qtbot, settings_path, lambda url, token: list(HOSTED_ROWS))
    checklist = _choose(qtbot, tab)
    write = tab.findChild(QPushButton, "projects-backup-write-checked")

    assert write.isEnabled()
    for row in range(checklist.count()):
        checklist.item(row).setCheckState(Qt.CheckState.Unchecked)

    assert not write.isEnabled()
    assert list(local_folder.glob("projects-*.json")) == []


def test_an_empty_hosted_answer_shows_no_checklist_and_writes_nothing(qtbot, settings_path, local_folder):
    tab = _tab(qtbot, settings_path, lambda url, token: [])
    backup_status = tab.findChild(QLabel, "projects-backup-status")

    tab.findChild(QPushButton, "projects-backup-choose").click()
    qtbot.waitUntil(lambda: backup_status.text() == projects_backup.NO_PROJECTS_TEXT, timeout=5000)

    assert _choice_box(tab).isHidden()
    assert _checklist(tab).count() == 0
    assert list(local_folder.glob("projects-*.json")) == []


def test_a_failed_choose_read_names_the_failure_and_writes_nothing(qtbot, settings_path, local_folder):
    def unreachable(url, token):
        raise hosted_client.HostedUnreachable()

    tab = _tab(qtbot, settings_path, unreachable)
    backup_status = tab.findChild(QLabel, "projects-backup-status")

    tab.findChild(QPushButton, "projects-backup-choose").click()
    qtbot.waitUntil(lambda: backup_status.text().startswith("Projects could not be backed up"), timeout=5000)

    assert FAKE_TOKEN not in backup_status.text()
    assert list(local_folder.glob("projects-*.json")) == []


# ----------------------------------------------------------------- Settings states the folder


def test_the_settings_connection_section_states_the_backup_folder(qtbot, settings_path, local_folder):
    SettingsStore(settings_path).save(Settings(mode="local", hosted_url=HOSTED_URL, local_url=LOCAL_URL))
    tab = SettingsTab(
        SettingsStore(settings_path),
        secrets=KeyringSecretStore(backend=MemoryKeyring()),
        list_hosted=lambda url, token: [],
    )
    qtbot.addWidget(tab)

    destination = tab.findChild(QLabel, "settings-backup-destination")

    assert destination is not None
    assert destination.text() == projects_backup.destination_text(local_folder)


# ----------------------------------------------------------------- a stale checklist never stays writable


class _Clock:
    """A clock the test moves by hand. The tab reads it for a list's age, so no test waits in real time."""

    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


CLOCK_START = 1_800_000_000.0
EXPIRED_TEXT = "The list is older than 10 minutes. Choose again."


def _tab_with_token(qtbot, settings_path, holder, list_hosted, clock=None):
    """A Projects tab whose saved token is read from ``holder``, so a test can change or remove it between loads."""
    SettingsStore(settings_path).save(Settings(mode="local", hosted_url=HOSTED_URL, local_url=LOCAL_URL))
    sources = DataSources(
        probe=lambda url: local_client.HEALTH_NOT_RUNNING,
        read_server=lambda url: [],
        read_file=lambda path: [],
        get_token=lambda: _read_holder_token(holder),
        list_hosted=list_hosted,
        read_account=lambda url, token: holder.get("email", ACCOUNT_EMAIL),
    )
    tab = ProjectsTab(SettingsStore(settings_path), sources, clock=clock if clock is not None else _Clock(CLOCK_START))
    qtbot.addWidget(tab)
    tab.refresh()
    status = tab.findChild(QLabel, "projects-status")
    qtbot.waitUntil(lambda: status.text() != LOADING_TEXT, timeout=5000)
    return tab


def _press_choose(qtbot, tab, expected_status: str) -> None:
    backup_status = tab.findChild(QLabel, "projects-backup-status")
    tab.findChild(QPushButton, "projects-backup-choose").click()
    qtbot.waitUntil(lambda: backup_status.text().startswith(expected_status), timeout=5000)


def _write_button(tab) -> QPushButton:
    return tab.findChild(QPushButton, "projects-backup-write-checked")


def test_an_empty_choose_clears_the_checklist_and_disables_write(qtbot, settings_path, local_folder):
    answers = iter([list(HOSTED_ROWS), []])
    tab = _tab_with_token(qtbot, settings_path, {"token": FAKE_TOKEN}, lambda url, token: next(answers))
    _choose(qtbot, tab)
    assert _checklist(tab).count() == 3 and _write_button(tab).isEnabled()

    _press_choose(qtbot, tab, projects_backup.NO_PROJECTS_TEXT)

    assert _checklist(tab).count() == 0
    assert _choice_box(tab).isHidden()
    assert not _write_button(tab).isEnabled()
    assert list(local_folder.glob("projects-*.json")) == []


def test_a_failed_choose_clears_the_checklist_and_disables_write(qtbot, settings_path, local_folder):
    answers = iter([list(HOSTED_ROWS), hosted_client.HostedUnreachable()])

    def list_hosted(url, token):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    tab = _tab_with_token(qtbot, settings_path, {"token": FAKE_TOKEN}, list_hosted)
    _choose(qtbot, tab)

    _press_choose(qtbot, tab, "Projects could not be backed up")

    assert _checklist(tab).count() == 0
    assert _choice_box(tab).isHidden()
    assert not _write_button(tab).isEnabled()


def test_a_refresh_for_a_different_hosted_url_clears_the_checklist(qtbot, settings_path, local_folder):
    tab = _tab_with_token(qtbot, settings_path, {"token": FAKE_TOKEN}, lambda url, token: list(HOSTED_ROWS))
    _choose(qtbot, tab)

    SettingsStore(settings_path).save(Settings(mode="local", hosted_url="https://other.test", local_url=LOCAL_URL))
    tab.refresh()
    status = tab.findChild(QLabel, "projects-status")
    qtbot.waitUntil(lambda: status.text() != LOADING_TEXT, timeout=5000)

    assert _checklist(tab).count() == 0
    assert _choice_box(tab).isHidden()
    assert not _write_button(tab).isEnabled()


def test_a_new_token_for_the_same_account_keeps_the_checklist_and_resets_nothing(qtbot, settings_path, local_folder):
    """The checklist is bound to the account, not the token. A re-sign-in to the same account changes nothing."""
    holder = {"token": FAKE_TOKEN}
    tab = _tab_with_token(qtbot, settings_path, holder, lambda url, token: list(HOSTED_ROWS))
    _choose(qtbot, tab)
    status_before = tab.findChild(QLabel, "projects-backup-status").text()

    holder["token"] = "sk_meridian_fake-same-account-0009"
    tab.refresh()
    status = tab.findChild(QLabel, "projects-status")
    qtbot.waitUntil(lambda: status.text() != LOADING_TEXT, timeout=5000)

    assert _checklist(tab).count() == 3
    assert _write_button(tab).isEnabled()
    assert tab.findChild(QLabel, "projects-backup-status").text() == status_before


def test_a_refresh_for_a_different_account_clears_the_checklist(qtbot, settings_path, local_folder):
    holder = {"token": FAKE_TOKEN}
    tab = _tab_with_token(qtbot, settings_path, holder, lambda url, token: list(HOSTED_ROWS))
    _choose(qtbot, tab)

    holder["email"] = "fake-other-owner@example.test"
    tab.refresh()
    status = tab.findChild(QLabel, "projects-status")
    qtbot.waitUntil(lambda: status.text() != LOADING_TEXT, timeout=5000)

    assert _checklist(tab).count() == 0
    assert not _write_button(tab).isEnabled()


def test_a_choose_is_refused_and_says_so_when_the_account_cannot_be_read(qtbot, settings_path, local_folder):
    reads: list[str] = []

    def list_hosted(url, token):
        reads.append(url)
        return list(HOSTED_ROWS)

    holder = {"token": FAKE_TOKEN, "email": None}
    tab = _tab_with_token(qtbot, settings_path, holder, list_hosted)
    backup_status = tab.findChild(QLabel, "projects-backup-status")

    tab.findChild(QPushButton, "projects-backup-choose").click()
    qtbot.waitUntil(lambda: backup_status.text() == IDENTITY_UNKNOWN_TEXT, timeout=5000)

    assert reads == []
    assert _checklist(tab).count() == 0
    assert list(local_folder.glob("projects-*.json")) == []


def test_the_account_key_is_the_normalized_url_and_identity_not_the_token():
    same = projects_backup.account_key("https://Usemeridian.US/", ACCOUNT_EMAIL)
    assert same == projects_backup.account_key("https://usemeridian.us", ACCOUNT_EMAIL)
    assert same != projects_backup.account_key("https://usemeridian.us", "fake-other-owner@example.test")
    assert same != projects_backup.account_key("https://other.test", ACCOUNT_EMAIL)


def test_a_choose_that_finishes_after_the_token_is_removed_stays_hidden(qtbot, settings_path, local_folder):
    """The race: a Choose is in flight, the saved token is removed, and then the Choose answers. Its checklist must not show."""
    release = threading.Event()
    holder = {"token": FAKE_TOKEN}

    def slow_list(url, token):
        release.wait(timeout=5)
        return list(HOSTED_ROWS)

    tab = _tab_with_token(qtbot, settings_path, holder, slow_list)
    tab.findChild(QPushButton, "projects-backup-choose").click()

    holder["token"] = None
    tab.refresh()
    status = tab.findChild(QLabel, "projects-status")
    qtbot.waitUntil(lambda: status.text() != LOADING_TEXT, timeout=5000)

    release.set()
    qtbot.waitUntil(lambda: not tab._backup_running, timeout=5000)

    assert _checklist(tab).count() == 0
    assert _choice_box(tab).isHidden()
    assert list(local_folder.glob("projects-*.json")) == []


def test_the_no_token_branch_of_choose_reads_nothing_and_says_so(settings_path):
    from meridian_desktop.ui.projects_tab import _choose_work

    SettingsStore(settings_path).save(Settings(mode="local", hosted_url=HOSTED_URL, local_url=LOCAL_URL))
    reads: list[str] = []

    def list_hosted(url, token):
        reads.append(url)
        return list(HOSTED_ROWS)

    sources = _sources(list_hosted=list_hosted)
    sources = DataSources(
        probe=sources.probe,
        read_server=sources.read_server,
        read_file=sources.read_file,
        get_token=lambda: None,
        list_hosted=list_hosted,
    )

    chosen = _choose_work(SettingsStore(settings_path), sources, 1)

    assert reads == []
    assert chosen.rows == ()
    assert chosen.message == projects_backup.NO_TOKEN_TEXT


def test_unchecking_every_project_says_so_in_the_status_line(qtbot, settings_path, local_folder):
    tab = _tab(qtbot, settings_path, lambda url, token: list(HOSTED_ROWS))
    checklist = _choose(qtbot, tab)
    status = tab.findChild(QLabel, "projects-backup-status")
    assert status.text().startswith("All 3 hosted project(s) are checked")

    for row in range(checklist.count()):
        checklist.item(row).setCheckState(Qt.CheckState.Unchecked)

    assert "No project is checked" in status.text()
    assert "All" not in status.text()


def test_write_after_choose_reads_the_hosted_list_only_once(qtbot, settings_path, local_folder):
    reads: list[str] = []

    def list_hosted(url, token):
        reads.append(url)
        return list(HOSTED_ROWS)

    tab = _tab(qtbot, settings_path, list_hosted)
    _choose(qtbot, tab)
    backup_status = tab.findChild(QLabel, "projects-backup-status")

    _write_button(tab).click()
    qtbot.waitUntil(lambda: backup_status.text().startswith("Backed up"), timeout=5000)

    assert reads == [HOSTED_URL]


def test_a_write_after_the_saved_account_changed_writes_nothing(qtbot, settings_path, local_folder):
    """Even with the checklist still on screen, a write re-checks the account and writes nothing for another one."""
    holder = {"token": FAKE_TOKEN}
    reads: list[str] = []

    def list_hosted(url, token):
        reads.append(url)
        return list(HOSTED_ROWS)

    tab = _tab_with_token(qtbot, settings_path, holder, list_hosted)
    _choose(qtbot, tab)
    holder["email"] = "fake-other-owner@example.test"
    backup_status = tab.findChild(QLabel, "projects-backup-status")

    _write_button(tab).click()
    qtbot.waitUntil(lambda: backup_status.text().startswith("The hosted account changed"), timeout=5000)

    assert list(local_folder.glob("projects-*.json")) == [] and list(local_folder.glob("subset-projects-*.json")) == []
    assert reads == [HOSTED_URL]


# ----------------------------------------------------------------- the backups folder, read defensively and pruned safely


@pytest.mark.parametrize(("legacy_ids", "covered"), [(["h-1"], True), (["only-here"], False)])
def test_a_legacy_backup_is_removed_only_when_a_newer_kept_backup_holds_all_its_projects(
    tmp_path, legacy_ids, covered
):
    """A plain-named file from before this branch may be a subset. It goes only when newer kept files hold all its projects."""
    folder = tmp_path / "b"
    legacy_rows = [{"id": pid, "name": pid} for pid in legacy_ids]
    legacy = projects_backup.write_backup(legacy_rows, folder=folder, taken_at=TAKEN - timedelta(days=30))
    for day in range(projects_backup.BACKUPS_KEPT + 1):
        projects_backup.write_backup(HOSTED_ROWS, folder=folder, taken_at=TAKEN + timedelta(days=day))

    names = {path.name for path in folder.iterdir()}

    assert (legacy.path.name in names) is not covered
    assert len(list(folder.glob(FULL_GLOB))) >= projects_backup.BACKUPS_KEPT


# ----------------------------------------------------------------- one reset path: every trigger clears the same way


CHOOSE_PROMPT_TEXT = "Press Choose projects to back up to read the hosted projects."


def _trigger_account_changed(qtbot, tab, settings_path, holder):
    SettingsStore(settings_path).save(Settings(mode="local", hosted_url="https://other.test", local_url=LOCAL_URL))
    tab.refresh()
    status = tab.findChild(QLabel, "projects-status")
    qtbot.waitUntil(lambda: status.text() != LOADING_TEXT, timeout=5000)
    return RESET_TEXT_FOR["account_changed"]


def _trigger_token_removed(qtbot, tab, settings_path, holder):
    holder["token"] = None
    tab.refresh()
    status = tab.findChild(QLabel, "projects-status")
    qtbot.waitUntil(lambda: status.text() != LOADING_TEXT, timeout=5000)
    return RESET_TEXT_FOR["token_removed"]


def _trigger_token_added(qtbot, tab, settings_path, holder):
    holder["token"] = None
    tab.refresh()
    status = tab.findChild(QLabel, "projects-status")
    qtbot.waitUntil(lambda: status.text() != LOADING_TEXT, timeout=5000)
    holder["token"] = FAKE_TOKEN
    tab.refresh()
    qtbot.waitUntil(lambda: status.text() != LOADING_TEXT, timeout=5000)
    return RESET_TEXT_FOR["token_added"]


def _trigger_no_projects(qtbot, tab, settings_path, holder):
    holder["answers"] = []
    tab.findChild(QPushButton, "projects-backup-choose").click()
    backup_status = tab.findChild(QLabel, "projects-backup-status")
    qtbot.waitUntil(lambda: backup_status.text() == projects_backup.NO_PROJECTS_TEXT, timeout=5000)
    return projects_backup.NO_PROJECTS_TEXT


def _trigger_expired(qtbot, tab, settings_path, holder):
    """The list is shown, ten minutes pass, and the tab is shown again: the checklist is reset."""
    holder["clock"].now += 601
    tab.showEvent(QShowEvent())
    return EXPIRED_TEXT


def _trigger_token_unreadable(qtbot, tab, settings_path, holder):
    holder["unreadable"] = True
    tab.refresh()
    status = tab.findChild(QLabel, "projects-status")
    qtbot.waitUntil(lambda: status.text() != LOADING_TEXT, timeout=5000)
    return TOKEN_UNREADABLE_TEXT


def _trigger_choose_failed(qtbot, tab, settings_path, holder):
    holder["fail"] = True
    tab.findChild(QPushButton, "projects-backup-choose").click()
    backup_status = tab.findChild(QLabel, "projects-backup-status")
    qtbot.waitUntil(lambda: backup_status.text().startswith(BACKUP_FAILED_TEXT), timeout=5000)
    return BACKUP_FAILED_TEXT


def _trigger_write_refused(qtbot, tab, settings_path, holder):
    holder["email"] = "fake-other-owner@example.test"
    tab.findChild(QPushButton, "projects-backup-write-checked").click()
    backup_status = tab.findChild(QLabel, "projects-backup-status")
    qtbot.waitUntil(lambda: backup_status.text() == projects_backup.ACCOUNT_CHANGED_TEXT, timeout=5000)
    return projects_backup.ACCOUNT_CHANGED_TEXT


def _trigger_stale_choose(qtbot, tab, settings_path, holder):
    """A Choose is in flight when the token is removed. Its late answer resets the busy flag and the status line."""
    holder["block"] = threading.Event()
    holder["entered"] = threading.Event()
    tab.findChild(QPushButton, "projects-backup-choose").click()
    # Wait until the Choose has read the token and is inside the hosted read, so the removal comes after the read.
    assert holder["entered"].wait(timeout=5)
    holder["token"] = None
    tab.refresh()
    status = tab.findChild(QLabel, "projects-status")
    qtbot.waitUntil(lambda: status.text() != LOADING_TEXT, timeout=5000)
    holder["block"].set()
    qtbot.waitUntil(lambda: not tab._backup_running, timeout=5000)
    return CHOOSE_PROMPT_TEXT


RESET_TEXT_FOR = {
    "account_changed": "The hosted account changed, so the checklist was cleared. Choose the projects again.",
    "token_removed": "No hosted token is saved, so the checklist was cleared.",
    "token_added": "A hosted token was saved again, so the checklist was cleared. Choose the projects again.",
}

TRIGGERS = {
    "account_changed": _trigger_account_changed,
    "token_removed": _trigger_token_removed,
    "token_added": _trigger_token_added,
    "no_projects": _trigger_no_projects,
    "choose_failed": _trigger_choose_failed,
    "write_refused": _trigger_write_refused,
    "stale_choose": _trigger_stale_choose,
    "token_unreadable": _trigger_token_unreadable,
    "expired": _trigger_expired,
}


@pytest.mark.parametrize("trigger", sorted(TRIGGERS))
def test_every_trigger_resets_the_checklist_through_the_same_path(qtbot, settings_path, local_folder, trigger):
    """Each trigger must clear the checklist, its rows and key, hide it, disable Write, and set the status line.

    The triggers are run one by one. Each one starts from a shown checklist, except token_added, which starts from a
    removed token because a list cannot be shown without one.
    """
    holder: dict = {"token": FAKE_TOKEN, "answers": list(HOSTED_ROWS), "fail": False}

    def list_hosted(url, token):
        block = holder.get("block")
        if block is not None:
            holder["entered"].set()
            block.wait(timeout=5)
        if holder["fail"]:
            raise hosted_client.HostedUnreachable()
        return list(holder["answers"])

    SettingsStore(settings_path).save(Settings(mode="local", hosted_url=HOSTED_URL, local_url=LOCAL_URL))
    sources = DataSources(
        probe=lambda url: local_client.HEALTH_NOT_RUNNING,
        read_server=lambda url: [],
        read_file=lambda path: [],
        get_token=lambda: _read_holder_token(holder),
        list_hosted=list_hosted,
        read_account=lambda url, token: holder.get("email", ACCOUNT_EMAIL),
    )
    holder["clock"] = _Clock(CLOCK_START)
    tab = ProjectsTab(SettingsStore(settings_path), sources, clock=holder["clock"])
    qtbot.addWidget(tab)
    tab.refresh()
    qtbot.waitUntil(lambda: tab.findChild(QLabel, "projects-status").text() != LOADING_TEXT, timeout=5000)
    if trigger != "token_added":
        _choose(qtbot, tab)

    expected = TRIGGERS[trigger](qtbot, tab, settings_path, holder)

    status = tab.findChild(QLabel, "projects-backup-status")
    assert _checklist(tab).count() == 0
    assert _choice_box(tab).isHidden()
    assert not _write_button(tab).isEnabled()
    assert tab._chosen == ()
    assert not tab._backup_running
    assert status.text().startswith(expected)


# ----------------------------------------------------------------- a keychain read failure is not a removed token


TOKEN_UNREADABLE_TEXT = "Could not read the saved token. Try again."


def _read_holder_token(holder):
    """What the keychain read returns in these tests: the token, None for no token, or a raised read failure."""
    if holder.get("unreadable"):
        raise token_store.TokenReadError("token could not be read: keyring call failed (RuntimeError)")
    return holder["token"]


def test_a_keychain_read_failure_keeps_the_saved_state_and_says_so(qtbot, settings_path, local_folder):
    holder = {"token": FAKE_TOKEN, "unreadable": False}
    tab = _tab_with_token(qtbot, settings_path, holder, lambda url, token: list(HOSTED_ROWS))
    _choose(qtbot, tab)
    button = tab.findChild(QPushButton, "projects-backup-button")
    assert not button.isHidden()

    holder["unreadable"] = True
    tab.refresh()
    status = tab.findChild(QLabel, "projects-status")
    qtbot.waitUntil(lambda: status.text() != LOADING_TEXT, timeout=5000)

    # The tab runs in local mode here, so the projects list does not use the token. The backup line says what happened.
    assert status.text() != TOKEN_UNREADABLE_TEXT
    assert not button.isHidden()  # the saved state is kept: an unreadable token is not a removed one
    assert tab.findChild(QLabel, "projects-backup-status").text() == TOKEN_UNREADABLE_TEXT
    assert _checklist(tab).count() == 0 and not _write_button(tab).isEnabled()


def test_a_removed_token_hides_the_backup_controls_and_is_not_a_read_failure(qtbot, settings_path, local_folder):
    holder = {"token": FAKE_TOKEN, "unreadable": False}
    tab = _tab_with_token(qtbot, settings_path, holder, lambda url, token: list(HOSTED_ROWS))
    _choose(qtbot, tab)

    holder["token"] = None
    tab.refresh()
    status = tab.findChild(QLabel, "projects-status")
    qtbot.waitUntil(lambda: status.text() != LOADING_TEXT, timeout=5000)

    assert tab.findChild(QPushButton, "projects-backup-button").isHidden()
    assert status.text() != TOKEN_UNREADABLE_TEXT
    assert _checklist(tab).count() == 0


# ----------------------------------------------------------------- the list expires after ten minutes


def test_the_list_age_counts_from_the_read_not_from_the_choose_or_the_write(qtbot, settings_path, local_folder):
    clock = _Clock(CLOCK_START)

    def list_hosted(url, token):
        clock.now = CLOCK_START + 300  # the hosted read takes five minutes; the list is read when it returns
        return list(HOSTED_ROWS)

    tab = _tab_with_token(qtbot, settings_path, {"token": FAKE_TOKEN}, list_hosted, clock=clock)
    _choose(qtbot, tab)
    write = _write_button(tab)
    backup_status = tab.findChild(QLabel, "projects-backup-status")
    read_at = CLOCK_START + 300

    clock.now = read_at + 600  # exactly ten minutes after the read: still allowed
    write.click()
    qtbot.waitUntil(lambda: backup_status.text().startswith("Backed up"), timeout=5000)

    clock.now = read_at + 601  # one second more: refused
    write.click()
    qtbot.waitUntil(lambda: backup_status.text() == EXPIRED_TEXT, timeout=5000)

    assert len(list(local_folder.glob(SUBSET_GLOB))) == 1


def test_a_checklist_older_than_ten_minutes_is_reset_when_the_tab_is_shown(qtbot, settings_path, local_folder):
    clock = _Clock(CLOCK_START)
    tab = _tab_with_token(
        qtbot, settings_path, {"token": FAKE_TOKEN}, lambda url, token: list(HOSTED_ROWS), clock=clock
    )
    _choose(qtbot, tab)

    clock.now = CLOCK_START + 600
    tab.showEvent(QShowEvent())
    assert _checklist(tab).count() == 3

    clock.now = CLOCK_START + 601
    tab.showEvent(QShowEvent())
    assert _checklist(tab).count() == 0
    assert not _write_button(tab).isEnabled()
    assert tab.findChild(QLabel, "projects-backup-status").text() == EXPIRED_TEXT
