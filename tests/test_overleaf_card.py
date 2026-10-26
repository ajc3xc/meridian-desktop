"""The Overleaf (LaTeX) card and the two LaTeX engine clients behind it.

Offscreen Qt, pytest-qt, fakes only. No test opens a socket, starts a process or reads the real settings. The LaTeX engine on
127.0.0.1:8471 is never reached: each transport, connector, launcher and engine call here is a fake that records its calls.
"""

import dataclasses
import json
import re
import subprocess
import threading

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QCheckBox, QFrame, QLabel, QMessageBox, QPushButton

from meridian_desktop import local_client, overleaf_client, overleaf_engine
from meridian_desktop.home_status import (
    LOGIN_ALREADY_OPEN_TEXT,
    LOGIN_ENGINE_DOWN_TEXT,
    LOGIN_NO_CHROME_TEXT,
    LOGIN_OPENED_TEXT,
    START_CANCELLED_TEXT,
    START_RUNNING_TEXT,
    START_TITLE,
)
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.ui import home_tab
from meridian_desktop.ui.home_tab import CHECK_INTERVAL_MS, HomeDeps, HomeTab, OverleafDeps
from meridian_desktop.zotero_client import API_NOT_RUNNING, ApiCheck, ZoteroStatus

LOOPBACK = "http://127.0.0.1:8471"
# A string that must never reach a card's text: it stands in for cookie, token and path text.
SENTINEL = "SENTINEL-cookie-7f3a9c"
CHECKING = "Checking..."
TITLE = "Overleaf (LaTeX)"
QUESTION = "Start the LaTeX engine on 127.0.0.1:8471 in the background?"
HEADLINE_TIME = re.compile(r"^Signed in \(checked \d{2}:\d{2}:\d{2}\)$")


# ----------------------------------------------------------------- fakes

class FakeTransport:
    """Records each request and answers with one fixed reply, or raises one fixed error. No socket is opened."""

    def __init__(self, reply=None, error=None):
        self.reply = reply
        self.error = error
        self.calls = []

    def __call__(self, method, url, headers, timeout):
        self.calls.append((method, url, dict(headers), timeout))
        if self.error is not None:
            raise self.error
        return self.reply


def _reply(status, payload=None, raw=None):
    body = raw if raw is not None else json.dumps(payload).encode("utf-8")
    return overleaf_client.Reply(status, body)


class FakeEngine:
    """Stands in for the LaTeX engine calls the card makes. Each call is counted, and nothing opens a socket.

    ``up`` is whether the port answers. ``start_brings_up`` makes the port answer once the starter has run. ``folder`` is
    what the folder check returns. ``session`` and ``login`` are the answers, or exceptions to raise. ``session_gate``, when
    set, holds a session answer until the test sets it.
    """

    def __init__(
        self,
        *,
        up=False,
        start_brings_up=False,
        folder=None,
        session=None,
        login=None,
        start=None,
        start_error=None,
        listening_error=None,
        session_error=None,
        session_gate=None,
        start_gate=None,
    ):
        self.up = up
        self.start_brings_up = start_brings_up
        self.folder_answer = folder
        self.session_answer = session
        self.login_answer = login
        self.start_answer = start if start is not None else overleaf_engine.EngineStart(True)
        self.start_error = start_error
        self.listening_error = listening_error
        self.session_error = session_error
        self.session_gate = session_gate
        self.start_gate = start_gate
        self.listening_calls = 0
        self.folder_calls = 0
        self.session_calls = []
        self.login_calls = []
        self.starts = []

    def listening(self, url):
        self.listening_calls += 1
        if self.listening_error is not None:
            raise self.listening_error
        return self.up

    def folder(self, settings=None):
        self.folder_calls += 1
        return self.folder_answer

    def start(self, folder):
        self.starts.append(folder)
        if self.start_error is not None:
            raise self.start_error
        if self.start_gate is not None:
            self.start_gate.wait(5)
        if self.start_brings_up:
            self.up = True
        return self.start_answer

    def session(self, url):
        self.session_calls.append(url)
        if self.session_gate is not None:
            self.session_gate.wait(5)
        if self.session_error is not None:
            raise self.session_error
        return self.session_answer

    def login(self, url):
        self.login_calls.append(url)
        return self.login_answer


class _Zotero:
    """The Zotero card's status read, with Zotero not installed. The card is not under test here."""

    def status(self):
        return ZoteroStatus(exe=None, process_running=False, api=ApiCheck(API_NOT_RUNNING, "not running"))


def _deps(tmp_path, engine, *, confirm=None, wait=0.5, choose=None):
    """Home's dependencies with the engine fakes. ``confirm`` defaults to the real ask_yes_no, so No stays the default.
    ``choose`` is the folder picker a test injects; without one the card never opens a picker."""
    picker = {} if choose is None else {"choose_folder": choose}
    overleaf = OverleafDeps(
        listening=engine.listening,
        folder=engine.folder,
        start=engine.start,
        session=engine.session,
        login=engine.login,
        confirm=confirm if confirm is not None else home_tab.ask_yes_no,
        wait_seconds=wait,
        **picker,
    )
    return HomeDeps(
        store=SettingsStore(tmp_path / "settings.json"),
        probe=lambda url: local_client.HEALTH_NOT_RUNNING,
        tunnel=None,
        actions=None,
        zotero=_Zotero(),
        tools=None,
        updates=None,
        current_version="0.2.0",
        overleaf=overleaf,
    )


def _shown(qtbot, deps):
    tab = HomeTab(deps)
    qtbot.addWidget(tab)
    tab.show()
    qtbot.waitExposed(tab)
    return tab


def _card(tab):
    card = tab.findChild(QFrame, "home-card-overleaf")
    assert card is not None
    return card


def _label(tab, name):
    return _card(tab).findChild(QLabel, name).text()


def _headline(tab):
    return _label(tab, "home-overleaf-headline")


def _note(tab):
    return _label(tab, "home-overleaf-note")


def _detail(tab):
    return _label(tab, "home-overleaf-detail")


