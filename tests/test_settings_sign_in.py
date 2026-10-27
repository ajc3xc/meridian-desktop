"""Tests for the Settings tab's browser sign-in (meridian_desktop/ui/settings_tab.py), with pytest-qt.

The three steps are fakes, the keychain is in memory, and no browser opens. FAKE_TOKEN and FAKE_DEVICE are obviously fake
values, so a leak check can search for them. Offscreen Qt, from conftest.
"""

from __future__ import annotations

import threading

import pytest
from PySide6.QtWidgets import QGridLayout, QLabel, QPushButton

from _fakes import MemoryKeyring
from meridian_desktop import hosted_sign_in as sign
from meridian_desktop.hosted_client import HostedUnreachable, UNREACHABLE_TEXT
from meridian_desktop.hosted_sign_in import DeviceSignIn, SignInResult
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.token_store import get_token
from meridian_desktop.ui.settings_tab import (
    SIGN_IN_BUTTON_TEXT,
    SIGN_IN_OUTCOME_TEXT,
    SIGN_IN_SAVED_TEXT,
    SIGN_IN_STOP_TEXT,
    SIGN_IN_UNEXPECTED_TEXT,
    TOKEN_SET_TEXT,
    HostedSignIn,
    SettingsTab,
)

FAKE_TOKEN = "sk_meridian_fake-sign-in-tab-0001"
FAKE_DEVICE = "device-fake-sign-in-tab-0001"
PAGE = "https://hosted.test/activate?code=WDJB-MJHT"
DEVICE = DeviceSignIn(
    device_code=FAKE_DEVICE,
    user_code="WDJB-MJHT",
    page_url=PAGE,
    interval=5.0,
    expires_in=300.0,
)


def _steps(
    *,
    outcome: str = sign.APPROVED,
    token: str | None = FAKE_TOKEN,
    opened: bool = True,
    start_error: BaseException | None = None,
    gate: threading.Event | None = None,
    urls: list | None = None,
):
    """Three fake steps. ``gate`` holds the poll until the test sets it. ``urls`` records the URL each start was given."""
    opened_urls: list[str] = []

    def start(url: str) -> DeviceSignIn:
        if urls is not None:
            urls.append(url)
        if start_error is not None:
            raise start_error
        return DEVICE

    def poll(url: str, device: DeviceSignIn, *, should_stop, sleep) -> SignInResult:
        if gate is not None:
            gate.wait(5)
        return SignInResult(outcome, token)

    def open_page(url: str) -> bool:
        opened_urls.append(url)
        return opened

    return HostedSignIn(start=start, poll=poll, open_page=open_page), opened_urls


def _tab(qtbot, settings_path, secrets, steps: HostedSignIn) -> SettingsTab:
    tab = SettingsTab(
        SettingsStore(settings_path),
        secrets=secrets,
        list_hosted=lambda url, token: [],
        hosted_sign_in_steps=steps,
    )
    qtbot.addWidget(tab)
    return tab


@pytest.fixture
def secrets():
    return KeyringSecretStore(backend=MemoryKeyring())


def _button(tab) -> QPushButton:
    return tab.findChild(QPushButton, "settings-sign-in")


def _message(tab) -> str:
    return tab.findChild(QLabel, "settings-sign-in-message").text()


def test_a_sign_in_shows_the_code_opens_the_page_and_saves_the_approved_token(qtbot, settings_path, secrets):
    steps, opened = _steps()
    tab = _tab(qtbot, settings_path, secrets, steps)

    _button(tab).click()

    qtbot.waitUntil(lambda: get_token(store=secrets) == FAKE_TOKEN, timeout=5000)
    qtbot.waitUntil(lambda: _message(tab) == SIGN_IN_SAVED_TEXT, timeout=5000)
    assert opened == [PAGE], "the page for the code opens in the browser"
    assert tab.findChild(QLabel, "settings-token-status").text() == TOKEN_SET_TEXT
    assert _button(tab).text() == SIGN_IN_BUTTON_TEXT and _button(tab).isEnabled()
    assert FAKE_TOKEN not in _message(tab), "the token is never shown"


def test_the_code_is_shown_before_the_approval_arrives(qtbot, settings_path, secrets):
    gate = threading.Event()
    steps, _opened = _steps(gate=gate)
    tab = _tab(qtbot, settings_path, secrets, steps)

    _button(tab).click()

    qtbot.waitUntil(lambda: "WDJB-MJHT" in _message(tab), timeout=5000)
    assert _button(tab).text() == SIGN_IN_STOP_TEXT, "the button reads Stop waiting while the wait runs"
    assert get_token(store=secrets) is None, "nothing is saved before the owner approves"
    gate.set()
    qtbot.waitUntil(lambda: get_token(store=secrets) == FAKE_TOKEN, timeout=5000)


def test_a_browser_that_does_not_open_leaves_the_address_on_screen(qtbot, settings_path, secrets):
    gate = threading.Event()
    steps, _opened = _steps(opened=False, gate=gate)
    tab = _tab(qtbot, settings_path, secrets, steps)

    _button(tab).click()

    qtbot.waitUntil(lambda: "did not open" in _message(tab), timeout=5000)
    assert PAGE in _message(tab), "the owner can open the page by hand"
    gate.set()
    qtbot.waitUntil(lambda: get_token(store=secrets) == FAKE_TOKEN, timeout=5000)


