"""Tests for the browser sign-in protocol (meridian_desktop/hosted_sign_in.py). Pure Python.

Every request goes through a fake transport that returns scripted replies, and the clock and the sleep are fakes, so no
test waits or reaches the network. FAKE_DEVICE and FAKE_TOKEN are obviously fake values, so the leak checks can search
for them.
"""

from __future__ import annotations

import json
import math
import urllib.parse

import pytest

from meridian_desktop import hosted_sign_in as sign
from meridian_desktop.hosted_client import BadBaseUrl, HostedError, Reply

BASE = "https://hosted.test"
FAKE_DEVICE = "device-fake-sign-in-0001"
FAKE_TOKEN = "sk_meridian_fake-sign-in-0001"
PAGE = "https://hosted.test/activate?code=WDJB-MJHT"
START_REPLY = {
    "device_code": FAKE_DEVICE,
    "user_code": "WDJB-MJHT",
    "verification_uri": "https://hosted.test/activate",
    "verification_uri_complete": PAGE,
    "expires_in": 300,
    "interval": 5,
}


class Scripted:
    """A transport that returns the scripted replies in order, or raises a scripted error. It records each request."""

    def __init__(self, *items: object) -> None:
        self.items = list(items)
        self.requests: list = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class Clock:
    """A fake clock: each sleep advances it, so a wait can be followed without real time passing."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> bool:
        self.sleeps.append(seconds)
        self.now += seconds
        return False


def _json(data: object, status: int = 200) -> Reply:
    return Reply(status, json.dumps(data).encode("utf-8"))


def _device(**changes: object) -> sign.DeviceSignIn:
    fields = {
        "device_code": FAKE_DEVICE,
        "user_code": "WDJB-MJHT",
        "page_url": PAGE,
        "interval": 5.0,
        "expires_in": 300.0,
    }
    fields.update(changes)
    return sign.DeviceSignIn(**fields)


def _form(request) -> dict[str, list[str]]:
    return urllib.parse.parse_qs(request.data.decode("ascii"))


# ----------------------------------------------------------- starting a sign-in


def test_start_posts_to_the_device_path_with_no_token_and_reads_the_code():
    transport = Scripted(_json(START_REPLY))

    device = sign.start_sign_in(BASE, transport=transport)

    request = transport.requests[0]
    assert request.get_method() == "POST"
    assert request.full_url == BASE + "/oauth/device"
    assert request.get_header("Authorization") is None, "the sign-in is sent with no token"
    assert device.user_code == "WDJB-MJHT"
    assert device.page_url == PAGE
    assert device.interval == 5.0
    assert device.expires_in == 300.0


def test_the_device_code_is_left_out_of_the_repr():
    device = sign.start_sign_in(BASE, transport=Scripted(_json(START_REPLY)))

    assert FAKE_DEVICE not in repr(device)
    assert device.device_code == FAKE_DEVICE, "the poll still has the code it needs"


def test_a_bad_hosted_url_is_refused_before_any_request():
    transport = Scripted(_json(START_REPLY))

    with pytest.raises(BadBaseUrl):
        sign.start_sign_in("http://hosted.test", transport=transport)
    assert transport.requests == []


def test_a_page_the_server_reports_over_http_is_opened_over_the_hosted_urls_https():
    reply = dict(START_REPLY, verification_uri_complete="http://hosted.test/activate?code=WDJB-MJHT")

    device = sign.start_sign_in(BASE, transport=Scripted(_json(reply)))

    assert device.page_url == PAGE, "the scheme comes from the hosted URL the owner chose, never from the reply"


def test_a_page_on_another_origin_is_refused_and_the_text_names_no_host():
    reply = dict(START_REPLY, verification_uri_complete="https://evil.test/activate?code=WDJB-MJHT")

    with pytest.raises(HostedError) as info:
        sign.start_sign_in(BASE, transport=Scripted(_json(reply)))

    assert info.value.code == "bad_reply"
    assert "evil.test" not in str(info.value)


@pytest.mark.parametrize(
    "change",
    [
        {"device_code": None},
        {"user_code": ""},
        {"verification_uri_complete": None, "verification_uri": None},
        {"expires_in": True},
        {"expires_in": 0},
    ],
)
def test_a_start_reply_that_lacks_a_usable_field_is_refused(change):
    reply = dict(START_REPLY, **change)

    with pytest.raises(HostedError) as info:
        sign.start_sign_in(BASE, transport=Scripted(_json(reply)))

    assert info.value.code == "bad_reply"


def test_a_start_reply_that_is_not_2xx_keeps_its_status():
    with pytest.raises(HostedError) as info:
        sign.start_sign_in(BASE, transport=Scripted(_json({}, status=500)))

    assert info.value.code == "http_500"
    assert info.value.status == 500


def test_a_lost_connection_on_start_is_reported_as_unreachable():
    from meridian_desktop.hosted_client import HostedUnreachable

    with pytest.raises(HostedUnreachable):
        sign.start_sign_in(BASE, transport=Scripted(OSError("connection refused")))


def test_the_interval_has_a_default_and_a_floor_and_the_lifetime_is_capped():
    no_interval = {key: value for key, value in START_REPLY.items() if key != "interval"}
    assert sign.start_sign_in(BASE, transport=Scripted(_json(no_interval))).interval == sign.DEFAULT_INTERVAL

    fast = dict(START_REPLY, interval=0.2)
    assert sign.start_sign_in(BASE, transport=Scripted(_json(fast))).interval == 1.0

    long = dict(START_REPLY, expires_in=99999)
    assert sign.start_sign_in(BASE, transport=Scripted(_json(long))).expires_in == sign.MAX_LIFETIME


# ----------------------------------------------------------- waiting for approval


def test_poll_waits_through_pending_replies_and_returns_the_token():
    transport = Scripted(
        _json({"error": "authorization_pending"}),
        _json({"error": "authorization_pending"}),
        _json({"access_token": FAKE_TOKEN, "token_type": "bearer", "expires_in": 7776000}),
    )
    clock = Clock()

    result = sign.poll_sign_in(BASE, _device(), transport=transport, sleep=clock.sleep, clock=clock)

    assert result.outcome == sign.APPROVED
    assert result.token == FAKE_TOKEN
    assert clock.sleeps == [5.0, 5.0, 5.0], "one wait before each poll"
    request = transport.requests[-1]
    assert request.full_url == BASE + "/oauth/token"
    assert request.get_header("Authorization") is None
    assert _form(request) == {
        "grant_type": ["urn:ietf:params:oauth:grant-type:device_code"],
        "device_code": [FAKE_DEVICE],
    }
    assert FAKE_TOKEN not in repr(result), "the token is left out of the repr"


def test_slow_down_adds_five_seconds_to_every_wait_after_it():
    transport = Scripted(
        _json({"error": "slow_down"}),
        _json({"error": "authorization_pending"}),
        _json({"access_token": FAKE_TOKEN}),
    )
    clock = Clock()

    sign.poll_sign_in(BASE, _device(), transport=transport, sleep=clock.sleep, clock=clock)

    assert clock.sleeps == [5.0, 10.0, 10.0]


@pytest.mark.parametrize(
    ("error", "outcome"),
    [("access_denied", sign.DENIED), ("expired_token", sign.EXPIRED)],
)
def test_a_denial_or_an_expired_code_ends_the_wait(error, outcome):
    clock = Clock()

    result = sign.poll_sign_in(
        BASE, _device(), transport=Scripted(_json({"error": error}, status=400)), sleep=clock.sleep, clock=clock
    )

    assert result.outcome == outcome
    assert result.token is None


@pytest.mark.parametrize(
    "reply",
    [
        Reply(200, b"<html>not json</html>"),
        _json({"access_token": "two words"}),
        _json({"access_token": 12}),
        _json({"error": "server_error"}),
        _json([]),
    ],
)
def test_a_reply_the_desktop_cannot_read_ends_the_wait_without_a_token(reply):
    clock = Clock()

    result = sign.poll_sign_in(BASE, _device(), transport=Scripted(reply), sleep=clock.sleep, clock=clock)

    assert result.outcome == sign.BAD_REPLY
    assert result.token is None


def test_a_lost_connection_while_waiting_ends_the_wait_as_unreachable():
    clock = Clock()

    result = sign.poll_sign_in(
        BASE, _device(), transport=Scripted(OSError("reset")), sleep=clock.sleep, clock=clock
    )

    assert result.outcome == sign.UNREACHABLE


def test_stopping_the_wait_sends_no_request():
    transport = Scripted(_json({"access_token": FAKE_TOKEN}))
    clock = Clock()

    result = sign.poll_sign_in(
        BASE, _device(), transport=transport, should_stop=lambda: True, sleep=clock.sleep, clock=clock
    )

    assert result.outcome == sign.STOPPED
    assert transport.requests == []
    assert clock.sleeps == [5.0]


def test_the_wait_ends_at_the_lifetime_without_a_request_after_it():
    transport = Scripted(_json({"error": "authorization_pending"}), _json({"error": "authorization_pending"}))
    clock = Clock()

    result = sign.poll_sign_in(BASE, _device(expires_in=12.0), transport=transport, sleep=clock.sleep, clock=clock)

    assert result.outcome == sign.EXPIRED
    assert len(transport.requests) == 2, "the third wait ends past the lifetime, so it sends nothing"


def test_a_bad_hosted_url_is_refused_before_any_poll_request():
    transport = Scripted(_json({"access_token": FAKE_TOKEN}))

    with pytest.raises(BadBaseUrl):
        sign.poll_sign_in("http://hosted.test", _device(), transport=transport, sleep=lambda seconds: None)
    assert transport.requests == []


@pytest.mark.parametrize("field", ["expires_in", "interval"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_time_is_refused_or_defaulted_and_never_disables_the_lifetime(field, value):
    reply = dict(START_REPLY, **{field: value})

    if field == "expires_in":
        with pytest.raises(HostedError) as info:
            sign.start_sign_in(BASE, transport=Scripted(_json(reply)))
        assert info.value.code == "bad_reply"
    else:
        device = sign.start_sign_in(BASE, transport=Scripted(_json(reply)))
        assert device.interval == sign.DEFAULT_INTERVAL
        assert math.isfinite(device.expires_in)


def test_the_interval_never_outlasts_the_lifetime_so_the_poll_asks_before_expiry():
    reply = dict(START_REPLY, expires_in=20, interval=300)

    device = sign.start_sign_in(BASE, transport=Scripted(_json(reply)))

    assert device.expires_in == 20.0
    assert device.interval == 20.0, "a poll waiting 300 seconds would miss a 20-second code"
