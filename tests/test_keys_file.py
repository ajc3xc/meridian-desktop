"""Tests for the keys file: a JSON or NAME=VALUE file of key names and values, and the Keys block listing it.

The file belongs to the owner, so the app never writes it. Every test that could touch it checks its bytes and its
modification time before and after. FAKE_VALUE is an obviously fake key, so the leak checks can search every output for
it. The keychain is a MemoryKeyring, every file dialog is an injected fake, and no test opens a socket, the real
credential store or a real dialog.
"""

from __future__ import annotations

import dataclasses
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

import pytest
from PySide6.QtWidgets import QLabel, QLineEdit, QPushButton, QWidget

from _fakes import MemoryKeyring
from meridian_desktop.api_keys import KEY_SERVICE, MAX_NAME_LENGTH, STATUS_SET, ApiKeys, account_for
from meridian_desktop.key_services import (
    FILE_UNREADABLE_TEXT,
    NO_CHECK_TEXT,
    NOT_IN_FILE_TEXT,
    REJECTED_TEXT,
    WORKS_TEXT,
    check_saved_key,
    service_for,
)
from meridian_desktop.keys_file import MAX_FILE_BYTES, FileNames, KeysFileError, read_names, read_value
from meridian_desktop.secret_store import MAX_SECRET_LENGTH, KeyringSecretStore
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.ui import keys_section as keys_module
from meridian_desktop.ui.keys_section import (
    FILE_LISTED_MANY_TEXT,
    FILE_PATH_TEXT,
    FILE_STOPPED_TEXT,
    FROM_FILE_NOTE,
    NO_FILE_TEXT,
    REMOVED_FROM_FILE_TEXT,
    STATUS_TEXT,
    KeysSection,
)

FAKE_VALUE = "sk-fake-keys-file-0001"
FAKE_VALUE_TWO = "sk-fake-keys-file-0002"
# 2001-09-09. An old modification time, so a later write to the file would show as a new one.
PAST_SECONDS = 1_000_000_000
OPENAI = service_for("OpenAI")


class FakeTransport:
    """Records each request and its timeout. It answers with a status, or raises the error it was given."""

    def __init__(self, status: int = 200, error: BaseException | None = None) -> None:
        self.status = status
        self.error = error
        self.calls: list[tuple[urllib.request.Request, float]] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> int:
        self.calls.append((request, timeout))
        if self.error is not None:
            raise self.error
        return self.status


def _header(request: urllib.request.Request, name: str) -> str | None:
    """A request header by name, ignoring case, since urllib capitalizes names its own way."""
    headers = {key.lower(): value for key, value in request.header_items()}
    return headers.get(name.lower())


def _all_text(widget: QWidget) -> str:
    """Every label, button and placeholder on the widget. The leak checks search this."""
    parts = [label.text() for label in widget.findChildren(QLabel)]
    parts += [button.text() for button in widget.findChildren(QPushButton)]
    parts += [line.placeholderText() for line in widget.findChildren(QLineEdit)]
    return "\n".join(parts)


def _write(tmp_path: Path, content, name: str = "my-keys.json") -> str:
    """Write the keys file under tmp_path and return its path. A dict is written as JSON; text is written as it is."""
    folder = tmp_path / "keys"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    text = content if isinstance(content, str) else json.dumps(content)
    path.write_text(text, encoding="utf-8")
    return str(path)


def _pin(path: str) -> None:
    """Give the file an old modification time, so a write to it cannot go unseen."""
    os.utime(path, (PAST_SECONDS, PAST_SECONDS))


def _state(path: str) -> tuple[bytes, int]:
    """The bytes and the modification time of a file. A write changes at least one of them."""
    return Path(path).read_bytes(), Path(path).stat().st_mtime_ns


def _empty_keychain() -> KeyringSecretStore:
    return KeyringSecretStore(backend=MemoryKeyring())


def _section(qtbot, settings_path: Path, backend, *, transport=None, choose=None) -> KeysSection:
    """A Keys block over the given keychain. ``choose`` is the file dialog fake. Its first read is awaited."""
    keys = ApiKeys(SettingsStore(settings_path), KeyringSecretStore(backend=backend))
    section = KeysSection(keys, transport=transport, choose_file=choose)
    qtbot.addWidget(section)
    qtbot.waitUntil(lambda: not section.loading, timeout=5000)
    return section


def _chooser(path: str):
    """A file dialog fake: the owner picks ``path``. It is called with the parent, as the real dialog is."""
    return lambda parent: path


def _choose(section: KeysSection) -> None:
    section.findChild(QPushButton, "keys-file-choose").click()


# ----------------------------------------------------------- the file as a listing


def test_a_valid_file_lists_its_names_in_file_order_and_no_value(tmp_path):
    path = _write(tmp_path, {"OpenAI": FAKE_VALUE, "Anthropic": FAKE_VALUE_TWO})

    listing = read_names(path)

    assert listing == FileNames(names=("OpenAI", "Anthropic"), problem=None)
    assert FAKE_VALUE not in repr(listing) and FAKE_VALUE_TWO not in repr(listing)


def test_an_empty_object_lists_no_names_and_is_not_a_refusal(tmp_path):
    assert read_names(_write(tmp_path, {})) == FileNames(names=(), problem=None)


@pytest.mark.parametrize("content", ['{"OpenAI": "sk-fake"', '{"OpenAI": "sk-fake"} trailing', "{not json"])
def test_a_brace_file_that_is_not_valid_json_is_refused_with_a_count_and_no_value(tmp_path, content):
    listing = read_names(_write(tmp_path, content))

    assert listing.names == ()
    assert listing.problem is not None and "0 entries were read" in listing.problem
    assert "sk-fake" not in listing.problem


