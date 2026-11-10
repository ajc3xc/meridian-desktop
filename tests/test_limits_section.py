"""Tests for the Limits block on the Settings tab (meridian_desktop/ui/limits_section.py), with pytest-qt.

Offscreen Qt (conftest). Every slider saves to a settings file in pytest's temp dir, so nothing touches the real one.
"""

from __future__ import annotations

import json

import pytest
from PySide6.QtWidgets import QAbstractSlider, QLabel, QSlider, QToolButton

from meridian_desktop.settings_store import LIMIT_KEYS, LIMIT_SPECS, SETTINGS_UNREADABLE_TEXT, SettingsStore
from meridian_desktop.ui.limits_section import LIMIT_ROWS, LIMITS_INTRO, LIMITS_TITLE, NOT_SAVED_TEXT, LimitsSection, value_text
from meridian_desktop.ui.settings_group import CollapsibleGroup
from meridian_desktop.ui.settings_tab import SettingsTab

SPEC = {spec.key: spec for spec in LIMIT_SPECS}
UNIT = {row.key: row.unit for row in LIMIT_ROWS}


def _saved(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _slider(section: LimitsSection, key: str) -> QSlider:
    found = section.findChild(QSlider, f"limit-slider-{key}")
    assert found is not None, key
    return found


def _value(section: LimitsSection, key: str) -> str:
    return section.findChild(QLabel, f"limit-value-{key}").text()


def _status(section: LimitsSection) -> QLabel:
    return section.findChild(QLabel, "limits-status")


@pytest.fixture
def section(qtbot, settings_path):
    widget = LimitsSection(SettingsStore(settings_path))
    qtbot.addWidget(widget)
    return widget


# ----------------------------------------------------------- layout and the saved values


def test_there_is_one_slider_per_limit_with_its_default_and_its_value_beside_it(section):
    assert len(section.findChildren(QSlider)) == len(LIMIT_KEYS) == 6
    for spec in LIMIT_SPECS:
        slider = _slider(section, spec.key)
        assert slider.minimum() == spec.minimum
        assert slider.maximum() == spec.maximum
        assert slider.value() == spec.default
        assert _value(section, spec.key) == value_text(spec.default, UNIT[spec.key])


def test_each_limit_shows_the_range_it_can_be_set_to(section):
    for spec in LIMIT_SPECS:
        label = section.findChild(QLabel, f"limit-range-{spec.key}")
        assert label.text() == f"{spec.minimum} to {spec.maximum} {UNIT[spec.key]}"


def test_the_intro_says_enforcement_is_on_the_server_and_not_active_yet(section):
    intro = section.findChild(QLabel, "limits-intro")
    assert intro.text() == LIMITS_INTRO
    assert "server" in intro.text()
    assert "not active yet" in intro.text()


def test_showing_the_saved_values_saves_nothing(section, settings_path):
    section.refresh()
    assert not settings_path.exists()


def test_a_value_saved_in_the_file_shows_on_the_slider_when_the_block_is_built(qtbot, settings_path):
    SettingsStore(settings_path).set_limit("limit_writes", 77)
    section = LimitsSection(SettingsStore(settings_path))
    qtbot.addWidget(section)
    assert _slider(section, "limit_writes").value() == 77
    assert _value(section, "limit_writes") == "77 writes"


def test_an_out_of_range_value_in_the_file_shows_the_clamped_value(qtbot, settings_path):
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({"limit_file_mb": 99999, "limit_claims": 0}), encoding="utf-8")
    section = LimitsSection(SettingsStore(settings_path))
    qtbot.addWidget(section)
    assert _slider(section, "limit_file_mb").value() == SPEC["limit_file_mb"].maximum
    assert _value(section, "limit_file_mb") == "500 MB"
    assert _slider(section, "limit_claims").value() == SPEC["limit_claims"].minimum
    assert _value(section, "limit_claims") == "1 claims"


# ----------------------------------------------------------- saving from the sliders


def test_moving_a_slider_saves_its_value_and_shows_it(section, settings_path):
    _slider(section, "limit_file_mb").setValue(180)
    assert _saved(settings_path)["limit_file_mb"] == 180
    assert _value(section, "limit_file_mb") == "180 MB"
    assert _status(section).isHidden()


