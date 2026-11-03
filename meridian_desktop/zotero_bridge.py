"""Save reading-list items into the owner's Zotero library through the Meridian Zotero bridge. Stdlib only, no Qt.

The bridge is the Meridian plugin that the owner installs in Zotero. It listens on 127.0.0.1:23125 and answers only
a request that carries its bearer token. A write also needs a grant key. When Zotero's open local writes setting is
on, a grant is answered at once with no prompt. This module never waits for a prompt and never clicks anything. If a
grant is not granted at once, it cancels the grant and reports not_open_for_writes.

- token_file_path and read_token: the token file under Meridian's local-runner state folder. A missing or empty file
  means the bridge is off, and no request is sent.
- bridge_status: GET /v1/status, which only reads.
- save_items: one grant for the batch, one POST /v1/items per item, then the grant ends. It stops at the first refusal
  and reports the titles that landed, the items not saved, and any item whose write may have landed without an answer.
  It never trashes, restores or deletes anything.
- parse_lines: reads the "Title | URL" lines the panel shows.

Every request goes through an injectable transport: transport(method, path, headers, body, timeout) -> Reply. A
transport raises ZoteroError when a request fails. Messages are fixed text. A response body, the bearer token and a
grant key never reach a message.
"""

from __future__ import annotations

import datetime as dt
import http.client
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from meridian_desktop import local_client
from meridian_desktop.local_client import LocalClientError
from meridian_desktop.zotero_client import Reply, ZoteroError, _NoRedirects, _read_bounded

BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 23125
BRIDGE_BASE_URL = f"http://{BRIDGE_HOST}:{BRIDGE_PORT}"
STATUS_PATH = "/v1/status"
GRANTS_PATH = "/v1/grants"
ITEMS_PATH = "/v1/items"
GRANT_HEADER = "Meridian-Write-Grant"
APP_NAME = "Meridian desktop"
ITEM_TYPE = "webpage"
TOKEN_SUBDIR = "zotero_bridge"
TOKEN_FILENAME = "api_token"
STATE_DIR_ENV_VAR = "MERIDIAN_LOCAL_RUNNER_STATE_DIR"
MAX_ITEMS = 50
MAX_TITLE_CHARS = 500
MAX_URL_CHARS = 2000
TIMEOUT = 10.0
# A grant is stopped this long before its expiry, so a write cannot land on the edge of it.
GRANT_MARGIN = dt.timedelta(seconds=30)
_SAFE_CODE = re.compile(r"^[a-z][a-z_]{0,39}$")
_BOM = "﻿"

# Result states.
SAVED = "saved"
BRIDGE_OFF = "bridge_off"
NOT_OPEN_FOR_WRITES = "not_open_for_writes"
GRANT_EXPIRED = "grant_expired"
UNKNOWN = "unknown"
ERROR = "error"

# Status states, as bridge_status reports them. ERROR is shared with the result states.
ANSWERING = "answering"
UNAUTHORIZED = "unauthorized"

