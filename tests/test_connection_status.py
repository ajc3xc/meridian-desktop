"""Tests for the mode bar's connection line: meridian_desktop/connection_status.py and its window wiring.

Covered: the fixed text of every state of the Local, Hosted and Tunnel checks; the address a line shows (host and port
only); the hosted check's order of requests (no token means no request, /health before /projects, the token only on the
read); and the window's line and Test connection button, which run one check each time they should.

Nothing here reaches the network, a process or the real keychain. The hosted host is a fake transport that records each
request. The local probe is a fake, and the tunnel is a fake runner. Settings and the keychain are in pytest's temp
directory and an in-memory backend. A token or a response body must never appear in any text the check gives.
"""

from __future__ import annotations

import re
import threading
import urllib.parse
from datetime import datetime

import pytest
from PySide6.QtCore import QCoreApplication, QThreadPool, Qt
from PySide6.QtWidgets import QLabel, QPushButton

from _fakes import MemoryKeyring
from meridian_desktop import connection_status as cs
from meridian_desktop import hosted_client, local_client, token_store
from meridian_desktop.data_source import DataSources
from meridian_desktop.hosted_client import Reply
from meridian_desktop.runner_client import RunnerResult
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.tunnel_status import NOT_RUNNING, RUNNING, UNKNOWN, TunnelStatus
from meridian_desktop.ui.main_window import MainWindow
from meridian_desktop.ui.text import SELECTABLE_TEXT

HOSTED_URL = "https://usemeridian.us"
LOCAL_URL = "http://127.0.0.1:7878"
TOKEN = "sk_meridian_FAKE-connection-0001"
BODY_MARKER = "BODY-MARKER-PROJECT-NAME"
BODY = ('[{"id": "p1", "name": "%s"}]' % BODY_MARKER).encode("utf-8")
CHECKED = datetime(2026, 10, 26, 9, 5, 7)
TIME_SUFFIX = re.compile(r" Checked at \d{2}:\d{2}:\d{2}\.$")


def _clock():
    return CHECKED


# ------------------------------------------------------------------ fakes


class FakeHostedTransport:
    """A fake hosted host. It records each request as (path, Authorization header or None, timeout).

    ``health`` is the reply to GET /health, and ``read`` the reply to GET /projects. Each is a Reply, or an exception
    the transport raises instead. Nothing goes to the network.
    """

    def __init__(self, *, health=None, read=None):
        self.health = health if health is not None else Reply(200, b"")
        self.read = read if read is not None else Reply(200, b"[]")
        self.requests = []
        self._lock = threading.Lock()

    def __call__(self, request, timeout):
        path = urllib.parse.urlsplit(request.full_url).path
        with self._lock:
            self.requests.append((path, request.get_header("Authorization"), timeout))
        reply = self.health if path == hosted_client.HEALTH_PATH else self.read
        if isinstance(reply, BaseException):
            raise reply
        return reply


class GatedHostedTransport(FakeHostedTransport):
    """A fake hosted host that holds each request at ``gate`` until the test sets it. ``entered`` is set on the first one."""

    def __init__(self, gate, **kwargs):
        super().__init__(**kwargs)
        self.gate = gate
        self.entered = threading.Event()

    def __call__(self, request, timeout):
        self.entered.set()
        self.gate.wait(5)
        return super().__call__(request, timeout)


class CountingProbe:
    """A fake local probe. It records each URL it is asked and answers ``state``. ``gate``, when set, holds the call."""

    def __init__(self, state=local_client.HEALTH_RUNNING, *, gate=None):
        self.state = state
        self.gate = gate
        self.calls = []
        self._lock = threading.Lock()

    def __call__(self, url):
        with self._lock:
            self.calls.append(url)
        if self.gate is not None:
            self.gate.wait(5)
        return self.state


class FakeRunner:
    """A fake runner with only the read-only status call. It records each scope it is asked."""

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def status(self, scope):
        self.calls.append(scope)
        return self.reply


