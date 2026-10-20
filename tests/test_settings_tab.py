"""Tests for the Settings tab's connection section (meridian_desktop/ui/settings_tab.py), with pytest-qt.

Every outside effect is injected: an in-memory keychain, a fake file chooser, and a list call that is
either a fake or the real hosted client with a fake transport. So nothing touches the real keychain,
the real file dialog or the network. FAKE_TOKEN is an obviously fake value, so a leak check can search
for it. Offscreen Qt, from conftest.
"""

from __future__ import annotations

import functools
import json
import threading
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QLineEdit, QPushButton

from _fakes import FailingKeyring, MemoryKeyring
from meridian_desktop import hosted_client, local_client
from meridian_desktop.hosted_client import (
    BAD_RESPONSE_TEXT,
    BAD_SCHEME_TEXT,
    FORBIDDEN_TEXT,
    NOT_FOUND_TEXT,
    NOT_SIGNED_IN_TEXT,
    REJECTED_TEXT,
    TIMEOUT_TEXT,
    UNREACHABLE_TEXT,
    Reply,
)
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import DEFAULT_HOSTED_URL, DEFAULT_LOCAL_URL, Settings, SettingsStore
from meridian_desktop.token_store import HOSTED_TOKEN_ACCOUNT, TOKEN_SERVICE, get_token, set_token
from meridian_desktop.ui.settings_tab import (
    DB_SAVED_TEXT,
    HOSTED_URL_SAVED_TEXT,
    TEST_CONNECTED_TEXT,
    TEST_RUNNING_TEXT,
    TEST_UNEXPECTED_TEXT,
    TOKEN_EMPTY_TEXT,
    TOKEN_NOT_SET_TEXT,
    TOKEN_NOTHING_REMOVED_TEXT,
    TOKEN_REMOVED_TEXT,
    TOKEN_SAVED_TEXT,
    TOKEN_SET_TEXT,
    SettingsTab,
)

FAKE_TOKEN = "sk_meridian_fake-settings-tab-0006"
HOSTED_URL = "https://hosted.test"
LOCAL_OK = "http://localhost:7999"
VALID_ROWS = [{"id": "p-1", "name": "Alpha"}, {"id": "p-2", "name": "Beta"}]
BODY_LEAK = "BODY-SECRET-DO-NOT-SHOW"
SELECTABLE_MOUSE = Qt.TextInteractionFlag.TextSelectableByMouse
SELECTABLE_KEYBOARD = Qt.TextInteractionFlag.TextSelectableByKeyboard


class FakeTransport:
    """A transport that returns one canned reply, or raises one canned error. It records each request."""

    def __init__(self, reply: Reply | None = None, error: BaseException | None = None) -> None:
        self.reply = reply if reply is not None else Reply(200, json.dumps(VALID_ROWS).encode("utf-8"))
        self.error = error
        self.requests: list = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.reply


def _unexpected_list(url, token):
    raise AssertionError("the hosted list must not be called in this test")


def _no_dialog(parent, start_dir):
    raise AssertionError("the real file dialog must not open in tests")


@pytest.fixture
def backend():
    return MemoryKeyring()


@pytest.fixture
def secrets(backend):
    return KeyringSecretStore(backend=backend)


def _tab(qtbot, settings_path, secrets, *, list_hosted=_unexpected_list, choose_database=_no_dialog):
    tab = SettingsTab(
        SettingsStore(settings_path),
        choose_database=choose_database,
        secrets=secrets,
        list_hosted=list_hosted,
    )
    qtbot.addWidget(tab)
    return tab


def _field(tab, name):
    return tab.findChild(QLineEdit, name)


def _button(tab, name):
    return tab.findChild(QPushButton, name)


def _label(tab, name):
    return tab.findChild(QLabel, name)


def _all_visible_text(tab) -> str:
    """Every label and button text on the tab, joined. A leak check searches this."""
    texts = [label.text() for label in tab.findChildren(QLabel)]
    texts += [button.text() for button in tab.findChildren(QPushButton)]
    return "\n".join(texts)