def _button(tab, name):
    button = tab.findChild(QPushButton, name)
    assert button is not None, name
    return button


def _card_texts(tab):
    """Every piece of text the card shows: its labels, the text block, and its buttons with their tooltips."""
    card = _card(tab)
    texts = [label.text() for label in card.findChildren(QLabel)]
    texts += [button.text() + " " + button.toolTip() for button in card.findChildren(QPushButton)]
    texts += [box.text() for box in card.findChildren(QCheckBox)]
    return "\n".join(texts)


def _wait_engine(qtbot, tab):
    qtbot.waitUntil(lambda: _headline(tab) != CHECKING, timeout=5000)


def _check_and_wait(qtbot, tab, expected_headline):
    _button(tab, "home-overleaf-check").click()
    if expected_headline.endswith(" (checked "):
        qtbot.waitUntil(lambda: HEADLINE_TIME.match(_headline(tab)) is not None, timeout=5000)
    else:
        qtbot.waitUntil(lambda: _headline(tab) == expected_headline, timeout=5000)


# ----------------------------------------------------------------- the client: the loopback guard

@pytest.mark.parametrize(
    "url",
    [
        "http://example.com:8471",
        "https://127.0.0.1:8471",
        "http://127.0.0.1.example.com:8471",
        "http://user:secret@127.0.0.1:8471",
        "http://192.168.1.20:8471",
        "",
        "not a url",
    ],
)
def test_a_non_loopback_address_sends_nothing_for_the_session_or_the_login(url):
    transport = FakeTransport(reply=_reply(200, {"state": "logged_in"}))
    session = overleaf_client.session_status(url, transport=transport)
    login = overleaf_client.start_login(url, transport=transport)
    assert transport.calls == []
    assert session.state == overleaf_client.STATE_ERROR
    assert login == overleaf_client.LoginStatus(False, overleaf_client.REASON_ERROR)
    assert "secret" not in session.error


# ----------------------------------------------------------------- the client: the session check

def test_the_session_check_sends_only_the_client_header_and_waits_forty_seconds():
    transport = FakeTransport(reply=_reply(200, {"state": "logged_in", "chromeFound": True, "savedAt": "2026-10-20"}))
    result = overleaf_client.session_status(LOOPBACK, transport=transport)
    assert len(transport.calls) == 1
    method, url, headers, timeout = transport.calls[0]
    assert method == "GET"
    assert url == "http://127.0.0.1:8471/overleaf/session"
    assert list(headers) == [overleaf_client.CLIENT_HEADER]
    assert headers[overleaf_client.CLIENT_HEADER].strip()
    assert timeout == 40.0 == overleaf_client.SESSION_TIMEOUT
    assert result == overleaf_client.SessionStatus("logged_in", chrome_found=True)


def test_no_authorization_cookie_or_token_header_is_ever_sent():
    transport = FakeTransport(reply=_reply(200, {"state": "missing", "chromeFound": False}))
    overleaf_client.session_status(LOOPBACK, transport=transport)
    overleaf_client.start_login(LOOPBACK, transport=FakeTransport(reply=_reply(200, {"started": True})))
    sent = [headers for _method, _url, headers, _timeout in transport.calls]
    for headers in sent:
        for name in headers:
            lowered = name.lower()
            assert lowered not in ("authorization", "cookie", "proxy-authorization", "x-api-key"), name
            assert "token" not in lowered and "cookie" not in lowered, name


def test_a_refused_connection_means_the_engine_is_not_running():
    transport = FakeTransport(error=local_client.LocalClientError("not_running", "refused"))
    result = overleaf_client.session_status(LOOPBACK, transport=transport)
    assert result == overleaf_client.SessionStatus(overleaf_client.STATE_ENGINE_NOT_RUNNING)


def test_a_timeout_reads_as_the_unknown_session_state():
    transport = FakeTransport(error=local_client.LocalClientError("timeout", "slow"))
    result = overleaf_client.session_status(LOOPBACK, transport=transport)
    assert result == overleaf_client.SessionStatus(overleaf_client.STATE_UNKNOWN)


def test_the_transport_maps_a_refused_connection_to_not_running_and_a_timeout_to_timeout():
    assert overleaf_client._transport_error(ConnectionRefusedError(10061, "refused")).code == "not_running"
    assert overleaf_client._transport_error(TimeoutError("slow")).code == "timeout"
    assert overleaf_client._transport_error(OSError("other")).code == "unreachable"


def test_a_403_is_a_fixed_error_and_its_body_is_never_shown():
    transport = FakeTransport(reply=_reply(403, raw=f'{{"error": "{SENTINEL}"}}'.encode("utf-8")))
    result = overleaf_client.session_status(LOOPBACK, transport=transport)
    assert result == overleaf_client.SessionStatus(overleaf_client.STATE_ERROR, error=overleaf_client.FORBIDDEN_TEXT)
    assert SENTINEL not in repr(result)


@pytest.mark.parametrize(
    "reply",
    [
        _reply(200, raw=b"not json at all"),
        _reply(200, {"state": "weird"}),
        _reply(200, {"chromeFound": True}),
        _reply(500, {"error": "boom"}),
        _reply(302, {"state": "logged_in"}),
    ],
)
def test_an_unreadable_or_unexpected_answer_is_an_error_with_fixed_text(reply):
    result = overleaf_client.session_status(LOOPBACK, transport=FakeTransport(reply=reply))
    assert result.state == overleaf_client.STATE_ERROR
    assert result.error


def test_a_non_boolean_chrome_flag_reads_as_not_said():
    result = overleaf_client.session_status(
        LOOPBACK, transport=FakeTransport(reply=_reply(200, {"state": "missing", "chromeFound": "yes"}))
    )
    assert result == overleaf_client.SessionStatus("missing", chrome_found=None)


def test_the_session_answer_keeps_no_cookie_text_even_when_the_body_carries_it():
    body = {"state": "expired", "chromeFound": True, "cookie": SENTINEL, "token": SENTINEL, "path": SENTINEL}
    result = overleaf_client.session_status(LOOPBACK, transport=FakeTransport(reply=_reply(200, body)))
    assert result == overleaf_client.SessionStatus("expired", chrome_found=True)
    assert SENTINEL not in repr(result)


