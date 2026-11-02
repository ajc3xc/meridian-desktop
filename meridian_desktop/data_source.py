"""Choose the data source for the Projects and Sessions views. Pure Python: no Qt import.

``read_in_order`` is the one source order both tabs use: the Projects tab through ``resolve``, and the Sessions tab
for the picked project's sessions. ``resolve`` reads the mode from Settings and returns one source, never two.

Local mode, in this order:
- the local Meridian server, when local_server_answers says it answered: source "server". Its answer is final, an
  error included. local_server_answers is the one check of the local server for both tabs: it is True only when the
  probe state is running. A refused connection, a timeout, a body that is not Meridian's health answer, or a refused
  address is False, so the next source is asked. The Sessions tab calls the same check, with the same probe, through
  ProjectsProvider.local_server_answers, so both tabs choose the same source for the same server.
- otherwise hosted Meridian, only when a hosted token is saved: source "hosted". Its answer is final when hosted
  answers, and so is a refusal: a token refused (401, 403), a page not found (404), or a reply that is not the projects
  list. Those are shown as they are, with no backup.
- otherwise, when hosted did not answer (the network failed: refused, timed out, lost; or the server errored: HTTP 5xx):
  the newest backup of the hosted projects (projects_backup), shown with the label "Backup from <date>. Hosted did not
  answer.": source "backup". When there is no backup, the hosted failure is shown as it is, and the database file is not
  read in its place.
- otherwise, with no token saved, the database file, read-only: source "file". A db_path chosen in Settings always
  wins. With no db_path, the file is <core_checkout>/data/meridian.db, the core's default database, which the core
  opens from its checkout. That file is a snapshot, so its Resolution carries the snapshot_note as its message, and the
  Projects tab shows it after the count. A chosen db_path that is gone is not replaced by the core file.
- otherwise state "no_local_data", with a message that names each place checked.

Hosted mode:
- no stored token: state "not_signed_in". Nothing is probed, read or fetched.
- a stored token: the rows from the hosted client, source "hosted". When hosted did not answer (see above), the newest
  backup, as in local mode, else the hosted failure. The local server and the database file are never read in hosted
  mode.

Tunnel mode reads exactly as hosted mode does. Hosted tools reach this computer through the
tunnel, so the projects come from the same hosted client with the same token. The tunnel's
state is shown on the top bar, not checked here, and a stopped tunnel does not change what is read.

This module makes no network call and reads no database itself: it only checks whether a file exists. Each outside
effect is an injected callable in DataSources. The app passes the real functions (local_client.probe_health,
local_client.list_projects, projects_reader.list_projects, token_store.get_token, hosted_client.list_projects and
projects_reader.last_written), and tests pass fakes. The token goes only to the hosted client. It never appears in a
Resolution, a message or an error.

ProjectsProvider is the Sessions tab's view of the same resolve: the (id, name) pairs for its picker, with the
local-server answer of that same resolve (resolve_with_local_check), so a full read asks the server only once. The
picker lists the live sources only (the local server, hosted), never the backup: the provider's view has no backup
step. It also has the local-server check its local reads use, the database file its file read uses, and the two
settings that name that file, so the tab can read again when either changes.

A failed read gives state "read_failed", with the chosen source and no rows. A HostedError
(from hosted_client) gives its own fixed text, which holds no token, body or path. Any other
exception is named by its class only, never a path, a token or a response body.
"""

from __future__ import annotations

import http.client
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from meridian_desktop import local_client, projects_backup, projects_reader
from meridian_desktop.hosted_client import HostedError, HostedTimeout, HostedUnreachable
from meridian_desktop.settings_store import VALID_MODES, Settings

LOCAL_MODE = "local"
# The modes whose projects come from the hosted client: hosted, and tunnel (reached through the tunnel).
HOSTED_PATH_MODES = ("hosted", "tunnel")

# What one tab's read gives back: a Resolution for Projects, a SessionsResult for Sessions.
T = TypeVar("T")

SOURCE_SERVER = "server"
SOURCE_FILE = "file"
SOURCE_HOSTED = "hosted"
SOURCE_BACKUP = "backup"

STATE_OK = "ok"
STATE_NO_LOCAL_DATA = "no_local_data"
STATE_NOT_SIGNED_IN = "not_signed_in"
STATE_READ_FAILED = "read_failed"