def _selectable(widget) -> bool:
    flags = widget.textInteractionFlags()
    return bool(flags & SELECTABLE_MOUSE) and bool(flags & SELECTABLE_KEYBOARD)


# ----------------------------------------------------------- hosted URL


def test_hosted_url_field_starts_at_the_default(qtbot, settings_path, secrets):
    tab = _tab(qtbot, settings_path, secrets)

    assert DEFAULT_HOSTED_URL == "https://usemeridian.us"
    assert _field(tab, "settings-hosted-url").text() == DEFAULT_HOSTED_URL


def test_save_hosted_url_writes_the_validated_url_and_keeps_other_fields(qtbot, settings_path, secrets):
    SettingsStore(settings_path).save(Settings(local_url=LOCAL_OK))
    tab = _tab(qtbot, settings_path, secrets)
    _field(tab, "settings-hosted-url").setText("  https://hosted.test/  ")

    _button(tab, "settings-save-hosted-url").click()

    saved = SettingsStore(settings_path).load().settings
    assert saved.hosted_url == HOSTED_URL
    assert saved.local_url == LOCAL_OK, "the local URL is kept"
    assert _label(tab, "settings-hosted-url-message").text() == HOSTED_URL_SAVED_TEXT
    assert not _label(tab, "settings-hosted-url-message").isHidden()
    assert _field(tab, "settings-hosted-url").text() == HOSTED_URL


@pytest.mark.parametrize(
    "value",
    [
        "http://usemeridian.us",
        "ftp://usemeridian.us",
        "https://",
        "https://user:pw-not-real@hosted.test",
        "https://hosted.test/?next=1",
        "http://127.0.0.1.example.com",
        "",
    ],
)
def test_save_hosted_url_refuses_an_unsafe_value_and_writes_nothing(qtbot, settings_path, secrets, value):
    SettingsStore(settings_path).save(Settings(hosted_url=HOSTED_URL))
    before = settings_path.read_bytes()
    tab = _tab(qtbot, settings_path, secrets)
    _field(tab, "settings-hosted-url").setText(value)

    _button(tab, "settings-save-hosted-url").click()

    message = _label(tab, "settings-hosted-url-message")
    assert message.text().startswith("Not saved. ")
    assert not message.isHidden()
    assert settings_path.read_bytes() == before


def test_save_hosted_url_accepts_http_on_loopback(qtbot, settings_path, secrets):
    tab = _tab(qtbot, settings_path, secrets)
    _field(tab, "settings-hosted-url").setText("http://127.0.0.1:7878")

    _button(tab, "settings-save-hosted-url").click()

    assert SettingsStore(settings_path).load().settings.hosted_url == "http://127.0.0.1:7878"


# ----------------------------------------------------------- hosted token


def test_token_field_hides_the_value_with_password_echo(qtbot, settings_path, secrets):
    tab = _tab(qtbot, settings_path, secrets)

    assert _field(tab, "settings-token-input").echoMode() == QLineEdit.EchoMode.Password


def test_token_status_starts_not_set(qtbot, settings_path, secrets):
    tab = _tab(qtbot, settings_path, secrets)

    assert _label(tab, "settings-token-status").text() == TOKEN_NOT_SET_TEXT


def test_saving_the_token_stores_it_in_the_keychain_and_shows_only_set(qtbot, settings_path, secrets, backend):
    tab = _tab(qtbot, settings_path, secrets)
    _field(tab, "settings-token-input").setText(FAKE_TOKEN)

    _button(tab, "settings-save-token").click()

    assert backend.snapshot() == {(TOKEN_SERVICE, HOSTED_TOKEN_ACCOUNT): FAKE_TOKEN}
    assert _label(tab, "settings-token-status").text() == TOKEN_SET_TEXT
    assert _field(tab, "settings-token-input").text() == "", "the box is cleared after a save"
    assert _label(tab, "settings-token-message").text() == TOKEN_SAVED_TEXT
    assert FAKE_TOKEN not in _all_visible_text(tab)