# ----------------------------------------------------------------- the client: the login

def test_the_login_is_a_post_with_only_the_client_header_and_a_ten_second_timeout():
    transport = FakeTransport(reply=_reply(200, {"started": True}))
    result = overleaf_client.start_login(LOOPBACK, transport=transport)
    assert result == overleaf_client.LoginStatus(True)
    method, url, headers, timeout = transport.calls[0]
    assert method == "POST"
    assert url == "http://127.0.0.1:8471/overleaf/login"
    assert list(headers) == [overleaf_client.CLIENT_HEADER]
    assert timeout == 10.0 == overleaf_client.LOGIN_TIMEOUT


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (_reply(200, {"started": False, "reason": "already_open"}), overleaf_client.LoginStatus(False, "already_open")),
        (_reply(200, {"started": False, "reason": "chrome_missing"}), overleaf_client.LoginStatus(False, "chrome_missing")),
        (_reply(200, {"started": False, "reason": "invented"}), overleaf_client.LoginStatus(False, "error")),
        (_reply(200, {"started": "yes"}), overleaf_client.LoginStatus(False, "error")),
        (_reply(200, raw=b"not json"), overleaf_client.LoginStatus(False, "error")),
        (_reply(403, {"started": True}), overleaf_client.LoginStatus(False, "error")),
    ],
)
def test_the_login_answers_map_to_their_reasons(reply, expected):
    assert overleaf_client.start_login(LOOPBACK, transport=FakeTransport(reply=reply)) == expected


def test_a_refused_login_means_the_engine_is_not_running_and_a_timeout_is_a_timeout():
    refused = FakeTransport(error=local_client.LocalClientError("not_running", "refused"))
    assert overleaf_client.start_login(LOOPBACK, transport=refused) == overleaf_client.LoginStatus(False, "engine_not_running")
    slow = FakeTransport(error=local_client.LocalClientError("timeout", "slow"))
    assert overleaf_client.start_login(LOOPBACK, transport=slow) == overleaf_client.LoginStatus(False, "timeout")


# ----------------------------------------------------------------- the engine port and starter

def test_the_port_check_refuses_a_non_loopback_address_before_it_connects():
    calls = []
    result = overleaf_engine.engine_listening(
        "http://example.com:8471", connector=lambda host, port, timeout: calls.append((host, port))
    )
    assert result is False
    assert calls == []


def test_the_port_check_without_a_port_connects_to_nothing():
    calls = []
    assert overleaf_engine.engine_listening("http://127.0.0.1", connector=lambda *a: calls.append(a)) is False
    assert calls == []


def test_the_port_check_is_true_only_when_the_connection_opens():
    calls = []
    assert overleaf_engine.engine_listening(LOOPBACK, connector=lambda host, port, timeout: calls.append((host, port, timeout)))
    assert calls == [("127.0.0.1", 8471, overleaf_engine.PROBE_TIMEOUT)]

    def refused(host, port, timeout):
        raise ConnectionRefusedError(10061, "refused")

    assert overleaf_engine.engine_listening(LOOPBACK, connector=refused) is False


def test_start_engine_does_nothing_off_windows(tmp_path):
    (tmp_path / overleaf_engine.STARTER_NAME).write_text("", encoding="utf-8")
    launches = []
    result = overleaf_engine.start_engine(tmp_path, platform="linux", launcher=lambda *a, **k: launches.append((a, k)))
    assert result == overleaf_engine.EngineStart(False, overleaf_engine.WINDOWS_ONLY_TEXT)
    assert launches == []


def test_start_engine_without_a_starter_file_starts_nothing(tmp_path):
    launches = []
    result = overleaf_engine.start_engine(tmp_path, platform="win32", launcher=lambda *a, **k: launches.append(a))
    assert result == overleaf_engine.EngineStart(False, overleaf_engine.NO_STARTER_TEXT)
    assert launches == []


def test_start_engine_runs_the_starter_detached_from_its_folder_and_returns_at_once(tmp_path):
    (tmp_path / overleaf_engine.STARTER_NAME).write_text("", encoding="utf-8")
    launches = []

    def launcher(command, **kwargs):
        launches.append((command, kwargs))
        return object()

    result = overleaf_engine.start_engine(tmp_path, platform="win32", launcher=launcher)
    assert result == overleaf_engine.EngineStart(True)
    assert len(launches) == 1
    command, kwargs = launches[0]
    assert command[0].lower().endswith("wscript.exe")
    assert command[1:] == ["//B", "//nologo", "start_server_hidden.vbs"]
    assert kwargs["cwd"] == str(tmp_path)
    assert kwargs["creationflags"] == overleaf_engine._DETACHED
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.DEVNULL
    assert kwargs["close_fds"] is True


def test_a_starter_that_cannot_launch_reports_the_class_name_only(tmp_path):
    (tmp_path / overleaf_engine.STARTER_NAME).write_text("", encoding="utf-8")

    def launcher(command, **kwargs):
        raise PermissionError(SENTINEL)

    result = overleaf_engine.start_engine(tmp_path, platform="win32", launcher=launcher)
    assert result == overleaf_engine.EngineStart(False, overleaf_engine.START_FAILED_TEXT.format("PermissionError"))
    assert SENTINEL not in result.reason


# ----------------------------------------------------------------- the card: what shows when

def test_the_card_is_titled_and_its_buttons_are_named_and_say_what_they_do(qtbot, tmp_path):
    tab = _shown(qtbot, _deps(tmp_path, FakeEngine()))
    assert _card(tab).findChild(QLabel, "home-overleaf-title").text() == TITLE
    check = _button(tab, "home-overleaf-check")
    assert check.text() == "Check"
    assert "sends your saved session to overleaf once" in check.toolTip().lower()
    assert _button(tab, "home-overleaf-login").text() == "Log in"
    assert _button(tab, "home-overleaf-start-engine").text() == "Start engine"


