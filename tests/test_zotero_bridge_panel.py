"""Zotero bridge: the client's loopback calls and answers, the bridge line on the card, and the Update bridge dialogs.

Offscreen Qt and no network. Every bridge call goes to a recording transport or a fake bridge, so no test reaches the
local server, Zotero or a service port. The Zotero status itself comes from a fake client, as in test_zotero_panel.py.
Each modal dialog is answered by a stand-in for exec(), so each test decides what the owner clicks, in order.
"""

import io
import json
import threading
import urllib.error

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QMessageBox, QPushButton

from meridian_desktop import local_client, zotero_bridge_client
from meridian_desktop.local_client import LocalClientError
from meridian_desktop.ui import zotero_panel
from meridian_desktop.ui.confirm import NO_TEXT, YES_TEXT
from meridian_desktop.ui.text import SELECTABLE_TEXT
from meridian_desktop.ui.zotero_panel import (
    AFTER_POLITE_TEXT,
    BRIDGE_CHECKING_TEXT,
    BRIDGE_DIALOG_DETAIL,
    BRIDGE_DIALOG_TEXT,
    BRIDGE_DONE_TEXT,
    BRIDGE_ERROR_TEXT,
    BRIDGE_NOT_CHECKED_TEXT,
    BRIDGE_SENDING_TEXT,
    CANCEL_BUTTON_TEXT,
    CHECK_FAILED_TEXT,
    FOLLOW_FORCE,
    FOLLOW_RETRY,
    FORCE_BUTTON_TEXT,
    FORCE_CONFIRM_TEXT,
    POLITE_BUTTON_TEXT,
    RETRY_BUTTON_TEXT,
    UPDATE_BUTTON_TEXT,
    WAIT_BUTTON_TEXT,
    WAIT_RESULT_TEXT,
    ZoteroPanel,
    ask_after_polite_failure,
    ask_bridge_update,
    bridge_status_text,
    bridge_update_dialog,
)
from meridian_desktop.zotero_bridge_client import (
    BAD_CHOICE_MESSAGE,
    CHOICE_FORCE,
    CHOICE_POLITE,
    CHOICE_WAIT,
    STATE_CURRENT,
    STATE_ERROR,
    STATE_NOT_INSTALLED,
    STATE_UPDATE_READY,
    STATE_WAIT_PENDING,
    STATUS_PATH,
    UPDATE_PATH,
    ZOTERO_DID_NOT_CLOSE_MESSAGE,
    BridgeResult,
    BridgeStatus,
    Reply,
    choice_body,
)
from meridian_desktop.zotero_client import API_ON, ApiCheck, ZoteroStatus

LOCAL = "http://127.0.0.1:7999"
REMOTE = "http://example.com:7999"
ZOTERO_EXE = r"C:\Program Files\Zotero\zotero.exe"
# A fake value that must never show up in any text, as a token would not.
SENTINEL = "TOKEN-SENTINEL-DO-NOT-SHOW"
# The three bodies the desktop may send, exactly as the core route takes them.
POLITE_BODY = {"when": "now", "close": "polite"}
FORCE_BODY = {"when": "now", "close": "force"}
WAIT_BODY = {"when": "wait"}
CURRENT = BridgeStatus(STATE_CURRENT, "0.1.4", "0.1.4", False)
UPDATE_READY = BridgeStatus(STATE_UPDATE_READY, "0.1.3", "0.1.4", False)
UPDATE_READY_ZOTERO_OPEN = BridgeStatus(STATE_UPDATE_READY, "0.1.3", "0.1.4", True)
NOT_INSTALLED = BridgeStatus(STATE_NOT_INSTALLED, None, "0.1.4", False)
WAIT_PENDING = BridgeStatus(STATE_WAIT_PENDING, "0.1.3", "0.1.4", True)
STILL_OPEN_BODY = json.dumps({"error": {"code": "zotero_still_open", "message": SENTINEL}}).encode("utf-8")
FORCED_LINE = "Zotero was closed by force and the bridge updated."


def _answer(state, *, installed="0.1.3", expected="0.1.4", running=False):
    return {"state": state, "installed_version": installed, "expected_version": expected, "zotero_running": running}


def _json_reply(payload, status=200):
    return Reply(status, json.dumps(payload).encode("utf-8"))


class Transport:
    """A bridge transport that records each call. An answer is a Reply or an exception. A list of answers is used in
    order, one per call, so a test can give a failure and then a success."""

    def __init__(self, get=None, post=None):
        self.answers = {"GET": get, "POST": post}
        self.calls = []

    def __call__(self, method, base_url, path, body, timeout):
        self.calls.append({"method": method, "base_url": base_url, "path": path, "body": body, "timeout": timeout})
        answer = self.answers[method]
        if isinstance(answer, list):
            answer = answer.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _posts(transport):
    return [call["body"] for call in transport.calls if call["method"] == "POST"]


class FakeBridge:
    """Answers status and update from the values it was given. It records each call and the thread it ran on."""

    def __init__(self, status=CURRENT, update=None, status_error=None):
        self.status_value = status
        self.update_value = BridgeResult(True) if update is None else update
        self.status_error = status_error
        self.calls = []
        self.threads = []
        self._lock = threading.Lock()

    def _record(self, *call):
        with self._lock:
            self.calls.append(call)
            self.threads.append(threading.current_thread())

    def names(self):
        return [call[0] for call in self.calls]

    def status(self, base_url):
        self._record("status", base_url)
        if self.status_error is not None:
            raise self.status_error
        return self.status_value

    def update(self, base_url, choice):
        self._record("update", base_url, choice)
        if isinstance(self.update_value, BaseException):
            raise self.update_value
        return self.update_value