NOTHING_TO_SAVE = "Type at least one line as Title | URL. Nothing was saved."
TOO_MANY_TEXT = f"Save at most {MAX_ITEMS} items at a time. Nothing was saved."
LINE_TEXT = "Line {} needs a title and a URL, separated by |. Nothing was saved."
LINE_PROBLEM_TEXT = "Line {}: {}. Nothing was saved."
ITEM_PROBLEM_TEXT = "Item {}: {}. Nothing was saved and no request was sent."
EMPTY_TITLE_TEXT = "the title is empty"
CONTROL_TITLE_TEXT = "the title has a line break or a control character"
LONG_TITLE_TEXT = f"the title is longer than {MAX_TITLE_CHARS} characters"
LONG_URL_TEXT = f"the URL is longer than {MAX_URL_CHARS} characters"
BAD_URL_TEXT = "the URL must start with http:// or https:// and name a host, with no spaces"
NO_TOKEN_TEXT = (
    "The Meridian Zotero bridge is off: its token file is missing or empty. "
    "Nothing was saved and no request was sent."
)
NOT_ANSWERING_TEXT = (
    "Nothing answered on 127.0.0.1:23125 within the connect window. Open Zotero with the Meridian plugin running."
)
UNREACHABLE_TEXT = "The Meridian Zotero bridge could not be reached on 127.0.0.1:23125."
TIMEOUT_TEXT = (
    "The Meridian Zotero bridge did not answer in time. If it was answering a grant request, a prompt may be open "
    "in Zotero. Press Deny there."
)
TOKEN_REFUSED_TEXT = "The Meridian Zotero bridge refused its token. Check the token file, then try again."
BAD_RESPONSE_TEXT = "The Meridian Zotero bridge sent a response the desktop could not read."
SAVED_TEXT = "Saved {} item(s) to Zotero through the Meridian bridge."
NOT_OPEN_TEXT = (
    "Nothing was saved. Zotero did not grant a write at once: {}. The request was cancelled. "
    "If Zotero asks a person to answer a prompt, open local writes is off in the Meridian Zotero plugin."
)
NOT_OPEN_CANCEL_FAILED_TEXT = (
    "Nothing was saved. Zotero did not grant a write at once: {}. The request could not be cancelled. "
    "A prompt may still be open in Zotero. Press Deny there."
)
KEY_MAY_STAY_LIVE_TEXT = "The write key could not be ended, so it may stay live for up to an hour."
LANDED_TEXT = "Landed in Zotero: {}."
NOTHING_LANDED_TEXT = "Nothing landed in Zotero."
NOT_SAVED_TEXT = "Not saved: {}."
NOT_SENT_TEXT = "Not sent: {}."
NO_ITEMS_AFTER_TEXT = "There were no items after it."
CHECK_BEFORE_SAVE_TEXT = "Check Zotero before you save again."
GRANT_EXPIRED_TEXT = "The write grant ended after {} of {} item(s). {} {} {}"
STOPPED_TEXT = "Saved {} of {} item(s). Stopped at item {}: {}. {} {} {}"
UNKNOWN_TEXT = (
    "Saved {} of {} item(s). Item {} may or may not have been saved. Check Zotero for it before you retry. {} {}"
)
PENDING_REASON = "Zotero is asking a person to answer a prompt"
DENIED_REASON = "a person refused it"
UNKNOWN_REASON = "Zotero did not grant it"
ITEM_REFUSED_TEXT = "Zotero refused the item ({})"
ITEM_HTTP_TEXT = "Zotero answered HTTP {}"
ITEM_BRIDGE_STOPPED = "the bridge stopped answering"
ITEM_TOKEN_REFUSED = "the bridge refused its token"
ITEM_LOST_REASON = "the reply was lost"
ITEM_GRANT_ENDED = "the write grant is no longer live"
ANSWERING_STATUS = "Bridge: answering on 127.0.0.1:23125."
NO_TOKEN_STATUS = "Bridge: off. Its token file is missing or empty."
NOT_ANSWERING_STATUS = (
    "Bridge: off. Nothing answered on 127.0.0.1:23125 within the connect window, so Zotero or its Meridian plugin "
    "is not running."
)
UNAUTHORIZED_STATUS = "Bridge: running, but it refused the token."
STATUS_ERROR_STATUS = "Bridge: could not be checked ({})."

# transport(method, path, headers, body, timeout) -> Reply. It raises ZoteroError for a failed request.
Transport = Callable[[str, str, Mapping[str, str], bytes | None, float], Reply]


@dataclass(frozen=True)
class ReadingItem:
    """One item to save. The title and the URL are plain text, already stripped by the reader."""

    title: str
    url: str


@dataclass(frozen=True)
class SaveResult:
    """What a save did.

    ``saved`` counts the items Zotero created before the batch stopped, and ``landed`` holds their titles in order.
    An item whose write got no answer is not counted as saved or as not saved. It is reported as unknown.
    """

    state: str
    saved: int
    message: str
    landed: tuple[str, ...] = ()


@dataclass(frozen=True)
class BridgeStatus:
    """Whether the bridge answers and accepts the token. ``message`` is fixed text for a person to read."""

    state: str
    message: str


