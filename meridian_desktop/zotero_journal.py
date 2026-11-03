"""The Zotero save journal: the outcome of each item write, kept on disk so it survives quitting. Stdlib only, no Qt.

Each entry holds one line of the reading list and an outcome. An item's entry is written as "writing" before its write
starts, and changed to landed, unknown or refused once the outcome is known. An entry that is still writing is held,
and so is an entry that is unknown, until the person checks Zotero and clears it. Only the line text, an id, an outcome
and a time are stored. Never a token, a grant key or a response body.

Several windows, and several processes, may use the journal at once. So every operation runs under an exclusive lock on
a sidecar lock file. Inside the lock it re-reads the journal from disk, applies one change, and writes the result.
Nothing writes from a copy that was read earlier. Before the journal is replaced, its new contents are first written to
a last-good backup. A journal that is missing or cannot be parsed, after the backup has been written once, is unreadable,
and its message names the lines the backup holds.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from meridian_desktop.settings_store import _replace_with_retry, default_settings_path

JOURNAL_NAME = "zotero_saves.json"
BACKUP_SUFFIX = ".bak"
LOCK_SUFFIX = ".lock"
JOURNAL_VERSION = 1
MAX_ENTRIES = 500
LOCK_TIMEOUT_SECONDS = 10.0
LOCK_POLL_SECONDS = 0.02
WRITING = "writing"
LANDED = "landed"
UNKNOWN = "unknown"
REFUSED = "refused"
OUTCOMES = (WRITING, LANDED, UNKNOWN, REFUSED)
HELD_OUTCOMES = (WRITING, UNKNOWN)
WRITE_FAILED_TEXT = (
    "The Zotero save journal could not be written, so nothing more was saved. Check Zotero before you save again."
)
LOCK_TIMEOUT_TEXT = "The Zotero save journal is busy in another window. Try again in a moment."
LINE_HELD_TEXT = (
    "That line is already held, or is being written in another window, so it was not sent. Check Zotero before you "
    "save it again."
)
NO_ENTRY_TEXT = "The Zotero save journal has no entry for that write, so its outcome was not recorded."
_FIELDS = ("id", "line", "outcome", "at")


def journal_unreadable_text(path: Path, held_lines: Sequence[str] = ()) -> str:
    """The message when the journal cannot be read. It names the held lines the backup holds, when there are any."""
    message = (
        "The Zotero save journal could not be read, so saving is off. Check Zotero before moving or fixing "
        f"{path}, then restart the desktop."
    )
    if held_lines:
        message += " These lines may already be in Zotero and stay held: " + "; ".join(held_lines) + "."
    return message


class JournalError(Exception):
    """The journal could not be read, written or locked. ``message`` is fixed text for a person to read.

    ``held_lines`` names the lines the last-good backup holds, when the journal is unreadable.
    """

    def __init__(self, message: str, held_lines: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.message = message
        self.held_lines = tuple(held_lines)


@dataclass(frozen=True)
class JournalEntry:
    """One line of the reading list and its outcome. ``at`` is the UTC time of the last change, in ISO 8601."""

    id: str
    line: str
    outcome: str
    at: str


def default_path() -> Path:
    """The journal beside the desktop's settings file, in the desktop state folder."""
    return default_settings_path().parent / JOURNAL_NAME


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _parse_entries(raw: bytes) -> list[JournalEntry] | None:
    """The entries in a journal's bytes, or None when they are not a journal. Never raises."""
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    entries = value.get("entries") if isinstance(value, dict) else None
    if not isinstance(entries, list):
        return None
    parsed: list[JournalEntry] = []
    for item in entries:
        if not isinstance(item, dict) or not all(isinstance(item.get(name), str) for name in _FIELDS):
            return None
        if item["outcome"] not in OUTCOMES:
            return None
        parsed.append(JournalEntry(**{name: item[name] for name in _FIELDS}))
    return parsed


def _serialize(entries: Sequence[JournalEntry]) -> bytes:
    payload = {"version": JOURNAL_VERSION, "entries": [{name: getattr(e, name) for name in _FIELDS} for e in entries]}
    return json.dumps(payload, indent=2).encode("utf-8")


def _pruned(entries: Sequence[JournalEntry]) -> list[JournalEntry]:
    """Keep every held entry, and the most recent of the rest, up to MAX_ENTRIES in all."""
    if len(entries) <= MAX_ENTRIES:
        return list(entries)
    held = [entry for entry in entries if entry.outcome in HELD_OUTCOMES]
    others = [entry for entry in entries if entry.outcome not in HELD_OUTCOMES]
    room = max(MAX_ENTRIES - len(held), 0)
    kept_others = {id(entry) for entry in others[len(others) - room :]} if room else set()
    return [entry for entry in entries if entry.outcome in HELD_OUTCOMES or id(entry) in kept_others]


