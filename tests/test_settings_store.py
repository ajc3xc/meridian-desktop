import ast
import os
from pathlib import Path

import pytest

from meridian_desktop import settings_store
from meridian_desktop.settings_store import (
    DEFAULT_LOCAL_URL,
    Settings,
    SettingsStore,
    default_settings_path,
)

REPO = Path(__file__).resolve().parents[1]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_missing_file_gives_defaults_without_warning(settings_path):
    result = SettingsStore(settings_path).load()
    assert result.settings == Settings()
    assert result.settings.mode == "local"
    assert result.warning is None


def test_round_trip(settings_path):
    saved = Settings(
        mode="hosted",
        hosted_url="https://example.test",
        local_url=DEFAULT_LOCAL_URL,
        engine_url="http://127.0.0.1:9999",
    )
    SettingsStore(settings_path).save(saved)
    result = SettingsStore(settings_path).load()
    assert result.warning is None
    assert result.settings == saved


def test_set_mode_round_trip(settings_path):
    store = SettingsStore(settings_path)
    store.set_mode("hosted")
    assert SettingsStore(settings_path).load().settings.mode == "hosted"
    store.set_mode("local")
    assert SettingsStore(settings_path).load().settings.mode == "local"


def test_corrupt_file_returns_defaults_and_warning(settings_path):
    _write(settings_path, "{not json")
    result = SettingsStore(settings_path).load()
    assert result.settings == Settings()
    assert result.warning is not None
    assert "corrupt" in result.warning


def test_json_that_is_not_an_object_returns_defaults_and_warning(settings_path):
    _write(settings_path, "[1, 2, 3]")
    result = SettingsStore(settings_path).load()
    assert result.settings == Settings()
    assert result.warning is not None
    assert "JSON object" in result.warning


def test_unreadable_file_returns_defaults_and_warning(tmp_path):
    # A directory at the settings path cannot be read as a file.
    path = tmp_path / "settings.json"
    path.mkdir()
    result = SettingsStore(path).load()
    assert result.settings == Settings()
    assert result.warning is not None
    assert "unreadable" in result.warning


def test_invalid_mode_in_file_falls_back_to_local(settings_path):
    _write(settings_path, '{"mode": "cloud", "hosted_url": "https://example.test"}')
    result = SettingsStore(settings_path).load()
    assert result.settings.mode == "local"
    assert result.settings.hosted_url == "https://example.test"
    assert result.warning is not None
    assert "mode" in result.warning


def test_non_string_url_falls_back_to_default(settings_path):
    _write(settings_path, '{"engine_url": 8471}')
    result = SettingsStore(settings_path).load()
    assert result.settings.engine_url == Settings().engine_url
    assert result.warning is not None


def test_invalid_mode_is_rejected_and_nothing_is_written(settings_path):
    store = SettingsStore(settings_path)
    for bad in ("cloud", "", "HOSTED", None, 1):
        with pytest.raises(ValueError):
            store.set_mode(bad)
    assert not settings_path.exists()


def test_atomic_write_leaves_no_temp_file(settings_path):
    store = SettingsStore(settings_path)
    store.set_mode("hosted")
    store.set_mode("local")
    assert sorted(os.listdir(settings_path.parent)) == ["settings.json"]


def test_failed_replace_keeps_old_file_and_removes_temp(settings_path, monkeypatch):
    store = SettingsStore(settings_path)
    store.set_mode("hosted")
    before = settings_path.read_text(encoding="utf-8")

    def failing_replace(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(settings_store.os, "replace", failing_replace)
    with pytest.raises(OSError):
        store.set_mode("local")
    assert settings_path.read_text(encoding="utf-8") == before
    assert sorted(os.listdir(settings_path.parent)) == ["settings.json"]


def test_default_path_uses_localappdata(tmp_path):
    env = {"LOCALAPPDATA": str(tmp_path / "local")}
    assert default_settings_path(env=env) == tmp_path / "local" / "meridian-desktop" / "settings.json"


def test_default_path_falls_back_to_home_when_localappdata_unset(tmp_path):
    assert default_settings_path(env={}, home=tmp_path) == tmp_path / ".meridian-desktop" / "settings.json"


def test_settings_store_imports_no_qt():
    tree = ast.parse((REPO / "meridian_desktop" / "settings_store.py").read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    qt_roots = {"PySide6", "PySide2", "PyQt5", "PyQt6", "PyQt4", "shiboken6", "shiboken2"}
    assert [name for name in imported if name.split(".")[0] in qt_roots] == []