class RealBridge:
    """The real bridge client, with a fake transport in place of the network."""

    def __init__(self, transport):
        self.transport = transport

    def status(self, base_url):
        return zotero_bridge_client.status(base_url, transport=self.transport)

    def update(self, base_url, choice):
        return zotero_bridge_client.update(base_url, choice, transport=self.transport)


class FakeZotero:
    """The Zotero client the card reads. It is installed and not running, and its API is on."""

    def status(self):
        return ZoteroStatus(exe=ZOTERO_EXE, process_running=False, api=ApiCheck(API_ON, "Zotero's local API answered."))

    def search_items(self, query):
        return []

    def probe_api(self):
        return ApiCheck(API_ON, "Zotero's local API answered.")

    def start_zotero(self, exe):
        return "started"


# ----------------------------------------------------------------- the client: requests


def test_status_is_a_get_on_the_status_route_with_a_five_second_timeout():
    transport = Transport(get=_json_reply(_answer(STATE_CURRENT)))
    zotero_bridge_client.status(LOCAL, transport=transport)
    assert transport.calls == [
        {"method": "GET", "base_url": LOCAL, "path": STATUS_PATH, "body": None, "timeout": 5.0},
    ]


@pytest.mark.parametrize(
    "choice, body",
    [(CHOICE_POLITE, POLITE_BODY), (CHOICE_FORCE, FORCE_BODY), (CHOICE_WAIT, WAIT_BODY)],
)
def test_update_posts_exactly_the_body_for_each_choice_with_a_seventy_five_second_timeout(choice, body):
    transport = Transport(post=_json_reply({"message": "Zotero was closed."}))
    result = zotero_bridge_client.update(LOCAL, choice, transport=transport)
    assert transport.calls == [
        {"method": "POST", "base_url": LOCAL, "path": UPDATE_PATH, "body": body, "timeout": 75.0},
    ]
    assert result.ok


def test_the_choice_bodies_are_the_three_fixed_bodies_and_nothing_else():
    assert choice_body(CHOICE_POLITE) == POLITE_BODY
    assert choice_body(CHOICE_FORCE) == FORCE_BODY
    assert choice_body(CHOICE_WAIT) == WAIT_BODY
    assert choice_body("now") is None
    assert choice_body(None) is None


@pytest.mark.parametrize("base", [REMOTE, "http://localhost.example.com:7999", "https://127.0.0.1:7999",
                                  "http://127.0.0.1.example.com:7999", "http://user:secret@127.0.0.1:7999"])
def test_a_non_loopback_address_is_refused_before_any_request(base):
    transport = Transport(get=_json_reply(_answer(STATE_CURRENT)), post=_json_reply({}))
    status = zotero_bridge_client.status(base, transport=transport)
    result = zotero_bridge_client.update(base, CHOICE_POLITE, transport=transport)
    assert status.state == STATE_ERROR
    assert status.reason
    assert not result.ok
    assert result.reason
    assert "secret" not in status.reason and "secret" not in result.reason
    assert transport.calls == []


@pytest.mark.parametrize("choice", ["now", "wait_now", "force_kill", "", None, POLITE_BODY])
def test_a_choice_the_desktop_does_not_send_is_refused_before_any_request(choice):
    transport = Transport(post=_json_reply({}))
    result = zotero_bridge_client.update(LOCAL, choice, transport=transport)
    assert result == BridgeResult(False, reason=BAD_CHOICE_MESSAGE)
    assert transport.calls == []


def test_the_request_carries_no_token_and_goes_only_to_loopback():
    request = zotero_bridge_client.build_request("POST", LOCAL, UPDATE_PATH, POLITE_BODY)
    assert request.get_method() == "POST"
    assert request.full_url == LOCAL + UPDATE_PATH
    assert request.data == b'{"when": "now", "close": "polite"}'
    assert request.get_header("Content-type") == "application/json"
    assert not request.has_header("Authorization")


def test_build_request_refuses_a_non_loopback_address_with_its_code():
    with pytest.raises(LocalClientError) as caught:
        zotero_bridge_client.build_request("GET", REMOTE, STATUS_PATH, None)
    assert caught.value.code == "not_loopback"


# ----------------------------------------------------------------- the client: answers


@pytest.mark.parametrize("state", [STATE_CURRENT, STATE_UPDATE_READY, STATE_NOT_INSTALLED, STATE_WAIT_PENDING])
def test_each_status_state_is_read_with_its_versions_and_running_flag(state):
    transport = Transport(get=_json_reply(_answer(state, running=True)))
    result = zotero_bridge_client.status(LOCAL, transport=transport)
    assert result.state == state
    assert (result.installed, result.expected, result.zotero_running) == ("0.1.3", "0.1.4", True)


def test_an_unknown_state_or_an_unreadable_answer_is_an_error_with_fixed_text():
    for reply in [
        _json_reply({"state": ["a", "list"]}),
        _json_reply({"state": "half_done"}),
        Reply(200, f"{SENTINEL} is not json".encode("utf-8")),
    ]:
        result = zotero_bridge_client.status(LOCAL, transport=Transport(get=reply))
        assert result.state == STATE_ERROR
        assert result.reason == zotero_bridge_client.BAD_ANSWER_MESSAGE
        assert SENTINEL not in result.reason