def test_entries_that_are_not_key_names_or_text_are_refused_with_a_count_and_no_name_or_value(tmp_path):
    path = _write(
        tmp_path,
        {"OpenAI": FAKE_VALUE, "Anthropic": 42, "GitHub": [FAKE_VALUE_TWO], "Bad/Name": FAKE_VALUE_TWO, "Blank": "   "},
    )

    listing = read_names(path)

    assert listing.names == ()
    assert "4 of 5 entries were refused" in listing.problem
    for hidden in (FAKE_VALUE, FAKE_VALUE_TWO, "Anthropic", "GitHub", "Bad/Name", "Blank"):
        assert hidden not in listing.problem, "a refusal names no entry and shows no value"


def test_a_name_repeated_by_case_or_naming_runpod_is_refused(tmp_path):
    text = '{"OpenAI": "%s", "openai": "%s", "RunPod": "%s"}' % (FAKE_VALUE, FAKE_VALUE_TWO, FAKE_VALUE)

    listing = read_names(_write(tmp_path, text))

    assert listing.names == ()
    assert "2 of 3 entries were refused" in listing.problem


def test_a_refused_runpod_entry_says_it_has_its_own_entry_and_shows_no_value(tmp_path):
    listing = read_names(_write(tmp_path, {"RunPod": FAKE_VALUE, "OpenAI": FAKE_VALUE_TWO}))

    assert listing.names == ()
    assert "1 of 2 entries were refused" in listing.problem
    assert "which has its own entry" in listing.problem, "the refusal says RunPod keeps its own entry"
    assert FAKE_VALUE not in listing.problem and FAKE_VALUE_TWO not in listing.problem


def test_a_value_longer_than_the_limit_is_refused(tmp_path):
    listing = read_names(_write(tmp_path, {"OpenAI": "x" * (MAX_SECRET_LENGTH + 1)}))

    assert listing.names == ()
    assert "1 of 1 entries were refused" in listing.problem


def test_a_file_larger_than_the_limit_is_refused_before_it_is_read(tmp_path):
    path = tmp_path / "big.json"
    path.write_text('{"OpenAI": "' + "a" * MAX_FILE_BYTES + '"}', encoding="utf-8")

    listing = read_names(str(path))

    assert listing.names == ()
    assert "larger than" in listing.problem


def test_a_missing_file_says_so_plainly(tmp_path):
    listing = read_names(str(tmp_path / "gone.json"))

    assert listing.names == ()
    assert "is missing" in listing.problem


def test_a_folder_in_place_of_the_file_is_refused_as_unreadable(tmp_path):
    listing = read_names(str(tmp_path))

    assert listing.names == ()
    assert "could not be read" in listing.problem


# ----------------------------------------------------------- the file as a source of one value


def test_a_check_reads_one_value_from_the_file_and_nothing_else(tmp_path):
    path = _write(tmp_path, {"OpenAI": f"  {FAKE_VALUE}  ", "Anthropic": FAKE_VALUE_TWO})

    assert read_value(path, "OpenAI") == FAKE_VALUE, "the value comes back stripped of its spaces"
    assert read_value(path, "anthropic") == FAKE_VALUE_TWO, "a name matches by its slug"
    assert read_value(path, "GitHub") is None, "a name the file does not hold has no value"


def test_a_check_refuses_a_file_that_the_listing_refuses(tmp_path):
    path = _write(tmp_path, {"OpenAI": FAKE_VALUE, "Anthropic": 7})

    with pytest.raises(KeysFileError) as caught:
        read_value(path, "OpenAI")

    assert FAKE_VALUE not in str(caught.value)


def test_listing_and_checking_never_write_the_file(tmp_path):
    path = _write(tmp_path, {"OpenAI": FAKE_VALUE})
    _pin(path)
    before = _state(path)

    read_names(path)
    read_value(path, "OpenAI")
    read_names(str(tmp_path / "gone.json"))

    assert _state(path) == before


# ----------------------------------------------------------- the NAME=VALUE format


# Each refused line with the fixed reason it is refused for.
REFUSED_ENV_LINES = [
    (f"={FAKE_VALUE}", "name is not allowed"),
    (f"Bad/Name={FAKE_VALUE}", "name is not allowed"),
    (f"{'N' * (MAX_NAME_LENGTH + 1)}={FAKE_VALUE}", "name is not allowed"),
    ("Blank=   ", "value is empty"),
    ('Quoted=""', "value is empty"),
    (f"Long={'x' * (MAX_SECRET_LENGTH + 1)}", "value is longer than 512 characters"),
]


def test_a_name_equals_value_file_lists_its_names_in_file_order_and_no_value(tmp_path):
    path = _write(tmp_path, f"OpenAI={FAKE_VALUE}\nAnthropic={FAKE_VALUE_TWO}\n", name="my-keys.env")

    listing = read_names(path)

    assert listing == FileNames(names=("OpenAI", "Anthropic"), problem=None)
    assert FAKE_VALUE not in repr(listing) and FAKE_VALUE_TWO not in repr(listing)


@pytest.mark.parametrize("content", ["", "# nothing yet\n\n   \n"])
def test_an_empty_or_comment_only_name_equals_value_file_lists_no_names_and_is_not_a_refusal(tmp_path, content):
    assert read_names(_write(tmp_path, content, name="my-keys.env")) == FileNames(names=(), problem=None)


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("my-keys.json", json.dumps({"OpenAI": f"  {FAKE_VALUE}  "})),
        ("my-keys.env", f'OpenAI="  {FAKE_VALUE}  "\n'),
    ],
)
def test_a_check_reads_the_same_stripped_value_from_either_format(tmp_path, name, text):
    path = _write(tmp_path, text, name=name)

    assert read_value(path, "OpenAI") == FAKE_VALUE, "the value comes back stripped of its spaces in either format"
    assert read_value(path, "openai") == FAKE_VALUE, "a name matches by its slug"
    assert read_value(path, "Anthropic") is None


