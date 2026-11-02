"""Read-only reader for one project's sessions in the Meridian database file. Pure Python: stdlib sqlite3 only, no Qt.

The Sessions tab reads this file only when the local server is not running and hosted Meridian gave no sessions list.
The file is opened through projects_reader.open_read_only, so nothing is written and no -wal or -shm file is created.
The reader never calls the core ``init_db``.

The SQL follows ``get_sessions`` in the core (meridian/db/__init__.py) with active_only=true: the project's sessions
whose status is not closed or archived, newest last_seen first. The core runs ``SELECT *``, so this reader names the
five columns the tab shows instead (SESSION_COLUMNS). The session summary, checkpoint data and every other column are
never selected.

The route (meridian/routes/projects.py, get_sessions) first runs the core's 30-minute expiry: an active session whose
last_seen is more than 30 minutes old is set to idle. This reader cannot write, so it applies the same rule when it
reads, and shows such a row as EXPIRED_STATUS.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from meridian_desktop import projects_reader
from meridian_desktop.sessions_client import (
    ERROR,
    MAX_SESSIONS,
    NO_PROJECT,
    NO_PROJECT_MESSAGE,
    SESSIONS,
    SessionsResult,
    parse_timestamp,
)
# The same row builder the HTTP reader uses, so a file row is cleaned and capped exactly as a server row is.
from meridian_desktop.sessions_client import _row  # noqa: PLC2701

# The source a file result names, so the tab can say where the rows came from.
SOURCE_FILE = "file"
SESSION_COLUMNS = ("name", "status", "last_seen", "client_type", "agent_framework")
EXPIRY_MINUTES = 30
EXPIRED_STATUS = "idle (expired)"
FILE_FAILED_MESSAGE = "The database file could not be read."

# The column list is built from the constant above, so it never carries user input.
_SQL = (
    "SELECT " + ", ".join(SESSION_COLUMNS) + " FROM sessions WHERE project_id = ? "
    "AND status NOT IN ('closed', 'archived') ORDER BY last_seen DESC LIMIT ?"
)


def list_sessions(path: Path | str, project_id: str) -> list[dict[str, Any]]:
    """Return the project's active and idle sessions, newest first, keyed by SESSION_COLUMNS.

    Raise projects_reader.NoDatabase when no file is there, and sqlite3.Error when it cannot be read.
    """
    conn = projects_reader.open_read_only(path)
    try:
        fetched = conn.execute(_SQL, (project_id, MAX_SESSIONS)).fetchall()
    finally:
        conn.close()
    return [dict(zip(SESSION_COLUMNS, row)) for row in fetched]


def _expired(row: dict[str, Any], cutoff: datetime) -> bool:
    seen = parse_timestamp(row.get("last_seen") or "")
    return row.get("status") == "active" and seen is not None and seen < cutoff


def file_sessions(
    path: Path | str,
    project_id: str,
    *,
    note: str = "",
    now: datetime | None = None,
) -> SessionsResult:
    """The project's sessions from the database file, as a result the Sessions tab shows.

    ``note`` is the snapshot label the status line shows with the rows. Rows past the 30-minute expiry read as
    EXPIRED_STATUS. A file that cannot be read gives an error with fixed text, which names no path and no exception.
    """
    if not isinstance(project_id, str) or not project_id.strip():
        return SessionsResult(NO_PROJECT, NO_PROJECT_MESSAGE)
    current = now if now is not None else datetime.now(timezone.utc)
    cutoff = current - timedelta(minutes=EXPIRY_MINUTES)
    try:
        rows = list_sessions(path, project_id.strip())
    except Exception:  # noqa: BLE001 - a missing file, a locked file or an old schema: one fixed line
        return SessionsResult(ERROR, FILE_FAILED_MESSAGE, SOURCE_FILE)
    shown = []
    for row in rows:
        if _expired(row, cutoff):
            row = {**row, "status": EXPIRED_STATUS}
        shown.append(_row(row))
    return SessionsResult(SESSIONS, note, SOURCE_FILE, tuple(shown))


__all__ = [
    "EXPIRED_STATUS",
    "EXPIRY_MINUTES",
    "FILE_FAILED_MESSAGE",
    "SESSION_COLUMNS",
    "SOURCE_FILE",
    "file_sessions",
    "list_sessions",
]
