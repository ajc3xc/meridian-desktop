"""Keys kept in a file the owner chooses, read as a list of names and as one value at a time. Pure Python: no Qt import.

The file is either a JSON object of name to value text, for example {"OpenAI": "..."}, or NAME=VALUE lines with one key
on each line, for example OpenAI=... . The first non-blank character picks the format: a { means JSON, and anything else
is read as NAME=VALUE. The owner picks the file. Only its path is kept in settings (``keys_file_path``). The values stay
in the file: they are never copied to the keychain or to settings, never sent anywhere except one Check request, and the
app never writes the file.

Listing reads the file to check every entry, because one bad entry refuses the whole file. Listing keeps the names only.
A Check reads the one value it asks for, for that one call. A file that is missing, that is larger than 64 KB, that is
not UTF-8 text, or that has an entry it refuses, is refused whole, with a plain reason and a count. No reason repeats a
name or a value, because a mistaken name can be a secret.
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
REFUSED_LINES_TEXT = (
    "Not loaded. {refused} of {total} entries were refused. Each entry must be a NAME=VALUE line with a key name and a "
    "text value of up to {limit} characters, and no name may repeat or be {built_in}, which has its own entry. Nothing "
    "was loaded."
)

# The line ends a NAME=VALUE file may use: Windows, classic Mac and Unix. Each one ends a line.
_LINE_END = re.compile(r"\r\n|\r|\n")


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

    def __init__(self, pairs: list[tuple[str | None, object]]) -> None:
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


def _json_pairs(text: str) -> list[tuple[str | None, object]]:
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


def _env_pairs(text: str) -> list[tuple[str | None, object]]:
    """The NAME=VALUE lines in file order. Blank lines and lines that start with # are skipped.

    A line with no = is kept with no name and no value, so it is counted as a refused entry and is never skipped.
    """
    pairs: list[tuple[str | None, object]] = []
    for line in _LINE_END.split(text):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, separator, value = stripped.partition("=")
        if not separator:
            pairs.append((None, None))
            continue
        pairs.append((name.strip(), _unquoted(value.strip())))
    return pairs


def _pairs_of(path: Path) -> tuple[bool, list[tuple[str | None, object]]]:
    """The entries of the file in file order, and whether the file is JSON. Raise KeysFileError when it cannot be read.

    The first non-blank character picks the format: a { is JSON, and anything else is read as NAME=VALUE lines.
    """
    text = _as_text(_file_bytes(path))
    if text.lstrip().startswith("{"):
        return True, _json_pairs(text)
    return False, _env_pairs(text)


def _name_of(key: str) -> str | None:
    """The key name as it is listed, or None when the name is not a valid key name."""
    try:
        return clean_name(key)
    except KeyNameError:
        return None


def _text_of(value: object) -> str | None:
    """The value stripped of its spaces, or None when it is not text, is blank, or is longer than a key may be."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > MAX_SECRET_LENGTH:
        return None
    return text


def _entries(path: Path) -> list[tuple[str, str]]:
    """Every entry of the file as (name, value), in file order. Raise KeysFileError when the file or an entry is refused.

    An entry is refused when its name is not a key name, its value is not usable text, its name repeats an earlier name
    by its slug, or its name is a built-in name such as RunPod, which keeps its own entry. In a NAME=VALUE file a line
    with no = is refused too, as an entry with no name and no value.
    """
    is_json, pairs = _pairs_of(path)
    taken = {slug(name) for name in BUILT_IN_NAMES}
    entries: list[tuple[str, str]] = []
    refused = 0
    for key, value in pairs:
        name = None if key is None else _name_of(key)
        text = _text_of(value)
        if name is None or text is None or slug(name) in taken:
            refused += 1
            continue
        taken.add(slug(name))
        entries.append((name, text))
    if refused:
        template = REFUSED_TEXT if is_json else REFUSED_LINES_TEXT
        raise KeysFileError(
            template.format(
                refused=refused,
                total=len(pairs),
                limit=MAX_SECRET_LENGTH,
                built_in=", ".join(BUILT_IN_NAMES),
            )
        )
    return entries


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