def test_comments_blank_lines_spaces_and_quotes_are_handled_in_a_name_equals_value_file(tmp_path):
    text = (
        "# keys for the desk\n"
        "# Retired=sk-fake-keys-file-0002\n"
        "\n"
        "   # an indented comment is still a comment\n"
        f"  OpenAI  =  {FAKE_VALUE}  \n"
        f'Anthropic="{FAKE_VALUE_TWO}"\n'
        "Gemini='  spaced inside quotes  '\n"
        "\r\n"
        "Mistral=abc=def\n"
    )
    path = _write(tmp_path, text, name="my-keys.env")

    assert read_names(path) == FileNames(names=("OpenAI", "Anthropic", "Gemini", "Mistral"), problem=None)
    assert read_value(path, "OpenAI") == FAKE_VALUE, "spaces at both ends of the name and the value are stripped"
    assert read_value(path, "Anthropic") == FAKE_VALUE_TWO, "one matching pair of double quotes is removed"
    assert read_value(path, "Gemini") == "spaced inside quotes", "spaces inside quotes are stripped as a value is"
    assert read_value(path, "Mistral") == "abc=def", "the value keeps any = after its first one"
    assert read_value(path, "Retired") is None, "a commented line names no key"


def test_quotes_are_removed_only_as_a_matching_pair_and_nothing_else_is_interpreted(tmp_path):
    text = (
        'Alpha="one\'\n'  # mismatched quotes are kept
        "Beta=${HOME}\n"  # no variable expansion
        "Gamma=back\\nslash\n"  # no escapes
        'Delta="it\'s"\n'  # a quote inside a matching pair is kept
    )
    path = _write(tmp_path, text, name="my-keys.env")

    assert read_value(path, "Alpha") == "\"one'"
    assert read_value(path, "Beta") == "${HOME}"
    assert read_value(path, "Gamma") == "back\\nslash"
    assert read_value(path, "Delta") == "it's"


@pytest.mark.parametrize("line", ["just some words", FAKE_VALUE, '["OpenAI"]', '"sk-fake"'])
def test_a_line_with_no_equals_or_colon_refuses_the_whole_file_on_its_line_and_shows_no_text(tmp_path, line):
    # A JSON array does not start with a brace, so it is read as NAME=VALUE lines, and its line has no = or :.
    path = _write(tmp_path, f"OpenAI={FAKE_VALUE_TWO}\n{line}\nAnthropic=ok\n", name="my-keys.env")

    listing = read_names(path)

    assert listing.names == ()
    assert listing.problem == (
        "Not loaded. 1 of 3 entries were refused.\n"
        "line 2: no = or : between the name and the value\n"
        "Nothing was loaded."
    )
    for hidden in (line, FAKE_VALUE, FAKE_VALUE_TWO, "OpenAI", "Anthropic"):
        assert hidden not in listing.problem, "a refusal shows neither the line nor a name or a value"
    with pytest.raises(KeysFileError):
        read_value(path, "OpenAI")


@pytest.mark.parametrize(("line", "reason"), REFUSED_ENV_LINES)
def test_an_invalid_name_or_value_refuses_the_whole_name_equals_value_file_on_its_line(tmp_path, line, reason):
    path = _write(tmp_path, f"OpenAI={FAKE_VALUE_TWO}\n{line}\n", name="my-keys.env")

    listing = read_names(path)

    assert listing.names == ()
    assert listing.problem == f"Not loaded. 1 of 2 entries were refused.\nline 2: {reason}\nNothing was loaded."
    for hidden in (FAKE_VALUE, FAKE_VALUE_TWO, "OpenAI", "Bad/Name", "Blank", "Quoted", "Long"):
        assert hidden not in listing.problem


def test_a_repeated_name_in_a_name_equals_value_file_is_refused_by_its_slug_and_names_the_first_line(tmp_path):
    text = f"OpenAI={FAKE_VALUE}\nopenai={FAKE_VALUE_TWO}\nOpen_AI={FAKE_VALUE}\nopen.ai={FAKE_VALUE_TWO}\n"

    listing = read_names(_write(tmp_path, text, name="my-keys.env"))

    assert listing.names == ()
    assert listing.problem == (
        "Not loaded. 2 of 4 entries were refused.\n"
        "line 2: name repeats the one on line 1\n"
        "line 4: name repeats the one on line 3\n"
        "Nothing was loaded."
    )
    assert FAKE_VALUE not in listing.problem and FAKE_VALUE_TWO not in listing.problem


def test_a_built_in_name_in_a_name_equals_value_file_is_refused_on_each_line(tmp_path):
    text = f"RunPod={FAKE_VALUE}\nrunpod={FAKE_VALUE_TWO}\nOpenAI=ok\n"

    listing = read_names(_write(tmp_path, text, name="my-keys.env"))

    assert listing.names == ()
    assert listing.problem == (
        "Not loaded. 2 of 3 entries were refused.\n"
        "line 1: name is a built-in name\n"
        "line 2: name is a built-in name\n"
        "Nothing was loaded."
    )
    assert FAKE_VALUE not in listing.problem and FAKE_VALUE_TWO not in listing.problem


def test_a_json_line_inside_a_name_equals_value_file_is_refused_whole(tmp_path):
    text = f'OpenAI={FAKE_VALUE}\n{{"Anthropic": "{FAKE_VALUE_TWO}"}}\n'

    listing = read_names(_write(tmp_path, text, name="my-keys.env"))

    assert listing.names == ()
    assert listing.problem == (
        "Not loaded. 1 of 2 entries were refused.\nline 2: name is not allowed\nNothing was loaded."
    )
    assert FAKE_VALUE not in listing.problem and FAKE_VALUE_TWO not in listing.problem


