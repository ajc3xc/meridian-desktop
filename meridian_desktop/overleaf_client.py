"""Client for the LaTeX engine's Overleaf session routes. Pure Python: stdlib urllib only, no Qt import.

The LaTeX engine is the local backup server on 127.0.0.1:8471. It keeps the owner's Overleaf session, and the desktop
asks it over loopback. Only these two routes are used:

    GET  {engine_url}/overleaf/session   checks the saved session once (one request to Overleaf per call)
    POST {engine_url}/overleaf/login     opens a visible login window in Chrome; the owner signs in there

- session_status() is called only when the owner presses Check, once per press. The timeout is 40 seconds, because
  Overleaf can be slow.
- start_login() is called only when the owner presses Log in. The timeout is 10 seconds.

Both functions return a plain result and never raise. The address is checked for loopback before any transport call, so
a non-loopback address sends nothing at all. The only header either call sends is X-Meridian-Client, which must be
non-empty. No cookie, token or Authorization header is ever sent. The engine never sends a cookie, and no answer body is
copied into a result: a result holds the engine's state words, a flag, and fixed text only.

Engine answers:
    GET 200   {"state": "logged_in" | "expired" | "missing" | "unknown", "savedAt": ..., "chromeFound": true | false, ...}
    POST 200  {"started": true}   or   {"started": false, "reason": "already_open" | "chrome_missing"}
A refused connection means the engine is not running. A timeout means Overleaf did not answer in time, and reads as
the "unknown" session state. Any other failure reads as STATE_ERROR with fixed text.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from meridian_desktop import __version__, local_client
from meridian_desktop.local_client import LocalClientError

SESSION_PATH = "/overleaf/session"
LOGIN_PATH = "/overleaf/login"
CLIENT_HEADER = "X-Meridian-Client"
CLIENT_VALUE = f"meridian-desktop/{__version__}"
# Overleaf can be slow, so a session check waits longer than the local probes do. A login only opens a window.
SESSION_TIMEOUT = 40.0
LOGIN_TIMEOUT = 10.0

# The session states the engine reports (its "state" field).
STATE_LOGGED_IN = "logged_in"
STATE_EXPIRED = "expired"
STATE_MISSING = "missing"
STATE_UNKNOWN = "unknown"
SESSION_STATES = frozenset({STATE_LOGGED_IN, STATE_EXPIRED, STATE_MISSING, STATE_UNKNOWN})
# Set by this client only. ENGINE_NOT_RUNNING: a refused connection. ERROR: any other failure, with fixed text.
STATE_ENGINE_NOT_RUNNING = "engine_not_running"
STATE_ERROR = "error"

# The reasons a login can give. The engine's own reasons are the first two; the rest are set by this client.
REASON_ALREADY_OPEN = "already_open"
REASON_CHROME_MISSING = "chrome_missing"
REASON_ENGINE_NOT_RUNNING = "engine_not_running"
REASON_TIMEOUT = "timeout"
REASON_ERROR = "error"
LOGIN_REASONS = frozenset({REASON_ALREADY_OPEN, REASON_CHROME_MISSING})

NOT_LOOPBACK_TEXT = "The LaTeX engine address must be 127.0.0.1, localhost or ::1. Nothing was sent."
NOT_RUNNING_TEXT = "The LaTeX engine is not running."
TIMEOUT_TEXT = "The LaTeX engine did not answer in time."
UNREACHABLE_TEXT = "The LaTeX engine could not be reached."
REDIRECT_TEXT = "The LaTeX engine sent a redirect, which the desktop does not follow."
FORBIDDEN_TEXT = "The LaTeX engine refused the request (HTTP 403)."
HTTP_TEXT = "The LaTeX engine returned HTTP {}."
BAD_ANSWER_TEXT = "The LaTeX engine sent an answer the desktop could not read."
BAD_URL_TEXT = "The LaTeX engine address could not be used."
UNCHECKED_TEXT = "The LaTeX engine session could not be checked."


@dataclass(frozen=True)
class Reply:
    """One HTTP answer: the status code and the raw body bytes. The body is read by the parsers and never copied out."""

    status: int
    body: bytes


# transport(method, url, headers, timeout) -> Reply. It raises LocalClientError for a failed request. Tests pass a fake,
# so they need no socket. ``headers`` holds the X-Meridian-Client header and nothing else.
Transport = Callable[[str, str, Mapping[str, str], float], Reply]


@dataclass(frozen=True)
class SessionStatus:
    """What the engine says about the saved Overleaf session. ``chrome_found`` is None when the engine did not say.
    ``error`` is fixed text, and it is set only for STATE_ERROR."""

    state: str
    chrome_found: bool | None = None
    error: str = ""


@dataclass(frozen=True)
class LoginStatus:
    """The outcome of one login request. ``reason`` is "" when the window opened, else one of the REASON_* codes."""

    started: bool
    reason: str = ""


def _transport_error(reason: object) -> LocalClientError:
    """Map the reason of a failed connection. A refused connection means no engine is listening. A timeout means the
    engine did not answer in time. Anything else means the engine could not be reached."""
    if isinstance(reason, ConnectionRefusedError):
        return LocalClientError("not_running", NOT_RUNNING_TEXT)
    if isinstance(reason, TimeoutError):
        return LocalClientError("timeout", TIMEOUT_TEXT)
    return LocalClientError("unreachable", UNREACHABLE_TEXT)


def _default_transport(method: str, url: str, headers: Mapping[str, str], timeout: float) -> Reply:
    request = urllib.request.Request(url, method=method, headers=dict(headers))
    deadline = time.monotonic() + timeout
    try:
        # The shared opener has an empty proxy table and refuses redirects, so loopback traffic stays on this machine.
        with local_client._opener().open(request, timeout=timeout) as response:
            return Reply(response.status, local_client._bounded_read(response, deadline))
    except urllib.error.HTTPError as exc:
        try:
            return Reply(exc.code, local_client._bounded_read(exc, deadline))
        finally:
            exc.close()
    except urllib.error.URLError as exc:
        raise _transport_error(exc.reason) from None
    except TimeoutError:
        raise LocalClientError("timeout", TIMEOUT_TEXT) from None
    except (OSError, http.client.HTTPException):
        raise LocalClientError("unreachable", UNREACHABLE_TEXT) from None
    except ValueError:
        raise LocalClientError("bad_url", BAD_URL_TEXT) from None


def _send(method: str, path: str, base_url: str, transport: Transport | None, timeout: float) -> Reply:
    # The loopback check runs first, so a non-loopback address sends nothing, even to an injected transport.
    local_client.assert_loopback(base_url)
    headers = {CLIENT_HEADER: CLIENT_VALUE}
    fetch = _default_transport if transport is None else transport
    return fetch(method, base_url.rstrip("/") + path, headers, timeout)


def _json_object(body: bytes) -> dict[str, Any] | None:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def session_status(
    base_url: str,
    *,
    transport: Transport | None = None,
    timeout: float = SESSION_TIMEOUT,
) -> SessionStatus:
    """Ask the engine once whether the saved Overleaf session works. Never raises.

    Call this only on an explicit Check press. Each call makes one request to Overleaf, on the engine's side.
    """
    try:
        reply = _send("GET", SESSION_PATH, base_url, transport, timeout)
    except LocalClientError as exc:
        return _session_from_error(exc)
    except Exception:  # noqa: BLE001 - a check must never raise into the UI
        return SessionStatus(STATE_ERROR, error=UNCHECKED_TEXT)
    return parse_session(reply)


def _session_from_error(exc: LocalClientError) -> SessionStatus:
    if exc.code == "not_loopback":
        return SessionStatus(STATE_ERROR, error=NOT_LOOPBACK_TEXT)
    if exc.code == "not_running":
        return SessionStatus(STATE_ENGINE_NOT_RUNNING)
    if exc.code == "timeout":
        # Overleaf did not answer in time, which the card shows as "could not reach Overleaf".
        return SessionStatus(STATE_UNKNOWN)
    return SessionStatus(STATE_ERROR, error=UNREACHABLE_TEXT)


def parse_session(reply: Reply) -> SessionStatus:
    """The session in one engine answer. A state that is not one of the four is an error with fixed text."""
    if 300 <= reply.status < 400:
        return SessionStatus(STATE_ERROR, error=REDIRECT_TEXT)
    if reply.status == 403:
        return SessionStatus(STATE_ERROR, error=FORBIDDEN_TEXT)
    if reply.status != 200:
        return SessionStatus(STATE_ERROR, error=HTTP_TEXT.format(reply.status))
    payload = _json_object(reply.body)
    state = payload.get("state") if payload is not None else None
    if not isinstance(state, str) or state not in SESSION_STATES:
        return SessionStatus(STATE_ERROR, error=BAD_ANSWER_TEXT)
    chrome = payload.get("chromeFound") if payload is not None else None
    return SessionStatus(state, chrome_found=chrome if isinstance(chrome, bool) else None)


def start_login(
    base_url: str,
    *,
    transport: Transport | None = None,
    timeout: float = LOGIN_TIMEOUT,
) -> LoginStatus:
    """Ask the engine to open its login window. Never raises. Call this only on an explicit Log in press."""
    try:
        reply = _send("POST", LOGIN_PATH, base_url, transport, timeout)
    except LocalClientError as exc:
        if exc.code == "not_running":
            return LoginStatus(False, REASON_ENGINE_NOT_RUNNING)
        if exc.code == "timeout":
            return LoginStatus(False, REASON_TIMEOUT)
        return LoginStatus(False, REASON_ERROR)
    except Exception:  # noqa: BLE001 - a login request must never raise into the UI
        return LoginStatus(False, REASON_ERROR)
    return parse_login(reply)


def parse_login(reply: Reply) -> LoginStatus:
    """The outcome in one engine answer. Anything other than a 200 with a boolean "started" is REASON_ERROR."""
    if reply.status != 200:
        return LoginStatus(False, REASON_ERROR)
    payload = _json_object(reply.body)
    started = payload.get("started") if payload is not None else None
    if not isinstance(started, bool):
        return LoginStatus(False, REASON_ERROR)
    if started:
        return LoginStatus(True)
    reason = payload.get("reason") if payload is not None else None
    return LoginStatus(False, reason if isinstance(reason, str) and reason in LOGIN_REASONS else REASON_ERROR)


__all__ = [
    "CLIENT_HEADER",
    "CLIENT_VALUE",
    "LOGIN_PATH",
    "LOGIN_REASONS",
    "LOGIN_TIMEOUT",
    "REASON_ALREADY_OPEN",
    "REASON_CHROME_MISSING",
    "REASON_ENGINE_NOT_RUNNING",
    "REASON_ERROR",
    "REASON_TIMEOUT",
    "SESSION_PATH",
    "SESSION_STATES",
    "SESSION_TIMEOUT",
    "STATE_ENGINE_NOT_RUNNING",
    "STATE_ERROR",
    "STATE_EXPIRED",
    "STATE_LOGGED_IN",
    "STATE_MISSING",
    "STATE_UNKNOWN",
    "LoginStatus",
    "Reply",
    "SessionStatus",
    "Transport",
    "parse_login",
    "parse_session",
    "session_status",
    "start_login",
]