class BridgeInputError(Exception):
    """The lines a person typed cannot be saved. ``message`` says which line and why, in fixed text."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class _Refusal:
    """One item the bridge did not take: the result state it implies, and a short reason."""

    state: str
    reason: str


class _NotOpen(Exception):
    """A grant was not granted at once. ``reason`` is a short phrase for the message.

    ``cancel_failed`` is set when the grant existed and the request to cancel it failed, so a prompt may still be open.
    """

    def __init__(self, reason: str, *, cancel_failed: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.cancel_failed = cancel_failed


def token_file_path(env: Mapping[str, str] | None = None) -> Path:
    """The token file: zotero_bridge/api_token under Meridian's local-runner state folder, where the plugin keeps it.

    The override folder named by MERIDIAN_LOCAL_RUNNER_STATE_DIR is used as it is. Otherwise the folder is
    ~/.meridian/local_runner.
    """
    env = os.environ if env is None else env
    override = env.get(STATE_DIR_ENV_VAR, "").strip()
    base = Path(override) if override else Path.home() / ".meridian" / "local_runner"
    return base / TOKEN_SUBDIR / TOKEN_FILENAME


def read_token(path: Path | None = None) -> str | None:
    """The bridge token, or None when the file is missing, unreadable or holds nothing usable. Never raises.

    The plugin tolerates whitespace and a byte-order mark at either end of the file, so this does too.
    """
    target = token_file_path() if path is None else path
    try:
        raw = target.read_bytes()
    except OSError:
        return None
    token = raw.decode("utf-8", errors="replace").strip().lstrip(_BOM).strip()
    if not token or not token.isascii() or any(ch.isspace() for ch in token):
        return None
    return token


def _transport_error(reason: object) -> ZoteroError:
    """Map the reason urllib gives for a request that failed before a reply.

    urllib wraps a failure in URLError only while it connects or sends the request. A refused connection, or a timeout
    inside URLError, therefore means nothing answered within the connect window, so the bridge is not running. The
    request is small, so a timeout while sending it is not expected here. A reply that never comes is not wrapped: its
    timeout is raised as a bare TimeoutError and handled as a timeout by the caller.
    """
    if isinstance(reason, (ConnectionRefusedError, TimeoutError)):
        return ZoteroError("not_running", NOT_ANSWERING_TEXT)
    return ZoteroError("unreachable", UNREACHABLE_TEXT)


def bridge_transport(base_url: str = BRIDGE_BASE_URL) -> Transport:
    """A loopback transport for one base URL. http_request, the default, is this for the bridge on 127.0.0.1:23125.

    A non-2xx status comes back as a Reply. A redirect is not followed. No Origin header is sent, so a browser page
    cannot use this path. Each request goes through an empty proxy handler, so loopback traffic never leaves the machine.
    """

    def transport(method: str, path: str, headers: Mapping[str, str], body: bytes | None, timeout: float) -> Reply:
        try:
            local_client.assert_loopback(base_url)
        except LocalClientError as exc:
            raise ZoteroError(exc.code, str(exc)) from None
        request = urllib.request.Request(
            base_url + path,
            data=body,
            method=method,
            headers={"User-Agent": local_client.USER_AGENT, "Accept": "application/json", **headers},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirects())
        try:
            with opener.open(request, timeout=timeout) as response:
                return Reply(response.status, _read_bounded(response))
        except urllib.error.HTTPError as exc:
            try:
                return Reply(exc.code, _read_bounded(exc))
            finally:
                exc.close()
        except urllib.error.URLError as exc:
            raise _transport_error(exc.reason) from None
        except TimeoutError:
            raise ZoteroError("timeout", TIMEOUT_TEXT) from None
        except (OSError, http.client.HTTPException, ValueError):
            raise ZoteroError("unreachable", UNREACHABLE_TEXT) from None

    return transport


http_request: Transport = bridge_transport()


def _send(
    fetch: Transport,
    method: str,
    path: str,
    token: str,
    *,
    grant_key: str | None = None,
    body: Mapping[str, object] | None = None,
) -> Reply:
    """One request with the bearer token, and the grant key when a write needs one. A 401 raises for every caller."""
    headers = {"Authorization": f"Bearer {token}"}
    if grant_key is not None:
        headers[GRANT_HEADER] = grant_key
    payload = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        payload = json.dumps(body).encode("utf-8")
    reply = fetch(method, path, headers, payload, TIMEOUT)
    if reply.status == 401:
        raise ZoteroError("unauthorized", TOKEN_REFUSED_TEXT)
    return reply


def _json_object(body: bytes) -> dict:
    """The JSON object in a body, or an empty dict. Never raises, and the text is never shown."""
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def _error_code(reply: Reply) -> str:
    """The bridge's short error code, when it sent a safe one. Otherwise "unknown". Its text is never used."""
    error = _json_object(reply.body).get("error")
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) and _SAFE_CODE.match(code) else "unknown"