def test_a_json_object_followed_by_name_equals_value_lines_is_refused_as_json(tmp_path):
    text = '{"OpenAI": "%s"}\nAnthropic=%s\n' % (FAKE_VALUE, FAKE_VALUE_TWO)

    listing = read_names(_write(tmp_path, text))

    assert listing.names == ()
    assert "0 entries were read" in listing.problem
    assert FAKE_VALUE not in listing.problem and FAKE_VALUE_TWO not in listing.problem


def test_a_name_equals_value_file_over_the_limit_is_refused_before_it_is_read(tmp_path):
    lines = "".join(f"Key{index}=value-{index}\n" for index in range(4000))
    path = tmp_path / "many.env"
    path.write_bytes(lines.encode("utf-8"))
    assert len(lines) > MAX_FILE_BYTES, "each line is valid, so only the size refuses the file"

    listing = read_names(str(path))

    assert listing.names == ()
    assert "larger than" in listing.problem


def test_a_name_equals_value_file_of_exactly_the_limit_is_read_and_one_byte_more_is_refused(tmp_path):
    body = b"OpenAI=ok\n"
    padding = b"#" + b" " * (MAX_FILE_BYTES - len(body) - 2) + b"\n"
    path = tmp_path / "limit.env"

    path.write_bytes(padding + body)
    assert path.stat().st_size == MAX_FILE_BYTES
    assert read_names(str(path)) == FileNames(names=("OpenAI",), problem=None)

    path.write_bytes(padding + body + b" ")
    assert "larger than" in read_names(str(path)).problem


def test_a_file_that_is_not_utf8_text_is_refused_with_a_count_and_no_value(tmp_path):
    plain = tmp_path / "plain.env"
    plain.write_bytes(b"OpenAI=" + FAKE_VALUE.encode("utf-8") + b"\xff\n")
    brace = tmp_path / "brace.json"
    brace.write_bytes(b'{"OpenAI": "' + FAKE_VALUE.encode("utf-8") + b'\xff"}')

    plain_listing = read_names(str(plain))
    brace_listing = read_names(str(brace))

    assert plain_listing.names == () and "is not UTF-8 text" in plain_listing.problem
    assert brace_listing.names == () and "is not valid JSON" in brace_listing.problem
    assert FAKE_VALUE not in plain_listing.problem and FAKE_VALUE not in brace_listing.problem


def test_a_json_file_with_leading_spaces_and_a_byte_order_mark_still_loads(tmp_path):
    path = tmp_path / "bom.json"
    path.write_bytes(b"\xef\xbb\xbf \n" + json.dumps({"OpenAI": FAKE_VALUE}).encode("utf-8"))

    assert read_names(str(path)) == FileNames(names=("OpenAI",), problem=None)
    assert read_value(str(path), "OpenAI") == FAKE_VALUE


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("my-keys.json", '{"ZetaKey": "%s", "zetakey": "%s"}' % (FAKE_VALUE, FAKE_VALUE_TWO)),
        ("my-keys.json", '{"Zeta/Key": "%s"}' % FAKE_VALUE),
        ("my-keys.json", '{"ZetaKey": "%s"}' % ("y" * (MAX_SECRET_LENGTH + 1))),
        ("my-keys.env", f"ZetaKey={FAKE_VALUE}\nzetakey={FAKE_VALUE_TWO}\n"),
        ("my-keys.env", f"Zeta/Key={FAKE_VALUE}\n"),
        ("my-keys.env", f"RunPod={FAKE_VALUE}\n"),
        ("my-keys.env", f"{FAKE_VALUE} ZetaKey\n"),
        ("my-keys.env", "ZetaKey=\n"),
        ("my-keys.env", "ZetaKey=" + "y" * (MAX_SECRET_LENGTH + 1) + "\n"),
    ],
)
def test_no_refusal_message_holds_a_name_or_a_value_in_either_format(tmp_path, name, text):
    listing = read_names(_write(tmp_path, text, name=name))

    assert listing.names == () and listing.problem
    for hidden in ("ZetaKey", "zetakey", "Zeta/Key", FAKE_VALUE, FAKE_VALUE_TWO, "yyyy"):
        assert hidden not in listing.problem


def test_listing_and_checking_a_name_equals_value_file_never_write_it(tmp_path):
    path = _write(tmp_path, f"OpenAI={FAKE_VALUE}\n", name="my-keys.env")
    _pin(path)
    before = _state(path)

    read_names(path)
    read_value(path, "OpenAI")
    read_value(path, "Gone")

    assert _state(path) == before


@pytest.mark.parametrize(
    "line",
    ["KEY=VALUE", "KEY = VALUE", "KEY= VALUE", "KEY:VALUE", "KEY: VALUE", "KEY : VALUE"],
)
def test_each_separator_form_reads_the_name_and_the_value(tmp_path, line):
    path = _write(tmp_path, f"{line}\n", name="my-keys.env")

    assert read_names(path) == FileNames(names=("KEY",), problem=None)
    assert read_value(path, "KEY") == "VALUE"


def test_a_url_value_keeps_its_colons_and_a_value_keeps_its_equals_signs(tmp_path):
    text = "URL: https://host.example/x?y=1\nOpenAI=https://host/x\nMistral=abc=def:ghi\n"
    path = _write(tmp_path, text, name="my-keys.env")

    assert read_names(path) == FileNames(names=("URL", "OpenAI", "Mistral"), problem=None)
    assert read_value(path, "URL") == "https://host.example/x?y=1"
    assert read_value(path, "OpenAI") == "https://host/x"
    assert read_value(path, "Mistral") == "abc=def:ghi"