def test_showing_home_reads_only_the_engine_port_and_never_asks_overleaf(qtbot, tmp_path):
    engine = FakeEngine(up=False, folder=None)
    tab = _shown(qtbot, _deps(tmp_path, engine))
    _wait_engine(qtbot, tab)
    assert engine.listening_calls >= 1
    assert engine.session_calls == []
    assert engine.login_calls == []
    assert engine.starts == []
    assert _headline(tab) == "Engine not running"


def test_the_home_timer_and_check_now_never_ask_overleaf(qtbot, tmp_path):
    engine = FakeEngine(up=True, folder=tmp_path)
    tab = _shown(qtbot, _deps(tmp_path, engine))
    _wait_engine(qtbot, tab)
    timers = [timer for timer in tab.findChildren(QTimer) if timer.interval() == CHECK_INTERVAL_MS]
    assert len(timers) == 1
    # The five-minute timer reads the cards, but not the engine's port and never the Overleaf session.
    before = engine.listening_calls
    timers[0].timeout.emit()
    qtbot.wait(100)
    assert engine.listening_calls == before
    assert engine.session_calls == []
    # Check now looks at the engine's port once more, and still asks nothing of Overleaf.
    _button(tab, "home-check-now").click()
    qtbot.waitUntil(lambda: engine.listening_calls == before + 1, timeout=5000)
    qtbot.wait(50)
    assert engine.session_calls == []
    assert engine.login_calls == []


def test_a_running_engine_with_no_check_yet_says_the_session_is_not_checked(qtbot, tmp_path):
    engine = FakeEngine(up=True, folder=None)
    tab = _shown(qtbot, _deps(tmp_path, engine))
    qtbot.waitUntil(lambda: _headline(tab) == "Engine running, session not checked yet", timeout=5000)
    assert engine.session_calls == []


def test_check_asks_the_engine_exactly_once_per_click(qtbot, tmp_path):
    engine = FakeEngine(up=True, session=overleaf_client.SessionStatus("logged_in", chrome_found=True))
    tab = _shown(qtbot, _deps(tmp_path, engine))
    _wait_engine(qtbot, tab)
    _check_and_wait(qtbot, tab, "Signed in (checked ")
    assert engine.session_calls == [LOOPBACK]
    _check_and_wait(qtbot, tab, "Signed in (checked ")
    assert engine.session_calls == [LOOPBACK, LOOPBACK]


def test_a_second_check_is_not_started_while_one_runs(qtbot, tmp_path):
    gate = threading.Event()
    engine = FakeEngine(up=True, session=overleaf_client.SessionStatus("logged_in", chrome_found=True), session_gate=gate)
    tab = _shown(qtbot, _deps(tmp_path, engine))
    _wait_engine(qtbot, tab)
    check = _button(tab, "home-overleaf-check")
    check.click()
    # The first check has reached the engine (the pool thread recorded the call) and is still held by the gate.
    qtbot.waitUntil(lambda: engine.session_calls == [LOOPBACK] and not check.isEnabled(), timeout=5000)
    assert _headline(tab) == CHECKING
    check.click()
    qtbot.wait(50)
    assert engine.session_calls == [LOOPBACK]
    gate.set()
    qtbot.waitUntil(lambda: HEADLINE_TIME.match(_headline(tab)) is not None, timeout=5000)
    assert engine.session_calls == [LOOPBACK]


@pytest.mark.parametrize(
    ("session", "headline"),
    [
        (overleaf_client.SessionStatus("logged_in", chrome_found=True), "Signed in (checked "),
        (overleaf_client.SessionStatus("expired", chrome_found=True), "Session expired: click Log in"),
        (overleaf_client.SessionStatus("missing", chrome_found=True), "No saved session: click Log in"),
        (overleaf_client.SessionStatus("unknown"), "Could not reach Overleaf: try again later"),
        (overleaf_client.SessionStatus("missing", chrome_found=False), "Chrome not found: install Chrome to log in"),
        (overleaf_client.SessionStatus("expired", chrome_found=False), "Chrome not found: install Chrome to log in"),
        (overleaf_client.SessionStatus("engine_not_running"), "Engine not running"),
    ],
)
def test_each_check_answer_shows_its_own_words(qtbot, tmp_path, session, headline):
    engine = FakeEngine(up=True, session=session)
    tab = _shown(qtbot, _deps(tmp_path, engine))
    _wait_engine(qtbot, tab)
    _check_and_wait(qtbot, tab, headline)
    if headline.endswith(" (checked "):
        assert HEADLINE_TIME.match(_headline(tab))
    else:
        assert _headline(tab) == headline
    assert engine.session_calls == [LOOPBACK]


def test_a_check_that_finds_the_engine_down_shows_engine_not_running(qtbot, tmp_path):
    engine = FakeEngine(up=True, session=overleaf_client.SessionStatus("engine_not_running"))
    tab = _shown(qtbot, _deps(tmp_path, engine))
    _wait_engine(qtbot, tab)
    _check_and_wait(qtbot, tab, "Engine not running")
    assert _detail(tab).startswith("Nothing listens on port 8471.")


def test_a_check_that_raises_shows_only_the_class_name(qtbot, tmp_path):
    engine = FakeEngine(up=True, session_error=RuntimeError(SENTINEL))
    tab = _shown(qtbot, _deps(tmp_path, engine))
    _wait_engine(qtbot, tab)
    _check_and_wait(qtbot, tab, "Could not check")
    assert "(RuntimeError)" in _detail(tab)
    assert SENTINEL not in _card_texts(tab)


def test_a_probe_that_raises_shows_could_not_check(qtbot, tmp_path):
    engine = FakeEngine(listening_error=RuntimeError(SENTINEL))
    tab = _shown(qtbot, _deps(tmp_path, engine))
    qtbot.waitUntil(lambda: _headline(tab) == "Could not check", timeout=5000)
    assert SENTINEL not in _card_texts(tab)


# ----------------------------------------------------------------- the card: Log in

