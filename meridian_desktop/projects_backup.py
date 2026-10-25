"""Backup of the hosted projects to this computer, and the reader of the newest backup. Pure Python: no Qt import.

A backup is one JSON file, ``projects-<UTC time>.json``, in the ``backups`` folder beside settings.json: that is
%LOCALAPPDATA%\\meridian-desktop\\backups, or ~/.meridian-desktop/backups when LOCALAPPDATA is unset. It holds only each
project's id, name and created_at, and the time it was taken. It never holds the token, the hosted URL, or any other
field of the hosted reply.

backup_hosted is the one write. It reads the hosted project list through the list function it is given (the app passes
the hosted client's GET /projects), then writes the file. A failed read writes nothing, so a failure never removes a
backup. A read that gives no usable project (an empty list, or no row with an id) writes nothing either, and raises
NoUsableProjects: an empty answer never stands in for a backup that holds projects. After a successful write the newest
BACKUPS_KEPT backup files are kept, and so is the newest backup that holds projects, even when newer files hold none
(prune_backups). Older backup files are removed.

newest_backup reads back the newest backup that holds projects. A file that does not parse, or holds no project, is
skipped. The Projects tab uses it as the answer when hosted did not answer (data_source.resolve). Nothing in this module
sends anything to the hosted service.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from meridian_desktop.settings_store import default_settings_path

BACKUP_DIR_NAME = "backups"
BACKUP_VERSION = 1
# After a write, the newest this many backup files are kept, by name. The newest backup that holds projects is kept too.
BACKUPS_KEPT = 5
# The file name, and the time inside it, both in UTC. The time is fixed width, so names sort oldest to newest.
_NAME = re.compile(r"projects-(\d{8}T\d{6})Z\.json")
_NAME_TIME = "%Y%m%dT%H%M%S"
_JSON_TIME = "%Y-%m-%dT%H:%M:%SZ"
# The only fields a backup keeps for a project.
KEPT_FIELDS = ("id", "name", "created_at")

_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

NO_TOKEN_TEXT = (
    "No hosted token is saved, so there are no hosted projects to back up. "
    "Paste your hosted token in Settings and press Save token."
)
NO_PROJECTS_TEXT = "Hosted Meridian returned no projects, so no backup was written. Earlier backups are kept."

ListHosted = Callable[[str, str], list[dict[str, Any]]]  # (hosted URL, token) -> the hosted client's project rows


@dataclass(frozen=True)
class Backup:
    """The projects in the newest backup, each with the KEPT_FIELDS only, and the UTC time it was taken."""

    projects: list[dict[str, Any]]
    taken_at: datetime


@dataclass(frozen=True)
class BackupWritten:
    """A backup that was written: its file, how many projects it holds, its time, and the status line for it."""

    path: Path
    count: int
    taken_at: datetime
    message: str


class NoUsableProjects(Exception):
    """The rows hold no project with a usable id, so there is nothing to back up. Nothing is written or removed.

    The message is NO_PROJECTS_TEXT, the status line the tab shows for it.
    """

    def __init__(self) -> None:
        super().__init__(NO_PROJECTS_TEXT)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(when: datetime) -> datetime:
    """``when`` as an aware UTC time. A naive time is taken as UTC, so no local zone is guessed."""
    if when.tzinfo is None:
        return when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def day_text(when: datetime) -> str:
    """The UTC day in plain words, such as 24 October 2026."""
    utc = _utc(when)
    return f"{utc.day} {_MONTHS[utc.month - 1]} {utc.year}"


def backup_dir(env: Mapping[str, str] | None = None, home: Path | None = None) -> Path:
    """The backups folder: beside settings.json, so it follows the same LOCALAPPDATA rule as the settings file."""
    return default_settings_path(env, home).parent / BACKUP_DIR_NAME


def backup_name(taken_at: datetime) -> str:
    return "projects-" + _utc(taken_at).strftime(_NAME_TIME) + "Z.json"


def backed_up_text(count: int, taken_at: datetime) -> str:
    noun = "project" if count == 1 else "projects"
    return f"Backed up {count} {noun} on {day_text(taken_at)}."


def backup_label(taken_at: datetime) -> str:
    """The label a backup carries when it is shown in place of hosted projects."""
    return f"Backup from {day_text(taken_at)}. Hosted did not answer."


def _text_or_none(value: Any) -> str | None:
    if value is None or isinstance(value, str):
        return value
    return str(value)


def _kept_row(row: Any) -> dict[str, Any] | None:
    """The KEPT_FIELDS of one project row, or None when the row has no usable id."""
    if not isinstance(row, Mapping):
        return None
    pid = row.get("id")
    if not isinstance(pid, str) or not pid.strip():
        return None
    return {"id": pid, "name": _text_or_none(row.get("name")), "created_at": _text_or_none(row.get("created_at"))}


def write_backup(rows: list[Any], *, folder: Path, taken_at: datetime) -> BackupWritten:
    """Write the rows as the newest backup in ``folder``, then prune the backup files (prune_backups).

    Only the KEPT_FIELDS of each row go in, and a row with no usable id is left out. When no row is left, nothing is
    written or removed, and NoUsableProjects is raised.
    """
    taken = _utc(taken_at)
    projects = [kept for kept in (_kept_row(row) for row in rows) if kept is not None]
    if not projects:
        raise NoUsableProjects()
    document = {"version": BACKUP_VERSION, "taken_at": taken.strftime(_JSON_TIME), "projects": projects}
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / backup_name(taken)
    temp = folder / (path.name + ".tmp")
    temp.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)
    prune_backups(folder, keep=path)
    return BackupWritten(path, len(projects), taken, backed_up_text(len(projects), taken))


def backup_hosted(
    list_hosted: ListHosted,
    hosted_url: str,
    token: str,
    *,
    folder: Path,
    taken_at: datetime,
) -> BackupWritten:
    """Read the hosted project list with ``list_hosted`` and write it as the newest backup.

    A failed read raises before anything is written. An answer with no usable project raises NoUsableProjects, and
    writes and removes nothing. The token is passed to ``list_hosted`` and goes nowhere else.
    """
    rows = list_hosted(hosted_url, token)
    return write_backup(rows, folder=folder, taken_at=taken_at)


def _backup_files(folder: Path) -> list[Path]:
    """The backup files in ``folder``, newest first. Only names this module writes are listed."""
    try:
        found = [path for path in folder.iterdir() if path.is_file() and _NAME.fullmatch(path.name)]
    except OSError:
        return []
    return sorted(found, key=lambda path: path.name, reverse=True)


def prune_backups(folder: Path, keep: Path | None = None) -> None:
    """Remove the backup files that retention does not keep, and leave every other file as it is.

    Kept: the newest BACKUPS_KEPT backup files by name, the newest backup that holds projects (even when newer files
    hold none), and ``keep``, the file just written. A file that cannot be removed is left as it is.
    """
    files = _backup_files(folder)  # newest first
    kept = set(files[:BACKUPS_KEPT])
    newest_with_projects = next((path for path in files if _usable(path) is not None), None)
    if newest_with_projects is not None:
        kept.add(newest_with_projects)
    if keep is not None:
        kept.add(keep)
    for path in files:
        if path not in kept:
            try:
                path.unlink()
            except OSError:
                pass


def _read_one(path: Path) -> Backup | None:
    """The backup in ``path``, or None when the file does not parse as one."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        taken_at = datetime.strptime(document["taken_at"], _JSON_TIME).replace(tzinfo=timezone.utc)
        projects = document["projects"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(projects, list):
        return None
    rows = [kept for kept in (_kept_row(row) for row in projects) if kept is not None]
    return Backup(rows, taken_at)


def _usable(path: Path) -> Backup | None:
    """The backup in ``path`` when it parses and holds at least one project, else None."""
    found = _read_one(path)
    return found if found is not None and found.projects else None


def newest_backup(folder: Path | None = None) -> Backup | None:
    """The newest backup that holds projects, from ``folder`` (the desktop backups folder by default), or None.

    A file that does not parse, and a file that holds no project, is skipped.
    """
    for path in _backup_files(backup_dir() if folder is None else folder):
        found = _usable(path)
        if found is not None:
            return found
    return None


__all__ = [
    "BACKUP_DIR_NAME",
    "BACKUPS_KEPT",
    "KEPT_FIELDS",
    "NO_PROJECTS_TEXT",
    "NO_TOKEN_TEXT",
    "Backup",
    "BackupWritten",
    "NoUsableProjects",
    "backed_up_text",
    "backup_dir",
    "backup_hosted",
    "backup_label",
    "backup_name",
    "day_text",
    "newest_backup",
    "prune_backups",
    "utc_now",
    "write_backup",
]