def test_a_comment_line_and_a_blank_line_are_skipped_even_with_a_separator_in_them(tmp_path):
    text = "# KEY: VALUE\n\n   # OTHER=VALUE\n\t\nOpenAI: ok\n"
    path = _write(tmp_path, text, name="my-keys.env")

    assert read_names(path) == FileNames(names=("OpenAI",), problem=None)
    assert read_value(path, "OpenAI") == "ok"


@pytest.mark.parametrize("line", ["export KEY=x", "Open AI=x", "Open\tAI=x"])
def test_a_name_that_still_has_a_space_is_refused_on_its_line(tmp_path, line):
    path = _write(tmp_path, f"OpenAI=ok\n{line}\n", name="my-keys.env")

    listing = read_names(path)

    assert listing.names == ()
    assert listing.problem == "Not loaded. 1 of 2 entries were refused.\nline 2: name has a space\nNothing was loaded."


def test_an_empty_value_and_an_over_long_value_are_refused_with_their_line_numbers(tmp_path):
    text = f"OpenAI=ok\nGemini=\nMistral={'y' * 513}\n"

    listing = read_names(_write(tmp_path, text, name="my-keys.env"))

    assert listing.names == ()
    assert listing.problem == (
        "Not loaded. 2 of 3 entries were refused.\n"
        "line 2: value is empty\n"
        "line 3: value is longer than 512 characters\n"
        "Nothing was loaded."
    )
    assert "yyyy" not in listing.problem


def test_a_repeat_names_the_first_line_it_repeats_even_with_other_lines_between(tmp_path):
    text = f"OpenAI={FAKE_VALUE}\nAnthropic={FAKE_VALUE_TWO}\nopenai=ok\n"

    listing = read_names(_write(tmp_path, text, name="my-keys.env"))

    assert listing.problem == (
        "Not loaded. 1 of 3 entries were refused.\nline 3: name repeats the one on line 1\nNothing was loaded."
    )


def test_more_than_ten_refused_lines_list_ten_and_say_how_many_more(tmp_path):
    text = "".join(f"just words {index}\n" for index in range(1, 13)) + "OpenAI=ok\n"

    listing = read_names(_write(tmp_path, text, name="my-keys.env"))

    lines = listing.problem.split("\n")
    assert lines[0] == "Not loaded. 12 of 13 entries were refused."
    assert lines[1:11] == [f"line {number}: no = or : between the name and the value" for number in range(1, 11)]
    assert lines[11:] == ["and 2 more", "Nothing was loaded."]
    assert "just words" not in listing.problem


def test_no_refusal_line_holds_a_name_or_a_value_whatever_its_reason(tmp_path):
    text = (
        "QuuxKeyName quux-value-42\n"  # no = or :
        "QuuxKey Name=quux-value-42\n"  # name has a space
        "Quux/Key=quux-value-42\n"  # name is not allowed
        "RunPod=quux-value-42\n"  # a built-in name
        "QuuxKeyName=quux-value-42\n"  # accepted, so the next line repeats it
        "quuxkeyname=quux-value-43\n"  # name repeats
        "QuuxEmpty=\n"  # value is empty
        f"QuuxLong={'q' * 513}\n"  # value is too long
    )

    listing = read_names(_write(tmp_path, text, name="my-keys.env"))

    assert listing.names == ()
    assert listing.problem == (
        "Not loaded. 7 of 8 entries were refused.\n"
        "line 1: no = or : between the name and the value\n"
        "line 2: name has a space\n"
        "line 3: name is not allowed\n"
        "line 4: name is a built-in name\n"
        "line 6: name repeats the one on line 5\n"
        "line 7: value is empty\n"
        "line 8: value is longer than 512 characters\n"
        "Nothing was loaded."
    )
    lowered = listing.problem.lower()
    for hidden in ("quux", "runpod", "quuxkeyname", "quux-value-42", "quux-value-43", "qqqq"):
        assert hidden not in lowered


def test_a_json_file_still_loads_names_with_spaces_colons_and_equals_signs(tmp_path):
    path = _write(tmp_path, {"Open AI": "sk-a=b:c", "URL": "https://host/x"})

    assert read_names(path) == FileNames(names=("Open AI", "URL"), problem=None)
    assert read_value(path, "Open AI") == "sk-a=b:c"
    assert read_value(path, "URL") == "https://host/x"


def test_a_json_refusal_keeps_its_count_text_and_lists_no_lines(tmp_path):
    listing = read_names(_write(tmp_path, {"OpenAI": FAKE_VALUE, "Anthropic": 9}))

    assert listing.problem == (
        "Not loaded. 1 of 2 entries were refused. Each entry must be a key name with a text value of up to "
        "512 characters, and no name may repeat or be RunPod, which has its own entry. Nothing was loaded."
    )


def test_a_refused_name_equals_value_file_shows_its_refused_lines_on_the_block(qtbot, settings_path, tmp_path):
    path = _write(tmp_path, "OpenAI=ok\nexport KEY=x\n", name="my-keys.env")
    section = _section(qtbot, settings_path, MemoryKeyring(), choose=_chooser(path))

    _choose(section)

    status = section.findChild(QLabel, "keys-file-status").text()
    assert status == "Not loaded. 1 of 2 entries were refused.\nline 2: name has a space\nNothing was loaded."
    assert SettingsStore(settings_path).load().settings.keys_file_path == ""


# ----------------------------------------------------------- the settings key


