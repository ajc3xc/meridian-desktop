"""Tests for meridian_desktop/sessions_reader.py and the read-only open in projects_reader.

Every database here is built in tmp_path with the core's own columns (meridian/db/__init__.py: the projects and
sessions tables, with the session_summary and agent_framework columns the core migrations add). A snapshot is made the
way a stopped server leaves one: the files are copied while a writer still holds them, so the -wal file is kept. No
test opens the real core database. The one test that uses real data copies the file named by
MERIDIAN_DESKTOP_SNAPSHOT_DB (and its -wal) into tmp_path first, and is skipped when that variable is unset.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from meridian_desktop import projects_reader, sessions_client as sc, sessions_reader
from meridian_desktop.projects_reader import NoDatabase, list_projects
from meridian_desktop.sessions_reader import (
    EXPIRED_STATUS,
    FILE_FAILED_MESSAGE,
    SESSION_COLUMNS,
    SOURCE_FILE,
    file_sessions,
    list_sessions,
)

PID = "5787cc92-ba7d-4788-b17c-28ab7938b839"
OTHER_PID = "11111111-2222-4333-8444-555555555555"
SECRET_SUMMARY = "SECRET-SUMMARY-do-not-read"
NOW = datetime(2026, 10, 23, 12, 0, 0, tzinfo=timezone.utc)

# The core columns of the two tables the desktop reads (meridian/db/__init__.py, plus the migration columns).
CORE_SCHEMA = """
CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    creator_human_id TEXT,
    executor_config TEXT,
    icon TEXT,
    agent_instructions TEXT,
    parent_project_id TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    priority TEXT NOT NULL DEFAULT 'P2',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    name TEXT NOT NULL,
    human_id TEXT,
    session_type TEXT DEFAULT 'human',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','idle','closed','archived')),
    last_seen TEXT NOT NULL DEFAULT (datetime('now')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    session_summary TEXT,
    agent_framework TEXT DEFAULT 'claude_code',
    client_type TEXT,
    checkpoint_data TEXT,
    sprint_version TEXT,
    goal_compliance TEXT
);
"""

# Alpha is the newest, so it is listed first and the Sessions picker starts on it.
PROJECTS = [
    (PID, "Alpha", "human-1", "EXEC-SECRET", None, "AGENT-NOTE", None, "active", "P1", "2026-03-05 09:00:00"),
    (OTHER_PID, "Beta", None, None, None, None, None, "active", "P2", "2026-02-05 09:00:00"),
]


def stamp(moment: datetime) -> str:
    """A last_seen value as the core writes it: UTC, 'YYYY-MM-DD HH:MM:SS', no offset."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def session_rows(now: datetime = NOW) -> list[tuple]:
    """(id, project_id, name, status, last_seen, client_type, agent_framework, session_summary)."""
    return [
        ("s-fresh", PID, "fresh-active", "active", stamp(now - timedelta(minutes=5)), "claude-code", "claude_code",
         SECRET_SUMMARY),
        ("s-stale", PID, "stale-active", "active", stamp(now - timedelta(hours=2)), None, "codex", SECRET_SUMMARY),
        ("s-idle", PID, "already-idle", "idle", stamp(now - timedelta(days=3)), "claude-desktop", "claude_code", None),
        ("s-closed", PID, "closed-one", "closed", stamp(now - timedelta(minutes=1)), "claude-code", "claude_code", None),
        ("s-archived", PID, "archived-one", "archived", stamp(now), "claude-code", "claude_code", None),
        ("s-other", OTHER_PID, "other-project", "active", stamp(now), "claude-code", "claude_code", None),
    ]


def _fill(conn: sqlite3.Connection, now: datetime) -> None:
    conn.executescript(CORE_SCHEMA)
    conn.executemany("INSERT INTO projects (id, name, creator_human_id, executor_config, icon, agent_instructions, "
                     "parent_project_id, status, priority, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)", PROJECTS)
    conn.executemany("INSERT INTO sessions (id, project_id, name, status, last_seen, client_type, agent_framework, "
                     "session_summary) VALUES (?,?,?,?,?,?,?,?)", session_rows(now))
    conn.commit()