def test_a_missing_route_says_the_server_is_too_old():
    status = zotero_bridge_client.status(LOCAL, transport=Transport(get=Reply(404, b"")))
    result = zotero_bridge_client.update(LOCAL, CHOICE_POLITE, transport=Transport(post=Reply(404, b"")))
    assert status.reason == zotero_bridge_client.NO_ROUTE_MESSAGE
    assert result.reason == zotero_bridge_client.NO_ROUTE_MESSAGE


def test_an_error_body_is_never_copied_into_the_reason_but_its_safe_code_is_shown():
    body = json.dumps({"error": {"code": "bridge_failed", "message": SENTINEL}}).encode("utf-8")
    status = zotero_bridge_client.status(LOCAL, transport=Transport(get=Reply(500, body)))
    result = zotero_bridge_client.update(LOCAL, CHOICE_FORCE, transport=Transport(post=Reply(500, body)))
    assert status.reason == zotero_bridge_client.HTTP_STATUS_MESSAGE.format(500)
    assert SENTINEL not in result.reason
    assert result.reason.endswith("Server code: bridge_failed.")


def test_a_failed_update_names_the_status_and_the_safe_code():
    body = json.dumps({"error": {"code": "bridge_busy", "message": "Zotero is busy."}}).encode("utf-8")
    result = zotero_bridge_client.update(LOCAL, CHOICE_WAIT, transport=Transport(post=Reply(409, body)))
    assert result == BridgeResult(
        False,
        reason="The local Meridian server could not update the bridge (HTTP 409). Server code: bridge_busy.",
    )


def test_a_polite_close_that_leaves_zotero_open_is_reported_as_not_closed():
    result = zotero_bridge_client.update(LOCAL, CHOICE_POLITE, transport=Transport(post=Reply(409, STILL_OPEN_BODY)))
    assert result == BridgeResult(False, reason=ZOTERO_DID_NOT_CLOSE_MESSAGE, still_open=True)
    assert SENTINEL not in result.reason


def test_only_the_still_open_code_marks_a_failure_as_not_closed():
    body = json.dumps({"error": {"code": "bridge_busy", "message": "Zotero is busy."}}).encode("utf-8")
    result = zotero_bridge_client.update(LOCAL, CHOICE_POLITE, transport=Transport(post=Reply(409, body)))
    assert result.still_open is False


def test_an_update_answer_that_is_not_an_object_is_a_failure():
    result = zotero_bridge_client.update(LOCAL, CHOICE_POLITE, transport=Transport(post=Reply(200, b"[1, 2]")))
    assert result == BridgeResult(False, reason=zotero_bridge_client.BAD_ANSWER_MESSAGE)


def test_the_update_line_is_one_short_line_of_the_servers_message():
    multi = zotero_bridge_client.update(
        LOCAL, CHOICE_POLITE, transport=Transport(post=_json_reply({"message": "Zotero closed.\nBridge  updated."}))
    )
    assert multi.line == "Zotero closed. Bridge updated."
    long = zotero_bridge_client.update(
        LOCAL, CHOICE_POLITE, transport=Transport(post=_json_reply({"message": "x" * 500}))
    )
    assert len(long.line) <= zotero_bridge_client.MAX_MESSAGE_CHARS
    assert long.line.endswith("...")


def test_a_refused_connection_gives_the_not_running_text():
    error = LocalClientError("not_running", local_client.NOT_RUNNING_MESSAGE)
    status = zotero_bridge_client.status(LOCAL, transport=Transport(get=error))
    result = zotero_bridge_client.update(LOCAL, CHOICE_POLITE, transport=Transport(post=error))
    assert status.reason == local_client.NOT_RUNNING_MESSAGE
    assert result.reason == local_client.NOT_RUNNING_MESSAGE


def test_an_unexpected_transport_failure_shows_fixed_text_and_no_detail():
    boom = RuntimeError("private detail")
    status = zotero_bridge_client.status(LOCAL, transport=Transport(get=boom))
    result = zotero_bridge_client.update(LOCAL, CHOICE_POLITE, transport=Transport(post=boom))
    assert status.reason == zotero_bridge_client.UNCHECKED_MESSAGE
    assert result.reason == zotero_bridge_client.UNCHECKED_MESSAGE
    assert "private detail" not in status.reason + result.reason


def test_a_redirect_is_refused_and_not_followed():
    status = zotero_bridge_client.status(LOCAL, transport=Transport(get=Reply(302, b"")))
    result = zotero_bridge_client.update(LOCAL, CHOICE_POLITE, transport=Transport(post=Reply(302, b"")))
    assert status.reason == local_client.REDIRECT_MESSAGE
    assert result.reason == local_client.REDIRECT_MESSAGE


def test_a_failed_update_with_an_unsafe_code_names_only_the_status():
    body = json.dumps({"error": {"code": "Not A Slug", "message": SENTINEL}}).encode("utf-8")
    result = zotero_bridge_client.update(LOCAL, CHOICE_POLITE, transport=Transport(post=Reply(503, body)))
    assert result == BridgeResult(False, reason=zotero_bridge_client.UPDATE_FAILED_MESSAGE.format(503))
    assert SENTINEL not in result.reason