def test_the_keys_file_path_is_saved_as_a_path_and_only_when_it_is_set(settings_path, tmp_path):
    path = _write(tmp_path, {"OpenAI": FAKE_VALUE})
    store = SettingsStore(settings_path)

    store.save(Settings(keys_file_path=path))
    text = settings_path.read_text(encoding="utf-8")
    assert json.loads(text)["keys_file_path"] == path
    assert FAKE_VALUE not in text, "the value never reaches the settings file"

    store.save(Settings())
    assert "keys_file_path" not in json.loads(settings_path.read_text(encoding="utf-8"))
    assert store.load().settings.keys_file_path == ""


def test_a_keys_file_path_that_is_not_text_is_refused_with_a_warning(settings_path):
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({"keys_file_path": ["x"]}), encoding="utf-8")

    loaded = SettingsStore(settings_path).load()

    assert loaded.settings.keys_file_path == ""
    assert loaded.warning is not None and "keys_file_path" in loaded.warning


# ----------------------------------------------------------- the Check reads from the file


def test_a_check_reads_its_value_from_the_file_and_not_from_the_keychain(tmp_path):
    path = _write(tmp_path, {"OpenAI": FAKE_VALUE})
    backend = MemoryKeyring()
    backend.set_password(KEY_SERVICE, account_for("OpenAI"), FAKE_VALUE_TWO)
    transport = FakeTransport(200)

    result = check_saved_key(OPENAI, "OpenAI", KeyringSecretStore(backend=backend), transport=transport, keys_file=path)

    assert result == WORKS_TEXT
    request, timeout = transport.calls[0]
    assert urllib.parse.urlsplit(request.full_url).hostname == "api.openai.com"
    assert _header(request, "Authorization") == "Bearer " + FAKE_VALUE
    assert timeout == 10.0


def test_a_check_for_a_name_the_file_does_not_hold_says_so_and_sends_nothing(tmp_path):
    path = _write(tmp_path, {"Anthropic": FAKE_VALUE})
    backend = MemoryKeyring()
    backend.set_password(KEY_SERVICE, account_for("OpenAI"), FAKE_VALUE_TWO)
    transport = FakeTransport(200)

    result = check_saved_key(OPENAI, "OpenAI", KeyringSecretStore(backend=backend), transport=transport, keys_file=path)

    assert result == NOT_IN_FILE_TEXT.format(name="OpenAI") == "OpenAI is not in the keys file."
    assert transport.calls == [], "the keychain value is not used for a file check"


def test_a_check_with_a_missing_or_refused_file_says_so_and_sends_nothing(tmp_path):
    missing = str(tmp_path / "gone.json")
    refused = _write(tmp_path, {"OpenAI": 7})
    transport = FakeTransport(200)

    for path in (missing, refused):
        result = check_saved_key(OPENAI, "OpenAI", _empty_keychain(), transport=transport, keys_file=path)
        assert result == FILE_UNREADABLE_TEXT == "The keys file could not be read. Choose the file again."

    assert transport.calls == []


def test_a_service_with_no_check_reads_neither_the_file_nor_the_keychain(tmp_path):
    refused = _write(tmp_path, {"OpenAI": 7})
    no_check = dataclasses.replace(OPENAI, check=None)
    transport = FakeTransport(200)

    result = check_saved_key(no_check, "OpenAI", _empty_keychain(), transport=transport, keys_file=refused)

    assert result == NO_CHECK_TEXT
    assert transport.calls == []


# ----------------------------------------------------------- the Keys block


def test_choosing_a_file_lists_its_names_as_from_file_and_copies_nothing_to_the_keychain(
    qtbot, settings_path, tmp_path
):
    path = _write(tmp_path, {"OpenAI": FAKE_VALUE, "Anthropic": FAKE_VALUE_TWO})
    backend = MemoryKeyring()
    section = _section(qtbot, settings_path, backend, choose=_chooser(path))

    _choose(section)

    assert section.findChild(QLabel, "keys-entry-openai-status").text() == FROM_FILE_NOTE
    assert section.findChild(QLabel, "keys-entry-anthropic-status").text() == FROM_FILE_NOTE
    assert section.findChild(QLineEdit, "keys-entry-openai-value") is None, "a file name has no value box"
    assert section.findChild(QLabel, "keys-file-status").text() == FILE_LISTED_MANY_TEXT.format(count=2)
    assert section.findChild(QLabel, "keys-file-path").text() == FILE_PATH_TEXT.format(name="my-keys.json")
    saved = SettingsStore(settings_path).load().settings
    assert saved.keys_file_path == path
    assert saved.key_names == (), "a file name is not saved as a keychain name"
    assert backend.snapshot() == {}, "nothing is copied to the keychain"
    assert FAKE_VALUE not in _all_text(section)
    assert FAKE_VALUE not in settings_path.read_text(encoding="utf-8")


def test_the_choose_dialog_opens_on_the_home_folder_and_a_cancel_changes_nothing(qtbot, settings_path, monkeypatch):
    asked: dict[str, object] = {}

    class FakeDialog:
        @staticmethod
        def getOpenFileName(parent, title, start, names):
            asked.update(start=start, names=names)
            return "", ""

    monkeypatch.setattr(keys_module, "QFileDialog", FakeDialog)
    section = _section(qtbot, settings_path, MemoryKeyring())

    _choose(section)

    assert asked["start"] == str(Path.home())
    assert "*.json" in asked["names"]
    assert SettingsStore(settings_path).load().settings.keys_file_path == ""
    assert section.findChild(QLabel, "keys-file-path").text() == NO_FILE_TEXT


