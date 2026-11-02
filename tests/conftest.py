"""Shared test setup. Qt runs on the offscreen platform, so no display is needed.

For the whole session the keyring module uses an in-memory backend, and HOME,
USERPROFILE and LOCALAPPDATA point at a temp directory. So no test can reach the
real OS credential store or the real user home directory, even by accident.

For the whole session a connect or bind to a real service port fails at once (see
SERVICE_PORTS), so no test can reach the core server, the LaTeX engine, a tunnel
slot or Zotero running on this computer, even by accident. A test marked
allow_service_ports opts out. Ports the system picks (port 0) are never touched.

For the whole test the hosted client's send step refuses the real transport for any host other than loopback (see
hosted_transport_guard), however that transport was reached, so no test can send a request, or the saved token, to the
hosted host, even by accident. A test that needs a hosted answer injects its own fake transport.

For the whole session a connect from a test to any host other than loopback (127.0.0.1, ::1 and localhost) is refused
before any bytes are sent (see non_loopback_connect_guard). A name is checked before any lookup, so a refused name is
never resolved, and an address is checked as the socket is given it. Each refused attempt is recorded as host and port
only, and the test fails at teardown. A test that really needs a non-loopback address is marked
allow_non_loopback_connect, and says why.
"""

import dataclasses
import errno
import os
import socket
import urllib.parse
from collections.abc import Callable

import keyring
import pytest

from _fakes import MemoryKeyring
from meridian_desktop.local_client import LOOPBACK_HOSTS

os.environ["QT_QPA_PLATFORM"] = "offscreen"

# The fixed ports the real services on this computer listen on: the core server, the LaTeX engine, the fourteen
# tunnel slots, Zotero's local API and the Zotero bridge API.
SERVICE_PORTS = frozenset({7878, 8471, 23119, 23125, *range(8808, 8822)})
ALLOW_SERVICE_PORTS = "allow_service_ports"
# A test marked with this may run the real hosted transport, against a loopback host only (its own stub).
ALLOW_LOOPBACK_HOSTED = "allow_loopback_hosted_transport"
# A test marked with this may connect to a host other than loopback. Only a test that truly needs a real non-loopback
# address uses it, and it says why (see non_loopback_connect_guard). No test of the suite needs one today.
ALLOW_NON_LOOPBACK_CONNECT = "allow_non_loopback_connect"

# The socket library's own methods, taken before the guard replaces them.
_REAL_CONNECT = socket.socket.connect
_REAL_CONNECT_EX = socket.socket.connect_ex
_REAL_BIND = socket.socket.bind


class ServicePortBlocked(OSError):
    """A test reached for a real service port, and the guard failed the call before the socket library saw it."""


class ServicePortRefused(ServicePortBlocked, ConnectionRefusedError):
    """A connect to a real service port, refused as if nothing listened there, so the caller's "not running" path runs."""


class ServicePortInUse(ServicePortBlocked):
    """A bind to a real service port, failed as if another process held the port."""


@dataclasses.dataclass
class ServicePortGuard:
    """What the guard blocks, whether the running test opted out, and each call it failed, as (call, address)."""

    Blocked = ServicePortBlocked
    Refused = ServicePortRefused
    InUse = ServicePortInUse

    ports: frozenset[int] = SERVICE_PORTS
    allowed: bool = False
    blocked: list[tuple[str, tuple]] = dataclasses.field(default_factory=list)

    def blocks(self, call: str, sock: socket.socket, address) -> bool:
        """True, and the call recorded, when ``address`` is a service port on an internet socket and no opt-out holds."""
        if self.allowed or sock.family not in (socket.AF_INET, socket.AF_INET6):
            return False
        if not isinstance(address, tuple) or len(address) < 2 or address[1] not in self.ports:
            return False
        self.blocked.append((call, tuple(address)))
        return True


_GUARD = ServicePortGuard()


def _guarded_connect(self, address):
    if _GUARD.blocks("connect", self, address):
        raise ServicePortRefused(errno.ECONNREFUSED, f"test guard: {address!r} is a real service port")
    return _REAL_CONNECT(self, address)


def _guarded_connect_ex(self, address):
    if _GUARD.blocks("connect_ex", self, address):
        return errno.ECONNREFUSED
    return _REAL_CONNECT_EX(self, address)


def _guarded_bind(self, address):
    if _GUARD.blocks("bind", self, address):
        raise ServicePortInUse(errno.EADDRINUSE, f"test guard: {address!r} is a real service port")
    return _REAL_BIND(self, address)


