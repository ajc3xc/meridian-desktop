"""A group of text that reads as one block is one selectable label, so one drag selects all of it and Ctrl+C copies it.

Each kind of group that became one TextBlock (meridian_desktop/ui/text.py) has one test here. The test drives the group
the way tests/test_copy_selection.py does: a real press, moves and release through the window handle, then Ctrl+C
through the same window. For each group it checks four things:

- the group is one selectable label, and the old label of each of its lines is never shown;
- a left-button drag from its first line to its last line selects all of it;
- Ctrl+C copies all of its visible text, one line per line;
- a refresh that sets the same text again keeps the selection.

Offscreen Qt, fakes only: no socket, no process, no real settings or keychain, and no modal dialog waits for an answer.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QAbstractButton, QLabel, QPushButton, QTableWidget, QToolButton, QVBoxLayout, QWidget

from _fakes import MemoryKeyring
from meridian_desktop import claude_processes
from meridian_desktop.claude_processes import ClaudeProcessesResult
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.ui.client_setup_section import HEADING_TEXT as CLIENT_HEADING_TEXT
from meridian_desktop.ui.client_setup_section import INTRO_TEXT as CLIENT_INTRO_TEXT
from meridian_desktop.ui.logs_page import DESKTOP_HEADING, INTRO_UNREAD_TEXT
from meridian_desktop.ui.runpod_panel import RunPodPanel
from meridian_desktop.ui.scope_controls import TUNNEL_OFF_TEXT
from meridian_desktop.ui.sessions_panel import SessionsPanel
from meridian_desktop.ui.settings_group import CollapsibleGroup
from meridian_desktop.ui.text import SELECTABLE_TEXT, TextBlock, paragraph_tops, text_bottom
from meridian_desktop.ui.writing_area import DOCX_LINE, SLIDES_LINE, WRITING_INTRO, WritingArea
from test_client_setup_section import _section
from test_copy_selection import (  # noqa: F401 - ui_font is an autouse fixture: each test runs in the window's font
    PID,
    _actions,
    _activate,
    _ctrl_c,
    _drag_over,
    _plain,
    _Sessions,
    _window,
    ui_font,
)
from test_logs_page import Recorder, _page, _tail_ok
from test_runpod_panel import FakeClient, FakeStore, wait_idle
from test_scope_controls import _widget
from test_settings_tab import _tab
from test_zotero_panel import FakeZotero, _shown_panel


@dataclass
class Group:
    """One group on screen: the window to drag through, its one label, the lines it shows, the old label of each line,
    and a refresh that sets every line to the text it already has."""

    top: QWidget
    block: QLabel
    lines: list[str]
    old_labels: list[QLabel]
    refresh: Callable[[], None]


def _same_text(labels: list[QLabel]) -> Callable[[], None]:
    """A refresh that sets each line to the text it already shows, as a redraw of unchanged text does."""

    def refresh() -> None:
        for label in labels:
            label.setText(label.text())

    return refresh


def _shown(widget: QWidget) -> QWidget:
    widget.show()
    return widget


def _runpod_panel(qtbot, tmp_path: Path) -> tuple[QWidget, RunPodPanel]:
    container = QWidget()
    qtbot.addWidget(container)
    QVBoxLayout(container)
    container.resize(720, 1000)  # a card squeezed to its minimum would clip its lines, and a drag cannot reach them
    panel = RunPodPanel(SettingsStore(tmp_path / "settings.json"), FakeStore(None), FakeClient(), parent=container)
    container.layout().addWidget(panel)
    _shown(container)
    qtbot.waitExposed(container)
    wait_idle(qtbot, panel)
    return container, panel


def _settings_card_status(qtbot, tmp_path: Path, settings_path: Path) -> Group:
    """The whole RunPod card before any check: the heading, the server and key status lines, and the line for no pods.

    The card is one block, so these lines are in the block's one drag, under the heading and above the buttons.
    """
    container, panel = _runpod_panel(qtbot, tmp_path)
    heading = panel.findChild(QLabel, "keys-runpod-name")
    server = panel.findChild(QLabel, "runpod-server-status")
    key = panel.findChild(QLabel, "runpod-key-status")
    pods_empty = panel.findChild(QLabel, "runpod-pods-empty")
    old = [heading, server, key, pods_empty]
    lines = [label.text() for label in old]
    # The key check sets the key status again, to the same text when no key is stored.
    return Group(
        container,
        panel.findChild(QLabel, "runpod-status"),
        lines,
        old,
        lambda: panel._on_key_checked(False, None),  # noqa: SLF001
    )


def _settings_card_result(qtbot, tmp_path: Path, settings_path: Path) -> Group:
    """The whole RunPod card after Test key sets its result line: every line of the card, in its one block.

    Test key is pressed on the card, so the result is the one the panel really shows, not a line set by the test.
    """
    container, panel = _runpod_panel(qtbot, tmp_path)
    panel.findChild(QPushButton, "runpod-test-key").click()
    wait_idle(qtbot, panel)
    old = [
        panel.findChild(QLabel, "keys-runpod-name"),
        panel.findChild(QLabel, "runpod-server-status"),
        panel.findChild(QLabel, "runpod-key-status"),
        panel.findChild(QLabel, "runpod-result"),
        panel.findChild(QLabel, "runpod-pods-empty"),
    ]
    lines = [label.text() for label in old]
    assert lines[3], "Test key sets the result line"
    return Group(container, panel.findChild(QLabel, "runpod-status"), lines, old, _same_text(old))


def _control_status(qtbot, tmp_path: Path, settings_path: Path) -> Group:
    """The gate note and the last result under a scope's Start, Stop and Restart buttons."""
    actions = _actions(SettingsStore(settings_path), asked=None)
    widget = _widget(qtbot, actions, gate=lambda core: (False, TUNNEL_OFF_TEXT))
    widget.resize(720, 400)
    _shown(widget)
    qtbot.waitExposed(widget)
    widget._show("Start cancelled. Nothing was started.")  # noqa: SLF001
    note = widget.findChild(QLabel, "tunnel-control-note")
    result = widget.findChild(QLabel, "tunnel-control-result")
    lines = [note.text(), result.text()]
    return Group(widget, widget.findChild(QLabel, "tunnel-control-text"), lines, [note, result], widget.refresh_enabled)


