import sqlite3

import pytest

from meridian_desktop import projects_reader
from meridian_desktop.projects_reader import PROJECT_COLUMNS, get_project, list_projects

# The core projects table, reduced to the columns the desktop may read. executor_config and
# agent_instructions are included so the test can prove the reader never selects them.
SCHEMA = """
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

# Inserted oldest first on purpose, so the expected order is not the insertion order.
ROWS = [
    ("p-old", "Oldest", "human-1", "book", "active", "P2", None,
     "2026-01-05 09:00:00", "EXEC-SECRET-OLD", "AGENT-NOTE-OLD"),
    ("p-new", "Newest", "human-1", "rocket", "parked", "P0", "p-old",
     "2026-03-02 17:30:00", "EXEC-SECRET-NEW", "AGENT-NOTE-NEW"),
    ("p-mid", "Middle", None, None, "archived", "P1", None,
     "2026-02-14 12:00:00", "EXEC-SECRET-MID", "AGENT-NOTE-MID"),
]


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "meridian.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.executemany("INSERT INTO projects VALUES (?,?,?,?,?,?,?,?,?,?)", ROWS)
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture
def reader_log(monkeypatch):
    """Record each connection the reader opens, with its arguments, and each statement it runs."""
    opened: list[tuple[tuple, dict]] = []
    statements: list[str] = []
    real_connect = sqlite3.connect

    def recording_connect(*args, **kwargs):
        opened.append((args, kwargs))
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(projects_reader.sqlite3, "connect", recording_connect)
    return opened, statements


def test_reader_selects_only_listed_columns_newest_first(db_path, reader_log):
    opened, statements = reader_log
    opened.clear()
    statements.clear()

    rows = list_projects(db_path)

    # Newest first by created_at, even though the rows were inserted oldest first.
    assert [row["id"] for row in rows] == ["p-new", "p-mid", "p-old"]
    # Only the eight listed columns come back, in the listed order.
    for row in rows:
        assert tuple(row) == PROJECT_COLUMNS
    assert rows[0] == {
        "id": "p-new",
        "name": "Newest",
        "creator_human_id": "human-1",
        "icon": "rocket",
        "status": "parked",
        "priority": "P0",
        "parent_project_id": "p-old",
        "created_at": "2026-03-02 17:30:00",
    }

    # One read-only connection, opened through a URI with mode=ro.
    (args, kwargs), = opened
    assert args[0].endswith("?mode=ro")
    assert kwargs.get("uri") is True

    # Every statement the reader ran is a SELECT that names its columns and skips the rest.
    assert statements
    assert all(sql.lstrip().upper().startswith("SELECT ") for sql in statements)
    assert not any("*" in sql or "executor_config" in sql or "agent_instructions" in sql for sql in statements)

    # The two columns the desktop must never read do not appear in any returned row.
    text = repr(rows)
    assert "EXEC-SECRET" not in text
    assert "AGENT-NOTE" not in text


def test_get_project_reads_one_row_by_id_or_none(db_path, reader_log):
    opened, statements = reader_log
    opened.clear()
    statements.clear()

    row = get_project(db_path, "p-mid")
    assert row == {
        "id": "p-mid",
        "name": "Middle",
        "creator_human_id": None,
        "icon": None,
        "status": "archived",
        "priority": "P1",
        "parent_project_id": None,
        "created_at": "2026-02-14 12:00:00",
    }
    assert get_project(db_path, "no-such-id") is None

    assert opened and all(args[0].endswith("?mode=ro") for args, _kwargs in opened)
    assert statements and all(sql.lstrip().upper().startswith("SELECT ") for sql in statements)