def _atomic_write(target: Path, data: bytes) -> None:
    """Write ``data`` to a temporary file beside ``target``, then replace ``target`` with it. Retries a Windows lock."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(temp, target)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def _lock_file(handle) -> None:
    """Take an exclusive lock on the whole lock file without waiting. Raises OSError while another holder has it."""
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class SaveJournal:
    """The journal at ``path``. Every operation re-reads the file under the lock, so windows and processes never drop
    each other's entries.

    Constructing it reads the journal, and raises JournalError if the journal is unreadable.
    """

    def __init__(self, path: Path, *, lock_timeout: float = LOCK_TIMEOUT_SECONDS) -> None:
        self._path = Path(path)
        self._backup = self._path.with_name(self._path.name + BACKUP_SUFFIX)
        self._lock_path = self._path.with_name(self._path.name + LOCK_SUFFIX)
        self._lock_timeout = lock_timeout
        with self._locked():
            self._read_locked()

    @property
    def path(self) -> Path:
        return self._path

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Hold the exclusive lock for one operation. Waits up to the lock timeout, then raises JournalError."""
        try:
            self._lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(self._lock_path, "a+b")
        except OSError:
            raise JournalError(WRITE_FAILED_TEXT) from None
        try:
            deadline = time.monotonic() + self._lock_timeout
            while True:
                try:
                    _lock_file(handle)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise JournalError(LOCK_TIMEOUT_TEXT) from None
                    time.sleep(LOCK_POLL_SECONDS)
            try:
                yield
            finally:
                try:
                    _unlock_file(handle)
                except OSError:
                    pass
        finally:
            handle.close()

    def _backup_held(self) -> tuple[str, ...]:
        """The lines the last-good backup holds as writing or unknown. Empty when the backup is missing or unreadable."""
        try:
            raw = self._backup.read_bytes()
        except OSError:
            return ()
        entries = _parse_entries(raw) or []
        return tuple(entry.line for entry in entries if entry.outcome in HELD_OUTCOMES)

    def _unreadable(self) -> JournalError:
        held = self._backup_held()
        return JournalError(journal_unreadable_text(self._path, held), held)

    def _read_locked(self) -> list[JournalEntry]:
        """The journal as it is on disk now. Caller holds the lock. A missing journal is empty only if no write has
        ever happened, which the backup records."""
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            if self._backup.exists():
                raise self._unreadable() from None
            return []
        except OSError:
            raise self._unreadable() from None
        entries = _parse_entries(raw)
        if entries is None:
            raise self._unreadable()
        return entries

    def _write_locked(self, entries: Sequence[JournalEntry]) -> None:
        """Write the journal. The new contents go to the last-good backup first, then replace the journal. Caller holds the lock."""
        data = _serialize(_pruned(entries))
        try:
            _atomic_write(self._backup, data)
            _atomic_write(self._path, data)
        except OSError:
            raise JournalError(WRITE_FAILED_TEXT) from None

    def held(self) -> list[JournalEntry]:
        """The entries that hold a line, read from disk under the lock: writing, or unknown."""
        with self._locked():
            return [entry for entry in self._read_locked() if entry.outcome in HELD_OUTCOMES]

    def begin(self, line: str) -> str:
        """Record that the write of ``line`` is about to start. Returns the entry's id.

        Refused when another entry already holds the same line, so two windows cannot both post it.
        """
        entry = JournalEntry(id=uuid.uuid4().hex, line=line, outcome=WRITING, at=_now())
        with self._locked():
            entries = self._read_locked()
            if any(other.line == line and other.outcome in HELD_OUTCOMES for other in entries):
                raise JournalError(LINE_HELD_TEXT)
            self._write_locked([*entries, entry])
        return entry.id

    def finish(self, entry_id: str, outcome: str) -> None:
        """Record the outcome of a write that was begun. ``outcome`` is landed, unknown or refused."""
        if outcome not in (LANDED, UNKNOWN, REFUSED):
            raise ValueError(f"not a finished outcome: {outcome}")
        with self._locked():
            entries = self._read_locked()
            if not any(entry.id == entry_id for entry in entries):
                raise JournalError(NO_ENTRY_TEXT)
            updated = [replace(e, outcome=outcome, at=_now()) if e.id == entry_id else e for e in entries]
            self._write_locked(updated)

    def record_unknown(self, lines: Sequence[str]) -> None:
        """Hold lines whose outcome is not known at all, such as when a save produced no result."""
        stamp = _now()
        added = [JournalEntry(id=uuid.uuid4().hex, line=line, outcome=UNKNOWN, at=stamp) for line in lines]
        with self._locked():
            entries = self._read_locked()
            self._write_locked([*entries, *added])

    def clear(self, entry_ids: Sequence[str]) -> int:
        """Remove the given entries, but only the unknown ones. A writing entry is never cleared, because its write may
        still be in flight in another window. Returns how many were removed."""
        wanted = set(entry_ids)
        with self._locked():
            entries = self._read_locked()
            kept = [entry for entry in entries if not (entry.id in wanted and entry.outcome == UNKNOWN)]
            removed = len(entries) - len(kept)
            if removed:
                self._write_locked(kept)
        return removed


__all__ = [
    "HELD_OUTCOMES",
    "JOURNAL_NAME",
    "LANDED",
    "REFUSED",
    "UNKNOWN",
    "WRITING",
    "JournalEntry",
    "JournalError",
    "SaveJournal",
    "default_path",
    "journal_unreadable_text",
]
