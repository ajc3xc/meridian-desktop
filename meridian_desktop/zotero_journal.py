"""The Zotero save journal: the outcome of each item write, kept on disk so it survives quitting. Stdlib only, no Qt.

Each entry holds one line of the reading list and an outcome. An item's entry is written as "writing" before its write
starts, and changed to landed, unknown or refused once the outcome is known. An entry that is still writing when the app
quits is held as unknown on the next launch, because the write may have landed. Held entries stop Save until the person
checks Zotero and clears them. Only the line text, an id, an outcome and a time are stored. Never a token, a grant key or
a response body. Every change is written to a temporary file that then replaces the journal, so a crash leaves the
previous journal whole.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import threading
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from meridian_desktop.settings_store import default_settings_path

JOURNAL_NAME = "zotero_saves.json"
JOURNAL_VERSION = 1
MAX_ENTRIES = 500
WRITING = "writing"
LANDED = "landed"
UNKNOWN = "unknown"
REFUSED = "refused"
OUTCOMES = (WRITING, LANDED, UNKNOWN, REFUSED)
HELD_OUTCOMES = (WRITING, UNKNOWN)
WRITE_FAILED_TEXT = "The Zotero save journal could not be written, so nothing more was saved. Check Zotero before you save again."
_FIELDS = ("id", "line", "outcome", "at")


def journal_unreadable_text(path: Path) -> str:
    return f"The Zotero save journal could not be read, so saving is off. Fix or move {path}, then restart the desktop."


class JournalError(Exception):
    """The journal could not be read or written. ``message`` is fixed text for a person to read."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


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


def _pruned(entries: Sequence[JournalEntry]) -> list[JournalEntry]:
    """Keep every held entry, and the most recent of the rest, up to MAX_ENTRIES in all."""
    if len(entries) <= MAX_ENTRIES:
        return list(entries)
    held = [entry for entry in entries if entry.outcome in HELD_OUTCOMES]
    others = [entry for entry in entries if entry.outcome not in HELD_OUTCOMES]
    room = max(MAX_ENTRIES - len(held), 0)
    kept_others = set(id(entry) for entry in others[len(others) - room :]) if room else set()
    return [entry for entry in entries if entry.outcome in HELD_OUTCOMES or id(entry) in kept_others]


class SaveJournal:
    """The journal in memory, and on disk at ``path``. Reading it at construction raises JournalError if it is unreadable."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._entries = self._read()

    @property
    def path(self) -> Path:
        return self._path

    def _read(self) -> list[JournalEntry]:
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return []
        except OSError:
            raise JournalError(journal_unreadable_text(self._path)) from None
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise JournalError(journal_unreadable_text(self._path)) from None
        entries = value.get("entries") if isinstance(value, dict) else None
        if not isinstance(entries, list):
            raise JournalError(journal_unreadable_text(self._path))
        parsed: list[JournalEntry] = []
        for item in entries:
            if not isinstance(item, dict) or not all(isinstance(item.get(name), str) for name in _FIELDS):
                raise JournalError(journal_unreadable_text(self._path))
            if item["outcome"] not in OUTCOMES:
                raise JournalError(journal_unreadable_text(self._path))
            parsed.append(JournalEntry(**{name: item[name] for name in _FIELDS}))
        return parsed

    def _persist(self) -> None:
        """Write the whole journal to a temporary file, then replace the journal with it. Raises JournalError."""
        self._entries = _pruned(self._entries)
        payload = {"version": JOURNAL_VERSION, "entries": [{name: getattr(e, name) for name in _FIELDS} for e in self._entries]}
        data = json.dumps(payload, indent=2).encode("utf-8")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp = tempfile.mkstemp(dir=self._path.parent, prefix=".zotero_saves-", suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, self._path)
            except BaseException:
                try:
                    os.unlink(temp)
                except OSError:
                    pass
                raise
        except OSError:
            raise JournalError(WRITE_FAILED_TEXT) from None

    def _mutate(self, change: Callable[[], None]) -> None:
        """Apply a change and persist it. If the write fails, the change is undone in memory too."""
        previous = list(self._entries)
        try:
            change()
            self._persist()
        except BaseException:
            self._entries = previous
            raise

    def held(self) -> list[JournalEntry]:
        """The entries that hold a line: writing (the write may have started) or unknown."""
        with self._lock:
            return [entry for entry in self._entries if entry.outcome in HELD_OUTCOMES]

    def begin(self, line: str) -> str:
        """Record that the write of ``line`` is about to start. Returns the entry's id."""
        entry = JournalEntry(id=uuid.uuid4().hex, line=line, outcome=WRITING, at=_now())
        with self._lock:
            self._mutate(lambda: self._entries.append(entry))
        return entry.id

    def finish(self, entry_id: str, outcome: str) -> None:
        """Record the outcome of a write that was begun. ``outcome`` is landed, unknown or refused."""
        if outcome not in (LANDED, UNKNOWN, REFUSED):
            raise ValueError(f"not a finished outcome: {outcome}")
        with self._lock:

            def change() -> None:
                for index, entry in enumerate(self._entries):
                    if entry.id == entry_id:
                        self._entries[index] = replace(entry, outcome=outcome, at=_now())
                        return
                raise JournalError(WRITE_FAILED_TEXT)

            self._mutate(change)

    def record_unknown(self, lines: Sequence[str]) -> None:
        """Hold lines whose outcome is not known at all, such as when a save produced no result."""
        stamp = _now()
        added = [JournalEntry(id=uuid.uuid4().hex, line=line, outcome=UNKNOWN, at=stamp) for line in lines]
        with self._lock:
            self._mutate(lambda: self._entries.extend(added))

    def clear(self, entry_ids: Sequence[str]) -> None:
        """Remove held entries after the person has checked Zotero. Nothing else is removed."""
        wanted = set(entry_ids)

        def change() -> None:
            self._entries = [entry for entry in self._entries if entry.id not in wanted]

        with self._lock:
            self._mutate(change)


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
