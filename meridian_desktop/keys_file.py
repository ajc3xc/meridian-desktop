"""Keys kept in a file the owner chooses, read as a list of names and as one value at a time. Pure Python: no Qt import.

The file is either a JSON object of name to value text, for example {"OpenAI": "..."}, or NAME=VALUE lines with one key
on each line, for example OpenAI=... or OpenAI: ... . The first non-blank character picks the format: a { means JSON,
and anything else is read as NAME=VALUE lines. Each such line is split at the first = or : on it, into a name and a
value. A line that starts with # is a comment. The owner picks the file. Only its path is kept in settings
(``keys_file_path``). The values stay in the file: they are never copied to the keychain or to settings, never sent
anywhere except one Check request, and the app never writes the file.

Listing reads the file to check every entry, because one bad entry refuses the whole file. Listing keeps the names only.
A Check reads the one value it asks for, for that one call. A file that is missing, that is larger than 64 KB, that is
not UTF-8 text, or that has an entry it refuses, is refused whole, with a plain reason and a count. Each refused entry
is also listed with its place in the file (a line number for NAME=VALUE, an entry number for JSON) and its plain reason.
A name is shown beside its place only when it looks like a name (see ``_shown_name``); a name that looks like a key value
is hidden and the line says so. A value is never shown. ``log_listing`` writes the same lines to the control log, one
line for each refused entry, so the owner can read them in the Logs page.
"""

from __future__ import annotations

import codecs
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from meridian_desktop.api_keys import BUILT_IN_NAMES, KeyNameError, clean_name, slug
from meridian_desktop.runner_controls import append_control_line, default_log_path
from meridian_desktop.secret_store import MAX_SECRET_LENGTH

MAX_FILE_BYTES = 64 * 1024

MISSING_TEXT = "The keys file is missing. Choose the file again."
UNREADABLE_TEXT = "The keys file could not be read. Choose the file again."
TOO_LARGE_TEXT = "Not loaded. The keys file is larger than 64 KB, so 0 entries were read."
NOT_TEXT_TEXT = "Not loaded. The keys file is not UTF-8 text, so 0 entries were read."
NOT_JSON_TEXT = "Not loaded. The keys file is not valid JSON, so 0 entries were read."
NOT_OBJECT_TEXT = "Not loaded. The keys file must hold one JSON object of name to text, so 0 entries were read."
# A refusal: the count line, then one line for each refused entry, up to LINES_LISTED of them, then the rest as a count,
# then the line that says nothing was loaded. The control log gets every refused entry, not only the first few.
REFUSED_COUNT_TEXT = "Not loaded. {refused} of {total} entries were refused."
REFUSED_LINE_TEXT = "{where}: {reason}"
REFUSED_NAMED_LINE_TEXT = '{where} "{name}": {reason}'
REFUSED_HIDDEN_LINE_TEXT = "{where}: {reason} (the name is not shown: it is not a plain name, or it looks like a key value)"
REFUSED_MORE_TEXT = "and {more} more"
NOTHING_LOADED_TEXT = "Nothing was loaded."
LINES_LISTED = 10
# The fixed reasons an entry is refused for. None of them holds a name or a value from the file.
REASON_NO_SEPARATOR = "no = or : between the name and the value"
REASON_NAME_SPACE = "name has a space"
REASON_NAME_NOT_ALLOWED = "name is not allowed"
REASON_NAME_BUILT_IN = "name is a built-in name, which has its own entry"
REASON_NAME_REPEATS = "name repeats the one on {place}"
REASON_VALUE_EMPTY = "value is empty"
REASON_VALUE_NOT_TEXT = "value is not text"
REASON_VALUE_LONG = "value is longer than {limit} characters"

# The line ends a NAME=VALUE file may use: Windows, classic Mac and Unix. Each one ends a line.
_LINE_END = re.compile(r"\r\n|\r|\n")
# Any whitespace inside a name. clean_name would fold it into one space, so a name that still has one is refused.
_INNER_SPACE = re.compile(r"\s")
_BUILT_IN_SLUGS = frozenset(slug(name) for name in BUILT_IN_NAMES)
# A name is shown in a refusal only when it looks like a name: at most 32 characters of letters, digits, spaces, dots,
# dashes and underscores, with no run of 16 letters and digits. A key value pasted where a name goes is longer than that,
# or has such a run, so it stays hidden. A short key with no run of 16 can still show; the name is then the owner's own.
_SHOWN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,31}")
_KEY_LIKE_RUN = re.compile(r"[A-Za-z0-9]{16,}")