def _tunnel_reply(child_state):
    payload = {"scope": "tunnel", "generated_at": 1_800_000_000.0, "child": {"state": child_state}}
    return RunnerResult(verb="status", ok=True, payload=payload, exit_code=0)


def _hosted(transport, url=HOSTED_URL, token=TOKEN):
    return cs.probe_hosted(url, token, transport=transport, clock=_clock)


def _assert_no_secret(result, *secrets):
    """No token and no response body in the result, its line, its status text or its repr."""
    texts = (cs.status_text(result), repr(result))
    for secret in secrets:
        for text in texts:
            assert secret not in text


# ------------------------------------------------------------------ the fixed texts, one state at a time


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (local_client.HEALTH_RUNNING, "Local server at 127.0.0.1:7878: running."),
        (
            local_client.HEALTH_NOT_RUNNING,
            "Local server at 127.0.0.1:7878: not running. Start Meridian on this computer.",
        ),
        (local_client.HEALTH_ERROR, "Local server at 127.0.0.1:7878: did not answer in time."),
        (local_client.HEALTH_NOT_MERIDIAN, "Local server at 127.0.0.1:7878: answered, but it is not Meridian."),
    ],
)
def test_each_local_probe_state_has_its_fixed_line(state, expected):
    result = cs.ConnectionResult(cs.LOCAL, state, "127.0.0.1:7878", CHECKED)
    assert cs.local_line(result) == expected


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (cs.HOSTED_CONNECTED, "Hosted at usemeridian.us: connected as the saved account."),
        (cs.HOSTED_NO_TOKEN, "Hosted at usemeridian.us: no token is saved. Add it in Settings."),
        (cs.HOSTED_REFUSED, "Hosted at usemeridian.us: the token was refused. Check it in Settings."),
        (cs.HOSTED_UNREACHABLE, "Hosted at usemeridian.us: not reachable."),
    ],
)
def test_each_hosted_state_has_its_fixed_line(state, expected):
    result = cs.ConnectionResult(cs.HOSTED, state, "usemeridian.us", CHECKED)
    assert cs.hosted_line(result) == expected


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (RUNNING, "Tunnel: running."),
        (NOT_RUNNING, "Tunnel: not running."),
        (UNKNOWN, "Tunnel: status unknown."),
    ],
)
def test_each_tunnel_state_has_its_fixed_line(state, expected):
    result = cs.tunnel_result(TunnelStatus(state=state, reason="why", checked_at=CHECKED))
    assert cs.tunnel_line(result) == expected


def test_the_extra_states_have_fixed_lines_with_no_placeholder_left_over():
    bad_local = cs.ConnectionResult(cs.LOCAL, cs.LOCAL_BAD_ADDRESS, "", CHECKED)
    bad_host = cs.ConnectionResult(cs.HOSTED, cs.HOSTED_BAD_ADDRESS, "", CHECKED)
    bad_token = cs.ConnectionResult(cs.HOSTED, cs.HOSTED_BAD_TOKEN, "usemeridian.us", CHECKED)
    other = cs.ConnectionResult(cs.HOSTED, cs.HOSTED_OTHER, "usemeridian.us", CHECKED)
    for result in (bad_local, bad_host, bad_token, other):
        assert "{" not in cs.status_line(result)
    assert cs.local_line(bad_local) == cs.LOCAL_BAD_ADDRESS_TEXT
    assert cs.hosted_line(bad_host) == cs.HOSTED_BAD_ADDRESS_TEXT
    assert cs.hosted_line(other) == "Hosted at usemeridian.us: answered, but the projects could not be read."


def test_the_time_checked_is_added_as_hh_mm_ss_after_the_line():
    result = cs.ConnectionResult(cs.LOCAL, local_client.HEALTH_RUNNING, "127.0.0.1:7878", CHECKED)
    assert cs.status_text(result) == "Local server at 127.0.0.1:7878: running. Checked at 09:05:07."


def test_a_result_with_no_time_shows_only_its_line():
    result = cs.ConnectionResult(cs.TUNNEL, RUNNING)
    assert cs.status_text(result) == "Tunnel: running."


