"""Shared test setup. Qt runs on the offscreen platform, so no display is needed.

For the whole session the keyring module uses an in-memory backend, and HOME,
USERPROFILE, LOCALAPPDATA and APPDATA point at a temp directory. CODEX_HOME is unset,
so Codex's config falls under that temp home too. So no test can reach the real OS
credential store, the real user home directory, or the real Claude Desktop and Codex
config files, even by accident.
"""

import os

import keyring
import pytest

from _fakes import MemoryKeyring

os.environ["QT_QPA_PLATFORM"] = "offscreen"


@pytest.fixture(scope="session", autouse=True)
def isolated_keyring_and_home(tmp_path_factory):
    """Install the in-memory keyring backend and a temp home for the whole session."""
    home = tmp_path_factory.mktemp("home")
    backend = MemoryKeyring()
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HOME", str(home))
        mp.setenv("USERPROFILE", str(home))
        mp.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
        mp.setenv("APPDATA", str(home / "AppData" / "Roaming"))
        mp.delenv("CODEX_HOME", raising=False)
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
def drain_pool_jobs():
    """Let every thread-pool job a test started finish before the next test starts.

    A window that is shown starts its Home checks on the pool. A check still running when a test ends would emit its
    result during a later test, and that test's recording of the shared delivery object would count it. Waiting here
    keeps each test's deliveries to its own.
    """
    yield
    from PySide6.QtCore import QThreadPool

    QThreadPool.globalInstance().waitForDone(5000)


@pytest.fixture(autouse=True)
def no_hosted_network(monkeypatch):
    """The connection bar's check and the Settings sign-in reach the hosted host through hosted_session.live_transport.
    No test may reach it, so that transport is replaced here by one that fails as an unreachable host would. A test
    that drives the hosted flow passes its own fake transport instead."""
    from meridian_desktop import hosted_session

    def offline(request, timeout):
        raise OSError("network is off in tests")

    monkeypatch.setattr(hosted_session, "live_transport", offline)


@pytest.fixture(autouse=True)
def no_real_user_environment(monkeypatch):
    """The Windows user environment is HKCU\\Environment on the machine running the tests. No test may write it.

    The real writer refuses, and pytest.fail is a BaseException, so the refusal cannot be swallowed by the code under test.
    A test that means to set a user variable injects a fake registry instead.
    """
    from meridian_desktop import user_environment

    def refuse(self, *args, **kwargs):
        pytest.fail("a test tried to write the real Windows user environment; inject a fake registry", pytrace=False)

    monkeypatch.setattr(user_environment.WindowsRegistry, "write_user_value", refuse)
    monkeypatch.setattr(user_environment.WindowsRegistry, "announce_change", refuse)


@pytest.fixture
def settings_path(tmp_path):
    """A settings file in pytest's temp dir. Tests never touch the real one."""
    return tmp_path / "settings" / "settings.json"