@pytest.mark.parametrize(
    ("login", "note"),
    [
        (overleaf_client.LoginStatus(True), LOGIN_OPENED_TEXT),
        (overleaf_client.LoginStatus(False, "already_open"), LOGIN_ALREADY_OPEN_TEXT),
        (overleaf_client.LoginStatus(False, "chrome_missing"), LOGIN_NO_CHROME_TEXT),
        (overleaf_client.LoginStatus(False, "engine_not_running"), LOGIN_ENGINE_DOWN_TEXT),
    ],
)
def test_log_in_calls_the_engine_once_and_shows_its_words(qtbot, tmp_path, login, note):
    engine = FakeEngine(up=True, login=login)
    tab = _shown(qtbot, _deps(tmp_path, engine))
    _wait_engine(qtbot, tab)
    _button(tab, "home-overleaf-login").click()
    qtbot.waitUntil(lambda: _note(tab) == note, timeout=5000)
    assert engine.login_calls == [LOOPBACK]
    assert engine.session_calls == []


def test_log_in_shows_a_timeout_and_a_refusal_plainly(qtbot, tmp_path):
    engine = FakeEngine(up=True, login=overleaf_client.LoginStatus(False, "timeout"))
    tab = _shown(qtbot, _deps(tmp_path, engine))
    _wait_engine(qtbot, tab)
    _button(tab, "home-overleaf-login").click()
    qtbot.waitUntil(lambda: "did not answer" in _note(tab), timeout=5000)
    assert engine.login_calls == [LOOPBACK]


# ----------------------------------------------------------------- the card: Start engine

def test_start_engine_is_offered_only_when_the_engine_is_down_and_its_folder_is_known(qtbot, tmp_path):
    engine = FakeEngine(up=False, folder=tmp_path)
    tab = _shown(qtbot, _deps(tmp_path, engine))
    _wait_engine(qtbot, tab)
    assert not _button(tab, "home-overleaf-start-engine").isHidden()


def test_start_engine_is_not_offered_when_the_engine_is_running(qtbot, tmp_path):
    engine = FakeEngine(up=True, folder=tmp_path)
    tab = _shown(qtbot, _deps(tmp_path, engine))
    _wait_engine(qtbot, tab)
    assert _button(tab, "home-overleaf-start-engine").isHidden()


def test_engine_folder_missing_means_start_engine_is_not_offered(qtbot, tmp_path):
    engine = FakeEngine(up=False, folder=None)
    tab = _shown(qtbot, _deps(tmp_path, engine))
    _wait_engine(qtbot, tab)
    assert _headline(tab) == "Engine not running"
    assert _button(tab, "home-overleaf-start-engine").isHidden()


def test_start_engine_asks_first_and_a_no_starts_nothing(qtbot, tmp_path):
    asked = []

    def confirm(parent, title, text):
        asked.append((title, text))
        return False

    engine = FakeEngine(up=False, folder=tmp_path)
    tab = _shown(qtbot, _deps(tmp_path, engine, confirm=confirm))
    _wait_engine(qtbot, tab)
    _button(tab, "home-overleaf-start-engine").click()
    qtbot.waitUntil(lambda: _note(tab) == START_CANCELLED_TEXT, timeout=5000)
    assert asked == [(START_TITLE, QUESTION)]
    assert engine.starts == []


def test_the_question_defaults_to_no_and_escape_is_no(qtbot, tmp_path, monkeypatch):
    seen = {}

    def fake_exec(box, *args, **kwargs):
        seen["default"] = box.defaultButton().text()
        seen["escape"] = box.escapeButton().text()
        seen["text"] = box.text()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    # The conftest stand-in refuses modal dialogs, so this test replaces that refusal for its own run only.
    engine = FakeEngine(up=False, folder=tmp_path)
    tab = _shown(qtbot, _deps(tmp_path, engine))
    _wait_engine(qtbot, tab)
    _button(tab, "home-overleaf-start-engine").click()
    qtbot.waitUntil(lambda: _note(tab) == START_CANCELLED_TEXT, timeout=5000)
    assert seen == {"default": "No", "escape": "No", "text": QUESTION}
    assert engine.starts == []


def test_start_engine_with_a_yes_starts_the_starter_and_waits_for_the_port(qtbot, tmp_path):
    engine = FakeEngine(up=False, folder=tmp_path, start_brings_up=True)
    tab = _shown(qtbot, _deps(tmp_path, engine, confirm=lambda parent, title, text: True))
    _wait_engine(qtbot, tab)
    _button(tab, "home-overleaf-start-engine").click()
    qtbot.waitUntil(lambda: _note(tab) == START_RUNNING_TEXT, timeout=5000)
    assert engine.starts == [tmp_path]
    assert _headline(tab) == "Engine running, session not checked yet"
    assert _button(tab, "home-overleaf-start-engine").isHidden()


def test_start_engine_says_so_when_the_port_never_answers(qtbot, tmp_path):
    engine = FakeEngine(up=False, folder=tmp_path, start_brings_up=False)
    tab = _shown(qtbot, _deps(tmp_path, engine, confirm=lambda parent, title, text: True, wait=0.3))
    _wait_engine(qtbot, tab)
    _button(tab, "home-overleaf-start-engine").click()
    qtbot.waitUntil(lambda: "did not answer" in _note(tab), timeout=5000)
    assert engine.starts == [tmp_path]
    assert _headline(tab) == "Engine not running"


def test_start_engine_shows_the_reason_when_the_starter_did_not_run(qtbot, tmp_path):
    refusal = overleaf_engine.EngineStart(False, overleaf_engine.WINDOWS_ONLY_TEXT)
    engine = FakeEngine(up=False, folder=tmp_path, start=refusal)
    tab = _shown(qtbot, _deps(tmp_path, engine, confirm=lambda parent, title, text: True))
    _wait_engine(qtbot, tab)
    _button(tab, "home-overleaf-start-engine").click()
    qtbot.waitUntil(lambda: _note(tab) == overleaf_engine.WINDOWS_ONLY_TEXT, timeout=5000)
    assert engine.starts == [tmp_path]
    assert _headline(tab) == "Engine not running"


# ----------------------------------------------------------------- the card: nothing secret or local is shown