def test_saved_token_never_reaches_the_settings_file(qtbot, settings_path, secrets):
    tab = _tab(qtbot, settings_path, secrets)
    _field(tab, "settings-token-input").setText(FAKE_TOKEN)
    _button(tab, "settings-save-token").click()
    _field(tab, "settings-hosted-url").setText(HOSTED_URL)
    _button(tab, "settings-save-hosted-url").click()
    SettingsStore(settings_path).set_mode("hosted")  # a later save through the store must not pick it up either

    assert settings_path.exists(), "the settings file should exist, or the scan proves nothing"
    needles = (FAKE_TOKEN.encode("utf-8"), FAKE_TOKEN.encode("utf-16-le"))
    for path in [p for p in settings_path.parent.rglob("*") if p.is_file()]:
        data = path.read_bytes()
        for needle in needles:
            assert needle not in data, f"token text found in {path.name}"
    saved = json.loads(settings_path.read_text(encoding="utf-8"))
    assert set(saved) == {"mode", "hosted_url", "local_url", "engine_url", "db_path"}


def test_empty_token_is_refused_and_nothing_is_stored(qtbot, settings_path, secrets, backend):
    tab = _tab(qtbot, settings_path, secrets)
    _field(tab, "settings-token-input").setText("   ")

    _button(tab, "settings-save-token").click()

    assert _label(tab, "settings-token-message").text() == TOKEN_EMPTY_TEXT
    assert backend.snapshot() == {}


def test_token_with_a_space_is_refused_and_not_stored(qtbot, settings_path, secrets, backend):
    tab = _tab(qtbot, settings_path, secrets)
    value = f"{FAKE_TOKEN} with a space"
    _field(tab, "settings-token-input").setText(value)

    _button(tab, "settings-save-token").click()

    message = _label(tab, "settings-token-message").text()
    assert message.startswith("Not saved. ")
    assert value not in message
    assert backend.snapshot() == {}


def test_remove_token_clears_the_keychain_entry(qtbot, settings_path, secrets, backend):
    tab = _tab(qtbot, settings_path, secrets)
    _field(tab, "settings-token-input").setText(FAKE_TOKEN)
    _button(tab, "settings-save-token").click()

    _button(tab, "settings-remove-token").click()

    assert backend.snapshot() == {}
    assert _label(tab, "settings-token-status").text() == TOKEN_NOT_SET_TEXT
    assert _label(tab, "settings-token-message").text() == TOKEN_REMOVED_TEXT

    _button(tab, "settings-remove-token").click()
    assert _label(tab, "settings-token-message").text() == TOKEN_NOTHING_REMOVED_TEXT


def test_a_keychain_failure_on_save_is_shown_without_the_token(qtbot, settings_path):
    # FailingKeyring puts the value in every error it raises, on purpose.
    secrets = KeyringSecretStore(backend=FailingKeyring(FAKE_TOKEN))
    tab = _tab(qtbot, settings_path, secrets)
    _field(tab, "settings-token-input").setText(FAKE_TOKEN)

    _button(tab, "settings-save-token").click()

    message = _label(tab, "settings-token-message").text()
    assert message.startswith("Not saved. ")
    assert "keyring save failed (ValueError)" in message
    assert FAKE_TOKEN not in _all_visible_text(tab)
    assert _label(tab, "settings-token-status").text() == TOKEN_NOT_SET_TEXT


def test_refresh_reflects_a_token_saved_elsewhere(qtbot, settings_path, secrets):
    tab = _tab(qtbot, settings_path, secrets)
    assert _label(tab, "settings-token-status").text() == TOKEN_NOT_SET_TEXT

    set_token(FAKE_TOKEN, store=secrets)
    tab.refresh()

    assert _label(tab, "settings-token-status").text() == TOKEN_SET_TEXT


# ----------------------------------------------------------- test connection