def test_an_unknown_mode_or_state_never_shows_a_wrong_line():
    assert cs.status_line(cs.ConnectionResult("cloud", "running")) == cs.CHECK_FAILED_TEXT
    assert cs.local_line(cs.ConnectionResult(cs.LOCAL, "no-such-state", "127.0.0.1:7878")) == cs.CHECK_FAILED_TEXT
    assert cs.tunnel_line(cs.ConnectionResult(cs.TUNNEL, "no-such-state")) == cs.TUNNEL_UNKNOWN_TEXT


# ------------------------------------------------------------------ the address a line shows


@pytest.mark.parametrize(
    ("url", "shown"),
    [
        ("http://127.0.0.1:7878", "127.0.0.1:7878"),
        ("http://127.0.0.1:7878/health?token=x#frag", "127.0.0.1:7878"),
        ("https://user:secret@usemeridian.us/projects", "usemeridian.us"),
        ("https://usemeridian.us", "usemeridian.us"),
        ("http://[::1]:7878", "[::1]:7878"),
        ("http://localhost:9000/", "localhost:9000"),
    ],
)
def test_an_address_shows_host_and_port_only(url, shown):
    assert cs.shown_address(url) == shown


@pytest.mark.parametrize("url", ["", "   ", "not a url", "https://usemeridian.us:notaport", None, 7878])
def test_an_address_with_no_host_shows_nothing(url):
    assert cs.shown_address(url) == ""


# ------------------------------------------------------------------ the local check


def test_the_local_check_asks_the_probe_once_at_the_local_url_and_shows_its_answer():
    probe = CountingProbe(local_client.HEALTH_NOT_RUNNING)
    result = cs.probe_local(LOCAL_URL, probe, clock=_clock)
    assert probe.calls == [LOCAL_URL]
    assert result.state == local_client.HEALTH_NOT_RUNNING
    assert cs.status_text(result) == (
        "Local server at 127.0.0.1:7878: not running. Start Meridian on this computer. Checked at 09:05:07."
    )


def test_a_local_url_that_is_not_on_this_computer_sends_no_probe():
    probe = CountingProbe()
    result = cs.probe_local("https://usemeridian.us", probe, clock=_clock)
    assert probe.calls == []
    assert result.state == cs.LOCAL_BAD_ADDRESS
    assert cs.local_line(result) == cs.LOCAL_BAD_ADDRESS_TEXT


def test_a_probe_that_raises_is_shown_as_not_answered_in_time():
    def broken(url):
        raise RuntimeError("probe went wrong")

    result = cs.probe_local(LOCAL_URL, broken, clock=_clock)
    assert cs.local_line(result) == "Local server at 127.0.0.1:7878: did not answer in time."
    assert "RuntimeError" not in cs.status_text(result)


# ------------------------------------------------------------------ the hosted check: order of requests and states


def test_no_token_saved_sends_no_request_at_all():
    transport = FakeHostedTransport()
    result = cs.probe_hosted(HOSTED_URL, None, transport=transport, clock=_clock)
    assert transport.requests == []
    assert result.state == cs.HOSTED_NO_TOKEN
    assert cs.hosted_line(result) == "Hosted at usemeridian.us: no token is saved. Add it in Settings."


@pytest.mark.parametrize("token", ["", "   "])
def test_a_blank_token_is_no_token_and_sends_no_request(token):
    transport = FakeHostedTransport()
    result = cs.probe_hosted(HOSTED_URL, token, transport=transport, clock=_clock)
    assert transport.requests == []
    assert result.state == cs.HOSTED_NO_TOKEN


def test_a_200_read_is_connected_and_the_health_check_comes_first_with_no_token():
    transport = FakeHostedTransport(health=Reply(200, b""), read=Reply(200, BODY))
    result = _hosted(transport)
    assert result.state == cs.HOSTED_CONNECTED
    assert transport.requests == [
        ("/health", None, hosted_client.HOSTED_TIMEOUT),
        ("/projects", f"Bearer {TOKEN}", hosted_client.HOSTED_TIMEOUT),
    ]
    assert cs.hosted_line(result) == "Hosted at usemeridian.us: connected as the saved account."
    _assert_no_secret(result, TOKEN, BODY_MARKER)