def _parse_time(value: object) -> dt.datetime | None:
    """An ISO 8601 time from the bridge, as an aware datetime. None when it is missing or cannot be read."""
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.timezone.utc)


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def check_item(title: str, url: str) -> str | None:
    """The first problem with one item, as a short phrase, or None when it is fine. Makes no request."""
    stripped = title.strip()
    if not stripped:
        return EMPTY_TITLE_TEXT
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in stripped):
        return CONTROL_TITLE_TEXT
    if len(stripped) > MAX_TITLE_CHARS:
        return LONG_TITLE_TEXT
    if len(url) > MAX_URL_CHARS:
        return LONG_URL_TEXT
    if not url or any(ch.isspace() for ch in url):
        return BAD_URL_TEXT
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return BAD_URL_TEXT
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return BAD_URL_TEXT
    return None


def parse_lines(text: str) -> list[ReadingItem]:
    """Read the lines a person typed, each "Title | URL". Blank lines are skipped. The URL is the text after the last |.

    Raises BridgeInputError, with the line number, before anything is sent. The items come back in line order, one per
    non-blank line.
    """
    items: list[ReadingItem] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        title, separator, url = line.rpartition("|")
        if not separator:
            raise BridgeInputError(LINE_TEXT.format(number))
        title, url = title.strip(), url.strip()
        problem = check_item(title, url)
        if problem is not None:
            raise BridgeInputError(LINE_PROBLEM_TEXT.format(number, problem))
        items.append(ReadingItem(title, url))
    if not items:
        raise BridgeInputError(NOTHING_TO_SAVE)
    if len(items) > MAX_ITEMS:
        raise BridgeInputError(TOO_MANY_TEXT)
    return items


def bridge_status(*, transport: Transport | None = None, token_path: Path | None = None) -> BridgeStatus:
    """Whether the bridge answers and accepts the token. Reads only, and never raises."""
    token = read_token(token_path)
    if token is None:
        return BridgeStatus(BRIDGE_OFF, NO_TOKEN_STATUS)
    fetch = http_request if transport is None else transport
    try:
        reply = _send(fetch, "GET", STATUS_PATH, token)
    except ZoteroError as exc:
        if exc.code == "unauthorized":
            return BridgeStatus(UNAUTHORIZED, UNAUTHORIZED_STATUS)
        if exc.code == "not_running":
            return BridgeStatus(BRIDGE_OFF, NOT_ANSWERING_STATUS)
        return BridgeStatus(ERROR, STATUS_ERROR_STATUS.format(exc.code))
    if reply.status == 200:
        return BridgeStatus(ANSWERING, ANSWERING_STATUS)
    return BridgeStatus(ERROR, STATUS_ERROR_STATUS.format(f"HTTP {reply.status}"))


def _batch_problem(items: Sequence[ReadingItem]) -> str | None:
    """The first problem with the batch, or None. Checked before any request is sent."""
    if not items:
        return NOTHING_TO_SAVE
    if len(items) > MAX_ITEMS:
        return TOO_MANY_TEXT
    for number, item in enumerate(items, start=1):
        problem = check_item(item.title, item.url)
        if problem is not None:
            return ITEM_PROBLEM_TEXT.format(number, problem)
    return None


def _not_granted_reason(status: object) -> str:
    if status == "pending":
        return PENDING_REASON
    if status == "denied":
        return DENIED_REASON
    return UNKNOWN_REASON


