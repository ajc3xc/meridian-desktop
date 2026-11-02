"""Tests for the Writing area in Settings (meridian_desktop/ui/writing_area.py), with pytest-qt.

The area reads nothing and sends nothing, so these tests need no socket and no fake server. The window test builds
the real MainWindow with fake data sources, the same way test_smoke does, so nothing touches the network or the disk.
Offscreen Qt, from conftest.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QWidget

from _fakes import MemoryKeyring
from _shared_text import MARKUP_LINE, assert_shows_literally
from meridian_desktop import local_client, projects_reader
from meridian_desktop.data_source import DataSources
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.ui.main_window import TAB_NAMES, MainWindow
from meridian_desktop.ui.text import SELECTABLE_TEXT
from meridian_desktop.ui.writing_area import DOCX_LINE, SLIDES_LINE, WRITING_SECTION, WritingArea


def _unexpected(*_args, **_kwargs):
    raise AssertionError("the Writing area must not reach the network or the data sources")


def _sources():
    return DataSources(
        probe=lambda url: local_client.HEALTH_NOT_RUNNING,
        read_server=_unexpected,
        read_file=projects_reader.list_projects,
        get_token=lambda: None,
        list_hosted=_unexpected,
    )


def test_docx_and_slides_each_say_not_connected_with_the_missing_route(qtbot):
    area = WritingArea()
    qtbot.addWidget(area)
    docx = area.findChild(QLabel, "writing-docx")
    slides = area.findChild(QLabel, "writing-slides")
    assert docx is not None and slides is not None
    assert docx.text() == DOCX_LINE
    assert slides.text() == SLIDES_LINE
    assert docx.text().startswith("DOCX: Not connected.")
    assert "no DOCX route yet" in docx.text()
    assert slides.text().startswith("Slides: Not connected.")
    assert "no Slides route yet" in slides.text()


def test_writing_labels_are_plain_text_and_selectable(qtbot):
    area = WritingArea()
    qtbot.addWidget(area)
    labels = area.findChildren(QLabel)
    # The three lines are one rich-text block, so one drag selects them all. Each line in it is inserted as plain text,
    # and each line keeps its own name as a hidden label.
    assert {label.objectName() for label in labels} == {"writing-text", "writing-intro", "writing-docx", "writing-slides"}
    for label in labels:
        if label.objectName() != "writing-text":
            assert label.textFormat() == Qt.TextFormat.PlainText
        assert label.textInteractionFlags() & SELECTABLE_TEXT == SELECTABLE_TEXT, label.objectName()


def test_a_markup_looking_line_in_the_writing_block_shows_its_characters_and_no_bold(qtbot):
    # The block is rich text, so a line in it must still show markup-looking text as its own characters.
    area = WritingArea()
    qtbot.addWidget(area)
    intro = area.findChild(QLabel, "writing-intro")
    intro.setText(MARKUP_LINE)
    assert intro.textFormat() == Qt.TextFormat.PlainText
    assert_shows_literally(area.findChild(QLabel, "writing-text"), MARKUP_LINE)


def test_settings_page_holds_the_writing_area_and_still_seven_tabs(qtbot, settings_path):
    window = MainWindow(SettingsStore(settings_path), secret_store=KeyringSecretStore(backend=MemoryKeyring()),
                        data_sources=_sources())
    qtbot.addWidget(window)
    # The Writing area is a section of Settings, never an eighth main tab.
    assert TAB_NAMES == ("Home", "Projects", "Sessions", "Code", "Outputs", "Research", "Settings")
    assert window.tabs.count() == 7
    names = [window.tabs.tabText(index) for index in range(window.tabs.count())]
    page = window.tabs.widget(names.index("Settings"))
    body = page.findChild(QWidget, "settings-page")
    area = body.findChild(WritingArea, "writing-area")
    assert area is not None
    assert WRITING_SECTION in [label.text() for label in body.findChildren(QLabel)]
    assert area.findChild(QLabel, "writing-docx") is not None
    assert area.findChild(QLabel, "writing-slides") is not None