def test_malformed_optional_fields_read_as_unknown_and_a_non_text_message_is_empty():
    payload = {
        "state": "update_ready",
        "installed_version": 3,
        "expected_version": "../escape",
        "zotero_running": "yes",
    }
    result = zotero_bridge_client.status(LOCAL, transport=Transport(get=_json_reply(payload)))
    assert (result.state, result.installed, result.expected, result.zotero_running) == (
        STATE_UPDATE_READY,
        None,
        None,
        None,
    )
    assert zotero_bridge_client.update(
        LOCAL, CHOICE_POLITE, transport=Transport(post=_json_reply({"message": 7}))
    ) == BridgeResult(True, line="")


# ----------------------------------------------------------------- the default transport, with a fake opener, no socket


class _FakeResponse:
    """A urllib response stand-in: a status and a body, read in one piece."""

    def __init__(self, status, body):
        self.status = status
        self.headers = None
        self._body = io.BytesIO(body)

    def read1(self, size):
        return self._body.read1(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeOpener:
    """Stands in for the urllib opener. It records each request, then returns its response or raises its error."""

    def __init__(self, answer):
        self.answer = answer
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request.get_method(), request.full_url, request.data, timeout))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


def _use_opener(monkeypatch, answer):
    opener = _FakeOpener(answer)
    monkeypatch.setattr(local_client, "_opener", lambda: opener)
    return opener


def test_the_default_transport_sends_a_get_for_the_status(monkeypatch):
    opener = _use_opener(monkeypatch, _FakeResponse(200, json.dumps(_answer(STATE_CURRENT)).encode("utf-8")))
    result = zotero_bridge_client.status(LOCAL)
    assert result.state == STATE_CURRENT
    assert opener.requests == [("GET", LOCAL + STATUS_PATH, None, 5.0)]


def test_the_default_transport_posts_the_choice_as_json_and_reads_its_message(monkeypatch):
    opener = _use_opener(monkeypatch, _FakeResponse(200, b'{"message": "Zotero closed."}'))
    result = zotero_bridge_client.update(LOCAL, CHOICE_FORCE)
    assert result == BridgeResult(True, line="Zotero closed.")
    assert opener.requests == [("POST", LOCAL + UPDATE_PATH, b'{"when": "now", "close": "force"}', 75.0)]


def test_the_default_transport_posts_the_wait_body(monkeypatch):
    opener = _use_opener(monkeypatch, _FakeResponse(200, b"{}"))
    zotero_bridge_client.update(LOCAL, CHOICE_WAIT)
    assert opener.requests == [("POST", LOCAL + UPDATE_PATH, b'{"when": "wait"}', 75.0)]


def test_an_http_error_answer_is_read_like_any_other_answer(monkeypatch):
    body = io.BytesIO(json.dumps({"error": {"code": "bridge_failed", "message": SENTINEL}}).encode("utf-8"))
    _use_opener(monkeypatch, urllib.error.HTTPError(LOCAL + STATUS_PATH, 500, "Server Error", None, body))
    status = zotero_bridge_client.status(LOCAL)
    assert status.reason == zotero_bridge_client.HTTP_STATUS_MESSAGE.format(500)
    assert SENTINEL not in status.reason


@pytest.mark.parametrize(
    "error, expected",
    [
        (urllib.error.URLError(ConnectionRefusedError()), local_client.NOT_RUNNING_MESSAGE),
        (TimeoutError(), local_client.TIMEOUT_MESSAGE),
        (urllib.error.URLError(OSError("no route")), local_client.UNREACHABLE_MESSAGE),
        (OSError("socket"), local_client.UNREACHABLE_MESSAGE),
        (ValueError("bad"), local_client.BAD_URL_MESSAGE),
    ],
)
def test_the_default_transport_maps_each_connection_failure_to_fixed_text(monkeypatch, error, expected):
    _use_opener(monkeypatch, error)
    assert zotero_bridge_client.status(LOCAL).reason == expected
    assert zotero_bridge_client.update(LOCAL, CHOICE_POLITE).reason == expected


# ----------------------------------------------------------------- the card's bridge line


@pytest.mark.parametrize(
    "status, expected",
    [
        (None, BRIDGE_NOT_CHECKED_TEXT),
        (CURRENT, "Bridge 0.1.4 installed; expected 0.1.4."),
        (UPDATE_READY, "Bridge 0.1.3 installed; expected 0.1.4. An update is ready."),
        (
            UPDATE_READY_ZOTERO_OPEN,
            "Bridge 0.1.3 installed; expected 0.1.4. An update is ready. "
            "Zotero is open, so it needs Zotero to close and restart.",
        ),
        (NOT_INSTALLED, "Bridge not installed; expected 0.1.4."),
        (
            WAIT_PENDING,
            "Bridge 0.1.3 installed; expected 0.1.4. Waiting: the bridge will update the next time Zotero starts.",
        ),
        (
            BridgeStatus(STATE_ERROR, reason="The local server address must be 127.0.0.1, localhost or ::1."),
            "Bridge: could not be checked. The local server address must be 127.0.0.1, localhost or ::1.",
        ),
    ],
)
def test_the_bridge_line_text_for_each_state(status, expected):
    assert bridge_status_text(status) == expected