def _open_grant(fetch: Transport, token: str) -> tuple[str, str, dt.datetime | None]:
    """Ask for a write grant and read its key. Raises _NotOpen when it is not granted at once.

    Any grant that exists and is not usable is cancelled before the error leaves this function. A failed cancel is
    reported on the _NotOpen as cancel_failed. A grant with no expires_at is still usable: the expiry is then unknown, and
    the bridge's own refusal (grant_required) stops the batch if the grant has lapsed.
    """
    reply = _send(fetch, "POST", GRANTS_PATH, token, body={"app_name": APP_NAME})
    body = _json_object(reply.body)
    grant_id = body.get("grant_id")
    if reply.status not in (200, 202) or not isinstance(grant_id, str) or not grant_id:
        raise _NotOpen(f"Zotero answered {_error_code(reply)}")
    try:
        if body.get("status") != "granted":
            raise _NotOpen(_not_granted_reason(body.get("status")))
        poll = _send(fetch, "GET", f"{GRANTS_PATH}/{grant_id}", token)
        grant = _json_object(poll.body)
        if poll.status != 200:
            raise ZoteroError("bad_response", BAD_RESPONSE_TEXT)
        if grant.get("status") != "granted":
            raise _NotOpen(_not_granted_reason(grant.get("status")))
        write_key = grant.get("write_key")
        if not isinstance(write_key, str) or not write_key:
            raise ZoteroError("bad_response", BAD_RESPONSE_TEXT)
        expires_at = _parse_time(grant.get("expires_at"))
    except _NotOpen as exc:
        if not _end_grant(fetch, token, grant_id):
            raise _NotOpen(exc.reason, cancel_failed=True) from None
        raise
    except BaseException:
        _end_grant(fetch, token, grant_id)
        raise
    return grant_id, write_key, expires_at


def _item_body(item: ReadingItem) -> dict[str, object]:
    """Title and URL go inside "fields". A top-level title is refused by the bridge with 422 invalid_fields."""
    return {"item_type": ITEM_TYPE, "fields": {"title": item.title.strip(), "url": item.url.strip()}}


def _create_item(fetch: Transport, token: str, write_key: str, item: ReadingItem) -> _Refusal | None:
    """Write one item. None when Zotero created it. Otherwise the refusal that stops the batch."""
    try:
        reply = _send(fetch, "POST", ITEMS_PATH, token, grant_key=write_key, body=_item_body(item))
    except ZoteroError as exc:
        if exc.code == "not_running":
            return _Refusal(BRIDGE_OFF, ITEM_BRIDGE_STOPPED)
        if exc.code == "unauthorized":
            return _Refusal(ERROR, ITEM_TOKEN_REFUSED)
        # A timeout or a lost reply can come after Zotero has created the item, so the item is unknown, not refused.
        return _Refusal(UNKNOWN, ITEM_LOST_REASON)
    if reply.status == 201:
        return None
    if reply.status == 403 and _error_code(reply) == "grant_required":
        return _Refusal(GRANT_EXPIRED, ITEM_GRANT_ENDED)
    if reply.status == 422:
        return _Refusal(ERROR, ITEM_REFUSED_TEXT.format(_error_code(reply)))
    return _Refusal(ERROR, ITEM_HTTP_TEXT.format(reply.status))


def _end_grant(fetch: Transport, token: str, grant_id: str) -> bool:
    """End a grant at once. True when the bridge confirmed it ended, or it was already gone. Never raises."""
    try:
        reply = _send(fetch, "DELETE", f"{GRANTS_PATH}/{grant_id}", token)
    except ZoteroError:
        return False
    return 200 <= reply.status < 300 or reply.status == 404


def _lapsed(clock: Callable[[], dt.datetime], expires_at: dt.datetime | None) -> bool:
    """True when the grant is at or near its expiry. An unknown expiry never lapses here."""
    return expires_at is not None and clock() + GRANT_MARGIN >= expires_at


def _landed(items: Sequence[ReadingItem], saved: int) -> tuple[str, ...]:
    return tuple(item.title for item in items[:saved])


def _landed_text(landed: tuple[str, ...]) -> str:
    return NOTHING_LANDED_TEXT if not landed else LANDED_TEXT.format("; ".join(landed))


def _span(first: int, last: int) -> str:
    return f"item {first}" if first == last else f"items {first} to {last}"


def _grant_ended(items: Sequence[ReadingItem], saved: int) -> SaveResult:
    total = len(items)
    landed = _landed(items, saved)
    message = GRANT_EXPIRED_TEXT.format(
        saved,
        total,
        _landed_text(landed),
        NOT_SAVED_TEXT.format(_span(saved + 1, total)),
        CHECK_BEFORE_SAVE_TEXT,
    )
    return SaveResult(GRANT_EXPIRED, saved, message, landed)


