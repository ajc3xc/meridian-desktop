"""Sign the desktop in to hosted Meridian in the browser, with the OAuth device grant (RFC 8628). Pure Python: no Qt import.

The owner presses Sign in with browser in Settings. start_sign_in asks hosted Meridian for a device code: POST /oauth/device
with no body and no token. The reply holds a short user code to type, a page to open, and the interval and lifetime. The
desktop opens that page, the owner signs in there and approves the code, and poll_sign_in asks POST /oauth/token with the
device grant every interval seconds. The poll ends with an access token, or a denial, or an expiry, or the owner stopping
the wait.

The access token is a hosted API token, the same kind the Token box saves, so the caller keeps it in the keychain through
token_store. This module keeps nothing. The device code lives only in the DeviceSignIn the caller holds, and it is left out
of its repr. The token is left out of the SignInResult repr. No error text holds either one.

Every request goes through hosted_client.post_to, so the hosted URL is checked first, no redirect is followed, and the
injected transport is the one that sends. The page URL the server gives must be on the same host as the hosted URL. The desktop opens it
over the hosted URL's scheme, so a reply that names another host, or a plain-http copy, never reaches the browser as given.
"""

from __future__ import annotations

import json
import math
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from meridian_desktop import hosted_client
from meridian_desktop.hosted_client import HOSTED_TIMEOUT, HostedError, Reply, Transport

DEVICE_PATH = "/oauth/device"
TOKEN_PATH = "/oauth/token"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"
# The poll interval when the server gives none, and the step a slow_down reply adds to it (RFC 8628 section 3.5).
DEFAULT_INTERVAL = 5.0
SLOW_DOWN_STEP = 5.0
# The longest wait the desktop allows, whatever the server says. The server gives 300 seconds today.
MAX_LIFETIME = 900.0
MAX_DEVICE_CODE_LENGTH = 256
MAX_USER_CODE_LENGTH = 32
MAX_PAGE_LENGTH = 2048

# The outcomes of one sign-in. A poll ends on one of them.
APPROVED = "approved"
DENIED = "denied"
EXPIRED = "expired"
STOPPED = "stopped"
UNREACHABLE = "unreachable"
BAD_REPLY = "bad_reply"
# The poll keeps going on these: the owner has not answered yet, or the server asked the desktop to wait longer.
PENDING = "pending"
SLOW_DOWN = "slow_down"

_ERROR_OUTCOMES = {
    "authorization_pending": PENDING,
    "slow_down": SLOW_DOWN,
    "access_denied": DENIED,
    "expired_token": EXPIRED,
}

START_BAD_REPLY_TEXT = "Hosted Meridian did not start a sign-in with a page and code the desktop can use."


@dataclass(frozen=True)
class DeviceSignIn:
    """What one sign-in needs: the code to type, the page to open, the poll interval and the lifetime in seconds.

    ``device_code`` is the secret that approval needs. It is kept out of the repr, so it is never shown by accident.
    """

    device_code: str = field(repr=False)
    user_code: str
    page_url: str
    interval: float
    expires_in: float


@dataclass(frozen=True)
class SignInResult:
    """How a poll ended: one of the outcomes above. ``token`` is set only for APPROVED, and it is kept out of the repr."""

    outcome: str
    token: str | None = field(default=None, repr=False)


def _never() -> bool:
    return False


def _json_object(body: bytes) -> dict[str, Any] | None:
    """The body as one JSON object, or None when it is anything else."""
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _text(value: object, limit: int) -> str | None:
    """The value stripped, when it is text of 1 to ``limit`` characters. None otherwise."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or len(cleaned) > limit:
        return None
    return cleaned


def _seconds(value: object) -> float | None:
    """The value as seconds, when it is a finite positive number that is not a boolean. None otherwise.

    json.loads accepts NaN and Infinity, and neither is a usable time, so both are refused here.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return float(value)


def _page_on_base(page: str, base: str) -> str | None:
    """The page rebuilt on the hosted URL's scheme and host and port, or None when the page is on another host.

    The server builds the page from the address it was asked on, which can say http behind a proxy. The desktop keeps the
    host the owner chose and its https scheme, so the browser never opens a plain-http copy of the page.
    """
    page_parts = urllib.parse.urlsplit(page)
    base_parts = urllib.parse.urlsplit(base)
    if not page_parts.netloc or page_parts.netloc.lower() != base_parts.netloc.lower():
        return None
    return urllib.parse.urlunsplit((base_parts.scheme, base_parts.netloc, page_parts.path, page_parts.query, ""))