def test_the_wait_result_line_is_the_waiting_sentence():
    assert WAIT_RESULT_TEXT == "Waiting: the bridge will update the next time Zotero starts."


def test_the_owner_wording_of_the_buttons_and_the_force_confirmation_is_exact():
    assert POLITE_BUTTON_TEXT == "Close Zotero politely, then install"
    assert FORCE_BUTTON_TEXT == "Force close Zotero, then install"
    assert WAIT_BUTTON_TEXT == "Wait until Zotero restarts"
    assert CANCEL_BUTTON_TEXT == "Cancel"
    assert RETRY_BUTTON_TEXT == "I will close Zotero myself, then try again"
    assert FORCE_CONFIRM_TEXT == "Force close ends Zotero immediately. Anything unsaved in Zotero is lost. Continue?"


def test_the_docs_describe_the_force_choice_and_never_say_force_is_not_used():
    text = " ".join([zotero_bridge_client.__doc__ or "", zotero_panel.__doc__ or "", BRIDGE_DIALOG_DETAIL])
    for stale in ("never forces", "never a forced", "forced kill", "never forced"):
        assert stale not in text
    assert "Force close" in BRIDGE_DIALOG_DETAIL


# ----------------------------------------------------------------- the panel and its dialogs


def _panel(qtbot, bridge=None, local_url=None, ask_update=None):
    panel = ZoteroPanel(client=FakeZotero(), bridge=bridge, local_url=local_url, ask_update=ask_update)
    qtbot.addWidget(panel)
    return panel


def _shown(qtbot, bridge, local_url=lambda: LOCAL, ask_update=None):
    panel = _panel(qtbot, bridge, local_url, ask_update)
    panel.show()
    qtbot.waitExposed(panel)
    _wait_bridge_checked(qtbot, panel)
    return panel


def _named(panel, cls, name):
    widget = panel.findChild(cls, name)
    assert widget is not None, name
    return widget


def _text(panel, name):
    return _named(panel, QLabel, name).text()


def _button(panel):
    return _named(panel, QPushButton, "zotero-bridge-update")


def _wait_bridge_checked(qtbot, panel):
    refresh = _named(panel, QPushButton, "zotero-refresh")
    qtbot.waitUntil(lambda: refresh.isEnabled())
    qtbot.waitUntil(lambda: _text(panel, "zotero-bridge") != BRIDGE_CHECKING_TEXT)


def _all_texts(panel):
    labels = [label.text() for label in panel.findChildren(QLabel)]
    buttons = [button.text() for button in panel.findChildren(QPushButton)]
    return labels + buttons


def _describe(box):
    """What the owner would see on one dialog: its buttons in order, its default and escape button, and its text."""
    default = box.defaultButton()
    escape = box.escapeButton()
    return {
        "buttons": [button.text() for button in box.buttons()],
        "default": default.text() if default is not None else None,
        "escape": escape.text() if escape is not None else None,
        "text": box.text(),
        "detail": box.informativeText(),
    }


class DialogAnswers:
    """Stands in for each modal dialog's exec, one call per dialog, in order. Each call records the dialog, then clicks
    the next label it was given. A label that is not on the dialog is recorded as missing. A dialog with no label left
    is recorded as unexpected and is cancelled, so a test can assert the exact dialogs that opened."""

    def __init__(self, labels):
        self.labels = list(labels)
        self.shown = []
        self.missing = []
        self.unexpected = 0

    def run(self, box):
        self.shown.append(_describe(box))
        if self.labels:
            label = self.labels.pop(0)
        else:
            self.unexpected += 1
            label = CANCEL_BUTTON_TEXT
        for button in box.buttons():
            if button.text() == label:
                button.click()
                return 0
        self.missing.append(label)
        return 0


def _dialogs(monkeypatch, *labels):
    answers = DialogAnswers(labels)
    monkeypatch.setattr(QMessageBox, "exec", lambda self: answers.run(self))
    return answers


def test_without_a_bridge_the_card_has_no_bridge_request_line_button_or_result(qtbot):
    """Without a bridge the card is what it was before the bridge: no request, no bridge line, no bridge controls."""
    panel = _panel(qtbot)
    panel.show()
    qtbot.waitExposed(panel)
    qtbot.waitUntil(lambda: _named(panel, QPushButton, "zotero-refresh").isEnabled())
    assert _text(panel, "zotero-bridge") == ""
    assert panel.findChild(QPushButton, "zotero-bridge-update") is None
    assert panel.findChild(QLabel, "zotero-bridge-result") is None


def test_the_bridge_line_shows_the_state_after_the_first_check(qtbot):
    panel = _shown(qtbot, FakeBridge(status=UPDATE_READY))
    assert _text(panel, "zotero-bridge") == bridge_status_text(UPDATE_READY)


def test_the_bridge_is_checked_on_show_and_again_on_refresh_off_the_ui_thread(qtbot):
    bridge = FakeBridge(status=CURRENT)
    panel = _shown(qtbot, bridge)
    assert bridge.names() == ["status"]
    _named(panel, QPushButton, "zotero-refresh").click()
    qtbot.waitUntil(lambda: bridge.names().count("status") == 2)
    _wait_bridge_checked(qtbot, panel)
    assert all(thread is not threading.main_thread() for thread in bridge.threads)


