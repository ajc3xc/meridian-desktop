"""Client for the Zotero bridge. Pure Python: stdlib urllib only, no Qt import.

The bridge keeps Zotero's plugin at the version Meridian expects. The local Meridian server manages it, and the
desktop asks that server over loopback:

    GET  {local_url}/desktop/zotero-bridge/status
    POST {local_url}/desktop/zotero-bridge/update      body: one of the three fixed bodies below

- status() reads the bridge and returns a BridgeStatus. The timeout is 5 seconds.
- update() sends the owner's choice and returns a BridgeResult. The timeout is 75 seconds, because a polite close can
  take up to a minute and a force close waits up to 15 seconds before it installs. The three choices and their bodies:
    polite  {"when": "now", "close": "polite"}  a normal close request to Zotero, then install and restart Zotero.
    force   {"when": "now", "close": "force"}   ends Zotero at once, then install and restart Zotero.
    wait    {"when": "wait"}                    closes nothing; the update applies the next time Zotero starts.
  The desktop sends force only after the owner confirms it a second time. Any other choice is refused before any
  request is sent.

Both functions return a plain result and never raise. The address is checked for loopback before any transport
call, so a non-loopback address sends nothing. No request carries a token: the route is loopback-only and no
Authorization header is sent. Failures carry fixed text. The body of an answer is never copied into a result.

Status answer, HTTP 200, a JSON object with exactly the keys the core route sends:
    {"state": "current" | "update_ready" | "zotero_running" | "not_installed",
     "installed_version": "0.1.3" or null, "expected_version": "0.1.4" or null,
     "pending_install": true | false}
"update_ready" means an update is ready and Zotero is closed. "zotero_running" means an update is ready while Zotero
is open. "pending_install" is true while a "wait" choice is recorded. Any other state is an error.
Update answer, HTTP 200, a JSON object: {"message": "one short line"}. The message is optional.
Update failure, any other status, with a JSON error object: {"error": {"code": "short_slug", "message": "..."}}. Only a
short lowercase code is shown. The code "zotero_still_open" means a polite close left Zotero open, so nothing was
installed, and the result is marked still_open.
"""

from __future__ import annotations

import http.client
import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from meridian_desktop import local_client
from meridian_desktop.local_client import LocalClientError

STATUS_PATH = "/desktop/zotero-bridge/status"
UPDATE_PATH = "/desktop/zotero-bridge/update"
STATUS_TIMEOUT = 5.0
UPDATE_TIMEOUT = 75.0
CHOICE_POLITE = "polite"
CHOICE_FORCE = "force"
CHOICE_WAIT = "wait"
CHOICES = (CHOICE_POLITE, CHOICE_FORCE, CHOICE_WAIT)
# The one body each choice sends. Nothing else is ever sent to the update route.
_BODIES = {
    CHOICE_POLITE: {"when": "now", "close": "polite"},
    CHOICE_FORCE: {"when": "now", "close": "force"},
    CHOICE_WAIT: {"when": "wait"},
}
STILL_OPEN_CODE = "zotero_still_open"
MAX_MESSAGE_CHARS = 200

# The states status() reports: the four the core route answers. STATE_ERROR is set by this client for any failed or
# unreadable check, including an answer whose state is not one of the four.
STATE_CURRENT = "current"
STATE_UPDATE_READY = "update_ready"
STATE_ZOTERO_RUNNING = "zotero_running"
STATE_NOT_INSTALLED = "not_installed"
STATE_ERROR = "error"
ANSWER_STATES = frozenset({STATE_CURRENT, STATE_UPDATE_READY, STATE_ZOTERO_RUNNING, STATE_NOT_INSTALLED})
# The states where Update bridge is offered: an update is ready (Zotero open or closed), or no bridge is installed.
UPDATABLE_STATES = frozenset({STATE_UPDATE_READY, STATE_ZOTERO_RUNNING, STATE_NOT_INSTALLED})

BAD_CHOICE_MESSAGE = "The desktop sends only the choices close politely, force close and wait. Nothing was sent."
ZOTERO_DID_NOT_CLOSE_MESSAGE = "Zotero did not close, so the bridge was not installed."
BAD_ANSWER_MESSAGE = "The local Meridian server sent a Zotero bridge answer the desktop could not read."
NO_ROUTE_MESSAGE = "The local Meridian server has no Zotero bridge route, so it is too old to manage the bridge."
HTTP_STATUS_MESSAGE = "The local Meridian server returned HTTP {}."
UPDATE_FAILED_MESSAGE = "The local Meridian server could not update the bridge (HTTP {})."
SERVER_CODE_TEXT = " Server code: {}."
UNCHECKED_MESSAGE = "The Zotero bridge could not be checked."

