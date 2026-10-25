"""Tests for the keys file: a JSON file of key names and values that the owner chooses, and the Keys block listing it.

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
from meridian_desktop.api_keys import KEY_SERVICE, STATUS_SET, ApiKeys, account_for
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


def _write(tmp_path: Path, content) -> str:
    """Write the keys file under tmp_path and return its path. A dict is written as JSON; text is written as it is."""
    folder = tmp_path / "keys"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "my-keys.json"
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


@pytest.mark.parametrize("content", ['["OpenAI"]', '"sk-fake"', "", "{not json"])
def test_a_file_that_is_not_a_json_object_is_refused_with_a_count_and_no_value(tmp_path, content):
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