def test_the_token_is_sent_only_on_the_read_and_never_on_the_health_check():
    transport = FakeHostedTransport(health=Reply(200, b""), read=Reply(200, b"[]"))
    _hosted(transport)
    health_auth = [auth for path, auth, _timeout in transport.requests if path == "/health"]
    assert health_auth == [None]


def test_a_401_is_a_refused_token_and_reads_no_body():
    transport = FakeHostedTransport(read=Reply(401, BODY))
    result = _hosted(transport)
    assert result.state == cs.HOSTED_REFUSED
    assert cs.hosted_line(result) == "Hosted at usemeridian.us: the token was refused. Check it in Settings."
    _assert_no_secret(result, TOKEN, BODY_MARKER)


def test_a_403_is_also_a_refused_token():
    result = _hosted(FakeHostedTransport(read=Reply(403, b"")))
    assert result.state == cs.HOSTED_REFUSED


def test_a_timeout_on_health_is_not_reachable_and_the_token_is_never_sent():
    transport = FakeHostedTransport(health=TimeoutError("slow"))
    result = _hosted(transport)
    assert result.state == cs.HOSTED_UNREACHABLE
    assert cs.hosted_line(result) == "Hosted at usemeridian.us: not reachable."
    assert transport.requests == [("/health", None, hosted_client.HOSTED_TIMEOUT)]
    _assert_no_secret(result, TOKEN)


def test_a_timeout_on_the_read_is_not_reachable():
    transport = FakeHostedTransport(read=TimeoutError("slow"))
    result = _hosted(transport)
    assert result.state == cs.HOSTED_UNREACHABLE
    _assert_no_secret(result, TOKEN)


def test_a_lost_connection_on_health_is_not_reachable():
    transport = FakeHostedTransport(health=OSError("connection reset"))
    result = _hosted(transport)
    assert result.state == cs.HOSTED_UNREACHABLE
    _assert_no_secret(result, TOKEN, "connection reset")


def test_a_failure_with_no_typed_error_is_still_shown_as_not_reachable():
    transport = FakeHostedTransport(read=RuntimeError("fake transport broke"))
    result = _hosted(transport)
    assert result.state == cs.HOSTED_UNREACHABLE
    assert "RuntimeError" not in cs.status_text(result)


def test_a_404_on_the_projects_page_says_the_projects_could_not_be_read():
    result = _hosted(FakeHostedTransport(read=Reply(404, b"")))
    assert result.state == cs.HOSTED_OTHER
    assert cs.hosted_line(result) == "Hosted at usemeridian.us: answered, but the projects could not be read."


def test_a_200_with_a_body_that_is_not_a_project_list_says_the_same():
    result = _hosted(FakeHostedTransport(read=Reply(200, b"<html>" + BODY_MARKER.encode() + b"</html>")))
    assert result.state == cs.HOSTED_OTHER
    _assert_no_secret(result, BODY_MARKER)


def test_a_token_with_a_space_is_refused_before_the_read_and_the_read_is_never_sent():
    transport = FakeHostedTransport()
    result = cs.probe_hosted(HOSTED_URL, "sk_meridian has a space", transport=transport, clock=_clock)
    assert result.state == cs.HOSTED_BAD_TOKEN
    assert transport.requests == [("/health", None, hosted_client.HOSTED_TIMEOUT)]
    assert "has a space" not in cs.status_text(result)


@pytest.mark.parametrize("url", ["http://usemeridian.us", "", "https://user:pw@usemeridian.us?x=1"])
def test_a_hosted_url_that_may_not_be_used_sends_no_request(url):
    transport = FakeHostedTransport()
    result = cs.probe_hosted(url, TOKEN, transport=transport, clock=_clock)
    assert transport.requests == []
    assert result.state == cs.HOSTED_BAD_ADDRESS
    assert cs.hosted_line(result) == cs.HOSTED_BAD_ADDRESS_TEXT