_SAFE_CODE = re.compile(r"^[a-z][a-z_]{0,39}$")
_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,31}$")


@dataclass(frozen=True)
class Reply:
    """One HTTP answer: the status code and the raw body bytes."""

    status: int
    body: bytes


# transport(method, base_url, path, body, timeout) -> Reply. ``body`` is a dict for a POST and None for a GET.
# A transport raises LocalClientError for a failed request. Tests pass a fake, so they need no socket.
Transport = Callable[[str, str, str, dict[str, Any] | None, float], Reply]


@dataclass(frozen=True)
class BridgeStatus:
    """What the bridge reports. ``installed`` and ``expected`` are versions, or None when unknown. ``zotero_running``
    is True in STATE_ZOTERO_RUNNING, False in STATE_UPDATE_READY and None otherwise. ``pending_install`` is True while
    a "wait" choice is recorded. ``reason`` is fixed text, and it is set only for STATE_ERROR."""

    state: str
    installed: str | None = None
    expected: str | None = None
    zotero_running: bool | None = None
    pending_install: bool = False
    reason: str = ""

    @property
    def can_update(self) -> bool:
        return self.state in UPDATABLE_STATES


@dataclass(frozen=True)
class BridgeResult:
    """The outcome of one update. ``line`` is the server's short message when ok. ``reason`` is fixed text when not ok.
    ``still_open`` is True only when a polite close left Zotero open, so nothing was installed.
    """

    ok: bool
    line: str = ""
    reason: str = ""
    still_open: bool = False


def choice_body(choice: object) -> dict[str, str] | None:
    """The one fixed body that ``choice`` sends, as a new dict, or None when it is not one of the three choices."""
    if not isinstance(choice, str) or choice not in _BODIES:
        return None
    return dict(_BODIES[choice])


def build_request(method: str, base_url: str, path: str, body: dict[str, Any] | None) -> urllib.request.Request:
    """The request for one call. A non-loopback address raises LocalClientError, and nothing is built to send."""
    local_client.assert_loopback(base_url)
    headers = {"User-Agent": local_client.USER_AGENT, "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    return urllib.request.Request(base_url.rstrip("/") + path, data=data, method=method, headers=headers)


def _default_transport(method: str, base_url: str, path: str, body: dict[str, Any] | None, timeout: float) -> Reply:
    request = build_request(method, base_url, path, body)
    deadline = time.monotonic() + timeout
    try:
        # An empty ProxyHandler keeps loopback traffic off any system proxy, and redirects are refused.
        with local_client._opener().open(request, timeout=timeout) as response:
            return Reply(response.status, local_client._bounded_read(response, deadline))
    except urllib.error.HTTPError as exc:
        try:
            return Reply(exc.code, local_client._bounded_read(exc, deadline))
        finally:
            exc.close()
    except urllib.error.URLError as exc:
        raise local_client._transport_error(exc.reason) from None
    except TimeoutError:
        raise LocalClientError("timeout", local_client.TIMEOUT_MESSAGE) from None
    except (OSError, http.client.HTTPException):
        raise LocalClientError("unreachable", local_client.UNREACHABLE_MESSAGE) from None
    except ValueError:
        raise LocalClientError("bad_url", local_client.BAD_URL_MESSAGE) from None


def _send(
    method: str,
    path: str,
    base_url: str,
    body: dict[str, Any] | None,
    transport: Transport | None,
    timeout: float,
) -> Reply:
    # The loopback check runs first, so a non-loopback address sends nothing, even to an injected transport.
    local_client.assert_loopback(base_url)
    fetch = _default_transport if transport is None else transport
    return fetch(method, base_url, path, body, timeout)


def status(base_url: str, *, transport: Transport | None = None, timeout: float = STATUS_TIMEOUT) -> BridgeStatus:
    """Read the bridge at ``base_url``. Never raises. A failed or unreadable check is STATE_ERROR with fixed text."""
    try:
        reply = _send("GET", STATUS_PATH, base_url, None, transport, timeout)
    except LocalClientError as exc:
        return BridgeStatus(STATE_ERROR, reason=str(exc))
    except Exception:  # noqa: BLE001 - a status check must never raise into the UI
        return BridgeStatus(STATE_ERROR, reason=UNCHECKED_MESSAGE)
    return parse_status(reply)


def update(
    base_url: str,
    choice: str,
    *,
    transport: Transport | None = None,
    timeout: float = UPDATE_TIMEOUT,
) -> BridgeResult:
    """Send the owner's choice: polite, force or wait. Never raises. Any other choice sends nothing."""
    body = choice_body(choice)
    if body is None:
        return BridgeResult(False, reason=BAD_CHOICE_MESSAGE)
    try:
        reply = _send("POST", UPDATE_PATH, base_url, body, transport, timeout)
    except LocalClientError as exc:
        return BridgeResult(False, reason=str(exc))
    except Exception:  # noqa: BLE001 - an update must never raise into the UI
        return BridgeResult(False, reason=UNCHECKED_MESSAGE)
    return parse_update(reply)


def parse_status(reply: Reply) -> BridgeStatus:
    """The status in one answer. A state that is not one of the four core states is an error with fixed text."""
    if 300 <= reply.status < 400:
        return BridgeStatus(STATE_ERROR, reason=local_client.REDIRECT_MESSAGE)
    if reply.status == 404:
        return BridgeStatus(STATE_ERROR, reason=NO_ROUTE_MESSAGE)
    if reply.status != 200:
        return BridgeStatus(STATE_ERROR, reason=HTTP_STATUS_MESSAGE.format(reply.status))
    payload = _json_object(reply.body)
    state = payload.get("state") if payload is not None else None
    if not isinstance(state, str) or state not in ANSWER_STATES:
        return BridgeStatus(STATE_ERROR, reason=BAD_ANSWER_MESSAGE)
    assert payload is not None  # a known state can only come from a JSON object
    return BridgeStatus(
        state=state,
        installed=_version(payload.get("installed_version")),
        expected=_version(payload.get("expected_version")),
        zotero_running=_zotero_running(state),
        # Only a boolean true counts as a recorded wait. Anything else reads as not pending.
        pending_install=payload.get("pending_install") is True,
    )


def parse_update(reply: Reply) -> BridgeResult:
    """The outcome in one answer. A failure names the HTTP status and the server's safe error code, never its body."""
    if 300 <= reply.status < 400:
        return BridgeResult(False, reason=local_client.REDIRECT_MESSAGE)
    if reply.status == 404:
        return BridgeResult(False, reason=NO_ROUTE_MESSAGE)
    if reply.status != 200:
        code = _server_code(reply.body)
        if code == STILL_OPEN_CODE:
            return BridgeResult(False, reason=ZOTERO_DID_NOT_CLOSE_MESSAGE, still_open=True)
        text = UPDATE_FAILED_MESSAGE.format(reply.status)
        if code:
            text += SERVER_CODE_TEXT.format(code)
        return BridgeResult(False, reason=text)
    payload = _json_object(reply.body)
    if payload is None:
        return BridgeResult(False, reason=BAD_ANSWER_MESSAGE)
    return BridgeResult(True, line=_line(payload.get("message")))


def _json_object(body: bytes) -> dict[str, Any] | None:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _server_code(body: bytes) -> str:
    """The server's error.code when it is a short lowercase slug, otherwise an empty string."""
    payload = _json_object(body)
    error = payload.get("error") if payload is not None else None
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) and _SAFE_CODE.fullmatch(code) else ""