@pytest.mark.parametrize(
    "outcome",
    [sign.DENIED, sign.EXPIRED, sign.STOPPED, sign.UNREACHABLE, sign.BAD_REPLY],
)
def test_a_sign_in_that_does_not_approve_saves_nothing_and_says_so(qtbot, settings_path, secrets, outcome):
    steps, _opened = _steps(outcome=outcome, token=None)
    tab = _tab(qtbot, settings_path, secrets, steps)

    _button(tab).click()

    qtbot.waitUntil(lambda: _message(tab) == SIGN_IN_OUTCOME_TEXT[outcome], timeout=5000)
    assert get_token(store=secrets) is None
    assert _button(tab).text() == SIGN_IN_BUTTON_TEXT and _button(tab).isEnabled()


def test_a_start_that_is_refused_shows_its_plain_words_and_saves_nothing(qtbot, settings_path, secrets):
    steps, _opened = _steps(start_error=HostedUnreachable())
    tab = _tab(qtbot, settings_path, secrets, steps)

    _button(tab).click()

    qtbot.waitUntil(lambda: _message(tab) == f"{UNREACHABLE_TEXT} Nothing was saved.", timeout=5000)
    assert get_token(store=secrets) is None


def test_an_unexpected_error_is_named_by_its_class_and_its_text_is_not_shown(qtbot, settings_path, secrets):
    steps, _opened = _steps(start_error=RuntimeError("sk_meridian_leak-check-0001"))
    tab = _tab(qtbot, settings_path, secrets, steps)

    _button(tab).click()

    qtbot.waitUntil(lambda: _message(tab) == SIGN_IN_UNEXPECTED_TEXT.format(name="RuntimeError"), timeout=5000)
    assert "leak-check" not in _message(tab)


def test_stop_waiting_ends_the_wait_and_the_button_reads_sign_in_again(qtbot, settings_path, secrets):
    def poll(url, device, *, should_stop, sleep):
        # Bounded, so a stop that never arrives fails the test instead of leaving a job running into the next test.
        for _ in range(1000):
            if should_stop():
                break
            sleep(0.01)
        return SignInResult(sign.STOPPED)

    steps = HostedSignIn(start=lambda url: DEVICE, poll=poll, open_page=lambda url: True)
    tab = _tab(qtbot, settings_path, secrets, steps)

    _button(tab).click()
    # Stop waiting is offered once the code has arrived; a press before that is ignored, as the next test checks.
    qtbot.waitUntil(lambda: _button(tab).isEnabled() and _button(tab).text() == SIGN_IN_STOP_TEXT, timeout=5000)
    _button(tab).click()

    qtbot.waitUntil(lambda: _message(tab) == SIGN_IN_OUTCOME_TEXT[sign.STOPPED], timeout=5000)
    assert _button(tab).text() == SIGN_IN_BUTTON_TEXT
    assert get_token(store=secrets) is None


def test_the_sign_in_uses_the_hosted_url_that_is_saved(qtbot, settings_path, secrets):
    SettingsStore(settings_path).save(Settings(hosted_url="https://other.test"))
    urls: list[str] = []
    steps, _opened = _steps(urls=urls)
    tab = _tab(qtbot, settings_path, secrets, steps)

    _button(tab).click()

    qtbot.waitUntil(lambda: get_token(store=secrets) == FAKE_TOKEN, timeout=5000)
    assert urls == ["https://other.test"]


def test_a_second_click_while_the_code_is_being_requested_does_nothing(qtbot, settings_path, secrets):
    gate = threading.Event()

    def start(url):
        gate.wait(5)
        return DEVICE

    steps = HostedSignIn(start=start, poll=lambda url, device, *, should_stop, sleep: SignInResult(sign.APPROVED, FAKE_TOKEN), open_page=lambda url: True)
    tab = _tab(qtbot, settings_path, secrets, steps)

    _button(tab).click()
    assert not _button(tab).isEnabled(), "the button is off while the code is being requested"
    _button(tab).click()
    gate.set()

    qtbot.waitUntil(lambda: get_token(store=secrets) == FAKE_TOKEN, timeout=5000)
    assert _message(tab) == SIGN_IN_SAVED_TEXT, "the second click did not cancel the sign-in"


def test_no_two_widgets_in_the_connection_group_share_a_grid_cell(qtbot, settings_path, secrets):
    steps, _opened = _steps()
    tab = _tab(qtbot, settings_path, secrets, steps)
    grids = [layout for layout in tab.findChildren(QGridLayout) if layout.indexOf(_button(tab)) >= 0]
    assert len(grids) == 1
    grid = grids[0]

    occupied: dict[tuple[int, int], str] = {}
    for index in range(grid.count()):
        widget = grid.itemAt(index).widget()
        if widget is None:
            continue
        row, column, row_span, column_span = grid.getItemPosition(index)
        name = widget.objectName() or type(widget).__name__
        for r in range(row, row + row_span):
            for c in range(column, column + column_span):
                assert (r, c) not in occupied, f"{name} shares cell {(r, c)} with {occupied.get((r, c))}"
                occupied[(r, c)] = name