def test_the_choose_dialog_offers_json_and_name_equals_value_files_only(qtbot, settings_path, monkeypatch):
    asked: dict[str, object] = {}

    class FakeDialog:
        @staticmethod
        def getOpenFileName(parent, title, start, names):
            asked.update(names=names)
            return "", ""

    monkeypatch.setattr(keys_module, "QFileDialog", FakeDialog)
    section = _section(qtbot, settings_path, MemoryKeyring())

    _choose(section)

    assert asked["names"] == "Keys files (*.json *.env *.txt)", "the filter lists the two keys file formats only"
    assert "All files" not in asked["names"]


def test_a_refused_file_is_not_chosen_and_its_count_is_shown_without_a_name_or_value(qtbot, settings_path, tmp_path):
    path = _write(tmp_path, {"OpenAI": FAKE_VALUE, "Anthropic": 9})
    section = _section(qtbot, settings_path, MemoryKeyring(), choose=_chooser(path))

    _choose(section)

    status = section.findChild(QLabel, "keys-file-status").text()
    assert "1 of 2 entries were refused" in status
    for hidden in (FAKE_VALUE, "OpenAI", "Anthropic"):
        assert hidden not in status
    assert SettingsStore(settings_path).load().settings.keys_file_path == ""
    assert section.findChild(QWidget, "keys-entry-openai") is None


def test_a_check_on_a_from_file_name_reads_the_value_from_the_file(qtbot, settings_path, tmp_path):
    path = _write(tmp_path, {"OpenAI": FAKE_VALUE})
    transport = FakeTransport(200)
    section = _section(qtbot, settings_path, MemoryKeyring(), transport=transport, choose=_chooser(path))
    _choose(section)
    result = section.findChild(QLabel, "keys-entry-openai-check-result")

    section.findChild(QPushButton, "keys-entry-openai-check").click()
    qtbot.waitUntil(lambda: result.text() == WORKS_TEXT, timeout=5000)

    assert _header(transport.calls[0][0], "Authorization") == "Bearer " + FAKE_VALUE
    assert FAKE_VALUE not in _all_text(section)


def test_a_check_on_a_from_file_name_that_the_file_later_drops_says_so(qtbot, settings_path, tmp_path):
    path = _write(tmp_path, {"OpenAI": FAKE_VALUE})
    transport = FakeTransport(200)
    section = _section(qtbot, settings_path, MemoryKeyring(), transport=transport, choose=_chooser(path))
    _choose(section)
    _write(tmp_path, {"Anthropic": FAKE_VALUE_TWO})
    section.findChild(QPushButton, "keys-entry-openai-check").click()
    result = section.findChild(QLabel, "keys-entry-openai-check-result")

    qtbot.waitUntil(lambda: result.text() == NOT_IN_FILE_TEXT.format(name="OpenAI"), timeout=5000)

    assert transport.calls == []


def test_remove_on_a_from_file_name_hides_it_for_this_session_and_leaves_the_file_unchanged(
    qtbot, settings_path, tmp_path
):
    path = _write(tmp_path, {"OpenAI": FAKE_VALUE, "Anthropic": FAKE_VALUE_TWO})
    _pin(path)
    before = _state(path)
    section = _section(qtbot, settings_path, MemoryKeyring(), choose=_chooser(path))
    _choose(section)

    section.findChild(QPushButton, "keys-entry-openai-remove").click()
    qtbot.waitUntil(lambda: section.findChild(QWidget, "keys-entry-openai") is None, timeout=5000)

    message = section.findChild(QLabel, "keys-message").text()
    assert message == REMOVED_FROM_FILE_TEXT.format(name="OpenAI")
    assert "unchanged" in message
    assert section.findChild(QWidget, "keys-entry-anthropic") is not None
    assert _state(path) == before, "the file is not written"
    assert SettingsStore(settings_path).load().settings.keys_file_path == path, "the file stays chosen"

    section.refresh()
    assert section.findChild(QWidget, "keys-entry-openai") is None, "a refresh does not bring the removed name back"


def test_a_missing_file_says_so_plainly_on_the_block(qtbot, settings_path, tmp_path):
    gone = str(tmp_path / "gone.json")
    SettingsStore(settings_path).save(Settings(keys_file_path=gone))

    section = _section(qtbot, settings_path, MemoryKeyring())

    assert "is missing" in section.findChild(QLabel, "keys-file-status").text()
    assert section.findChild(QLabel, "keys-file-path").text() == FILE_PATH_TEXT.format(name="gone.json")
    assert section.findChild(QPushButton, "keys-file-stop").isEnabled(), "a missing file can still be stopped"
    assert section.findChild(QWidget, "keys-entry-openai") is None


def test_stop_using_the_file_clears_the_path_and_its_names_and_leaves_the_file(qtbot, settings_path, tmp_path):
    path = _write(tmp_path, {"OpenAI": FAKE_VALUE})
    _pin(path)
    before = _state(path)
    section = _section(qtbot, settings_path, MemoryKeyring(), choose=_chooser(path))
    assert not section.findChild(QPushButton, "keys-file-stop").isEnabled(), "there is nothing to stop yet"
    _choose(section)

    section.findChild(QPushButton, "keys-file-stop").click()
    qtbot.waitUntil(lambda: section.findChild(QWidget, "keys-entry-openai") is None, timeout=5000)

    assert SettingsStore(settings_path).load().settings.keys_file_path == ""
    assert section.findChild(QLabel, "keys-file-path").text() == NO_FILE_TEXT
    assert section.findChild(QLabel, "keys-message").text() == FILE_STOPPED_TEXT
    assert _state(path) == before


