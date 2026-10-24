"""Read-only reader for the Meridian project list. Pure Python: stdlib sqlite3 only, no Qt import.

The desktop shows projects from the Meridian database file, and this module only reads it.
Each call opens the file with the SQLite URI flag ``mode=ro``, runs one SELECT, and closes the
connection. The reader never calls the core ``init_db``, so it never creates tables, migrates a
schema, or creates a missing file.

A missing file raises NoDatabase before any connection is opened, and the error names the path.
An INSERT or other write on the reader connection is refused by SQLite (sqlite3.OperationalError).
The reader writes no rows, and it never creates a file beside the database. SQLite would create
the -wal and -shm files of a WAL-mode database for a mode=ro reader, so open_read_only picks the
open by the files that are there:

- a database that is not in WAL mode, or a WAL database with both its -wal and -shm files: mode=ro.
  A WAL database is read through those files, so the rows a running Meridian committed are listed. As every SQLite
  reader does, it records its read marks in the existing -shm index. The database and -wal bytes never change.
- a WAL database with no -wal file: mode=ro with immutable=1. Every committed row is in the main
  file, and SQLite then opens no -wal or -shm file.
- a WAL database with a -wal file but no -shm file: the database and its -wal are copied to a
  private temp folder, and the copy is read with mode=ro. The temp folder is removed on close.

Only the columns in PROJECT_COLUMNS are selected. The core table also holds executor_config and
agent_instructions. This reader never selects them, so they never reach a caller.

The list SQL is copied from ``list_projects`` in the core (meridian/db/__init__.py). The core
runs ``SELECT *``, so this reader names the eight columns it needs instead. The placeholder is
``?`` because the sqlite3 module takes qmark parameters.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_COLUMNS = (
    "id",
    "name",
    "creator_human_id",
    "icon",
    "status",
    "priority",
    "parent_project_id",
    "created_at",
)

# The column list is built from the constant above, so it never carries user input.
_SELECT = "SELECT " + ", ".join(PROJECT_COLUMNS) + " FROM projects"
_LIST_SQL = _SELECT + " ORDER BY created_at DESC"
_GET_SQL = _SELECT + " WHERE id = ?"


class NoDatabase(Exception):
    """No database file is at ``path``. The reader raises this and never creates the file."""

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        super().__init__(f"no Meridian database file at {self.path}")


def _uri(path: Path | str, extra: str = "") -> str:
    """Return the SQLite file URI for ``path``, with mode=ro. Path.as_uri percent-encodes the path."""
    return Path(path).resolve().as_uri() + "?mode=ro" + extra


def _sidecar(path: Path, suffix: str) -> Path:
    return path.with_name(path.name + suffix)


def _is_wal(path: Path) -> bool:
    """True when the database header says WAL mode (file format bytes 18 and 19 are 2)."""
    try:
        with path.open("rb") as handle:
            header = handle.read(20)
    except OSError:
        return False
    return len(header) == 20 and (header[18] == 2 or header[19] == 2)


class _CopyConnection(sqlite3.Connection):
    """A read-only connection to a private copy. Closing it removes the copy's temp folder."""

    scratch: tempfile.TemporaryDirectory[str] | None = None

    def close(self) -> None:
        try:
            super().close()
        finally:
            scratch, self.scratch = self.scratch, None
            if scratch is not None:
                scratch.cleanup()


def _open_copy(target: Path, wal: Path) -> sqlite3.Connection:
    """Copy the database and its -wal to a temp folder and open the copy read-only. Nothing is written beside ``target``."""
    scratch = tempfile.TemporaryDirectory(prefix="meridian-desktop-read-")
    try:
        copy = Path(scratch.name) / target.name
        shutil.copyfile(target, copy)
        shutil.copyfile(wal, _sidecar(copy, "-wal"))
        conn = sqlite3.connect(_uri(copy), uri=True, factory=_CopyConnection)
    except BaseException:
        scratch.cleanup()
        raise
    conn.scratch = scratch
    return conn


def open_read_only(path: Path | str) -> sqlite3.Connection:
    """Open ``path`` read-only, creating no -wal or -shm file beside it (see the module notes for the three cases).

    Raise NoDatabase first when no file is there, so SQLite never creates one.
    """
    target = Path(path)
    if not target.is_file():
        raise NoDatabase(path)
    wal, shm = _sidecar(target, "-wal"), _sidecar(target, "-shm")
    try:
        if not _is_wal(target) or (wal.is_file() and shm.is_file()):
            return sqlite3.connect(_uri(target), uri=True)
        if not wal.exists():
            return sqlite3.connect(_uri(target, "&immutable=1"), uri=True)
        return _open_copy(target, wal)
    except (sqlite3.OperationalError, FileNotFoundError):
        # The file can disappear between the check and the open. That is still a missing file.
        if not target.is_file():
            raise NoDatabase(path) from None
        raise


# The name the tests and older callers use.
_connect = open_read_only


def last_written(path: Path | str) -> datetime | None:
    """The local time the database or its -wal file was last changed, whichever is later. None when neither can be read.

    The -shm file is not counted: a reader can touch it without writing a row.
    """
    target = Path(path)
    times = []
    for candidate in (target, _sidecar(target, "-wal")):
        try:
            times.append(candidate.stat().st_mtime)
        except OSError:
            continue
    return datetime.fromtimestamp(max(times)) if times else None


def _select(path: Path | str, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    conn = _connect(path)
    try:
        fetched = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [dict(zip(PROJECT_COLUMNS, row)) for row in fetched]


def list_projects(path: Path | str) -> list[dict[str, Any]]:
    """Return every project row, newest first by created_at. Each row is keyed by PROJECT_COLUMNS."""
    return _select(path, _LIST_SQL)


def get_project(path: Path | str, project_id: str) -> dict[str, Any] | None:
    """Return the project row with this id, keyed by PROJECT_COLUMNS, or None when there is none."""
    found = _select(path, _GET_SQL, (project_id,))
    return found[0] if found else None


__all__ = ["PROJECT_COLUMNS", "NoDatabase", "get_project", "last_written", "list_projects", "open_read_only"]