# The core's default database, relative to its checkout (the core runs from there, so data/meridian.db lands there).
CORE_DATA_FILE = Path("data") / "meridian.db"

# The no-data message names each place that was checked. See no_local_data_message for the three parts.
NO_LOCAL_DATA_MESSAGE = (
    "No local Meridian data is available. Checked: {server}; {saved}; {core}. "
    "Start Meridian on this computer, or choose a database file in Settings."
)
_SERVER_CHECKED = "no Meridian server answered at {url}"
_SAVED_NONE = "no database file is chosen in Settings"
_SAVED_GONE = "the chosen database file {path} was not found"
_CORE_NOT_USED = "the core folder file is not used while a database file is chosen"
_CORE_UNSET = "no core folder is set, so no core data file was checked"
_CORE_GONE = "no data file at {path}"

SNAPSHOT_NOTE = "Read-only copy from the core folder, last written {date}. The local server is not running."
SNAPSHOT_NOTE_UNDATED = "Read-only copy from the core folder. The local server is not running."
_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

NOT_SIGNED_IN_MESSAGE = (
    "No hosted token is saved, so no hosted projects are loaded. "
    "Paste your hosted token in Settings and press Save token."
)
_READ_FAILED_TEXT = {
    SOURCE_SERVER: "The local Meridian server could not be read",
    SOURCE_FILE: "The database file could not be read",
    SOURCE_HOSTED: "Hosted Meridian projects could not be loaded",
}

Row = dict[str, Any]
# Each callable does its own I/O. The resolver only decides which one is called.
ProbeFn = Callable[[str], str]  # local URL -> probe state (local_client.probe_health)
ReadServerFn = Callable[[str], list[Row]]  # local URL -> projects from the running server
ReadFileFn = Callable[[Path], list[Row]]  # database path -> projects (projects_reader.list_projects)
GetTokenFn = Callable[[], str | None]  # -> the stored hosted token, or None (token_store.get_token)
ListHostedFn = Callable[[str, str], list[Row]]  # (hosted URL, token) -> projects from the hosted client
LastWrittenFn = Callable[[Path], datetime | None]  # database path -> when it was last written (projects_reader)
BackupFn = Callable[[], projects_backup.Backup | None]  # -> the newest hosted-projects backup, or None


@dataclass(frozen=True)
class DataSources:
    """The callables the resolver may call. Each is injected, so nothing here is wired to a host or a file.

    ``read_backup`` is the newest backup of the hosted projects. None means there is no backup step: the Sessions
    picker's view (ProjectsProvider) has none, so the picker lists the live sources only.
    """

    probe: ProbeFn
    read_server: ReadServerFn
    read_file: ReadFileFn
    get_token: GetTokenFn
    list_hosted: ListHostedFn
    last_written: LastWrittenFn = projects_reader.last_written
    read_backup: BackupFn | None = None


@dataclass(frozen=True)
class LocalDatabase:
    """The database file the local mode reads. ``from_core`` is True for the core folder's file, False for db_path."""

    path: Path
    from_core: bool


@dataclass(frozen=True)
class Resolution:
    """The result for one mode. ``source`` is None when no source was used."""

    state: str
    source: str | None
    rows: list[Row] = field(default_factory=list)
    message: str = ""


def _is_file(text: str) -> bool:
    """True when ``text`` names an existing regular file. An unusable path reads as not a file."""
    try:
        return Path(text).is_file()
    except (OSError, ValueError):
        return False


def core_database_path(settings: Settings) -> Path | None:
    """<core_checkout>/data/meridian.db, or None when no core folder is set. Whether the file exists is not checked."""
    checkout = settings.core_checkout.strip()
    return Path(checkout) / CORE_DATA_FILE if checkout else None


def local_database(settings: Settings) -> LocalDatabase | None:
    """The file local mode reads: db_path when it is set (always, even when it is gone), else the core folder's file.

    None when the file that applies is not there.
    """
    if settings.db_path is not None:
        return LocalDatabase(Path(settings.db_path), False) if _is_file(settings.db_path) else None
    core = core_database_path(settings)
    if core is not None and _is_file(str(core)):
        return LocalDatabase(core, True)
    return None


