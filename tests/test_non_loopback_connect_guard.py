"""The non-loopback connect guard in conftest.py. No test here reaches a real host.

A test may connect to 127.0.0.1, ::1 and localhost. Any other host is refused before any bytes are sent, and the attempt
is recorded as host and port only. The refusal cases use names that never resolve (example.invalid is a reserved name)
or the documentation range 192.0.2.0/24. Each is refused before the socket library runs, so nothing is sent. The
loopback case uses a server that this test starts on 127.0.0.1.
"""

import errno
import http.client
import socket

import pytest


@pytest.mark.parametrize(
    ("address", "target"),
    [
        (("example.invalid", 443), ("example.invalid", 443)),
        (("192.0.2.1", 443), ("192.0.2.1", 443)),
        # The allowed set is exactly 127.0.0.1, ::1 and localhost, so the rest of 127/8 is refused too.
        (("127.0.0.2", 7000), ("127.0.0.2", 7000)),
        (("0.0.0.0", 80), ("0.0.0.0", 80)),
        (("::ffff:127.0.0.1", 7000), ("::ffff:127.0.0.1", 7000)),
        (("localhost.example", 80), ("localhost.example", 80)),
        ((b"example.invalid", 443), ("example.invalid", 443)),
    ],
)
def test_every_host_but_loopback_is_a_target_to_refuse(non_loopback_connect_guard, address, target):
    assert non_loopback_connect_guard.target(address) == target


@pytest.mark.parametrize("address", [("127.0.0.1", 7000), ("localhost", 7000), ("::1", 7000), ("::1", 7000, 0, 0)])
def test_loopback_addresses_are_never_a_target(non_loopback_connect_guard, address):
    assert non_loopback_connect_guard.target(address) is None


@pytest.mark.parametrize("address", ["relative-socket-path", None, 7000, ("only-one",)])
def test_an_address_that_is_not_an_internet_tuple_is_never_a_target(non_loopback_connect_guard, address):
    assert non_loopback_connect_guard.target(address) is None


def test_create_connection_to_a_name_is_refused_before_any_lookup(non_loopback_connect_guard, monkeypatch):
    def no_lookup(*args, **kwargs):
        raise AssertionError("the name was looked up before the guard refused it")

    monkeypatch.setattr(socket, "getaddrinfo", no_lookup)
    with pytest.raises(non_loopback_connect_guard.Blocked) as caught:
        socket.create_connection(("example.invalid", 443), timeout=1)
    assert isinstance(caught.value, OSError)
    assert caught.value.errno == errno.ENETUNREACH
    assert non_loopback_connect_guard.attempts == [("example.invalid", 443)]
    non_loopback_connect_guard.attempts.clear()


def test_a_connect_on_a_socket_to_a_name_is_refused_before_the_library_resolves_it(non_loopback_connect_guard):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        with pytest.raises(non_loopback_connect_guard.Blocked):
            sock.connect(("example.invalid", 443))
    assert non_loopback_connect_guard.attempts == [("example.invalid", 443)]
    non_loopback_connect_guard.attempts.clear()


def test_connect_ex_reports_a_refused_connect_and_sends_nothing(non_loopback_connect_guard):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        assert sock.connect_ex(("192.0.2.1", 443)) == errno.ENETUNREACH
    assert non_loopback_connect_guard.attempts == [("192.0.2.1", 443)]
    non_loopback_connect_guard.attempts.clear()


def test_an_http_request_to_a_name_records_host_and_port_only(non_loopback_connect_guard):
    connection = http.client.HTTPConnection("example.invalid", 80, timeout=1)
    with pytest.raises(non_loopback_connect_guard.Blocked):
        connection.request("GET", "/path?secret=not-a-real-value")
    assert non_loopback_connect_guard.attempts == [("example.invalid", 80)]
    assert "secret" not in repr(non_loopback_connect_guard.attempts)
    non_loopback_connect_guard.attempts.clear()


def test_a_connect_to_loopback_by_name_goes_through_and_records_nothing(non_loopback_connect_guard):
    with socket.create_server(("127.0.0.1", 0)) as server:
        server.settimeout(5)
        port = server.getsockname()[1]
        with socket.create_connection(("localhost", port), timeout=5):
            accepted, _ = server.accept()
            accepted.close()
    assert non_loopback_connect_guard.attempts == []


def test_an_unmarked_test_is_not_allowed_a_non_loopback_connect(non_loopback_connect_guard):
    assert not non_loopback_connect_guard.allowed


@pytest.mark.allow_non_loopback_connect
def test_a_test_marked_allow_non_loopback_connect_gets_past_the_guard(non_loopback_connect_guard):
    # The marker lifts the refusal. A closed socket is used, so the real call fails inside the socket library and sends
    # nothing, and the call is not refused by the guard.
    assert non_loopback_connect_guard.allowed
    closed = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    closed.close()
    with pytest.raises(OSError) as caught:
        closed.connect(("192.0.2.1", 443))
    assert not isinstance(caught.value, non_loopback_connect_guard.Blocked)
    assert non_loopback_connect_guard.attempts == []