@pytest.mark.parametrize("status", [CURRENT, WAIT_PENDING, BridgeStatus(STATE_ERROR, reason="x")])
def test_the_update_button_is_disabled_for_current_waiting_and_error(qtbot, status):
    panel = _shown(qtbot, FakeBridge(status=status))
    assert not _button(panel).isEnabled()


@pytest.mark.parametrize("status", [UPDATE_READY, UPDATE_READY_ZOTERO_OPEN, NOT_INSTALLED])
def test_the_update_button_is_enabled_for_update_ready_zotero_open_with_update_and_not_installed(qtbot, status):
    panel = _shown(qtbot, FakeBridge(status=status))
    assert _button(panel).isEnabled()
    assert _button(panel).text() == UPDATE_BUTTON_TEXT


def test_the_first_dialog_has_four_buttons_in_the_owner_order_and_cancel_is_the_default_and_escape(qtbot):
    box, polite, force, wait, cancel = bridge_update_dialog(None)
    qtbot.addWidget(box)
    assert [button.text() for button in box.buttons()] == [
        POLITE_BUTTON_TEXT,
        FORCE_BUTTON_TEXT,
        WAIT_BUTTON_TEXT,
        CANCEL_BUTTON_TEXT,
    ]
    assert box.defaultButton() is cancel
    assert box.escapeButton() is cancel
    assert box.clickedButton() is None
    assert len({polite, force, wait, cancel}) == 4


def test_the_first_dialog_text_says_what_each_choice_does(qtbot):
    box, _polite, _force, _wait, _cancel = bridge_update_dialog(None)
    qtbot.addWidget(box)
    assert box.text() == BRIDGE_DIALOG_TEXT
    detail = box.informativeText()
    assert detail == BRIDGE_DIALOG_DETAIL
    assert "normal close" in detail
    assert "up to a minute" in detail
    assert "force ends Zotero at once" in detail
    assert "Anything unsaved in Zotero is lost" in detail
    assert "this closes nothing" in detail
    assert "Cancel: nothing is closed, nothing is updated and nothing is sent." in detail


def test_the_dialog_text_is_plain_and_selectable(qtbot):
    box, _polite, _force, _wait, _cancel = bridge_update_dialog(None)
    qtbot.addWidget(box)
    assert box.textFormat() == Qt.TextFormat.PlainText
    for label in box.findChildren(QLabel):
        assert label.textFormat() == Qt.TextFormat.PlainText
        assert (label.textInteractionFlags() & SELECTABLE_TEXT) == SELECTABLE_TEXT


@pytest.mark.parametrize(
    "label, choice",
    [
        (POLITE_BUTTON_TEXT, CHOICE_POLITE),
        (FORCE_BUTTON_TEXT, CHOICE_FORCE),
        (WAIT_BUTTON_TEXT, CHOICE_WAIT),
        (CANCEL_BUTTON_TEXT, None),
    ],
)
def test_each_first_dialog_button_maps_to_its_choice(monkeypatch, label, choice):
    _dialogs(monkeypatch, label)
    assert ask_bridge_update(None) == choice


def test_a_closed_first_dialog_without_a_choice_is_no_choice(monkeypatch):
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    assert ask_bridge_update(None) is None


@pytest.mark.parametrize(
    "label, choice",
    [(FORCE_BUTTON_TEXT, FOLLOW_FORCE), (RETRY_BUTTON_TEXT, FOLLOW_RETRY), (CANCEL_BUTTON_TEXT, None)],
)
def test_each_after_polite_button_maps_to_its_follow_up(monkeypatch, label, choice):
    _dialogs(monkeypatch, label)
    assert ask_after_polite_failure(None) == choice


def test_cancel_sends_nothing_and_changes_nothing(qtbot, monkeypatch):
    bridge = FakeBridge(status=UPDATE_READY)
    panel = _shown(qtbot, bridge)
    before = _text(panel, "zotero-bridge-result")
    answers = _dialogs(monkeypatch, CANCEL_BUTTON_TEXT)
    _button(panel).click()
    assert len(answers.shown) == 1
    assert "update" not in bridge.names()
    assert _text(panel, "zotero-bridge-result") == before
    assert _button(panel).isEnabled()


def test_polite_sends_only_the_polite_body_and_shows_the_core_line(qtbot, monkeypatch):
    transport = Transport(
        get=_json_reply(_answer(STATE_UPDATE_READY)),
        post=_json_reply({"message": "Zotero closed and the bridge updated."}),
    )
    panel = _shown(qtbot, RealBridge(transport))
    answers = _dialogs(monkeypatch, POLITE_BUTTON_TEXT)
    _button(panel).click()
    assert _text(panel, "zotero-bridge-result") == BRIDGE_SENDING_TEXT
    assert not _button(panel).isEnabled()
    qtbot.waitUntil(lambda: _text(panel, "zotero-bridge-result") == "Zotero closed and the bridge updated.")
    assert _posts(transport) == [POLITE_BODY]
    assert len(answers.shown) == 1
    assert answers.shown[0]["buttons"] == [POLITE_BUTTON_TEXT, FORCE_BUTTON_TEXT, WAIT_BUTTON_TEXT, CANCEL_BUTTON_TEXT]