def test_the_hosted_line_shows_the_host_and_port_and_never_the_path():
    transport = FakeHostedTransport()
    result = cs.probe_hosted("https://usemeridian.us:8443/some/path", TOKEN, transport=transport, clock=_clock)
    assert result.address == "usemeridian.us:8443"
    assert cs.hosted_line(result).startswith("Hosted at usemeridian.us:8443: connected")
    assert "some/path" not in cs.status_text(result)


def test_a_hosted_url_with_a_password_is_refused_and_the_password_never_shows():
    transport = FakeHostedTransport()
    result = cs.probe_hosted("https://user:hunter2@usemeridian.us", TOKEN, transport=transport, clock=_clock)
    assert transport.requests == []
    assert result.state == cs.HOSTED_BAD_ADDRESS
    assert "hunter2" not in cs.status_text(result)
    assert "hunter2" not in repr(result)


# ------------------------------------------------------------------ the window: the line, the button, and each run


def _settle():
    """Wait until the pool is idle, then let the queued deliveries reach the window."""
    QThreadPool.globalInstance().waitForDone(5000)
    for _ in range(3):
        QCoreApplication.processEvents()


def _window(
    qtbot, settings_path, *, mode=None, probe=None, transport=None, runner=None, token=None, gate=None, smoke=False
):
    if mode is not None:
        SettingsStore(settings_path).set_mode(mode)
    secrets = KeyringSecretStore(backend=MemoryKeyring())
    if token is not None:
        token_store.set_token(token, store=secrets)
    sources = DataSources(
        probe=probe if probe is not None else CountingProbe(local_client.HEALTH_RUNNING),
        read_server=lambda url: [],
        read_file=lambda path: [],
        get_token=lambda: None,
        list_hosted=lambda url, tok: [],
    )
    window = MainWindow(
        SettingsStore(settings_path),
        secret_store=secrets,
        data_sources=sources,
        tunnel_runner=runner if runner is not None else FakeRunner(_tunnel_reply(RUNNING)),
        # Never the real transport: a window in hosted mode in a test gets a fake that records each request.
        hosted_transport=transport if transport is not None else FakeHostedTransport(),
        # A window built the way ui/app.py builds one for --smoke makes no connection check at all.
        skip_connection_check=smoke,
    )
    qtbot.addWidget(window)
    return window


def _line(window):
    return window.connection_status_label.text()


def _mode_index(window, value):
    return window.mode_selector.findData(value)


def test_the_mode_bar_has_one_selectable_connection_line_and_a_test_connection_button(qtbot, settings_path):
    window = _window(qtbot, settings_path)
    label = window.connection_status_label
    assert label.objectName() == "connection-status"
    assert isinstance(label, QLabel)
    assert label.textInteractionFlags() == SELECTABLE_TEXT
    assert label.textFormat() == Qt.TextFormat.PlainText
    button = window.findChild(QPushButton, "mode-test-connection")
    assert button is not None
    assert button.text() == "Test connection"
    # The check the window starts at open holds the button until it has delivered, so wait for it first.
    _settle()
    assert button.isEnabled()
    # The line and its button sit in the mode bar, under the description.
    assert button.parentWidget() is label.parentWidget() is window.mode_description.parentWidget()


def test_the_local_line_is_checked_once_when_the_window_opens(qtbot, settings_path):
    probe = CountingProbe(local_client.HEALTH_RUNNING)
    window = _window(qtbot, settings_path, probe=probe)
    qtbot.waitUntil(lambda: _line(window).startswith("Local server at 127.0.0.1:7878: running."), timeout=5000)
    _settle()
    assert probe.calls == [LOCAL_URL]
    assert re.search(TIME_SUFFIX, _line(window))


def test_the_test_connection_button_runs_the_local_check_exactly_once(qtbot, settings_path):
    probe = CountingProbe(local_client.HEALTH_NOT_RUNNING)
    window = _window(qtbot, settings_path, probe=probe)
    _settle()
    before = len(probe.calls)
    window.findChild(QPushButton, "mode-test-connection").click()
    qtbot.waitUntil(lambda: _line(window).startswith("Local server at 127.0.0.1:7878: not running."), timeout=5000)
    _settle()
    assert len(probe.calls) == before + 1