def snapshot_note(written: datetime | None) -> str:
    """The label for rows read from the core folder's file, with the day it was last written when that is known."""
    if written is None:
        return SNAPSHOT_NOTE_UNDATED
    return SNAPSHOT_NOTE.format(date=f"{written.day} {_MONTHS[written.month - 1]} {written.year}")


def file_note(database: LocalDatabase, last_written: LastWrittenFn) -> str:
    """The snapshot label for the core folder's file. A file the owner chose carries no label."""
    if not database.from_core:
        return ""
    try:
        written = last_written(database.path)
    except Exception:  # noqa: BLE001 - an unreadable time only drops the date from the label
        written = None
    return snapshot_note(written)


def no_local_data_message(settings: Settings) -> str:
    """Plain words that name what local mode checked: the local address, the saved database file and the core file."""
    if settings.db_path is not None:
        saved = _SAVED_GONE.format(path=settings.db_path)
        core = _CORE_NOT_USED
    else:
        saved = _SAVED_NONE
        core_path = core_database_path(settings)
        core = _CORE_UNSET if core_path is None else _CORE_GONE.format(path=core_path)
    return NO_LOCAL_DATA_MESSAGE.format(server=_SERVER_CHECKED.format(url=settings.local_url), saved=saved, core=core)


def local_server_answers(local_url: str, probe: ProbeFn) -> bool:
    """Whether the local Meridian server answered: the one check both the Projects and the Sessions tab use.

    True only when ``probe`` (local_client.probe_health in the app) gives HEALTH_RUNNING: GET /health answered 200 with
    Meridian's health body. Every other state is False, so the tab asks the next source: not_running (a refused
    connection), not_meridian (another body, status or service) and error (a timeout, or an address that is not
    loopback).
    """
    return probe(local_url) == local_client.HEALTH_RUNNING


def _usable_token(token: object) -> bool:
    return isinstance(token, str) and bool(token.strip())


def _read_failed_message(source: str, exc: BaseException) -> str:
    if isinstance(exc, HostedError):
        # The hosted client's messages are fixed text: no token, no response body, no raw exception.
        return f"{_READ_FAILED_TEXT[source]}. {exc}"
    return f"{_READ_FAILED_TEXT[source]} ({exc.__class__.__name__})."


def _hosted_did_not_answer(exc: BaseException) -> bool:
    """True when hosted gave no answer that the backup may stand in for: the network failed, or the server errored.

    That is a connection refused, a timeout, a lost connection, or an HTTP 5xx. A refusal is an answer, and is shown as
    it is: a token refused (401, 403), a page not found (404), a redirect, a reply that is not the projects list, or any
    other error with no network reason.
    """
    if isinstance(exc, HostedError):
        if isinstance(exc, (HostedUnreachable, HostedTimeout)):
            return True
        return exc.status is not None and exc.status >= 500
    # A raw network error from an injected reader. The hosted client maps its own to the typed errors above.
    return isinstance(exc, (OSError, http.client.HTTPException))


def _read(source: str, load: Callable[[], list[Row]]) -> Resolution:
    """Run one injected read. Any failure becomes state read_failed, reported by class name only."""
    try:
        rows = list(load())
    except Exception as exc:  # noqa: BLE001 - a failed read is reported by class name only
        return Resolution(STATE_READ_FAILED, source, message=_read_failed_message(source, exc))
    return Resolution(STATE_OK, source, rows=rows)


def _after_hosted(
    token: str,
    hosted: Callable[[str], T | None],
    backup: Callable[[], T | None] | None,
) -> T | None:
    """A saved token's read: hosted's answer when it answers, else the backup when the caller gives one, else None.

    None from ``hosted`` means it did not answer. The caller keeps the reason, so a failure with no backup is shown.
    """
    answered = hosted(token)
    if answered is not None:
        return answered
    return backup() if backup is not None else None


