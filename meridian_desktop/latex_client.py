"""Probe for the local LaTeX engine over loopback. Pure Python: no Qt import.

The engine is a separate program the owner runs on this computer. The desktop never
bundles, copies or reads its files. The probe asks two routes in order, GET /health and
then GET /extension-version. The engine counts as connected only when both answer with
HTTP 200 and a JSON object. The version each route reports is shown as sent when it is
a plain version string, and as "not reported" otherwise.

The requests go through the loopback GET in local_client, so they keep its limits: an
http URL on 127.0.0.1, localhost or ::1, no redirects, no proxy, a response size cap
and a deadline. Results carry a state and fixed text. They never carry response body text.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from meridian_desktop import local_client
from meridian_desktop.local_client import LocalClientError

ENGINE_CONNECTED = "connected"
ENGINE_NOT_RUNNING = "not_running"
ENGINE_NOT_ANSWERING = "not_answering"
ENGINE_ERROR = "error"

HEALTH_PATH = "/health"
EXTENSION_VERSION_PATH = "/extension-version"
PROBE_TIMEOUT = 2.0
NOT_REPORTED = "not reported"

CONNECTED_MESSAGE = "Both the health and extension-version checks answered."
NOT_RUNNING_MESSAGE = "No LaTeX engine is running at this address on this computer."
NOT_ANSWERING_MESSAGE = "The address did not answer both the health and extension-version checks."
NOT_LOOPBACK_MESSAGE = "The engine address in Settings is not a loopback address, so no request was sent."
TIMEOUT_MESSAGE = "The LaTeX engine did not answer in time."
UNREACHABLE_MESSAGE = "The LaTeX engine could not be reached."
TOO_LARGE_MESSAGE = "The LaTeX engine sent a response that is too large to read."
UNCHECKED_ERROR_MESSAGE = "The LaTeX engine could not be checked."

# A reported version is a short token such as 1.4.0 or 0.9.2-beta. Anything else is not shown.
_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z._+-]{0,63}")

_ERROR_MESSAGES = {
    "not_running": (ENGINE_NOT_RUNNING, NOT_RUNNING_MESSAGE),
    "timeout": (ENGINE_ERROR, TIMEOUT_MESSAGE),
    "unreachable": (ENGINE_ERROR, UNREACHABLE_MESSAGE),
    "too_large": (ENGINE_ERROR, TOO_LARGE_MESSAGE),
    "not_loopback": (ENGINE_ERROR, NOT_LOOPBACK_MESSAGE),
}


@dataclass(frozen=True)
class EngineStatus:
    """The result of one probe. The versions are set only when the engine is connected."""

    state: str
    message: str
    engine_version: str | None = None
    extension_version: str | None = None

    @property
    def connected(self) -> bool:
        return self.state == ENGINE_CONNECTED


class _NoAnswer(Exception):
    """A route did not answer with HTTP 200 and a JSON object."""


def _json_object(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise _NoAnswer() from None
    if not isinstance(value, dict):
        raise _NoAnswer()
    return value


def _json_answer(reply: Any) -> dict[str, Any]:
    """Return the JSON object from a 200 reply. Any other reply is no answer."""
    if reply.status != 200:
        raise _NoAnswer()
    return _json_object(reply.body)


def _version(payload: dict[str, Any]) -> str:
    value = payload.get("version")
    if isinstance(value, str) and _VERSION.fullmatch(value):
        return value
    return NOT_REPORTED


def _from_error(error: LocalClientError) -> EngineStatus:
    state, message = _ERROR_MESSAGES.get(error.code, (ENGINE_ERROR, UNCHECKED_ERROR_MESSAGE))
    return EngineStatus(state, message)


def probe_engine(base_url: str, timeout: float = PROBE_TIMEOUT, get: Callable[..., Any] | None = None) -> EngineStatus:
    """Probe the engine at ``base_url`` and return what was found.

    ``get`` is the request function, called as get(base_url, path, timeout) and returning
    an object with ``status`` and ``body``. It defaults to the loopback client's GET. Tests
    pass a fake. The address is checked for loopback before any request, so a fake is
    never handed a non-loopback address either.
    """
    try:
        local_client.assert_loopback(base_url)
    except LocalClientError:
        return EngineStatus(ENGINE_ERROR, NOT_LOOPBACK_MESSAGE)

    fetch = local_client._get if get is None else get
    try:
        health = _json_answer(fetch(base_url, HEALTH_PATH, timeout))
        extension = _json_answer(fetch(base_url, EXTENSION_VERSION_PATH, timeout))
    except LocalClientError as exc:
        return _from_error(exc)
    except _NoAnswer:
        return EngineStatus(ENGINE_NOT_ANSWERING, NOT_ANSWERING_MESSAGE)
    return EngineStatus(
        ENGINE_CONNECTED,
        CONNECTED_MESSAGE,
        engine_version=_version(health),
        extension_version=_version(extension),
    )


__all__ = [
    "ENGINE_CONNECTED",
    "ENGINE_ERROR",
    "ENGINE_NOT_ANSWERING",
    "ENGINE_NOT_RUNNING",
    "EXTENSION_VERSION_PATH",
    "HEALTH_PATH",
    "NOT_REPORTED",
    "PROBE_TIMEOUT",
    "EngineStatus",
    "probe_engine",
]