def _settings_status(qtbot, tmp_path: Path, settings_path: Path) -> Group:
    """The Zotero intro and its status lines, on the Settings page."""
    panel = _shown_panel(qtbot, FakeZotero())
    panel.resize(720, 700)
    names = ("zotero-intro", "zotero-installed", "zotero-process", "zotero-api", "zotero-api-detail", "zotero-start-hint")
    old = [panel.findChild(QLabel, name) for name in names]
    lines = [label.text() for label in old if label.text()]
    return Group(panel, panel.findChild(QLabel, "zotero-status-text"), lines, old, _same_text(old))


def _settings_text(qtbot, tmp_path: Path, settings_path: Path) -> Group:
    """The Writing intro and its DOCX and Slides lines, in Settings."""
    area = WritingArea()
    qtbot.addWidget(area)
    area.resize(720, 300)
    _shown(area)
    qtbot.waitExposed(area)
    old = [area.findChild(QLabel, name) for name in ("writing-intro", "writing-docx", "writing-slides")]
    lines = [WRITING_INTRO, DOCX_LINE, SLIDES_LINE]
    return Group(area, area.findChild(QLabel, "writing-text"), lines, old, _same_text(old))


def _top_bar_detail(qtbot, tmp_path: Path, settings_path: Path) -> Group:
    """The mode description and the tunnel detail line under the Mode row."""
    window = _window(qtbot, tmp_path)
    window.resize(1000, 800)
    _activate(window)
    window.tunnel_detail.setText("The tunnel detail names the tools that need the tunnel.")
    window.tunnel_detail.setVisible(True)
    old = [window.mode_description, window.tunnel_detail]
    lines = [window.mode_description.text(), window.tunnel_detail.text()]
    return Group(window, window.findChild(QLabel, "mode-text"), lines, old, _same_text(old))


