"""Save to Zotero through the Meridian bridge: the lines, the batch rules, the grant flow, the token, the status, the real
transport and the panel.

Every request goes to a FakeBridge, which records each one. The real transport is run only against a stub server on a
loopback port of its own. conftest refuses any real request to 127.0.0.1:23125 and fails a test that tries to write
there, so no test reaches the owner's Zotero. Offscreen Qt, no network.
"""

import datetime as dt
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from PySide6.QtWidgets import QLabel, QPlainTextEdit, QPushButton

from meridian_desktop.ui.zotero_panel import HELD_RELEASED_TEXT, SAVE_NO_RESULT_TEXT, ZoteroPanel
from meridian_desktop.zotero_bridge import (
    ANSWERING,
    ANSWERING_STATUS,
    BRIDGE_OFF,
    ERROR,
    GRANT_EXPIRED,
    KEY_MAY_STAY_LIVE_TEXT,
    NO_TOKEN_STATUS,
    NO_TOKEN_TEXT,
    NOT_ANSWERING_STATUS,
    NOT_ANSWERING_TEXT,
    NOT_OPEN_CANCEL_FAILED_TEXT,
    NOT_OPEN_FOR_WRITES,
    PROMPT_OPEN_WARNING,
    SAVED,
    SAVED_TEXT,
    TIMEOUT_TEXT,
    UNAUTHORIZED,
    UNKNOWN,
    UNREACHABLE_TEXT,
    BridgeInputError,
    BridgeStatus,
    ReadingItem,
    SaveResult,
    bridge_status,
    bridge_transport,
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
FIVE_ITEMS = [ReadingItem(f"Paper {n}", f"https://example.org/{n}") for n in range(1, 6)]
PENDING_PROMPT = "Zotero is asking a person to answer a prompt"


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
    """Answers the bridge's routes from a script, and records every request as (method, path, headers, body).

    ``expires_at`` may be a datetime, a raw string, or None for a grant with no expiry field. ``grant_error`` and
    ``delete_reply`` may each be a Reply or an exception, which is raised instead.
    """

    def __init__(
        self,
        *,
        grant="granted",
        grant_refusal=None,
        grant_error=None,
        writes=(),
        expires_at=EXPIRES,
        status=200,
        delete_reply=None,
    ):
        self.calls: list[tuple[str, str, dict, bytes | None]] = []
        self.grant = grant
        self.grant_refusal = grant_refusal
        self.grant_error = grant_error
        self.writes = list(writes)
        self.expires_at = expires_at
        self.status = status
        self.delete_reply = delete_reply
        self.written = 0

    def _expiry(self):
        if self.expires_at is None or isinstance(self.expires_at, str):
            return self.expires_at
        return _iso(self.expires_at)

    def __call__(self, method, path, headers, body, timeout):
        self.calls.append((method, path, dict(headers), body))
        if method == "GET" and path == "/v1/status":
            if self.status == 200:
                return Reply(200, _json({"plugin_version": "0.1.6"}))
            return Reply(self.status, _json({}))
        if method == "POST" and path == "/v1/grants":
            if self.grant_error is not None:
                raise self.grant_error
            if self.grant_refusal is not None:
                status, code = self.grant_refusal
                return Reply(status, _error(code))
            return Reply(202, _json({"grant_id": GRANT_ID, "status": self.grant}))
        if method == "GET" and path == f"/v1/grants/{GRANT_ID}":
            payload = {"grant_id": GRANT_ID, "status": "granted", "write_key": WRITE_KEY}
            if self._expiry() is not None:
                payload["expires_at"] = self._expiry()
            return Reply(200, _json(payload))
        if method == "DELETE" and path == f"/v1/grants/{GRANT_ID}":
            reply = self.delete_reply
            if reply is None:
                reply = Reply(200, _json({"grant_id": GRANT_ID, "status": "revoked"}))
            if isinstance(reply, Exception):
                raise reply
            return reply
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


def _deletes(bridge: FakeBridge) -> list[tuple[str, str]]:
    return [pair for pair in _paths(bridge) if pair[0] == "DELETE"]


def _lines(*titles: str) -> str:
    return "\n".join(f"{title} | https://example.org/{title.split()[-1]}" for title in titles)


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
    assert ("Press Deny there." in result.message) == (answer == "pending")
    assert _writes(bridge) == []
    assert ("DELETE", f"/v1/grants/{GRANT_ID}") in _paths(bridge)


def test_a_failed_cancel_of_a_denied_grant_says_no_prompt_is_waiting(tmp_path):
    bridge = FakeBridge(grant="denied", delete_reply=ZoteroError("unreachable", UNREACHABLE_TEXT))
    result = save_items(ITEMS, transport=bridge, token_path=_token_file(tmp_path), now=_clock(NOW))
    assert result.state == NOT_OPEN_FOR_WRITES
    assert "The request could not be cancelled, and no prompt is waiting." in result.message
    assert "Press Deny" not in result.message


def test_a_refused_grant_request_reports_the_code_and_has_no_grant_to_cancel(tmp_path):
    bridge = FakeBridge(grant_refusal=(429, "grant_cooldown"))
    result = save_items(ITEMS, transport=bridge, token_path=_token_file(tmp_path))
    assert result.state == NOT_OPEN_FOR_WRITES
    assert "grant_cooldown" in result.message
    assert "so nothing was opened or cancelled" in result.message
    assert _writes(bridge) == []
    assert _deletes(bridge) == []


def test_a_granted_batch_writes_each_item_with_the_grant_key_and_ends_the_grant(tmp_path):
    bridge = FakeBridge()
    result = save_items(ITEMS, transport=bridge, token_path=_token_file(tmp_path), now=_clock(NOW))
    assert result == SaveResult(SAVED, 2, SAVED_TEXT.format(2), ("First paper", "Second paper"))
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


def test_a_refused_item_stops_the_batch_and_names_what_landed_and_what_did_not(tmp_path):
    bridge = FakeBridge(writes=[Reply(201, _json({"key": "AAAA0001"})), Reply(422, _error("invalid_fields"))])
    result = save_items(THREE_ITEMS, transport=bridge, token_path=_token_file(tmp_path), now=_clock(NOW))
    assert result.state == ERROR
    assert result.saved == 1
    assert result.landed == ("First paper",)
    assert result.message == (
        "Saved 1 of 3 item(s). Stopped at item 2: Zotero refused the item (invalid_fields). "
        "Landed in Zotero: First paper. Not saved: items 2 to 3. Check Zotero before you save again."
    )
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
    assert result.landed == ("First paper",)
    assert len(_writes(bridge)) == 1
    assert ("DELETE", f"/v1/grants/{GRANT_ID}") in _paths(bridge)


def test_a_grant_about_to_lapse_writes_nothing(tmp_path):
    bridge = FakeBridge(expires_at=NOW + dt.timedelta(seconds=10))
    result = save_items(ITEMS, transport=bridge, token_path=_token_file(tmp_path), now=_clock(NOW))
    assert result.state == GRANT_EXPIRED
    assert result.saved == 0
    assert result.message == (
        "The write grant ended after 0 of 2 item(s). Nothing landed in Zotero. Not saved: items 1 to 2. "
        "Check Zotero before you save again."
    )
    assert _writes(bridge) == []


def test_grant_required_mid_batch_is_reported_as_an_expired_grant(tmp_path):
    bridge = FakeBridge(writes=[Reply(201, _json({"key": "AAAA0001"})), Reply(403, _error("grant_required"))])
    result = save_items(ITEMS, transport=bridge, token_path=_token_file(tmp_path), now=_clock(NOW))
    assert result.state == GRANT_EXPIRED
    assert result.saved == 1
    assert result.message.startswith("The write grant ended after 1 of 2 item(s).")
    assert "Landed in Zotero: First paper." in result.message
    assert "Not saved: item 2." in result.message


def test_the_expired_and_stopped_results_name_the_landed_titles_and_the_items_not_saved(tmp_path):
    expired = save_items(
        FIVE_ITEMS,
        transport=FakeBridge(writes=[Reply(201, _json({"key": "A"})), Reply(201, _json({"key": "B"})), Reply(403, _error("grant_required"))]),
        token_path=_token_file(tmp_path),
        now=_clock(NOW),
    )
    assert expired.state == GRANT_EXPIRED
    assert expired.landed == ("Paper 1", "Paper 2")
    assert expired.message == (
        "The write grant ended after 2 of 5 item(s). Landed in Zotero: Paper 1; Paper 2. "
        "Not saved: items 3 to 5. Check Zotero before you save again."
    )

    stopped = save_items(
        FIVE_ITEMS,
        transport=FakeBridge(writes=[Reply(201, _json({"key": "A"})), Reply(201, _json({"key": "B"})), Reply(422, _error("invalid_fields"))]),
        token_path=_token_file(tmp_path),
        now=_clock(NOW),
    )
    assert stopped.state == ERROR
    assert stopped.landed == ("Paper 1", "Paper 2")
    assert stopped.message == (
        "Saved 2 of 5 item(s). Stopped at item 3: Zotero refused the item (invalid_fields). "
        "Landed in Zotero: Paper 1; Paper 2. Not saved: items 3 to 5. Check Zotero before you save again."
    )


def test_an_expiry_before_the_first_write_says_nothing_landed(tmp_path):
    result = save_items(
        ITEMS,
        transport=FakeBridge(expires_at=NOW + dt.timedelta(seconds=10)),
        token_path=_token_file(tmp_path),
        now=_clock(NOW),
    )
    assert result.landed == ()
    assert "Nothing landed in Zotero." in result.message
    assert "Check Zotero before you save again." in result.message


@pytest.mark.parametrize("code", ["timeout", "unreachable", "too_large"])
def test_an_item_write_with_no_answer_is_unknown_not_not_saved(tmp_path, code):
    bridge = FakeBridge(writes=[Reply(201, _json({"key": "AAAA0001"})), ZoteroError(code, "fixed text")])
    result = save_items(THREE_ITEMS, transport=bridge, token_path=_token_file(tmp_path), now=_clock(NOW))
    assert result.state == UNKNOWN
    assert result.saved == 1
    assert result.landed == ("First paper",)
    assert result.message == (
        "Saved 1 of 3 item(s). Item 2 may or may not have been saved. Check Zotero for it before you retry. "
        "Landed in Zotero: First paper. Not sent: item 3."
    )
    assert "Not saved" not in result.message
    assert len(_writes(bridge)) == 2
    assert ("DELETE", f"/v1/grants/{GRANT_ID}") in _paths(bridge)


def test_an_unknown_last_item_is_followed_by_no_items(tmp_path):
    bridge = FakeBridge(
        writes=[Reply(201, _json({"key": "A"})), Reply(201, _json({"key": "B"})), ZoteroError("timeout", TIMEOUT_TEXT)]
    )
    result = save_items(THREE_ITEMS, transport=bridge, token_path=_token_file(tmp_path), now=_clock(NOW))
    assert result.state == UNKNOWN
    assert "There were no items after it." in result.message
    assert len(_writes(bridge)) == 3


def test_an_unknown_first_item_says_nothing_is_confirmed_to_have_landed(tmp_path):
    bridge = FakeBridge(writes=[ZoteroError("timeout", TIMEOUT_TEXT)])
    result = save_items(ITEMS, transport=bridge, token_path=_token_file(tmp_path), now=_clock(NOW))
    assert result.state == UNKNOWN
    assert result.saved == 0
    assert result.landed == ()
    assert result.message == (
        "Saved 0 of 2 item(s). Item 1 may or may not have been saved. Check Zotero for it before you retry. "
        "Nothing is confirmed to have landed. Not sent: item 2."
    )


def test_an_unexpected_error_mid_batch_still_returns_the_items_that_landed(tmp_path):
    bridge = FakeBridge(writes=[Reply(201, _json({"key": "AAAA0001"})), TimeoutError("timed out")])
    result = save_items(ITEMS, transport=bridge, token_path=_token_file(tmp_path), now=_clock(NOW))
    assert result.state == UNKNOWN
    assert result.saved == 1
    assert result.landed == ("First paper",)
    assert "so item 2 may or may not have been saved" in result.message
    assert "Landed in Zotero: First paper." in result.message
    assert KEY_MAY_STAY_LIVE_TEXT not in result.message


def test_a_delete_that_raises_in_the_finally_still_returns_the_items_that_landed(tmp_path):
    bridge = FakeBridge(delete_reply=TimeoutError("timed out"))
    result = save_items(ITEMS, transport=bridge, token_path=_token_file(tmp_path), now=_clock(NOW))
    assert result.state == SAVED
    assert result.landed == ("First paper", "Second paper")
    assert result.message == f"{SAVED_TEXT.format(2)} {KEY_MAY_STAY_LIVE_TEXT}"


def test_a_timed_out_error_body_keeps_the_status_and_the_items_that_landed(monkeypatch, tmp_path, save_stub):
    monkeypatch.setattr(zotero_bridge, "TIMEOUT", 0.3)
    result = save_items(ITEMS, transport=bridge_transport(save_stub), token_path=_token_file(tmp_path))
    assert result.state == ERROR
    assert result.saved == 1
    assert result.landed == ("First paper",)
    assert result.message == (
        "Saved 1 of 2 item(s). Stopped at item 2: Zotero answered HTTP 500. Landed in Zotero: First paper. "
        "Not saved: item 2. Check Zotero before you save again."
    )


def test_the_bridge_stopping_mid_batch_is_bridge_off(tmp_path):
    bridge = FakeBridge(writes=[Reply(201, _json({"key": "AAAA0001"})), ZoteroError("not_running", NOT_ANSWERING_TEXT)])
    result = save_items(ITEMS, transport=bridge, token_path=_token_file(tmp_path), now=_clock(NOW))
    assert result.state == BRIDGE_OFF
    assert result.saved == 1
    assert result.message == (
        "Saved 1 of 2 item(s). Stopped at item 2: the bridge stopped answering. Landed in Zotero: First paper. "
        "Not saved: item 2. Check Zotero before you save again."
    )


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


# ----------------------------------------------------------- the grant's lost reply and its key


def test_a_lost_grant_request_warns_that_a_prompt_may_be_open_and_cancels_nothing(tmp_path):
    bridge = FakeBridge(grant_error=ZoteroError("timeout", TIMEOUT_TEXT))
    result = save_items(ITEMS, transport=bridge, token_path=_token_file(tmp_path), now=_clock(NOW))
    assert result.state == ERROR
    assert result.saved == 0
    assert result.message == TIMEOUT_TEXT
    assert "a prompt may be open in Zotero" in result.message
    assert "Press Deny there." in result.message
    assert _writes(bridge) == []
    assert _deletes(bridge) == []


def test_a_reset_grant_request_warns_that_a_prompt_may_be_open(tmp_path):
    bridge = FakeBridge(grant_error=ZoteroError("unreachable", UNREACHABLE_TEXT))
    result = save_items(ITEMS, transport=bridge, token_path=_token_file(tmp_path), now=_clock(NOW))
    assert result.state == ERROR
    assert result.message == f"{UNREACHABLE_TEXT} {PROMPT_OPEN_WARNING}"
    assert _writes(bridge) == []


def test_a_refused_connection_at_the_grant_request_warns_of_nothing(tmp_path):
    bridge = FakeBridge(grant_error=ZoteroError("not_running", NOT_ANSWERING_TEXT))
    result = save_items(ITEMS, transport=bridge, token_path=_token_file(tmp_path), now=_clock(NOW))
    assert result.state == BRIDGE_OFF
    assert result.message == NOT_ANSWERING_TEXT


def test_a_grant_without_an_expiry_field_is_unknown_not_a_failure(tmp_path):
    bridge = FakeBridge(expires_at=None)
    result = save_items(ITEMS, transport=bridge, token_path=_token_file(tmp_path), now=_clock(NOW))
    assert result.state == SAVED
    assert result.saved == 2
    assert len(_writes(bridge)) == 2


def test_an_unreadable_expiry_is_unknown_not_a_failure(tmp_path):
    bridge = FakeBridge(expires_at="soon")
    result = save_items(ITEMS, transport=bridge, token_path=_token_file(tmp_path), now=_clock(NOW))
    assert result.state == SAVED
    assert result.saved == 2


@pytest.mark.parametrize(
    "delete_reply",
    [
        ZoteroError("unreachable", UNREACHABLE_TEXT),
        Reply(500, _error("internal")),
    ],
)
def test_a_failed_cancel_of_a_pending_grant_says_a_prompt_may_still_be_open(tmp_path, delete_reply):
    bridge = FakeBridge(grant="pending", delete_reply=delete_reply)
    result = save_items(ITEMS, transport=bridge, token_path=_token_file(tmp_path), now=_clock(NOW))
    assert result.state == NOT_OPEN_FOR_WRITES
    assert result.saved == 0
    assert result.message == NOT_OPEN_CANCEL_FAILED_TEXT.format(PENDING_PROMPT)
    assert "A prompt may still be open in Zotero. Press Deny there." in result.message
    assert "The request was cancelled." not in result.message
    assert _writes(bridge) == []


def test_a_successful_cancel_of_a_pending_grant_still_says_cancelled(tmp_path):
    bridge = FakeBridge(grant="pending")
    result = save_items(ITEMS, transport=bridge, token_path=_token_file(tmp_path), now=_clock(NOW))
    assert "The request was cancelled." in result.message
    assert "A prompt may still be open in Zotero. Press Deny there." in result.message


@pytest.mark.parametrize(
    "writes, state",
    [
        ((), SAVED),
        ((Reply(201, _json({"key": "A"})), Reply(403, _error("grant_required"))), GRANT_EXPIRED),
        ((Reply(201, _json({"key": "A"})), Reply(422, _error("invalid_fields"))), ERROR),
    ],
)
@pytest.mark.parametrize(
    "delete_reply",
    [
        ZoteroError("unreachable", UNREACHABLE_TEXT),
        Reply(500, _error("internal")),
    ],
)
def test_a_failed_cancel_of_a_granted_key_says_the_key_may_stay_live(tmp_path, writes, state, delete_reply):
    bridge = FakeBridge(writes=list(writes), delete_reply=delete_reply)
    result = save_items(ITEMS, transport=bridge, token_path=_token_file(tmp_path), now=_clock(NOW))
    assert result.state == state
    assert result.message.endswith(f" {KEY_MAY_STAY_LIVE_TEXT}")
    assert "may stay live until Zotero restarts or for up to eight hours" in result.message


def test_a_grant_already_gone_when_cancelled_is_not_reported_as_live(tmp_path):
    bridge = FakeBridge(delete_reply=Reply(404, _error("grant_not_found")))
    result = save_items(ITEMS, transport=bridge, token_path=_token_file(tmp_path), now=_clock(NOW))
    assert result.state == SAVED
    assert "stay live" not in result.message


def test_a_successful_cancel_of_a_granted_key_adds_no_key_warning(tmp_path):
    bridge = FakeBridge()
    result = save_items(ITEMS, transport=bridge, token_path=_token_file(tmp_path), now=_clock(NOW))
    assert result.message == SAVED_TEXT.format(2)


def test_the_save_path_never_trashes_restores_or_deletes_an_item(tmp_path):
    scenarios = [
        FakeBridge(),
        FakeBridge(writes=[Reply(201, _json({"key": "AAAA0001"})), Reply(422, _error("invalid_fields"))]),
        FakeBridge(writes=[Reply(201, _json({"key": "AAAA0001"})), Reply(403, _error("grant_required"))]),
        FakeBridge(writes=[Reply(201, _json({"key": "AAAA0001"})), ZoteroError("timeout", TIMEOUT_TEXT)]),
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
        save_items(
            THREE_ITEMS,
            transport=FakeBridge(writes=[Reply(201, _json({"key": "A"})), ZoteroError("timeout", TIMEOUT_TEXT)]),
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
        save_items(ITEMS, transport=FakeBridge(delete_reply=ZoteroError("unreachable", "x")), token_path=path, now=_clock(NOW)),
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


def test_the_token_path_resolves_under_the_test_home():
    """conftest points the state folder at the test home, so no test can read the owner's real token file."""
    home = Path(os.environ["USERPROFILE"])
    assert Path(os.environ["MERIDIAN_LOCAL_RUNNER_STATE_DIR"]).is_relative_to(home)
    assert token_file_path().is_relative_to(home)


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


# ----------------------------------------------------------- the real transport, against a stub server


class _StubHandler(BaseHTTPRequestHandler):
    """Answers each test route. Requests are recorded by the test through the reply itself."""

    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        return

    def do_GET(self):  # noqa: N802 - http.server naming
        self._route()

    def do_POST(self):  # noqa: N802
        self._route()

    def do_DELETE(self):  # noqa: N802
        self._route()

    def _route(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if 0 < length < 65536 else b""
        if self.path == "/echo":
            echoed = {
                "method": self.command,
                "host": self.headers.get("Host"),
                "origin": self.headers.get("Origin"),
                "authorization": self.headers.get("Authorization"),
                "grant": self.headers.get("Meridian-Write-Grant"),
                "body": body.decode("utf-8"),
            }
            self._send(200, _json(echoed))
        elif self.path == "/forbidden":
            self._send(403, _error("grant_required"))
        elif self.path == "/unauthorized":
            self._send(401, _error("unauthorized"))
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/echo")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/slow":
            time.sleep(1.0)
            self._send(200, b"{}")
        elif self.path == "/big":
            self.send_response(200)
            self.send_header("Content-Length", "2000000")
            self.end_headers()
            self.wfile.write(b"{}")
        else:
            self._send(404, _error("not_found"))

    def _send(self, status, payload):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def stub_base():
    """A stub bridge on a loopback port of its own. It is not 127.0.0.1:23125, so it cannot collide with Zotero."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


class _SaveStubHandler(BaseHTTPRequestHandler):
    """A stub bridge for a whole save: a grant, the items, and the grant's end.

    The second item's reply has its status and headers at once, and its body only after the client has given up.
    """

    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        return

    def _reply(self, status, payload):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def do_POST(self):  # noqa: N802 - http.server naming
        self._read_body()
        if self.path == "/v1/grants":
            self._reply(202, _json({"grant_id": GRANT_ID, "status": "granted"}))
        elif self.path == "/v1/items":
            self.server.item_posts += 1
            if self.server.item_posts == 1:
                self._reply(201, _json({"key": "AAAA0001"}))
            else:
                self.send_response(500)
                self.send_header("Content-Length", "100")
                self.end_headers()
                time.sleep(1.0)
                try:
                    self.wfile.write(b"x" * 100)
                except OSError:
                    return
        else:
            self._reply(404, _json({}))

    def do_GET(self):  # noqa: N802
        payload = {"grant_id": GRANT_ID, "status": "granted", "expires_at": "2099-01-01T00:00:00Z", "write_key": WRITE_KEY}
        self._reply(200, _json(payload))

    def do_DELETE(self):  # noqa: N802
        self._read_body()
        self._reply(200, _json({"grant_id": GRANT_ID, "status": "revoked"}))


@pytest.fixture
def save_stub():
    """A stub bridge for one whole save, on a loopback port of its own."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SaveStubHandler)
    server.daemon_threads = True
    server.item_posts = 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_the_real_transport_sends_the_bearer_and_the_grant_key_and_no_origin(stub_base):
    transport = bridge_transport(stub_base)
    reply = transport(
        "POST",
        "/echo",
        {"Authorization": f"Bearer {TOKEN}", "Meridian-Write-Grant": WRITE_KEY},
        b'{"a": 1}',
        2.0,
    )
    assert reply.status == 200
    echoed = json.loads(reply.body)
    assert echoed["host"] == stub_base.removeprefix("http://")
    assert echoed["origin"] is None
    assert echoed["authorization"] == f"Bearer {TOKEN}"
    assert echoed["grant"] == WRITE_KEY
    assert echoed["method"] == "POST"
    assert echoed["body"] == '{"a": 1}'


def test_the_real_transport_returns_an_error_status_as_a_reply(stub_base):
    reply = bridge_transport(stub_base)("POST", "/forbidden", {}, b"{}", 2.0)
    assert reply.status == 403
    assert json.loads(reply.body)["error"]["code"] == "grant_required"


def test_the_real_transport_returns_a_401_as_a_reply(stub_base):
    assert bridge_transport(stub_base)("GET", "/unauthorized", {}, None, 2.0).status == 401


def test_the_real_transport_does_not_follow_a_redirect(stub_base):
    assert bridge_transport(stub_base)("GET", "/redirect", {}, None, 2.0).status == 302


def test_the_real_transport_refuses_a_body_over_the_cap(stub_base):
    with pytest.raises(ZoteroError) as info:
        bridge_transport(stub_base)("GET", "/big", {}, None, 5.0)
    assert info.value.code == "too_large"


def test_the_real_transport_maps_a_slow_reply_to_timeout(stub_base):
    with pytest.raises(ZoteroError) as info:
        bridge_transport(stub_base)("GET", "/slow", {}, None, 0.2)
    assert info.value.code == "timeout"
    assert info.value.message == TIMEOUT_TEXT


def _failing_opener(error: Exception):
    """A stand-in for urllib's build_opener whose open raises ``error``, so no socket is ever opened."""

    class _Opener:
        def open(self, request, timeout=None):
            raise error

    def build_opener(*handlers):
        return _Opener()

    return build_opener


def test_a_refused_connection_maps_to_not_running(monkeypatch):
    """The refusal is injected, so this tests the mapping alone and opens no socket."""
    refused = urllib.error.URLError(ConnectionRefusedError(111, "refused"))
    monkeypatch.setattr(urllib.request, "build_opener", _failing_opener(refused))
    with pytest.raises(ZoteroError) as info:
        bridge_transport("http://127.0.0.1:9")("GET", "/v1/status", {}, None, 2.0)
    assert info.value.code == "not_running"
    assert info.value.message == NOT_ANSWERING_TEXT


def test_a_connect_timeout_maps_to_not_running(monkeypatch):
    """A connect that times out means nothing answered within the connect window, so the bridge is not running."""
    timed_out = urllib.error.URLError(TimeoutError("timed out"))
    monkeypatch.setattr(urllib.request, "build_opener", _failing_opener(timed_out))
    with pytest.raises(ZoteroError) as info:
        bridge_transport("http://127.0.0.1:9")("GET", "/v1/status", {}, None, 2.0)
    assert info.value.code == "not_running"
    assert info.value.message == NOT_ANSWERING_TEXT


def test_a_reply_that_never_comes_stays_a_timeout(monkeypatch):
    """A reply that never comes is raised as a bare TimeoutError, not wrapped, so it stays a timeout."""
    monkeypatch.setattr(urllib.request, "build_opener", _failing_opener(TimeoutError("timed out")))
    with pytest.raises(ZoteroError) as info:
        bridge_transport("http://127.0.0.1:9")("GET", "/v1/status", {}, None, 2.0)
    assert info.value.code == "timeout"
    assert info.value.message == TIMEOUT_TEXT


@contextmanager
def _unanswered_loopback_url():
    """A loopback URL whose port is bound but not listening.

    The probe stays bound while the URL is used. A port that was bound and then closed can be handed to the client as its
    own source port, and then the client connects to itself and waits for a reply instead of being refused.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    try:
        yield f"http://127.0.0.1:{probe.getsockname()[1]}"
    finally:
        probe.close()


def test_the_real_transport_maps_an_unanswered_port_to_not_running():
    """This machine does not refuse a connection to a port with nothing listening. It leaves the attempt unanswered, so
    the connect window runs out, and the test passes here through the connect timeout. On a machine that refuses the
    connection, the same test passes through the refusal. Either way the answer is not_running."""
    with _unanswered_loopback_url() as base:
        with pytest.raises(ZoteroError) as info:
            bridge_transport(base)("GET", "/v1/status", {}, None, 0.5)
    assert info.value.code == "not_running"
    assert info.value.message == NOT_ANSWERING_TEXT


def test_a_host_that_is_not_loopback_is_refused_before_any_socket(monkeypatch):
    """The refusal comes before an opener is built. The opener is stubbed to fail the test if it is ever reached."""

    def opener_reached(*args, **kwargs):
        raise AssertionError("an opener was built for a host that is not loopback")

    monkeypatch.setattr(urllib.request, "build_opener", opener_reached)
    with pytest.raises(ZoteroError) as info:
        bridge_transport("http://192.0.2.1:9")("GET", "/v1/status", {}, None, 0.5)
    assert info.value.code == "not_loopback"


# Captured when this module is imported, before conftest replaces the bridge's default transport with a refusal.
_REAL_DEFAULT_TRANSPORT = zotero_bridge.http_request


def test_the_default_transport_targets_the_bridge_on_its_own_port(monkeypatch):
    """The default transport calls the bridge's own address. The opener is stubbed, so the URL is read without a socket."""
    seen: list[str] = []

    class _Opener:
        def open(self, request, timeout=None):
            seen.append(request.full_url)
            raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))

    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: _Opener())
    with pytest.raises(ZoteroError) as info:
        _REAL_DEFAULT_TRANSPORT("GET", "/v1/status", {}, None, 0.5)
    assert seen == ["http://127.0.0.1:23125/v1/status"]
    assert info.value.code == "not_running"


def test_a_refused_status_check_over_the_real_transport_is_bridge_off(monkeypatch, tmp_path):
    """A refused connection through the real transport's error mapping reports bridge_off."""
    refused = urllib.error.URLError(ConnectionRefusedError(111, "refused"))
    monkeypatch.setattr(urllib.request, "build_opener", _failing_opener(refused))
    result = bridge_status(transport=bridge_transport("http://127.0.0.1:9"), token_path=_token_file(tmp_path))
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
    """The bridge as the panel sees it: the real line reader, a fixed status, and a save.

    ``save`` may be a function of the items. Without it, the save returns ``result``.
    """

    def __init__(self, *, status=None, result=None, gate=None, save=None):
        self.status_value = status or BridgeStatus(ANSWERING, ANSWERING_STATUS)
        self.result = result or SaveResult(SAVED, 2, SAVED_TEXT.format(2))
        self.gate = gate
        self.save = save
        self.batches: list[tuple[list[ReadingItem], bool]] = []

    def parse_lines(self, text):
        return parse_lines(text)

    def bridge_status(self):
        return self.status_value

    def save_items(self, items):
        if self.gate is not None:
            self.gate.wait(5)
        self.batches.append((list(items), threading.current_thread() is threading.main_thread()))
        if self.save is not None:
            return self.save(items)
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


def _box(panel) -> QPlainTextEdit:
    return _named(panel, QPlainTextEdit, "zotero-save-input")


def _click_save(panel):
    _named(panel, QPushButton, "zotero-save-button").click()


def test_the_bridge_state_shows_next_to_the_save_button(qtbot):
    panel = _save_panel(qtbot, PanelBridge(status=BridgeStatus(BRIDGE_OFF, NO_TOKEN_STATUS)))
    qtbot.waitUntil(lambda: _text(panel, "zotero-bridge-state") == NO_TOKEN_STATUS, timeout=5000)


def test_each_line_is_saved_off_the_ui_thread_and_the_result_is_shown(qtbot):
    bridge = PanelBridge()
    panel = _save_panel(qtbot, bridge)
    qtbot.waitUntil(lambda: _text(panel, "zotero-bridge-state") == ANSWERING_STATUS, timeout=5000)
    _box(panel).setPlainText(
        "First paper | https://example.org/one\nSecond paper | https://example.org/two"
    )
    _click_save(panel)
    qtbot.waitUntil(lambda: _text(panel, "zotero-save-result") == bridge.result.message, timeout=5000)
    items, on_gui_thread = bridge.batches[0]
    assert items == ITEMS
    assert on_gui_thread is False


def test_a_bad_line_is_shown_and_nothing_is_sent(qtbot):
    bridge = PanelBridge()
    panel = _save_panel(qtbot, bridge)
    _box(panel).setPlainText("no separator here")
    _click_save(panel)
    qtbot.waitUntil(lambda: "Line 1 needs a title and a URL" in _text(panel, "zotero-save-result"), timeout=5000)
    assert bridge.batches == []


def test_the_save_button_and_the_box_are_locked_while_a_save_is_running(qtbot):
    gate = threading.Event()
    bridge = PanelBridge(gate=gate)
    panel = _save_panel(qtbot, bridge)
    _box(panel).setPlainText("First paper | https://example.org/one")
    button = _named(panel, QPushButton, "zotero-save-button")
    button.click()
    qtbot.waitUntil(lambda: not button.isEnabled(), timeout=5000)
    assert _box(panel).isReadOnly()
    gate.set()
    qtbot.waitUntil(lambda: button.isEnabled(), timeout=5000)
    assert not _box(panel).isReadOnly()
    assert _text(panel, "zotero-save-result") == bridge.result.message


def test_a_grant_that_lapses_after_two_leaves_only_lines_three_to_five(qtbot, tmp_path):
    fake = FakeBridge(writes=[Reply(201, _json({"key": "A"})), Reply(201, _json({"key": "B"})), Reply(403, _error("grant_required"))])
    token = _token_file(tmp_path)
    bridge = PanelBridge(save=lambda items: save_items(items, transport=fake, token_path=token, now=_clock(NOW)))
    panel = _save_panel(qtbot, bridge)
    _box(panel).setPlainText(_lines("Paper 1", "Paper 2", "Paper 3", "Paper 4", "Paper 5"))
    _click_save(panel)
    qtbot.waitUntil(lambda: _text(panel, "zotero-save-result").startswith("The write grant ended after 2 of 5"), timeout=5000)
    assert _box(panel).toPlainText() == _lines("Paper 3", "Paper 4", "Paper 5")
    assert not _box(panel).isReadOnly()


def test_a_full_success_empties_the_box(qtbot, tmp_path):
    fake = FakeBridge()
    token = _token_file(tmp_path)
    bridge = PanelBridge(save=lambda items: save_items(items, transport=fake, token_path=token, now=_clock(NOW)))
    panel = _save_panel(qtbot, bridge)
    _box(panel).setPlainText(_lines("Paper 1", "Paper 2"))
    _click_save(panel)
    qtbot.waitUntil(lambda: _text(panel, "zotero-save-result") == SAVED_TEXT.format(2), timeout=5000)
    assert _box(panel).toPlainText() == ""


def test_a_second_save_after_success_posts_zero_items(qtbot, tmp_path):
    fake = FakeBridge()
    token = _token_file(tmp_path)
    bridge = PanelBridge(save=lambda items: save_items(items, transport=fake, token_path=token, now=_clock(NOW)))
    panel = _save_panel(qtbot, bridge)
    _box(panel).setPlainText(_lines("Paper 1", "Paper 2"))
    _click_save(panel)
    qtbot.waitUntil(lambda: _text(panel, "zotero-save-result") == SAVED_TEXT.format(2), timeout=5000)
    posted_before = len(_writes(fake))
    batches_before = len(bridge.batches)
    _click_save(panel)
    qtbot.waitUntil(lambda: "Type at least one line" in _text(panel, "zotero-save-result"), timeout=5000)
    assert len(_writes(fake)) == posted_before
    assert len(bridge.batches) == batches_before


def test_an_unknown_item_is_held_back_and_a_second_save_posts_nothing(qtbot, tmp_path):
    fake = FakeBridge(writes=[Reply(201, _json({"key": "A"})), ZoteroError("timeout", TIMEOUT_TEXT)])
    token = _token_file(tmp_path)
    bridge = PanelBridge(save=lambda items: save_items(items, transport=fake, token_path=token, now=_clock(NOW)))
    panel = _save_panel(qtbot, bridge)
    _box(panel).setPlainText(_lines("Paper 1", "Paper 2", "Paper 3"))
    _click_save(panel)
    qtbot.waitUntil(lambda: "may or may not have been saved" in _text(panel, "zotero-save-result"), timeout=5000)
    assert _named(panel, QPlainTextEdit, "zotero-held-lines").toPlainText() == _lines("Paper 2")
    assert _box(panel).toPlainText() == _lines("Paper 3")
    assert not _named(panel, QPushButton, "zotero-save-button").isEnabled()
    posted = len(_writes(fake))
    _click_save(panel)  # Save is off while a line is held, so this posts nothing
    assert len(_writes(fake)) == posted


def test_a_save_with_no_result_holds_every_line_back(qtbot, tmp_path):
    typed = _lines("Paper 1", "Paper 2")

    def broken(items):
        raise RuntimeError("boom")

    bridge = PanelBridge(save=broken)
    panel = _save_panel(qtbot, bridge)
    _box(panel).setPlainText(typed)
    _click_save(panel)
    qtbot.waitUntil(lambda: _text(panel, "zotero-save-result") == SAVE_NO_RESULT_TEXT, timeout=5000)
    assert _box(panel).toPlainText() == ""
    assert _named(panel, QPlainTextEdit, "zotero-held-lines").toPlainText() == typed
    assert not _named(panel, QPushButton, "zotero-save-button").isEnabled()


def test_checking_zotero_releases_the_held_line_without_retrying_it(qtbot, tmp_path):
    fake = FakeBridge(writes=[Reply(201, _json({"key": "A"})), ZoteroError("timeout", TIMEOUT_TEXT)])
    token = _token_file(tmp_path)
    bridge = PanelBridge(save=lambda items: save_items(items, transport=fake, token_path=token, now=_clock(NOW)))
    panel = _save_panel(qtbot, bridge)
    _box(panel).setPlainText(_lines("Paper 1", "Paper 2", "Paper 3"))
    _click_save(panel)
    qtbot.waitUntil(lambda: "may or may not have been saved" in _text(panel, "zotero-save-result"), timeout=5000)
    _named(panel, QPushButton, "zotero-held-checked").click()
    assert _named(panel, QPlainTextEdit, "zotero-held-lines").toPlainText() == ""
    assert _named(panel, QPushButton, "zotero-save-button").isEnabled()
    assert _text(panel, "zotero-save-result") == HELD_RELEASED_TEXT
    posted = len(_writes(fake))
    _click_save(panel)
    qtbot.waitUntil(lambda: _text(panel, "zotero-save-result") == SAVED_TEXT.format(1), timeout=5000)
    assert len(_writes(fake)) == posted + 1
    assert json.loads(_writes(fake)[-1][3])["fields"]["title"] == "Paper 3"
    assert _box(panel).toPlainText() == ""


def test_a_refusal_after_two_items_land_keeps_only_the_lines_that_did_not_land(qtbot, tmp_path):
    fake = FakeBridge(
        writes=[Reply(201, _json({"key": "A"})), Reply(201, _json({"key": "B"})), Reply(422, _error("invalid_fields"))]
    )
    token = _token_file(tmp_path)
    bridge = PanelBridge(save=lambda items: save_items(items, transport=fake, token_path=token, now=_clock(NOW)))
    panel = _save_panel(qtbot, bridge)
    _box(panel).setPlainText(_lines("Paper 1", "Paper 2", "Paper 3", "Paper 4", "Paper 5"))
    _click_save(panel)
    qtbot.waitUntil(lambda: _text(panel, "zotero-save-result").startswith("Saved 2 of 5"), timeout=5000)
    assert _box(panel).toPlainText() == _lines("Paper 3", "Paper 4", "Paper 5")
    assert _named(panel, QPlainTextEdit, "zotero-held-lines").toPlainText() == ""