def _device_from(data: dict[str, Any], base: str) -> DeviceSignIn | None:
    """The sign-in the server described, or None when the reply lacks a field or its page is on another host."""
    device_code = _text(data.get("device_code"), MAX_DEVICE_CODE_LENGTH)
    user_code = _text(data.get("user_code"), MAX_USER_CODE_LENGTH)
    page = _text(data.get("verification_uri_complete") or data.get("verification_uri"), MAX_PAGE_LENGTH)
    expires = _seconds(data.get("expires_in"))
    if device_code is None or user_code is None or page is None or expires is None:
        return None
    page_url = _page_on_base(page, base)
    if page_url is None:
        return None
    lifetime = min(expires, MAX_LIFETIME)
    interval = _seconds(data.get("interval")) or DEFAULT_INTERVAL
    # The interval is floored at one second, and capped at the lifetime, so the poll always asks before the code expires.
    return DeviceSignIn(
        device_code=device_code,
        user_code=user_code,
        page_url=page_url,
        interval=min(max(1.0, interval), lifetime),
        expires_in=lifetime,
    )


def start_sign_in(
    base_url: str,
    *,
    transport: Transport | None = None,
    timeout: float = HOSTED_TIMEOUT,
) -> DeviceSignIn:
    """Ask hosted Meridian for a device code and a page to approve it on.

    Raise BadBaseUrl before any request when the URL may not be used. Raise HostedError with fixed text when the reply is
    not a 2xx status, or lacks a field, or names a page on another origin. A timeout or a lost connection raises the
    HostedTimeout or HostedUnreachable that hosted_client raises.
    """
    base = hosted_client.validate_base_url(base_url)
    reply = hosted_client.post_to(base, DEVICE_PATH, b"", transport=transport, timeout=timeout)
    if not 200 <= reply.status < 300:
        raise HostedError(
            f"http_{reply.status}",
            f"Hosted Meridian did not start a sign-in (HTTP {reply.status}).",
            reply.status,
        )
    data = _json_object(reply.body)
    device = _device_from(data, base) if data is not None else None
    if device is None:
        raise HostedError("bad_reply", START_BAD_REPLY_TEXT)
    return device


def _read_poll_reply(reply: Reply) -> tuple[str, str | None]:
    """The outcome of one poll reply, and the token when it approved. Any reply the desktop cannot read is BAD_REPLY."""
    data = _json_object(reply.body)
    if data is None:
        return BAD_REPLY, None
    if "access_token" in data:
        token = data.get("access_token")
        try:
            cleaned = hosted_client.usable_token(token)
        except HostedError:
            return BAD_REPLY, None
        return APPROVED, cleaned
    error = data.get("error")
    if isinstance(error, str) and error in _ERROR_OUTCOMES:
        return _ERROR_OUTCOMES[error], None
    return BAD_REPLY, None


def poll_sign_in(
    base_url: str,
    device: DeviceSignIn,
    *,
    transport: Transport | None = None,
    timeout: float = HOSTED_TIMEOUT,
    should_stop: Callable[[], bool] = _never,
    sleep: Callable[[float], object] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> SignInResult:
    """Wait for the owner to approve the device code, and return how the wait ended.

    The first poll comes one interval after the call. ``should_stop`` is checked after each wait, so the caller can end the
    wait; ``sleep`` may return early for that reason. The wait ends at the code's lifetime. A slow_down reply adds
    SLOW_DOWN_STEP to the interval. No request is sent after the wait has stopped or expired.
    """
    base = hosted_client.validate_base_url(base_url)
    body = urllib.parse.urlencode({"grant_type": DEVICE_GRANT, "device_code": device.device_code}).encode("ascii")
    deadline = clock() + device.expires_in
    interval = device.interval
    while True:
        sleep(interval)
        if should_stop():
            return SignInResult(STOPPED)
        if clock() >= deadline:
            return SignInResult(EXPIRED)
        try:
            reply = hosted_client.post_to(
                base, TOKEN_PATH, body, content_type=FORM_CONTENT_TYPE, transport=transport, timeout=timeout
            )
        except HostedError:
            return SignInResult(UNREACHABLE)
        outcome, token = _read_poll_reply(reply)
        if outcome == PENDING:
            continue
        if outcome == SLOW_DOWN:
            interval += SLOW_DOWN_STEP
            continue
        return SignInResult(outcome, token)


__all__ = [
    "APPROVED",
    "BAD_REPLY",
    "DENIED",
    "EXPIRED",
    "STOPPED",
    "UNREACHABLE",
    "DeviceSignIn",
    "SignInResult",
    "poll_sign_in",
    "start_sign_in",
]
