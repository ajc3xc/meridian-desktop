"""Keys kept in a file the owner chooses, read as a list of names and as one value at a time. Pure Python: no Qt import.

The file is a JSON object of name to value text, for example {"OpenAI": "..."}. The owner picks it. Only its path is
kept in settings (``keys_file_path``). The values stay in the file: they are never copied to the keychain or to
settings, never sent anywhere except one Check request, and the app never writes the file.

Listing reads the file to check every entry, because one bad entry refuses the whole file. Listing keeps the names only.
A Check reads the one value it asks for, for that one call. A file that is missing, that is larger than 64 KB, that is
not UTF-8 text, that is not a JSON object of name to text, or that has an entry it refuses, is refused whole, with a plain
reason and a count. Each refused entry is also listed with its place in the file (its entry number) and its plain reason.
No name or value from the file is ever shown, so a mistaken name that is really a secret stays hidden. ``log_listing``
writes the same lines to the control log, one line for each refused entry, so the owner can read them in the Logs page.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from meridian_desktop import api_keys
from meridian_desktop.api_keys import BUILT_IN_NAMES, KeyNameError, clean_name, slug
from meridian_desktop.runner_controls import append_control_line, default_log_path
from meridian_desktop.secret_store import MAX_SECRET_LENGTH

MAX_FILE_BYTES = 64 * 1024

MISSING_TEXT = "The keys file is missing. Choose the file again."
UNREADABLE_TEXT = "The keys file could not be read. Choose the file again."
TOO_LARGE_TEXT = "Not loaded. The keys file is larger than 64 KB, so 0 entries were read."
NOT_JSON_TEXT = "Not loaded. The keys file is not valid JSON, so 0 entries were read."
NOT_OBJECT_TEXT = "Not loaded. The keys file must hold one JSON object of name to text, so 0 entries were read."
# A refusal: the count line, then one line for each refused entry, up to LINES_LISTED of them, then the rest as a count,
# then the line that says nothing was loaded. The control log gets every refused entry, not only the first few.
REFUSED_COUNT_TEXT = "Not loaded. {refused} of {total} entries were refused."
REFUSED_LINE_TEXT = "{where}: {reason}"
REFUSED_MORE_TEXT = "and {more} more"
NOTHING_LOADED_TEXT = "Nothing was loaded."
LINES_LISTED = 10
# The fixed reasons an entry is refused for. None of them holds a name or a value from the file.
REASON_NAME_NOT_ALLOWED = "name is not allowed"
REASON_NAME_BUILT_IN = "name is a built-in name, which has its own entry"
REASON_NAME_REPEATS = "name repeats the one on {place}"
REASON_VALUE_EMPTY = "value is empty"
REASON_VALUE_NOT_TEXT = "value is not text"
REASON_VALUE_LONG = "value is longer than {limit} characters"
_BUILT_IN_SLUGS = frozenset(slug(name) for name in BUILT_IN_NAMES)


class KeysFileError(Exception):
    """The keys file cannot be used. The message is plain words. ``refused`` lists each refused entry, when there are any."""

    def __init__(self, message: str, refused: tuple[Refusal, ...] = (), total: int = 0) -> None:
        super().__init__(message)
        self.refused = refused
        self.total = total


@dataclass(frozen=True)
class Refusal:
    """One refused entry: its place in the file and its plain reason. Its name and value are never kept or shown."""

    where: str
    reason: str


@dataclass(frozen=True)
class FileNames:
    """What the Keys block lists from the file: its names, or the plain reason it is refused or missing.

    ``refused`` and ``total`` are set only when entries were refused: the refused entries, and how many entries the file
    holds in all.
    """

    names: tuple[str, ...] = ()
    problem: str | None = None
    refused: tuple[Refusal, ...] = ()
    total: int = 0


class _Object:
    """The pairs of one JSON object, in file order. A repeated name stays in the pairs, so it is seen and refused."""

    __slots__ = ("pairs",)

    def __init__(self, pairs: list[tuple[str, object]]) -> None:
        self.pairs = pairs


def _local_now() -> datetime:
    return datetime.now().astimezone()


def is_classified(name: str) -> bool:
    """True when ``name`` is a known service (``key_services.SERVICES``) or a built-in name (``BUILT_IN_NAMES``).

    Every other name is unclassified. Both lists are read on each call, so a name either gains is classified at once.
    key_services is imported here, not at the top of the module: key_services imports read_value from this module, so a
    top-level import would be circular.
    """
    from meridian_desktop import key_services

    known = {slug(service.name) for service in key_services.SERVICES}
    known |= {slug(built_in) for built_in in api_keys.BUILT_IN_NAMES}
    return slug(name) in known


def split_by_classification(names: Iterable[str]) -> tuple[list[str], list[str]]:
    """The names split into (classified, unclassified). Each group keeps the order the names came in."""
    listed = list(names)
    return [name for name in listed if is_classified(name)], [name for name in listed if not is_classified(name)]


def _read_pairs(path: Path) -> list[tuple[str, object]]:
    """The top-level pairs of the file. Raise KeysFileError, with plain words, when it is missing, unreadable or not an object."""
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        raise KeysFileError(MISSING_TEXT) from None
    except (OSError, ValueError):
        raise KeysFileError(UNREADABLE_TEXT) from None
    if size > MAX_FILE_BYTES:
        raise KeysFileError(TOO_LARGE_TEXT)
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
    except (OSError, ValueError):
        raise KeysFileError(UNREADABLE_TEXT) from None
    if len(raw) > MAX_FILE_BYTES:
        raise KeysFileError(TOO_LARGE_TEXT)
    try:
        data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_Object)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise KeysFileError(NOT_JSON_TEXT) from None
    if not isinstance(data, _Object):
        raise KeysFileError(NOT_OBJECT_TEXT)
    return data.pairs


def _name_of(key: str) -> str | None:
    """The key name as it is listed, or None when the name is not a valid key name."""
    try:
        return clean_name(key)
    except KeyNameError:
        return None


def _refusal_text(refusal: Refusal) -> str:
    """The one line that says where an entry is and why it is refused. It never holds the entry's name or value."""
    return REFUSED_LINE_TEXT.format(where=refusal.where, reason=refusal.reason)