def make_core_db(folder: Path, *, keep: tuple[str, ...] | None = ("-wal", "-shm"), now: datetime = NOW) -> Path:
    """Build ``folder``/meridian.db in WAL mode with the core schema and rows.

    ``keep`` names the sidecar files the snapshot keeps. The files are copied while the writer is still open, as a
    stopped server leaves them, and every row is then only in the -wal. ``keep=()`` closes the writer first, so the
    rows reach the main file and SQLite removes both sidecars, as after a clean shutdown. ``keep=None`` builds a plain
    rollback-journal database.
    """
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "meridian.db"
    if not keep:
        conn = sqlite3.connect(target)
        try:
            if keep is not None:
                conn.execute("PRAGMA journal_mode=WAL")
            _fill(conn, now)
        finally:
            conn.close()
        return target
    build = folder.parent / (folder.name + "-build")
    build.mkdir(parents=True, exist_ok=True)
    source = build / "meridian.db"
    conn = sqlite3.connect(source)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")  # keep every row in the -wal, as a server that never checkpointed
        _fill(conn, now)
        shutil.copyfile(source, target)
        for suffix in keep:
            shutil.copyfile(build / ("meridian.db" + suffix), folder / ("meridian.db" + suffix))
    finally:
        conn.close()
    shutil.rmtree(build, ignore_errors=True)
    return target


def folder_state(folder: Path) -> dict[str, str]:
    """Each file name in ``folder`` and a hash of its bytes.

    An -shm file that was already there is listed by name only: it is SQLite's shared index, and a mode=ro reader
    records its read marks in it, as every SQLite reader does. The database and -wal bytes must never change.
    """
    return {
        p.name: "shared index" if p.name.endswith("-shm") else hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(folder.iterdir())
        if p.is_file()
    }


# ----------------------------------------------------------------- the read-only open creates nothing


@pytest.mark.parametrize("keep", [("-wal", "-shm"), ("-wal",), ()], ids=["wal-and-shm", "wal-only", "no-sidecars"])
def test_a_wal_snapshot_is_read_without_creating_or_changing_any_file(tmp_path, keep):
    folder = tmp_path / "data"
    db = make_core_db(folder, keep=keep)
    assert sorted(folder_state(folder)) == sorted("meridian.db" + suffix for suffix in ("",) + keep)
    assert db.read_bytes()[18:20] == bytes([2, 2])  # the header says WAL mode, as the core's database does
    before = folder_state(folder)

    projects = list_projects(db)
    sessions = list_sessions(db, PID)

    # Every committed row is read, including rows that are only in the -wal.
    assert sorted(row["name"] for row in projects) == ["Alpha", "Beta"]
    assert [row["name"] for row in sessions] == ["fresh-active", "stale-active", "already-idle"]
    # No -wal or -shm was created, and no byte of the database or its -wal changed.
    assert folder_state(folder) == before


def test_the_private_copy_is_removed_when_the_read_ends(tmp_path, monkeypatch):
    db = make_core_db(tmp_path / "data", keep=("-wal",))
    scratch_root = tmp_path / "temp"
    scratch_root.mkdir()
    monkeypatch.setattr(projects_reader.tempfile, "tempdir", str(scratch_root))

    assert [row["name"] for row in list_projects(db)]

    assert list(scratch_root.iterdir()) == []