def test_test_connection_uses_the_saved_url_and_token_not_the_typed_text(qtbot, settings_path, secrets):
    SettingsStore(settings_path).save(Settings(hosted_url=HOSTED_URL))
    set_token(FAKE_TOKEN, store=secrets)
    calls: list[tuple[str, str | None]] = []

    def fake_list(url, token):
        calls.append((url, token))
        return list(VALID_ROWS)

    tab = _tab(qtbot, settings_path, secrets, list_hosted=fake_list)
    _field(tab, "settings-hosted-url").setText("https://typed-but-not-saved.test")

    _button(tab, "settings-test-connection").click()

    result = _label(tab, "settings-test-result")
    expected = TEST_CONNECTED_TEXT.format(url=HOSTED_URL, count=2)
    qtbot.waitUntil(lambda: result.text() == expected, timeout=5000)
    assert calls == [(HOSTED_URL, FAKE_TOKEN)]
    assert _button(tab, "settings-test-connection").isEnabled()
    assert FAKE_TOKEN not in result.text()


@pytest.mark.parametrize(
    ("case", "token_saved", "transport", "expected"),
    [
        ("no token", False, FakeTransport(), NOT_SIGNED_IN_TEXT),
        ("rejected token", True, FakeTransport(Reply(401, BODY_LEAK.encode())), REJECTED_TEXT),
        ("forbidden", True, FakeTransport(Reply(403, b"")), FORBIDDEN_TEXT),
        ("not found", True, FakeTransport(Reply(404, b"")), NOT_FOUND_TEXT),
        ("timeout", True, FakeTransport(error=TimeoutError("slow")), TIMEOUT_TEXT),
        ("unreachable", True, FakeTransport(error=ConnectionRefusedError("refused")), UNREACHABLE_TEXT),
        ("server error", True, FakeTransport(Reply(500, BODY_LEAK.encode())), "Hosted Meridian returned HTTP 500."),
        ("bad body", True, FakeTransport(Reply(200, b"not json")), BAD_RESPONSE_TEXT),
        ("connected", True, FakeTransport(), TEST_CONNECTED_TEXT.format(url=DEFAULT_HOSTED_URL, count=2)),
    ],
)
def test_test_connection_shows_plain_words_for_each_outcome(
    qtbot, settings_path, secrets, case, token_saved, transport, expected
):
    if token_saved:
        set_token(FAKE_TOKEN, store=secrets)
    list_hosted = functools.partial(hosted_client.list_projects, transport=transport)
    tab = _tab(qtbot, settings_path, secrets, list_hosted=list_hosted)

    _button(tab, "settings-test-connection").click()

    result = _label(tab, "settings-test-result")
    qtbot.waitUntil(lambda: result.text() not in ("", TEST_RUNNING_TEXT), timeout=5000)
    assert result.text() == expected, case
    assert FAKE_TOKEN not in _all_visible_text(tab), case
    assert BODY_LEAK not in _all_visible_text(tab), case
    if not token_saved:
        assert transport.requests == [], "no token means no request"


def test_test_connection_refuses_a_saved_insecure_url_without_a_request(qtbot, settings_path, secrets):
    SettingsStore(settings_path).save(Settings(hosted_url="http://usemeridian.us"))
    set_token(FAKE_TOKEN, store=secrets)
    transport = FakeTransport()
    tab = _tab(qtbot, settings_path, secrets, list_hosted=functools.partial(hosted_client.list_projects, transport=transport))

    _button(tab, "settings-test-connection").click()

    result = _label(tab, "settings-test-result")
    qtbot.waitUntil(lambda: result.text() == BAD_SCHEME_TEXT, timeout=5000)
    assert transport.requests == []


def test_unexpected_error_names_the_class_only(qtbot, settings_path, secrets):
    set_token(FAKE_TOKEN, store=secrets)

    def boom(url, token):
        raise RuntimeError(f"leaked {FAKE_TOKEN}")

    tab = _tab(qtbot, settings_path, secrets, list_hosted=boom)

    _button(tab, "settings-test-connection").click()

    result = _label(tab, "settings-test-result")
    expected = TEST_UNEXPECTED_TEXT.format(name="RuntimeError")
    qtbot.waitUntil(lambda: result.text() == expected, timeout=5000)
    assert FAKE_TOKEN not in _all_visible_text(tab)


