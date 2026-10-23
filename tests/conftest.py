"""Shared test setup. Qt runs on the offscreen platform, so no display is needed.

For the whole session the keyring module uses an in-memory backend, and HOME,
USERPROFILE and LOCALAPPDATA point at a temp directory. So no test can reach the
real OS credential store or the real user home directory, even by accident.
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


@pytest.fixture
def settings_path(tmp_path):
    """A settings file in pytest's temp dir. Tests never touch the real one."""
    return tmp_path / "settings" / "settings.json"
