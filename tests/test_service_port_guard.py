"""The guard in conftest.py that keeps every test off the real service ports. No test here reaches a real port.

The core server (7878), the LaTeX engine (8471), the tunnel slots (8808 to 8821) and Zotero's local API (23119) may be
running on the computer that runs the suite. Inside a test, a connect or bind to one of those ports fails at once, before
anything is sent, unless the test is marked allow_service_ports. A marked test here only calls the real socket method on
a closed socket, which fails inside the socket library without sending anything. Ports the system picks (port 0) work as
before.
"""

import errno
import socket
import threading

import pytest

GUARDED_PORTS = (7878, 8471, 23119, *range(8808, 8822))


def test_a_connect_to_the_zotero_port_is_refused_inside_a_test(service_port_guard):
    before = len(service_port_guard.blocked)
    with pytest.raises(service_port_guard.Refused) as caught:
        socket.create_connection(("127.0.0.1", 23119), timeout=1)
    # The caller sees an ordinary refusal, as if nothing listened there, so its "not running" path runs.
    assert isinstance(caught.value, ConnectionRefusedError)
    assert caught.value.errno == errno.ECONNREFUSED
    assert service_port_guard.blocked[before:] == [("connect", ("127.0.0.1", 23119))]


def test_the_guard_covers_exactly_the_core_latex_slot_and_zotero_ports(service_port_guard):
    assert service_port_guard.ports == frozenset(GUARDED_PORTS)


@pytest.mark.parametrize("port", GUARDED_PORTS)
def test_a_connect_to_any_guarded_port_is_refused(service_port_guard, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        with pytest.raises(service_port_guard.Refused):
            sock.connect(("127.0.0.1", port))


@pytest.mark.skipif(not socket.has_ipv6, reason="no IPv6 on this computer")
def test_a_connect_over_ipv6_loopback_is_refused(service_port_guard):
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
        with pytest.raises(service_port_guard.Refused):
            sock.connect(("::1", 7878))


def test_connect_ex_reports_a_refusal_and_does_not_connect(service_port_guard):
    before = len(service_port_guard.blocked)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        assert sock.connect_ex(("127.0.0.1", 8471)) == errno.ECONNREFUSED
    assert service_port_guard.blocked[before:] == [("connect_ex", ("127.0.0.1", 8471))]


@pytest.mark.parametrize("host", ["127.0.0.1", ""])
def test_a_bind_to_a_guarded_port_fails(service_port_guard, host):
    before = len(service_port_guard.blocked)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        with pytest.raises(service_port_guard.InUse) as caught:
            sock.bind((host, 8812))
        assert caught.value.errno == errno.EADDRINUSE
    assert service_port_guard.blocked[before:] == [("bind", (host, 8812))]


def test_a_connect_from_another_thread_is_refused_too(service_port_guard):
    # The window's Home checks run on the thread pool, so the guard has to hold off the main thread as well.
    errors = []

    def reach():
        try:
            socket.create_connection(("127.0.0.1", 8808), timeout=1).close()
        except OSError as exc:
            errors.append(exc)

    worker = threading.Thread(target=reach)
    worker.start()
    worker.join(5)
    assert not worker.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], service_port_guard.Refused)


def test_ephemeral_ports_still_work(service_port_guard):
    before = len(service_port_guard.blocked)
    with socket.create_server(("127.0.0.1", 0)) as server:
        port = server.getsockname()[1]
        assert port not in service_port_guard.ports
        with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
            accepted, _ = server.accept()
            with accepted:
                client.sendall(b"ping")
                assert accepted.recv(4) == b"ping"
    assert service_port_guard.blocked[before:] == []


@pytest.mark.allow_service_ports
def test_a_test_marked_allow_service_ports_reaches_the_real_socket_calls(service_port_guard):
    before = len(service_port_guard.blocked)
    # A closed socket: the real call fails inside the socket library, so nothing is sent to the port.
    closed = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    closed.close()
    with pytest.raises(OSError) as connect_error:
        closed.connect(("127.0.0.1", 23119))
    with pytest.raises(OSError) as bind_error:
        closed.bind(("127.0.0.1", 23119))
    assert closed.connect_ex(("127.0.0.1", 23119)) != errno.ECONNREFUSED
    assert not isinstance(connect_error.value, service_port_guard.Blocked)
    assert not isinstance(bind_error.value, service_port_guard.Blocked)
    assert service_port_guard.blocked[before:] == []


def test_an_unmarked_test_is_guarded_whatever_ran_before_it(service_port_guard):
    assert not service_port_guard.allowed
    closed = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    closed.close()
    with pytest.raises(service_port_guard.Refused):
        closed.connect(("127.0.0.1", 23119))
