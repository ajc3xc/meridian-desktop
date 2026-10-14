"""Smoke tests for the wave 1 window. Offscreen Qt, no network."""

from PySide6.QtWidgets import QLabel

from meridian_desktop import __main__ as entry
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.ui.main_window import TAB_NAMES, WRITING_SECTIONS, MainWindow

EXPECTED_TABS = ("Projects", "Writing", "Zotero", "Tunnel", "Services", "Code", "Logs", "Settings")
EXPECTED_WAVES = {
    "Projects": 2,
    "Writing": 5,
    "Zotero": 4,
    "Tunnel": 3,
    "Services": 3,
    "Code": 6,
    "Logs": 3,
    "Settings": 2,
}
STATUS = "No services checked (wave 1 makes no network calls)"


def _window(qtbot, settings_path):
    window = MainWindow(SettingsStore(settings_path))
    qtbot.addWidget(window)
    return window


def _tab_names(window):
    return tuple(window.tabs.tabText(i) for i in range(window.tabs.count()))


def _page(window, name):
    return window.tabs.widget(_tab_names(window).index(name))


def _label_texts(widget):
    return [label.text() for label in widget.findChildren(QLabel)]


def test_tab_names_and_order(qtbot, settings_path):
    window = _window(qtbot, settings_path)
    assert _tab_names(window) == EXPECTED_TABS
    assert TAB_NAMES == EXPECTED_TABS
    assert entry.TAB_NAMES == EXPECTED_TABS


def test_placeholders_name_their_wave(qtbot, settings_path):
    window = _window(qtbot, settings_path)
    for name, wave in EXPECTED_WAVES.items():
        assert f"Not built yet (wave {wave})" in _label_texts(_page(window, name)), name


def test_writing_lists_three_named_sections(qtbot, settings_path):
    window = _window(qtbot, settings_path)
    assert WRITING_SECTIONS == (
        "DOCX (meridian-docs)",
        "LaTeX (local engine on 127.0.0.1:8471)",
        "Google Docs (approach not decided)",
    )
    texts = _label_texts(_page(window, "Writing"))
    for section in WRITING_SECTIONS:
        assert section in texts


def test_mode_selector_offers_hosted_and_local(qtbot, settings_path):
    window = _window(qtbot, settings_path)
    combo = window.mode_selector
    assert [combo.itemData(i) for i in range(combo.count())] == ["hosted", "local"]


def test_mode_selector_writes_to_temp_settings_path(qtbot, settings_path):
    window = _window(qtbot, settings_path)
    combo = window.mode_selector
    assert combo.currentData() == "local"
    assert not settings_path.exists()

    combo.setCurrentIndex(combo.findData("hosted"))

    assert settings_path.is_file()
    assert SettingsStore(settings_path).load().settings.mode == "hosted"


def test_mode_selector_reads_saved_mode(qtbot, settings_path):
    SettingsStore(settings_path).set_mode("hosted")
    window = _window(qtbot, settings_path)
    assert window.mode_selector.currentData() == "hosted"


def test_status_bar_text(qtbot, settings_path):
    window = _window(qtbot, settings_path)
    assert window.statusBar().currentMessage() == STATUS


def test_smoke_flag_returns_zero_and_prints_ok(settings_path, capsys):
    code = entry.main(["--smoke"], settings_path=settings_path)
    assert code == 0
    assert "smoke ok" in capsys.readouterr().out
