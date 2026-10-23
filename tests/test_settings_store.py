import ast
import json
import os
from pathlib import Path

import pytest

from meridian_desktop import settings_store
from meridian_desktop.settings_store import (
    DEFAULT_LOCAL_URL,
    Settings,
    SettingsStore,
    core_path_problem,
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


SAVED_KEYS = {"mode", "hosted_url", "local_url", "engine_url", "db_path"}


def test_db_path_round_trip_and_blank_is_none(settings_path):
    store = SettingsStore(settings_path)
    assert store.load().settings.db_path is None

    db = str(settings_path.parent / "meridian.db")
    store.save(Settings(db_path=db))
    result = SettingsStore(settings_path).load()
    assert result.warning is None
    assert result.settings.db_path == db

    # An unset db_path is saved as null and reads back as None with no warning.
    store.save(Settings())
    result = SettingsStore(settings_path).load()
    assert result.settings.db_path is None
    assert result.warning is None

    # A blank or non-string value loads as None with a warning, and never raises.
    for bad in ("", "   ", 42, True, ["a"], {"path": "x"}):
        _write(settings_path, json.dumps({"db_path": bad}))
        result = SettingsStore(settings_path).load()
        assert result.settings.db_path is None, bad
        assert result.warning is not None and "db_path" in result.warning, bad


def test_file_written_before_db_path_loads_without_warning(settings_path):
    _write(
        settings_path,
        json.dumps(
            {
                "mode": "hosted",
                "hosted_url": "https://example.test",
                "local_url": DEFAULT_LOCAL_URL,
                "engine_url": "http://127.0.0.1:9999",
            }
        ),
    )
    result = SettingsStore(settings_path).load()
    assert result.warning is None
    assert result.settings.mode == "hosted"
    assert result.settings.db_path is None


def test_saved_file_holds_only_the_five_keys(settings_path):
    SettingsStore(settings_path).save(Settings(db_path=str(settings_path.parent / "meridian.db")))
    saved = json.loads(settings_path.read_text(encoding="utf-8"))
    assert set(saved) == SAVED_KEYS


def test_token_like_keys_are_neither_kept_nor_written(settings_path):
    _write(
        settings_path,
        json.dumps({"mode": "hosted", "hosted_token": "fake-token-value-123", "api_key": "fake-key-value-456"}),
    )
    SettingsStore(settings_path).set_mode("local")
    text = settings_path.read_text(encoding="utf-8")
    assert "fake-token-value-123" not in text
    assert "fake-key-value-456" not in text
    assert set(json.loads(text)) == SAVED_KEYS


# ----------------------------------------------------------- core checkout and core Python


def _core_checkout(tmp_path: Path) -> Path:
    """A folder with the meridian package in it, as a Meridian core checkout has."""
    checkout = tmp_path / "core"
    (checkout / "meridian").mkdir(parents=True)
    (checkout / "meridian" / "__init__.py").write_text("", encoding="utf-8")
    return checkout


def test_core_settings_default_to_not_set_without_a_warning(settings_path):
    result = SettingsStore(settings_path).load()
    assert result.settings.core_checkout == ""
    assert result.settings.core_python == ""
    assert result.warning is None


def test_core_settings_round_trip(settings_path, tmp_path):
    saved = Settings(core_checkout=str(tmp_path / "core"), core_python=str(tmp_path / "python.exe"))
    SettingsStore(settings_path).save(saved)
    result = SettingsStore(settings_path).load()
    assert result.warning is None
    assert result.settings == saved


def test_unset_core_settings_are_not_written(settings_path):
    SettingsStore(settings_path).save(Settings())
    assert set(json.loads(settings_path.read_text(encoding="utf-8"))) == SAVED_KEYS


def test_set_core_settings_are_written_and_only_the_set_one_when_one_is_set(settings_path, tmp_path):
    SettingsStore(settings_path).save(Settings(core_python=str(tmp_path / "python.exe")))
    saved = json.loads(settings_path.read_text(encoding="utf-8"))
    assert saved["core_python"] == str(tmp_path / "python.exe")
    assert "core_checkout" not in saved


def test_non_text_core_values_warn_and_are_not_set(settings_path):
    _write(settings_path, json.dumps({"core_checkout": 5, "core_python": ["a"]}))
    result = SettingsStore(settings_path).load()
    assert result.settings.core_checkout == ""
    assert result.settings.core_python == ""
    assert result.warning is not None
    assert "core_checkout" in result.warning and "core_python" in result.warning


def test_blank_core_value_loads_as_not_set_without_a_warning(settings_path):
    _write(settings_path, json.dumps({"core_checkout": "   "}))
    result = SettingsStore(settings_path).load()
    assert result.settings.core_checkout == ""
    assert result.warning is None


def test_core_path_problem_refuses_a_checkout_folder_that_does_not_exist(tmp_path):
    message = core_path_problem("core_checkout", str(tmp_path / "gone"))
    assert message is not None
    assert message.startswith("Not saved. ")
    assert "does not exist" in message


def test_core_path_problem_refuses_a_folder_without_the_meridian_package(tmp_path):
    folder = tmp_path / "empty"
    folder.mkdir()
    message = core_path_problem("core_checkout", str(folder))
    assert message is not None and "not a Meridian core checkout" in message


def test_core_path_problem_accepts_a_real_checkout_and_an_empty_value(tmp_path):
    assert core_path_problem("core_checkout", str(_core_checkout(tmp_path))) is None
    assert core_path_problem("core_checkout", "") is None
    assert core_path_problem("core_python", "") is None


def test_core_path_problem_refuses_a_python_that_does_not_exist_or_is_a_folder(tmp_path):
    missing = core_path_problem("core_python", str(tmp_path / "nope" / "python.exe"))
    assert missing is not None and "does not exist" in missing
    folder = core_path_problem("core_python", str(tmp_path))
    assert folder is not None and "does not exist" in folder


def test_core_path_problem_rejects_an_unknown_key(tmp_path):
    with pytest.raises(ValueError):
        core_path_problem("core_token", str(tmp_path))


def test_set_core_path_saves_the_value_and_keeps_every_other_field(settings_path, tmp_path):
    python = tmp_path / "python.exe"
    python.write_text("", encoding="utf-8")
    SettingsStore(settings_path).save(Settings(mode="hosted", local_url="http://127.0.0.1:9999"))

    updated = SettingsStore(settings_path).set_core_path("core_python", str(python))

    loaded = SettingsStore(settings_path).load().settings
    assert loaded.core_python == str(python)
    assert loaded.mode == "hosted" and loaded.local_url == "http://127.0.0.1:9999"
    assert updated == loaded


def test_a_refused_core_path_raises_plain_words_and_writes_nothing(settings_path, tmp_path):
    store = SettingsStore(settings_path)
    with pytest.raises(ValueError) as refused:
        store.set_core_path("core_checkout", str(tmp_path / "gone"))
    assert str(refused.value).startswith("Not saved. ")
    assert not settings_path.exists()


def test_clearing_a_core_path_removes_its_key(settings_path, tmp_path):
    store = SettingsStore(settings_path)
    store.set_core_path("core_checkout", str(_core_checkout(tmp_path)))
    store.set_core_path("core_checkout", "")
    assert store.load().settings.core_checkout == ""
    assert "core_checkout" not in json.loads(settings_path.read_text(encoding="utf-8"))


# ----------------------------------------------------------- update check


def test_check_updates_defaults_to_on_without_a_warning(settings_path):
    assert Settings().check_updates is True
    result = SettingsStore(settings_path).load()
    assert result.settings.check_updates is True
    assert result.warning is None


def test_check_updates_round_trips_off_and_back_on(settings_path):
    store = SettingsStore(settings_path)
    store.save(Settings(check_updates=False))
    assert json.loads(settings_path.read_text(encoding="utf-8"))["check_updates"] is False
    result = SettingsStore(settings_path).load()
    assert result.warning is None
    assert result.settings == Settings(check_updates=False)
    # A later mode change keeps the setting as it was.
    store.set_mode("hosted")
    assert SettingsStore(settings_path).load().settings.check_updates is False
    # On is the default, so turning it back on removes the key again.
    store.save(Settings(check_updates=True))
    assert "check_updates" not in json.loads(settings_path.read_text(encoding="utf-8"))
    assert SettingsStore(settings_path).load().settings == Settings(check_updates=True)


def test_check_updates_that_is_not_true_or_false_warns_and_stays_on(settings_path):
    _write(settings_path, json.dumps({"check_updates": "no"}))
    result = SettingsStore(settings_path).load()
    assert result.settings.check_updates is True
    assert result.warning is not None and "check_updates" in result.warning


# ----------------------------------------------------------- the three runner control switches (contract rule 10, amendment A and B)

CONTROL_KEYS = ("tunnel_control_enabled", "zotero_autostart", "local_server_autostart")


def test_runner_control_switches_default_to_off_and_are_not_written(settings_path):
    store = SettingsStore(settings_path)
    loaded = store.load()

    assert loaded.warning is None
    for key in CONTROL_KEYS:
        assert getattr(loaded.settings, key) is False, key
    store.save(loaded.settings)
    saved = json.loads(settings_path.read_text(encoding="utf-8"))
    assert not any(key in saved for key in CONTROL_KEYS)


@pytest.mark.parametrize("key", CONTROL_KEYS)
def test_each_runner_switch_round_trips_when_on_and_leaves_the_others_off(settings_path, key):
    store = SettingsStore(settings_path)
    store.save(Settings(**{key: True}))

    loaded = store.load()
    assert loaded.warning is None
    assert getattr(loaded.settings, key) is True
    for other in CONTROL_KEYS:
        if other != key:
            assert getattr(loaded.settings, other) is False, other
    assert json.loads(settings_path.read_text(encoding="utf-8"))[key] is True


def test_turning_a_runner_switch_back_off_removes_its_key(settings_path):
    store = SettingsStore(settings_path)
    store.save(Settings(zotero_autostart=True, tunnel_control_enabled=True))
    store.save(Settings(zotero_autostart=True, tunnel_control_enabled=False))

    saved = json.loads(settings_path.read_text(encoding="utf-8"))
    assert saved["zotero_autostart"] is True
    assert "tunnel_control_enabled" not in saved


@pytest.mark.parametrize("key", CONTROL_KEYS)
@pytest.mark.parametrize("value", ["yes", 1, None, []])
def test_a_non_boolean_runner_switch_is_refused_with_a_warning(settings_path, key, value):
    _write(settings_path, json.dumps({key: value}))

    loaded = SettingsStore(settings_path).load()

    assert getattr(loaded.settings, key) is False
    assert f"{key} must be true or false" in loaded.warning


def test_a_saved_switch_keeps_every_other_field(settings_path):
    store = SettingsStore(settings_path)
    store.save(Settings(local_url="http://127.0.0.1:7999", zotero_autostart=True))

    loaded = store.load().settings
    assert loaded.local_url == "http://127.0.0.1:7999"
    assert loaded.zotero_autostart is True
    assert loaded.local_server_autostart is False
