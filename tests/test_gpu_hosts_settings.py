"""Tests for the GPUs tab's saved SSH hosts: the settings key (meridian_desktop/settings_store.py) and the Settings field
(meridian_desktop/ui/settings_tab.py). Offscreen Qt, a temporary settings file, an in-memory keychain, and a core
search that is a fake. Nothing starts a process, opens a socket or reads the real keychain.
"""

import json

import pytest
from PySide6.QtWidgets import QLineEdit, QPushButton, QLabel

from _fakes import MemoryKeyring
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.ui.runner_actions import RunnerActions
from meridian_desktop.ui.settings_tab import (
    GPU_HOSTS_CLEARED_TEXT,
    GPU_HOSTS_SAVED_TEXT,
    SettingsTab,
)


def _saved(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write_raw(path, data):
    """Write a settings file by hand, so a test can give the loader a value the writer would never produce."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _no_list(url, token):
    raise AssertionError("the hosted list must not be called in this test")


def _store_with(settings_path, **changes):
    store = SettingsStore(settings_path)
    store.save(Settings(**changes))
    return store


# ----------------------------------------------------------------- the settings key


def test_a_file_with_no_saved_hosts_has_no_gpu_hosts_key(settings_path):
    store = SettingsStore(settings_path)
    store.save(Settings())
    assert "gpu_hosts" not in _saved(settings_path)
    assert store.load().settings.gpu_hosts == ()


def test_saved_hosts_are_written_as_a_list_and_read_back_in_order(settings_path):
    store = SettingsStore(settings_path)
    updated = store.set_gpu_hosts(" gpu-box, lab_a100 ,gpu-box")
    assert updated.gpu_hosts == ("gpu-box", "lab_a100")
    assert _saved(settings_path)["gpu_hosts"] == ["gpu-box", "lab_a100"]
    assert store.load().settings.gpu_hosts == ("gpu-box", "lab_a100")


def test_a_refused_line_writes_nothing_and_keeps_the_file_as_it_was(settings_path):
    store = SettingsStore(settings_path)
    store.set_gpu_hosts("gpu-box")
    before = settings_path.read_bytes()
    with pytest.raises(ValueError) as caught:
        store.set_gpu_hosts("gpu-box, -oProxyCommand=evil")
    assert "not a valid SSH host name" in str(caught.value)
    assert settings_path.read_bytes() == before


def test_an_empty_line_clears_the_saved_hosts_and_the_key(settings_path):
    store = SettingsStore(settings_path)
    store.set_gpu_hosts("gpu-box")
    cleared = store.set_gpu_hosts("   ")
    assert cleared.gpu_hosts == ()
    assert "gpu_hosts" not in _saved(settings_path)


def test_saving_hosts_keeps_every_other_field(settings_path):
    store = _store_with(settings_path, mode="local", local_url="http://127.0.0.1:9999", check_updates=False)
    store.set_gpu_hosts("gpu-box")
    loaded = store.load().settings
    assert loaded.mode == "local"
    assert loaded.local_url == "http://127.0.0.1:9999"
    assert loaded.check_updates is False


def test_a_file_with_a_bad_host_list_uses_no_hosts_and_says_why(settings_path):
    _write_raw(settings_path, {"gpu_hosts": ["good", "-oX=y"]})
    result = SettingsStore(settings_path).load()
    assert result.settings.gpu_hosts == ()
    assert result.warning is not None
    assert "gpu_hosts" in result.warning


def test_a_file_with_more_than_the_host_limit_uses_no_hosts(settings_path):
    names = [f"host{index}" for index in range(9)]
    _write_raw(settings_path, {"gpu_hosts": names})
    result = SettingsStore(settings_path).load()
    assert result.settings.gpu_hosts == ()
    assert "gpu_hosts" in (result.warning or "")


def test_a_file_whose_host_list_is_not_a_list_uses_no_hosts(settings_path):
    _write_raw(settings_path, {"gpu_hosts": "gpu-box"})
    result = SettingsStore(settings_path).load()
    assert result.settings.gpu_hosts == ()
    assert "gpu_hosts" in (result.warning or "")


def test_a_valid_host_list_in_a_file_loads_without_warning(settings_path):
    _write_raw(settings_path, {"gpu_hosts": ["gpu-box", "lab"]})
    result = SettingsStore(settings_path).load()
    assert result.settings.gpu_hosts == ("gpu-box", "lab")
    assert result.warning is None


# ----------------------------------------------------------------- the Settings field


def _tab(qtbot, settings_path):
    actions = RunnerActions(SettingsStore(settings_path), locate=lambda store: None, choose_folder=lambda parent, start: "")
    tab = SettingsTab(
        SettingsStore(settings_path),
        secrets=KeyringSecretStore(backend=MemoryKeyring()),
        list_hosted=_no_list,
        actions=actions,
    )
    qtbot.addWidget(tab)
    return tab


def test_the_field_shows_the_saved_hosts(qtbot, settings_path):
    _store_with(settings_path, gpu_hosts=("gpu-box", "lab"))
    tab = _tab(qtbot, settings_path)
    field = tab.findChild(QLineEdit, "settings-gpu-hosts")
    assert field.text() == "gpu-box, lab"


def test_saving_the_field_writes_the_hosts_and_says_so(qtbot, settings_path):
    tab = _tab(qtbot, settings_path)
    tab.findChild(QLineEdit, "settings-gpu-hosts").setText("gpu-box, lab")
    tab.findChild(QPushButton, "settings-save-gpu-hosts").click()
    message = tab.findChild(QLabel, "settings-gpu-hosts-message")
    assert message.text() == GPU_HOSTS_SAVED_TEXT
    assert message.isHidden() is False
    assert _saved(settings_path)["gpu_hosts"] == ["gpu-box", "lab"]


def test_a_refused_name_is_shown_and_nothing_is_written(qtbot, settings_path):
    tab = _tab(qtbot, settings_path)
    tab.findChild(QLineEdit, "settings-gpu-hosts").setText("-oX=y")
    tab.findChild(QPushButton, "settings-save-gpu-hosts").click()
    message = tab.findChild(QLabel, "settings-gpu-hosts-message")
    assert message.text().startswith("Not saved. ")
    assert "gpu_hosts" not in (_saved(settings_path) if settings_path.exists() else {})


def test_nine_names_are_refused_with_a_clear_message_and_nothing_is_truncated(qtbot, settings_path):
    _store_with(settings_path, gpu_hosts=("gpu-box",))
    tab = _tab(qtbot, settings_path)
    nine = ", ".join(f"host{index}" for index in range(9))
    tab.findChild(QLineEdit, "settings-gpu-hosts").setText(nine)
    tab.findChild(QPushButton, "settings-save-gpu-hosts").click()
    message = tab.findChild(QLabel, "settings-gpu-hosts-message").text()
    assert message == "Not saved. Save at most 8 SSH hosts. Remove 1 to save."
    assert "are kept" not in message
    # The saved file is unchanged: no hosts were silently cut to eight.
    assert _saved(settings_path)["gpu_hosts"] == ["gpu-box"]


def test_clearing_the_field_says_no_hosts_are_read(qtbot, settings_path):
    _store_with(settings_path, gpu_hosts=("gpu-box",))
    tab = _tab(qtbot, settings_path)
    tab.findChild(QLineEdit, "settings-gpu-hosts").setText("")
    tab.findChild(QPushButton, "settings-save-gpu-hosts").click()
    assert tab.findChild(QLabel, "settings-gpu-hosts-message").text() == GPU_HOSTS_CLEARED_TEXT
    assert "gpu_hosts" not in _saved(settings_path)