class KeysFileError(Exception):
    """The keys file cannot be used. The message is plain words. ``refused`` lists each refused entry, when there are any."""

    def __init__(self, message: str, refused: tuple[Refusal, ...] = (), total: int = 0) -> None:
        super().__init__(message)
        self.refused = refused
        self.total = total


@dataclass(frozen=True)
class Refusal:
    """One refused entry: its place in the file, its name when the name may be shown, and its plain reason.

    ``hidden`` is True when the entry has a name that is not shown, so the line can say so. ``name`` is None when the name
    is hidden or the entry has none.
    """

    where: str
    reason: str
    name: str | None = None
    hidden: bool = False


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


def _file_bytes(path: Path) -> bytes:
    """The bytes of the file. Raise KeysFileError, with plain words, when it is missing, unreadable or too large."""
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
    return raw


def _starts_with_brace(raw: bytes) -> bool:
    """True when the first non-blank byte is a brace, after a byte order mark and any ASCII spaces."""
    body = raw[len(codecs.BOM_UTF8) :] if raw.startswith(codecs.BOM_UTF8) else raw
    return body.lstrip()[:1] == b"{"


def _as_text(raw: bytes) -> str:
    """The file as text, read as UTF-8 with any byte order mark dropped. Raise KeysFileError when it is not UTF-8."""
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        # The text cannot be read, so the first byte says which format the owner meant. A brace keeps the JSON reason.
        raise KeysFileError(NOT_JSON_TEXT if _starts_with_brace(raw) else NOT_TEXT_TEXT) from None


def _json_pairs(text: str) -> list[tuple[str, object]]:
    """The top-level pairs of a JSON object, in file order. Raise KeysFileError when the text is not one JSON object."""
    try:
        data = json.loads(text, object_pairs_hook=_Object)
    except (ValueError, RecursionError):
        raise KeysFileError(NOT_JSON_TEXT) from None
    if not isinstance(data, _Object):
        raise KeysFileError(NOT_OBJECT_TEXT)
    return data.pairs


