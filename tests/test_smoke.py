"""Smoke tests for the wave 1 window. Offscreen Qt, no network."""

from PySide6.QtWidgets import QLabel

from meridian_desktop import __main__ as entry
from meridian_desktop import settings_store
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
}
STATUS = "No services checked yet"


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


def test_settings_tab_holds_runpod_panel_not_placeholder(qtbot, settings_path):
    window = _window(qtbot, settings_path)
    page = _page(window, "Settings")
    assert page.objectName() == "runpod-panel"
    assert not any("Not built yet" in text for text in _label_texts(page))


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


def test_failed_mode_save_reverts_selector(qtbot, settings_path, monkeypatch):
    SettingsStore(settings_path).set_mode("hosted")
    before = settings_path.read_bytes()
    window = _window(qtbot, settings_path)
    window.show()
    qtbot.waitExposed(window)
    combo = window.mode_selector
    assert combo.currentData() == "hosted"

    attempts = []
    real_save = SettingsStore.save

    def counting_save(self, settings):
        attempts.append(settings.mode)
        return real_save(self, settings)

    def failing_replace(src, dst):
        # The last step of the atomic save. Failing here leaves settings.json as it was.
        raise OSError("simulated disk failure")

    monkeypatch.setattr(SettingsStore, "save", counting_save)
    monkeypatch.setattr(settings_store.os, "replace", failing_replace)

    combo.setCurrentIndex(combo.findData("local"))

    # One attempt for the user's change. The revert must not trigger a second save.
    assert attempts == ["local"]
    assert combo.currentData() == "hosted"
    assert "could not save mode" in window.settings_warning.text()
    assert window.settings_warning.isVisible()
    assert settings_path.read_bytes() == before
    assert sorted(p.name for p in settings_path.parent.iterdir()) == ["settings.json"]


def test_status_bar_text(qtbot, settings_path):
    window = _window(qtbot, settings_path)
    assert window.statusBar().currentMessage() == STATUS


def test_smoke_flag_returns_zero_and_prints_ok(settings_path, capsys):
    code = entry.main(["--smoke"], settings_path=settings_path)
    assert code == 0
    assert "smoke ok" in capsys.readouterr().out
