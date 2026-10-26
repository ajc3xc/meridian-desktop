"""Keys kept in a file the owner chooses, read as a list of names and as one value at a time. Pure Python: no Qt import.

The file is either a JSON object of name to value text, for example {"OpenAI": "..."}, or NAME=VALUE lines with one key
on each line, for example OpenAI=... or OpenAI: ... . The first non-blank character picks the format: a { means JSON,
and anything else is read as NAME=VALUE lines. Each such line is split at the first = or : on it, into a name and a
value. A line that starts with # is a comment. The owner picks the file. Only its path is kept in settings
(``keys_file_path``). The values stay in the file: they are never copied to the keychain or to settings, never sent
anywhere except one Check request, and the app never writes the file.

Listing reads the file to check every entry, because one bad entry refuses the whole file. Listing keeps the names only.
A Check reads the one value it asks for, for that one call. A file that is missing, that is larger than 64 KB, that is
not UTF-8 text, or that has an entry it refuses, is refused whole, with a plain reason and a count. A NAME=VALUE file
also lists each refused line by its number and a fixed reason. No reason repeats a name or a value, because a mistaken
name can be a secret.
"""

from __future__ import annotations

import codecs
import json
import re
from dataclasses import dataclass
from pathlib import Path

from meridian_desktop.api_keys import BUILT_IN_NAMES, KeyNameError, clean_name, slug
from meridian_desktop.secret_store import MAX_SECRET_LENGTH

MAX_FILE_BYTES = 64 * 1024

MISSING_TEXT = "The keys file is missing. Choose the file again."
UNREADABLE_TEXT = "The keys file could not be read. Choose the file again."
TOO_LARGE_TEXT = "Not loaded. The keys file is larger than 64 KB, so 0 entries were read."
NOT_TEXT_TEXT = "Not loaded. The keys file is not UTF-8 text, so 0 entries were read."
NOT_JSON_TEXT = "Not loaded. The keys file is not valid JSON, so 0 entries were read."
NOT_OBJECT_TEXT = "Not loaded. The keys file must hold one JSON object of name to text, so 0 entries were read."
REFUSED_TEXT = (
    "Not loaded. {refused} of {total} entries were refused. Each entry must be a key name with a text value of up to "
    "{limit} characters, and no name may repeat or be {built_in}, which has its own entry. Nothing was loaded."
)
# A NAME=VALUE refusal: the count line, then one line for each refused line, up to LINES_LISTED of them, then the rest
# as a count, then the line that says nothing was loaded.
REFUSED_LINES_TEXT = "Not loaded. {refused} of {total} entries were refused."
REFUSED_LINE_TEXT = "line {number}: {reason}"
REFUSED_MORE_TEXT = "and {more} more"
NOTHING_LOADED_TEXT = "Nothing was loaded."
LINES_LISTED = 10
# The fixed reasons a NAME=VALUE line is refused for. None of them holds a name or a value from the file.
REASON_NO_SEPARATOR = "no = or : between the name and the value"
REASON_NAME_SPACE = "name has a space"
REASON_NAME_NOT_ALLOWED = "name is not allowed"
REASON_NAME_BUILT_IN = "name is a built-in name"
REASON_NAME_REPEATS = "name repeats the one on line {number}"
REASON_VALUE_EMPTY = "value is empty"
REASON_VALUE_LONG = "value is longer than {limit} characters"

# The line ends a NAME=VALUE file may use: Windows, classic Mac and Unix. Each one ends a line.
_LINE_END = re.compile(r"\r\n|\r|\n")
# Any whitespace inside a name. clean_name would fold it into one space, so a name that still has one is refused.
_INNER_SPACE = re.compile(r"\s")
_BUILT_IN_SLUGS = frozenset(slug(name) for name in BUILT_IN_NAMES)


class KeysFileError(Exception):
    """The keys file cannot be used. The message is plain words and never holds a name or a value."""