def _stopped(items: Sequence[ReadingItem], saved: int, number: int, refusal: _Refusal) -> SaveResult:
    total = len(items)
    landed = _landed(items, saved)
    message = STOPPED_TEXT.format(
        saved,
        total,
        number,
        refusal.reason,
        _landed_text(landed),
        NOT_SAVED_TEXT.format(_span(number, total)),
        CHECK_BEFORE_SAVE_TEXT,
    )
    return SaveResult(refusal.state, saved, message, landed)


def _unknown(items: Sequence[ReadingItem], saved: int, number: int) -> SaveResult:
    """Item ``number`` got no answer, so it is reported as unknown. It is not counted as saved or as not saved."""
    total = len(items)
    landed = _landed(items, saved)
    rest = NOT_SENT_TEXT.format(_span(number + 1, total)) if number < total else NO_ITEMS_AFTER_TEXT
    message = UNKNOWN_TEXT.format(saved, total, number, _landed_text(landed), rest)
    return SaveResult(UNKNOWN, saved, message, landed)


def _write_batch(
    fetch: Transport,
    token: str,
    write_key: str,
    items: Sequence[ReadingItem],
    clock: Callable[[], dt.datetime],
    expires_at: dt.datetime | None,
) -> SaveResult:
    saved = 0
    for number, item in enumerate(items, start=1):
        if _lapsed(clock, expires_at):
            return _grant_ended(items, saved)
        refusal = _create_item(fetch, token, write_key, item)
        if refusal is None:
            saved += 1
            continue
        if refusal.state == GRANT_EXPIRED:
            return _grant_ended(items, saved)
        if refusal.state == UNKNOWN:
            return _unknown(items, saved, number)
        return _stopped(items, saved, number, refusal)
    return SaveResult(SAVED, saved, SAVED_TEXT.format(saved), _landed(items, saved))


def save_items(
    items: Sequence[ReadingItem],
    *,
    transport: Transport | None = None,
    token_path: Path | None = None,
    now: Callable[[], dt.datetime] | None = None,
) -> SaveResult:
    """Save the items as one batch: one grant, one write per item, then the grant ends. Never raises.

    The batch is checked before any request. A grant that is not granted at once writes nothing. A refusal stops the
    batch. The result names the titles that landed, the items not saved, and an item whose write got no answer, which
    is reported as unknown and never retried. If the grant cannot be ended, the result says the key may stay live. The
    batch never trashes or deletes.
    """
    problem = _batch_problem(items)
    if problem is not None:
        return SaveResult(ERROR, 0, problem)
    token = read_token(token_path)
    if token is None:
        return SaveResult(BRIDGE_OFF, 0, NO_TOKEN_TEXT)
    fetch = http_request if transport is None else transport
    clock = _utc_now if now is None else now
    try:
        grant_id, write_key, expires_at = _open_grant(fetch, token)
    except _NotOpen as exc:
        template = NOT_OPEN_CANCEL_FAILED_TEXT if exc.cancel_failed else NOT_OPEN_TEXT
        return SaveResult(NOT_OPEN_FOR_WRITES, 0, template.format(exc.reason))
    except ZoteroError as exc:
        state = BRIDGE_OFF if exc.code == "not_running" else ERROR
        return SaveResult(state, 0, exc.message)
    try:
        result = _write_batch(fetch, token, write_key, items, clock, expires_at)
    finally:
        ended = _end_grant(fetch, token, grant_id)
    if ended:
        return result
    return replace(result, message=f"{result.message} {KEY_MAY_STAY_LIVE_TEXT}")


__all__ = [
    "ANSWERING",
    "BRIDGE_OFF",
    "ERROR",
    "GRANT_EXPIRED",
    "NOT_OPEN_FOR_WRITES",
    "SAVED",
    "UNAUTHORIZED",
    "UNKNOWN",
    "BridgeInputError",
    "BridgeStatus",
    "ReadingItem",
    "SaveResult",
    "bridge_status",
    "bridge_transport",
    "check_item",
    "http_request",
    "parse_lines",
    "read_token",
    "save_items",
    "token_file_path",
]