def _logs_intro(qtbot, tmp_path: Path, settings_path: Path) -> Group:
    """The Logs intro and the desktop log's heading."""
    page = _page(qtbot, tunnel=Recorder(_tail_ok([], "tunnel.log")))
    page.resize(720, 600)
    _shown(page)
    qtbot.waitExposed(page)
    intro = page.findChild(QLabel, "logs-intro")
    heading = page.findChild(QLabel, "logs-desktop-heading")
    assert intro.text() == INTRO_UNREAD_TEXT
    assert heading.text() == f"{DESKTOP_HEADING} not read yet."
    lines = [intro.text(), heading.text()]
    return Group(page, page.findChild(QLabel, "logs-intro-text"), lines, [intro, heading], _same_text([intro, heading]))


def _client_intro(qtbot, tmp_path: Path, settings_path: Path) -> Group:
    """The Client setup heading, its intro, and the search result, at the top of the section."""
    section = _section(qtbot, SettingsStore(settings_path))
    section.resize(720, 900)
    _shown(section)
    qtbot.waitExposed(section)
    heading = section.findChild(QLabel, "client-setup-heading")
    intro = section.findChild(QLabel, "client-setup-intro")
    finder = section.findChild(QLabel, "client-setup-finder-status")
    assert heading.text() == CLIENT_HEADING_TEXT
    assert intro.text() == CLIENT_INTRO_TEXT
    lines = [heading.text(), intro.text()] + ([finder.text()] if finder.text() else [])
    old = [heading, intro, finder]
    return Group(section, section.findChild(QLabel, "client-setup-intro-text"), lines, old, _same_text(old))


def _client_card(qtbot, tmp_path: Path, settings_path: Path) -> Group:
    """One client's path and detail lines, under its name and state row."""
    section = _section(qtbot, SettingsStore(settings_path))
    section.resize(720, 900)
    _shown(section)
    qtbot.waitExposed(section)
    path = section.findChild(QLabel, "client-setup-claude-code-path")
    detail = section.findChild(QLabel, "client-setup-claude-code-detail")
    path.setText("Settings file: an example path in the test")
    detail.setText("Meridian has no entries in this file yet.")
    lines = [path.text(), detail.text()]
    return Group(
        section,
        section.findChild(QLabel, "client-setup-claude-code-text"),
        lines,
        [path, detail],
        _same_text([path, detail]),
    )


def _sessions_status(qtbot, tmp_path: Path, settings_path: Path) -> Group:
    """The Sessions status line and the time it was checked."""
    local = _Sessions()
    panel = SessionsPanel(
        lambda: "http://127.0.0.1:7878",
        lambda: ("https://hosted.invalid", None),
        lambda: [(PID, "Alpha")],
        lambda: "local",
        local_transport=local,
        process_lister=lambda: ClaudeProcessesResult(claude_processes.LISTED, "", ()),
    )
    qtbot.addWidget(panel)
    panel.resize(1000, 700)
    _activate(panel)
    table = panel.findChild(QTableWidget, "sessions-table")
    qtbot.waitUntil(lambda: panel._in_flight is None and table.rowCount() == 3, timeout=5000)  # noqa: SLF001
    panel._timer.stop()  # noqa: SLF001 - the test sends no ticks
    status = panel.findChild(QLabel, "sessions-status")
    checked = panel.findChild(QLabel, "sessions-checked")
    old = [status, checked]
    lines = [status.text(), checked.text()]
    return Group(panel, panel.findChild(QLabel, "sessions-status-text"), lines, old, _same_text(old))


def _diagnostics(qtbot, tmp_path: Path, settings_path: Path) -> Group:
    """The Diagnostics intro and the settings file line, in an expanded Diagnostics group."""
    tab = _tab(qtbot, settings_path, KeyringSecretStore(backend=MemoryKeyring()))
    tab.findChild(CollapsibleGroup, "settings-group-diagnostics").set_expanded(True)
    tab.resize(900, 3000)
    _shown(tab)
    qtbot.waitExposed(tab)
    intro = tab.findChild(QLabel, "diagnostics-intro")
    settings_file = tab.findChild(QLabel, "settings-file-path")
    old = [intro, settings_file]
    lines = [intro.text(), settings_file.text()]
    return Group(tab, tab.findChild(QLabel, "diagnostics-text"), lines, old, _same_text(old))