class NonLoopbackConnectBlocked(OSError):
    """A test tried to connect to a host other than loopback. The connect was refused before any bytes were sent."""


@dataclasses.dataclass
class NonLoopbackConnectGuard:
    """Refuses a test's connect to a host other than loopback, and records each refused attempt as (host, port).

    Only the host and the port are kept: never a path, a query or a header. ``allowed`` is set for a test marked
    ALLOW_NON_LOOPBACK_CONNECT. Loopback (127.0.0.1, ::1 and localhost, as LOOPBACK_HOSTS names them) is never refused,
    and an address that is not an internet tuple (a Unix socket path) is never refused either.
    """

    Blocked = NonLoopbackConnectBlocked

    allowed: bool = False
    attempts: list[tuple[str, int]] = dataclasses.field(default_factory=list)

    def target(self, address) -> tuple[str, int] | None:
        """The (host, port) of ``address`` when a connect to it must be refused, otherwise None."""
        if self.allowed or not isinstance(address, tuple) or len(address) < 2:
            return None
        host, port = address[0], address[1]
        if isinstance(host, bytes):
            host = host.decode("ascii", "replace")
        if not isinstance(host, str) or host in LOOPBACK_HOSTS:
            return None
        return host, port


_NON_LOOPBACK = NonLoopbackConnectGuard()


def _non_loopback_refusal(address) -> tuple[str, int] | None:
    """Record a refused connect to ``address`` and return its (host, port). None when the connect may go on."""
    target = _NON_LOOPBACK.target(address)
    if target is not None:
        _NON_LOOPBACK.attempts.append(target)
    return target


def _describe(target: tuple[str, int]) -> str:
    """The host and port as text, with an IPv6 host in brackets. Nothing else of the connect is in it."""
    host, port = target
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _refusal_text(target: tuple[str, int]) -> str:
    return f"test guard: a connect to {_describe(target)} is refused; only loopback is allowed in a test"


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        f"{ALLOW_SERVICE_PORTS}: let this test connect to or bind the real service ports (core, LaTeX, slots, Zotero).",
    )
    config.addinivalue_line(
        "markers",
        f"{ALLOW_LOOPBACK_HOSTED}: let this test run the real hosted transport against a loopback stub only.",
    )
    config.addinivalue_line(
        "markers",
        f"{ALLOW_NON_LOOPBACK_CONNECT}: let this test connect to a host other than loopback. Only where a real "
        "non-loopback address is required, and the test says why.",
    )


