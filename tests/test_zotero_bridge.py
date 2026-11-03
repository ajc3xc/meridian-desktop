"""Save to Zotero through the Meridian bridge: the lines, the batch rules, the grant flow, the token, the status and the panel.

Every request goes to a FakeBridge, which records each one. conftest refuses any real request to 127.0.0.1:23125 and
fails a test that tries to write there, so no test reaches the owner's Zotero. Offscreen Qt, no network.
"""

import datetime as dt
import json
import re
import threading
from pathlib import Path

import pytest
from PySide6.QtWidgets import QLabel, QPlainTextEdit, QPushButton

from meridian_desktop.ui.zotero_panel import ZoteroPanel
from meridian_desktop.zotero_bridge import (
    ANSWERING,
    ANSWERING_STATUS,
    BRIDGE_OFF,
    ERROR,
    GRANT_EXPIRED,
    NO_TOKEN_STATUS,
    NO_TOKEN_TEXT,
    NOT_ANSWERING_STATUS,
    NOT_ANSWERING_TEXT,
    NOT_OPEN_FOR_WRITES,
    SAVED,
    SAVED_TEXT,
    UNAUTHORIZED,
    UNAUTHORIZED_STATUS,
    BridgeInputError,
    BridgeStatus,
    ReadingItem,
    SaveResult,
    bridge_status,
    parse_lines,
    read_token,
    save_items,
    token_file_path,
)
from meridian_desktop.zotero_client import (
    API_NOT_RUNNING,
    NOT_RUNNING_MESSAGE,
    ApiCheck,
    Reply,
    ZoteroError,
    ZoteroStatus,
)
from meridian_desktop import zotero_bridge

TOKEN = "zb-test-token-7f3c"
WRITE_KEY = "zb-write-key-9a1d"
GRANT_ID = "grant-test-1"
NOW = dt.datetime(2026, 11, 3, 5, 0, tzinfo=dt.timezone.utc)
EXPIRES = NOW + dt.timedelta(hours=1)
ITEMS = [ReadingItem("First paper", "https://example.org/one"), ReadingItem("Second paper", "https://example.org/two")]
THREE_ITEMS = ITEMS + [ReadingItem("Third paper", "https://example.org/three")]