def read_in_order(
    mode: str,
    get_token: Callable[[], object],
    *,
    server: Callable[[], T | None],
    hosted: Callable[[str], T | None],
    file: Callable[[], T | None],
    backup: Callable[[], T | None] | None = None,
) -> T | None:
    """The one source order of the Projects and Sessions tabs. Return the first answer, or None when none answered.

    Each tab passes its own reads. A read that does not answer returns None. Local mode asks them in this order:

    1. ``server()``, the local Meridian server. None means no local server answered, so the next source is asked. Any
       other answer is final, an error included.
    2. ``hosted(token)``, only when a hosted token is saved. Its answer is final when it is not None, and a refusal (a
       401, 403 or 404 reply) is an answer too: the caller returns it as the failure it is. None means hosted did not
       answer (the network failed, or a 5xx): the next source is asked.
    3. ``backup()``, only after hosted did not answer, and only when the caller gives one. None means no backup, and
       then nothing is returned, so the caller shows hosted's own failure, as it does with no backup.
    4. ``file()``, only when no token is saved. None means no database file was found. A saved token never reads the
       file, whether hosted answers or not.

    Hosted and tunnel modes ask ``hosted(token)`` when a token is saved, then ``backup()`` when hosted did not answer,
    and nothing else: the local server and the database file are never asked. With no token, neither hosted nor the
    backup is asked. ``get_token`` is called at most once, and not at all when the local server answered.
    Raise ValueError when the mode is not one of VALID_MODES, before anything is asked.
    """
    if mode == LOCAL_MODE:
        answered = server()
        if answered is not None:
            return answered
        token = get_token()
        if isinstance(token, str) and _usable_token(token):
            return _after_hosted(token, hosted, backup)
        return file()
    if mode in HOSTED_PATH_MODES:
        token = get_token()
        if isinstance(token, str) and _usable_token(token):
            return _after_hosted(token, hosted, backup)
        return None
    raise ValueError(f"mode must be one of {VALID_MODES}, got {mode!r}")


def _backup_resolution(read_backup: BackupFn | None) -> Resolution | None:
    """The newest hosted-projects backup as a result, with its label. None when there is none, or it cannot be read."""
    if read_backup is None:
        return None
    try:
        found = read_backup()
    except Exception:  # noqa: BLE001 - a backup that cannot be read is no backup
        return None
    if found is None:
        return None
    label = projects_backup.backup_label(found.taken_at)
    return Resolution(STATE_OK, SOURCE_BACKUP, rows=list(found.projects), message=label)


def resolve(settings: Settings, sources: DataSources) -> Resolution:
    """Return the one source the mode uses, with its rows, or the state that says why there is none.

    The order is read_in_order's. "hosted" and "tunnel" both read through the hosted path. A failed hosted read is
    passed to the backup; when there is no backup, that failure is the result, as it is with no backup. Raise ValueError
    when the mode is not one of VALID_MODES.
    """
    # A failed hosted read. It is the result only when no backup answers, so it is kept here until then.
    hosted_failures: list[Resolution] = []

    def server() -> Resolution | None:
        if not local_server_answers(settings.local_url, sources.probe):
            return None
        # A running server wins. The file is not read, so the result holds one source only.
        return _read(SOURCE_SERVER, lambda: sources.read_server(settings.local_url))

    def hosted(token: str) -> Resolution | None:
        try:
            rows = list(sources.list_hosted(settings.hosted_url, token))
        except Exception as exc:  # noqa: BLE001 - a failed read is reported by class name, or the client's own words
            failure = Resolution(STATE_READ_FAILED, SOURCE_HOSTED, message=_read_failed_message(SOURCE_HOSTED, exc))
            if not _hosted_did_not_answer(exc):
                # Hosted answered with a refusal (a token refused, a page not found, and so on): that is the answer.
                return failure
            # Hosted did not answer: the backup is asked next. The failure stays, the result if no backup is there.
            hosted_failures.append(failure)
            return None
        return Resolution(STATE_OK, SOURCE_HOSTED, rows=rows)

    def backup() -> Resolution | None:
        return _backup_resolution(sources.read_backup)

    def database_file() -> Resolution | None:
        database = local_database(settings)
        if database is None:
            return None
        outcome = _read(SOURCE_FILE, lambda: sources.read_file(database.path))
        if outcome.state == STATE_OK:
            return replace(outcome, message=file_note(database, sources.last_written))
        return outcome

    outcome = read_in_order(
        settings.mode,
        sources.get_token,
        server=server,
        hosted=hosted,
        file=database_file,
        backup=backup,
    )
    if outcome is not None:
        return outcome
    if hosted_failures:
        # Hosted did not answer and there is no backup: the hosted failure is shown as it is.
        return hosted_failures[0]
    if settings.mode == LOCAL_MODE:
        return Resolution(STATE_NO_LOCAL_DATA, None, message=no_local_data_message(settings))
    # Hosted or tunnel mode with no token: the probe, the file and the hosted client were never called.
    return Resolution(STATE_NOT_SIGNED_IN, None, message=NOT_SIGNED_IN_MESSAGE)