def _deps_with_real_session(tmp_path, engine, transport):
    """The fake engine, except that the session check is the real client, reading a fake transport's answer."""
    deps = _deps(tmp_path, engine)
    overleaf = dataclasses.replace(
        deps.overleaf,
        session=lambda url: overleaf_client.session_status(url, transport=transport),
    )
    return dataclasses.replace(deps, overleaf=overleaf)


def test_a_real_session_answer_shows_no_cookie_token_or_path_text(qtbot, tmp_path):
    body = {"state": "expired", "chromeFound": True, "cookie": SENTINEL, "token": SENTINEL, "savedAt": SENTINEL}
    transport = FakeTransport(reply=_reply(200, body))
    secret_folder = tmp_path / SENTINEL
    engine = FakeEngine(up=True, folder=secret_folder)
    tab = _shown(qtbot, _deps_with_real_session(tmp_path, engine, transport))
    _wait_engine(qtbot, tab)
    _check_and_wait(qtbot, tab, "Session expired: click Log in")
    assert len(transport.calls) == 1
    texts = _card_texts(tab)
    assert SENTINEL not in texts
    assert str(tmp_path) not in texts
    assert str(secret_folder) not in texts


def test_a_real_403_shows_the_fixed_error_and_no_body_text(qtbot, tmp_path):
    transport = FakeTransport(reply=_reply(403, raw=f'{{"error": "{SENTINEL}"}}'.encode("utf-8")))
    engine = FakeEngine(up=True)
    tab = _shown(qtbot, _deps_with_real_session(tmp_path, engine, transport))
    _wait_engine(qtbot, tab)
    _check_and_wait(qtbot, tab, "Could not check")
    assert _detail(tab) == overleaf_client.FORBIDDEN_TEXT
    assert SENTINEL not in _card_texts(tab)


def test_the_card_never_shows_the_engine_folder_path_before_start(qtbot, tmp_path):
    secret_folder = tmp_path / SENTINEL
    engine = FakeEngine(up=False, folder=secret_folder)
    tab = _shown(qtbot, _deps(tmp_path, engine))
    _wait_engine(qtbot, tab)
    assert str(secret_folder) not in _card_texts(tab)
    assert SENTINEL not in _card_texts(tab)


# ----------------------------------------------------------------- the card's wording, in plain functions

def test_the_login_and_start_words_come_from_the_engine_answer():
    from meridian_desktop.home_status import overleaf_login_text, overleaf_start_text

    assert overleaf_login_text(overleaf_client.LoginStatus(True)) == "Login window opened: sign in there, then click Check."
    assert overleaf_login_text(overleaf_client.LoginStatus(False, "already_open")) == "The login window is already open."
    assert overleaf_login_text(overleaf_client.LoginStatus(False, "chrome_missing")) == (
        "Chrome not found: install Chrome to log in."
    )
    assert overleaf_login_text(overleaf_client.LoginStatus(False, "engine_not_running")) == "Start the engine first."
    assert overleaf_start_text(True, "", 8.0) == START_RUNNING_TEXT
    assert overleaf_start_text(False, "", 8.0) == "The LaTeX engine did not answer within 8 seconds. Press Check now in a moment."
    assert overleaf_start_text(False, overleaf_engine.WINDOWS_ONLY_TEXT, 8.0) == overleaf_engine.WINDOWS_ONLY_TEXT


# ----------------------------------------------------------------- the engine folder: only the one the owner saved

def _folder_with_starter(tmp_path, name="engine"):
    folder = tmp_path / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / overleaf_engine.STARTER_NAME).write_text("", encoding="utf-8")
    return folder


def test_the_engine_folder_is_the_saved_folder_only_while_it_holds_the_starter(tmp_path):
    folder = _folder_with_starter(tmp_path)
    assert overleaf_engine.engine_folder(Settings(engine_folder=str(folder))) == folder