@pytest.fixture(scope="session", autouse=True)
def service_port_patches():
    """Route every socket's connect, connect_ex and bind through the guard for the whole session, on every thread."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(socket.socket, "connect", _guarded_connect)
        mp.setattr(socket.socket, "connect_ex", _guarded_connect_ex)
        mp.setattr(socket.socket, "bind", _guarded_bind)
        yield _GUARD


@pytest.fixture(autouse=True)
def service_port_guard(request, service_port_patches):
    """Close the service ports for this test, or open them when it is marked allow_service_ports. Yields the guard."""
    service_port_patches.allowed = request.node.get_closest_marker(ALLOW_SERVICE_PORTS) is not None
    yield service_port_patches
    service_port_patches.allowed = False


@pytest.fixture(scope="session", autouse=True)
def non_loopback_connect_patches(service_port_patches):
    """Route every connect and create_connection through the non-loopback guard, for the whole session and every thread.

    create_connection checks the name it is given before the lookup, so a refused name is never resolved and the lookup
    is never needed to refuse it. connect and connect_ex check the address the socket is given, which also covers a name
    the socket library resolves itself. The patches sit in front of the service-port guard, which still covers loopback.
    """
    previous_connect = socket.socket.connect
    previous_connect_ex = socket.socket.connect_ex
    previous_create_connection = socket.create_connection

    def connect(self, address):
        target = _non_loopback_refusal(address)
        if target is not None:
            raise NonLoopbackConnectBlocked(errno.ENETUNREACH, _refusal_text(target))
        return previous_connect(self, address)

    def connect_ex(self, address):
        if _non_loopback_refusal(address) is not None:
            return errno.ENETUNREACH
        return previous_connect_ex(self, address)

    def create_connection(address, *args, **kwargs):
        target = _non_loopback_refusal(address)
        if target is not None:
            raise NonLoopbackConnectBlocked(errno.ENETUNREACH, _refusal_text(target))
        return previous_create_connection(address, *args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(socket.socket, "connect", connect)
        mp.setattr(socket.socket, "connect_ex", connect_ex)
        mp.setattr(socket, "create_connection", create_connection)
        yield _NON_LOOPBACK


@pytest.fixture(autouse=True)
def non_loopback_connect_guard(request, non_loopback_connect_patches):
    """Refuse this test's connects to any host other than loopback, unless it is marked allow_non_loopback_connect.

    Each refused attempt is recorded as (host, port). A test with a recorded attempt fails at teardown and names each
    one. The record starts empty for every test, so one test's attempt never fails another. Yields the guard, so a test
    can read ``attempts`` directly.
    """
    guard = non_loopback_connect_patches
    guard.allowed = request.node.get_closest_marker(ALLOW_NON_LOOPBACK_CONNECT) is not None
    guard.attempts.clear()
    yield guard
    guard.allowed = False
    attempts = list(guard.attempts)
    guard.attempts.clear()
    if attempts:
        pytest.fail(
            f"{len(attempts)} connect(s) from this test went to a host other than loopback, and each was refused "
            "before any bytes were sent: " + "; ".join(_describe(target) for target in attempts) + ". Inject a fake "
            "for this test instead, or mark it allow_non_loopback_connect if it really needs a non-loopback address.",
            pytrace=False,
        )


@pytest.fixture(scope="session", autouse=True)
def isolated_keyring_and_home(tmp_path_factory):
    """Install the in-memory keyring backend and a temp home for the whole session."""
    home = tmp_path_factory.mktemp("home")
    backend = MemoryKeyring()
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HOME", str(home))
        mp.setenv("USERPROFILE", str(home))
        mp.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
        keyring.set_keyring(backend)
        yield backend


@pytest.fixture(autouse=True)
def no_loopback_probe_waits(monkeypatch):
    """Home's services card probes the LaTeX engine and the slot ports on 127.0.0.1. On a loopback address those
    probes wait out their timeouts, which takes seconds, so every test gets fast stand-ins. A test that wants the
    real probe replaces these again. No test opens a socket for these checks."""
    from meridian_desktop.latex_client import ENGINE_NOT_RUNNING, EngineStatus
    from meridian_desktop.slot_ports import SlotPortResult
    from meridian_desktop.ui import home_tab

    monkeypatch.setattr(
        home_tab,
        "probe_engine_status",
        lambda url, timeout=None: EngineStatus(state=ENGINE_NOT_RUNNING, message="Not running (test stand-in)."),
    )
    monkeypatch.setattr(
        home_tab,
        "probe_slot_ports",
        lambda: [SlotPortResult("test slot", 1, False)],
    )


@pytest.fixture(autouse=True)
def no_overleaf_engine(monkeypatch):
    """Home's Overleaf (LaTeX) card would ask the LaTeX engine on 127.0.0.1:8471, which is running on this computer.
    No test may reach it. The card's default calls are replaced here by stand-ins that open no socket and start nothing:
    the port reads as closed, the starter folder is unknown, and any session, login or start call fails the test. A test
    that wants the card's behaviour passes its own OverleafDeps (HomeDeps.overleaf).
    """
    from meridian_desktop.ui import home_tab

    def refuse(*args, **kwargs):
        raise AssertionError("the LaTeX engine was called from a test; pass a fake OverleafDeps instead")

    def stand_in_deps():
        return home_tab.OverleafDeps(
            listening=lambda url: False,
            folder=lambda settings: None,
            start=refuse,
            session=refuse,
            login=refuse,
            # Every question the card asks is answered No, the default, so nothing is saved unless a test answers Yes itself.
            confirm=lambda parent, title, text: False,
        )

    monkeypatch.setattr(home_tab, "_default_overleaf_deps", stand_in_deps)


@pytest.fixture(autouse=True)
def no_modal_dialog(monkeypatch):
    """A modal dialog never returns offscreen, so a test that opens one by accident would hang the whole run.

    Any exec() of a message box or dialog fails that test at once. A test that means to answer a question injects the
    answer instead, as the confirmation fakes do.
    """
    from PySide6.QtWidgets import QDialog, QMessageBox

    def refuse(self, *args, **kwargs):
        raise AssertionError("a modal dialog opened in a test; inject the answer instead")

    for cls in (QDialog, QMessageBox):
        for name in ("exec", "exec_"):
            if hasattr(cls, name):
                monkeypatch.setattr(cls, name, refuse)


class HostedCallBlocked(AssertionError):
    """A test reached the real hosted transport. The call was refused before any request was sent."""


@dataclasses.dataclass
class HostedTransportGuard:
    """Stands in for the hosted client's real transport (urllib). Each refused call is recorded, and then refused.

    ``calls`` holds one "METHOD scheme://host/path" entry per refused call. The query, the headers and the token are
    never kept, so no failure message can carry the token.

    ``real`` is the real transport. A test marked ALLOW_LOOPBACK_HOSTED gets ``real`` for a loopback host only, which
    is its own stub. Every other host is refused, the hosted host included.
    """

    real: Callable
    loopback_allowed: bool = False
    calls: list[str] = dataclasses.field(default_factory=list)

    def check(self, request) -> None:
        """Return when the request may reach the real transport. Otherwise record the call and refuse it."""
        parts = urllib.parse.urlsplit(request.full_url)
        if self.loopback_allowed and parts.hostname in LOOPBACK_HOSTS:
            return
        target = urllib.parse.urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", ""))
        entry = f"{request.get_method()} {target}"
        self.calls.append(entry)
        raise HostedCallBlocked(f"a test reached the hosted host through the real transport: {entry}")

    def __call__(self, request, timeout):
        self.check(request)
        return self.real(request, timeout)


@pytest.fixture(autouse=True)
def hosted_transport_guard(request, monkeypatch):
    """No test sends a request to the hosted host. The real transport is replaced by a guard that records each call and
    refuses it, and the hosted client's send step refuses the real transport too. That step is where every hosted call
    ends, so the real transport is refused however a caller reached it: the module default, an explicit transport
    argument, or the attribute re-patched to ``guard.real``. A test that calls it fails at teardown, and the failure
    names the URL. A test that needs a hosted answer injects a fake transport instead (the transport argument, or
    MainWindow's hosted_transport). A fake transport passes through the send step untouched.

    The guard stays in place until the thread-pool jobs have drained (see drain_pool_jobs), so a late job is refused.
    Yields the guard, so a test can read ``calls`` directly.
    """
    from meridian_desktop import hosted_client

    guard = HostedTransportGuard(
        real=hosted_client._urllib_transport,  # noqa: SLF001 - taken before the guard replaces it
        loopback_allowed=request.node.get_closest_marker(ALLOW_LOOPBACK_HOSTED) is not None,
    )
    real_send = hosted_client._send  # noqa: SLF001 - taken before the guard wraps it

    def guarded_send(transport, send_request, timeout):
        if transport is guard or transport is guard.real:
            guard.check(send_request)
        return real_send(transport, send_request, timeout)

    monkeypatch.setattr(hosted_client, "_urllib_transport", guard)
    monkeypatch.setattr(hosted_client, "_send", guarded_send)
    yield guard
    if guard.calls:
        pytest.fail(
            f"{len(guard.calls)} request(s) reached the hosted host through the real transport and were refused: "
            + "; ".join(guard.calls)
            + ". Inject a fake transport for this test instead.",
            pytrace=False,
        )


@pytest.fixture(autouse=True)
def drain_pool_jobs(service_port_guard, hosted_transport_guard, non_loopback_connect_guard):
    """Let every thread-pool job a test started finish before the next test starts.

    A window that is shown starts its Home checks on the pool. A check still running when a test ends would emit its
    result during a later test, and that test's recording of the shared delivery object would count it. Waiting here
    keeps each test's deliveries to its own. It waits before the port guard's per-test setting is reset, so a job
    sees the same open or closed service ports as the test that started it. It also waits before the hosted guard
    and the non-loopback guard are checked and removed, so a late job cannot reach a real host. If the pool has not
    drained after 5 seconds, the test fails here, while those guards are still in place.
    """
    yield
    from PySide6.QtCore import QThreadPool

    if not QThreadPool.globalInstance().waitForDone(5000):
        pytest.fail(
            "A thread-pool job this test started was still running 5 seconds after the test ended. It would run on "
            "into a later test. Wait for the job inside the test (for example with qtbot.waitUntil), or give it a fake "
            "that returns at once.",
            pytrace=False,
        )


@pytest.fixture
def settings_path(tmp_path):
    """A settings file in pytest's temp dir. Tests never touch the real one."""
    return tmp_path / "settings" / "settings.json"