def test_a_keyboard_step_saves_the_new_value(section, settings_path):
    slider = _slider(section, "limit_claims")
    slider.triggerAction(QAbstractSlider.SliderAction.SliderSingleStepAdd)
    assert slider.value() == SPEC["limit_claims"].default + 1
    assert _saved(settings_path)["limit_claims"] == SPEC["limit_claims"].default + 1
    assert _value(section, "limit_claims") == f"{SPEC['limit_claims'].default + 1} claims"


def test_a_drag_shows_the_moving_value_and_saves_once_on_release(section, settings_path):
    slider = _slider(section, "limit_writes")
    slider.setSliderDown(True)
    slider.setSliderPosition(333)
    assert _value(section, "limit_writes") == "333 writes"
    assert not settings_path.exists(), "nothing is saved while the slider is still held"
    slider.setSliderDown(False)
    assert _saved(settings_path)["limit_writes"] == 333


def test_a_refused_save_says_so_and_the_slider_goes_back_to_the_saved_value(qtbot, settings_path, monkeypatch):
    store = SettingsStore(settings_path)
    section = LimitsSection(store)
    qtbot.addWidget(section)

    def refuse(settings):
        raise OSError("disk full")

    monkeypatch.setattr(store, "save", refuse)
    _slider(section, "limit_file_mb").setValue(400)
    assert not _status(section).isHidden()
    assert _status(section).text() == NOT_SAVED_TEXT.format(name="OSError")
    assert _slider(section, "limit_file_mb").value() == SPEC["limit_file_mb"].default
    assert _value(section, "limit_file_mb") == "50 MB"
    assert not settings_path.exists()


def test_an_unreadable_settings_file_is_not_overwritten_by_a_slider(section, settings_path):
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text("[1, 2, 3]", encoding="utf-8")
    _slider(section, "limit_zip_download_mb").setValue(4000)
    assert _status(section).text() == SETTINGS_UNREADABLE_TEXT
    assert not _status(section).isHidden()
    assert settings_path.read_text(encoding="utf-8") == "[1, 2, 3]"
    assert settings_path.with_name(settings_path.name + ".unreadable.bak").read_text(encoding="utf-8") == "[1, 2, 3]"
    assert _slider(section, "limit_zip_download_mb").value() == SPEC["limit_zip_download_mb"].default


def test_a_successful_save_clears_an_earlier_message(qtbot, settings_path, monkeypatch):
    store = SettingsStore(settings_path)
    section = LimitsSection(store)
    qtbot.addWidget(section)
    real_save = store.save

    def refuse(settings):
        raise OSError("busy")

    monkeypatch.setattr(store, "save", refuse)
    _slider(section, "limit_file_mb").setValue(90)
    assert not _status(section).isHidden()

    monkeypatch.setattr(store, "save", real_save)
    _slider(section, "limit_file_mb").setValue(91)
    assert _status(section).isHidden()
    assert _saved(settings_path)["limit_file_mb"] == 91


# ----------------------------------------------------------- the Settings tab


def test_the_settings_tab_holds_a_folded_limits_group(qtbot, settings_path):
    tab = SettingsTab(SettingsStore(settings_path))
    qtbot.addWidget(tab)
    toggle = tab.findChild(QToolButton, "settings-group-limits-toggle")
    assert toggle is not None
    assert toggle.text() == LIMITS_TITLE
    assert tab.findChild(CollapsibleGroup, "settings-group-limits").is_expanded() is False
    assert tab.findChild(LimitsSection, "limits-section") is not None


def test_the_settings_tab_refresh_shows_a_limit_saved_elsewhere(qtbot, settings_path):
    tab = SettingsTab(SettingsStore(settings_path))
    qtbot.addWidget(tab)
    SettingsStore(settings_path).set_limit("limit_claims", 45)
    tab.refresh()
    assert tab.findChild(QSlider, "limit-slider-limit_claims").value() == 45
    assert tab.findChild(QLabel, "limit-value-limit_claims").text() == "45 claims"