def test_test_connection_is_off_while_a_hosted_check_runs_and_on_again_when_it_finishes(qtbot, settings_path):
    gate = threading.Event()
    transport = GatedHostedTransport(gate)
    window = _window(qtbot, settings_path, mode="hosted", token=TOKEN, transport=transport)
    button = window.findChild(QPushButton, "mode-test-connection")
    try:
        qtbot.waitUntil(transport.entered.is_set, timeout=5000)
        # The check the window starts at open is held at the transport, so the button is off and a press starts nothing.
        assert not button.isEnabled()
        button.click()
    finally:
        gate.set()
    _settle()
    # Only the one check ran: one /health and one /projects. A press that got through would add a second pair.
    assert [path for path, _auth, _timeout in transport.requests] == ["/health", "/projects"]
    assert button.isEnabled()
    assert _line(window).startswith("Hosted at usemeridian.us: connected")


def test_test_connection_comes_back_on_when_a_mode_change_drops_the_held_check(qtbot, settings_path):
    gate = threading.Event()
    transport = GatedHostedTransport(gate)
    window = _window(qtbot, settings_path, mode="hosted", token=TOKEN, transport=transport)
    button = window.findChild(QPushButton, "mode-test-connection")
    try:
        qtbot.waitUntil(transport.entered.is_set, timeout=5000)
        assert not button.isEnabled()
        # Tunnel mode reads its status and finishes while the hosted check is still held. The held result is then
        # dropped, because the mode has changed. Test connection must still come back on once nothing is left running.
        window.mode_selector.setCurrentIndex(_mode_index(window, "tunnel"))
        qtbot.waitUntil(lambda: window.tunnel_status_label.text().startswith("Tunnel running."), timeout=5000)
        assert not button.isEnabled()
    finally:
        gate.set()
    _settle()
    assert button.isEnabled()
    assert _line(window).startswith("Tunnel: running.")


def test_changing_the_mode_runs_the_hosted_check_exactly_once(qtbot, settings_path):
    transport = FakeHostedTransport()
    window = _window(qtbot, settings_path, token=TOKEN, transport=transport)
    _settle()
    before = len(transport.requests)
    window.mode_selector.setCurrentIndex(_mode_index(window, "hosted"))
    qtbot.waitUntil(lambda: _line(window).startswith("Hosted at usemeridian.us: connected"), timeout=5000)
    _settle()
    # One check is one /health and one /projects: two requests, and no more.
    assert [path for path, _auth, _timeout in transport.requests[before:]] == ["/health", "/projects"]
    assert re.search(TIME_SUFFIX, _line(window))


def test_hosted_mode_with_no_token_sends_nothing_and_says_so(qtbot, settings_path):
    transport = FakeHostedTransport()
    window = _window(qtbot, settings_path, mode="hosted", transport=transport)
    qtbot.waitUntil(lambda: _line(window).startswith("Hosted at usemeridian.us: no token is saved."), timeout=5000)
    _settle()
    assert transport.requests == []


def test_a_refused_token_shows_its_line_in_the_window(qtbot, settings_path):
    transport = FakeHostedTransport(read=Reply(401, BODY))
    window = _window(qtbot, settings_path, mode="hosted", token=TOKEN, transport=transport)
    qtbot.waitUntil(lambda: _line(window).startswith("Hosted at usemeridian.us: the token was refused."), timeout=5000)
    _settle()
    assert TOKEN not in _line(window)
    assert BODY_MARKER not in _line(window)


def test_the_tunnel_line_is_the_top_bar_read_and_the_tunnel_is_read_once(qtbot, settings_path):
    runner = FakeRunner(_tunnel_reply(RUNNING))
    window = _window(qtbot, settings_path, mode="tunnel", runner=runner)
    qtbot.waitUntil(lambda: _line(window).startswith("Tunnel: running. Checked at "), timeout=5000)
    _settle()
    assert runner.calls == ["tunnel"]
    assert window.tunnel_status_label.text().startswith("Tunnel running.")


