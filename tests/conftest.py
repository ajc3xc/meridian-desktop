"""Shared test setup. Qt runs on the offscreen platform, so no display is needed.

For the whole session the keyring module uses an in-memory backend, and HOME,
USERPROFILE and LOCALAPPDATA point at a temp directory. So no test can reach the
real OS credential store or the real user home directory, even by accident.

For the whole session a connect or bind to a real service port fails at once (see
SERVICE_PORTS), so no test can reach the core server, the LaTeX engine, a tunnel
slot or Zotero running on this computer, even by accident. A test marked
allow_service_ports opts out. Ports the system picks (port 0) are never touched.
"""

import dataclasses
import errno
import os
import socket

import keyring
import pytest

from _fakes import MemoryKeyring

os.environ["QT_QPA_PLATFORM"] = "offscreen"

# The fixed ports the real services on this computer listen on: the core server, the LaTeX engine, the fourteen
# tunnel slots, Zotero's local API and the Zotero bridge API.
SERVICE_PORTS = frozenset({7878, 8471, 23119, 23125, *range(8808, 8822)})
ALLOW_SERVICE_PORTS = "allow_service_ports"

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


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        f"{ALLOW_SERVICE_PORTS}: let this test connect to or bind the real service ports (core, LaTeX, slots, Zotero).",
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


@pytest.fixture(autouse=True)
def drain_pool_jobs(service_port_guard):
    """Let every thread-pool job a test started finish before the next test starts.

    A window that is shown starts its Home checks on the pool. A check still running when a test ends would emit its
    result during a later test, and that test's recording of the shared delivery object would count it. Waiting here
    keeps each test's deliveries to its own. It waits before the port guard's per-test setting is reset, so a job
    sees the same open or closed service ports as the test that started it.
    """
    yield
    from PySide6.QtCore import QThreadPool

    QThreadPool.globalInstance().waitForDone(5000)


@pytest.fixture
def settings_path(tmp_path):
    """A settings file in pytest's temp dir. Tests never touch the real one."""
    return tmp_path / "settings" / "settings.json"