def test_polite_success_with_no_line_shows_the_fixed_sentence(qtbot, monkeypatch):
    bridge = FakeBridge(status=UPDATE_READY, update=BridgeResult(True, line=""))
    panel = _shown(qtbot, bridge)
    _dialogs(monkeypatch, POLITE_BUTTON_TEXT)
    _button(panel).click()
    qtbot.waitUntil(lambda: _text(panel, "zotero-bridge-result") == BRIDGE_DONE_TEXT)


def test_force_sends_the_force_body_only_after_the_second_dialog_says_yes(qtbot, monkeypatch):
    transport = Transport(
        get=_json_reply(_answer(STATE_UPDATE_READY)),
        post=_json_reply({"message": "Zotero was closed by force and the bridge updated."}),
    )
    panel = _shown(qtbot, RealBridge(transport))
    answers = _dialogs(monkeypatch, FORCE_BUTTON_TEXT, YES_TEXT)
    _button(panel).click()
    qtbot.waitUntil(lambda: _text(panel, "zotero-bridge-result") == FORCED_LINE)
    assert _posts(transport) == [FORCE_BODY]
    assert answers.missing == [] and answers.unexpected == 0
    assert [shown["text"] for shown in answers.shown] == [BRIDGE_DIALOG_TEXT, FORCE_CONFIRM_TEXT]
    second = answers.shown[1]
    assert set(second["buttons"]) == {YES_TEXT, NO_TEXT}
    assert second["default"] == NO_TEXT
    assert second["escape"] == NO_TEXT


def test_force_sends_nothing_when_the_second_dialog_is_no(qtbot, monkeypatch):
    transport = Transport(get=_json_reply(_answer(STATE_UPDATE_READY)), post=_json_reply({}))
    panel = _shown(qtbot, RealBridge(transport))
    before = _text(panel, "zotero-bridge-result")
    answers = _dialogs(monkeypatch, FORCE_BUTTON_TEXT, NO_TEXT)
    _button(panel).click()
    assert len(answers.shown) == 2
    assert _posts(transport) == []
    assert _text(panel, "zotero-bridge-result") == before
    assert _button(panel).isEnabled()


def test_wait_sends_only_the_wait_body_and_shows_the_waiting_line(qtbot, monkeypatch):
    transport = Transport(
        get=_json_reply(_answer(STATE_UPDATE_READY)),
        post=_json_reply({"message": "a different core line"}),
    )
    panel = _shown(qtbot, RealBridge(transport))
    _dialogs(monkeypatch, WAIT_BUTTON_TEXT)
    _button(panel).click()
    qtbot.waitUntil(lambda: _text(panel, "zotero-bridge-result") == WAIT_RESULT_TEXT)
    assert _posts(transport) == [WAIT_BODY]


def test_a_failed_force_shows_its_plain_reason_and_offers_no_follow_up(qtbot, monkeypatch):
    body = json.dumps({"error": {"code": "bridge_failed", "message": SENTINEL}}).encode("utf-8")
    transport = Transport(get=_json_reply(_answer(STATE_UPDATE_READY)), post=Reply(500, body))
    panel = _shown(qtbot, RealBridge(transport))
    answers = _dialogs(monkeypatch, FORCE_BUTTON_TEXT, YES_TEXT)
    _button(panel).click()
    qtbot.waitUntil(lambda: "bridge_failed" in _text(panel, "zotero-bridge-result"))
    assert len(answers.shown) == 2
    assert _posts(transport) == [FORCE_BODY]
    assert _button(panel).isEnabled()


def test_a_failed_wait_shows_its_plain_reason_and_the_button_stays_usable(qtbot, monkeypatch):
    reason = "The local Meridian server could not update the bridge (HTTP 409). Server code: bridge_busy."
    bridge = FakeBridge(status=UPDATE_READY, update=BridgeResult(False, reason=reason))
    panel = _shown(qtbot, bridge)
    _dialogs(monkeypatch, WAIT_BUTTON_TEXT)
    _button(panel).click()
    qtbot.waitUntil(lambda: _text(panel, "zotero-bridge-result") == reason)
    assert _button(panel).isEnabled()


def test_a_polite_close_that_leaves_zotero_open_says_so_and_offers_force_retry_or_cancel(qtbot, monkeypatch):
    transport = Transport(get=_json_reply(_answer(STATE_UPDATE_READY)), post=Reply(409, STILL_OPEN_BODY))
    panel = _shown(qtbot, RealBridge(transport))
    answers = _dialogs(monkeypatch, POLITE_BUTTON_TEXT, CANCEL_BUTTON_TEXT)
    _button(panel).click()
    qtbot.waitUntil(lambda: _text(panel, "zotero-bridge-result") == ZOTERO_DID_NOT_CLOSE_MESSAGE)
    assert _posts(transport) == [POLITE_BODY]
    assert len(answers.shown) == 2
    follow_up = answers.shown[1]
    assert follow_up["text"] == AFTER_POLITE_TEXT
    assert follow_up["buttons"] == [FORCE_BUTTON_TEXT, RETRY_BUTTON_TEXT, CANCEL_BUTTON_TEXT]
    assert follow_up["default"] == CANCEL_BUTTON_TEXT
    assert follow_up["escape"] == CANCEL_BUTTON_TEXT
    assert _button(panel).isEnabled()