def test_test_button_is_disabled_while_the_test_runs(qtbot, settings_path, secrets):
    gate = threading.Event()

    def slow(url, token):
        gate.wait(5)
        return []

    tab = _tab(qtbot, settings_path, secrets, list_hosted=slow)
    button = _button(tab, "settings-test-connection")

    button.click()

    assert not button.isEnabled()
    assert _label(tab, "settings-test-result").text() == TEST_RUNNING_TEXT
    gate.set()
    result = _label(tab, "settings-test-result")
    qtbot.waitUntil(lambda: button.isEnabled(), timeout=5000)
    assert result.text() == TEST_CONNECTED_TEXT.format(url=DEFAULT_HOSTED_URL, count=0)


# ----------------------------------------------------------- local URL and database file (kept)


@pytest.mark.parametrize("value", ["http://example.test:7878", "https://127.0.0.1:7878", "http://127.0.0.1.example.com:7878", "ftp://127.0.0.1"])
def test_settings_tab_rejects_non_loopback_local_url(qtbot, settings_path, secrets, value):
    tab = _tab(qtbot, settings_path, secrets)
    _field(tab, "settings-local-url").setText(value)

    _button(tab, "settings-save-local-url").click()

    message = _label(tab, "settings-local-url-message")
    assert message.text().startswith("Not saved. ")
    assert not settings_path.exists(), "a refused local URL writes nothing"


def test_settings_tab_saves_a_loopback_local_url(qtbot, settings_path, secrets):
    tab = _tab(qtbot, settings_path, secrets)
    _field(tab, "settings-local-url").setText(LOCAL_OK)

    _button(tab, "settings-save-local-url").click()

    assert SettingsStore(settings_path).load().settings.local_url == LOCAL_OK
    assert DEFAULT_LOCAL_URL != LOCAL_OK
    assert local_client.assert_loopback(LOCAL_OK) is None


def test_database_file_is_chosen_through_the_dialog_and_shown_back(qtbot, settings_path, secrets, tmp_path):
    chosen = tmp_path / "meridian.db"
    start_dirs: list[str] = []

    def chooser(parent, start_dir):
        start_dirs.append(start_dir)
        return str(chosen)

    tab = _tab(qtbot, settings_path, secrets, choose_database=chooser)

    _button(tab, "settings-choose-db").click()

    assert start_dirs == [""], "no file chosen yet, so the dialog starts empty"
    assert _label(tab, "settings-db-path").text() == str(chosen)
    assert SettingsStore(settings_path).load().settings.db_path == str(chosen)
    assert _label(tab, "settings-db-message").text() == DB_SAVED_TEXT


# ----------------------------------------------------------- page wording and selectable text


def test_no_not_built_yet_wording_on_the_settings_tab(qtbot, settings_path, secrets):
    tab = _tab(qtbot, settings_path, secrets)
    texts = [_all_visible_text(tab)]
    texts += [field.placeholderText() for field in tab.findChildren(QLineEdit)]
    combined = "\n".join(texts).lower()

    assert "not built" not in combined
    assert "wave" not in combined


def test_every_label_on_the_settings_tab_is_selectable(qtbot, settings_path, secrets):
    tab = _tab(qtbot, settings_path, secrets)
    labels = tab.findChildren(QLabel)

    assert len(labels) >= 10, "the walk reaches the real labels"
    missing = [label.objectName() or label.text() for label in labels if not _selectable(label)]
    assert missing == []


def test_token_text_is_never_in_the_settings_file_after_remove(qtbot, settings_path, secrets, backend):
    tab = _tab(qtbot, settings_path, secrets)
    _field(tab, "settings-token-input").setText(FAKE_TOKEN)
    _button(tab, "settings-save-token").click()
    _button(tab, "settings-remove-token").click()

    assert get_token(store=secrets) is None
    assert backend.snapshot() == {}
    assert not any(FAKE_TOKEN.encode() in p.read_bytes() for p in Path(settings_path).parent.rglob("*") if p.is_file())