KINDS: dict[str, Callable[..., Group]] = {
    "settings-card-status": _settings_card_status,
    "settings-card-result": _settings_card_result,
    "control-status": _control_status,
    "settings-status": _settings_status,
    "settings-text": _settings_text,
    "top-bar-detail": _top_bar_detail,
    "logs-intro": _logs_intro,
    "client-setup-intro": _client_intro,
    "client-setup-card": _client_card,
    "sessions-status": _sessions_status,
    "diagnostics-text": _diagnostics,
}


@pytest.mark.parametrize("kind", list(KINDS), ids=list(KINDS))
def test_the_group_is_one_selectable_label_one_drag_selects_all_of_it_and_ctrl_c_copies_it(
    qtbot, tmp_path, settings_path, kind
):
    group = KINDS[kind](qtbot, tmp_path, settings_path)
    block = group.block
    expected = "\n".join(group.lines)
    assert len(group.lines) >= 2, "a block holds more than one line"

    # One selectable label holds every line. Each old line label is visible to Qt when it is shown, but it has no size
    # of its own, so it draws nothing: its block draws its text.
    assert block.textInteractionFlags() == SELECTABLE_TEXT
    assert block.isVisible()
    assert [label.objectName() for label in group.old_labels if not label.size().isEmpty()] == []

    # A left-button drag from the first line to the last selects all of it, and Ctrl+C copies it. The drag needs the
    # block laid out at its full height: a line shown since the group was built is placed by the next layout pass.
    _activate(group.top)
    qtbot.waitUntil(lambda: block.height() >= text_bottom(block), timeout=5000)
    _drag_over(group.top, block)
    assert block.hasSelectedText()
    assert _plain(block.selectedText()) == expected
    assert _ctrl_c(group.top) == expected

    # A refresh that sets the same text again keeps the selection.
    group.refresh()
    assert block.hasSelectedText()
    assert _plain(block.selectedText()) == expected


@pytest.mark.parametrize("kind", ["client-setup-card", "top-bar-detail"])
def test_the_second_line_starts_the_gap_a_layout_gives_labels_below_the_first(qtbot, tmp_path, settings_path, kind):
    """A block's lines keep the gap a layout gives two labels on this style, 6 px, as they had as labels of their own.

    On run/desk-fix-integrate (3aad775) the path and the detail were two labels in a layout, 6 px apart. A block takes its
    gap as a number, not from a layout that is not parented yet, so this checks the laid-out text: the second line starts
    one line of text below the first, plus the parented layout's spacing.
    """
    group = KINDS[kind](qtbot, tmp_path, settings_path)
    block = group.block
    _activate(group.top)
    tops = paragraph_tops(block)
    assert len(tops) >= 2, "a block holds more than one line"
    stacked = QWidget()  # the parent of the reference layout: a layout's spacing() is set only while it has one
    gap = QVBoxLayout(stacked).spacing()
    assert gap == 6, "this style's layout gap between two labels is 6 px"
    assert tops[1] - tops[0] == QFontMetrics(block.font()).lineSpacing() + gap