def _unquoted(value: str) -> str:
    """The value without one matching pair of single or double quotes around it. Nothing else is taken out."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _split_at_separator(line: str) -> tuple[str, str] | None:
    """The text before and after the first = or : on a line, each stripped of spaces. None when the line has neither."""
    cuts = [index for index in (line.find("="), line.find(":")) if index >= 0]
    if not cuts:
        return None
    cut = min(cuts)
    return line[:cut].strip(), line[cut + 1 :].strip()


def _shown_name(raw: str) -> str | None:
    """The name as it may be shown in a refusal, or None when it looks like a key value and must stay hidden."""
    if _SHOWN_NAME.fullmatch(raw) is None or _KEY_LIKE_RUN.search(raw):
        return None
    return raw


def _refusal(where: str, raw_name: str | None, reason: str) -> Refusal:
    """The refusal of one entry. ``raw_name`` is the name as the file gives it, or None when the entry has no name."""
    if not raw_name:
        return Refusal(where=where, reason=reason)
    shown = _shown_name(raw_name)
    return Refusal(where=where, reason=reason, name=shown, hidden=shown is None)


def _refusal_text(refusal: Refusal) -> str:
    """The one line that says where an entry is, its name when it may be shown, and why it is refused."""
    if refusal.name is not None:
        return REFUSED_NAMED_LINE_TEXT.format(where=refusal.where, name=refusal.name, reason=refusal.reason)
    if refusal.hidden:
        return REFUSED_HIDDEN_LINE_TEXT.format(where=refusal.where, reason=refusal.reason)
    return REFUSED_LINE_TEXT.format(where=refusal.where, reason=refusal.reason)


def _env_line(line: str, number: int, first_line: dict[str, int]) -> tuple[str, str] | Refusal:
    """The (name, value) of one NAME=VALUE line, or the refusal of the line.

    ``line`` is stripped of its spaces and is neither blank nor a comment. ``first_line`` maps the slug of each valid
    name to the number of the first line that gave it. A name that repeats one is refused even when that earlier line's
    value was refused too, so every repeat is seen in one pass.
    """
    where = f"line {number}"
    parts = _split_at_separator(line)
    if parts is None:
        return _refusal(where, None, REASON_NO_SEPARATOR)
    raw_name, raw_value = parts
    if _INNER_SPACE.search(raw_name):
        return _refusal(where, raw_name, REASON_NAME_SPACE)
    try:
        name = clean_name(raw_name)
    except KeyNameError:
        return _refusal(where, raw_name, REASON_NAME_NOT_ALLOWED)
    key = slug(name)
    if key in _BUILT_IN_SLUGS:
        return _refusal(where, name, REASON_NAME_BUILT_IN)
    if key in first_line:
        return _refusal(where, name, REASON_NAME_REPEATS.format(place=f"line {first_line[key]}"))
    first_line[key] = number
    value = _unquoted(raw_value).strip()
    if not value:
        return _refusal(where, name, REASON_VALUE_EMPTY)
    if len(value) > MAX_SECRET_LENGTH:
        return _refusal(where, name, REASON_VALUE_LONG.format(limit=MAX_SECRET_LENGTH))
    return name, value


def _refused_text(refused: tuple[Refusal, ...], total: int) -> str:
    """The refusal of a file: its count, its refused entries, any rest as a count, then the line that says nothing loaded."""
    lines = [REFUSED_COUNT_TEXT.format(refused=len(refused), total=total)]
    lines += [_refusal_text(item) for item in refused[:LINES_LISTED]]
    if len(refused) > LINES_LISTED:
        lines.append(REFUSED_MORE_TEXT.format(more=len(refused) - LINES_LISTED))
    lines.append(NOTHING_LOADED_TEXT)
    return "\n".join(lines)


def _env_entries(text: str) -> list[tuple[str, str]]:
    """Every entry of a NAME=VALUE file, in file order. Raise KeysFileError that lists each refused line by its number.

    Blank lines and lines that start with # are skipped and not counted. Every other line is an entry, and the file is
    refused whole when any entry is refused. Line numbers count every physical line, skipped ones included.
    """
    first_line: dict[str, int] = {}
    entries: list[tuple[str, str]] = []
    refused: list[Refusal] = []
    total = 0
    for number, raw in enumerate(_LINE_END.split(text), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        total += 1
        checked = _env_line(line, number, first_line)
        if isinstance(checked, Refusal):
            refused.append(checked)
        else:
            entries.append(checked)
    if refused:
        raise KeysFileError(_refused_text(tuple(refused), total), refused=tuple(refused), total=total)
    return entries


def _name_of(key: str) -> str | None:
    """The key name as it is listed, or None when the name is not a valid key name."""
    try:
        return clean_name(key)
    except KeyNameError:
        return None


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
            refused.append(_refusal(where, key, REASON_NAME_NOT_ALLOWED))
            continue
        found = slug(name)
        if found in _BUILT_IN_SLUGS:
            refused.append(_refusal(where, name, REASON_NAME_BUILT_IN))
            continue
        if found in first_entry:
            refused.append(_refusal(where, name, REASON_NAME_REPEATS.format(place=f"entry {first_entry[found]}")))
            continue
        first_entry[found] = position
        reason = _json_value_reason(value)
        if reason is not None:
            refused.append(_refusal(where, name, reason))
            continue
        entries.append((name, value.strip()))
    if refused:
        raise KeysFileError(_refused_text(tuple(refused), len(pairs)), refused=tuple(refused), total=len(pairs))
    return entries


def _entries(path: Path) -> list[tuple[str, str]]:
    """Every entry of the file as (name, value), in file order. Raise KeysFileError when the file or an entry is refused.

    The first non-blank character picks the format: a { is JSON, and anything else is read as NAME=VALUE lines.
    """
    text = _as_text(_file_bytes(path))
    if text.lstrip().startswith("{"):
        return _json_entries(_json_pairs(text))
    return _env_entries(text)


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
    a file that lists its names. A line never holds a value, and holds a name only when the name may be shown."""
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
    "listing_log_lines",
    "log_listing",
    "read_names",
    "read_value",
]