def _json(payload) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _iso(moment: dt.datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _error(code: str) -> bytes:
    return _json({"error": {"code": code, "message": "fixed text"}})


def _token_file(tmp_path: Path, text: str = TOKEN + "\n") -> Path:
    path = tmp_path / "zotero_bridge" / "api_token"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _clock(*moments: dt.datetime):
    """Each call returns the next moment. The last one repeats."""
    queue = list(moments)

    def clock() -> dt.datetime:
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return clock


class FakeBridge:
    """Answers the bridge's routes from a script, and records every request as (method, path, headers, body)."""

    def __init__(self, *, grant="granted", grant_refusal=None, writes=(), expires_at=EXPIRES, status=200):
        self.calls: list[tuple[str, str, dict, bytes | None]] = []
        self.grant = grant
        self.grant_refusal = grant_refusal
        self.writes = list(writes)
        self.expires_at = expires_at
        self.status = status
        self.written = 0

    def __call__(self, method, path, headers, body, timeout):
        self.calls.append((method, path, dict(headers), body))
        if method == "GET" and path == "/v1/status":
            if self.status == 200:
                return Reply(200, _json({"plugin_version": "0.1.6"}))
            return Reply(self.status, _json({}))
        if method == "POST" and path == "/v1/grants":
            if self.grant_refusal is not None:
                status, code = self.grant_refusal
                return Reply(status, _error(code))
            return Reply(202, _json({"grant_id": GRANT_ID, "status": self.grant}))
        if method == "GET" and path == f"/v1/grants/{GRANT_ID}":
            return Reply(
                200,
                _json(
                    {
                        "grant_id": GRANT_ID,
                        "status": "granted",
                        "expires_at": _iso(self.expires_at),
                        "write_key": WRITE_KEY,
                    }
                ),
            )
        if method == "DELETE" and path == f"/v1/grants/{GRANT_ID}":
            return Reply(200, _json({"grant_id": GRANT_ID, "status": "revoked"}))
        if method == "POST" and path == "/v1/items":
            if self.writes:
                reply = self.writes.pop(0)
                if isinstance(reply, Exception):
                    raise reply
                return reply
            self.written += 1
            return Reply(201, _json({"key": f"ITEM{self.written:04d}"}))
        raise AssertionError(f"unexpected request {method} {path}")


def _writes(bridge: FakeBridge) -> list:
    return [call for call in bridge.calls if call[0] == "POST" and call[1] == "/v1/items"]


def _paths(bridge: FakeBridge) -> list[tuple[str, str]]:
    return [(method, path) for method, path, _headers, _body in bridge.calls]


# ----------------------------------------------------------- reading the lines


def test_lines_become_items_and_the_url_is_the_text_after_the_last_bar():
    items = parse_lines("A paper | https://example.org/a\n\n  Title with a | bar | https://example.org/b  \n")
    assert items == [
        ReadingItem("A paper", "https://example.org/a"),
        ReadingItem("Title with a | bar", "https://example.org/b"),
    ]


def test_a_line_without_a_bar_is_refused_with_its_number():
    with pytest.raises(BridgeInputError, match="Line 2 needs a title and a URL"):
        parse_lines("ok | https://example.org/a\nno separator here")


@pytest.mark.parametrize(
    "text, reason",
    [
        (" | https://example.org/a", "Line 1: the title is empty"),
        ("A | ftp://example.org/a", "the URL must start with http:// or https://"),
        ("A | javascript:alert(1)", "the URL must start with http:// or https://"),
        ("A | http://", "the URL must start with http:// or https://"),
        ("A | https://exa mple.org", "the URL must start with http:// or https://"),
    ],
)
def test_a_bad_line_is_refused_before_anything_is_sent(text, reason):
    with pytest.raises(BridgeInputError, match=re.escape(reason)):
        parse_lines(text)


def test_blank_input_and_too_many_lines_are_refused():
    with pytest.raises(BridgeInputError, match="Type at least one line"):
        parse_lines("\n  \n")
    many = "\n".join(f"Title {n} | https://example.org/{n}" for n in range(zotero_bridge.MAX_ITEMS + 1))
    with pytest.raises(BridgeInputError, match="Save at most 50 items"):
        parse_lines(many)


# ----------------------------------------------------------- the batch


@pytest.mark.parametrize(
    "item",
    [
        ReadingItem("", "https://example.org/a"),
        ReadingItem("A", "file:///etc/passwd"),
        ReadingItem("A", "https://"),
        ReadingItem("A\nB", "https://example.org/a"),
    ],
)
def test_a_bad_item_stops_the_batch_before_any_request(tmp_path, item):
    bridge = FakeBridge()
    result = save_items([ITEMS[0], item], transport=bridge, token_path=_token_file(tmp_path))
    assert result.state == ERROR
    assert result.saved == 0
    assert result.message.startswith("Item 2: ")
    assert "Nothing was saved and no request was sent." in result.message
    assert bridge.calls == []


def test_an_empty_batch_and_an_oversized_batch_are_refused_before_any_request(tmp_path):
    bridge = FakeBridge()
    assert save_items([], transport=bridge, token_path=_token_file(tmp_path)).state == ERROR
    too_many = [ReadingItem(f"T{n}", "https://example.org/x") for n in range(zotero_bridge.MAX_ITEMS + 1)]
    assert save_items(too_many, transport=bridge, token_path=_token_file(tmp_path)).state == ERROR
    assert bridge.calls == []


@pytest.mark.parametrize("contents", [None, "", "   \n"])
def test_no_usable_token_is_bridge_off_and_sends_nothing(tmp_path, contents):
    path = tmp_path / "zotero_bridge" / "api_token"
    if contents is not None:
        _token_file(tmp_path, contents)
    bridge = FakeBridge()
    result = save_items(ITEMS, transport=bridge, token_path=path)
    assert result == SaveResult(BRIDGE_OFF, 0, NO_TOKEN_TEXT)
    assert bridge.calls == []


def test_without_a_transport_and_without_a_token_nothing_is_sent(tmp_path):
    result = save_items(ITEMS, token_path=tmp_path / "missing" / "api_token")
    assert result.state == BRIDGE_OFF
    assert result.saved == 0


@pytest.mark.parametrize(
    "answer, phrase",
    [
        ("pending", "asking a person to answer a prompt"),
        ("denied", "a person refused it"),
    ],
)
def test_a_grant_not_granted_at_once_is_cancelled_and_nothing_is_written(tmp_path, answer, phrase):
    bridge = FakeBridge(grant=answer)
    result = save_items(ITEMS, transport=bridge, token_path=_token_file(tmp_path), now=_clock(NOW))
    assert result.state == NOT_OPEN_FOR_WRITES
    assert result.saved == 0
    assert phrase in result.message
    assert _writes(bridge) == []
    assert ("DELETE", f"/v1/grants/{GRANT_ID}") in _paths(bridge)


def test_a_refused_grant_request_reports_the_code_and_has_no_grant_to_cancel(tmp_path):
    bridge = FakeBridge(grant_refusal=(429, "grant_cooldown"))
    result = save_items(ITEMS, transport=bridge, token_path=_token_file(tmp_path))
    assert result.state == NOT_OPEN_FOR_WRITES
    assert "grant_cooldown" in result.message
    assert _writes(bridge) == []
    assert not any(method == "DELETE" for method, _path in _paths(bridge))


def test_a_granted_batch_writes_each_item_with_the_grant_key_and_ends_the_grant(tmp_path):
    bridge = FakeBridge()
    result = save_items(ITEMS, transport=bridge, token_path=_token_file(tmp_path), now=_clock(NOW))
    assert result == SaveResult(SAVED, 2, SAVED_TEXT.format(2))
    writes = _writes(bridge)
    assert len(writes) == 2
    for _method, _path, headers, _body in writes:
        assert headers["Meridian-Write-Grant"] == WRITE_KEY
        assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert json.loads(writes[0][3]) == {
        "item_type": "webpage",
        "fields": {"title": "First paper", "url": "https://example.org/one"},
    }
    assert _paths(bridge)[-1] == ("DELETE", f"/v1/grants/{GRANT_ID}")


def test_the_title_and_url_are_inside_fields_and_never_at_the_top_of_the_body(tmp_path):
    bridge = FakeBridge()
    save_items(ITEMS, transport=bridge, token_path=_token_file(tmp_path), now=_clock(NOW))
    for _method, _path, _headers, body in _writes(bridge):
        payload = json.loads(body)
        assert "title" not in payload
        assert "url" not in payload
        assert set(payload["fields"]) == {"title", "url"}


def test_a_refused_item_stops_the_batch_and_reports_how_many_were_saved(tmp_path):
    bridge = FakeBridge(
        writes=[
            Reply(201, _json({"key": "AAAA0001"})),
            Reply(422, _error("invalid_fields")),
        ]
    )
    result = save_items(THREE_ITEMS, transport=bridge, token_path=_token_file(tmp_path), now=_clock(NOW))
    assert result.state == ERROR
    assert result.saved == 1
    assert result.message == "Saved 1 of 3 item(s). Stopped at item 2: Zotero refused the item (invalid_fields)."
    assert len(_writes(bridge)) == 2
    assert ("DELETE", f"/v1/grants/{GRANT_ID}") in _paths(bridge)


def test_an_expired_grant_stops_the_batch_before_the_next_write(tmp_path):
    bridge = FakeBridge(expires_at=EXPIRES)
    result = save_items(
        ITEMS,
        transport=bridge,
        token_path=_token_file(tmp_path),
        now=_clock(NOW, EXPIRES),
    )
    assert result.state == GRANT_EXPIRED
    assert result.saved == 1
    assert len(_writes(bridge)) == 1
    assert ("DELETE", f"/v1/grants/{GRANT_ID}") in _paths(bridge)


def test_a_grant_about_to_lapse_writes_nothing(tmp_path):
    bridge = FakeBridge(expires_at=NOW + dt.timedelta(seconds=10))
    result = save_items(ITEMS, transport=bridge, token_path=_token_file(tmp_path), now=_clock(NOW))
    assert result.state == GRANT_EXPIRED
    assert result.saved == 0
    assert _writes(bridge) == []


def test_grant_required_mid_batch_is_reported_as_an_expired_grant(tmp_path):
    bridge = FakeBridge(writes=[Reply(201, _json({"key": "AAAA0001"})), Reply(403, _error("grant_required"))])
    result = save_items(ITEMS, transport=bridge, token_path=_token_file(tmp_path), now=_clock(NOW))
    assert result.state == GRANT_EXPIRED
    assert result.saved == 1
    assert result.message.startswith("The write grant ended after 1 of 2 item(s).")


def test_the_bridge_stopping_mid_batch_is_bridge_off(tmp_path):
    bridge = FakeBridge(writes=[Reply(201, _json({"key": "AAAA0001"})), ZoteroError("not_running", NOT_ANSWERING_TEXT)])
    result = save_items(ITEMS, transport=bridge, token_path=_token_file(tmp_path), now=_clock(NOW))
    assert result.state == BRIDGE_OFF
    assert result.saved == 1
    assert result.message == "Saved 1 of 2 item(s). Stopped at item 2: the bridge stopped answering."


def test_no_bridge_answering_at_the_start_is_bridge_off_with_nothing_saved(tmp_path):
    def refused(method, path, headers, body, timeout):
        raise ZoteroError("not_running", NOT_ANSWERING_TEXT)

    result = save_items(ITEMS, transport=refused, token_path=_token_file(tmp_path), now=_clock(NOW))
    assert result == SaveResult(BRIDGE_OFF, 0, NOT_ANSWERING_TEXT)


def test_a_refused_token_at_the_start_is_an_error_with_nothing_saved(tmp_path):
    bridge = FakeBridge(grant_refusal=(401, "unauthorized"))
    result = save_items(ITEMS, transport=bridge, token_path=_token_file(tmp_path), now=_clock(NOW))
    assert result.state == ERROR
    assert result.saved == 0
    assert "refused its token" in result.message


def test_the_save_path_never_trashes_restores_or_deletes_an_item(tmp_path):
    scenarios = [
        FakeBridge(),
        FakeBridge(writes=[Reply(201, _json({"key": "AAAA0001"})), Reply(422, _error("invalid_fields"))]),
        FakeBridge(writes=[Reply(201, _json({"key": "AAAA0001"})), Reply(403, _error("grant_required"))]),
        FakeBridge(grant="pending"),
    ]
    for bridge in scenarios:
        save_items(THREE_ITEMS, transport=bridge, token_path=_token_file(tmp_path), now=_clock(NOW))
        for method, path in _paths(bridge):
            assert "trash" not in path
            assert "restore" not in path
            assert method != "DELETE" or path.startswith("/v1/grants/")


def test_the_token_and_the_write_key_never_appear_in_a_message(tmp_path):
    path = _token_file(tmp_path)
    results = [
        save_items(ITEMS, transport=FakeBridge(), token_path=path, now=_clock(NOW)),
        save_items(
            THREE_ITEMS,
            transport=FakeBridge(writes=[Reply(201, _json({"key": "A"})), Reply(422, _error("invalid_fields"))]),
            token_path=path,
            now=_clock(NOW),
        ),
        save_items(ITEMS, transport=FakeBridge(grant="pending"), token_path=path, now=_clock(NOW)),
        save_items(
            ITEMS,
            transport=FakeBridge(grant_refusal=(401, "unauthorized")),
            token_path=path,
            now=_clock(NOW),
        ),
        save_items(ITEMS, transport=FakeBridge(expires_at=NOW), token_path=path, now=_clock(NOW)),
        bridge_status(transport=FakeBridge(status=401), token_path=path),
        bridge_status(transport=FakeBridge(), token_path=path),
    ]
    for result in results:
        for secret in (TOKEN, WRITE_KEY):
            assert secret not in result.message


# ----------------------------------------------------------- the token file


def test_the_token_file_tolerates_a_byte_order_mark_and_surrounding_whitespace(tmp_path):
    assert read_token(_token_file(tmp_path, "﻿  " + TOKEN + "\r\n")) == TOKEN


def test_a_token_with_a_space_inside_is_not_usable(tmp_path):
    assert read_token(_token_file(tmp_path, "abc def\n")) is None


def test_the_token_path_follows_the_state_folder_override(tmp_path):
    env = {"MERIDIAN_LOCAL_RUNNER_STATE_DIR": str(tmp_path / "runner")}
    assert token_file_path(env) == tmp_path / "runner" / "zotero_bridge" / "api_token"


def test_the_token_path_defaults_under_the_home_folder():
    assert token_file_path({}) == Path.home() / ".meridian" / "local_runner" / "zotero_bridge" / "api_token"


# ----------------------------------------------------------- status


@pytest.mark.parametrize(
    "status, state, phrase",
    [
        (200, ANSWERING, "answering on 127.0.0.1:23125"),
        (401, UNAUTHORIZED, "refused the token"),
        (500, ERROR, "HTTP 500"),
    ],
)
def test_bridge_status_reads_only_and_reports_what_it_sees(tmp_path, status, state, phrase):
    bridge = FakeBridge(status=status)
    result = bridge_status(transport=bridge, token_path=_token_file(tmp_path))
    assert result.state == state
    assert phrase in result.message
    assert [(method, path) for method, path, _h, _b in bridge.calls] == [("GET", "/v1/status")]
    assert TOKEN not in result.message


def test_bridge_status_is_off_without_a_token_and_sends_nothing(tmp_path):
    bridge = FakeBridge()
    assert bridge_status(transport=bridge, token_path=tmp_path / "missing") == BridgeStatus(BRIDGE_OFF, NO_TOKEN_STATUS)
    assert bridge.calls == []


def test_bridge_status_is_off_when_nothing_answers(tmp_path):
    def refused(method, path, headers, body, timeout):
        raise ZoteroError("not_running", NOT_ANSWERING_TEXT)

    result = bridge_status(transport=refused, token_path=_token_file(tmp_path))
    assert result == BridgeStatus(BRIDGE_OFF, NOT_ANSWERING_STATUS)


# ----------------------------------------------------------- the panel


class StubZotero:
    """The Zotero reads the panel makes. None of them is the bridge."""

    def status(self):
        return ZoteroStatus(exe=None, process_running=False, api=ApiCheck(API_NOT_RUNNING, NOT_RUNNING_MESSAGE))

    def probe_api(self):
        return ApiCheck(API_NOT_RUNNING, NOT_RUNNING_MESSAGE)

    def search_items(self, query):
        return []

    def start_zotero(self, exe):
        return ""


class PanelBridge:
    """The bridge as the panel sees it: the real line reader, a fixed status, and a recorded save."""

    def __init__(self, *, status=None, result=None, gate=None):
        self.status_value = status or BridgeStatus(ANSWERING, ANSWERING_STATUS)
        self.result = result or SaveResult(SAVED, 2, SAVED_TEXT.format(2))
        self.gate = gate
        self.batches: list[tuple[list[ReadingItem], bool]] = []

    def parse_lines(self, text):
        return parse_lines(text)

    def bridge_status(self):
        return self.status_value

    def save_items(self, items):
        if self.gate is not None:
            self.gate.wait(5)
        self.batches.append((list(items), threading.current_thread() is threading.main_thread()))
        return self.result


def _save_panel(qtbot, bridge):
    panel = ZoteroPanel(client=StubZotero(), bridge=bridge)
    qtbot.addWidget(panel)
    panel.show()
    qtbot.waitExposed(panel)
    qtbot.waitUntil(lambda: panel.findChild(QPushButton, "zotero-refresh").isEnabled(), timeout=5000)
    return panel


def _named(panel, cls, name):
    widget = panel.findChild(cls, name)
    assert widget is not None, name
    return widget


def _text(panel, name):
    return _named(panel, QLabel, name).text()


def test_the_bridge_state_shows_next_to_the_save_button(qtbot):
    panel = _save_panel(qtbot, PanelBridge(status=BridgeStatus(BRIDGE_OFF, NO_TOKEN_STATUS)))
    qtbot.waitUntil(lambda: _text(panel, "zotero-bridge-state") == NO_TOKEN_STATUS, timeout=5000)


def test_each_line_is_saved_off_the_ui_thread_and_the_result_is_shown(qtbot):
    bridge = PanelBridge()
    panel = _save_panel(qtbot, bridge)
    qtbot.waitUntil(lambda: _text(panel, "zotero-bridge-state") == ANSWERING_STATUS, timeout=5000)
    _named(panel, QPlainTextEdit, "zotero-save-input").setPlainText(
        "First paper | https://example.org/one\nSecond paper | https://example.org/two"
    )
    _named(panel, QPushButton, "zotero-save-button").click()
    qtbot.waitUntil(lambda: _text(panel, "zotero-save-result") == bridge.result.message, timeout=5000)
    items, on_gui_thread = bridge.batches[0]
    assert items == ITEMS
    assert on_gui_thread is False


def test_a_bad_line_is_shown_and_nothing_is_sent(qtbot):
    bridge = PanelBridge()
    panel = _save_panel(qtbot, bridge)
    _named(panel, QPlainTextEdit, "zotero-save-input").setPlainText("no separator here")
    _named(panel, QPushButton, "zotero-save-button").click()
    qtbot.waitUntil(lambda: "Line 1 needs a title and a URL" in _text(panel, "zotero-save-result"), timeout=5000)
    assert bridge.batches == []


def test_the_save_button_is_off_while_a_save_is_running(qtbot):
    gate = threading.Event()
    bridge = PanelBridge(gate=gate)
    panel = _save_panel(qtbot, bridge)
    _named(panel, QPlainTextEdit, "zotero-save-input").setPlainText("First paper | https://example.org/one")
    button = _named(panel, QPushButton, "zotero-save-button")
    button.click()
    qtbot.waitUntil(lambda: not button.isEnabled(), timeout=5000)
    gate.set()
    qtbot.waitUntil(lambda: button.isEnabled(), timeout=5000)
    assert _text(panel, "zotero-save-result") == bridge.result.message