class ProjectsUnresolved(Exception):
    """The resolver found no projects to list. ``message`` is the resolver's own plain text, safe to show as it is.

    ``local_answered`` is whether the local server answered in that resolve, or None when the resolve did not say.
    """

    def __init__(self, message: str, *, local_answered: bool | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.local_answered = local_answered


class ProjectsProvider:
    """The Sessions tab's view of the Projects resolve. It holds a settings loader and the sources, never a window.

    Calling it returns the resolved projects as (id, name) pairs, or raises ProjectsUnresolved with the resolver's
    message (no source, or a failed read such as a hosted 401). local_server_answers is the resolver's own local-server
    check, with the same probe, for the tab's local reads. local_file names the database file the tab's file read
    uses, with its snapshot label; the tab asks for it only where read_in_order reaches the file. watch_key is the two
    settings that choose that file, so the tab can read again when either one changes.
    """

    def __init__(self, load_settings: Callable[[], Settings], sources: DataSources) -> None:
        self._load = load_settings
        # The picker lists the live sources only. Its view has no backup step, so a backup project is never listed for a
        # read that then goes to hosted (the Sessions read of one project asks hosted, not the backup).
        self._sources = replace(sources, read_backup=None)

    def __call__(self) -> list[tuple[str, str]]:
        pairs, _local_answered = self.resolve_with_local_check()
        return pairs

    def resolve_with_local_check(self) -> tuple[list[tuple[str, str]], bool]:
        """The resolved projects as (id, name) pairs, and whether the local server answered in this one resolve.

        One resolve asks the local server once. Its answer is returned here, so the Sessions full read reuses it and
        does not ask again. Raises ProjectsUnresolved, carrying the same answer, when the resolve found no projects.
        """
        outcome = resolve(self._load(), self._sources)
        # A running server is the only source whose resolution names SOURCE_SERVER: resolve returns it only when the
        # check answered, and asks no other source in that case.
        local_answered = outcome.source == SOURCE_SERVER
        if outcome.state != STATE_OK:
            raise ProjectsUnresolved(outcome.message, local_answered=local_answered)
        pairs: list[tuple[str, str]] = []
        for row in outcome.rows:
            pid = row.get("id")
            if isinstance(pid, str) and pid.strip():
                name = row.get("name")
                pairs.append((pid, name if isinstance(name, str) else ""))
        return pairs, local_answered

    def local_server_answers(self, local_url: str) -> bool:
        """data_source.local_server_answers with the probe resolve uses, so Sessions decides as Projects does.

        Runs on the worker. ``local_url`` is the address the tab then reads, the Local URL from Settings.
        """
        return local_server_answers(local_url, self._sources.probe)

    def local_file(self) -> tuple[Path, str] | None:
        """(path, snapshot label) of the file local mode reads, or None when there is none. Runs on the worker."""
        database = local_database(self._load())
        if database is None:
            return None
        return database.path, file_note(database, self._sources.last_written)

    def watch_key(self) -> tuple[str, str]:
        """(db_path, core_checkout) as saved now. A change in either one changes which file is read."""
        settings = self._load()
        return settings.db_path or "", settings.core_checkout


__all__ = [
    "CORE_DATA_FILE",
    "HOSTED_PATH_MODES",
    "LOCAL_MODE",
    "NOT_SIGNED_IN_MESSAGE",
    "NO_LOCAL_DATA_MESSAGE",
    "SNAPSHOT_NOTE",
    "SOURCE_BACKUP",
    "SOURCE_FILE",
    "SOURCE_HOSTED",
    "SOURCE_SERVER",
    "STATE_NOT_SIGNED_IN",
    "STATE_NO_LOCAL_DATA",
    "STATE_OK",
    "STATE_READ_FAILED",
    "DataSources",
    "LocalDatabase",
    "ProjectsProvider",
    "ProjectsUnresolved",
    "Resolution",
    "core_database_path",
    "file_note",
    "local_database",
    "local_server_answers",
    "no_local_data_message",
    "read_in_order",
    "resolve",
    "snapshot_note",
]