def test_a_stale_local_result_is_dropped_once_the_mode_has_changed(qtbot, settings_path):
    gate = threading.Event()
    probe = CountingProbe(local_client.HEALTH_RUNNING, gate=gate)
    window = _window(qtbot, settings_path, probe=probe)
    # The local check is held at the probe. Switching to Hosted (no token: no request) shows its own line.
    window.mode_selector.setCurrentIndex(_mode_index(window, "hosted"))
    qtbot.waitUntil(lambda: _line(window).startswith("Hosted at usemeridian.us: no token is saved."), timeout=5000)
    gate.set()
    _settle()
    qtbot.wait(50)
    assert _line(window).startswith("Hosted at usemeridian.us: no token is saved.")
    assert "Local server" not in _line(window)


def test_the_connection_line_has_no_timer_and_runs_no_check_on_its_own(qtbot, settings_path):
    probe = CountingProbe(local_client.HEALTH_RUNNING)
    window = _window(qtbot, settings_path, probe=probe)
    _settle()
    before = len(probe.calls)
    qtbot.wait(300)
    _settle()
    assert len(probe.calls) == before


# ------------------------------------------------------------------ the smoke run and the normal launch


def test_a_window_built_for_a_smoke_run_makes_no_hosted_request_even_with_a_token_saved(qtbot, settings_path):
    transport = FakeHostedTransport()
    window = _window(qtbot, settings_path, mode="hosted", token=TOKEN, transport=transport, smoke=True)
    window.show()
    _settle()
    assert transport.requests == []
    assert _line(window) == ""
    assert window.findChild(QPushButton, "mode-test-connection").isEnabled()


def test_a_smoke_built_window_starts_no_check_when_its_mode_changes_either(qtbot, settings_path):
    transport = FakeHostedTransport()
    window = _window(qtbot, settings_path, token=TOKEN, transport=transport, smoke=True)
    window.mode_selector.setCurrentIndex(_mode_index(window, "hosted"))
    _settle()
    assert transport.requests == []


def test_a_smoke_built_tunnel_window_starts_no_tunnel_check_from_test_connection(qtbot, settings_path):
    # The window's own read at open is not counted: this test counts only what the button starts. The button is held
    # off until that read delivers, so wait for it first.
    runner = FakeRunner(_tunnel_reply(RUNNING))
    window = _window(qtbot, settings_path, mode="tunnel", runner=runner, smoke=True)
    button = window.findChild(QPushButton, "mode-test-connection")
    qtbot.waitUntil(button.isEnabled, timeout=5000)
    _settle()
    before = len(runner.calls)
    button.click()
    _settle()
    assert len(runner.calls) == before


def test_test_connection_in_tunnel_mode_reads_the_tunnel_once_when_the_window_is_not_a_smoke_run(qtbot, settings_path):
    # The control for the smoke case above: outside a smoke run the same button press does read the tunnel once.
    runner = FakeRunner(_tunnel_reply(RUNNING))
    window = _window(qtbot, settings_path, mode="tunnel", runner=runner)
    button = window.findChild(QPushButton, "mode-test-connection")
    qtbot.waitUntil(button.isEnabled, timeout=5000)
    _settle()
    before = len(runner.calls)
    button.click()
    qtbot.waitUntil(lambda: len(runner.calls) == before + 1, timeout=5000)
    _settle()
    assert len(runner.calls) == before + 1


def test_a_window_built_for_a_normal_launch_checks_the_hosted_host_when_it_opens(qtbot, settings_path):
    transport = FakeHostedTransport()
    window = _window(qtbot, settings_path, mode="hosted", token=TOKEN, transport=transport)
    window.show()
    qtbot.waitUntil(lambda: _line(window).startswith("Hosted at usemeridian.us: connected"), timeout=5000)
    _settle()
    assert [(path, auth) for path, auth, _timeout in transport.requests] == [
        ("/health", None),
        ("/projects", f"Bearer {TOKEN}"),
    ]
