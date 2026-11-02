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
from PySide6.QtWidgets import QLabel, QListWidget, QPushButton, QWidget

from _fakes import MemoryKeyring
from meridian_desktop import hosted_client, local_client, projects_backup
from meridian_desktop.data_source import DataSources
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.ui.projects_tab import LOADING_TEXT, ProjectsTab
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


def _tab_with_token(qtbot, settings_path, holder, list_hosted):
    """A Projects tab whose saved token is read from ``holder``, so a test can change or remove it between loads."""
    SettingsStore(settings_path).save(Settings(mode="local", hosted_url=HOSTED_URL, local_url=LOCAL_URL))
    sources = DataSources(
        probe=lambda url: local_client.HEALTH_NOT_RUNNING,
        read_server=lambda url: [],
        read_file=lambda path: [],
        get_token=lambda: holder["token"],
        list_hosted=list_hosted,
    )
    tab = ProjectsTab(SettingsStore(settings_path), sources)
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


def test_a_refresh_for_a_different_token_clears_the_checklist(qtbot, settings_path, local_folder):
    holder = {"token": FAKE_TOKEN}
    tab = _tab_with_token(qtbot, settings_path, holder, lambda url, token: list(HOSTED_ROWS))
    _choose(qtbot, tab)

    holder["token"] = "sk_meridian_fake-other-account-0009"
    tab.refresh()
    status = tab.findChild(QLabel, "projects-status")
    qtbot.waitUntil(lambda: status.text() != LOADING_TEXT, timeout=5000)

    assert _checklist(tab).count() == 0
    assert not _write_button(tab).isEnabled()


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
    holder["token"] = "sk_meridian_fake-other-account-0010"
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