def _refused_text(refused: tuple[Refusal, ...], total: int) -> str:
    """The refusal of a file: its count, its refused entries, any rest as a count, then the line that says nothing loaded."""
    lines = [REFUSED_COUNT_TEXT.format(refused=len(refused), total=total)]
    lines += [_refusal_text(item) for item in refused[:LINES_LISTED]]
    if len(refused) > LINES_LISTED:
        lines.append(REFUSED_MORE_TEXT.format(more=len(refused) - LINES_LISTED))
    lines.append(NOTHING_LOADED_TEXT)
    return "\n".join(lines)


def _json_value_reason(value: object) -> str | None:
    """Why a JSON value is refused, or None when it is usable text."""
    if not isinstance(value, str):
        return REASON_VALUE_NOT_TEXT
    text = value.strip()
    if not text:
        return REASON_VALUE_EMPTY
    if len(text) > MAX_SECRET_LENGTH:
        return REASON_VALUE_LONG.format(limit=MAX_SECRET_LENGTH)
    return None


def _json_entries(pairs: list[tuple[str, object]]) -> list[tuple[str, str]]:
    """Every entry of a JSON object, in file order. Raise KeysFileError that lists each refused entry by its number.

    An entry is refused when its name is not a key name, its name is a built-in name such as RunPod, which keeps its own
    entry, its name repeats an earlier name by its slug, or its value is not usable text. Entries are numbered from 1 in
    file order, because a JSON object has no line numbers here.
    """
    first_entry: dict[str, int] = {}
    entries: list[tuple[str, str]] = []
    refused: list[Refusal] = []
    for position, (key, value) in enumerate(pairs, start=1):
        where = f"entry {position}"
        name = _name_of(key)
        if name is None:
            refused.append(Refusal(where=where, reason=REASON_NAME_NOT_ALLOWED))
            continue
        found = slug(name)
        if found in _BUILT_IN_SLUGS:
            refused.append(Refusal(where=where, reason=REASON_NAME_BUILT_IN))
            continue
        if found in first_entry:
            refused.append(Refusal(where=where, reason=REASON_NAME_REPEATS.format(place=f"entry {first_entry[found]}")))
            continue
        first_entry[found] = position
        reason = _json_value_reason(value)
        if reason is not None:
            refused.append(Refusal(where=where, reason=reason))
            continue
        entries.append((name, value.strip()))
    if refused:
        raise KeysFileError(_refused_text(tuple(refused), len(pairs)), refused=tuple(refused), total=len(pairs))
    return entries


def _entries(path: Path) -> list[tuple[str, str]]:
    """Every entry of the file as (name, value), in file order. Raise KeysFileError when the file or an entry is refused."""
    return _json_entries(_read_pairs(path))


def read_names(path: str | Path) -> FileNames:
    """The key names in the keys file, or the plain reason it is refused or missing. No value is returned or kept."""
    try:
        entries = _entries(Path(path))
    except KeysFileError as exc:
        return FileNames(problem=str(exc), refused=exc.refused, total=exc.total)
    return FileNames(names=tuple(name for name, _text in entries))


def read_value(path: str | Path, name: str) -> str | None:
    """The value of ``name`` in the keys file, read for one Check. None when the file holds no such name.

    Raise KeysFileError when the file is missing or refused. The value goes only to the caller, which sends one request
    with it. It is not kept here after the call.
    """
    key = slug(name)
    for listed, text in _entries(Path(path)):
        if slug(listed) == key:
            return text
    return None


def listing_log_lines(listing: FileNames, stamp: str) -> list[str]:
    """The control log lines for one listing: one for each refused entry, or one for a refused or missing file, or one for
    a file that lists its names. A line never holds a name or a value from the file."""
    if listing.refused:
        lines = [f"{stamp} keys file refused: {len(listing.refused)} of {listing.total} entries, nothing loaded"]
        lines += [f"{stamp} keys file {_refusal_text(item)}" for item in listing.refused]
        return lines
    if listing.problem is not None:
        return [f"{stamp} keys file: {listing.problem}"]
    return [f"{stamp} keys file: names listed, {len(listing.names)} in all"]


def log_listing(settings_path: Path, listing: FileNames, *, clock: Callable[[], datetime] = _local_now) -> None:
    """Append the listing's lines to the control log beside the settings file. It never raises: a log that cannot be
    written must not change the Keys block."""
    try:
        stamp = clock().isoformat(timespec="seconds")
        lines = listing_log_lines(listing, stamp)
    except Exception:  # noqa: BLE001 - a logging failure is not the owner's concern and must not change the block
        return
    log = default_log_path(settings_path)
    for line in lines:
        append_control_line(log, line)


__all__ = [
    "MAX_FILE_BYTES",
    "FileNames",
    "KeysFileError",
    "Refusal",
    "is_classified",
    "listing_log_lines",
    "log_listing",
    "read_names",
    "read_value",
    "split_by_classification",
]