def _version(value: object) -> str | None:
    return value if isinstance(value, str) and _VERSION.fullmatch(value) else None


def _zotero_running(state: str) -> bool | None:
    """The Zotero fact a state carries: True while Zotero is open, False when the core found it closed, else None."""
    if state == STATE_ZOTERO_RUNNING:
        return True
    if state == STATE_UPDATE_READY:
        return False
    return None


def _line(value: object) -> str:
    """The server's message as one short line: whitespace collapsed and a length cap.
    Anything that is not text is empty.
    """
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    return text if len(text) <= MAX_MESSAGE_CHARS else text[: MAX_MESSAGE_CHARS - 3] + "..."


__all__ = [
    "ANSWER_STATES",
    "BAD_ANSWER_MESSAGE",
    "BAD_CHOICE_MESSAGE",
    "CHOICES",
    "CHOICE_FORCE",
    "CHOICE_POLITE",
    "CHOICE_WAIT",
    "HTTP_STATUS_MESSAGE",
    "MAX_MESSAGE_CHARS",
    "NO_ROUTE_MESSAGE",
    "STATE_CURRENT",
    "STATE_ERROR",
    "STATE_NOT_INSTALLED",
    "STATE_UPDATE_READY",
    "STATE_ZOTERO_RUNNING",
    "STATUS_PATH",
    "STATUS_TIMEOUT",
    "STILL_OPEN_CODE",
    "UNCHECKED_MESSAGE",
    "UPDATABLE_STATES",
    "UPDATE_FAILED_MESSAGE",
    "UPDATE_PATH",
    "UPDATE_TIMEOUT",
    "ZOTERO_DID_NOT_CLOSE_MESSAGE",
    "BridgeResult",
    "BridgeStatus",
    "Reply",
    "Transport",
    "build_request",
    "choice_body",
    "parse_status",
    "parse_update",
    "status",
    "update",
]