def test_after_a_polite_close_that_did_not_close_try_again_sends_polite_again(qtbot, monkeypatch):
    transport = Transport(
        get=_json_reply(_answer(STATE_UPDATE_READY)),
        post=[Reply(409, STILL_OPEN_BODY), _json_reply({"message": "Zotero closed and the bridge updated."})],
    )
    panel = _shown(qtbot, RealBridge(transport))
    _dialogs(monkeypatch, POLITE_BUTTON_TEXT, RETRY_BUTTON_TEXT)
    _button(panel).click()
    qtbot.waitUntil(lambda: _text(panel, "zotero-bridge-result") == "Zotero closed and the bridge updated.")
    assert _posts(transport) == [POLITE_BODY, POLITE_BODY]


def test_after_a_polite_close_that_did_not_close_force_goes_through_the_second_dialog(qtbot, monkeypatch):
    transport = Transport(
        get=_json_reply(_answer(STATE_UPDATE_READY)),
        post=[Reply(409, STILL_OPEN_BODY), _json_reply({"message": FORCED_LINE})],
    )
    panel = _shown(qtbot, RealBridge(transport))
    answers = _dialogs(monkeypatch, POLITE_BUTTON_TEXT, FORCE_BUTTON_TEXT, YES_TEXT)
    _button(panel).click()
    qtbot.waitUntil(lambda: _text(panel, "zotero-bridge-result") == FORCED_LINE)
    assert _posts(transport) == [POLITE_BODY, FORCE_BODY]
    assert [shown["text"] for shown in answers.shown][2] == FORCE_CONFIRM_TEXT
    assert answers.shown[2]["default"] == NO_TEXT


def test_after_a_polite_close_that_did_not_close_force_answered_no_sends_nothing_more(qtbot, monkeypatch):
    transport = Transport(get=_json_reply(_answer(STATE_UPDATE_READY)), post=[Reply(409, STILL_OPEN_BODY)])
    panel = _shown(qtbot, RealBridge(transport))
    answers = _dialogs(monkeypatch, POLITE_BUTTON_TEXT, FORCE_BUTTON_TEXT, NO_TEXT)
    _button(panel).click()
    qtbot.waitUntil(lambda: _text(panel, "zotero-bridge-result") == ZOTERO_DID_NOT_CLOSE_MESSAGE)
    assert len(answers.shown) == 3
    assert _posts(transport) == [POLITE_BODY]
    assert _button(panel).isEnabled()


def test_a_failed_polite_for_another_reason_offers_no_follow_up(qtbot, monkeypatch):
    body = json.dumps({"error": {"code": "bridge_failed", "message": SENTINEL}}).encode("utf-8")
    transport = Transport(get=_json_reply(_answer(STATE_UPDATE_READY)), post=Reply(500, body))
    panel = _shown(qtbot, RealBridge(transport))
    answers = _dialogs(monkeypatch, POLITE_BUTTON_TEXT)
    _button(panel).click()
    qtbot.waitUntil(lambda: "bridge_failed" in _text(panel, "zotero-bridge-result"))
    assert len(answers.shown) == 1
    assert answers.unexpected == 0
    assert _posts(transport) == [POLITE_BODY]


def test_a_non_loopback_local_url_is_refused_with_no_request_from_the_panel(qtbot):
    transport = Transport(get=_json_reply(_answer(STATE_CURRENT)), post=_json_reply({}))
    panel = _shown(qtbot, RealBridge(transport), local_url=lambda: REMOTE)
    assert transport.calls == []
    assert "must be 127.0.0.1, localhost or ::1" in _text(panel, "zotero-bridge")
    assert not _button(panel).isEnabled()


def test_the_token_text_never_appears_in_any_panel_text(qtbot, monkeypatch):
    body = json.dumps({"error": {"code": "bridge_failed", "message": SENTINEL}}).encode("utf-8")
    transport = Transport(get=_json_reply(_answer(STATE_UPDATE_READY)), post=Reply(500, body))
    panel = _shown(qtbot, RealBridge(transport))
    _dialogs(monkeypatch, POLITE_BUTTON_TEXT)
    _button(panel).click()
    qtbot.waitUntil(lambda: "bridge_failed" in _text(panel, "zotero-bridge-result"))
    assert transport.calls[-1]["body"] == POLITE_BODY
    for text in _all_texts(panel):
        assert SENTINEL not in text
    box, _polite, _force, _wait, _cancel = bridge_update_dialog(None)
    qtbot.addWidget(box)
    assert SENTINEL not in box.text() + box.informativeText()


def test_a_local_url_reader_that_raises_shows_the_class_name_and_no_detail(qtbot):
    def broken_url():
        raise RuntimeError("private detail in the settings")

    panel = _shown(qtbot, FakeBridge(status=UPDATE_READY), local_url=broken_url)
    line = _text(panel, "zotero-bridge")
    assert line == BRIDGE_ERROR_TEXT.format(CHECK_FAILED_TEXT.format("RuntimeError"))
    assert "private detail" not in line
    assert not _button(panel).isEnabled()


def test_an_update_that_raises_shows_the_class_name_and_no_detail(qtbot, monkeypatch):
    bridge = FakeBridge(status=UPDATE_READY, update=RuntimeError("private detail"))
    panel = _shown(qtbot, bridge)
    _dialogs(monkeypatch, POLITE_BUTTON_TEXT)
    _button(panel).click()
    qtbot.waitUntil(lambda: _text(panel, "zotero-bridge-result") == CHECK_FAILED_TEXT.format("RuntimeError"))
    assert "private detail" not in _text(panel, "zotero-bridge-result")