# The button counts with every Settings group expanded. They were counted at run/desk-fix-integrate (3aad775), before the
# block changes, and the block changes left the Settings page as it was. The Settings page held 34 QPushButtons and five
# group headers, which are QToolButtons: 39 buttons in all. Before the Search tab split the main window held 54
# QPushButtons and 71 buttons in all (every QAbstractButton). The split replaces the one Search tab with three search
# tabs, so it adds two search tabs, and each search tab holds one search button (a QPushButton) and one table corner
# button (a QAbstractButton). The main window then held 56 QPushButtons and 75 buttons in all.
# The keys file block adds two push buttons to the Settings page: Choose file and Stop using this file. They are the
# reason the Settings page held 36 QPushButtons and five group headers, 41 buttons in all. The Settings page is in
# the main window, so the main window held the same two more: 58 QPushButtons and 77 buttons in all.
# The Update bridge button (object name zotero-bridge-update) is built in the app now: the Settings Zotero panel is
# given the bridge and the Local URL reader (run/desk-bridge-updater). It is one more QPushButton, and one more
# QAbstractButton, on the Settings page and in the main window, so each count goes up by one more. The Settings page
# now holds 37 QPushButtons and 42 buttons in all, and the main window holds 59 QPushButtons and 78 buttons in all.
# The Overleaf (LaTeX) card on Home (run/desk-overleaf-login) adds four push buttons to the main window, and so four
# QAbstractButtons: Check, Log in and Start engine, plus the card's own action button, which the card keeps hidden as every
# card does. The Settings page does not change. The main window now holds 63 QPushButtons and 82 buttons in all.
# The Overleaf card's launch-time checks and folder choice (run/desk-overleaf-autostart) add one more push button, Choose
# engine folder, and two check boxes, the two switches. A check box is a QAbstractButton but not a QPushButton, so the push
# count goes up by one and the button count by three. The Settings page does not change. The main window now holds 64
# QPushButtons and 85 buttons in all.
SETTINGS_PUSH_BUTTONS = 37
SETTINGS_BUTTONS = 42
MAIN_WINDOW_PUSH_BUTTONS = 64
MAIN_WINDOW_BUTTONS = 85


def test_the_settings_and_main_window_button_counts_match_after_the_search_split_and_block_change(qtbot, tmp_path):
    window = _window(qtbot, tmp_path)
    groups = window.findChildren(CollapsibleGroup)
    assert len(groups) == 5, "the five Settings groups"
    for group in groups:
        group.set_expanded(True)
    names = [window.tabs.tabText(index) for index in range(window.tabs.count())]
    settings = window.tabs.widget(names.index("Settings"))
    push = settings.findChildren(QPushButton)
    headers = settings.findChildren(QToolButton)
    assert len(headers) == len(groups), "the group headers are the only tool buttons on the Settings page"
    assert len(push) == SETTINGS_PUSH_BUTTONS
    assert len(push) + len(headers) == SETTINGS_BUTTONS
    assert len(window.findChildren(QPushButton)) == MAIN_WINDOW_PUSH_BUTTONS
    assert len(window.findChildren(QAbstractButton)) == MAIN_WINDOW_BUTTONS


def test_a_block_line_reports_its_visibility_to_qt_and_draws_nothing_of_its_own(qtbot):
    """A line is hidden to Qt when its owner hides it, and visible to Qt when its owner shows it, so isHidden() and
    isVisible() are Qt's own answers. A line keeps no size of its own: its block draws its text, and only shown lines
    are drawn.
    """
    owner = QWidget()
    qtbot.addWidget(owner)
    block = TextBlock(owner, "visibility-text")
    shown = block.line("visibility-shown", "Shown line")
    hidden = block.line("visibility-hidden", "Hidden line")
    hidden.setVisible(False)
    assert hidden.isHidden() and not hidden.isVisible()
    assert not shown.isHidden() and not shown.isVisible(), "a line is not visible until its owner is shown"

    owner.show()
    qtbot.waitExposed(owner)
    assert shown.isVisible() and not shown.isHidden()
    assert hidden.isHidden() and not hidden.isVisible()

    hidden.show()
    assert hidden.isVisible() and not hidden.isHidden()
    hidden.hide()
    assert hidden.isHidden() and not hidden.isVisible()

    late = block.line("visibility-late", "Late line")
    assert late.isVisible() and not late.isHidden(), "a line added to a shown owner is visible at once"
    assert shown.size().isEmpty() and late.size().isEmpty(), "a line keeps no size of its own"
    assert len(paragraph_tops(block.label)) == 2, "the block draws the two shown lines and not the hidden one"