def test_the_engine_folder_is_none_when_none_is_saved_or_the_starter_is_missing(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert overleaf_engine.engine_folder(Settings()) is None
    assert overleaf_engine.engine_folder(Settings(engine_folder=str(empty))) is None
    assert overleaf_engine.engine_folder(Settings(engine_folder=str(tmp_path / "gone"))) is None


# ----------------------------------------------------------------- the card: the launch-time checks and the switches

# The words the owner reads on the card. Each is spelled out here, so a change to any of them is a visible change.
ENGINE_FOLDER_NOT_SET = "Engine folder not set: choose it to start the engine automatically"
STARTING = "Starting the engine..."
ENGINE_STARTED = "Engine started"
ENGINE_NOT_STARTED = "Engine did not start"
NO_STARTER = "That folder has no start_server_hidden.vbs"
FOLDER_SAVED = "Engine folder saved. Nothing was started."
START_BOX = "Start the engine when Meridian opens"
CHECK_BOX = "Check the Overleaf session when Meridian opens"
CHOOSE = "Choose engine folder"
SIGNED_IN = overleaf_client.SessionStatus("logged_in", chrome_found=True)


def _save_settings(tmp_path, **changes):
    """Save the settings file the card reads: the given fields, and every other field at its default."""
    SettingsStore(tmp_path / "settings.json").save(dataclasses.replace(Settings(), **changes))


def _saved(tmp_path):
    return SettingsStore(tmp_path / "settings.json").load().settings


def _launch_deps(tmp_path, engine, *, choose=None, wait=0.5):
    """Home's dependencies with the real folder rule: the engine folder is the one saved in settings, if it holds the starter."""
    deps = _deps(tmp_path, engine, choose=choose, wait=wait)
    return dataclasses.replace(deps, overleaf=dataclasses.replace(deps.overleaf, folder=overleaf_engine.engine_folder))


def _settle(qtbot, tab):
    """Wait until every Overleaf job the card started has been delivered to it."""
    qtbot.waitUntil(lambda: not tab._overleaf_busy, timeout=5000)  # noqa: SLF001 - the job count is the test's only view


def _launch(qtbot, tab):
    """Run the launch-time checks the way the window does once it shows, then wait for their answers."""
    tab.run_launch_checks()
    _settle(qtbot, tab)


def _checkbox(tab, name):
    box = _card(tab).findChild(QCheckBox, name)
    assert box is not None, name
    return box


def test_showing_home_alone_runs_no_launch_check(qtbot, tmp_path):
    folder = _folder_with_starter(tmp_path)
    _save_settings(tmp_path, engine_folder=str(folder))
    engine = FakeEngine(up=True, session=SIGNED_IN)
    tab = _shown(qtbot, _launch_deps(tmp_path, engine))
    _wait_engine(qtbot, tab)
    assert engine.starts == []
    assert engine.session_calls == []


def test_with_the_engine_switch_off_the_engine_is_never_started_even_with_a_folder(qtbot, tmp_path):
    folder = _folder_with_starter(tmp_path)
    _save_settings(tmp_path, engine_auto_start=False, engine_folder=str(folder))
    engine = FakeEngine(up=False, start_brings_up=True, session=SIGNED_IN)
    tab = _shown(qtbot, _launch_deps(tmp_path, engine))
    _launch(qtbot, tab)
    assert engine.starts == []
    assert engine.session_calls == []
    assert _note(tab) == ""
    assert _headline(tab) == "Engine not running"


def test_with_no_engine_folder_the_card_asks_for_one_and_starts_nothing(qtbot, tmp_path):
    engine = FakeEngine(up=False, start_brings_up=True, session=SIGNED_IN)
    tab = _shown(qtbot, _launch_deps(tmp_path, engine))
    _launch(qtbot, tab)
    assert _note(tab) == ENGINE_FOLDER_NOT_SET
    assert engine.starts == []
    assert engine.session_calls == []


def test_a_running_engine_is_not_started_again_at_launch(qtbot, tmp_path):
    folder = _folder_with_starter(tmp_path)
    _save_settings(tmp_path, engine_folder=str(folder))
    engine = FakeEngine(up=True, session=SIGNED_IN)
    tab = _shown(qtbot, _launch_deps(tmp_path, engine))
    _launch(qtbot, tab)
    assert engine.starts == []
    assert engine.session_calls == [LOOPBACK]


def test_a_down_engine_with_a_folder_is_started_once_and_then_checked(qtbot, tmp_path):
    folder = _folder_with_starter(tmp_path)
    _save_settings(tmp_path, engine_folder=str(folder))
    engine = FakeEngine(up=False, start_brings_up=True, session=SIGNED_IN)
    tab = _shown(qtbot, _launch_deps(tmp_path, engine))
    _launch(qtbot, tab)
    assert engine.starts == [folder]
    assert _note(tab) == ENGINE_STARTED
    assert engine.session_calls == [LOOPBACK]
    assert HEADLINE_TIME.match(_headline(tab))


def test_a_starter_that_never_brings_the_port_up_is_reported_as_not_started(qtbot, tmp_path):
    folder = _folder_with_starter(tmp_path)
    _save_settings(tmp_path, engine_folder=str(folder))
    engine = FakeEngine(up=False, start_brings_up=False, session=SIGNED_IN)
    tab = _shown(qtbot, _launch_deps(tmp_path, engine, wait=0.3))
    _launch(qtbot, tab)
    assert engine.starts == [folder]
    assert _note(tab) == ENGINE_NOT_STARTED
    assert engine.session_calls == []
    assert _headline(tab) == "Engine not running"


def test_a_launch_start_that_raises_says_the_class_name_and_never_the_message(qtbot, tmp_path):
    folder = _folder_with_starter(tmp_path)
    _save_settings(tmp_path, engine_folder=str(folder))
    engine = FakeEngine(up=False, start_error=PermissionError(SENTINEL), session=SIGNED_IN)
    tab = _shown(qtbot, _launch_deps(tmp_path, engine))
    _launch(qtbot, tab)
    assert engine.starts == [folder]
    assert _note(tab) == f"{ENGINE_NOT_STARTED}. PermissionError"
    assert SENTINEL not in _note(tab)
    assert engine.session_calls == []


def test_the_card_says_starting_while_the_starter_runs_and_holds_the_start_button(qtbot, tmp_path):
    folder = _folder_with_starter(tmp_path)
    _save_settings(tmp_path, engine_folder=str(folder))
    gate = threading.Event()
    engine = FakeEngine(up=False, start_brings_up=True, session=SIGNED_IN, start_gate=gate)
    tab = _shown(qtbot, _launch_deps(tmp_path, engine))
    tab.run_launch_checks()
    qtbot.waitUntil(lambda: engine.starts == [folder], timeout=5000)
    qtbot.waitUntil(lambda: _note(tab) == STARTING, timeout=5000)
    assert not _button(tab, "home-overleaf-start-engine").isEnabled()
    gate.set()
    qtbot.waitUntil(lambda: _note(tab) == ENGINE_STARTED, timeout=5000)
    _settle(qtbot, tab)
    assert engine.starts == [folder]


def test_with_the_check_switch_off_overleaf_is_never_asked_at_launch(qtbot, tmp_path):
    _save_settings(tmp_path, overleaf_auto_check=False)
    engine = FakeEngine(up=True, session=SIGNED_IN)
    tab = _shown(qtbot, _launch_deps(tmp_path, engine))
    _launch(qtbot, tab)
    assert engine.session_calls == []
    assert engine.login_calls == []
    assert engine.starts == []


def test_with_the_engine_up_and_the_check_switch_on_overleaf_is_asked_exactly_once(qtbot, tmp_path):
    engine = FakeEngine(up=True, session=SIGNED_IN)
    tab = _shown(qtbot, _launch_deps(tmp_path, engine))
    _launch(qtbot, tab)
    assert engine.session_calls == [LOOPBACK]
    assert HEADLINE_TIME.match(_headline(tab))


def test_a_later_show_or_the_home_timer_never_repeats_the_launch_session_check(qtbot, tmp_path):
    engine = FakeEngine(up=True, session=SIGNED_IN)
    tab = _shown(qtbot, _launch_deps(tmp_path, engine))
    _launch(qtbot, tab)
    tab.hide()
    tab.show()
    qtbot.waitExposed(tab)
    qtbot.wait(home_tab.SHOW_DEBOUNCE_MS + 200)
    timers = [timer for timer in tab.findChildren(QTimer) if timer.interval() == CHECK_INTERVAL_MS]
    assert len(timers) == 1
    timers[0].timeout.emit()
    _settle(qtbot, tab)
    qtbot.wait(50)
    assert engine.session_calls == [LOOPBACK]


def test_the_launch_checks_run_once_and_a_second_call_does_nothing(qtbot, tmp_path):
    engine = FakeEngine(up=True, session=SIGNED_IN)
    tab = _shown(qtbot, _launch_deps(tmp_path, engine))
    _launch(qtbot, tab)
    _launch(qtbot, tab)
    assert engine.session_calls == [LOOPBACK]


def test_with_both_switches_off_nothing_runs_at_launch(qtbot, tmp_path):
    folder = _folder_with_starter(tmp_path)
    _save_settings(tmp_path, engine_auto_start=False, overleaf_auto_check=False, engine_folder=str(folder))
    engine = FakeEngine(up=False, start_brings_up=True, session=SIGNED_IN)
    tab = _shown(qtbot, _launch_deps(tmp_path, engine))
    _launch(qtbot, tab)
    assert engine.starts == []
    assert engine.session_calls == []
    assert engine.login_calls == []


def test_an_expired_session_at_launch_is_said_and_log_in_stays_visible(qtbot, tmp_path):
    engine = FakeEngine(up=True, session=overleaf_client.SessionStatus("expired", chrome_found=True))
    tab = _shown(qtbot, _launch_deps(tmp_path, engine))
    _launch(qtbot, tab)
    assert _headline(tab) == "Session expired: click Log in"
    assert not _button(tab, "home-overleaf-login").isHidden()


# ----------------------------------------------------------------- the card: choosing the engine folder

def test_choosing_a_folder_without_the_starter_saves_nothing_and_says_so(qtbot, tmp_path):
    empty = tmp_path / "no-starter"
    empty.mkdir()
    engine = FakeEngine(up=False)
    tab = _shown(qtbot, _launch_deps(tmp_path, engine, choose=lambda parent, start_dir: str(empty)))
    _wait_engine(qtbot, tab)
    _button(tab, "home-overleaf-choose-folder").click()
    qtbot.waitUntil(lambda: _note(tab) == NO_STARTER, timeout=5000)
    assert not (tmp_path / "settings.json").exists()
    assert _saved(tmp_path).engine_folder == ""
    assert engine.starts == []


def test_choosing_a_folder_with_the_starter_saves_it_and_starts_nothing(qtbot, tmp_path):
    folder = _folder_with_starter(tmp_path)
    engine = FakeEngine(up=False, start_brings_up=True)
    tab = _shown(qtbot, _launch_deps(tmp_path, engine, choose=lambda parent, start_dir: str(folder)))
    _wait_engine(qtbot, tab)
    _button(tab, "home-overleaf-choose-folder").click()
    qtbot.waitUntil(lambda: _note(tab) == FOLDER_SAVED, timeout=5000)
    assert _saved(tmp_path).engine_folder == str(folder)
    assert engine.starts == []
    assert engine.session_calls == []
    assert not _button(tab, "home-overleaf-start-engine").isHidden()


def test_cancelling_the_folder_choice_changes_nothing(qtbot, tmp_path):
    engine = FakeEngine(up=False)
    tab = _shown(qtbot, _launch_deps(tmp_path, engine, choose=lambda parent, start_dir: ""))
    _wait_engine(qtbot, tab)
    _button(tab, "home-overleaf-choose-folder").click()
    qtbot.wait(100)
    assert _note(tab) == ""
    assert not (tmp_path / "settings.json").exists()
    assert engine.starts == []


# ----------------------------------------------------------------- the card: the two switches and the folder button

def test_the_card_has_the_two_switches_and_the_folder_button_with_their_words(qtbot, tmp_path):
    tab = _shown(qtbot, _launch_deps(tmp_path, FakeEngine(up=False)))
    _wait_engine(qtbot, tab)
    assert _checkbox(tab, "home-overleaf-auto-start").text() == START_BOX
    assert _checkbox(tab, "home-overleaf-auto-check").text() == CHECK_BOX
    assert _button(tab, "home-overleaf-choose-folder").text() == CHOOSE


def test_the_switches_show_the_saved_values(qtbot, tmp_path):
    _save_settings(tmp_path, engine_auto_start=False)
    tab = _shown(qtbot, _launch_deps(tmp_path, FakeEngine(up=False)))
    _wait_engine(qtbot, tab)
    assert _checkbox(tab, "home-overleaf-auto-start").isChecked() is False
    assert _checkbox(tab, "home-overleaf-auto-check").isChecked() is True


def test_each_switch_saves_at_once_when_it_is_clicked(qtbot, tmp_path):
    tab = _shown(qtbot, _launch_deps(tmp_path, FakeEngine(up=False)))
    _wait_engine(qtbot, tab)
    _checkbox(tab, "home-overleaf-auto-start").click()
    assert _saved(tmp_path).engine_auto_start is False
    _checkbox(tab, "home-overleaf-auto-check").click()
    assert _saved(tmp_path).overleaf_auto_check is False
    assert _checkbox(tab, "home-overleaf-auto-check").isChecked() is False


def test_no_cookie_token_or_path_text_shows_through_the_launch_or_the_folder_choice(qtbot, tmp_path):
    folder = _folder_with_starter(tmp_path, name=SENTINEL)
    _save_settings(tmp_path, engine_folder=str(folder))
    engine = FakeEngine(up=False, start_brings_up=True, session=SIGNED_IN)
    tab = _shown(qtbot, _launch_deps(tmp_path, engine, choose=lambda parent, start_dir: str(folder)))
    _launch(qtbot, tab)
    _button(tab, "home-overleaf-choose-folder").click()
    texts = _card_texts(tab)
    assert SENTINEL not in texts
    assert str(folder) not in texts
    assert str(tmp_path) not in texts