def test_the_read_only_open_refuses_a_write(tmp_path):
    db = make_core_db(tmp_path / "data")
    conn = projects_reader.open_read_only(db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE sessions SET status = 'idle'")
    finally:
        conn.close()


def test_last_written_is_the_later_of_the_database_and_its_wal(tmp_path):
    db = make_core_db(tmp_path / "data")
    wal = db.with_name("meridian.db-wal")
    shm = db.with_name("meridian.db-shm")
    older = datetime(2026, 9, 17, 16, 20).timestamp()
    newer = datetime(2026, 10, 12, 20, 49).timestamp()
    os.utime(db, (older, older))
    os.utime(wal, (newer, newer))
    os.utime(shm, (newer + 86400 * 10, newer + 86400 * 10))  # a reader touched the -shm later: not counted

    assert projects_reader.last_written(db) == datetime.fromtimestamp(newer)
    wal.unlink()
    assert projects_reader.last_written(db) == datetime.fromtimestamp(older)
    assert projects_reader.last_written(tmp_path / "absent.db") is None


# ----------------------------------------------------------------- the sessions reader


def test_the_reader_selects_only_the_five_columns_for_one_project_active_only(tmp_path, monkeypatch):
    db = make_core_db(tmp_path / "data")
    statements: list[str] = []
    real_connect = sqlite3.connect

    def recording_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(projects_reader.sqlite3, "connect", recording_connect)

    rows = list_sessions(db, PID)

    assert SESSION_COLUMNS == ("name", "status", "last_seen", "client_type", "agent_framework")
    assert all(tuple(row) == SESSION_COLUMNS for row in rows)
    # Closed and archived sessions are left out, as the core's active-only list does, and so is the other project.
    assert [row["name"] for row in rows] == ["fresh-active", "stale-active", "already-idle"]
    assert SECRET_SUMMARY not in repr(rows)
    assert statements and all(sql.lstrip().upper().startswith("SELECT ") for sql in statements)
    assert not any("*" in sql or "session_summary" in sql for sql in statements)


def test_file_sessions_label_rows_past_the_thirty_minute_expiry(tmp_path):
    db = make_core_db(tmp_path / "data")

    result = file_sessions(db, PID, note="A note.", now=NOW)

    assert result.state == sc.SESSIONS
    assert result.source == SOURCE_FILE
    assert result.message == "A note."
    assert [(row.name, row.status, row.client) for row in result.rows] == [
        ("fresh-active", "active", "claude-code"),
        ("stale-active", EXPIRED_STATUS, "codex"),  # client_type is empty, so the framework names the client
        ("already-idle", "idle", "claude-desktop"),
    ]
    assert EXPIRED_STATUS == "idle (expired)"
    assert sc.last_seen_age(result.rows[0].last_seen, NOW) == "5 min ago"


def test_file_sessions_for_a_missing_file_or_a_broken_table_is_a_plain_error(tmp_path):
    missing = file_sessions(tmp_path / "absent.db", PID)
    assert (missing.state, missing.message, missing.source) == (sc.ERROR, FILE_FAILED_MESSAGE, SOURCE_FILE)
    assert str(tmp_path) not in missing.message

    no_table = tmp_path / "empty.db"
    sqlite3.connect(no_table).close()
    broken = file_sessions(no_table, PID)
    assert (broken.state, broken.message) == (sc.ERROR, FILE_FAILED_MESSAGE)


def test_file_sessions_without_a_project_sends_nothing(tmp_path):
    result = file_sessions(tmp_path / "absent.db", "  ")
    assert result.state == sc.NO_PROJECT


def test_missing_file_raises_no_database_and_creates_nothing(tmp_path):
    with pytest.raises(NoDatabase):
        list_sessions(tmp_path / "absent.db", PID)
    assert list(tmp_path.iterdir()) == []


def test_the_reader_module_imports_no_qt():
    text = Path(sessions_reader.__file__).read_text(encoding="utf-8")
    assert "PySide" not in text and "PyQt" not in text
    assert "init_db(" not in text


# ----------------------------------------------------------------- a copy of the real snapshot (opt in)


@pytest.mark.skipif(not os.environ.get("MERIDIAN_DESKTOP_SNAPSHOT_DB"), reason="MERIDIAN_DESKTOP_SNAPSHOT_DB not set")
def test_a_copy_of_the_real_core_snapshot_reads_and_creates_nothing(tmp_path):
    source = Path(os.environ["MERIDIAN_DESKTOP_SNAPSHOT_DB"])
    folder = tmp_path / "core" / "data"
    folder.mkdir(parents=True)
    shutil.copyfile(source, folder / "meridian.db")
    if source.with_name(source.name + "-wal").is_file():
        shutil.copyfile(source.with_name(source.name + "-wal"), folder / "meridian.db-wal")
    db = folder / "meridian.db"
    before = folder_state(folder)

    projects = list_projects(db)
    assert projects
    for project in projects:
        result = file_sessions(db, project["id"])
        assert result.state == sc.SESSIONS
        assert all(row.status not in ("closed", "archived") for row in result.rows)
    assert folder_state(folder) == before