def test_a_name_in_both_the_keychain_and_the_file_shows_once_as_the_keychain_name(qtbot, settings_path, tmp_path):
    path = _write(tmp_path, {"openai": FAKE_VALUE_TWO})
    backend = MemoryKeyring()
    backend.set_password(KEY_SERVICE, account_for("OpenAI"), FAKE_VALUE)
    SettingsStore(settings_path).save(Settings(key_names=("OpenAI",), keys_file_path=path))

    section = _section(qtbot, settings_path, backend)

    assert section.findChild(QLineEdit, "keys-entry-openai-value") is not None, "the keychain card is the one shown"
    assert section.findChild(QLabel, "keys-entry-openai-status").text() == STATUS_TEXT[STATUS_SET]
    assert FAKE_VALUE not in _all_text(section)
    assert FAKE_VALUE_TWO not in _all_text(section)


def test_a_file_that_goes_bad_after_it_was_chosen_lists_nothing_and_says_why(qtbot, settings_path, tmp_path):
    path = _write(tmp_path, {"OpenAI": FAKE_VALUE})
    section = _section(qtbot, settings_path, MemoryKeyring(), choose=_chooser(path))
    _choose(section)
    assert section.findChild(QWidget, "keys-entry-openai") is not None

    _write(tmp_path, {"OpenAI": FAKE_VALUE, "Anthropic": 3})
    section.refresh()
    qtbot.waitUntil(lambda: section.findChild(QWidget, "keys-entry-openai") is None, timeout=5000)

    status = section.findChild(QLabel, "keys-file-status").text()
    assert "1 of 2 entries were refused" in status
    assert FAKE_VALUE not in status


def test_no_value_reaches_the_keychain_the_settings_file_or_any_text_on_the_block(qtbot, settings_path, tmp_path):
    path = _write(tmp_path, {"OpenAI": FAKE_VALUE})
    backend = MemoryKeyring()
    transport = FakeTransport(401)
    section = _section(qtbot, settings_path, backend, transport=transport, choose=_chooser(path))
    _choose(section)
    result = section.findChild(QLabel, "keys-entry-openai-check-result")

    section.findChild(QPushButton, "keys-entry-openai-check").click()
    qtbot.waitUntil(lambda: result.text() == REJECTED_TEXT.format(service="OpenAI"), timeout=5000)

    request, _timeout = transport.calls[0]
    assert urllib.parse.urlsplit(request.full_url).hostname == "api.openai.com", "the value goes to the provider only"
    assert _header(request, "Authorization") == "Bearer " + FAKE_VALUE
    assert backend.snapshot() == {}
    assert FAKE_VALUE not in settings_path.read_text(encoding="utf-8")
    assert FAKE_VALUE not in _all_text(section)


def test_the_file_is_never_written_by_choosing_checking_removing_or_stopping(qtbot, settings_path, tmp_path):
    path = _write(tmp_path, {"OpenAI": FAKE_VALUE, "Anthropic": FAKE_VALUE_TWO})
    _pin(path)
    before = _state(path)
    section = _section(qtbot, settings_path, MemoryKeyring(), transport=FakeTransport(200), choose=_chooser(path))
    _choose(section)
    result = section.findChild(QLabel, "keys-entry-openai-check-result")
    section.findChild(QPushButton, "keys-entry-openai-check").click()
    qtbot.waitUntil(lambda: result.text() == WORKS_TEXT, timeout=5000)

    section.findChild(QPushButton, "keys-entry-openai-remove").click()
    qtbot.waitUntil(lambda: section.findChild(QWidget, "keys-entry-openai") is None, timeout=5000)
    section.findChild(QPushButton, "keys-file-stop").click()
    qtbot.waitUntil(lambda: section.findChild(QWidget, "keys-entry-anthropic") is None, timeout=5000)

    assert _state(path) == before


def test_choosing_a_name_equals_value_file_lists_its_names_from_the_file(qtbot, settings_path, tmp_path):
    path = _write(tmp_path, f"OpenAI={FAKE_VALUE}\nAnthropic={FAKE_VALUE_TWO}\n", name="my-keys.env")
    backend = MemoryKeyring()
    section = _section(qtbot, settings_path, backend, choose=_chooser(path))

    _choose(section)

    assert section.findChild(QLabel, "keys-entry-openai-status").text() == FROM_FILE_NOTE
    assert section.findChild(QLabel, "keys-entry-anthropic-status").text() == FROM_FILE_NOTE
    assert section.findChild(QLabel, "keys-file-status").text() == FILE_LISTED_MANY_TEXT.format(count=2)
    assert SettingsStore(settings_path).load().settings.keys_file_path == path
    assert backend.snapshot() == {}, "nothing is copied to the keychain"
    assert FAKE_VALUE not in _all_text(section)
    assert FAKE_VALUE not in settings_path.read_text(encoding="utf-8")


def test_a_check_on_a_name_from_a_name_equals_value_file_reads_the_file(qtbot, settings_path, tmp_path):
    path = _write(tmp_path, f"OpenAI={FAKE_VALUE}\n", name="my-keys.env")
    transport = FakeTransport(200)
    section = _section(qtbot, settings_path, MemoryKeyring(), transport=transport, choose=_chooser(path))
    _choose(section)
    result = section.findChild(QLabel, "keys-entry-openai-check-result")

    section.findChild(QPushButton, "keys-entry-openai-check").click()
    qtbot.waitUntil(lambda: result.text() == WORKS_TEXT, timeout=5000)

    assert _header(transport.calls[0][0], "Authorization") == "Bearer " + FAKE_VALUE
    assert FAKE_VALUE not in _all_text(section)


def test_the_keys_block_describes_both_file_formats_and_comments(qtbot, settings_path):
    section = _section(qtbot, settings_path, MemoryKeyring())

    intro = section.findChild(QLabel, "keys-file-intro").text()

    assert "JSON object" in intro and "NAME=VALUE" in intro and "NAME: VALUE" in intro
    assert "starts with #" in intro, "a line that starts with # is a comment"