@dataclass(frozen=True)
class FileNames:
    """What the Keys block lists from the file: its names, or the plain reason it is refused or missing."""

    names: tuple[str, ...] = ()
    problem: str | None = None


class _Object:
    """The pairs of one JSON object, in file order. A repeated name stays in the pairs, so it is seen and refused."""

    __slots__ = ("pairs",)

    def __init__(self, pairs: list[tuple[str, object]]) -> None:
        self.pairs = pairs


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


def _env_line(line: str, number: int, first_line: dict[str, int]) -> tuple[str, str] | str:
    """The (name, value) of one NAME=VALUE line, or the fixed reason the line is refused.

    ``line`` is stripped of its spaces and is neither blank nor a comment. ``first_line`` maps the slug of each valid
    name to the number of the first line that gave it. A name that repeats one is refused even when that earlier line's
    value was refused too, so every repeat is seen in one pass.
    """
    parts = _split_at_separator(line)
    if parts is None:
        return REASON_NO_SEPARATOR
    raw_name, raw_value = parts
    if _INNER_SPACE.search(raw_name):
        return REASON_NAME_SPACE
    try:
        name = clean_name(raw_name)
    except KeyNameError:
        return REASON_NAME_NOT_ALLOWED
    key = slug(name)
    if key in _BUILT_IN_SLUGS:
        return REASON_NAME_BUILT_IN
    if key in first_line:
        return REASON_NAME_REPEATS.format(number=first_line[key])
    first_line[key] = number
    value = _unquoted(raw_value).strip()
    if not value:
        return REASON_VALUE_EMPTY
    if len(value) > MAX_SECRET_LENGTH:
        return REASON_VALUE_LONG.format(limit=MAX_SECRET_LENGTH)
    return name, value


def _refused_lines_text(refused: list[tuple[int, str]], total: int) -> str:
    """The refusal of a NAME=VALUE file: its count, its refused lines, any rest as a count, then the end."""
    lines = [REFUSED_LINES_TEXT.format(refused=len(refused), total=total)]
    lines += [REFUSED_LINE_TEXT.format(number=number, reason=reason) for number, reason in refused[:LINES_LISTED]]
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
    refused: list[tuple[int, str]] = []
    total = 0
    for number, raw in enumerate(_LINE_END.split(text), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        total += 1
        checked = _env_line(line, number, first_line)
        if isinstance(checked, str):
            refused.append((number, checked))
        else:
            entries.append(checked)
    if refused:
        raise KeysFileError(_refused_lines_text(refused, total))
    return entries


def _text_of(value: object) -> str | None:
    """The value stripped of its spaces, or None when it is not text, is blank, or is longer than a key may be."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > MAX_SECRET_LENGTH:
        return None
    return text


def _name_of(key: str) -> str | None:
    """The key name as it is listed, or None when the name is not a valid key name."""
    try:
        return clean_name(key)
    except KeyNameError:
        return None


def _json_entries(pairs: list[tuple[str, object]]) -> list[tuple[str, str]]:
    """Every entry of a JSON object, in file order. Raise KeysFileError when an entry is refused.

    An entry is refused when its name is not a key name, its value is not usable text, its name repeats an earlier name
    by its slug, or its name is a built-in name such as RunPod, which keeps its own entry.
    """
    taken = {slug(name) for name in BUILT_IN_NAMES}
    entries: list[tuple[str, str]] = []
    refused = 0
    for key, value in pairs:
        name = _name_of(key)
        text = _text_of(value)
        if name is None or text is None or slug(name) in taken:
            refused += 1
            continue
        taken.add(slug(name))
        entries.append((name, text))
    if refused:
        raise KeysFileError(
            REFUSED_TEXT.format(
                refused=refused,
                total=len(pairs),
                limit=MAX_SECRET_LENGTH,
                built_in=", ".join(BUILT_IN_NAMES),
            )
        )
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
        return FileNames(problem=str(exc))
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


__all__ = [
    "MAX_FILE_BYTES",
    "FileNames",
    "KeysFileError",
    "read_names",
    "read_value",
]
